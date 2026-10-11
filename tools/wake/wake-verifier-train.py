#!/usr/bin/env python3
"""Train the second-stage "is this really our Hey Jarvis" check for wake_word.py.

Why it exists: on 2026-10-05 a meeting playing in the room fired the wake word
about once a minute at scores of 0.10-0.50, the same range as the household's
real "Hey Jarvis" (0.12-0.81). No threshold separates the two. A small
logistic regression on openWakeWord's own embedding window does: it only has
to tell this voice saying the phrase from everything else that makes the base
model twitch.

Training data are the 2-channel captures wake_capture.py saves, sorted by hand:

    ~/.local/share/ai-speaker/wake-verifier/positive/   real "Hey Jarvis"
    ~/.local/share/ai-speaker/wake-verifier/negative/   false or near triggers

Copy new captures in (they are deleted from wake-captures beyond 200 MB) and
run this again; wake-word.service picks the model up on restart.

    nice ~/.venvs/wake-word/bin/python tools/wake/wake-verifier-train.py
    ... --report-only     cross-validate, write nothing
"""

from __future__ import annotations

import argparse
import glob
import os
import pickle
import time
import wave

import numpy as np
import openwakeword
from openwakeword.model import Model
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

HOME_DIR = os.path.expanduser("~/.local/share/ai-speaker/wake-verifier")
FRAME = 1280
PHASES = (0, 320, 640, 960)  # the live loop's frame boundary falls anywhere in the phrase
HIT_SCORE = 0.05  # base-model score from which a frame counts as "about the wake word"
BACKGROUND_SCORE = 0.01
BACKGROUND_ROWS = 1500
HARD_NEGATIVE_WEIGHT = 3.0
REGULARISATION_C = 0.001  # as openWakeWord's own verifier; 0.01 measured no better


def frames_of(path: str, model: Model, name: str, channel: int) -> list[tuple[int, float, np.ndarray]]:
    """(phase, base score, flattened embedding window) for every frame of a capture."""
    with wave.open(path) as wav:
        audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        audio = audio.reshape(-1, wav.getnchannels())
    mono = audio[:, min(channel, audio.shape[1] - 1)]
    window = model.model_inputs[name]
    rows = []
    for phase in PHASES:
        model.reset()
        samples = mono[phase:]
        for index, start in enumerate(range(0, len(samples) - FRAME, FRAME)):
            score = float(model.predict(samples[start:start + FRAME])[name])
            if index < window:
                continue
            features = model.preprocessor.get_features(window)[0]
            rows.append((phase, score, features.astype(np.float32).ravel()))
    return rows


def fit(positive: list, negative: list, background: list):
    hits = [f for rows in positive for _, s, f in rows if s >= HIT_SCORE]
    hard = [f for rows in negative for _, s, f in rows if s >= HIT_SCORE]
    quiet = [f for rows in background for _, s, f in rows if s < BACKGROUND_SCORE]
    rng = np.random.default_rng(0)
    quiet = [quiet[i] for i in rng.permutation(len(quiet))[:BACKGROUND_ROWS]]
    x = np.array(hits + hard + quiet)
    y = np.array([1] * len(hits) + [0] * (len(hard) + len(quiet)))
    weight = np.array([1.0] * len(hits) + [HARD_NEGATIVE_WEIGHT] * len(hard) + [1.0] * len(quiet))
    pipeline = make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=400, C=REGULARISATION_C, class_weight="balanced"),
    )
    pipeline.fit(x, y, logisticregression__sample_weight=weight)
    return pipeline, (len(hits), len(hard), len(quiet))


def fire_fraction(pipeline, rows: list, base_threshold: float, threshold: float) -> float:
    """Share of the frame phases in which this capture would still trigger."""
    fired = 0
    for phase in PHASES:
        over = [f for p, s, f in rows if p == phase and s >= base_threshold]
        if over and pipeline.predict_proba(np.array(over))[:, 1].max() >= threshold:
            fired += 1
    return fired / len(PHASES)


def report(positive: dict, negative: dict, base_threshold: float, folds: int = 6) -> None:
    thresholds = (0.3, 0.5, 0.6, 0.7, 0.9)
    pos, neg = sorted(positive), sorted(negative)
    kept = {t: [] for t in thresholds}
    passed = {t: [] for t in thresholds}
    for fold in range(folds):
        held = set(pos[fold::folds] + neg[fold::folds])
        train_pos = [positive[n] for n in pos if n not in held]
        train_neg = [negative[n] for n in neg if n not in held]
        pipeline, _ = fit(train_pos, train_neg, train_pos + train_neg)
        for t in thresholds:
            kept[t] += [fire_fraction(pipeline, positive[n], base_threshold, t) for n in pos if n in held]
            passed[t] += [fire_fraction(pipeline, negative[n], base_threshold, t) for n in neg if n in held]
    base_pos = np.mean([fire_fraction(_PassAll(), positive[n], base_threshold, 0.5) for n in pos])
    base_neg = np.mean([fire_fraction(_PassAll(), negative[n], base_threshold, 0.5) for n in neg])
    print(f"held-out check, {folds} folds, base threshold {base_threshold:.2f}")
    print(f"  no verifier     real kept {100 * base_pos:3.0f}%   false passed {100 * base_neg:3.0f}%")
    for t in thresholds:
        print(
            f"  verifier >= {t:.1f} real kept {100 * np.mean(kept[t]):3.0f}%"
            f"   false passed {100 * np.mean(passed[t]):3.0f}%"
        )


class _PassAll:
    def predict_proba(self, x):
        return np.ones((len(x), 2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", default=HOME_DIR)
    parser.add_argument("--channel", type=int, default=int(os.environ.get("WAKE_WORD_CHANNEL", "1")))
    parser.add_argument("--base-threshold", type=float, default=0.10)
    parser.add_argument("--model", default="hey_jarvis")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()

    path = [
        p for p in openwakeword.get_pretrained_model_paths()
        if args.model in os.path.basename(p) and p.endswith(".onnx")
    ][0]
    name = os.path.basename(path)[:-5]
    model = Model(wakeword_model_paths=[path])

    sets = {}
    for label in ("positive", "negative"):
        files = sorted(glob.glob(os.path.join(args.dir, label, "*.wav")))
        print(f"{label}: {len(files)} captures", flush=True)
        sets[label] = {os.path.basename(f): frames_of(f, model, name, args.channel) for f in files}
    if len(sets["positive"]) < 10 or len(sets["negative"]) < 10:
        print("need at least 10 captures of each kind")
        return 1

    report(sets["positive"], sets["negative"], args.base_threshold)
    if args.report_only:
        return 0

    everything = list(sets["positive"].values()) + list(sets["negative"].values())
    pipeline, counts = fit(list(sets["positive"].values()), list(sets["negative"].values()), everything)
    out = os.path.join(args.dir, f"{name}.pkl")
    with open(out + ".tmp", "wb") as handle:
        pickle.dump(
            {
                "pipeline": pipeline,
                "model": name,
                "window": int(model.model_inputs[name]),
                "channel": args.channel,
                "trained": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "captures": {k: len(v) for k, v in sets.items()},
                "rows": dict(zip(("positive", "hard_negative", "background"), counts)),
            },
            handle,
        )
    os.replace(out + ".tmp", out)
    print(f"wrote {out}  rows={counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
