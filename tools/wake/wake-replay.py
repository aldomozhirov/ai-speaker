#!/usr/bin/env python3
"""Replay audio through the production wake-word loop, offline.

Two jobs:

  wake-replay.py selftest
      Regression test for wake_word.py. Feeds synthetic "Hey Jarvis" clips
      (wake-test-clips/) through the same Listener / run_stream code the
      service runs and checks that one utterance produces exactly one
      detection -- with local intent off and on, at several thresholds --
      plus the capture recorder and the microphone watchdog. This is the test
      that would have caught the 2026-09 retrigger chain (one utterance, up to
      ten triggers).

  wake-replay.py score FILE_OR_DIR... [--thresholds 0.1,0.2,0.3] [--gain-db 0,12]
      Scores recordings: mono 16 kHz WAVs, or the 2-channel captures
      wake_capture.py saves under ~/.local/share/ai-speaker/wake-captures.
      Prints, per channel, the true peak score and whether the gate would
      fire at each threshold. This is the tool for choosing a threshold, a
      channel or an input gain from real utterances instead of from journal
      lines.

No audio hardware is touched and nothing is relayed: the trigger is a fake,
and local actions are stubbed out.

Run with the wake-word venv:
    ~/.venvs/wake-word/bin/python tools/wake/wake-replay.py selftest
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys
import tempfile
import time
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CLIPS = os.path.join(HERE, "wake-test-clips")
ww = None  # the wake_word module, loaded by load_module()


def load_module(module_dir: str):
    """Import wake_word.py from `module_dir` (its helpers from ~/.local/bin)."""
    global ww
    os.environ.setdefault("WAKE_WORD_DEBUG", "0")
    os.environ.setdefault("WAKE_WORD_CHANNEL", "1")
    sys.path.insert(0, os.path.expanduser("~/.local/bin"))
    sys.path.insert(0, os.path.abspath(module_dir))
    import wake_word

    ww = wake_word
    # Never run a real command or make a sound from a replay.
    calls: list = []

    async def fake_action(name, kwargs):
        calls.append((name, kwargs))
        return True, "stubbed"

    ww.local_actions.run_action = fake_action
    ww.sound_fx.play = lambda name: None
    ww.replay_action_calls = calls
    return ww


class FakeTrigger:
    def __init__(self, clock) -> None:
        self._clock = clock
        self.fires: list[tuple[float, float]] = []
        self.local: list[str] = []

    def fire(self, source: str, score: float | None = None) -> bool:
        self.fires.append((self._clock(), float(score or 0.0)))
        return True

    def mark_voice_activity(self) -> None:
        pass

    def log_local_action(self, action: str, ok: bool, detail: str) -> None:
        self.local.append(action)


def read_wav(path: str) -> np.ndarray:
    """int16 array shaped (samples, channels)."""
    with wave.open(path, "rb") as f:
        if f.getframerate() != 16000 or f.getsampwidth() != 2:
            raise ValueError(f"{path}: need 16 kHz 16-bit audio")
        data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
        return data.reshape(-1, f.getnchannels())


def noise(seconds: float, rng, rms: float = 30.0) -> np.ndarray:
    return (rng.standard_normal(int(seconds * 16000)) * rms).astype(np.int16)


def as_stream(mono: np.ndarray) -> np.ndarray:
    """A mono signal as the 2-channel stream wake_word.py expects."""
    return np.repeat(mono.reshape(-1, 1), 2, axis=1)


def clip_stream(parts: list, lead: float = 3.0, tail: float = 12.0, seed: int = 1) -> np.ndarray:
    """Concatenate clips (arrays) and gaps (floats, seconds) over a low noise floor."""
    rng = np.random.default_rng(seed)
    out = [noise(lead, rng)]
    for part in parts:
        if isinstance(part, (int, float)):
            out.append(noise(part, rng))
        else:
            mono = part[:, 0].astype(np.int32) + noise(len(part) / 16000, rng)
            out.append(np.clip(mono, -32768, 32767).astype(np.int16))
    out.append(noise(tail, rng))
    return as_stream(np.concatenate(out))


def replay(stream: np.ndarray, *, threshold: float, patience: int = 1, channel: int | None = None,
           local_intent: bool = False, recorder=None, watchdog=None, refractory: float | None = None):
    """Run one 2-channel stream through the production loop.

    Returns (fires, scores, trigger): fires as (seconds, score-at-firing),
    scores as the per-frame raw trajectory.
    """
    if channel is not None:
        ww.CHANNEL_INDEX = channel
    ww.local_intent.LOCAL_INTENT_ENABLED = local_intent
    model, name = ww.load_model()
    refractory = ww.REFRACTORY_SECONDS if refractory is None else refractory
    gate = ww.DetectionGate(threshold, patience, threshold / 2, round(refractory / ww.FRAME_SECONDS))
    listener = ww.Listener(model, name, gate, recorder, watchdog, threshold)

    scores: list[float] = []
    inner = listener.ingest

    def ingest(raw):
        result = inner(raw)
        scores.append(result[1])
        return result

    listener.ingest = ingest
    trigger = FakeTrigger(lambda: len(scores) * ww.FRAME_SECONDS)
    with tempfile.NamedTemporaryFile(suffix=".raw") as tmp:
        usable = len(stream) // ww.FRAME_SAMPLES * ww.FRAME_SAMPLES
        stream[:usable].astype(np.int16).tofile(tmp.name)
        with open(tmp.name, "rb", buffering=0) as f:
            ww.run_stream(f, listener, trigger)
    return trigger.fires, scores, trigger


# --- selftest ----------------------------------------------------------------


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        self.failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}{'  -- ' + detail if detail else ''}", flush=True)


def selftest() -> int:
    c = Checks()
    wake_clips = sorted(glob.glob(os.path.join(CLIPS, "hj_*.wav")))
    if not wake_clips:
        print(f"no clips under {CLIPS}")
        return 2

    # 1. The gate on its own.
    g = ww.DetectionGate(0.10, 1, 0.05, 25)
    fires = [g.update(s) for s in [0.0, 0.2, 0.9, 0.9, 0.3, 0.0] + [0.0] * 30 + [0.5]]
    c.check(fires.count(True) == 2 and fires[1] and fires[-1], "gate: one fire per rise, re-arms after refractory")
    g = ww.DetectionGate(0.10, 1, 0.05, 25)
    fires = [g.update(0.9) for _ in range(200)]
    c.check(fires.count(True) == 1, "gate: a score stuck high fires once, never again")
    g = ww.DetectionGate(0.10, 1, 0.05, 25)
    fires = [g.update(s) for s in [0.9] + [0.0] * 10 + [0.9]]
    c.check(fires.count(True) == 1, "gate: a second rise inside the refractory period is ignored")
    g = ww.DetectionGate(0.5, 3, 0.25, 25)
    fires = [g.update(s) for s in [0.6, 0.6, 0.1, 0.6, 0.6, 0.6]]
    c.check(fires == [False] * 5 + [True], "gate: patience needs consecutive frames")

    # 2. One utterance, one detection -- the regression that matters.
    for path in wake_clips:
        name = os.path.basename(path)
        clip = read_wav(path)
        stream = clip_stream([clip])
        for local in (False, True):
            for threshold in (0.10, 0.25, 0.5):
                fires, scores, _ = replay(stream, threshold=threshold, local_intent=local)
                expected = 1 if max(scores) >= threshold else 0
                c.check(
                    len(fires) == expected,
                    f"{name} thr={threshold:.2f} local_intent={'on ' if local else 'off'}: "
                    f"{len(fires)} detection(s), expected {expected}",
                    f"peak {max(scores):.3f}, {sum(s >= threshold for s in scores)} frames over",
                )

    # 3. Two utterances are two detections; a repeat inside the refractory is one.
    clip = read_wav(wake_clips[0])
    fires, _, _ = replay(clip_stream([clip, 4.0, clip]), threshold=0.10)
    c.check(len(fires) == 2, f"two utterances 4 s apart: {len(fires)} detections, expected 2")
    fires, _, _ = replay(clip_stream([clip, 0.3, clip]), threshold=0.10)
    c.check(len(fires) == 1, f"two utterances 0.3 s apart: {len(fires)} detection(s), expected 1")

    # 4. Local intent on, followed by an English command it knows: handled
    #    locally (stubbed), nothing relayed, and still exactly one detection.
    command = os.path.join(CLIPS, "en_cmd_timer.wav")
    if os.path.exists(command):
        ww.replay_action_calls.clear()
        fires, _, trigger = replay(clip_stream([clip, 0.4, read_wav(command)]), threshold=0.10, local_intent=True)
        c.check(
            len(fires) == 0 and len(ww.replay_action_calls) == 1,
            "wake word + English timer command with local intent on: handled locally, no relay",
            f"relays={len(fires)} actions={ww.replay_action_calls}",
        )

    # 5. Microphone watchdog and stall detection.
    class NoHealth:
        state = None

        def set(self, state, detail=""):
            self.state = state

    health = NoHealth()
    zeros = np.zeros((16000 * 15, 2), dtype=np.int16)
    try:
        replay(zeros, threshold=0.10, watchdog=ww.MicWatchdog(health))
        c.check(False, "watchdog: 15 s of digital silence raises CaptureProblem")
    except ww.CaptureProblem as exc:
        c.check(exc.state == "mic_silent", "watchdog: 15 s of digital silence raises CaptureProblem", str(exc))
    health = NoHealth()
    replay(clip_stream([clip], tail=2.0), threshold=0.10, watchdog=ww.MicWatchdog(health))
    c.check(health.state == "ok", "watchdog: a live stream reports ok")
    r, w = os.pipe()
    with os.fdopen(r, "rb", buffering=0) as stalled:
        t0 = time.monotonic()
        try:
            ww.read_exact(stalled, 16, timeout=0.3)
            c.check(False, "stall: a silent pipe raises CaptureProblem")
        except ww.CaptureProblem as exc:
            c.check(exc.state == "capture_stalled" and time.monotonic() - t0 < 1.0,
                    "stall: a pipe that delivers nothing raises CaptureProblem")
    os.close(w)

    # 6. Capture recorder.
    with tempfile.TemporaryDirectory() as tmp:
        def recorder(**kw):
            return ww.wake_capture.CaptureRecorder(
                sample_rate=16000, channels=2, frame_samples=ww.FRAME_SAMPLES, directory=tmp,
                meta={"threshold": 0.10}, **kw)

        rec = recorder()
        stream = clip_stream([clip], lead=6.0, tail=8.0)
        fires, scores, _ = replay(stream, threshold=0.10, recorder=rec)
        rec.flush()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not glob.glob(os.path.join(tmp, "*.json")):
            time.sleep(0.05)
        sidecars = glob.glob(os.path.join(tmp, "*.json"))
        ok = len(sidecars) == 1
        detail = f"{len(sidecars)} file(s)"
        if ok:
            meta = json.load(open(sidecars[0]))
            audio = read_wav(sidecars[0][:-5] + ".wav")
            first_hot = next(i for i, s in enumerate(scores) if s > 0.05)
            start = (first_hot - 38) * ww.FRAME_SAMPLES
            ok = (
                meta["kind"] == "wake"
                and len(meta["fired_frames"]) == 1
                and abs(meta["peak"] - max(scores)) < 1e-3
                and meta["notes"].get("relayed") is True
                and audio.shape[1] == 2
                and len(audio) == len(meta["scores"]) * ww.FRAME_SAMPLES
                and np.array_equal(audio, stream[start:start + len(audio)])
            )
            detail = (f"{os.path.basename(sidecars[0])}: {len(audio) / 16000:.1f} s, peak {meta['peak']}, "
                      f"pre-roll {meta['fired_frames'][0] * ww.FRAME_SECONDS:.2f} s before firing")
        c.check(ok, "recorder: a detection is saved with pre-roll, both channels and its score trajectory", detail)

        for f in glob.glob(os.path.join(tmp, "*")):
            os.unlink(f)
        rec = recorder()
        quiet = clip_stream([], lead=12.0, tail=0.0)
        frames = [quiet[i:i + ww.FRAME_SAMPLES].tobytes() for i in range(0, len(quiet) - ww.FRAME_SAMPLES + 1, ww.FRAME_SAMPLES)]
        for raw in frames[:140]:
            rec.push(raw, 0.0)
        rec.request_snapshot("button")
        for raw in frames[:70]:
            rec.push(raw, 0.0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not glob.glob(os.path.join(tmp, "*.json")):
            time.sleep(0.05)
        sidecars = glob.glob(os.path.join(tmp, "*-button-*.json"))
        seconds = len(json.load(open(sidecars[0]))["scores"]) * ww.FRAME_SECONDS if sidecars else 0
        c.check(len(sidecars) == 1 and 14.5 < seconds < 15.5,
                "recorder: a button press saves the 10 s before it and 5 s after", f"{seconds:.1f} s")

        rec = recorder(max_mb=1.2)
        for n in range(4):
            for raw in frames[:60]:
                rec.push(raw, 0.0)
            rec.push(frames[0], 0.5, fired=True)
            for raw in frames[:70]:
                rec.push(raw, 0.0)
            time.sleep(0.4)
        time.sleep(1.0)
        size = sum(os.path.getsize(f) for f in glob.glob(os.path.join(tmp, "*")))
        wavs, jsons = glob.glob(os.path.join(tmp, "*.wav")), glob.glob(os.path.join(tmp, "*.json"))
        c.check(size <= 1.2 * 1024 * 1024 and len(wavs) == len(jsons) and 0 < len(wavs) < 5,
                "recorder: oldest captures are pruned at the size cap, WAV and sidecar together",
                f"{len(wavs)} kept, {size / 1024:.0f} kB")

    print(f"\n{'ALL PASS' if not c.failed else str(c.failed) + ' FAILED'}")
    return 1 if c.failed else 0


# --- score -------------------------------------------------------------------


def score(paths: list[str], thresholds: list[float], gains: list[float], patience: int) -> int:
    files: list[str] = []
    for p in paths:
        p = os.path.expanduser(p)
        files += sorted(glob.glob(os.path.join(p, "*.wav"))) if os.path.isdir(p) else [p]
    head = " ".join(f"@{t:.2f}" for t in thresholds)
    print(f"{'file':44s} {'ch':>2s} {'gain':>5s} {'peak':>6s}  fires {head}")
    for path in files:
        audio = read_wav(path)
        if audio.shape[1] == 1:
            stream, channels = clip_stream([audio], tail=3.0), [ww.CHANNEL_INDEX]
        else:
            rng = np.random.default_rng(1)
            pad = as_stream(noise(1.0, rng))
            stream, channels = np.concatenate([pad, audio, pad]), [0, 1]
        for channel in channels:
            for gain in gains:
                boosted = np.clip(stream.astype(np.float64) * 10 ** (gain / 20), -32768, 32767).astype(np.int16)
                row, peak = [], 0.0
                for t in thresholds:
                    fires, scores, _ = replay(boosted, threshold=t, patience=patience, channel=channel)
                    peak = max(scores)
                    row.append(f"{len(fires):5d}")
                print(f"{os.path.basename(path)[:44]:44s} {channel:2d} {gain:+5.0f} {peak:6.3f}        {' '.join(row)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--module-dir", default=os.path.expanduser("~/.local/bin"),
                    help="directory holding the wake_word.py to test (default: the live one)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    sp = sub.add_parser("score")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--thresholds", default="0.10,0.20,0.30,0.50")
    sp.add_argument("--gain-db", default="0")
    sp.add_argument("--patience", type=int, default=1)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    load_module(args.module_dir)
    if args.cmd == "selftest":
        return selftest()
    return score(args.paths, [float(t) for t in args.thresholds.split(",")],
                 [float(g) for g in args.gain_db.split(",")], args.patience)


if __name__ == "__main__":
    sys.exit(main())
