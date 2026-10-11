"""Command execution for local_intent.py matches, run from wake-word.service.

Deliberately does NOT import spotify_tools.py/volume_tools.py: those live
under the voice-agent venv and import pipecat, which wake-word.service's venv
does not have on purpose (see AI-SPEAKER-SETUP-NOTES.md -- isolating each
service's dependency weight was a deliberate choice to avoid version fights).
This module reimplements just the same playerctl/wpctl subprocess calls,
kept parallel to spotify_tools.py/volume_tools.py by convention (same
binaries, same env var names) rather than by shared code, so a wake-word
process never needs to import pipecat just to run "pause".
"""

from __future__ import annotations

import asyncio
import logging
import os

import timer_alarm_client

LOG = logging.getLogger("wake-word.local-actions")

PLAYERCTL = os.environ.get("PLAYERCTL_PATH", "/usr/bin/playerctl")
WPCTL = os.environ.get("WPCTL_PATH", "/usr/bin/wpctl")
VOLUME_SINK = os.environ.get("VOLUME_SINK", "@DEFAULT_AUDIO_SINK@")
VOLUME_MAX = float(os.environ.get("VOLUME_MAX", "1.0"))
VOLUME_STEP_PERCENT = int(os.environ.get("LOCAL_INTENT_VOLUME_STEP", "10"))


async def _run(*args: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError:
        return False, f"{args[0]} not found"
    except asyncio.TimeoutError:
        return False, f"{args[0]} timed out"
    if proc.returncode != 0:
        return False, (stderr or stdout).decode().strip() or "command failed"
    return True, ""


async def spotify_play(**_kwargs) -> tuple[bool, str]:
    return await _run(PLAYERCTL, "play")


async def spotify_pause(**_kwargs) -> tuple[bool, str]:
    return await _run(PLAYERCTL, "pause")


async def spotify_next(**_kwargs) -> tuple[bool, str]:
    return await _run(PLAYERCTL, "next")


async def spotify_previous(**_kwargs) -> tuple[bool, str]:
    return await _run(PLAYERCTL, "previous")


async def adjust_volume(direction: str, step_percent: int | None = None) -> tuple[bool, str]:
    if direction not in {"up", "down"}:
        return False, f"invalid direction: {direction!r}"
    step = step_percent or VOLUME_STEP_PERCENT
    sign = "+" if direction == "up" else "-"
    args = [WPCTL, "set-volume", VOLUME_SINK, f"{step}%{sign}"]
    if direction == "up":
        args.extend(["--limit", f"{VOLUME_MAX:g}"])
    return await _run(*args)


async def set_timer(seconds: int, **_kwargs) -> tuple[bool, str]:
    # timer_alarm_client's socket calls are blocking (short timeout) --
    # offloaded to a thread so they don't stall wake_word.py's event loop.
    return await asyncio.to_thread(timer_alarm_client.set_timer, int(seconds))


async def cancel_timer(**_kwargs) -> tuple[bool, str]:
    return await asyncio.to_thread(timer_alarm_client.cancel_timer)


async def set_alarm(hour: int, minute: int, **_kwargs) -> tuple[bool, str]:
    return await asyncio.to_thread(timer_alarm_client.set_alarm, int(hour), int(minute))


async def cancel_alarm(**_kwargs) -> tuple[bool, str]:
    return await asyncio.to_thread(timer_alarm_client.cancel_alarm)


ACTIONS = {
    "spotify_play": spotify_play,
    "spotify_pause": spotify_pause,
    "spotify_next": spotify_next,
    "spotify_previous": spotify_previous,
    "adjust_volume": adjust_volume,
    "set_timer": set_timer,
    "cancel_timer": cancel_timer,
    "set_alarm": set_alarm,
    "cancel_alarm": cancel_alarm,
}


async def run_action(name: str, kwargs: dict) -> tuple[bool, str]:
    fn = ACTIONS.get(name)
    if fn is None:
        return False, f"unknown local action: {name!r}"
    try:
        return await fn(**kwargs)
    except Exception:  # noqa: BLE001 -- a local-intent action must never crash wake-word.service
        LOG.exception("local action %s(%r) crashed", name, kwargs)
        return False, "local action crashed"
