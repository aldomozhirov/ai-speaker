#!/usr/bin/env python3
"""Re-record synthetic "Hey Jarvis" voices through the speaker's own
microphones (docs/plans/wake-word-custom-model.md, stage B4).

Synthetic voices (wake-model-train.py synth) never pass through the XVF3800,
its beamformer and the room. Played from a phone or laptop at a position in
the room and recorded here, they become takes like the enrolment ones: other
voices for training at one position, and a test at another.

    tools/wake/wake-rerecord.py make --set A        # writes a ~15 min WAV to play
    tools/wake/wake-rerecord.py record --set A --position kitchen --round 1
    tools/wake/wake-rerecord.py make --set B
    tools/wake/wake-rerecord.py record --set B --position corridor --round 2   # test only

`make` puts 200 clips four seconds apart into
wake-enroll/playback/set-<X>.wav, with a sweep tone at the start and every
minute. Copy it to a phone (scp), stand at the position, start `record`,
then press play: the sweeps tell this tool exactly when each clip played.
Sets A and B use different clips. The result is a block in
wake-enroll/playback-r<round>/ that wake-eval.py and wake-model-train.py
read like any enrolment block (person "playback").

voice-agent is stopped while recording (the speaker would answer every
clip) and started again at the end. System python3 with numpy.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import random
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wake_data as wd  # noqa: E402

spec = importlib.util.spec_from_file_location("wake_enroll", os.path.join(HERE, "wake-enroll.py"))
enroll = importlib.util.module_from_spec(spec)
sys.modules["wake_enroll"] = enroll
spec.loader.exec_module(enroll)

SYNTH = os.path.join(wd.TRAIN, "synth")
PLAYBACK = os.path.join(wd.ENROLL, "playback")
CLIPS = 200
SPACING = 4.0
SWEEP_EVERY = 60.0
SWEEP_SECONDS = 0.8
LEAD = 3.0


def sweep() -> np.ndarray:
    t = np.arange(int(SWEEP_SECONDS * wd.RATE)) / wd.RATE
    f0, f1 = 600.0, 3500.0
    phase = 2 * np.pi * (f0 * t + (f1 - f0) * t ** 2 / (2 * SWEEP_SECONDS))
    env = np.minimum(1, np.minimum(t, SWEEP_SECONDS - t) / 0.02)
    return (np.sin(phase) * env).astype(np.float32)


def cmd_make(args) -> int:
    meta_path = os.path.join(SYNTH, "clips.jsonl")
    if not os.path.exists(meta_path):
        print("No synthetic clips yet: wake-model-train.py synth", file=sys.stderr)
        return 1
    with open(meta_path) as f:
        clips = [json.loads(line) for line in f if '"kind": "pos"' in line]
    # Set A takes even clip numbers, set B odd: never the same clip in both.
    pool = [c for c in clips if int(os.path.basename(c["file"])[:5]) % 2 == (0 if args.set == "A" else 1)]
    chosen = random.Random(args.set).sample(pool, min(CLIPS, len(pool)))
    total = LEAD + len(chosen) * SPACING + 2
    out = np.zeros(int(total * wd.RATE), np.float32)
    sw = sweep() * 0.5
    sweeps, items = [], []
    t = 0.5
    while t < total - SWEEP_SECONDS:
        out[int(t * wd.RATE):int(t * wd.RATE) + len(sw)] += sw
        sweeps.append(round(t, 3))
        t += SWEEP_EVERY
    for i, c in enumerate(chosen):
        start = LEAD + i * SPACING
        if any(abs(start - s) < 1.5 or s - 1.5 < start + 2.5 < s + 1.5 for s in sweeps):
            start += 1.6  # keep clear of a sweep
        x = wd.read_wav(os.path.join(SYNTH, c["file"]))[:, 0].astype(np.float32)
        x *= 10 ** ((-20 - 20 * np.log10(np.sqrt((x ** 2).mean()) / 32767 + 1e-9)) / 20) / 32767
        a = int(start * wd.RATE)
        out[a:a + len(x)] += x[: len(out) - a]
        items.append({"offset": round(start, 3), "end": round(start + c["end"], 3), **c})
    os.makedirs(PLAYBACK, mode=0o700, exist_ok=True)
    path = os.path.join(PLAYBACK, f"set-{args.set}.wav")
    wd.write_wav(path, np.clip(out * 32767, -32767, 32767))
    with open(path[:-4] + ".json", "w") as f:
        json.dump({"set": args.set, "seconds": round(total, 2), "sweeps": sweeps, "clips": items}, f, indent=1)
    print(f"{path}: {len(items)} clips, {total / 60:.1f} min. Copy it to a phone, e.g.\n"
          f"  scp <speaker>:{path} .")
    return 0


def find_sweeps(mono: np.ndarray, planned: list[float]) -> list[tuple[float, float]]:
    """(planned time, recorded time) for each sweep found in the recording."""
    ref = sweep()
    x = mono.astype(np.float32)
    n = 1 << int(np.ceil(np.log2(len(x) + len(ref))))
    corr = np.fft.irfft(np.fft.rfft(x, n) * np.conj(np.fft.rfft(ref, n)), n)[: len(x)]
    env = np.abs(corr)
    first = int(np.argmax(env[: int((planned[0] + 30) * wd.RATE)]))  # the first sweep, within 30 s of start
    offset = first / wd.RATE - planned[0]
    pairs = []
    for p in planned:
        c = int((p + offset) * wd.RATE)
        a, z = max(0, c - wd.RATE // 2), min(len(env), c + wd.RATE // 2)
        if z <= a:
            continue
        k = a + int(np.argmax(env[a:z]))
        if env[k] > 5 * np.median(env[a:z]):
            pairs.append((p, k / wd.RATE))
    return pairs


def cmd_record(args) -> int:
    meta_path = os.path.join(PLAYBACK, f"set-{args.set}.json")
    if not os.path.exists(meta_path):
        print(f"Make the set first: wake-rerecord.py make --set {args.set}", file=sys.stderr)
        return 1
    with open(meta_path) as f:
        plan = json.load(f)
    positions = {p["name"]: p for p in enroll.load_positions()}
    if args.position not in positions:
        print(f"Unknown position; known: {', '.join(positions)}", file=sys.stderr)
        return 2
    pos = positions[args.position]
    out_dir = os.path.join(wd.ENROLL, f"playback-r{args.round}")
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    name = f"quiet-{pos['name']}-{args.set}"
    if not asyncio.run(enroll.wait_for_node(enroll.SOURCE, timeout=5)):
        print("Microphone node not there.", file=sys.stderr)
        return 1
    if enroll.session_active():
        print("A voice session is active; try again when it has ended.", file=sys.stderr)
        return 1
    subprocess.run(["systemctl", "--user", "stop", "voice-agent"], check=True)
    rec = None
    try:
        rec = enroll.Recorder(os.path.join(out_dir, name + ".wav"))
        if not rec.wait_for_audio():
            raise RuntimeError("no audio from the microphone")
        print(f"Recording. Press play on set-{args.set}.wav now at {pos['name']} ({pos['metres']:g} m). "
              f"It stops by itself after {plan['seconds'] / 60 + 0.5:.1f} min (Ctrl-C to abort).")
        deadline = plan["seconds"] + 30
        while rec.seconds() < deadline:
            print(f"\r  {rec.seconds() / 60:4.1f} min", end="", flush=True)
            time.sleep(1)
        print()
        rec.close(keep=True)
        audio = wd.read_wav(rec.path)
        rec_started = rec.started
        rec = None
    except KeyboardInterrupt:
        print("\nAborted; nothing kept.")
        return 130
    finally:
        if rec is not None:
            rec.close(keep=False)
        subprocess.run(["systemctl", "--user", "start", "voice-agent"])
        print("voice-agent started again.")

    pairs = find_sweeps(audio[:, 1], plan["sweeps"])
    if len(pairs) < 2:
        print(f"Found {len(pairs)} sweep(s); cannot align. Was it played loud enough?", file=sys.stderr)
        return 1
    p, r = np.array(pairs).T
    a, b = np.polyfit(p, r, 1)
    resid = float(np.abs(r - (a * p + b)).max())
    takes = [{"take": i + 1, "style": "normal", "cue_seconds": round(a * c["offset"] + b, 3),
              "end_seconds": round(a * c["end"] + b, 3), "cue_time": round(rec_started + a * c["offset"] + b, 3),
              "voice": c["voice"], "speaker": c["speaker"], "text": c["text"]}
             for i, c in enumerate(plan["clips"])]
    block = {"file": name + ".wav", "condition": "quiet", "position": pos["name"], "metres": pos["metres"],
             "where": pos["where"], "start_time": round(rec_started, 3),
             "start_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(rec_started)),
             "seconds": round(len(audio) / wd.RATE, 2), "takes": takes, "bed_seconds": [0.0, 0.0],
             "playback_set": args.set, "sweeps_found": len(pairs), "align_residual_s": round(resid, 4)}
    tj = os.path.join(out_dir, "takes.json")
    meta = json.load(open(tj)) if os.path.exists(tj) else {"person": "playback", "round": args.round, "blocks": {}}
    meta["blocks"][name] = block
    with open(tj, "w") as f:
        json.dump(meta, f, indent=1)
    print(f"{len(takes)} takes aligned on {len(pairs)} sweeps (largest error {1000 * resid:.0f} ms) -> {out_dir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--set", choices=("A", "B"), required=True)
    r = sub.add_parser("record")
    r.add_argument("--set", choices=("A", "B"), required=True)
    r.add_argument("--position", required=True)
    r.add_argument("--round", type=int, choices=(1, 2), required=True)
    args = ap.parse_args()
    return {"make": cmd_make, "record": cmd_record}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
