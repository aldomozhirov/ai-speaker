"""Master volume tools for the voice agent's function calling (Phase 8.2).

Both drive @DEFAULT_AUDIO_SINK@ via wpctl -- the same physical volume
authority volume-buttons.py and volume_sync.py already use, so voice changes
show up consistently on the physical button and in the Spotify app (which
volume_sync.py mirrors the sink volume into over MPRIS).
"""

from __future__ import annotations

import asyncio
import logging
import os

from pipecat.services.llm_service import FunctionCallParams

import session_signal

LOG = logging.getLogger("voice-agent.volume")

WPCTL = os.environ.get("WPCTL_PATH", "/usr/bin/wpctl")
SINK = os.environ.get("VOLUME_SINK", "@DEFAULT_AUDIO_SINK@")
MAX_VOLUME = float(os.environ.get("VOLUME_MAX", "1.0"))


async def _run_wpctl(*args: str) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            WPCTL,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
    except FileNotFoundError:
        return False, "wpctl not found"
    except asyncio.TimeoutError:
        return False, "wpctl timed out"

    if proc.returncode != 0:
        message = (stderr or stdout).decode().strip()
        return False, message or "wpctl failed"
    return True, ""


async def _current_volume_percent() -> int | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            WPCTL, "get-volume", SINK, stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
    except (FileNotFoundError, asyncio.TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    # Output looks like "Volume: 0.45"
    try:
        fraction = float(stdout.decode().strip().split()[-1])
    except (IndexError, ValueError):
        return None
    return round(fraction * 100)


async def set_volume(params: FunctionCallParams, level_percent: int) -> None:
    """Set the speaker's volume to an exact level.

    Args:
        level_percent: Target volume from 0 to 100.
    """
    level_percent = max(0, min(100, level_percent))
    fraction = min(MAX_VOLUME, level_percent / 100.0)
    ok, error = await _run_wpctl("set-volume", SINK, f"{fraction:.3f}")
    if ok:
        await session_signal.complete_quick_action(
            params,
            {"status": "ok", "volume_percent": round(fraction * 100)},
        )
    else:
        LOG.warning("set_volume failed: %s", error)
        await params.result_callback({"status": "error", "error": error})


async def adjust_volume(
    params: FunctionCallParams, direction: str, step_percent: int = 10
) -> None:
    """Turn the speaker's volume up or down by a relative amount.

    Args:
        direction: Either "up" or "down".
        step_percent: How much to change the volume by, in percentage
            points. Use the default of 10 for a normal adjustment, a
            smaller value like 5 for a request like "a little", and a
            larger value like 20 for "a lot".
    """
    if direction not in {"up", "down"}:
        await params.result_callback(
            {"status": "error", "error": f"invalid direction: {direction!r}"}
        )
        return

    step_percent = max(1, min(100, step_percent))
    sign = "+" if direction == "up" else "-"
    args = ["set-volume", SINK, f"{step_percent}%{sign}"]
    if direction == "up":
        args.extend(["--limit", f"{MAX_VOLUME:g}"])
    ok, error = await _run_wpctl(*args)
    if not ok:
        LOG.warning("adjust_volume failed: %s", error)
        await params.result_callback({"status": "error", "error": error})
        return

    volume_percent = await _current_volume_percent()
    await session_signal.complete_quick_action(
        params,
        {"status": "ok", "volume_percent": volume_percent},
    )
