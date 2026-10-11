#!/usr/bin/env python3
"""Display PipeWire's default-sink volume on a ReSpeaker XVF3800 LED ring.

The daemon observes the PipeWire PulseAudio compatibility server.  It reacts to
the resulting sink state, rather than to individual buttons, so volume changes
from GPIO buttons, the desktop, wpctl, Spotify, and other clients share one LED
behaviour.

The XVF3800 USB device is controlled with the vendor-control protocol used by
Seeed's xvf_host.py.  The USB handle remains open while the service runs.
"""

from __future__ import annotations

import argparse
import array
import contextlib
import logging
import math
import os
import queue
import signal
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional, Sequence

import pulsectl
import usb.core
import usb.util


LOG = logging.getLogger("volume-led")

XVF_VID = 0x2886
XVF_PID = 0x001A
XVF_GPO_SERVICER_RESID = 20
XVF_LED_EFFECT_CMD = 12
XVF_LED_GAMMIFY_CMD = 14
XVF_LED_RING_COLOR_CMD = 19
XVF_LED_EFFECT_RING = 5
LED_COUNT = 12


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)), 0)
    except ValueError:
        return default


def parse_rgb(text: str) -> tuple[int, int, int]:
    value = text.strip().removeprefix("#")
    if value.lower().startswith("0x"):
        value = value[2:]
    if len(value) != 6:
        raise ValueError(f"RGB colour must have six hex digits: {text!r}")
    packed = int(value, 16)
    return (packed >> 16) & 0xFF, (packed >> 8) & 0xFF, packed & 0xFF


def blend_rgb(
    first: tuple[int, int, int], second: tuple[int, int, int], fraction: float
) -> tuple[int, int, int]:
    fraction = clamp(fraction, 0.0, 1.0)
    return tuple(
        round(a + (b - a) * fraction) for a, b in zip(first, second)
    )  # type: ignore[return-value]


def scale_rgb(colour: tuple[int, int, int], amount: float) -> int:
    amount = clamp(amount, 0.0, 1.0)
    red, green, blue = (round(channel * amount) for channel in colour)
    return (red << 16) | (green << 8) | blue


def smoothstep(value: float) -> float:
    value = clamp(value, 0.0, 1.0)
    return value * value * (3.0 - 2.0 * value)


@dataclass(frozen=True)
class VolumeState:
    sink_name: str
    volume: float
    muted: bool


class PulseStateReader:
    """Read the current default sink through PipeWire-Pulse."""

    def __init__(self) -> None:
        self._pulse: Optional[pulsectl.Pulse] = None

    def _connect(self) -> pulsectl.Pulse:
        if self._pulse is None:
            self._pulse = pulsectl.Pulse("xvf-volume-led-state")
        return self._pulse

    def close(self) -> None:
        if self._pulse is not None:
            try:
                self._pulse.close()
            except Exception:
                pass
            self._pulse = None

    def read(self) -> Optional[VolumeState]:
        for attempt in range(2):
            try:
                pulse = self._connect()
                sink_name = pulse.server_info().default_sink_name
                if not sink_name:
                    raise RuntimeError("PipeWire-Pulse has no default sink")
                sink = pulse.get_sink_by_name(sink_name)
                channels = list(sink.volume.values)
                volume = max(channels) if channels else 0.0
                return VolumeState(sink_name, max(0.0, volume), bool(sink.mute))
            except Exception as exc:
                self.close()
                if attempt:
                    LOG.warning("Cannot read the default sink: %s", exc)
        return None


class PulseEventWatcher(threading.Thread):
    """Coalesce sink/server subscription events into a one-slot queue."""

    def __init__(self, events: queue.Queue[object]) -> None:
        super().__init__(name="pipewire-pulse-events", daemon=True)
        self._events = events
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _notify(self, _event: object = None) -> None:
        try:
            self._events.put_nowait(object())
        except queue.Full:
            pass

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                with pulsectl.Pulse("xvf-volume-led-events") as pulse:
                    pulse.event_mask_set("sink", "server")
                    pulse.event_callback_set(self._notify)
                    self._notify()
                    LOG.info("Watching PipeWire-Pulse sink events")
                    pulse.event_listen()
            except Exception as exc:
                if not self._stop_event.is_set():
                    LOG.warning("PipeWire-Pulse event connection lost: %s", exc)
                    self._stop_event.wait(1.0)


class AudioLevelMonitor(threading.Thread):
    """Tracks a smoothed 0..1 loudness level of whatever the current default
    sink is playing, by tapping its PipeWire monitor stream with
    ``pw-record`` (no PulseAudio CLI tools are installed on this system, but
    pw-record can target the same sink pulsectl reports and reads its
    monitor with ``stream.capture.sink=true``). This is the assistant's own
    synthesized speech, since that's what the voice agent plays -- used to
    make the "speaking" LED brightness pulse with the actual audio instead
    of staying flat.
    """

    # Capture at the sink's native rate rather than asking pw-record to
    # resample -- a 48kHz -> 8kHz downsample filter's own group delay turned
    # out to be the dominant source of lag between audio and the ring, worse
    # than any of the buffering/smoothing settings below.
    SAMPLE_RATE = 48000
    CHUNK_SAMPLES = 480  # 10ms per read -- finer-grained and lower-latency
    LATENCY = "10ms"  # pw-record's own capture buffering, trimmed down from
    # its 100ms default.

    def __init__(self) -> None:
        super().__init__(name="assistant-audio-level", daemon=True)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._level = 0.0
        self._process: Optional[subprocess.Popen] = None
        self._current_sink: Optional[str] = None

    def level(self) -> float:
        with self._lock:
            return self._level

    def stop(self) -> None:
        self._stop_event.set()
        self._kill_process()

    def _kill_process(self) -> None:
        if self._process is not None:
            with contextlib.suppress(Exception):
                self._process.kill()
            with contextlib.suppress(Exception):
                self._process.wait(timeout=1.0)
            self._process = None

    def _default_sink_name(self) -> Optional[str]:
        try:
            with pulsectl.Pulse("xvf-volume-led-audiolevel") as pulse:
                return pulse.server_info().default_sink_name
        except Exception:
            return None

    def _spawn(self, sink_name: str) -> Optional[subprocess.Popen]:
        try:
            return subprocess.Popen(
                [
                    "pw-record",
                    "--target", sink_name,
                    "-P", "{ stream.capture.sink=true }",
                    "--latency", self.LATENCY,
                    "--rate", str(self.SAMPLE_RATE),
                    "--channels", "1",
                    "--format", "s16",
                    "--raw",
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            LOG.warning("Cannot start pw-record for audio-level metering: %s", exc)
            return None

    def run(self) -> None:
        chunk_bytes = self.CHUNK_SAMPLES * 2
        chunk_seconds = self.CHUNK_SAMPLES / self.SAMPLE_RATE
        # Fast attack so a pulse rises promptly, a quick-ish release so it
        # still tracks speech falling silent without feeling laggy, while
        # staying just slow enough not to flicker to black between syllables.
        attack_alpha = clamp(chunk_seconds / ASSISTANT_LED_SPEECH_ATTACK, 0.0, 1.0)
        release_alpha = clamp(chunk_seconds / ASSISTANT_LED_SPEECH_RELEASE, 0.0, 1.0)
        next_sink_check = 0.0

        while not self._stop_event.is_set():
            now = time.monotonic()
            if self._process is None or now >= next_sink_check:
                next_sink_check = now + 2.0
                sink_name = self._default_sink_name()
                if sink_name and sink_name != self._current_sink:
                    self._kill_process()
                    self._current_sink = sink_name
                if self._process is None and self._current_sink:
                    self._process = self._spawn(self._current_sink)
                    if self._process is None:
                        self._stop_event.wait(1.0)
                        continue

            if self._process is None or self._process.stdout is None:
                self._stop_event.wait(0.5)
                continue

            try:
                chunk = self._process.stdout.read(chunk_bytes)
            except Exception:
                chunk = b""

            if not chunk:
                # pw-record died (sink removed, PipeWire restart, etc) --
                # drop to silence and let the top of the loop respawn it.
                self._kill_process()
                with self._lock:
                    self._level = 0.0
                self._stop_event.wait(0.5)
                continue

            samples = array.array("h")
            samples.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
            if samples:
                mean_square = sum(s * s for s in samples) / len(samples)
                rms = math.sqrt(mean_square) / 32768.0
            else:
                rms = 0.0

            target = clamp(
                rms * ASSISTANT_LED_SPEECH_GAIN, 0.0, 1.0
            ) ** ASSISTANT_LED_SPEECH_GAMMA
            with self._lock:
                alpha = attack_alpha if target > self._level else release_alpha
                self._level += (target - self._level) * alpha

        self._kill_process()


class MicLevelMonitor(threading.Thread):
    """Tracks a smoothed 0..1 loudness level of the user's live voice, by
    tapping the XVF3800 mic-source node directly with its own ``pw-record``
    subprocess. Fully independent of the ``pw-cat`` capture that feeds the
    Gemini Live conversation (``pw_transport.py``) and of ``wake_word.py``'s
    own tap -- mirroring the same "multiple concurrent PipeWire consumers of
    this node" pattern those two already use safely in production. Used to
    make the "listening" LED breathing brighten in sync with how loudly the
    user is actually talking, instead of just breathing on a fixed cycle.

    Unlike ``AudioLevelMonitor``, which polls for "the current default sink"
    and taps its monitor port, this targets a fixed, env-configured
    mic-source node directly, and must pick a single channel in Python since
    the XVF3800's L/R capture channels carry independently configured
    content (the same reason ``pw_transport.py``/``wake_word.py`` each pick
    one channel already).
    """

    SAMPLE_RATE = 48000
    CHUNK_SAMPLES = 480  # 10ms per read -- same cadence as AudioLevelMonitor
    LATENCY = "10ms"
    CHANNELS = 2

    def __init__(self, source_name: str, channel_index: int) -> None:
        super().__init__(name="assistant-mic-level", daemon=True)
        self._source_name = source_name
        self._channel_index = channel_index
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._level = 0.0
        self._process: Optional[subprocess.Popen] = None

    def level(self) -> float:
        with self._lock:
            return self._level

    def stop(self) -> None:
        self._stop_event.set()
        self._kill_process()

    def _kill_process(self) -> None:
        if self._process is not None:
            with contextlib.suppress(Exception):
                self._process.kill()
            with contextlib.suppress(Exception):
                self._process.wait(timeout=1.0)
            self._process = None

    def _wait_for_node(self, timeout: float = 15.0, interval: float = 0.5) -> bool:
        """Poll `pw-dump` for the mic-source node before trusting
        `pw-record --target` to resolve it -- pw-record does not error when
        `--target` fails to resolve, it silently falls back to the default
        source instead (same failure class as pw_transport.py's own
        _wait_for_node). Unlike a dead process, a misdirected one would just
        keep running against the wrong node forever, so this is worth
        waiting for once up front rather than detecting after the fact.
        """
        needle = f'"node.name": "{self._source_name}"'.encode()
        deadline = time.monotonic() + timeout
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            try:
                out = subprocess.run(
                    ["pw-dump"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=5.0,
                ).stdout
            except Exception:
                out = b""
            if needle in out:
                return True
            self._stop_event.wait(interval)
        return False

    def _spawn(self) -> Optional[subprocess.Popen]:
        try:
            return subprocess.Popen(
                [
                    "pw-record",
                    "--target", self._source_name,
                    "--latency", self.LATENCY,
                    "--rate", str(self.SAMPLE_RATE),
                    "--channels", str(self.CHANNELS),
                    "--format", "s16",
                    "--raw",
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            LOG.warning("Cannot start pw-record for mic-level metering: %s", exc)
            return None

    def run(self) -> None:
        chunk_bytes = self.CHUNK_SAMPLES * self.CHANNELS * 2
        chunk_seconds = self.CHUNK_SAMPLES / self.SAMPLE_RATE
        attack_alpha = clamp(chunk_seconds / ASSISTANT_LED_MIC_ATTACK, 0.0, 1.0)
        release_alpha = clamp(chunk_seconds / ASSISTANT_LED_MIC_RELEASE, 0.0, 1.0)

        if not self._wait_for_node():
            if not self._stop_event.is_set():
                LOG.warning(
                    "Mic-source node %r not found; mic-level metering disabled",
                    self._source_name,
                )
            return

        while not self._stop_event.is_set():
            if self._process is None:
                self._process = self._spawn()
                if self._process is None:
                    self._stop_event.wait(1.0)
                    continue

            if self._process.stdout is None:
                self._stop_event.wait(0.5)
                continue

            try:
                chunk = self._process.stdout.read(chunk_bytes)
            except Exception:
                chunk = b""

            if not chunk:
                # pw-record died (node removed, PipeWire restart, etc) --
                # drop to silence and let the top of the loop respawn it.
                self._kill_process()
                with self._lock:
                    self._level = 0.0
                self._stop_event.wait(0.5)
                continue

            samples = array.array("h")
            frame_bytes = 2 * self.CHANNELS
            samples.frombytes(chunk[: len(chunk) - (len(chunk) % frame_bytes)])
            channel_samples = samples[self._channel_index :: self.CHANNELS]
            if channel_samples:
                mean_square = sum(s * s for s in channel_samples) / len(channel_samples)
                rms = math.sqrt(mean_square) / 32768.0
            else:
                rms = 0.0

            target = clamp(
                rms * ASSISTANT_LED_MIC_GAIN, 0.0, 1.0
            ) ** ASSISTANT_LED_MIC_GAMMA
            with self._lock:
                alpha = attack_alpha if target > self._level else release_alpha
                self._level += (target - self._level) * alpha

        self._kill_process()


ASSISTANT_STATES = ("idle", "listening", "thinking", "speaking", "error", "ringing")


class AssistantStateWatcher(threading.Thread):
    """Listen on a UNIX socket for assistant state updates.

    Protocol: newline-delimited state names (idle/listening/thinking/speaking/
    error/ringing), one per line, from any number of client connections. The
    most recently received line wins. A line of "flash" is a one-shot event
    instead -- a request for the same end-of-session camera-flash pulse,
    without changing the persistent state (used by the playback button).
    This lets the wake-word service and, later,
    the conversation loop drive ring colour without a second process fighting
    this one for the LED USB endpoint.

    "timer <fraction>" / "timer clear" are a second, independent layer
    (timer_alarm.py), not part of the state machine above: a background
    countdown display that should only show up when nothing else is
    happening (see effective_state()), so it doesn't fight or flicker
    against a real listening/speaking/ringing state pushed at the same time.
    """

    def __init__(self, socket_path: str) -> None:
        super().__init__(name="assistant-led-socket", daemon=True)
        self._socket_path = socket_path
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._state = "idle"
        self._flash_pending = False
        self._timer_fraction: Optional[float] = None
        self._server: Optional[socket.socket] = None

    def state(self) -> str:
        with self._lock:
            return self._state

    def effective_state(self) -> str:
        """The state run_daemon should actually render: the real foreground
        state whenever it isn't idle (listening/speaking/thinking/error/
        ringing always win), else "timer" if a countdown is registered, else
        idle (the normal volume display)."""
        with self._lock:
            if self._state != "idle":
                return self._state
            return "timer" if self._timer_fraction is not None else "idle"

    def timer_fraction(self) -> float:
        with self._lock:
            return self._timer_fraction if self._timer_fraction is not None else 0.0

    def consume_flash(self) -> bool:
        """One-shot: returns whether a "flash" request has arrived since the
        last call, and clears it. Kept separate from self._state since it's
        an instantaneous event (a button press), not a persistent mode."""
        with self._lock:
            pending = self._flash_pending
            self._flash_pending = False
            return pending

    def _set_state(self, value: str) -> None:
        value = value.strip().lower()
        if value == "flash":
            with self._lock:
                self._flash_pending = True
            return
        if value == "timer clear":
            with self._lock:
                self._timer_fraction = None
            return
        if value.startswith("timer "):
            try:
                fraction = clamp(float(value.split(maxsplit=1)[1]), 0.0, 1.0)
            except ValueError:
                LOG.warning("Ignoring malformed timer line %r", value)
                return
            with self._lock:
                self._timer_fraction = fraction
            return
        if value not in ASSISTANT_STATES:
            LOG.warning("Ignoring unknown assistant state %r", value)
            return
        with self._lock:
            if value == self._state:
                return
            self._state = value
        LOG.info("Assistant state -> %s", value)

    def stop(self) -> None:
        self._stop_event.set()
        with contextlib.suppress(Exception):
            if self._server is not None:
                self._server.close()

    def _handle_client(self, conn: socket.socket) -> None:
        with conn, conn.makefile("r") as f:
            for line in f:
                if not line.strip():
                    continue
                self._set_state(line)

    def run(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._socket_path)
        os.makedirs(os.path.dirname(self._socket_path), exist_ok=True)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        try:
            server.bind(self._socket_path)
            os.chmod(self._socket_path, 0o660)
            server.listen(4)
            server.settimeout(0.5)
            LOG.info("Listening for assistant state on %s", self._socket_path)
            while not self._stop_event.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(
                    target=self._handle_client, args=(conn,), daemon=True
                ).start()
        finally:
            with contextlib.suppress(Exception):
                server.close()
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._socket_path)


class XvfLedRing:
    """Minimal XVF3800 LED-ring controller using USB vendor requests."""

    def __init__(
        self,
        brightness: float,
        low_colour: tuple[int, int, int],
        high_colour: tuple[int, int, int],
        mute_colour: tuple[int, int, int],
        start_pixel: int,
        reverse: bool,
    ) -> None:
        self.brightness = clamp(brightness, 0.0, 1.0)
        self.low_colour = low_colour
        self.high_colour = high_colour
        self.mute_colour = mute_colour
        self.start_pixel = start_pixel % LED_COUNT
        self.reverse = reverse
        self._device = None
        self._last_colours: Optional[tuple[int, ...]] = None
        self._last_missing_log = 0.0
        self._last_mode_write = 0.0

    def close(self) -> None:
        if self._device is not None:
            try:
                usb.util.dispose_resources(self._device)
            except Exception:
                pass
        self._device = None
        self._last_colours = None
        self._last_mode_write = 0.0

    def _transfer(self, command: int, payload: bytes) -> None:
        assert self._device is not None
        self._device.ctrl_transfer(
            usb.util.CTRL_OUT
            | usb.util.CTRL_TYPE_VENDOR
            | usb.util.CTRL_RECIPIENT_DEVICE,
            0,
            command,
            XVF_GPO_SERVICER_RESID,
            payload,
            timeout=1000,
        )

    def _connect(self) -> bool:
        if self._device is not None:
            return True

        try:
            device = usb.core.find(idVendor=XVF_VID, idProduct=XVF_PID)
            if device is None:
                now = time.monotonic()
                if now - self._last_missing_log >= 10.0:
                    LOG.warning("XVF3800 USB device 2886:001a is not available")
                    self._last_missing_log = now
                return False

            # Do not call set_configuration() or reset(): PipeWire may already be
            # streaming audio through other interfaces of the same USB device.
            self._device = device
            self._transfer(XVF_LED_EFFECT_CMD, bytes([XVF_LED_EFFECT_RING]))
            self._transfer(XVF_LED_GAMMIFY_CMD, bytes([1]))
            self._last_mode_write = time.monotonic()
            self._last_colours = None
            LOG.info("Connected to the XVF3800 LED control interface")
            return True
        except usb.core.USBError as exc:
            self.close()
            now = time.monotonic()
            if now - self._last_missing_log >= 10.0:
                LOG.warning("Cannot open XVF3800 USB control interface: %s", exc)
                self._last_missing_log = now
            return False

    def _ordered(self, colours: Sequence[int]) -> tuple[int, ...]:
        ordered = list(reversed(colours)) if self.reverse else list(colours)
        if self.start_pixel:
            ordered = ordered[-self.start_pixel :] + ordered[: -self.start_pixel]
        return tuple(ordered)

    def write_colours(self, colours: Sequence[int]) -> bool:
        if len(colours) != LED_COUNT:
            raise ValueError(f"Expected {LED_COUNT} LED colours")
        ordered = self._ordered(colours)
        if not self._connect():
            return False

        try:
            # Reassert ring mode occasionally. This recovers if a firmware boot
            # animation or another transient controller selected a different
            # LED effect after this process first connected.
            refreshed_mode = False
            if time.monotonic() - self._last_mode_write >= 5.0:
                self._transfer(XVF_LED_EFFECT_CMD, bytes([XVF_LED_EFFECT_RING]))
                self._transfer(XVF_LED_GAMMIFY_CMD, bytes([1]))
                self._last_mode_write = time.monotonic()
                refreshed_mode = True

            if ordered == self._last_colours and not refreshed_mode:
                return True
            self._transfer(XVF_LED_RING_COLOR_CMD, struct.pack("<12I", *ordered))
            self._last_colours = ordered
            return True
        except usb.core.USBError as exc:
            LOG.warning("XVF3800 LED write failed: %s", exc)
            self.close()
            return False

    def off(self) -> bool:
        return self.write_colours([0] * LED_COUNT)

    def render(self, state: VolumeState, fade: float = 1.0) -> bool:
        fade = smoothstep(fade)
        colours: list[int] = []

        if state.muted:
            # A dim red ring is unambiguous without being dazzling.
            amount = self.brightness * 0.75 * fade
            colours = [scale_rgb(self.mute_colour, amount)] * LED_COUNT
        else:
            level = clamp(state.volume, 0.0, 1.0) * LED_COUNT
            full_pixels = int(level)
            partial = level - full_pixels

            for index in range(LED_COUNT):
                if index < full_pixels:
                    pixel_level = 1.0
                elif index == full_pixels and full_pixels < LED_COUNT:
                    pixel_level = partial
                else:
                    pixel_level = 0.0

                # Zero volume still gets a faint first dot so the user sees that
                # the volume change was registered. It fades away normally.
                if state.volume <= 0.0005 and index == 0:
                    pixel_level = 0.16

                position = index / (LED_COUNT - 1)
                colour = blend_rgb(self.low_colour, self.high_colour, position)
                colours.append(
                    scale_rgb(colour, self.brightness * pixel_level * fade)
                )

        return self.write_colours(colours)


def state_changed(previous: VolumeState, current: VolumeState) -> bool:
    return (
        previous.sink_name != current.sink_name
        or previous.muted != current.muted
        or abs(previous.volume - current.volume) >= 0.002
    )


def run_demo(ring: XvfLedRing, fade_seconds: float, fps: float) -> int:
    LOG.info("Running LED demonstration")
    if not ring.off():
        LOG.error("Cannot start the LED demonstration; check USB and permissions")
        return 1
    for step in range(LED_COUNT + 1):
        ring.render(VolumeState("demo", step / LED_COUNT, False))
        time.sleep(0.08)
    time.sleep(0.4)

    frames = max(1, round(fade_seconds * fps))
    for frame in range(frames, -1, -1):
        ring.render(VolumeState("demo", 1.0, False), frame / frames)
        time.sleep(1.0 / fps)

    ring.render(VolumeState("demo", 0.0, True))
    time.sleep(0.5)
    for frame in range(frames, -1, -1):
        ring.render(VolumeState("demo", 0.0, True), frame / frames)
        time.sleep(1.0 / fps)
    ring.off()
    return 0


ASSISTANT_COLOURS: dict[str, tuple[int, int, int]] = {
    "listening": parse_rgb(os.environ.get("ASSISTANT_LED_LISTENING_COLOUR", "FFFFFF")),
    "thinking": parse_rgb(os.environ.get("ASSISTANT_LED_THINKING_COLOUR", "00E676")),
    "speaking": parse_rgb(os.environ.get("ASSISTANT_LED_SPEAKING_COLOUR", "20D060")),
    "error": parse_rgb(os.environ.get("ASSISTANT_LED_ERROR_COLOUR", "FF2A1E")),
    "ringing": parse_rgb(os.environ.get("ASSISTANT_LED_RINGING_COLOUR", "FF8800")),
}
# A timer/alarm actually going off is meant to be noticeably more urgent than
# the "error" blink -- faster, and its own colour so the two are never
# mistaken for each other at a glance.
ASSISTANT_LED_RINGING_BLINK_HZ = env_float("ASSISTANT_LED_RINGING_BLINK_HZ", 3.0)

# Colour for the timer countdown arc (a distinct background layer, see
# AssistantStateWatcher.effective_state() -- not one of ASSISTANT_COLOURS
# since "timer" isn't a foreground assistant state).
TIMER_LED_COLOUR = parse_rgb(os.environ.get("TIMER_LED_COLOUR", "FFC800"))

# Instead of cutting to the volume display mid-frame, or just fading the last
# frame out, a session ending (button cancel, turn complete, etc) plays a
# quick camera-flash pulse across the whole ring -- fast bright rise, slower
# fall to black -- as a distinct "session over" cue.
ASSISTANT_LED_FLASH_SECONDS = env_float("ASSISTANT_LED_FLASH_SECONDS", 0.6)
ASSISTANT_LED_FLASH_ATTACK = env_float("ASSISTANT_LED_FLASH_ATTACK", 0.12)
ASSISTANT_LED_FLASH_COLOUR = parse_rgb(os.environ.get("ASSISTANT_LED_FLASH_COLOUR", "FFFFFF"))
ASSISTANT_LED_FLASH_BOOST = env_float("ASSISTANT_LED_FLASH_BOOST", 1.8)

# The "thinking" ring is a single comet-like arc that rotates continuously
# while the assistant is working, with a gentle brightness breathing on top
# of the travelling arc.
ASSISTANT_LED_WAVE_SPEED_DEG = env_float("ASSISTANT_LED_WAVE_SPEED_DEG", 180.0)
ASSISTANT_LED_WAVE_WIDTH_IDLE_DEG = env_float("ASSISTANT_LED_WAVE_WIDTH_IDLE_DEG", 240.0)
ASSISTANT_LED_WAVE_DIM_IDLE = env_float("ASSISTANT_LED_WAVE_DIM_IDLE", 0.4)
ASSISTANT_LED_WAVE_BREATH_PERIOD = env_float("ASSISTANT_LED_WAVE_BREATH_PERIOD", 2.2)
ASSISTANT_LED_WAVE_BREATH_DEPTH = env_float("ASSISTANT_LED_WAVE_BREATH_DEPTH", 0.85)
ASSISTANT_LED_FREEZE_BREATHING = os.environ.get(
    "ASSISTANT_LED_FREEZE_BREATHING", "0"
).lower() in {"1", "true", "yes", "on"}

# The "listening" ring is all 12 LEDs breathing in sync, slowly, so it never
# looks static while waiting for you to talk.
ASSISTANT_LED_LISTEN_BREATH_PERIOD = env_float("ASSISTANT_LED_LISTEN_BREATH_PERIOD", 3.0)
ASSISTANT_LED_LISTEN_BREATH_FLOOR = env_float("ASSISTANT_LED_LISTEN_BREATH_FLOOR", 0.35)

# While "speaking", ring brightness follows the loudness of the assistant's
# own synthesized audio (tapped from its PipeWire sink monitor) instead of
# staying flat, so the green ring visibly pulses along with the voice.
# GAIN scales the raw 0..1 RMS reading up before GAMMA (<1) brightens the
# quieter parts perceptually; FLOOR keeps it from ever going fully dark
# between words.
ASSISTANT_LED_SPEECH_GAIN = env_float("ASSISTANT_LED_SPEECH_GAIN", 4.0)
ASSISTANT_LED_SPEECH_GAMMA = env_float("ASSISTANT_LED_SPEECH_GAMMA", 0.6)
ASSISTANT_LED_SPEECH_FLOOR = env_float("ASSISTANT_LED_SPEECH_FLOOR", 0.25)
# Meter ballistics, in seconds: how fast brightness rises to meet a louder
# level (ATTACK) vs falls back down as it gets quieter (RELEASE).
ASSISTANT_LED_SPEECH_ATTACK = env_float("ASSISTANT_LED_SPEECH_ATTACK", 0.03)
ASSISTANT_LED_SPEECH_RELEASE = env_float("ASSISTANT_LED_SPEECH_RELEASE", 0.10)

# While "listening", ring brightness follows the loudness of the user's own
# live voice (tapped directly from the mic-source node via MicLevelMonitor,
# independent of the pw-cat capture that feeds the Gemini Live session) --
# same GAIN/GAMMA/FLOOR law shape as ASSISTANT_LED_SPEECH_* above, but
# calibrated separately since raw mic levels run far quieter than
# sink-monitor program material. This overrides the slow idle breathing
# whenever it would be brighter (see AssistantAnimator.step's "listening"
# branch).
ASSISTANT_LED_MIC_SOURCE = os.environ.get(
    "ASSISTANT_LED_MIC_SOURCE",
    "alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993700254500350-00.analog-stereo",
)
# Default 0 matches VOICE_INPUT_CHANNEL's default -- the channel actually
# driving the Gemini conversation, not the (possibly different) wake-word
# channel.
ASSISTANT_LED_MIC_CHANNEL = env_int("ASSISTANT_LED_MIC_CHANNEL", 0)
# Calibrated against this room's ambient noise floor on channel 0 (RMS
# ~0.01-0.02): GAIN=80 pinned mic_level at ~0.85-1.0 from ambient alone,
# leaving no headroom for actual speech. GAIN=15 puts ambient rest around
# 0.38-0.43 instead. Raw mic levels here run far louder than the "~1-2 peak
# silence / ~400-600 peak voice" figures in pw_transport.py's AudioGate
# docstring, so that reference does not transfer to this channel/path.
# Speaking-volume calibration (the top end of this range) still needs a
# live on-device test -- see ASSISTANT_LED_MIC_DEBUG.
ASSISTANT_LED_MIC_GAIN = env_float("ASSISTANT_LED_MIC_GAIN", 15.0)
ASSISTANT_LED_MIC_GAMMA = env_float("ASSISTANT_LED_MIC_GAMMA", 0.5)
ASSISTANT_LED_MIC_FLOOR = env_float("ASSISTANT_LED_MIC_FLOOR", 0.35)
ASSISTANT_LED_MIC_ATTACK = env_float("ASSISTANT_LED_MIC_ATTACK", 0.03)
ASSISTANT_LED_MIC_RELEASE = env_float("ASSISTANT_LED_MIC_RELEASE", 0.15)
# Rollback lever: when off, the mic-tap thread/subprocess never starts, and
# the listening-wave brightness law collapses to exactly its pre-feature
# form (not merely GAIN=0, which would leave a dimmer-than-before floor
# applied permanently).
ASSISTANT_LED_MIC_LEVEL_ENABLED = os.environ.get(
    "ASSISTANT_LED_MIC_LEVEL_ENABLED", "1"
).lower() in {"1", "true", "yes", "on"}
ASSISTANT_LED_MIC_DEBUG = os.environ.get("ASSISTANT_LED_MIC_DEBUG", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _pulse_envelope(phase: float, attack_fraction: float = 0.35) -> float:
    """Smooth 0..1 heartbeat-shaped pulse: a quicker rise, a slower fall, and
    zero slope at both the trough and the peak (so it eases rather than
    kinks), unlike a plain triangle wave."""
    phase = phase % 1.0
    attack_fraction = clamp(attack_fraction, 0.05, 0.95)
    if phase < attack_fraction:
        t = phase / attack_fraction
        return 0.5 * (1.0 - math.cos(math.pi * t))
    t = (phase - attack_fraction) / (1.0 - attack_fraction)
    return 0.5 * (1.0 + math.cos(math.pi * t))


def _ring_pixel_gains(center_deg: float, width_deg: float) -> list[float]:
    """Per-pixel gain in [0, 1] (ring-angle space), peaking at the LED
    nearest center_deg and easing away over +/- width_deg/2 with a
    raised-cosine skirt, so neighbouring pixels blend smoothly."""
    half = max(1.0, width_deg / 2.0)
    gains = []
    for index in range(LED_COUNT):
        pixel_angle = index * (360.0 / LED_COUNT)
        delta = (pixel_angle - center_deg + 180.0) % 360.0 - 180.0
        if abs(delta) >= half:
            gains.append(0.0)
        else:
            gains.append(0.5 * (1.0 + math.cos(math.pi * delta / half)))
    return gains


class AssistantAnimator:
    """Renders the ring while an assistant state is active, and owns the
    short fade-to-black used when it drops back to idle -- replacing the
    previous abrupt cut to the volume display on session end/cancel.
    """

    def __init__(self, brightness: float) -> None:
        self._brightness = brightness
        self._state = "idle"
        self._fading = False
        self._phase = 0.0
        self._amount = 0.0
        self._colour: tuple[int, int, int] = (255, 255, 255)
        # Thinking-state comet: a free-running rotation angle, in
        # ring-pixel-angle space (not raw mic-array degrees).
        self._wave_phase = 0.0
        self._last_colours = [0] * LED_COUNT
        # Progress (0..1) through the end-of-session flash pulse.
        self._flash_phase = 1.0

    def step(
        self,
        state: str,
        dt: float,
        now: float,
        speech_level: float = 0.0,
        mic_level: float = 0.0,
        flash: bool = False,
        timer_fraction: float = 0.0,
    ) -> Optional[list[int]]:
        """Advance the animation by dt seconds. Returns the 12 LED colours to
        write this frame, or None if the volume display should take over."""
        if state == "timer":
            # Background countdown display (see AssistantStateWatcher.
            # effective_state()) -- a shrinking arc, same fractional-final-
            # pixel technique as XvfLedRing.render()'s volume arc, just
            # counting down instead of up and in its own colour.
            level = clamp(timer_fraction, 0.0, 1.0) * LED_COUNT
            full_pixels = int(level)
            partial = level - full_pixels
            colours = []
            for index in range(LED_COUNT):
                if index < full_pixels:
                    pixel_level = 1.0
                elif index == full_pixels and full_pixels < LED_COUNT:
                    pixel_level = partial
                else:
                    pixel_level = 0.0
                colours.append(scale_rgb(TIMER_LED_COLOUR, self._brightness * pixel_level))
            self._last_colours = colours
            return colours

        if state != self._state:
            if state == "idle":
                self._fading = True
                self._flash_phase = 0.0
            else:
                # Ease up from the floor on every fresh entry rather than
                # picking up mid-cycle, which used to cause a visible pop.
                self._phase = 0.0
                self._fading = False
            self._state = state

        if flash and state == "idle":
            # A one-shot flash request (e.g. the playback button actually
            # toggling play/pause) re-triggers the pulse even though we were
            # already idle and wouldn't otherwise see a state transition.
            self._fading = True
            self._flash_phase = 0.0

        if state == "idle":
            if not self._fading:
                return None
            duration = max(0.05, ASSISTANT_LED_FLASH_SECONDS)
            self._flash_phase = min(1.0, self._flash_phase + dt / duration)
            envelope = _pulse_envelope(self._flash_phase, ASSISTANT_LED_FLASH_ATTACK)
            if self._flash_phase >= 1.0:
                self._fading = False
            amount = self._brightness * ASSISTANT_LED_FLASH_BOOST * envelope
            colours = [scale_rgb(ASSISTANT_LED_FLASH_COLOUR, amount)] * LED_COUNT
            self._last_colours = colours
            return colours

        if state == "speaking":
            floor = ASSISTANT_LED_SPEECH_FLOOR
            self._amount = clamp(floor + (1.0 - floor) * speech_level, 0.0, 1.0)
            self._colour = ASSISTANT_COLOURS[state]
        elif state == "error":
            self._amount = 1.0 if int(now * 4.0) % 2 == 0 else 0.15
            self._colour = ASSISTANT_COLOURS[state]
        elif state == "ringing":
            self._amount = 1.0 if int(now * ASSISTANT_LED_RINGING_BLINK_HZ * 2) % 2 == 0 else 0.15
            self._colour = ASSISTANT_COLOURS[state]
        elif state == "thinking":
            self._colour = ASSISTANT_COLOURS[state]
            colours = self._step_comet_wave(dt)
            self._last_colours = colours
            return colours
        else:  # listening
            self._colour = ASSISTANT_COLOURS[state]
            if ASSISTANT_LED_FREEZE_BREATHING:
                breath = 1.0
            else:
                period = max(0.05, ASSISTANT_LED_LISTEN_BREATH_PERIOD)
                self._phase = (self._phase + dt / period) % 1.0
                floor = ASSISTANT_LED_LISTEN_BREATH_FLOOR
                breath = floor + (1.0 - floor) * (
                    0.5 * (1.0 - math.cos(2.0 * math.pi * self._phase))
                )
            if ASSISTANT_LED_MIC_LEVEL_ENABLED:
                floor = ASSISTANT_LED_MIC_FLOOR
                mic_amount = clamp(floor + (1.0 - floor) * mic_level, 0.0, 1.0)
            else:
                mic_amount = 0.0
            # Loud speech overrides the slow idle breathing; otherwise the
            # ring just breathes on its own.
            self._amount = max(breath, mic_amount)

        base = self._brightness * self._amount
        colours = [scale_rgb(self._colour, base) for _ in range(LED_COUNT)]
        self._last_colours = colours
        return colours

    def _step_comet_wave(self, dt: float) -> list[int]:
        """The "thinking" ring is a single comet-like arc that rotates
        continuously while the assistant works, with a gentle brightness
        breathing on top of the travelling arc."""
        self._wave_phase = (
            self._wave_phase + dt * ASSISTANT_LED_WAVE_SPEED_DEG
        ) % 360.0

        if ASSISTANT_LED_FREEZE_BREATHING:
            breath = 1.0
        else:
            self._phase = (self._phase + dt / ASSISTANT_LED_WAVE_BREATH_PERIOD) % 1.0
            breath = 1.0 - ASSISTANT_LED_WAVE_BREATH_DEPTH * (
                1.0 - _pulse_envelope(self._phase)
            )

        gains = _ring_pixel_gains(self._wave_phase, ASSISTANT_LED_WAVE_WIDTH_IDLE_DEG)
        dim = ASSISTANT_LED_WAVE_DIM_IDLE
        base = self._brightness
        return [
            scale_rgb(self._colour, base * (dim + (1.0 - dim) * gain * breath))
            for gain in gains
        ]


def run_daemon(
    ring: XvfLedRing,
    hold_seconds: float,
    fade_seconds: float,
    fps: float,
    assistant_watcher: Optional[AssistantStateWatcher] = None,
) -> int:
    running = True

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    events: queue.Queue[object] = queue.Queue(maxsize=1)
    watcher = PulseEventWatcher(events)
    reader = PulseStateReader()
    audio_level = AudioLevelMonitor()
    mic_level_monitor: Optional[MicLevelMonitor] = None
    if ASSISTANT_LED_MIC_LEVEL_ENABLED:
        mic_level_monitor = MicLevelMonitor(
            ASSISTANT_LED_MIC_SOURCE, ASSISTANT_LED_MIC_CHANNEL
        )
    watcher.start()
    audio_level.start()
    if mic_level_monitor is not None:
        mic_level_monitor.start()

    last_state: Optional[VolumeState] = None
    displayed_state: Optional[VolumeState] = None
    hold_until = 0.0
    fade_until = 0.0
    next_fallback_read = 0.0
    next_idle_blank = 0.0
    frame_period = 1.0 / max(1.0, fps)
    last_frame_time = time.monotonic()
    animator = AssistantAnimator(ring.brightness)

    # Blank any firmware boot animation as soon as USB becomes available.
    ring.off()

    try:
        while running:
            now = time.monotonic()
            dt = max(0.0, now - last_frame_time)
            last_frame_time = now

            assistant_state = assistant_watcher.effective_state() if assistant_watcher else "idle"
            speech_level = audio_level.level() if assistant_state == "speaking" else 0.0
            mic_level = (
                mic_level_monitor.level()
                if mic_level_monitor is not None and assistant_state == "listening"
                else 0.0
            )
            flash_requested = assistant_watcher.consume_flash() if assistant_watcher else False
            timer_fraction = assistant_watcher.timer_fraction() if assistant_watcher else 0.0
            if ASSISTANT_LED_MIC_DEBUG and assistant_state == "listening":
                LOG.info("DEBUG mic_level=%.3f", mic_level)
            colours = animator.step(
                assistant_state, dt, now, speech_level, mic_level, flash_requested, timer_fraction
            )
            if colours is not None:
                ring.write_colours(colours)
                # Force the next volume read to be treated as a fresh baseline
                # once we return to idle, rather than diffing against stale
                # pre-assistant-state volume.
                last_state = None
                displayed_state = None
                time.sleep(frame_period)
                continue

            dirty = False
            try:
                events.get_nowait()
                dirty = True
            except queue.Empty:
                pass

            # The periodic read is a low-cost safety net in case an event is
            # coalesced while PipeWire-Pulse is reconnecting.
            if dirty or now >= next_fallback_read:
                next_fallback_read = now + 2.0
                current = reader.read()
                if current is not None:
                    if last_state is None:
                        LOG.info(
                            "Default sink is %s (volume %.1f%%, muted=%s)",
                            current.sink_name,
                            current.volume * 100.0,
                            current.muted,
                        )
                    elif state_changed(last_state, current):
                        displayed_state = current
                        hold_until = now + max(0.0, hold_seconds)
                        fade_until = hold_until + max(0.01, fade_seconds)
                        LOG.info(
                            "Volume %.1f%%, muted=%s, sink=%s",
                            current.volume * 100.0,
                            current.muted,
                            current.sink_name,
                        )
                    last_state = current

            if displayed_state is not None:
                if now <= hold_until:
                    fade = 1.0
                elif now < fade_until:
                    fade = 1.0 - (now - hold_until) / (fade_until - hold_until)
                else:
                    fade = 0.0

                if fade > 0.0:
                    ring.render(displayed_state, fade)
                else:
                    ring.off()
                    displayed_state = None
                    next_idle_blank = now + 10.0
            elif now >= next_idle_blank:
                # Also reconnects and clears the ring after USB hot-plug.
                ring.off()
                next_idle_blank = now + 10.0

            time.sleep(frame_period)
    finally:
        watcher.stop()
        audio_level.stop()
        if mic_level_monitor is not None:
            mic_level_monitor.stop()
        reader.close()
        ring.off()
        ring.close()

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="run an LED test and exit")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    parser.add_argument(
        "--hold",
        type=float,
        default=env_float("VOLUME_LED_HOLD", 0.9),
        help="seconds to hold the volume indication",
    )
    parser.add_argument(
        "--fade",
        type=float,
        default=env_float("VOLUME_LED_FADE", 0.55),
        help="seconds used for the fade to black",
    )
    parser.add_argument(
        "--brightness",
        type=float,
        default=env_float("VOLUME_LED_BRIGHTNESS", 0.32),
        help="maximum LED brightness from 0.0 to 1.0",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=env_float("VOLUME_LED_FPS", 30.0),
        help="fade animation frame rate",
    )
    parser.add_argument(
        "--low-colour",
        default=os.environ.get("VOLUME_LED_LOW_COLOUR", "0068FF"),
        help="six-digit RGB colour at the beginning of the arc",
    )
    parser.add_argument(
        "--high-colour",
        default=os.environ.get("VOLUME_LED_HIGH_COLOUR", "00D8C0"),
        help="six-digit RGB colour at the end of the arc",
    )
    parser.add_argument(
        "--mute-colour",
        default=os.environ.get("VOLUME_LED_MUTE_COLOUR", "FF180C"),
        help="six-digit RGB colour used while muted",
    )
    parser.add_argument(
        "--start-pixel",
        type=int,
        default=env_int("VOLUME_LED_START_PIXEL", 0),
        help="rotate the beginning of the volume arc (0-11)",
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        default=os.environ.get("VOLUME_LED_REVERSE", "0").lower()
        in {"1", "true", "yes", "on"},
        help="reverse the ring direction",
    )
    default_socket = os.path.join(
        os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "assistant-led.sock"
    )
    parser.add_argument(
        "--assistant-socket",
        default=os.environ.get("ASSISTANT_LED_SOCKET", default_socket),
        help="UNIX socket path other services use to report assistant state",
    )
    parser.add_argument(
        "--no-assistant-socket",
        action="store_true",
        help="disable the assistant-state socket entirely",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        ring = XvfLedRing(
            brightness=args.brightness,
            low_colour=parse_rgb(args.low_colour),
            high_colour=parse_rgb(args.high_colour),
            mute_colour=parse_rgb(args.mute_colour),
            start_pixel=args.start_pixel,
            reverse=args.reverse,
        )
    except ValueError as exc:
        LOG.error("Invalid configuration: %s", exc)
        return 2

    if args.demo:
        try:
            return run_demo(ring, max(0.05, args.fade), max(1.0, args.fps))
        finally:
            ring.off()
            ring.close()

    assistant_watcher = None
    if not args.no_assistant_socket:
        assistant_watcher = AssistantStateWatcher(args.assistant_socket)
        assistant_watcher.start()

    try:
        return run_daemon(
            ring,
            hold_seconds=max(0.0, args.hold),
            fade_seconds=max(0.05, args.fade),
            fps=max(1.0, args.fps),
            assistant_watcher=assistant_watcher,
        )
    finally:
        if assistant_watcher is not None:
            assistant_watcher.stop()


if __name__ == "__main__":
    raise SystemExit(main())
