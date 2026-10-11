"""Thin stdlib-only client for timer_alarm.py's UNIX socket.

Deliberately no vosk/numpy/pipecat imports -- importable from any venv in
this stack (wake-word today, voice-agent later if this is ever exposed as a
cloud tool too) without dragging in dependency weight, same rationale as
local_actions.py not importing spotify_tools.py/volume_tools.py (see
AI-SPEAKER-SETUP-NOTES.md's venv-isolation rationale). Mirrors the
socket-client pattern already used for WAKE_TRIGGER_SOCKET/
ASSISTANT_LED_SOCKET elsewhere in this stack, except the protocol here is
one JSON command in, one JSON response line back (those two are fire-and-forget).
"""

from __future__ import annotations

import json
import logging
import os
import socket

LOG = logging.getLogger("timer-alarm-client")

TIMER_ALARM_SOCKET = os.environ.get(
    "TIMER_ALARM_SOCKET",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "timer-alarm.sock"),
)


def _call(cmd: dict, timeout: float = 2.0) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(TIMER_ALARM_SOCKET)
            s.sendall((json.dumps(cmd) + "\n").encode())
            with s.makefile("r") as f:
                line = f.readline()
    except OSError as exc:
        LOG.debug("Could not reach timer-alarm socket %s: %s", TIMER_ALARM_SOCKET, exc)
        return {"ok": False, "error": str(exc)}
    if not line:
        return {"ok": False, "error": "no response"}
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad response"}


def status() -> dict:
    """Returns {"ringing": bool, "timer": {...}|None, "alarm": {...}|None},
    or {"ringing": False, ...} defaults if the service is unreachable --
    callers (e.g. wake_word.py's ring-check) should treat "can't tell" the
    same as "not ringing" rather than block on it."""
    resp = _call({"cmd": "status"})
    resp.setdefault("ringing", False)
    return resp


def is_ringing() -> bool:
    return bool(status().get("ringing"))


def stop_ring() -> tuple[bool, str]:
    resp = _call({"cmd": "stop_ring"})
    return bool(resp.get("ok")), str(resp.get("error", ""))


def set_timer(seconds: int) -> tuple[bool, str]:
    resp = _call({"cmd": "set_timer", "seconds": seconds})
    return bool(resp.get("ok")), str(resp.get("error", ""))


def cancel_timer() -> tuple[bool, str]:
    resp = _call({"cmd": "cancel_timer"})
    return bool(resp.get("ok")), str(resp.get("error", ""))


def set_alarm(hour: int, minute: int) -> tuple[bool, str]:
    resp = _call({"cmd": "set_alarm", "hour": hour, "minute": minute})
    return bool(resp.get("ok")), str(resp.get("error", ""))


def cancel_alarm() -> tuple[bool, str]:
    resp = _call({"cmd": "cancel_alarm"})
    return bool(resp.get("ok")), str(resp.get("error", ""))
