#!/usr/bin/env python3
"""Train a "Hey Jarvis" model on this household's voices and this room's sound
(docs/plans/wake-word-custom-model.md, stage E).

The model is a small head on openWakeWord's speech features, the same kind of
model as the pretrained `hey_jarvis`, so wake_word.py runs it the same way and
at the same cost. Everything runs on the Pi and stays on it.

    PY="OMP_NUM_THREADS=1 nice -n 19 $HOME/.venvs/wake-train/bin/python"
    $PY tools/wake/wake-model-train.py check          # E2: can wake_word.py load a head next to the pretrained one?
    $PY tools/wake/wake-model-train.py synth          # E3: synthetic "Hey Jarvis" and near-phrases (Piper)
    $PY tools/wake/wake-model-train.py features       # E4: training windows from real + synthetic audio
    $PY tools/wake/wake-model-train.py train          # E6: train, pick the threshold, export ONNX
    then: tools/wake/wake-eval.py model <onnx>        # E7

Data it reads (all on the device): enrolment round 1 (never round 2), the
labelled captures up to --cutoff, the feature log, synthetic clips, and
openWakeWord's public features in wake-train/public/. Output:
wake-train/{synth,features}/ and wake-model/<name>.onnx with a .json beside
it (data counts, split, chosen threshold, validation figures).

Needs the wake-train venv (deps/requirements-wake-train.txt).
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import multiprocessing as mp
import os
import random
import sys
import time
import wave
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wake_data as wd  # noqa: E402
import wake_model as wm  # noqa: E402

SYNTH = os.path.join(wd.TRAIN, "synth")
FEATS = os.path.join(wd.TRAIN, "features")
VOICES = os.path.join(wd.TRAIN, "voices")
PUBLIC = os.path.join(wd.TRAIN, "public", "openwakeword_features_ACAV100M_2000_hrs_16bit.npy")
PUBLIC_VAL = os.path.join(wd.TRAIN, "public", "validation_set_features.npy")

# Windows whose audio ends this long after the phrase count as "the wake word".
END_FRAMES = 6  # 0 .. 0.4 s
POSITIVE_TEXTS = {
    "en": ["hey jarvis", "hey jarvis.", "Hey, Jarvis!", "hey jarvis?", "Hey Jarvis"],
    "ru": ["хей джарвис", "эй джарвис", "хэй джарвис", "хей, джарвис!"],
}
NEAR_TEXTS = {
    "en": ["hey travis", "hey davis", "hey marvin", "hey service", "hey charles", "jar of jam",
           "harvest", "nervous", "hey jars", "a jar is", "hey there", "hey javi", "carve us",
           "hey garvey", "service", "hey, how is it", "hey jess", "jarring", "hey, nervous"],
    "ru": ["эй, марвин", "давай", "жарко", "хватит", "чарльз", "харвест", "эй, жар", "джаз",
           "хей, давид", "привет", "эй, джес", "жарить", "хорошо", "харизма"],
}


# --- E2: check ---------------------------------------------------------------


def export_onnx(layers: list[dict], path: str, window: int = 16, features: int = 96) -> None:
    """Write the MLP head as a plain ONNX graph (MatMul, Add,
    LayerNormalization, Relu, Sigmoid), input [batch, 16, 96], output [batch, 1].
    `layers` holds numpy weights: {"w": (in, out), "b": (out,), "ln_g": .., "ln_b": ..}."""
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    inits, nodes = [], []
    inits.append(numpy_helper.from_array(np.array([-1, window * features], np.int64), "shape"))
    nodes.append(helper.make_node("Reshape", ["x", "shape"], ["h0"]))
    cur = "h0"
    for i, layer in enumerate(layers):
        w, b = f"w{i}", f"b{i}"
        inits += [numpy_helper.from_array(layer["w"].astype(np.float32), w),
                  numpy_helper.from_array(layer["b"].astype(np.float32), b)]
        nodes += [helper.make_node("MatMul", [cur, w], [f"m{i}"]), helper.make_node("Add", [f"m{i}", b], [f"a{i}"])]
        cur = f"a{i}"
        if "ln_g" in layer:
            g, bb = f"g{i}", f"lb{i}"
            inits += [numpy_helper.from_array(layer["ln_g"].astype(np.float32), g),
                      numpy_helper.from_array(layer["ln_b"].astype(np.float32), bb)]
            nodes += [helper.make_node("LayerNormalization", [cur, g, bb], [f"n{i}"], axis=-1, epsilon=1e-5),
                      helper.make_node("Relu", [f"n{i}"], [f"r{i}"])]
            cur = f"r{i}"
    nodes.append(helper.make_node("Sigmoid", [cur], ["y"]))
    graph = helper.make_graph(
        nodes, "hey_jarvis_home",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, ["batch", window, features])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, ["batch", 1])],
        inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], producer_name="ai-speaker")
    model.ir_version = 8
    onnx.checker.check_model(model)
    onnx.save(model, path)


def random_layers(rng, sizes=(1536, 64, 64, 64, 1)) -> list[dict]:
    layers = []
    for i in range(len(sizes) - 1):
        layer = {"w": rng.normal(0, 1 / math.sqrt(sizes[i]), (sizes[i], sizes[i + 1])), "b": np.zeros(sizes[i + 1])}
        if i < len(sizes) - 2:
            layer.update(ln_g=np.ones(sizes[i + 1]), ln_b=np.zeros(sizes[i + 1]))
        layers.append(layer)
    return layers


def cmd_check(args) -> int:
    """E2: a custom head runs next to the pretrained one in one openWakeWord
    Model (as wake_word.py's shadow will), and scores exactly what
    wake_model.Head scores offline."""
    from openwakeword.model import Model
    import tempfile
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "check_head.onnx")
    export_onnx(random_layers(np.random.default_rng(0)), path)
    pre = wm.pretrained_path()
    model = Model(wakeword_model_paths=[pre, path])
    names = list(model.models)
    clip = wd.read_wav(os.path.join(HERE, "wake-test-clips", "hj_alan.wav"))[:, 0]
    rng = np.random.default_rng(1)
    stream = np.concatenate([rng.normal(0, 30, 3 * wd.RATE), clip, rng.normal(0, 30, 2 * wd.RATE)]).astype(np.int16)
    stream = stream[: len(stream) // wd.FRAME * wd.FRAME]
    live = {n: [] for n in names}
    t = time.time()
    for i in range(0, len(stream), wd.FRAME):
        p = model.predict(stream[i:i + wd.FRAME])
        for n in names:
            live[n].append(p[n])
    per_frame = (time.time() - t) / (len(stream) // wd.FRAME) * 1000
    win = wm.frame_windows(wm.stream_rows(stream))
    offline = wm.Head(path).scores(win)
    custom = next(n for n in names if n.startswith("check_head"))
    # The first 16 frames differ only by how the window is padded at the start.
    diff = np.abs(np.array(live[custom])[20:] - offline[20:]).max()
    print(f"models in one openWakeWord Model: {names}")
    print(f"pretrained peak {max(live[names[0]]):.3f}; custom head scored live vs offline: max difference {diff:.6f}")
    print(f"{per_frame:.1f} ms per frame for both heads together")
    ok = len(names) == 2 and diff < 1e-3
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


# --- E3: synth -----------------------------------------------------------------


def _quiet_worker() -> None:
    """Every worker: lowest priority and one thread, whatever the libraries
    would pick by themselves."""
    os.environ["OMP_NUM_THREADS"] = "1"
    try:
        os.nice(19 - os.nice(0))
    except OSError:
        pass


def _voices() -> list[tuple[str, str, int]]:
    out = []
    for cfg in sorted(glob.glob(os.path.join(VOICES, "*.onnx.json"))):
        with open(cfg) as f:
            c = json.load(f)
        lang = "ru" if os.path.basename(cfg).startswith("ru_") else "en"
        out.append((cfg[:-5], lang, int(c.get("num_speakers") or 1)))
    return out


def _speech_end(x: np.ndarray) -> float:
    """Seconds to the end of speech in a clean synthetic clip."""
    hop = 160
    n = len(x) // hop
    rms = np.sqrt((x[: n * hop].astype(np.float32).reshape(n, hop) ** 2).mean(1))
    on = np.where(rms > rms.max() * 0.05)[0]
    return float((on[-1] + 1) * hop / wd.RATE) if len(on) else len(x) / wd.RATE


def _synth_worker(job) -> list[dict]:
    from piper import PiperVoice, SynthesisConfig
    from scipy.signal import resample_poly
    voice_path, lang, speakers, items, seed = job
    os.environ["OMP_NUM_THREADS"] = "1"
    import onnxruntime as ort
    voice = PiperVoice.load(voice_path)
    # Piper's own session uses every core and ignores OMP_NUM_THREADS: two
    # workers pushed the load to 9 next to the live audio (2026-11-15).
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    voice.session = ort.InferenceSession(voice_path, sess_options=opts, providers=["CPUExecutionProvider"])
    rng = random.Random(seed)
    rate = voice.config.sample_rate
    out = []
    for kind, idx, text in items:
        cfg = SynthesisConfig(speaker_id=rng.randrange(speakers) if speakers > 1 else None,
                              length_scale=rng.uniform(0.8, 1.3), noise_scale=rng.uniform(0.4, 0.9),
                              noise_w_scale=rng.uniform(0.5, 1.0))
        chunks = [np.frombuffer(c.audio_int16_bytes, np.int16) for c in voice.synthesize(text, syn_config=cfg)]
        if not chunks:
            continue
        x = np.concatenate(chunks).astype(np.float32)
        y = resample_poly(x, 16000, rate).clip(-32767, 32767).astype(np.int16)
        path = os.path.join(SYNTH, kind, f"{idx:05d}.wav")
        wd.write_wav(path, y)
        out.append({"file": os.path.relpath(path, SYNTH), "kind": kind, "text": text, "lang": lang,
                    "voice": os.path.basename(voice_path), "speaker": cfg.speaker_id, "end": _speech_end(y)})
    return out


def cmd_synth(args) -> int:
    voices = _voices()
    if not voices:
        print(f"No Piper voices in {VOICES}: python -m piper.download_voices --data-dir {VOICES} en_US-libritts_r-medium …")
        return 1
    for kind in ("pos", "near"):
        os.makedirs(os.path.join(SYNTH, kind), exist_ok=True)
    rng = random.Random(0)
    # Speakers weigh the choice of voice: libritts_r (904) gets most English clips.
    weights = [s if lang == "en" else max(s, 60) for _, lang, s in voices]
    jobs = {i: [] for i in range(len(voices))}
    for kind, count, texts in (("pos", args.positives, POSITIVE_TEXTS), ("near", args.near, NEAR_TEXTS)):
        for idx in range(count):
            v = rng.choices(range(len(voices)), weights)[0]
            jobs[v].append((kind, idx, rng.choice(texts[voices[v][1]])))
    work = []
    for v, items in jobs.items():
        for start in range(0, len(items), 100):
            work.append((voices[v][0], voices[v][1], voices[v][2], items[start:start + 100], v * 100000 + start))
    meta = []
    t = time.time()
    with mp.Pool(args.jobs, initializer=_quiet_worker) as pool:
        for n, res in enumerate(pool.imap_unordered(_synth_worker, work), 1):
            meta += res
            print(f"\r{len(meta)} clips, {time.time() - t:.0f} s", end="", flush=True)
    with open(os.path.join(SYNTH, "clips.jsonl"), "w") as f:
        for m in sorted(meta, key=lambda m: m["file"]):
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    print(f"\n{len(meta)} clips in {SYNTH}")
    return 0


# --- E4: features ------------------------------------------------------------


def synthetic_rir(rng: np.random.Generator) -> np.ndarray:
    """A room impulse response: direct sound plus exponentially decaying noise."""
    rt60 = rng.uniform(0.2, 0.8)
    n = int(rt60 * wd.RATE)
    t = np.arange(n) / wd.RATE
    tail = rng.normal(0, 1, n) * np.exp(-6.9 * t / rt60)
    tail[: int(0.003 * wd.RATE)] = 0
    rir = tail * rng.uniform(0.2, 0.6) / (np.abs(tail).max() + 1e-9)
    rir[0] = 1.0
    return rir / np.sqrt((rir ** 2).sum())


def rms_db(x: np.ndarray) -> float:
    return 20 * math.log10(float(np.sqrt((x.astype(np.float64) ** 2).mean())) + 1e-9)


def set_level(x: np.ndarray, db: float) -> np.ndarray:
    return x.astype(np.float32) * 10 ** ((db - rms_db(x)) / 20)


class Background:
    """Room sound to put under speech: enrolment beds and false captures
    (channel 1), else quiet noise."""

    def __init__(self, segments: list[np.ndarray], rng: np.random.Generator) -> None:
        self.segments = [s for s in segments if len(s) > wd.RATE]
        self.rng = rng

    def take(self, n: int) -> np.ndarray:
        if not self.segments:
            return self.rng.normal(0, 30, n).astype(np.float32)
        out = np.empty(0, np.float32)
        while len(out) < n:
            s = self.segments[self.rng.integers(len(self.segments))]
            a = self.rng.integers(0, max(1, len(s) - n))
            out = np.concatenate([out, s[a:a + n - len(out)].astype(np.float32)])
        return out


def _windows_at(rows: np.ndarray, ends_s: list[float]) -> list[np.ndarray]:
    """For each phrase end, the END_FRAMES windows whose audio ends 0-0.4 s after it."""
    win = wm.frame_windows(rows)
    out = []
    for end in ends_s:
        k0 = int(math.ceil(end / wd.FRAME_SECONDS)) - 1
        ks = [k for k in range(k0, k0 + END_FRAMES) if 0 <= k < len(win)]
        out.append(win[ks] if ks else np.empty((0, 16, 96), np.float32))
    return out


def _slot_stream(clips: list[tuple[np.ndarray, float]], bg: Background, rng, rir: bool, mix: bool):
    """Clips one after another over room sound, as one stream; returns
    (stream, phrase end times, clip spans)."""
    lead = bg.take(int(2.5 * wd.RATE)) * (rng.uniform(0.3, 1.0) if mix else 0.05)
    parts, ends, spans = [lead], [], []
    pos = len(lead)
    for x, end in clips:
        y = x.astype(np.float32)
        if rir:
            from scipy.signal import fftconvolve
            y = fftconvolve(y, synthetic_rir(rng))[: len(y) + int(0.3 * wd.RATE)]
        y = set_level(y, rng.uniform(-48, -28))
        gap = int(rng.uniform(0.6, 1.2) * wd.RATE)
        seg = np.concatenate([y, np.zeros(gap, np.float32)])
        under = bg.take(len(seg))
        if mix:
            snr = rng.uniform(3, 20)
            under = set_level(under, rms_db(y) - snr) if rms_db(under) > -90 else under
        else:
            under = under * 0.05
        parts.append(seg + under)
        ends.append((pos + end * wd.RATE) / wd.RATE)
        spans.append((pos / wd.RATE, (pos + len(seg)) / wd.RATE))
        pos += len(seg)
    stream = np.concatenate(parts).clip(-32767, 32767).astype(np.int16)
    return stream, ends, spans


def _synth_features_worker(job):
    os.environ["OMP_NUM_THREADS"] = "1"
    items, bg_segments, seed, kind = job
    rng = np.random.default_rng(seed)
    bg = Background(bg_segments, rng)
    clips = [(wd.read_wav(os.path.join(SYNTH, m["file"]))[:, 0], m["end"]) for m in items]
    pos, neg = [], []
    variants = ((False, False), (True, False), (False, True), (True, True)) if kind == "pos" else ((False, True), (True, True))
    for rir, mix in variants:
        stream, ends, spans = _slot_stream(clips, bg, rng, rir, mix)
        rows = wm.stream_rows(stream)
        if kind == "pos":
            pos += [w for w in _windows_at(rows, ends) if len(w)]
            # The first word alone ("hey") is not the wake word.
            cut = [(x[: int(len(x) * 0.45)], e * 0.45) for x, e in clips[: len(clips) // 4]]
            s2, _, _ = _slot_stream(cut, bg, rng, rir, mix)
            neg.append(wd.windows(wm.stream_rows(s2), step=2))
        else:
            neg.append(wd.windows(rows, step=2))
    return pos, neg


def real_sources(cutoff: float):
    """(id, mono channel 1, [phrase end seconds]) for enrolment round 1 and
    labelled real captures before the cut-off."""
    for b in wd.enrol_blocks((1,)):
        mono = wd.read_wav(b["path"])[:, 1]
        ends = [e for t in b["takes"]
                if (e := t.get("end_seconds") or wd.phrase_end(mono, t["cue_seconds"], t["cue_seconds"] + 4.5))
                is not None]
        yield f"enrol:{b['person']}:{b['block']}", mono, ends
    for stem, rec in sorted(wd.load_labels().items()):
        if rec["label"] != "real" or not wd.capture_path(stem):
            continue
        try:
            if wd.capture_time(stem) >= cutoff:
                continue
        except ValueError:
            continue
        mono = wd.read_wav(wd.capture_path(stem))[:, 1] if wd.read_wav(wd.capture_path(stem)).shape[1] > 1 \
            else wd.read_wav(wd.capture_path(stem))[:, 0]
        meta = wd.capture_meta(stem)
        if meta.get("peak", 0) >= 0.05 and "peak_seconds" in meta:
            ends = [float(meta["peak_seconds"]) + wd.FRAME_SECONDS - 0.08]
        else:
            e = wd.phrase_end(mono, 2.0, 6.0)
            ends = [e] if e else []
        if ends:
            yield f"capture:{stem}", mono, ends


def false_sources(cutoff: float):
    for stem, rec in sorted(wd.load_labels().items()):
        if rec["label"] != "false" or not wd.capture_path(stem):
            continue
        try:
            if wd.capture_time(stem) >= cutoff:
                continue
        except ValueError:
            continue
        audio = wd.read_wav(wd.capture_path(stem))
        yield stem, audio[:, min(1, audio.shape[1] - 1)]


def cmd_features(args) -> int:
    cutoff = time.mktime(time.strptime(args.cutoff, "%Y-%m-%d"))
    os.makedirs(FEATS, exist_ok=True)
    rng = np.random.default_rng(0)

    # Room sound for mixing: enrolment beds and false captures.
    bg_segments = []
    for b in wd.enrol_blocks((1,)):
        a, z = b["bed_seconds"]
        mono = wd.read_wav(b["path"])[:, 1]
        bg_segments.append(mono[int(a * wd.RATE):int(z * wd.RATE)])
    false_audio = list(false_sources(cutoff))
    bg_segments += [m for _, m in false_audio]
    print(f"background: {len(bg_segments)} segments, {sum(len(s) for s in bg_segments) / wd.RATE / 60:.1f} min")

    pos, pos_src, neg = [], [], []
    # Real wake-ups: as recorded, -6 dB, +6 dB, and over room sound.
    n_real = 0
    bg = Background(bg_segments, rng)
    for sid, mono, ends in real_sources(cutoff):
        for variant in ("as-is", "-6", "+6", "mix"):
            x = mono.astype(np.float32)
            if variant in ("-6", "+6"):
                x = x * 10 ** (int(variant) / 20)
            elif variant == "mix":
                x = x + set_level(bg.take(len(x)), rms_db(x) - rng.uniform(5, 15))
            rows = wm.stream_rows(x.clip(-32767, 32767).astype(np.int16))
            for w in _windows_at(rows, ends):
                if len(w):
                    pos.append(w)
                    pos_src += [sid] * len(w)
        n_real += len(ends)
        print(f"\rreal: {n_real} wake-ups", end="", flush=True)
    print()
    # False captures: every window is a negative.
    for stem, mono in false_audio:
        neg.append(wd.windows(wm.stream_rows(mono), step=2))
    for b in wd.enrol_blocks((1,)):
        a, z = b["bed_seconds"]
        mono = wd.read_wav(b["path"])[:, 1]
        neg.append(wd.windows(wm.stream_rows(mono[int(max(0, a - 2.0) * wd.RATE):int(z * wd.RATE)]), step=2))
    print(f"false captures: {len(false_audio)}; enrolment beds: {len(wd.enrol_blocks((1,)))}")

    # Synthetic.
    meta_path = os.path.join(SYNTH, "clips.jsonl")
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            clips = [json.loads(line) for line in f]
        jobs = []
        for kind in ("pos", "near"):
            items = [m for m in clips if m["kind"] == kind]
            for i in range(0, len(items), 50):
                jobs.append((items[i:i + 50], bg_segments, 1000 + len(jobs), kind))
        t = time.time()
        with mp.Pool(args.jobs, initializer=_quiet_worker) as pool:
            for n, (p, ng) in enumerate(pool.imap_unordered(_synth_features_worker, jobs), 1):
                for w in p:
                    pos.append(w)
                    pos_src += ["synth"] * len(w)
                neg += ng
                print(f"\rsynthetic: {n}/{len(jobs)} batches, {time.time() - t:.0f} s", end="", flush=True)
        print()
    else:
        print("no synthetic clips (run synth first)")

    pos_arr = np.concatenate(pos).astype(np.float16) if pos else np.empty((0, 16, 96), np.float16)
    neg_arr = np.concatenate([n for n in neg if len(n)]).astype(np.float16) if neg else np.empty((0, 16, 96), np.float16)
    np.save(os.path.join(FEATS, "positive.npy"), pos_arr)
    np.save(os.path.join(FEATS, "negative_hard.npy"), neg_arr)
    with open(os.path.join(FEATS, "positive_sources.json"), "w") as f:
        json.dump(pos_src, f)
    info = {"cutoff": args.cutoff, "made": dt.datetime.now().isoformat(timespec="seconds"),
            "positive_windows": len(pos_arr), "real_wakeups": n_real,
            "real_windows": sum(s != "synth" for s in pos_src), "negative_windows": len(neg_arr),
            "false_captures": len(false_audio)}
    with open(os.path.join(FEATS, "info.json"), "w") as f:
        json.dump(info, f, indent=1)
    print(json.dumps(info, indent=1))
    return 0


# --- E6: train -----------------------------------------------------------------


def room_windows(since: float, until: float, limit: int, rng, step: int = 3):
    """Training negatives from the feature log, outside known real spans."""
    spans = wd.known_real_spans(since, until)
    out, total = [], 0
    for path, t0 in wd.log_files(since, until):
        rows = wd.read_log(path)
        win = wd.windows(rows, step=step)
        if not len(win):
            continue
        t = t0 + (np.arange(len(win)) * step + wd.WINDOW) * wd.FRAME_SECONDS
        keep = (t >= since) & (t < until) & wd.outside(t, spans)
        idx = np.where(keep)[0]
        total += len(idx)
        out.append(np.ascontiguousarray(win[idx]).astype(np.float16))
    allw = np.concatenate(out) if out else np.empty((0, 16, 96), np.float16)
    if len(allw) > limit:
        allw = allw[np.sort(rng.choice(len(allw), limit, replace=False))]
    return allw


def room_sequences(since: float, until: float):
    """Per log file: (windows in time order, mask outside known real spans), for counting firings."""
    spans = wd.known_real_spans(since, until)
    for path, t0 in wd.log_files(since, until):
        rows = wd.read_log(path)
        win = wd.windows(rows)
        if not len(win):
            continue
        t = t0 + (np.arange(len(win)) + wd.WINDOW) * wd.FRAME_SECONDS
        yield win, (t >= since) & (t < until) & wd.outside(t, spans)


def public_subset(n: int, rng) -> np.ndarray:
    """A random sample of openWakeWord's public features (general speech,
    music, noise), read as contiguous chunks; works on a file that is still
    downloading."""
    if not os.path.exists(PUBLIC):
        return np.empty((0, 16, 96), np.float16)
    with open(PUBLIC, "rb") as f:
        version = np.lib.format.read_magic(f)
        shape, _, dtype = (np.lib.format.read_array_header_1_0(f) if version == (1, 0)
                           else np.lib.format.read_array_header_2_0(f))
        offset = f.tell()
    row = int(np.prod(shape[1:])) * np.dtype(dtype).itemsize
    total = min(shape[0], (os.path.getsize(PUBLIC) - offset) // row)
    if total <= 0:
        return np.empty((0, 16, 96), np.float16)
    arr = np.memmap(PUBLIC, dtype=dtype, mode="r", offset=offset, shape=(total, *shape[1:]))
    chunk = 2000
    count = min(max(1, n // chunk), max(1, total // chunk))
    starts = rng.choice(max(1, total - chunk), count, replace=False)
    return np.concatenate([np.asarray(arr[s:s + chunk]) for s in sorted(starts)])[:n]


def cmd_train(args) -> int:
    import torch
    torch.set_num_threads(1)
    rng = np.random.default_rng(args.seed)
    torch.manual_seed(args.seed)
    info = json.load(open(os.path.join(FEATS, "info.json")))
    pos = np.load(os.path.join(FEATS, "positive.npy"))
    pos_src = json.load(open(os.path.join(FEATS, "positive_sources.json")))
    hard = np.load(os.path.join(FEATS, "negative_hard.npy"))
    # The room log has its own end: by default everything logged so far.
    cutoff = time.mktime(time.strptime(args.log_until, "%Y-%m-%d %H:%M")) if args.log_until else time.time()

    logs = wd.log_files(None, cutoff)
    if not logs:
        print("The feature log is empty; it is needed for room negatives and validation.")
        return 1
    log_start = logs[0][1]
    val_start = time.mktime(time.strptime(args.val_since, "%Y-%m-%d %H:%M")) if args.val_since \
        else log_start + (cutoff - log_start) * 0.5
    print(f"room log: train {wd.stamp(log_start)} – {wd.stamp(val_start)}, "
          f"validation {wd.stamp(val_start)} – {wd.stamp(cutoff)}")
    room = room_windows(log_start, val_start, args.room_windows, rng)
    public = public_subset(args.public_windows, rng)
    print(f"positives {len(pos)} ({info['real_windows']} real), hard negatives {len(hard)}, "
          f"room {len(room)}, public {len(public)}", flush=True)

    # Held-out real wake-ups for choosing the checkpoint: one source in five.
    held = np.array([s != "synth" and zlib.crc32(s.encode()) % 5 == 0 for s in pos_src])
    if not held.any():  # no real data yet: hold out synthetic windows instead
        held = rng.random(len(pos)) < 0.05
    train_pos = pos[~held]
    held_pos = pos[held]
    held_groups = np.arange(held.sum()) // END_FRAMES  # windows of one wake-up are consecutive

    sizes = (16 * 96, 64, 64, 64, 1)
    blocks = []
    for i in range(3):
        blocks += [torch.nn.Linear(sizes[i], sizes[i + 1]), torch.nn.LayerNorm(sizes[i + 1]), torch.nn.ReLU()]
    net = torch.nn.Sequential(torch.nn.Flatten(), *blocks, torch.nn.Linear(64, 1))
    opt = torch.optim.Adam(net.parameters(), lr=args.lr, weight_decay=1e-5)
    loss_fn = torch.nn.BCEWithLogitsLoss()

    def batch(arr, n):
        if len(arr) == 0:
            return np.empty((0, 16, 96), np.float32)
        return arr[rng.integers(0, len(arr), n)].astype(np.float32)

    def predict(arr, bs=8192):
        net.eval()
        with torch.no_grad():
            out = [torch.sigmoid(net(torch.from_numpy(np.array(arr[i:i + bs], np.float32)))).numpy().ravel()
                   for i in range(0, len(arr), bs)]
        net.train()
        return np.concatenate(out) if out else np.zeros(0)

    val_seqs = list(room_sequences(val_start, cutoff))
    val_hours = sum(m.sum() for _, m in val_seqs) * wd.FRAME_SECONDS / 3600
    thresholds = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98]

    def evaluate():
        held_scores = predict(held_pos)
        val_scores = [(predict(w), m) for w, m in val_seqs]
        best = None
        for thr in thresholds:
            for pat in (1, 2, 3):
                fires = sum(len(wm.fires(np.where(m, s, 0.0), thr, pat)) for s, m in val_scores)
                per_week = fires / val_hours * 168 if val_hours else float("inf")
                hit = np.zeros(held_groups.max() + 1 if len(held_groups) else 0, bool)
                ok = held_scores >= thr
                for g in np.unique(held_groups):  # `pat` consecutive windows over the threshold
                    run = 0
                    for v in ok[held_groups == g]:
                        run = run + 1 if v else 0
                        if run >= pat:
                            hit[g] = True
                            break
                recall = hit.mean() if len(hit) else 0.0
                cand = {"threshold": thr, "patience": pat, "recall": float(recall),
                        "room_per_week": float(per_week), "room_fires": fires}
                if per_week <= args.max_per_week and (best is None or (recall, -thr) > (best["recall"], -best["threshold"])):
                    best = cand
        return best

    hard_pool = hard
    best_overall, best_state = None, None
    t0 = time.time()
    for step in range(1, args.steps + 1):
        neg = np.concatenate([batch(public, args.batch_neg // 2), batch(room, args.batch_neg // 4),
                              batch(hard_pool, args.batch_neg // 4)])
        p = batch(train_pos, args.batch_pos)
        x = torch.from_numpy(np.concatenate([p, neg]))
        y = torch.cat([torch.ones(len(p), 1), torch.zeros(len(neg), 1)])
        w = torch.cat([torch.full((len(p), 1), args.pos_weight), torch.ones(len(neg), 1)])
        loss = (torch.nn.functional.binary_cross_entropy_with_logits(net(x), y, reduction="none") * w).mean()
        opt.zero_grad()
        loss.backward()
        opt.step()
        if step % args.mine_every == 0:
            # Room and public windows the model now scores high become hard negatives.
            for src in (room, public):
                if len(src):
                    s = predict(src)
                    extra = src[s > 0.1]
                    if len(extra):
                        hard_pool = np.concatenate([hard_pool, extra[: args.mine_max]])
            if len(hard_pool) > len(hard) + args.hard_cap:
                hard_pool = np.concatenate([hard, hard_pool[-args.hard_cap:]])
        if step % args.eval_every == 0 or step == args.steps:
            res = evaluate()
            msg = (f"step {step}: loss {loss.item():.4f}, hard pool {len(hard_pool)}, "
                   f"{(time.time() - t0) / 60:.1f} min")
            if res:
                msg += (f" | best within {args.max_per_week:g}/week: thr {res['threshold']} pat {res['patience']} "
                        f"recall {100 * res['recall']:.0f} %, room {res['room_per_week']:.1f}/week")
                if best_overall is None or (res["recall"], -res["threshold"]) > (best_overall["recall"], -best_overall["threshold"]):
                    best_overall = {**res, "step": step}
                    best_state = {k: v.clone() for k, v in net.state_dict().items()}
            else:
                msg += " | no setting within the false-alarm limit yet"
            print(msg, flush=True)

    if best_state is None:
        print("No checkpoint met the false-alarm limit; nothing exported.")
        return 1
    net.load_state_dict(best_state)
    lin = [m for m in net if isinstance(m, torch.nn.Linear)]
    lns = [m for m in net if isinstance(m, torch.nn.LayerNorm)]
    layers = []
    for i, l in enumerate(lin):
        layer = {"w": l.weight.detach().numpy().T, "b": l.bias.detach().numpy()}
        if i < len(lns):
            layer.update(ln_g=lns[i].weight.detach().numpy(), ln_b=lns[i].bias.detach().numpy())
        layers.append(layer)
    os.makedirs(wd.MODELS, mode=0o700, exist_ok=True)
    path = os.path.join(wd.MODELS, args.name + ".onnx")
    export_onnx(layers, path)
    # The exported graph must score what the trained network scores.
    probe = held_pos[:256] if len(held_pos) else train_pos[:256]
    diff = float(np.abs(wm.Head(path).scores(probe) - predict(probe)).max())
    meta = {"name": args.name, "trained": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "features": info, "split": {"label_cutoff": info["cutoff"], "room_train": [wd.stamp(log_start), wd.stamp(val_start)],
                                        "room_validation": [wd.stamp(val_start), wd.stamp(cutoff)],
                                        "held_out": "real sources with crc32(id) % 5 == 0"},
            "counts": {"positives": int(len(train_pos)), "held_out": int(len(held_pos)), "room": int(len(room)),
                       "public": int(len(public)), "hard_start": int(len(hard)), "hard_end": int(len(hard_pool))},
            "chosen": best_overall, "validation_hours": float(val_hours), "export_max_diff": diff,
            "args": vars(args), "minutes": round((time.time() - t0) / 60, 1)}
    with open(path[:-5] + ".json", "w") as f:
        json.dump(meta, f, indent=1)
    print(f"\n{path}\n  chosen: threshold {best_overall['threshold']} patience {best_overall['patience']} "
          f"(step {best_overall['step']}): held-out recall {100 * best_overall['recall']:.0f} %, "
          f"{best_overall['room_per_week']:.1f} room firings a week over {val_hours:.1f} h\n"
          f"  exported graph vs network: max difference {diff:.2e}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    s = sub.add_parser("synth")
    s.add_argument("--positives", type=int, default=5000)
    s.add_argument("--near", type=int, default=1000)
    s.add_argument("--jobs", type=int, default=1, help="worker processes, one core each (live audio shares the Pi)")
    f = sub.add_parser("features")
    f.add_argument("--cutoff", default=dt.date.today().isoformat(),
                   help="labelled captures and feature log from this day on are left for testing")
    f.add_argument("--jobs", type=int, default=1, help="worker processes, one core each (live audio shares the Pi)")
    t = sub.add_parser("train")
    t.add_argument("--name", default="hey_jarvis_home_v1")
    t.add_argument("--steps", type=int, default=30000)
    t.add_argument("--batch-pos", type=int, default=256)
    t.add_argument("--batch-neg", type=int, default=2048)
    t.add_argument("--pos-weight", type=float, default=1.0)
    t.add_argument("--lr", type=float, default=1e-3)
    t.add_argument("--room-windows", type=int, default=300000)
    t.add_argument("--public-windows", type=int, default=300000)
    t.add_argument("--log-until", help='"YYYY-MM-DD HH:MM" end of the room log used (default: now); '
                   "keep the shadow week after it for testing")
    t.add_argument("--val-since", help='"YYYY-MM-DD HH:MM" start of the validation span (default: second half of the log)')
    t.add_argument("--max-per-week", type=float, default=1.0)
    t.add_argument("--mine-every", type=int, default=2000)
    t.add_argument("--mine-max", type=int, default=20000)
    t.add_argument("--hard-cap", type=int, default=200000)
    t.add_argument("--eval-every", type=int, default=2000)
    t.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    return {"check": cmd_check, "synth": cmd_synth, "features": cmd_features, "train": cmd_train}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
