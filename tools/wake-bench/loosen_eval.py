#!/usr/bin/env python3
"""Throwaway: what if the first stage were looser (threshold 0.05, no verifier)
and the second opinion looked at both channels? For every capture, judge the
3 s ending 0.24 s after (a) the first frame at or over each threshold and
(b) the peak frame, per channel; cache to loosen.json."""
import glob, json, os, sys, wave
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import openwakeword
from openwakeword.model import Model
import wake_confirm

DATA = os.path.expanduser("~/.local/share/ai-speaker")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "loosen.json")
FRAME = 1280
files = []
for pattern in ("wake-verifier/positive", "wake-verifier/negative", "wake-captures"):
    files += sorted(glob.glob(os.path.join(DATA, pattern, "*.wav")))
files += sorted(glob.glob(os.path.join(HERE, "kept-fired", "*.wav")))
results = json.load(open(OUT)) if os.path.exists(OUT) else {}
model = Model(wakeword_model_paths=[next(p for p in openwakeword.get_pretrained_model_paths() if "hey_jarvis" in p)])
key = next(iter(model.models))
judge = wake_confirm.Judge()
for n, path in enumerate(files):
    name = os.path.basename(path)
    if name in results and results[name].get("dir") == os.path.basename(os.path.dirname(path)):
        continue
    with wave.open(path) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, w.getnchannels())
    model.reset()
    scores = [float(model.predict(np.ascontiguousarray(a[s:s + FRAME, 1]))[key]) for s in range(0, len(a) - FRAME + 1, FRAME)]
    entry = {"dir": os.path.basename(os.path.dirname(path)), "peak": max(scores), "at": {}}
    points = {"peak": int(np.argmax(scores))}
    for t in (0.05, 0.10):
        over = [i for i, s in enumerate(scores) if s >= t]
        if over:
            points[str(t)] = over[0]
    for label, index in points.items():
        end = min(len(a), (index + 1 + 3) * FRAME)
        window = a[max(0, end - 48000):end]
        entry["at"][label] = [[round(o.vosk, 3), round(o.micro, 3)] for o in
                              (judge.judge(np.ascontiguousarray(window[:, c])) for c in (0, 1))]
    results[name] = entry
    if n % 25 == 0:
        json.dump(results, open(OUT, "w"))
json.dump(results, open(OUT, "w"))
print(len(results), "captures")
