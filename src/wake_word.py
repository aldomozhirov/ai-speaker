#!/usr/bin/env python3
"""openWakeWord service for the AI speaker.

Reads 16 kHz mono PCM from the XVF3800's *processed* capture channel (post-AEC,
post-beamform -- never raw mics) via a `pw-cat` subprocess, and runs it through
openWakeWord. It also listens on a UNIX socket (WAKE_TRIGGER_SOCKET) for
manual wake triggers, e.g. from the wake/assistant GPIO button handled in
volume-buttons.py -- so button and voice both funnel through one place. Every
trigger (voice or button) is relayed to voice_agent.py over VOICE_SESSION_SOCKET,
which owns session-active state, LED state from "listening" onward, and the
actual Gemini Live conversation (Phase 5). This service prints a JSON relay
line to stdout (captured by the journal) for anything downstream to consume.

One rule holds this file together: every frame read from the capture pipe
goes through Listener.ingest(), in order, no matter what else is happening.
See Listener for what broke when that was not true.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pickle
import select
import socket
import subprocess
import sys
import threading
import time

import numpy as np
import openwakeword
from openwakeword.model import Model

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_actions  # noqa: E402
import local_intent  # noqa: E402
import sound_fx  # noqa: E402
import timer_alarm_client  # noqa: E402
import wake_capture  # noqa: E402
import wake_confirm  # noqa: E402
from pw_node import wait_for_node  # noqa: E402

LOG = logging.getLogger("wake-word")

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80ms, openWakeWord's native chunk size
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
CAPTURE_CHANNELS = 2
FRAME_BYTES = FRAME_SAMPLES * CAPTURE_CHANNELS * 2  # s16, interleaved

XVF_SOURCE = os.environ.get(
    "WAKE_WORD_SOURCE",
    "alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993700254500350-00.analog-stereo",
)
# The XVF3800's L/R USB capture channels are independently configured
# (AUDIO_MGR_OP_L / AUDIO_MGR_OP_R report different category/source pairs on
# this unit -- they are NOT a plain stereo pair). Averaging them, which is
# what a naive 2ch->1ch downmix does, mixes two different signals together
# and measurably hurts detection range. Pick one channel explicitly instead;
# 0 = L (FL), 1 = R (FR). Compare with WAKE_WORD_CHANNEL=1 if L underperforms.
CHANNEL_INDEX = int(os.environ.get("WAKE_WORD_CHANNEL", "0"))
WAKEWORD_MODEL = os.environ.get("WAKE_WORD_MODEL", "hey_jarvis")
THRESHOLD = float(os.environ.get("WAKE_WORD_THRESHOLD", "0.5"))
PATIENCE_FRAMES = int(os.environ.get("WAKE_WORD_PATIENCE", "2"))
COOLDOWN_SECONDS = float(os.environ.get("WAKE_WORD_COOLDOWN", "1.5"))
DEBUG_SCORES = os.environ.get("WAKE_WORD_DEBUG", "0").lower() in {"1", "true", "yes"}
# Scores above this are journalled per frame when WAKE_WORD_DEBUG is on.
DEBUG_SCORE_FLOOR = 0.05

# One detection per utterance -- see DetectionGate. After firing, the detector
# stays disarmed for at least WAKE_WORD_REFRACTORY seconds and until the score
# has dropped back below WAKE_WORD_REARM_SCORE.
REFRACTORY_SECONDS = float(os.environ.get("WAKE_WORD_REFRACTORY", "2.0"))
REARM_SCORE = float(os.environ.get("WAKE_WORD_REARM_SCORE", str(THRESHOLD / 2)))

# Second opinion on every frame that reaches WAKE_WORD_THRESHOLD -- see
# Verifier. The model file is written by tools/wake/wake-verifier-train.py
# from sorted captures; no file (or WAKE_WORD_VERIFIER=0) means no verifier.
VERIFIER_PATH = os.environ.get(
    "WAKE_WORD_VERIFIER",
    os.path.expanduser("~/.local/share/ai-speaker/wake-verifier/{model}.pkl"),
)
VERIFIER_THRESHOLD = float(os.environ.get("WAKE_WORD_VERIFIER_THRESHOLD", "0.5"))

# Capture health -- see read_exact() and MicWatchdog.
STALL_SECONDS = float(os.environ.get("WAKE_WORD_STALL_SECONDS", "10"))
SILENCE_SECONDS = float(os.environ.get("WAKE_WORD_SILENCE_SECONDS", "10"))
SILENCE_RECHECK_SECONDS = float(os.environ.get("WAKE_WORD_SILENCE_RECHECK_SECONDS", "60"))
# While the microphone node is missing: how often to look for it, and how
# often to say so in the journal.
ABSENT_POLL_SECONDS = 5.0
PROBLEM_LOG_INTERVAL_SECONDS = 600.0
HEALTH_FILE = os.environ.get(
    "WAKE_WORD_HEALTH_FILE",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "wake-word-health.json"),
)

VOICE_SESSION_SOCKET = os.environ.get(
    "VOICE_SESSION_SOCKET",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "voice-session.sock"),
)
WAKE_TRIGGER_SOCKET = os.environ.get(
    "WAKE_TRIGGER_SOCKET",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "wake-trigger.sock"),
)


class WakeTrigger:
    """Cooldown-debounced relay from a wake source (voice detection or an
    external source like the GPIO button) to voice_agent.py's session socket.

    voice_agent.py (Phase 5+) is the single owner of session-active state and
    every LED state from "listening" onward, including push-to-cancel
    semantics (button while already listening/speaking = cancel). This class
    only exists to debounce rapid-fire voice detections within the same
    utterance before relaying -- the button isn't debounced the same way,
    every press is forwarded.

    A voice detection can also be relayed tentatively (fire(tentative=True),
    used when the second opinion of wake_confirm.py may block): voice_agent.py
    then opens the microphone but shows and plays nothing until settle() has
    told it whether the second opinion agreed.
    """

    def __init__(self, cooldown_seconds: float) -> None:
        self._cooldown = cooldown_seconds
        self._lock = threading.Lock()
        self._last_voice_relay = 0.0
        self._unsettled = False
        # Called with the source name on every non-voice trigger. main() points
        # it at the capture recorder: a button press right after an unanswered
        # "Hey Jarvis" is the only evidence a total miss leaves behind.
        self.on_external = None

    def _emit(self, source: str, score: float | None) -> None:
        print(
            json.dumps(
                {
                    "event": "relay",
                    "source": source,
                    "score": round(float(score), 4) if score is not None else None,
                    "ts": time.time(),
                }
            ),
            flush=True,
        )

    def _relay(self, source: str) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                s.connect(VOICE_SESSION_SOCKET)
                s.sendall((source + "\n").encode())
        except OSError as exc:
            LOG.warning("Could not reach voice-session socket %s: %s", VOICE_SESSION_SOCKET, exc)

    def fire(self, source: str, score: float | None = None, tentative: bool = False) -> bool:
        if source != "voice" and self.on_external is not None:
            self.on_external(source)

        # A timer/alarm ringing takes priority over anything else a wake
        # trigger would normally do: silence it instead of also opening a
        # voice session (or, for the button, instead of relaying at all) --
        # same "button during an active state means cancel/stop" convention
        # documented on this class already, just extended to timer_alarm.py.
        if timer_alarm_client.is_ringing():
            ok, _ = timer_alarm_client.stop_ring()
            LOG.info("Wake trigger (%s) silenced an active timer/alarm ring (ok=%s)", source, ok)
            return ok

        if source != "button":
            now = time.monotonic()
            with self._lock:
                if (now - self._last_voice_relay) < self._cooldown:
                    return False
                self._last_voice_relay = now

        if score is None:
            LOG.info("Relaying wake trigger from %s", source)
        else:
            LOG.info("Relaying wake word detection (score=%.3f)", score)
        self._emit(source, score)
        if tentative:
            with self._lock:
                self._unsettled = True
            self._relay(source + "-pending")
        else:
            self._relay(source)
        return True

    def settle(self, opinion) -> None:
        """Tell voice_agent.py what became of the last tentative relay. Called
        by the second opinion's thread; `opinion` is None when it could not
        judge, which lets the session through."""
        with self._lock:
            unsettled, self._unsettled = self._unsettled, False
        if not unsettled:  # e.g. the detection silenced a ringing timer instead
            return
        if opinion is None or opinion.passed:
            LOG.info("Wake word confirmed%s", "" if opinion else " (second opinion unavailable)")
            self._relay("voice-confirm")
        else:
            LOG.info("Wake word blocked by the second opinion (%s)", opinion.describe())
            self._relay("voice-reject")

    def mark_voice_activity(self) -> None:
        """Apply the same cooldown as fire(), without relaying anything --
        used when a wake word was handled by local_intent.py instead, so a
        second wake word said immediately after (e.g. echoed by a chime) is
        still debounced the same way a relayed one would be."""
        with self._lock:
            self._last_voice_relay = time.monotonic()

    def log_local_action(self, action: str, ok: bool, detail: str) -> None:
        print(
            json.dumps(
                {
                    "event": "local_action",
                    "action": action,
                    "ok": ok,
                    "detail": detail,
                    "ts": time.time(),
                }
            ),
            flush=True,
        )
        LOG.info("Handled locally: %s (ok=%s%s)", action, ok, f", {detail}" if detail else "")


class WakeTriggerListener(threading.Thread):
    """Accepts external wake triggers over a UNIX socket. Any client that
    connects and sends a line (e.g. "button\\n") counts as a trigger; the
    line content is used as the reported source."""

    def __init__(self, socket_path: str, trigger: WakeTrigger) -> None:
        super().__init__(name="wake-trigger-socket", daemon=True)
        self._socket_path = socket_path
        self._trigger = trigger
        self._stop_event = threading.Event()
        self._server: socket.socket | None = None

    def stop(self) -> None:
        self._stop_event.set()
        with contextlib.suppress(Exception):
            if self._server is not None:
                self._server.close()

    def _handle_client(self, conn: socket.socket) -> None:
        with conn, conn.makefile("r") as f:
            line = f.readline()
        source = line.strip() or "external"
        self._trigger.fire(source)

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
            LOG.info("Listening for external wake triggers on %s", self._socket_path)
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


class CaptureProblem(Exception):
    """The capture stream is open but not usable. `state` is what gets
    published in the health file."""

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state


def open_capture() -> subprocess.Popen:
    cmd = [
        "pw-cat",
        "--record",
        # Headerless PCM. Without --raw, pw-cat starts the stream with a
        # 24-byte AU file header ("dns."), which decodes as six samples of
        # loud garbage at the top of every capture.
        "--raw",
        "--target",
        XVF_SOURCE,
        # Deliberately NOT `-P '{ node.dont-fallback = true }'`. It looks like
        # the cure for pw-cat silently recording the default source when the
        # target is missing, but with WirePlumber 0.5.8 it kills healthy
        # streams: every linked stream gets target.node=-1 metadata, and the
        # next time the policy re-checks it (i.e. whenever any other stream
        # starts or stops) that -1 plus dont-fallback is "defined target not
        # found" and the stream is destroyed. Tried on 2026-10-04: this
        # capture and the voice session's own died at every wake word. A
        # missing array is handled in main() and by MicWatchdog instead.
        "--rate",
        str(SAMPLE_RATE),
        "--channels",
        str(CAPTURE_CHANNELS),
        "--format",
        "s16",
        "--latency",
        "40ms",
        "-",
    ]
    LOG.info("Starting capture: %s", " ".join(cmd))
    # Unbuffered so read_exact() can select() on the pipe for its stall check.
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)


def read_exact(stream, n: int, timeout: float | None = None) -> bytes | None:
    """Read exactly `n` bytes; None once the stream has ended.

    With `timeout`, raises CaptureProblem if the stream delivers nothing for
    that long. A linked PipeWire capture produces data continuously, silence
    included, so a stall means the stream has lost its node.
    """
    buf = b""
    while len(buf) < n:
        if timeout is not None:
            ready, _, _ = select.select([stream], [], [], timeout)
            if not ready:
                raise CaptureProblem(
                    "capture_stalled", f"no audio from the capture stream for {timeout:.0f}s"
                )
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class Health:
    """Publishes this service's view of the microphone as a small JSON file
    for ai-speaker-status.sh. "The unit is active" says nothing about whether
    it can hear: the array was unplugged for the whole 2026-09-08..20 boot and
    its DSP was wedged for a quarter of an hour on 2026-10-04, and both times
    every check in the status script stayed green."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._state: str | None = None

    def set(self, state: str, detail: str = "") -> None:
        if state == self._state:
            return
        self._state = state
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"state": state, "detail": detail, "since": time.time()}, f)
            os.replace(tmp, self._path)
        except OSError as exc:
            LOG.warning("Could not write %s: %s", self._path, exc)


class MicWatchdog:
    """Notices a capture stream that delivers exact digital zeros.

    A live XVF3800 never does that for long -- its idle output sits around
    -60..-70 dBFS, not at zero. Sustained zeros on both channels mean the
    stream is not carrying the microphones: the chip's DSP is wedged, the
    array is muted, or the stream is attached to something else. Raises
    CaptureProblem so main() reopens the capture, in case the fault is the
    stream rather than the device.
    """

    def __init__(self, health: Health) -> None:
        self._health = health
        self._zero_frames = 0
        self._silent = False

    def update(self, stereo: np.ndarray) -> None:
        if stereo.any():
            if self._silent:
                LOG.warning("Microphone signal is back")
                self._silent = False
                _problem_logged.clear()
            self._zero_frames = 0
            self._health.set("ok")
            return
        self._zero_frames += 1
        limit = SILENCE_RECHECK_SECONDS if self._silent else SILENCE_SECONDS
        if self._zero_frames * FRAME_SECONDS >= limit:
            self._zero_frames = 0
            self._silent = True
            raise CaptureProblem(
                "mic_silent",
                "the microphone stream is digital silence on both channels "
                "(XVF3800 DSP wedged, array muted, or stream on the wrong source)",
            )


_problem_logged: dict[str, float] = {}


def log_problem(key: str, message: str, *args) -> None:
    """ERROR the first time a problem shows up, then at most once per
    PROBLEM_LOG_INTERVAL_SECONDS while it persists -- a dead microphone can
    last for days and must not bury everything else in the journal."""
    now = time.monotonic()
    last = _problem_logged.get(key)
    if last is not None and now - last < PROBLEM_LOG_INTERVAL_SECONDS:
        return
    _problem_logged[key] = now
    LOG.error(message, *args)


class Verifier:
    """Tells this household's "Hey Jarvis" from other speech that excites the
    base model.

    The threshold has to sit at 0.10 because the real phrase often scores only
    0.12-0.30 here, and at 0.10 ordinary speech gets through: on 2026-10-05 a
    meeting playing in the room fired 50 times in 35 minutes, at scores up to
    0.50. No threshold separates the two. A logistic regression over the same
    embedding window the base model has just scored does (held out: 93% of 33
    real wake-ups kept, 0 of 62 false ones passed), so a frame only counts
    towards a detection when both agree.
    """

    def __init__(self, pipeline, window: int, threshold: float) -> None:
        self._pipeline = pipeline
        self._window = window
        self.threshold = threshold

    @classmethod
    def load(cls, path: str, model_name: str, threshold: float) -> "Verifier | None":
        if path.lower() in {"", "0", "off", "none"}:
            return None
        path = path.format(model=model_name)
        if not os.path.exists(path):
            LOG.warning("No wake-word verifier at %s; running without one", path)
            return None
        try:
            with open(path, "rb") as handle:
                saved = pickle.load(handle)
            if saved["model"] != model_name:
                raise ValueError(f"trained for {saved['model']}, not {model_name}")
            verifier = cls(saved["pipeline"], int(saved["window"]), threshold)
        except Exception:
            # A broken file must not take the wake word down with it.
            LOG.exception("Could not load wake-word verifier %s; running without one", path)
            return None
        LOG.info(
            "Wake-word verifier %s (trained %s on %s) threshold=%.2f",
            path, saved.get("trained"), saved.get("captures"), threshold,
        )
        return verifier

    def confidence(self, model) -> float:
        """Probability that the window the model scored last is a real wake-up."""
        features = model.preprocessor.get_features(self._window)[0]
        return float(self._pipeline.predict_proba(features.reshape(1, -1))[0, 1])


class DetectionGate:
    """Turns the per-frame score into at most one detection per utterance.

    openWakeWord rescans a sliding ~1.3 s window every 80 ms, so a single
    "Hey Jarvis" scores above threshold for several frames in a row. Fire on
    the first frame that satisfies the patience rule (`patience` consecutive
    frames at or above `threshold`, as openWakeWord's own patience argument
    defines it), then stay disarmed until `refractory_frames` have gone by
    *and* the score has fallen below `rearm_score`. A frame the Verifier
    turned down (`verified=False`) does not count towards patience.

    Counted in frames rather than wall-clock time, so the gate behaves the
    same when the loop is working through a backlog, and in offline replay.
    """

    def __init__(
        self, threshold: float, patience: int, rearm_score: float, refractory_frames: int
    ) -> None:
        self._threshold = threshold
        self._patience = max(1, patience)
        self._rearm_score = rearm_score
        self._refractory_frames = refractory_frames
        self._run = 0
        self._armed = True
        self._since_fire = 0

    @property
    def passing(self) -> bool:
        """Whether the latest frame satisfied the patience rule, armed or not."""
        return self._run >= self._patience

    def update(self, score: float, verified: bool = True) -> bool:
        self._run = self._run + 1 if score >= self._threshold and verified else 0
        if not self._armed:
            self._since_fire += 1
            if self._since_fire >= self._refractory_frames and score < self._rearm_score:
                self._armed = True
            return False
        if self.passing:
            self.hold()
            return True
        return False

    def hold(self) -> None:
        """Disarm and restart the refractory period."""
        self._armed = False
        self._since_fire = 0


class Listener:
    """The one place a captured frame is consumed.

    Every frame read from the capture pipe has to pass through ingest(), in
    order. The journal for 2026-09-20..10-03 shows what happens otherwise:
    the local-intent handler used to read its command audio straight from the
    pipe, so openWakeWord's sliding window stopped advancing with "Hey
    Jarvis" still inside it, and the first frame fed after the handler
    returned scored high all over again. 93 triggers came from 41 utterances,
    in chains of up to ten, and sessions kept opening with nobody speaking.
    """

    def __init__(
        self,
        model,
        model_name: str,
        gate: DetectionGate,
        recorder=None,
        watchdog: MicWatchdog | None = None,
        threshold: float = THRESHOLD,
        verifier: Verifier | None = None,
        second_opinion: "wake_confirm.SecondOpinion | None" = None,
    ) -> None:
        self._model = model
        self._model_name = model_name
        self._threshold = threshold
        self._verifier = verifier
        self.second_opinion = second_opinion
        self._watchdog = watchdog
        self.gate = gate
        self.recorder = recorder if recorder is not None else wake_capture.NullRecorder()
        self._event: dict | None = None

    def ingest(self, raw: bytes) -> tuple[np.ndarray, float, bool]:
        """Returns (mono frame on the configured channel, raw score, fired)."""
        stereo = np.frombuffer(raw, dtype=np.int16).reshape(-1, CAPTURE_CHANNELS)
        if self._watchdog is not None:
            self._watchdog.update(stereo)
        mono = np.ascontiguousarray(stereo[:, CHANNEL_INDEX])
        score = float(self._model.predict(mono).get(self._model_name, 0.0))
        confidence = None
        if self._verifier is not None and score >= self._threshold:
            confidence = self._verifier.confidence(self._model)
            LOG.info(
                "verifier=%.3f %s (raw_score=%.3f)",
                confidence,
                "ok" if confidence >= self._verifier.threshold else "rejected",
                score,
            )
        fired = self.gate.update(
            score, confidence is None or confidence >= self._verifier.threshold
        )
        if self.second_opinion is not None:
            # Does not change `fired`; see _handle_voice_detection for what it
            # may block.
            verdict = ""
            if confidence is not None:
                verdict = "ok" if confidence >= self._verifier.threshold else "rejected"
            self.second_opinion.push(mono, score, verdict)
        if DEBUG_SCORES:
            self._log_scores(score, fired, confidence)
        self.recorder.push(raw, score, fired)
        return mono, score, fired

    def _log_scores(self, score: float, fired: bool, confidence: float | None = None) -> None:
        # Log every frame while there's actual signal (not just a 1-in-N
        # sample) so a ~700ms phrase's true peak isn't missed; stay quiet
        # during silence so the journal isn't flooded. gated_score is the
        # score with the patience rule applied, as openWakeWord would report
        # it, and is what tools reading the journal already expect.
        if score > DEBUG_SCORE_FLOOR:
            LOG.info(
                "raw_score=%.3f gated_score=%.3f", score, score if self.gate.passing else 0.0
            )
            if self._event is None:
                self._event = {"peak": 0.0, "over": 0, "fired": False, "quiet": 0, "verifier": None}
            event = self._event
            event["peak"] = max(event["peak"], score)
            if confidence is not None:
                event["verifier"] = max(event["verifier"] or 0.0, confidence)
                # Into the capture's sidecar too: these are tomorrow's training labels.
                self.recorder.annotate(verifier=round(event["verifier"], 3))
            event["over"] += score >= self._threshold
            event["quiet"] = 0
        elif self._event is not None:
            self._event["quiet"] += 1
        if self._event is not None:
            event = self._event
            event["fired"] = event["fired"] or fired
            # One line per utterance with its real peak. The score at the
            # moment of firing is only the rising edge -- the frame that first
            # crossed -- and badly understates how well the phrase scored.
            if event["quiet"] * FRAME_SECONDS >= 1.0:
                LOG.info(
                    "wake event: peak=%.3f frames_over_threshold=%d fired=%s%s",
                    event["peak"],
                    event["over"],
                    "yes" if event["fired"] else "no",
                    "" if event["verifier"] is None else " verifier=%.3f" % event["verifier"],
                )
                self._event = None


# How long the continuation pass (see _handle_voice_detection) listens for
# the rest of a truncated timer/alarm phrase, and how long it waits for the
# user to resume before concluding they already finished talking. Both are
# only ever paid on a phrase that already looked truncated, so short
# commands keep their original latency.
LOCAL_INTENT_CONTINUE_SECONDS = float(
    os.environ.get("LOCAL_INTENT_CONTINUE_SECONDS", "3.0")
)
LOCAL_INTENT_CONTINUE_LEAD_SECONDS = float(
    os.environ.get("LOCAL_INTENT_CONTINUE_LEAD_SECONDS", "1.2")
)


def _buffer_command_audio(stdout, listener: Listener, max_seconds: float | None = None,
                          lead_silence_seconds: float | None = None) -> bytes:
    """Read frames from the same live capture stream right after a wake
    trigger, for local_intent.py matching. Mono, same channel openWakeWord
    itself uses. Stops early once trailing near-silence follows real signal,
    else after `max_seconds` (default LOCAL_INTENT_WINDOW_SECONDS) -- most
    commands are a second or two, no need to pay the full window on every
    one.

    Frames still go through listener.ingest() so the detector keeps seeing
    the stream (see Listener); a wake word said again inside this window is
    part of the command audio and does not start a second detection.

    `lead_silence_seconds` bounds how long to wait for the user to say
    anything at all before giving up. Only the continuation pass sets it: on
    the first pass the user is definitely mid-command, but a continuation
    may find they already finished, and waiting the whole window there would
    add dead air before the fall-through to a cloud session.
    """
    if max_seconds is None:
        max_seconds = local_intent.LOCAL_INTENT_WINDOW_SECONDS
    max_frames = int(max_seconds * SAMPLE_RATE / FRAME_SAMPLES)
    silence_frames_needed = max(
        1, int(local_intent.LOCAL_INTENT_SILENCE_CUTOFF_SECONDS * SAMPLE_RATE / FRAME_SAMPLES)
    )
    lead_frames_allowed = (
        None if lead_silence_seconds is None
        else max(1, int(lead_silence_seconds * SAMPLE_RATE / FRAME_SAMPLES))
    )
    chunks: list[bytes] = []
    heard_signal = False
    silence_run = 0
    for _ in range(max_frames):
        raw = read_exact(stdout, FRAME_BYTES, STALL_SECONDS)
        if raw is None:
            break
        mono, _, _ = listener.ingest(raw)
        chunks.append(mono.tobytes())
        peak = int(np.abs(mono).max())
        if peak >= local_intent.LOCAL_INTENT_SILENCE_PEAK:
            heard_signal = True
            silence_run = 0
        else:
            silence_run += 1
        if heard_signal and silence_run >= silence_frames_needed:
            break
        if (
            not heard_signal
            and lead_frames_allowed is not None
            and silence_run >= lead_frames_allowed
        ):
            break
    return b"".join(chunks)


def _handle_voice_detection(stdout, listener: Listener, trigger: "WakeTrigger",
                            score: float) -> None:
    """Called on a wake-word detection. If local_intent is enabled, tries to
    match+handle the immediately-following command locally; only relays to
    voice_agent.py (opening a full Gemini session) if local matching is
    disabled or didn't match -- see local_intent.py for the trade-offs of
    this being on."""
    if not local_intent.available():
        second = listener.second_opinion
        if second is not None and second.blocking:
            # The session opens only tentatively; the second opinion settles
            # it a moment later (wake_confirm.py), from its own thread.
            relayed = trigger.fire("voice", score, tentative=True)
            if relayed:
                second.decide(score, trigger.settle)
            listener.recorder.annotate(relayed=relayed, tentative=True)
            return
        listener.recorder.annotate(relayed=trigger.fire("voice", score))
        return

    audio = _buffer_command_audio(stdout, listener)
    try:
        hit = local_intent.match(audio, SAMPLE_RATE)
        # "set a timer for" <pause> "ten minutes" trips the silence cutoff
        # mid-phrase, so the number lands in a window of its own with no
        # "timer" token in it and neither half parses (observed live
        # 2026-08-30). Keep listening and re-match the joined audio rather
        # than fall through to a cloud session.
        if hit is None and local_intent.looks_truncated(local_intent.last_text()):
            LOG.info(
                "local_intent: %r looks truncated -- listening for the rest",
                local_intent.last_text(),
            )
            audio += _buffer_command_audio(
                stdout,
                listener,
                max_seconds=LOCAL_INTENT_CONTINUE_SECONDS,
                lead_silence_seconds=LOCAL_INTENT_CONTINUE_LEAD_SECONDS,
            )
            hit = local_intent.match(audio, SAMPLE_RATE)
    except CaptureProblem:
        raise
    except Exception:
        LOG.exception("local_intent.match failed; falling back to a cloud session")
        hit = None

    # Whatever was said during the window above belongs to this detection.
    listener.gate.hold()

    if hit is None:
        listener.recorder.annotate(
            relayed=trigger.fire("voice", score), local_intent_heard=local_intent.last_text()
        )
        return

    action, kwargs = hit
    ok, detail = asyncio.run(local_actions.run_action(action, kwargs))
    trigger.mark_voice_activity()
    trigger.log_local_action(action, ok, detail)
    listener.recorder.annotate(relayed=False, local_action=action)
    if ok:
        sound_fx.play("session-ended")


def run_stream(stdout, listener: Listener, trigger: WakeTrigger) -> None:
    """Feed one capture stream to the detector until the stream ends."""
    while True:
        raw = read_exact(stdout, FRAME_BYTES, STALL_SECONDS)
        if raw is None:
            return
        _, score, fired = listener.ingest(raw)
        if fired:
            _handle_voice_detection(stdout, listener, trigger, score)


def load_model() -> tuple[Model, str] | None:
    candidates = [
        p
        for p in openwakeword.get_pretrained_model_paths()
        if os.path.basename(p).startswith(WAKEWORD_MODEL)
    ]
    if not candidates:
        LOG.error(
            "No bundled openWakeWord model matches %r (available under %s)",
            WAKEWORD_MODEL,
            os.path.dirname(openwakeword.get_pretrained_model_paths()[0]),
        )
        return None
    model_path = candidates[0]
    model_name = os.path.basename(model_path)[:-5]  # strip ".onnx", matches Model's own naming

    LOG.info(
        "Loading openWakeWord model=%s (%s) threshold=%.2f patience=%d refractory=%.1fs",
        model_name,
        model_path,
        THRESHOLD,
        PATIENCE_FRAMES,
        REFRACTORY_SECONDS,
    )
    return Model(wakeword_model_paths=[model_path]), model_name


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    loaded = load_model()
    if loaded is None:
        return 1
    model, model_name = loaded

    if local_intent.available():
        LOG.info(
            "local_intent enabled: window=%.1fs model=%s",
            local_intent.LOCAL_INTENT_WINDOW_SECONDS,
            local_intent.LOCAL_INTENT_MODEL_DIR,
        )

    if wake_capture.ENABLED:
        recorder = wake_capture.CaptureRecorder(
            sample_rate=SAMPLE_RATE,
            channels=CAPTURE_CHANNELS,
            frame_samples=FRAME_SAMPLES,
            meta={
                "model": model_name,
                "wake_channel": CHANNEL_INDEX,
                "threshold": THRESHOLD,
                "patience": PATIENCE_FRAMES,
            },
        )
        LOG.info(
            "Near-trigger capture enabled: %s (score > %.2f, %.0fs before / %.0fs after, "
            "capped at %.0f MB)",
            wake_capture.DIRECTORY,
            wake_capture.SCORE,
            wake_capture.PRE_SECONDS,
            wake_capture.POST_SECONDS,
            wake_capture.MAX_MB,
        )
    else:
        recorder = wake_capture.NullRecorder()

    health = Health(HEALTH_FILE)
    health.set("starting")
    gate = DetectionGate(
        THRESHOLD, PATIENCE_FRAMES, REARM_SCORE, round(REFRACTORY_SECONDS / FRAME_SECONDS)
    )
    verifier = Verifier.load(VERIFIER_PATH, model_name, VERIFIER_THRESHOLD)
    second_opinion = wake_confirm.SecondOpinion.load(
        FRAME_SECONDS,
        THRESHOLD,
        # Into the capture's sidecar, next to the verifier's score.
        on_opinion=lambda opinion: recorder.annotate(
            second_opinion={
                "vosk": round(opinion.vosk, 3),
                "micro": round(opinion.micro, 3),
                "passed": opinion.passed,
            }
        ),
    )
    listener = Listener(
        model,
        model_name,
        gate,
        recorder,
        MicWatchdog(health),
        verifier=verifier,
        second_opinion=second_opinion,
    )

    trigger = WakeTrigger(COOLDOWN_SECONDS)
    trigger.on_external = recorder.request_snapshot
    trigger_listener = WakeTriggerListener(WAKE_TRIGGER_SOCKET, trigger)
    trigger_listener.start()

    backoff = 1.0
    while True:
        # pw-cat does not wait for a target that isn't there: it silently
        # records the default source instead (see pw_node.py) -- which with
        # the array unplugged is the snd-aloop capture, i.e. permanent
        # silence. So: no node, no capture. (If the array vanishes while a
        # capture is open, the stream is moved to that silent source and
        # MicWatchdog notices the zeros.) Button triggers keep working
        # meanwhile; they don't depend on this loop.
        if not asyncio.run(wait_for_node(XVF_SOURCE)):
            health.set("mic_absent", f"PipeWire node {XVF_SOURCE} is not present")
            log_problem(
                "mic_absent",
                "Microphone node %r is not present (array unplugged?); wake-word "
                "detection is OFF until it appears",
                XVF_SOURCE,
            )
            if not asyncio.run(
                wait_for_node(
                    XVF_SOURCE,
                    timeout=PROBLEM_LOG_INTERVAL_SECONDS,
                    interval=ABSENT_POLL_SECONDS,
                )
            ):
                continue
            LOG.warning("Microphone node %r appeared", XVF_SOURCE)
            _problem_logged.clear()

        proc = open_capture()
        assert proc.stdout is not None
        started = time.monotonic()
        try:
            run_stream(proc.stdout, listener, trigger)
            LOG.warning("Capture stream ended; restarting")
            if time.monotonic() - started < 5.0:
                health.set("capture_failing", "pw-cat exits right after starting")
        except CaptureProblem as exc:
            health.set(exc.state, str(exc))
            log_problem(exc.state, "Capture problem: %s; reopening the capture", exc)
        except Exception:
            LOG.exception("Capture loop failed")
        finally:
            recorder.flush()
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        # Only a capture that died young keeps backing off.
        if time.monotonic() - started > 60.0:
            backoff = 1.0
        time.sleep(backoff)
        backoff = min(backoff * 2, 15.0)


if __name__ == "__main__":
    sys.exit(main())
