#!/usr/bin/env python3

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
from typing import Any


try:
    import dbus  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - only needed on the speaker
    dbus = None  # type: ignore[assignment]


DBUS_BUS = "org.freedesktop.DBus"
DBUS_PROPERTIES = "org.freedesktop.DBus.Properties"
MPRIS_PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_BUS_PREFIX = "org.mpris.MediaPlayer2.spotifyd."

DEFAULT_EPSILON = 0.005
DEFAULT_POLL_SECONDS = 0.25


def clamp_volume(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class VolumeSyncState:
    """Pure synchronization policy used by the runtime and unit tests."""

    def __init__(self, epsilon: float = DEFAULT_EPSILON) -> None:
        self.epsilon = epsilon
        self.pipewire_volume: float | None = None
        self.mpris_volumes: dict[str, float] = {}
        self.pending_players: set[str] = set()

    def initialize(self, pipewire_volume: float) -> None:
        self.pipewire_volume = clamp_volume(pipewire_volume)

    def mark_player_pending(self, player: str) -> None:
        self.pending_players.add(player)

    def add_player(self, player: str, current_volume: float) -> list[tuple[Any, ...]]:
        self.pending_players.discard(player)
        current = clamp_volume(current_volume)
        self.mpris_volumes[player] = current

        if self.pipewire_volume is None:
            return []

        if abs(current - self.pipewire_volume) <= self.epsilon:
            self.mpris_volumes[player] = self.pipewire_volume
            return []

        self.mpris_volumes[player] = self.pipewire_volume
        return [("mpris", player, self.pipewire_volume)]

    def remove_player(self, player: str) -> None:
        self.pending_players.discard(player)
        self.mpris_volumes.pop(player, None)

    def mpris_changed(self, player: str, volume: float) -> list[tuple[Any, ...]]:
        if player in self.pending_players:
            return []

        value = clamp_volume(volume)
        previous = self.mpris_volumes.get(player)
        self.mpris_volumes[player] = value

        if previous is not None and abs(previous - value) <= self.epsilon:
            return []

        if self.pipewire_volume is not None and abs(self.pipewire_volume - value) <= self.epsilon:
            self.pipewire_volume = value
            return []

        self.pipewire_volume = value
        return [("pipewire", value)]

    def pipewire_changed(self, volume: float) -> list[tuple[Any, ...]]:
        value = clamp_volume(volume)

        if self.pipewire_volume is not None and abs(self.pipewire_volume - value) <= self.epsilon:
            return []

        self.pipewire_volume = value
        actions: list[tuple[Any, ...]] = []
        for player, previous in self.mpris_volumes.items():
            if abs(previous - value) > self.epsilon:
                self.mpris_volumes[player] = value
                actions.append(("mpris", player, value))
        return actions


class VolumeSynchronizer:
    def __init__(
        self,
        bus: Any,
        *,
        wpctl: str = "/usr/bin/wpctl",
        sink: str = "@DEFAULT_AUDIO_SINK@",
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.bus = bus
        self.wpctl = wpctl
        self.sink = sink
        self.poll_milliseconds = max(50, int(poll_seconds * 1000))
        self.state = VolumeSyncState()
        self.players: dict[str, str] = {}
        self.logger = logging.getLogger("volume-sync")

    def start(self) -> None:
        self._discover_players()

        pipewire_volume = self._read_pipewire_volume()
        if pipewire_volume is None:
            raise RuntimeError("unable to read the PipeWire default sink volume")
        self.state.initialize(pipewire_volume)

        for player in list(self.players):
            self._attach_player(player)

        self._install_dbus_receivers()
        GLib.timeout_add(self.poll_milliseconds, self._poll_pipewire)
        GLib.timeout_add_seconds(2, self._discover_players)

        self.logger.info(
            "Volume synchronizer ready: sink=%s initial_volume=%.3f",
            self.sink,
            pipewire_volume,
        )

    def _install_dbus_receivers(self) -> None:
        self.bus.add_signal_receiver(
            self._on_properties_changed,
            dbus_interface=DBUS_PROPERTIES,
            signal_name="PropertiesChanged",
            path=MPRIS_PATH,
            sender_keyword="sender",
        )
        self.bus.add_signal_receiver(
            self._on_name_owner_changed,
            dbus_interface=DBUS_BUS,
            signal_name="NameOwnerChanged",
        )

    def _discover_players(self) -> bool:
        dbus_object = self.bus.get_object(DBUS_BUS, "/org/freedesktop/DBus")
        dbus_interface = dbus.Interface(dbus_object, DBUS_BUS)

        for raw_name in dbus_interface.ListNames():
            name = str(raw_name)
            if name.startswith(MPRIS_BUS_PREFIX):
                self._register_player(name)
        return True

    def _register_player(self, name: str) -> None:
        try:
            dbus_object = self.bus.get_object(DBUS_BUS, "/org/freedesktop/DBus")
            dbus_interface = dbus.Interface(dbus_object, DBUS_BUS)
            owner = str(dbus_interface.GetNameOwner(name))
        except dbus.exceptions.DBusException:
            return

        if self.players.get(name) == owner:
            return

        self.players[name] = owner
        self.state.mark_player_pending(name)
        GLib.idle_add(self._attach_player, name)

    def _on_name_owner_changed(self, name: str, old_owner: str, new_owner: str) -> None:
        name = str(name)
        if not name.startswith(MPRIS_BUS_PREFIX):
            return

        if new_owner:
            self._register_player(name)
            return

        self.players.pop(name, None)
        self.state.remove_player(name)
        self.logger.info("spotifyd MPRIS player disappeared: %s", name)

    def _attach_player(self, player: str) -> bool:
        if player not in self.players:
            return False

        try:
            current_volume = self._read_player_volume(player)
        except dbus.exceptions.DBusException as error:
            self.logger.warning("Cannot read %s volume: %s", player, error)
            return False

        self.logger.info("spotifyd MPRIS player available: %s volume=%.3f", player, current_volume)
        self._apply_actions(self.state.add_player(player, current_volume))
        return False

    def _on_properties_changed(
        self,
        interface_name: str,
        changed_properties: dict[str, Any],
        invalidated_properties: list[str],
        *,
        sender: str | None = None,
    ) -> None:
        del invalidated_properties
        if str(interface_name) != MPRIS_PLAYER_INTERFACE or "Volume" not in changed_properties:
            return

        player = self._player_for_sender(sender)
        if player is None:
            return

        volume = float(changed_properties["Volume"])
        self.logger.info("MPRIS volume change: %s -> %.3f", player, volume)
        self._apply_actions(self.state.mpris_changed(player, volume))

    def _player_for_sender(self, sender: str | None) -> str | None:
        if sender is None:
            return None
        sender = str(sender)
        for player, owner in self.players.items():
            if sender in {player, owner}:
                return player
        return None

    def _poll_pipewire(self) -> bool:
        volume = self._read_pipewire_volume()
        if volume is not None:
            actions = self.state.pipewire_changed(volume)
            if actions:
                self.logger.info("PipeWire volume change: %.3f", volume)
                self._apply_actions(actions)
        return True

    def _read_pipewire_volume(self) -> float | None:
        try:
            result = subprocess.run(
                [self.wpctl, "get-volume", self.sink],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            self.logger.error("Cannot read PipeWire volume: %s", error)
            return None

        if result.returncode != 0:
            self.logger.error("wpctl get-volume failed: %s", result.stderr.strip())
            return None

        match = re.search(r"Volume:\s+([0-9]+(?:\.[0-9]+)?)", result.stdout)
        if match is None:
            self.logger.error("Unexpected wpctl output: %r", result.stdout.strip())
            return None
        return clamp_volume(float(match.group(1)))

    def _set_pipewire_volume(self, volume: float) -> None:
        try:
            result = subprocess.run(
                [self.wpctl, "set-volume", self.sink, f"{volume:.4f}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            self.logger.error("Cannot set PipeWire volume: %s", error)
            return

        if result.returncode != 0:
            self.logger.error("wpctl set-volume failed: %s", result.stderr.strip())

    def _read_player_volume(self, player: str) -> float:
        dbus_object = self.bus.get_object(player, MPRIS_PATH)
        properties = dbus.Interface(dbus_object, DBUS_PROPERTIES)
        return clamp_volume(float(properties.Get(MPRIS_PLAYER_INTERFACE, "Volume")))

    def _set_player_volume(self, player: str, volume: float) -> None:
        try:
            dbus_object = self.bus.get_object(player, MPRIS_PATH)
            properties = dbus.Interface(dbus_object, DBUS_PROPERTIES)
            properties.Set(
                MPRIS_PLAYER_INTERFACE,
                "Volume",
                dbus.Double(clamp_volume(volume)),
            )
        except dbus.exceptions.DBusException as error:
            self.logger.error("Cannot set %s volume: %s", player, error)

    def _apply_actions(self, actions: list[tuple[Any, ...]]) -> None:
        for action in actions:
            if action[0] == "pipewire":
                self._set_pipewire_volume(float(action[1]))
            elif action[0] == "mpris":
                self._set_player_volume(str(action[1]), float(action[2]))


def main() -> int:
    global GLib

    if dbus is None:
        raise RuntimeError("python3-dbus is required")

    from dbus.mainloop import glib as dbus_glib
    from gi.repository import GLib as _GLib

    GLib = _GLib
    dbus_glib.DBusGMainLoop(set_as_default=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    bus = dbus.SessionBus()
    synchronizer = VolumeSynchronizer(
        bus,
        wpctl=os.getenv("WPCTL_PATH", "/usr/bin/wpctl"),
        sink=os.getenv("VOLUME_SINK", "@DEFAULT_AUDIO_SINK@"),
        poll_seconds=float(os.getenv("VOLUME_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))),
    )
    synchronizer.start()

    loop = GLib.MainLoop()

    def stop(signum: int, _frame: object) -> None:
        logging.info("Stopping after signal %d", signum)
        loop.quit()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    loop.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
