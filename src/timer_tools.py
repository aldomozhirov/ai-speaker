"""Timer & alarm tools for the voice agent's function calling.

Companion to the offline path, not a replacement for it. local_intent.py can
already set a timer without any network at all, but it only runs on the
wake-word branch of wake_word.py and only for phrases its Vosk grammar
covers -- a button-triggered session, or any phrasing outside that grammar,
goes straight to the cloud LLM, which until now had no timer tool and
truthfully answered that it couldn't set one. These tools close that gap so
the capability exists on both paths.

Both paths converge on the same place: timer_alarm.py's UNIX socket, via the
stdlib-only timer_alarm_client. There is no second copy of the scheduling,
persistence, ringing or stop-listening logic -- this module only translates
LLM arguments into client calls.

Mirrors volume_tools.py's conventions: plain async functions bound directly
by LLMContext (so the docstrings below ARE the LLM-facing schema -- Pipecat's
DirectFunction introspects them), a session_signal.mark_quick_action() on
success so the session doesn't sit waiting for another turn, and blocking
socket work pushed to a thread so the pipeline's event loop keeps running.
"""

from __future__ import annotations

import asyncio
import logging
import time

from pipecat.services.llm_service import FunctionCallParams

import session_signal
import timer_alarm_client

LOG = logging.getLogger("voice-agent.timer")

# A timer the user asks for by voice is realistically seconds-to-hours. The
# upper bound is a guard against a misheard "a hundred hours", not a real
# product limit.
MAX_TIMER_SECONDS = 24 * 3600


def _describe_duration(seconds: int) -> str:
    """Spoken-friendly duration ("1 hour 5 minutes"), for the tool result the
    LLM reads back to the user."""
    seconds = int(round(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    for value, unit in ((hours, "hour"), (minutes, "minute"), (secs, "second")):
        if value:
            parts.append(f"{value} {unit}{'s' if value != 1 else ''}")
    return " ".join(parts) if parts else "0 seconds"


async def set_timer(params: FunctionCallParams, duration_seconds: int) -> None:
    """Start a countdown timer that rings when it reaches zero.

    Args:
        duration_seconds: How long the timer should run, in seconds. Convert
            whatever the user said into seconds first -- "five minutes" is
            300, "an hour and a half" is 5400, "thirty seconds" is 30.
    """
    try:
        duration_seconds = int(duration_seconds)
    except (TypeError, ValueError):
        await params.result_callback(
            {"status": "error", "error": "duration_seconds must be a whole number of seconds"}
        )
        return

    if duration_seconds <= 0:
        await params.result_callback(
            {"status": "error", "error": "a timer needs a positive duration"}
        )
        return
    if duration_seconds > MAX_TIMER_SECONDS:
        await params.result_callback(
            {"status": "error", "error": "that's longer than the 24 hour maximum for a timer"}
        )
        return

    ok, error = await asyncio.to_thread(timer_alarm_client.set_timer, duration_seconds)
    if not ok:
        LOG.warning("set_timer failed: %s", error)
        await params.result_callback({"status": "error", "error": error or "timer service unreachable"})
        return

    await session_signal.complete_quick_action(
        params,
        {
            "status": "ok",
            "duration_seconds": duration_seconds,
            "duration_spoken": _describe_duration(duration_seconds),
        },
    )


async def cancel_timer(params: FunctionCallParams) -> None:
    """Cancel the timer that is currently counting down, if there is one."""
    ok, error = await asyncio.to_thread(timer_alarm_client.cancel_timer)
    if not ok:
        LOG.warning("cancel_timer failed: %s", error)
        await params.result_callback({"status": "error", "error": error or "timer service unreachable"})
        return
    await session_signal.complete_quick_action(
        params,
        {"status": "ok"},
    )


async def set_alarm(params: FunctionCallParams, hour: int, minute: int = 0) -> None:
    """Set a one-off alarm to ring at a specific wall-clock time. It rings at
    the next occurrence of that time -- later today if it hasn't passed yet,
    otherwise tomorrow.

    Args:
        hour: Hour on a 24-hour clock, 0 to 23. Convert from what the user
            said -- "seven fifteen in the evening" is hour 19, "half past
            six in the morning" is hour 6.
        minute: Minute past the hour, 0 to 59. Defaults to 0.
    """
    try:
        hour, minute = int(hour), int(minute)
    except (TypeError, ValueError):
        await params.result_callback(
            {"status": "error", "error": "hour and minute must be whole numbers"}
        )
        return

    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        await params.result_callback(
            {"status": "error", "error": f"{hour}:{minute:02d} isn't a valid time of day"}
        )
        return

    ok, error = await asyncio.to_thread(timer_alarm_client.set_alarm, hour, minute)
    if not ok:
        LOG.warning("set_alarm failed: %s", error)
        await params.result_callback({"status": "error", "error": error or "timer service unreachable"})
        return

    await session_signal.complete_quick_action(
        params,
        {"status": "ok", "hour": hour, "minute": minute},
    )


async def cancel_alarm(params: FunctionCallParams) -> None:
    """Cancel the alarm that is currently set, if there is one."""
    ok, error = await asyncio.to_thread(timer_alarm_client.cancel_alarm)
    if not ok:
        LOG.warning("cancel_alarm failed: %s", error)
        await params.result_callback({"status": "error", "error": error or "timer service unreachable"})
        return
    await session_signal.complete_quick_action(
        params,
        {"status": "ok"},
    )


async def get_timer_alarm_status(params: FunctionCallParams) -> None:
    """Check whether a timer or alarm is currently set, how much time is left
    on the timer, and what time the alarm will ring. Use this for questions
    like "how long is left?" or "do I have an alarm set?"."""
    status = await asyncio.to_thread(timer_alarm_client.status)
    if not isinstance(status, dict):
        await params.result_callback({"status": "error", "error": "timer service unreachable"})
        return

    result: dict = {"status": "ok", "ringing": bool(status.get("ringing"))}

    timer = status.get("timer")
    if timer:
        remaining = max(0, int(round(timer["fire_at"] - time.time())))
        result["timer"] = {
            "remaining_seconds": remaining,
            "remaining_spoken": _describe_duration(remaining),
        }
    else:
        result["timer"] = None

    alarm = status.get("alarm")
    if alarm:
        rings_at = time.localtime(alarm["fire_at"])
        result["alarm"] = {
            "hour": rings_at.tm_hour,
            "minute": rings_at.tm_min,
            "rings_at": time.strftime("%H:%M", rings_at),
        }
    else:
        result["alarm"] = None

    await params.result_callback(result)
