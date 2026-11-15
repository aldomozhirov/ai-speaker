"""Scoring wake-word heads offline exactly as wake_word.py does live
(docs/plans/wake-word-custom-model.md, stages D-F).

A "head" is any ONNX model that reads 16 rows of openWakeWord's 96 speech
features and returns one score: the pretrained `hey_jarvis` and the
household models trained by wake-model-train.py alike. Features are always
computed the streaming way, 1280 samples at a time, because that is what the
live loop and the feature log use; openWakeWord's batch path
(`embed_clips`) gives different numbers (2026-11-15: up to 10 on a scale of
60 for the same audio).

Needs numpy, onnxruntime and openwakeword (the wake-word or wake-train venv).
"""

from __future__ import annotations

import os
import pickle

import numpy as np

import wake_data as wd

REFRACTORY_FRAMES = 25  # WAKE_WORD_REFRACTORY 2.0 s


def pretrained_path(name: str = "hey_jarvis") -> str:
    import openwakeword
    return next(p for p in openwakeword.get_pretrained_model_paths()
                if os.path.basename(p).startswith(name) and p.endswith(".onnx"))


class Head:
    """An ONNX head scoring (n, 16, 96) windows."""

    def __init__(self, path: str) -> None:
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self.path = path
        self.name = os.path.basename(path)[:-5]
        self._session = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        inp = self._session.get_inputs()[0]
        self._input = inp.name
        self._batch = inp.shape[0] if isinstance(inp.shape[0], int) else None  # None = any batch

    def scores(self, windows: np.ndarray, batch: int = 4096) -> np.ndarray:
        windows = np.asarray(windows, dtype=np.float32)
        if len(windows) == 0:
            return np.zeros(0, np.float32)
        if self._batch == 1:
            return np.array([float(self._session.run(None, {self._input: w[None]})[0].ravel()[0])
                             for w in windows], np.float32)
        out = [self._session.run(None, {self._input: windows[i:i + batch]})[0].ravel()
               for i in range(0, len(windows), batch)]
        return np.concatenate(out).astype(np.float32)


class Verifier:
    """The household verifier of 2026-10-05 (wake_word.Verifier), offline."""

    def __init__(self, path: str | None = None, threshold: float = 0.5) -> None:
        path = path or os.path.join(wd.VERIFIER_DIR, "hey_jarvis_v0.1.pkl")
        with open(path, "rb") as f:
            saved = pickle.load(f)
        self._pipeline = saved["pipeline"]
        self._window = int(saved["window"])
        self.threshold = threshold

    def confidence(self, windows: np.ndarray) -> np.ndarray:
        """Per window; the verifier reads the last `window` rows of each."""
        w = np.asarray(windows, np.float32)[:, -self._window:, :]
        return self._pipeline.predict_proba(w.reshape(len(w), -1))[:, 1]


def audio_features():
    """openWakeWord's feature extractor with one-thread onnxruntime sessions
    (by default they use every core)."""
    import onnxruntime as ort
    from openwakeword.utils import AudioFeatures
    af = AudioFeatures()
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    for attr in ("melspec_model", "embedding_model"):
        sess = getattr(af, attr, None)
        if sess is not None and hasattr(sess, "_model_path"):
            setattr(af, attr, ort.InferenceSession(sess._model_path, sess_options=opts,
                                                   providers=["CPUExecutionProvider"]))
    return af


def stream_rows(mono: np.ndarray, features=None) -> np.ndarray:
    """Feature rows of 16 kHz audio, one per 80 ms frame, as the live loop
    computes them. Pass the same `features` object to continue a stream."""
    features = features or audio_features()
    mono = np.asarray(mono, dtype=np.int16)
    usable = len(mono) // wd.FRAME * wd.FRAME
    rows = np.empty((usable // wd.FRAME, 96), np.float32)
    for k, i in enumerate(range(0, usable, wd.FRAME)):
        features(mono[i:i + wd.FRAME])
        rows[k] = features.get_features(1)[0, 0]
    return rows


def frame_windows(rows: np.ndarray) -> np.ndarray:
    """The 16-row window the heads see at every frame (zeros before the start,
    like openWakeWord's own buffer warm-up)."""
    padded = np.concatenate([np.zeros((wd.WINDOW - 1, 96), np.float32), rows[:, :96]])
    return wd.windows(padded)


def fires(scores: np.ndarray, threshold: float, patience: int = 1,
          verified: np.ndarray | None = None, refractory: int = REFRACTORY_FRAMES) -> list[int]:
    """Frames at which wake_word.DetectionGate would fire."""
    out = []
    run, armed, since = 0, True, 0
    rearm = threshold / 2
    patience = max(1, patience)
    for k, s in enumerate(scores):
        ok = s >= threshold and (verified is None or verified[k])
        run = run + 1 if ok else 0
        if not armed:
            since += 1
            if since >= refractory and s < rearm:
                armed = True
            continue
        if run >= patience:
            out.append(k)
            armed, since = False, 0
    return out
