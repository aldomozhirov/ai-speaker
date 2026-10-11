"""A second opinion on wake-word triggers, from two engines of a different kind.

Why it exists: the Verifier in wake_word.py still lets room speech through
(6-8 Oct 2026: 19 sessions opened with nobody asking, about six a day). A
comparison of five engines on 485 captures from this room
(tools/wake-bench in the ai-speaker repo) showed that none of them hears this household's
"Hey Jarvis" as often as openWakeWord does, but that two of them almost never
agree with its false triggers:

    Vosk, allowed to hear only "hey jarvis" / "jarvis" / something else
    microWakeWord's "Hey Jarvis" model, at 0.5 instead of its own 0.97

"Either of the two heard it" kept 34 of 42 real wake-ups and passed 0 of 62
hand-sorted false triggers and 0 of those 19 sessions.

wake_word.py hands it every frame; a little after a frame reaches the
wake-word threshold it judges the last few seconds on a thread of its own and
journals what it made of them. In "log" mode that is all. In "block" mode
(since 2026-10-09, after a day in which it would have stopped 10 of 10 false
wake-ups and kept 2 of 2 real ones) a detection opens the session only
tentatively and SecondOpinion.decide() settles it: see WakeTrigger in
wake_word.py and VoiceAgent in voice_agent.py. A judgement that fails counts
as "let it through", so a fault here cannot make the speaker deaf.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass

import numpy as np

LOG = logging.getLogger("wake-word.second-opinion")

SAMPLE_RATE = 16000
MODE = os.environ.get("WAKE_WORD_SECOND_OPINION", "0").lower()  # "block", "log" or off
VOSK_MODEL_DIR = os.environ.get(
    "WAKE_WORD_SECOND_OPINION_VOSK",
    os.path.expanduser("~/.local/share/ai-speaker/vosk-model-small-en-us-0.15"),
)
VOSK_CONFIDENCE = float(os.environ.get("WAKE_WORD_SECOND_OPINION_VOSK_CONFIDENCE", "0.5"))
MICRO_CUTOFF = float(os.environ.get("WAKE_WORD_SECOND_OPINION_MICRO_CUTOFF", "0.5"))
# What is judged: this much audio, ending this long after the frame that
# reached the threshold. openWakeWord crosses on the rising edge, often before
# the word is over, and a recogniser needs the whole word. On the captures
# 0.16 s after the trigger did as well as 0.4 s and 0.8 s (31-32 of 39-41 real
# wake-ups); in "block" mode this is time the user waits for the chime.
WINDOW_SECONDS = float(os.environ.get("WAKE_WORD_SECOND_OPINION_WINDOW", "3.0"))
DELAY_SECONDS = float(os.environ.get("WAKE_WORD_SECOND_OPINION_DELAY", "0.16"))


@dataclass(frozen=True)
class Opinion:
    vosk: float  # best confidence for the word "jarvis", 0.0 when not heard
    micro: float  # microWakeWord's peak probability
    passed: bool
    seconds: float  # how long judging took

    def describe(self) -> str:
        return "vosk=%.2f micro=%.2f -> %s (%.0f ms)" % (
            self.vosk, self.micro, "pass" if self.passed else "block", self.seconds * 1000,
        )


class Judge:
    """Decides whether a stretch of 16 kHz mono audio holds the wake word."""

    def __init__(self, vosk_model_dir: str = VOSK_MODEL_DIR,
                 vosk_confidence: float = VOSK_CONFIDENCE,
                 micro_cutoff: float = MICRO_CUTOFF) -> None:
        import vosk
        from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures, Model

        vosk.SetLogLevel(-1)  # Vosk logs its own C++-level lines to stderr by default.
        if not os.path.isdir(vosk_model_dir):
            raise FileNotFoundError(f"Vosk model dir not found: {vosk_model_dir}")
        self._vosk = vosk
        self._vosk_model = vosk.Model(vosk_model_dir)
        self._grammar = json.dumps(["hey jarvis", "jarvis", "[unk]"])
        self._micro = lambda: MicroWakeWord.from_builtin(Model.HEY_JARVIS)
        self._micro_features = MicroWakeWordFeatures
        self._vosk_confidence = vosk_confidence
        self._micro_cutoff = micro_cutoff

    def _vosk_score(self, pcm: np.ndarray) -> float:
        recogniser = self._vosk.KaldiRecognizer(self._vosk_model, SAMPLE_RATE, self._grammar)
        recogniser.SetWords(True)
        recogniser.AcceptWaveform(pcm.tobytes())
        words = json.loads(recogniser.FinalResult()).get("result", [])
        return max((float(w["conf"]) for w in words if w["word"] == "jarvis"), default=0.0)

    def _micro_score(self, pcm: np.ndarray) -> float:
        engine = self._micro()  # the model is streaming; a new one starts clean
        features = self._micro_features()
        peak = 0.0
        data = pcm.tobytes()
        try:
            for start in range(0, len(data), 320):  # 10 ms
                for frame in features.process_streaming(data[start:start + 320]):
                    peak = max(peak, float(engine.process_streaming_prob(frame) or 0.0))
        finally:
            engine.close()
        return peak

    def judge(self, pcm: np.ndarray) -> Opinion:
        started = time.monotonic()
        vosk_score = self._vosk_score(pcm)
        micro_score = self._micro_score(pcm)
        return Opinion(
            vosk=vosk_score,
            micro=micro_score,
            passed=vosk_score >= self._vosk_confidence or micro_score >= self._micro_cutoff,
            seconds=time.monotonic() - started,
        )


class SecondOpinion:
    """Watches the frame stream and has the Judge look at each trigger.

    push() is called for every frame from the capture loop and never blocks:
    it keeps the last WINDOW_SECONDS, and DELAY_SECONDS after a frame reached
    the threshold it hands a copy of that window to a worker thread. While one
    window is waiting for its delay, further frames over the threshold belong
    to it, so an utterance is judged once or twice, not once per frame.

    decide() asks for a judgement that somebody is waiting for: the window
    ending DELAY_SECONDS after the call, with the answer (an Opinion, or None
    if it could not be judged) handed to the given function on the worker
    thread. It takes over a window that is already waiting.
    """

    def __init__(self, judge: Judge, frame_seconds: float, threshold: float,
                 on_opinion=None, window_seconds: float = WINDOW_SECONDS,
                 delay_seconds: float = DELAY_SECONDS, threaded: bool = True) -> None:
        self._judge = judge
        self._threshold = threshold
        self._on_opinion = on_opinion
        self._ring: collections.deque = collections.deque(
            maxlen=max(1, round(window_seconds / frame_seconds)))
        self._delay_frames = max(0, round(delay_seconds / frame_seconds))
        self._countdown: int | None = None
        self._pending: tuple[float, str] | None = None
        self._on_decision = None
        self.blocking = False
        self._queue: queue.Queue | None = None
        if threaded:
            self._queue = queue.Queue(maxsize=4)
            threading.Thread(target=self._work, name="second-opinion", daemon=True).start()

    @classmethod
    def load(cls, frame_seconds: float, threshold: float, on_opinion=None) -> "SecondOpinion | None":
        if MODE in {"", "0", "off", "none"}:
            return None
        try:
            judge = Judge()
        except Exception:
            # A missing package or model must not take the wake word down with it.
            LOG.exception("Could not set up the second opinion; running without it")
            return None
        LOG.info(
            "Second opinion on (%s): vosk>=%.2f or micro>=%.2f, %.1fs window, %.2fs after the trigger",
            {"block": "blocks", "log": "log only"}.get(MODE, f"unknown mode {MODE!r}, log only"),
            VOSK_CONFIDENCE, MICRO_CUTOFF, WINDOW_SECONDS, DELAY_SECONDS,
        )
        second = cls(judge, frame_seconds, threshold, on_opinion)
        second.blocking = MODE == "block"
        return second

    def decide(self, score: float, on_decision) -> None:
        """Judge the window ending DELAY_SECONDS from now and tell `on_decision`."""
        self._countdown = self._delay_frames
        self._pending = (score, "fired")
        self._on_decision = on_decision

    def push(self, mono: np.ndarray, score: float, verdict: str = "") -> None:
        """`verdict` is what the live detector made of this frame, for the log."""
        self._ring.append(mono)
        if score >= self._threshold:
            if self._countdown is None:
                self._countdown = self._delay_frames
                self._pending = (score, verdict)
            elif score > self._pending[0]:
                keep = self._on_decision is not None or not verdict
                self._pending = (score, self._pending[1] if keep else verdict)
        if self._countdown is None:
            return
        if self._countdown > 0:
            self._countdown -= 1
            return
        job = (np.concatenate(self._ring), *self._pending, self._on_decision)
        self._countdown = self._pending = self._on_decision = None
        if self._queue is None:
            self._run(job)
            return
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            LOG.warning("Second opinion is falling behind; skipping a trigger")
            if job[3] is not None:
                job[3](None)

    def _work(self) -> None:
        while True:
            self._run(self._queue.get())

    def _run(self, job: tuple) -> None:
        pcm, score, verdict, on_decision = job
        try:
            opinion = self._judge.judge(pcm)
        except Exception:
            LOG.exception("Second opinion failed on a trigger")
            if on_decision is not None:
                on_decision(None)
            return
        LOG.info(
            "second opinion: %s raw_score=%.3f%s",
            opinion.describe(), score, f" detector={verdict}" if verdict else "",
        )
        if self._on_opinion is not None:
            self._on_opinion(opinion)
        if on_decision is not None:
            on_decision(opinion)
