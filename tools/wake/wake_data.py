"""Shared helpers for the wake-word tools of the custom-model plan
(docs/plans/wake-word-custom-model.md): where the household data lives, the
label file, enrolment takes, the feature log, captures and the journal.

Standard library plus numpy only, so the labelling and evaluation tools run
in any of the venvs (or system python with numpy).
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import time
import wave

import numpy as np

DATA = os.path.expanduser("~/.local/share/ai-speaker")
CAPTURES = os.path.join(DATA, "wake-captures")
KEPT = os.path.join(CAPTURES, "kept")
LABELS = os.path.join(DATA, "wake-labels.jsonl")
ENROLL = os.path.join(DATA, "wake-enroll")
FEATURES = os.path.join(DATA, "wake-features")
TRAIN = os.path.join(DATA, "wake-train")
MODELS = os.path.join(DATA, "wake-model")
VERIFIER_DIR = os.path.join(DATA, "wake-verifier")

RATE = 16000
FRAME = 1280
FRAME_SECONDS = FRAME / RATE
WINDOW = 16  # feature rows a head scores
LABEL_VALUES = ("real", "false", "unsure")


# --- labels ------------------------------------------------------------------


def load_labels() -> dict[str, dict]:
    """capture stem -> latest label record."""
    out: dict[str, dict] = {}
    try:
        with open(LABELS) as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    out[rec["capture"]] = rec
    except FileNotFoundError:
        pass
    return out


def set_label(stem: str, label: str, who: str = "", source: str = "page") -> dict:
    """Append a label (the latest line for a capture wins) and keep its audio."""
    if label not in LABEL_VALUES:
        raise ValueError(label)
    rec = {"capture": stem, "label": label, "who": who, "source": source,
           "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    os.makedirs(os.path.dirname(LABELS), exist_ok=True)
    with open(LABELS, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.chmod(LABELS, 0o600)
    keep(stem)
    return rec


def capture_path(stem: str) -> str | None:
    """The WAV of a capture, in the rotating folder or in kept/."""
    for d in (CAPTURES, KEPT):
        p = os.path.join(d, stem + ".wav")
        if os.path.exists(p):
            return p
    return None


def keep(stem: str) -> None:
    """Copy a capture (WAV and JSON) to kept/, which rotation never touches."""
    os.makedirs(KEPT, mode=0o700, exist_ok=True)
    for ext in (".wav", ".json"):
        src = os.path.join(CAPTURES, stem + ext)
        dst = os.path.join(KEPT, stem + ext)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)


def capture_meta(stem: str) -> dict:
    for d in (CAPTURES, KEPT):
        p = os.path.join(d, stem + ".json")
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    return {}


def capture_time(stem: str) -> float:
    """Start of a capture from its name (YYYYMMDD-HHMMSS-...)."""
    return time.mktime(time.strptime(stem[:15], "%Y%m%d-%H%M%S"))


# --- audio -------------------------------------------------------------------


def read_wav(path: str) -> np.ndarray:
    """(samples, channels) int16; 16 kHz is checked."""
    with wave.open(path) as w:
        if w.getframerate() != RATE or w.getsampwidth() != 2:
            raise ValueError(f"{path}: need 16 kHz 16-bit")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        return data.reshape(-1, w.getnchannels())


def write_wav(path: str, mono: np.ndarray) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.asarray(mono, dtype=np.int16).tobytes())


def phrase_end(mono: np.ndarray, start_s: float, end_s: float) -> float | None:
    """End of the loudest burst of speech between start_s and end_s, in
    seconds, from 20 ms energy; None if nothing stands out of the floor."""
    a = int(start_s * RATE)
    b = min(len(mono), int(end_s * RATE))
    seg = mono[a:b].astype(np.float32)
    hop = 320
    n = len(seg) // hop
    if n < 10:
        return None
    rms = np.sqrt((seg[: n * hop].reshape(n, hop) ** 2).mean(1) + 1e-6)
    db = 20 * np.log10(rms)
    floor = np.percentile(db, 20)
    peak = db.max()
    if peak - floor < 6:
        return None
    on = db > floor + 0.5 * (peak - floor)
    top = int(db.argmax())
    end = top
    gap = 0
    for i in range(top, n):  # allow short gaps between "hey" and "jarvis"
        if on[i]:
            end, gap = i, 0
        else:
            gap += 1
            if gap > 12:
                break
    return start_s + (end + 1) * hop / RATE


# --- enrolment ----------------------------------------------------------------


def enrol_blocks(rounds: tuple[int, ...] = (1, 2)) -> list[dict]:
    """Every recorded block, with person, round and the WAV's full path."""
    out = []
    for meta_path in sorted(glob.glob(os.path.join(ENROLL, "*-r*", "takes.json"))):
        with open(meta_path) as f:
            meta = json.load(f)
        if meta.get("round") not in rounds:
            continue
        folder = os.path.dirname(meta_path)
        for name, block in sorted(meta["blocks"].items()):
            out.append({**block, "person": meta["person"], "round": meta["round"],
                        "path": os.path.join(folder, block["file"]), "block": name})
    return out


# --- feature log --------------------------------------------------------------


def log_files(since: float | None = None, until: float | None = None) -> list[tuple[str, float]]:
    """(path, wall-clock time of row 0) of the feature-log files overlapping the span."""
    out = []
    for p in sorted(glob.glob(os.path.join(FEATURES, "*.f16"))):
        stem = os.path.basename(p)[:15]
        t0 = time.mktime(time.strptime(stem, "%Y%m%d-%H%M%S"))
        rows = os.path.getsize(p) // (97 * 2)
        t1 = t0 + rows * FRAME_SECONDS
        if (since is None or t1 >= since) and (until is None or t0 < until):
            out.append((p, t0))
    return out


def read_log(path: str) -> np.ndarray:
    data = np.fromfile(path, dtype="<f2")
    usable = len(data) // 97 * 97
    return data[:usable].reshape(-1, 97).astype(np.float32)


def windows(rows: np.ndarray, step: int = 1) -> np.ndarray:
    """All 16-row windows of a (n, 96+) row array, as (m, 16, 96) views."""
    feats = np.ascontiguousarray(rows[:, :96])
    if len(feats) < WINDOW:
        return np.empty((0, WINDOW, 96), np.float32)
    v = np.lib.stride_tricks.sliding_window_view(feats, (WINDOW, 96))[:, 0]
    return v[::step]


# --- journal -----------------------------------------------------------------


def journal(since: str, until: str | None = None, units=("wake-word", "voice-agent"),
            grep: str | None = None) -> list[tuple[float, str, str]]:
    """(time, unit, message) for the user units, oldest first; `grep` is
    journalctl's own pattern filter (much faster than reading everything)."""
    cmd = ["journalctl", "--user", "-o", "json", "--no-pager", "-q", "--since", since]
    if grep:
        cmd += ["--grep", grep]
    for u in units:
        cmd += ["-u", u]
    if until:
        cmd += ["--until", until]
    out = []
    proc = subprocess.run(cmd, capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = rec.get("MESSAGE")
        if isinstance(msg, list):  # journald hands non-UTF-8 lines over as bytes
            msg = bytes(msg).decode("utf-8", "replace")
        if not isinstance(msg, str):
            continue
        unit = (rec.get("_SYSTEMD_USER_UNIT") or rec.get("USER_UNIT") or "").replace(".service", "")
        out.append((int(rec["__REALTIME_TIMESTAMP"]) / 1e6, unit, msg))
    return out


def known_real_spans(since: float, until: float) -> list[tuple[float, float]]:
    """Spans in which someone was (or may have been) talking to the speaker:
    labelled real captures, enrolment recordings, voice sessions and tool
    calls. Firings inside them are not false alarms, and their features are
    not used as negatives."""
    spans = []
    for stem, rec in load_labels().items():
        if rec["label"] != "real":
            continue
        try:
            t = capture_time(stem) + float(capture_meta(stem).get("peak_seconds", 3.0))
        except ValueError:
            continue
        spans.append((t - 5, t + 5))
    for b in enrol_blocks():
        spans.append((b["start_time"] - 5, b["start_time"] + b["seconds"] + 5))
    for t, _, _ in journal(stamp(since - 60), stamp(until + 60), units=("voice-agent",),
                           grep="Starting session|Tool call:|Button pressed"):
        spans.append((t - 6, t + 20))
    return sorted(spans)


def outside(times: np.ndarray, spans: list[tuple[float, float]]) -> np.ndarray:
    """Mask of `times` that fall in none of the spans."""
    keep = np.ones(len(times), bool)
    for a, z in spans:
        keep &= ~((times >= a) & (times <= z))
    return keep


def stamp(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
