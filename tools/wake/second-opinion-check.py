#!/usr/bin/env python3
"""Replay saved captures through openWakeWord and wake_confirm.SecondOpinion,
frame by frame as the live loop does, and count what the second opinion would
have decided in each group of captures.

    OMP_NUM_THREADS=1 nice -n 19 tools/wake-bench/venv/bin/python \
        second-opinion-check.py [window_seconds delay_seconds]

A capture counts as passed when any of its triggers was passed. Groups are the
ones of tools/wake-bench/report.py.
"""

from __future__ import annotations

import os
import sys
import wave

import numpy as np
import openwakeword
from openwakeword.model import Model

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))
sys.path.insert(0, os.path.join(HERE, "..", "wake-bench"))
import bench  # noqa: E402
import report  # noqa: E402
import wake_confirm  # noqa: E402

FRAME = 1280
THRESHOLD = 0.10
CHANNEL = 1


def main() -> int:
    window = float(sys.argv[1]) if len(sys.argv) > 1 else wake_confirm.WINDOW_SECONDS
    delay = float(sys.argv[2]) if len(sys.argv) > 2 else wake_confirm.DELAY_SECONDS
    path = next(p for p in openwakeword.get_pretrained_model_paths() if "hey_jarvis" in p)
    model = Model(wakeword_model_paths=[path])
    key = next(iter(model.models))
    judge = wake_confirm.Judge()
    counts: dict[str, list[int]] = {}
    took = []
    for capture in bench.capture_files():
        group = report.group_of(capture)
        if group == "skip":
            continue
        with wave.open(capture) as wav:
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
            audio = audio.reshape(-1, wav.getnchannels())
        mono = np.ascontiguousarray(audio[:, CHANNEL])
        opinions: list[wake_confirm.Opinion] = []
        second = wake_confirm.SecondOpinion(
            judge, FRAME / 16000, THRESHOLD, opinions.append,
            window_seconds=window, delay_seconds=delay, threaded=False,
        )
        model.reset()
        for start in range(0, len(mono) - FRAME + 1, FRAME):
            frame = mono[start:start + FRAME]
            second.push(frame, float(model.predict(frame)[key]))
        took += [o.seconds for o in opinions]
        tally = counts.setdefault(group, [0, 0, 0])
        tally[0] += 1
        tally[1] += bool(opinions)
        tally[2] += any(o.passed for o in opinions)
    print(f"window={window}s delay={delay}s   judging took median "
          f"{np.median(took) * 1000:.0f} ms, max {max(took) * 1000:.0f} ms")
    for group in ("real", "false", "blocked", "below", "fired?"):
        total, triggered, passed = counts.get(group, [0, 0, 0])
        print(f"  {group:8s} captures={total:4d}  reached threshold={triggered:4d}  passed={passed:4d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
