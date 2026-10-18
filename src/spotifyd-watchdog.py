#!/home/aldom/.venvs/voice-agent/bin/python3
"""Restart spotifyd when Spotify has lost it.

spotifyd 0.4.2 can lose its connection to Spotify's dealer websocket ("Websocket
peer does not respond.") and never reconnect: the process keeps running, systemd
sees nothing wrong, but the speaker is gone from the account's Connect device
list, so "play X" by voice fails with 404 and the speaker is missing in the
Spotify app (2026-10-12: dropped at 02:00, found at 09:24).

Run by spotifyd-watchdog.timer every few minutes, and started at once by
spotify_web.py when a play request cannot find the speaker. Restarts spotifyd
only when the speaker is confirmed missing from the device list twice, RECHECK
seconds apart. Missing from the list means it cannot be the active Connect
device, so the restart never interrupts playback (see AGENTS.md).

Does nothing when Spotify cannot be reached at all (a restart would not help),
when spotifyd is not running or has only just started, or when it already
restarted spotifyd within COOLDOWN seconds.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time

import spotify_web

LOG = logging.getLogger("spotifyd-watchdog")

UNIT = "spotifyd.service"
# spotifyd needs a few seconds after start to log in and register.
GRACE = float(os.environ.get("SPOTIFYD_WATCHDOG_GRACE", "90"))
RECHECK = float(os.environ.get("SPOTIFYD_WATCHDOG_RECHECK", "20"))
# At most one restart per COOLDOWN, so a broken login is not restarted forever.
COOLDOWN = float(os.environ.get("SPOTIFYD_WATCHDOG_COOLDOWN", "900"))
STAMP = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    "spotifyd-watchdog.last-restart",
)


def _unit_uptime() -> float | None:
    """Seconds since spotifyd became active, or None if it is not active."""
    out = subprocess.run(
        ["systemctl", "--user", "show", UNIT,
         "--property=ActiveState,ActiveEnterTimestampMonotonic"],
        capture_output=True, text=True, check=False,
    ).stdout
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if props.get("ActiveState") != "active":
        return None
    entered = int(props.get("ActiveEnterTimestampMonotonic") or 0) / 1e6
    return time.clock_gettime(time.CLOCK_MONOTONIC) - entered


async def _listed() -> bool | None:
    """Whether the speaker is in the Connect device list; None if unknown."""
    try:
        names = [(d.get("name") or "").casefold() for d in await spotify_web.devices()]
    except spotify_web.SpotifyError as exc:
        LOG.warning("Cannot check the device list: %s", exc.detail or exc.message)
        return None
    return spotify_web.DEVICE_NAME.casefold() in names


async def check() -> int:
    uptime = _unit_uptime()
    if uptime is None or uptime < GRACE:
        return 0
    try:
        if await _listed() is not False:
            return 0
        LOG.info("%r is not in the Connect device list; checking again in %.0fs",
                 spotify_web.DEVICE_NAME, RECHECK)
        await asyncio.sleep(RECHECK)
        if await _listed() is not False:
            return 0
    finally:
        await spotify_web.close()

    try:
        since = time.time() - os.path.getmtime(STAMP)
    except OSError:
        since = None
    if since is not None and since < COOLDOWN:
        LOG.warning("Still not listed, but spotifyd was restarted %.0fs ago; leaving it", since)
        return 1

    LOG.warning("%r still not listed after %.0fs: restarting spotifyd",
                spotify_web.DEVICE_NAME, RECHECK)
    with open(STAMP, "w"):
        pass
    subprocess.run(["systemctl", "--user", "restart", UNIT], check=False)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(asyncio.run(check()))
