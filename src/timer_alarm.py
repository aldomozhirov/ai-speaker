#!/usr/bin/env python3
"""Offline timer & alarm service for the AI speaker.

Owns a single active timer and a single active alarm (v1 scope -- setting a
new one replaces whichever is active; extending to multiples later is a
small change to TimerAlarmState, not an architectural one). Both are stored
as absolute `fire_at` epoch timestamps and persisted to disk on every
mutation, so a service restart doesn't lose or reset either one -- same
"supervise absolute state, not remaining time" instinct as xvf-aec-ref.sh
(see AI-SPEAKER-SETUP-NOTES.md).

Reachable two ways:
  - Voice, via wake_word.py -> local_intent.py's offline Vosk grammar
    (LOCAL_INTENT_ENABLED=1 required) -> local_actions.py -> this service's
    UNIX socket (TIMER_ALARM_SOCKET), newline-delimited JSON in, one JSON
    response line back.
  - Programmatically via timer_alarm_client.py, the thin stdlib-only client
    for that same socket.

On fire: pushes "ringing" to the shared assistant-LED socket, starts sound_fx
looping the timer/alarm sound (never stops on its own), and opens a
dedicated offline "stop" listener against the XVF3800 capture -- open-mic,
no wake word needed, independent of wake_word.py's own grammar so "stop"
doesn't change meaning anywhere else. Silenced by that listener, the wake
button (wake_word.py's WakeTrigger.fire() checks this service first), or a
TIMER_ALARM_MAX_RING_SECONDS safety cutoff.

While a timer (not alarm -- see plan) is counting down and nothing is
ringing, pushes "timer <fraction>" once a second to the LED socket so
volume_led.py can show a shrinking countdown arc whenever nothing else is
happening on the ring; pushes "timer clear" the moment the timer is gone
(fired or cancelled) so the LED falls back to its normal idle/volume
display.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sound_fx  # noqa: E402
from pw_node import wait_for_node  # noqa: E402

LOG = logging.getLogger("timer-alarm")

STATE_PATH = os.environ.get(
    "TIMER_ALARM_STATE_PATH",
    os.path.expanduser("~/.local/share/ai-speaker/timer-alarm-state.json"),
)
TIMER_ALARM_SOCKET = os.environ.get(
    "TIMER_ALARM_SOCKET",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "timer-alarm.sock"),
)
ASSISTANT_LED_SOCKET = os.environ.get(
    "ASSISTANT_LED_SOCKET",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "assistant-led.sock"),
)

# Same source/channel wake_word.py listens on -- the stop-listener taps the
# same XVF3800 processed capture, not raw mics (see wake_word.py's own note).
XVF_SOURCE = os.environ.get(
    "WAKE_WORD_SOURCE",
    "alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993700254500350-00.analog-stereo",
)
CHANNEL_INDEX = int(os.environ.get("WAKE_WORD_CHANNEL", "0"))
SAMPLE_RATE = 16000
CAPTURE_CHANNELS = 2
STOP_FRAME_SAMPLES = 1600  # 100ms
STOP_FRAME_BYTES = STOP_FRAME_SAMPLES * CAPTURE_CHANNELS * 2  # s16, interleaved

MAX_RING_SECONDS = float(os.environ.get("TIMER_ALARM_MAX_RING_SECONDS", "600"))
TICK_SECONDS = 1.0

STOP_MODEL_DIR = os.environ.get(
    "LOCAL_INTENT_MODEL_DIR",
    os.path.expanduser("~/.local/share/ai-speaker/vosk-model-small-en-us-0.15"),
)
# Deliberately its own tiny grammar, independent of local_intent.py's -- only
# active while something is ringing, so it doesn't change what "stop" means
# the rest of the time (still "pause Spotify" via local_intent's own grammar).
_STOP_WORDS = ("stop", "cancel", "off")
_STOP_GRAMMAR = [
    "stop", "stop it", "stop the timer", "stop the alarm", "cancel",
    "cancel it", "turn it off", "[unk]",
]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def send_led(line: str) -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(ASSISTANT_LED_SOCKET)
            s.sendall((line + "\n").encode())
    except OSError as exc:
        LOG.debug("Could not reach assistant-led socket: %s", exc)


def _next_occurrence(hour: int, minute: int) -> float:
    """Epoch time of the next wall-clock occurrence of hour:minute -- today
    if it hasn't happened yet, else tomorrow."""
    now = time.localtime()
    target = time.struct_time(
        (now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, 0, 0, -1)
    )
    target_epoch = time.mktime(target)
    if target_epoch <= time.time():
        target_epoch += 86400
    return target_epoch


def _read_exact(stream, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


@dataclass
class Schedule:
    fire_at: float
    duration_s: float | None = None  # timer only, denominator for the LED fraction


class TimerAlarmState:
    """Persists timer/alarm as absolute fire_at timestamps so a service
    restart doesn't lose or reset either one (see module docstring)."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self.timer: Schedule | None = None
        self.alarm: Schedule | None = None
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        if data.get("timer"):
            self.timer = Schedule(**data["timer"])
        if data.get("alarm"):
            self.alarm = Schedule(**data["alarm"])

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        data = {
            "timer": asdict(self.timer) if self.timer else None,
            "alarm": asdict(self.alarm) if self.alarm else None,
        }
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self._path)

    def set_timer(self, seconds: float) -> None:
        with self._lock:
            self.timer = Schedule(fire_at=time.time() + seconds, duration_s=seconds)
            self._save()

    def cancel_timer(self) -> bool:
        with self._lock:
            had = self.timer is not None
            self.timer = None
            self._save()
            return had

    def set_alarm(self, hour: int, minute: int) -> None:
        with self._lock:
            self.alarm = Schedule(fire_at=_next_occurrence(hour, minute))
            self._save()

    def cancel_alarm(self) -> bool:
        with self._lock:
            had = self.alarm is not None
            self.alarm = None
            self._save()
            return had


class Ringer:
    """Owns looped playback + the offline stop-listener while something is
    ringing. One ring at a time (start() is a no-op if already ringing)."""

    def __init__(self) -> None:
        self._active_kind: str | None = None
        self._loop_handle: sound_fx.LoopHandle | None = None
        self._stop_event = threading.Event()
        self._listener_thread: threading.Thread | None = None
        self._safety_handle: asyncio.TimerHandle | None = None
        self._lock = threading.Lock()

    def is_ringing(self) -> bool:
        with self._lock:
            return self._active_kind is not None

    async def start(self, kind: str) -> None:
        with self._lock:
            if self._active_kind is not None:
                return
            self._active_kind = kind

        LOG.info("%s firing -- starting ring", kind)
        send_led("ringing")
        self._loop_handle = sound_fx.play_loop(kind)
        if self._loop_handle is None:
            LOG.warning(
                "No %s sound to play (see sound_fx.SOUNDS/SOUND_FX_DIR) -- "
                "ringing silently, stop-listener still active", kind
            )

        self._stop_event.clear()
        if not await wait_for_node(XVF_SOURCE):
            LOG.error(
                "XVF source %r never appeared; stop-listener will likely "
                "capture the wrong mic", XVF_SOURCE,
            )
        self._listener_thread = threading.Thread(
            target=self._listen_for_stop, name="timer-alarm-stop-listener", daemon=True
        )
        self._listener_thread.start()

        loop = asyncio.get_running_loop()
        self._safety_handle = loop.call_later(
            MAX_RING_SECONDS, lambda: asyncio.ensure_future(self.stop())
        )

    def _listen_for_stop(self) -> None:
        import vosk

        vosk.SetLogLevel(-1)
        try:
            model = vosk.Model(STOP_MODEL_DIR)
        except Exception:
            LOG.exception("Could not load Vosk model for stop-listener")
            return

        recognizer = vosk.KaldiRecognizer(model, SAMPLE_RATE, json.dumps(_STOP_GRAMMAR))
        cmd = [
            "pw-cat", "--record", "--target", XVF_SOURCE,
            "--rate", str(SAMPLE_RATE), "--channels", str(CAPTURE_CHANNELS),
            "--format", "s16", "--latency", "40ms", "-",
        ]
        LOG.info("Stop-listener: starting capture")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=STOP_FRAME_BYTES * 4)
        try:
            while not self._stop_event.is_set():
                raw = _read_exact(proc.stdout, STOP_FRAME_BYTES)
                if raw is None:
                    LOG.warning("Stop-listener: capture stream ended")
                    break
                stereo = np.frombuffer(raw, dtype=np.int16).reshape(-1, CAPTURE_CHANNELS)
                mono = np.ascontiguousarray(stereo[:, CHANNEL_INDEX]).tobytes()
                if recognizer.AcceptWaveform(mono):
                    text = json.loads(recognizer.Result()).get("text", "")
                else:
                    text = json.loads(recognizer.PartialResult()).get("partial", "")
                if text and text != "[unk]" and any(w in text for w in _STOP_WORDS):
                    LOG.info("Stop-listener heard: %r", text)
                    self._stop_event.set()
                    break
        except Exception:
            LOG.exception("Stop-listener crashed")
        finally:
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)

    async def stop(self) -> bool:
        with self._lock:
            if self._active_kind is None:
                return False
            self._active_kind = None

        if self._safety_handle is not None:
            self._safety_handle.cancel()
            self._safety_handle = None
        self._stop_event.set()
        if self._loop_handle is not None:
            self._loop_handle.stop()
            self._loop_handle = None
        if self._listener_thread is not None:
            await asyncio.to_thread(self._listener_thread.join, 3)
            self._listener_thread = None
        LOG.info("Ring stopped")
        send_led("idle")
        return True


class Scheduler:
    """Ticks once a second: fires an overdue timer/alarm, and keeps the LED's
    background timer-countdown display in sync with current state."""

    def __init__(self, state: TimerAlarmState, ringer: Ringer) -> None:
        self.state = state
        self.ringer = ringer
        self._had_timer_display = False

    async def run(self) -> None:
        while True:
            now = time.time()

            if (
                self.state.timer is not None
                and now >= self.state.timer.fire_at
                and not self.ringer.is_ringing()
            ):
                self.state.cancel_timer()
                if self._had_timer_display:
                    send_led("timer clear")
                    self._had_timer_display = False
                await self.ringer.start("timer")

            if (
                self.state.alarm is not None
                and now >= self.state.alarm.fire_at
                and not self.ringer.is_ringing()
            ):
                self.state.cancel_alarm()
                await self.ringer.start("alarm")

            if self.state.timer is not None and not self.ringer.is_ringing():
                remaining = max(0.0, self.state.timer.fire_at - now)
                fraction = 1.0
                if self.state.timer.duration_s:
                    fraction = clamp(remaining / self.state.timer.duration_s, 0.0, 1.0)
                send_led(f"timer {fraction:.3f}")
                self._had_timer_display = True
            elif self._had_timer_display:
                send_led("timer clear")
                self._had_timer_display = False

            await asyncio.sleep(TICK_SECONDS)


async def dispatch(cmd: dict, state: TimerAlarmState, ringer: Ringer) -> dict:
    name = cmd.get("cmd")

    if name == "set_timer":
        seconds = cmd.get("seconds")
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return {"ok": False, "error": "invalid seconds"}
        state.set_timer(float(seconds))
        LOG.info("Timer set for %.0fs", seconds)
        return {"ok": True}

    if name == "cancel_timer":
        had = state.cancel_timer()
        return {"ok": had, "error": "" if had else "no active timer"}

    if name == "set_alarm":
        hour, minute = cmd.get("hour"), cmd.get("minute")
        if (
            not isinstance(hour, int) or not isinstance(minute, int)
            or not (0 <= hour <= 23 and 0 <= minute <= 59)
        ):
            return {"ok": False, "error": "invalid time"}
        state.set_alarm(hour, minute)
        LOG.info("Alarm set for %02d:%02d", hour, minute)
        return {"ok": True}

    if name == "cancel_alarm":
        had = state.cancel_alarm()
        return {"ok": had, "error": "" if had else "no active alarm"}

    if name == "status":
        return {
            "ringing": ringer.is_ringing(),
            "timer": asdict(state.timer) if state.timer else None,
            "alarm": asdict(state.alarm) if state.alarm else None,
        }

    if name == "stop_ring":
        stopped = await ringer.stop()
        return {"ok": stopped, "error": "" if stopped else "nothing ringing"}

    return {"ok": False, "error": f"unknown cmd: {name!r}"}


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    state: TimerAlarmState,
    ringer: Ringer,
) -> None:
    try:
        line = await reader.readline()
        if not line:
            return
        try:
            cmd = json.loads(line.decode())
        except json.JSONDecodeError:
            response = {"ok": False, "error": "bad json"}
        else:
            response = await dispatch(cmd, state, ringer)
        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def main_async() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    state = TimerAlarmState(STATE_PATH)
    ringer = Ringer()
    scheduler = Scheduler(state, ringer)

    with contextlib.suppress(FileNotFoundError):
        os.unlink(TIMER_ALARM_SOCKET)
    os.makedirs(os.path.dirname(TIMER_ALARM_SOCKET), exist_ok=True)

    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(r, w, state, ringer), path=TIMER_ALARM_SOCKET
    )
    os.chmod(TIMER_ALARM_SOCKET, 0o660)
    LOG.info("Listening on %s", TIMER_ALARM_SOCKET)
    if state.timer:
        LOG.info("Restored timer, fires at %s", time.ctime(state.timer.fire_at))
    if state.alarm:
        LOG.info("Restored alarm, fires at %s", time.ctime(state.alarm.fire_at))

    async with server:
        await asyncio.gather(server.serve_forever(), scheduler.run())
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
