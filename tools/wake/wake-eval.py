#!/usr/bin/env python3
"""Measure the wake word: what today's pipeline does with the enrolment takes
(stage D), and how a household model compares (stage E7).

    # What the live pipeline did with every take, from the journal, plus the
    # same takes scored offline (pretrained, verifier, second opinion; channel
    # 0 against channel 1)
    ~/.venvs/wake-word/bin/python tools/wake/wake-eval.py baseline

    # A trained head against today's pipeline: recall on round-2 takes and on
    # labelled real captures, false alarms in the room's feature log and in
    # openWakeWord's public 11-hour validation set; a threshold x patience grid
    ~/.venvs/wake-word/bin/python tools/wake/wake-eval.py model ~/.local/share/ai-speaker/wake-model/hey_jarvis_home_v1.onnx

Results go to ~/.local/state/ai-speaker/measurements/wake-eval-<date>-<what>.md
(and .json). Takes and captures are household audio and stay on the device.
Run niced: OMP_NUM_THREADS=1 nice -n 19 ...
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))
import wake_data as wd  # noqa: E402
import wake_model as wm  # noqa: E402

OUT = os.path.expanduser("~/.local/state/ai-speaker/measurements")
TAKE_SECONDS = 4.5  # after the cue, a take's phrase must have been heard
LIVE_THRESHOLD = 0.10
RX_EVENT = re.compile(r"wake event: peak=([\d.]+) frames_over_threshold=\d+ fired=(yes|no)(?: verifier=([\d.]+))?")
RX_OPINION = re.compile(r"second opinion: vosk=([\d.]+) micro=([\d.]+) -> (pass|block)")


def write(name: str, lines: list[str], data: dict) -> str:
    os.makedirs(OUT, exist_ok=True)
    base = os.path.join(OUT, f"wake-eval-{dt.date.today().isoformat()}-{name}")
    with open(base + ".md", "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(base + ".json", "w") as f:
        json.dump(data, f, indent=1, default=float)
    return base + ".md"


def cell_table(rows: list[dict], keys: list[tuple[str, str]], by=("condition", "position")) -> list[str]:
    """Markdown table: one row per (condition, position), share of takes per key."""
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r[b] for b in by)].append(r)
    head = "| " + " | ".join(by) + " | takes | " + " | ".join(k[1] for k in keys) + " |"
    out = [head, "|" + "---|" * (len(by) + 1 + len(keys))]
    order = {"quiet": 0, "music": 1, "talk": 2}
    for g in sorted(groups, key=lambda g: (order.get(g[0], 9), g[1:])):
        rs = groups[g]
        cells = [f"{100 * sum(bool(r[k]) for r in rs) / len(rs):.0f} %" for k, _ in keys]
        out.append("| " + " | ".join(map(str, g)) + f" | {len(rs)} | " + " | ".join(cells) + " |")
    total = [f"{100 * sum(bool(r[k]) for r in rows) / len(rows):.0f} %" for k, _ in keys] if rows else []
    out.append("| **all** |" + " |" * (len(by) - 1) + f" {len(rows)} | " + " | ".join(total) + " |")
    return out


# --- baseline (stage D) -------------------------------------------------------


def journal_verdicts(blocks: list[dict]) -> dict:
    """(block, take) -> what the live pipeline journalled for it."""
    if not blocks:
        return {}
    since = wd.stamp(min(b["start_time"] for b in blocks) - 5)
    until = wd.stamp(max(b["start_time"] + b["seconds"] for b in blocks) + 10)
    lines = [(t, m) for t, _, m in wd.journal(since, until, units=("wake-word",))
             if "wake event:" in m or "second opinion:" in m]
    out = {}
    for b in blocks:
        for take in b["takes"]:
            a, z = take["cue_time"] - 0.5, take["cue_time"] + TAKE_SECONDS + 1.5
            v = {"peak": None, "fired": False, "verifier": None, "opinion": None}
            for t, m in lines:
                if not a <= t <= z:
                    continue
                if mm := RX_EVENT.search(m):
                    v["peak"] = max(v["peak"] or 0.0, float(mm[1]))
                    v["fired"] = v["fired"] or mm[2] == "yes"
                    if mm[3]:
                        v["verifier"] = max(v["verifier"] or 0.0, float(mm[3]))
                elif (mm := RX_OPINION.search(m)) and "detector=fired" in m:
                    v["opinion"] = "pass" if mm[3] == "pass" or v["opinion"] == "pass" else "block"
            out[(b["block"], b["person"], b["round"], take["take"])] = v
    return out


def score_blocks(blocks: list[dict], heads: list, verifier=None, judge=None, channels=(1,)) -> list[dict]:
    """Every take of every block, scored offline the way the live loop works."""
    rows_out = []
    for b in blocks:
        audio = wd.read_wav(b["path"])
        per_channel = {}
        for ch in channels:
            mono = audio[:, min(ch, audio.shape[1] - 1)]
            win = wm.frame_windows(wm.stream_rows(mono))
            per_channel[ch] = {"windows": win, "mono": mono,
                               "scores": {h.name: h.scores(win) for h in heads}}
        for take in b["takes"]:
            k0 = int(take["cue_seconds"] / wd.FRAME_SECONDS)
            k1 = int((take["cue_seconds"] + TAKE_SECONDS) / wd.FRAME_SECONDS)
            r = {"person": b["person"], "round": b["round"], "condition": b["condition"],
                 "position": b["position"], "metres": b["metres"], "block": b["block"],
                 "take": take["take"], "style": take["style"]}
            for ch, pc in per_channel.items():
                for name, s in pc["scores"].items():
                    r[f"peak_{name}_ch{ch}"] = float(s[k0:k1].max()) if k1 > k0 else 0.0
            if verifier is not None and 1 in per_channel:
                pc = per_channel[1]
                s = pc["scores"][heads[0].name]
                over = [k for k in range(k0, min(k1, len(s))) if s[k] >= LIVE_THRESHOLD]
                conf = verifier.confidence(pc["windows"][over]) if over else np.zeros(0)
                r["verifier"] = float(conf.max()) if len(conf) else None
                r["verifier_ok"] = bool(len(conf) and conf.max() >= verifier.threshold)
                if judge is not None and r["verifier_ok"]:
                    k = over[int(np.argmax(conf >= verifier.threshold))]
                    end = int((k + 1) * wd.FRAME + 0.16 * wd.RATE)
                    pcm = pc["mono"][max(0, end - 3 * wd.RATE):end]
                    r["second_opinion"] = bool(judge.judge(np.ascontiguousarray(pcm)).passed)
            rows_out.append(r)
    return rows_out


def cmd_baseline(args) -> int:
    blocks = wd.enrol_blocks(tuple(args.rounds))
    if not blocks:
        print(f"No enrolment recordings in {wd.ENROLL} yet (tools/wake/wake-enroll.py record).")
        return 1
    print(f"{len(blocks)} blocks, {sum(len(b['takes']) for b in blocks)} takes")
    live = journal_verdicts(blocks)
    pre = wm.Head(wm.pretrained_path())
    verifier = wm.Verifier()
    judge = None
    if not args.no_second_opinion:
        import wake_confirm
        judge = wake_confirm.Judge()
    rows = score_blocks(blocks, [pre], verifier, judge, channels=(0, 1))
    pn = pre.name
    for r in rows:
        v = live.get((r["block"], r["person"], r["round"], r["take"]), {})
        r["live_logged"] = v.get("peak") is not None
        r["live_detector"] = (v.get("peak") or 0) >= LIVE_THRESHOLD
        r["live_verifier"] = bool(v.get("fired"))
        r["live_open"] = v.get("opinion") == "pass"
        r["off_detector"] = r[f"peak_{pn}_ch1"] >= LIVE_THRESHOLD
        r["off_detector_ch0"] = r[f"peak_{pn}_ch0"] >= LIVE_THRESHOLD
        r["off_open"] = bool(r.get("verifier_ok") and r.get("second_opinion", True))
    lines = [f"# Wake-word baseline {dt.date.today()}", "",
             f"{len(blocks)} blocks, {len(rows)} takes (rounds {', '.join(map(str, args.rounds))}). "
             "Share of takes reaching each stage.", "",
             "## Live pipeline (journal)", "",
             "*logged*: score > 0.05 at all; *detector*: ≥ 0.10; *verifier*: passed; *opens*: second opinion passed too.", ""]
    lines += cell_table(rows, [("live_logged", "logged"), ("live_detector", "detector"),
                               ("live_verifier", "verifier"), ("live_open", "opens")])
    lines += ["", "## Offline, the same audio", "",
              "Pretrained model ≥ 0.10 on channel 1 and on channel 0, verifier, second opinion.", ""]
    lines += cell_table(rows, [("off_detector", "detector ch1"), ("off_detector_ch0", "detector ch0"),
                               ("verifier_ok", "verifier"), ("off_open", "opens")])
    lines += ["", "## By person", ""]
    lines += cell_table(rows, [("live_open", "opens live"), ("off_open", "opens offline")], by=("person", "condition"))
    lines += ["", "## By style (takes 1-8 normal, 9-10 quiet, 11-12 fast)", ""]
    lines += cell_table(rows, [("live_open", "opens live"), ("off_open", "opens offline")], by=("style", "condition"))
    print("\n".join(lines))
    print("\n->", write("baseline", lines, {"takes": rows}))
    return 0


# --- model (stage E7) ---------------------------------------------------------


def log_false_alarms(heads: list, since: float, until: float, thresholds, patiences, verifier=None):
    """Fires of each head over the feature log, outside known real spans."""
    spans = wd.known_real_spans(since, until)
    hours = 0.0
    counts = defaultdict(int)
    times = defaultdict(list)
    for path, t0 in wd.log_files(since, until):
        rows = wd.read_log(path)
        if len(rows) < wd.WINDOW:
            continue
        win = wd.windows(rows)
        frame_t = t0 + (np.arange(len(win)) + wd.WINDOW) * wd.FRAME_SECONDS
        keep = (frame_t >= since) & (frame_t < until) & wd.outside(frame_t, spans)
        hours += keep.sum() * wd.FRAME_SECONDS / 3600
        for h in heads:
            s = h["head"].scores(win) if h["kind"] == "head" else None
            if h["kind"] == "head":
                for thr in thresholds:
                    for pat in patiences:
                        for k in wm.fires(np.where(keep, s, 0.0), thr, pat):
                            counts[(h["name"], thr, pat)] += 1
                            times[(h["name"], thr, pat)].append(float(frame_t[k]))
            else:  # today's pipeline: pretrained >= 0.10 and the verifier
                base = h["head"].scores(win)
                ver = np.zeros(len(base), bool)
                over = np.where((base >= LIVE_THRESHOLD) & keep)[0]
                if len(over):
                    ver[over] = verifier.confidence(win[over]) >= verifier.threshold
                counts[(h["name"], "detector", 1)] += len(wm.fires(np.where(keep, base, 0.0), LIVE_THRESHOLD))
                counts[(h["name"], "verifier", 1)] += len(wm.fires(np.where(keep, base, 0.0), LIVE_THRESHOLD, 1, ver))
    return hours, counts, times


def public_false_alarms(head, thresholds, patiences) -> tuple[float, dict]:
    path = os.path.join(wd.TRAIN, "public", "validation_set_features.npy")
    if not os.path.exists(path):
        return 0.0, {}
    rows = np.load(path, mmap_mode="r")
    s = head.scores(wd.windows(np.asarray(rows)))
    hours = len(rows) * wd.FRAME_SECONDS / 3600
    return hours, {(thr, pat): len(wm.fires(s, thr, pat)) for thr in thresholds for pat in patiences}


def capture_recall(head, pre, verifier, since: float | None) -> list[dict]:
    out = []
    for stem, rec in sorted(wd.load_labels().items()):
        if rec["label"] != "real" or not wd.capture_path(stem):
            continue
        try:
            if since and wd.capture_time(stem) < since:
                continue
        except ValueError:
            continue
        audio = wd.read_wav(wd.capture_path(stem))
        win = wm.frame_windows(wm.stream_rows(audio[:, min(1, audio.shape[1] - 1)]))
        base = pre.scores(win)
        over = np.where(base >= LIVE_THRESHOLD)[0]
        ver = verifier.confidence(win[over]) if len(over) else np.zeros(0)
        out.append({"capture": stem, "who": rec.get("who", ""), "scores": head.scores(win),
                    "pre_detector": bool(len(over)), "pre_verifier": bool(len(ver) and ver.max() >= verifier.threshold)})
    return out


def cmd_model(args) -> int:
    head = wm.Head(args.onnx)
    pre = wm.Head(wm.pretrained_path())
    verifier = wm.Verifier()
    thresholds = [float(x) for x in args.thresholds.split(",")]
    patiences = [int(x) for x in args.patience.split(",")]
    lines = [f"# {head.name} against today's pipeline, {dt.date.today()}", ""]
    data: dict = {"model": args.onnx}

    # Takes: recall per cell at every setting.
    blocks = wd.enrol_blocks(tuple(args.rounds))
    take_rows = []
    if blocks:
        print(f"Scoring {len(blocks)} blocks…", flush=True)
        for b in blocks:
            audio = wd.read_wav(b["path"])
            win = wm.frame_windows(wm.stream_rows(audio[:, 1]))
            s = head.scores(win)
            base = pre.scores(win)
            over = np.where(base >= LIVE_THRESHOLD)[0]
            ver = np.zeros(len(base), bool)
            if len(over):
                ver[over] = verifier.confidence(win[over]) >= verifier.threshold
            fired = {(thr, pat): set(wm.fires(s, thr, pat)) for thr in thresholds for pat in patiences}
            today = set(wm.fires(base, LIVE_THRESHOLD, 1, ver))
            for take in b["takes"]:
                k0 = int(take["cue_seconds"] / wd.FRAME_SECONDS)
                k1 = int((take["cue_seconds"] + TAKE_SECONDS) / wd.FRAME_SECONDS)
                r = {"person": b["person"], "condition": b["condition"], "position": b["position"],
                     "take": take["take"], "peak": float(s[k0:k1].max()),
                     "today": any(k0 <= k < k1 for k in today)}
                for key, fs in fired.items():
                    r[f"{key[0]:.2f}/{key[1]}"] = any(k0 <= k < k1 for k in fs)
                take_rows.append(r)
    else:
        lines += ["No enrolment takes for these rounds yet: recall on takes not measured.", ""]

    # False alarms in the room's own log.
    since = time.mktime(time.strptime(args.log_since, "%Y-%m-%d %H:%M")) if args.log_since else 0
    until = time.mktime(time.strptime(args.log_until, "%Y-%m-%d %H:%M")) if args.log_until else time.time()
    print("Scoring the feature log…", flush=True)
    hours, counts, fa_times = log_false_alarms(
        [{"name": head.name, "head": head, "kind": "head"}, {"name": "today", "head": pre, "kind": "today"}],
        since, until, thresholds, patiences, verifier)
    print("Scoring the public validation set…", flush=True)
    pub_hours, pub = public_false_alarms(head, thresholds, patiences)
    caps = capture_recall(head, pre, verifier, time.mktime(time.strptime(args.captures_since, "%Y-%m-%d"))
                          if args.captures_since else None)

    def recall(key):
        return sum(r[key] for r in take_rows) / len(take_rows) if take_rows else float("nan")

    def cap_recall(thr, pat):
        return sum(bool(wm.fires(c["scores"], thr, pat)) for c in caps) / len(caps) if caps else float("nan")

    per_week = lambda n: n / hours * 168 if hours else float("nan")  # noqa: E731
    lines += [f"Room feature log: {hours:.1f} h outside known real wake-ups; public validation set: {pub_hours:.1f} h; "
              f"takes: {len(take_rows)}; labelled real captures: {len(caps)}.", "",
              "## Today's pipeline on the same data", "",
              f"- takes opened (detector 0.10 + verifier): {100 * recall('today'):.0f} %" if take_rows else "- takes: –",
              f"- real captures passing detector / verifier: "
              f"{sum(c['pre_detector'] for c in caps)} / {sum(c['pre_verifier'] for c in caps)} of {len(caps)} "
              "(biased: most labelled captures exist because the detector fired)",
              f"- room log, firings per week: detector alone {per_week(counts[('today', 'detector', 1)]):.0f}, "
              f"with verifier {per_week(counts[('today', 'verifier', 1)]):.0f} (the second opinion cannot be "
              "replayed on features)", "",
              "## Grid", "",
              "| threshold | patience | takes | real captures | room firings / week | public firings / 10 h |",
              "|---|---|---|---|---|---|"]
    grid = []
    for thr in thresholds:
        for pat in patiences:
            key = f"{thr:.2f}/{pat}"
            g = {"threshold": thr, "patience": pat, "takes": recall(key), "captures": cap_recall(thr, pat),
                 "room_per_week": per_week(counts[(head.name, thr, pat)]),
                 "public_per_10h": pub.get((thr, pat), 0) / pub_hours * 10 if pub_hours else float("nan")}
            grid.append(g)
            lines.append(f"| {thr:.2f} | {pat} | {100 * g['takes']:.0f} % | {100 * g['captures']:.0f} % | "
                         f"{g['room_per_week']:.1f} | {g['public_per_10h']:.1f} |")
    ok = [g for g in grid if g["room_per_week"] <= args.max_per_week]
    best = max(ok, key=lambda g: (np.nan_to_num(g["takes"]), np.nan_to_num(g["captures"]), g["threshold"]),
               default=None)
    lines += ["", "## Chosen setting", ""]
    if best:
        key = f"{best['threshold']:.2f}/{best['patience']}"
        lines += [f"Threshold {best['threshold']:.2f}, patience {best['patience']}: the best recall with at most "
                  f"{args.max_per_week:g} room firing a week.", ""]
        if take_rows:
            lines += cell_table(take_rows, [(key, "household model"), ("today", "today")])
        data["chosen"] = best
        if fa_times.get((head.name, best["threshold"], best["patience"])):
            lines += ["", "Room firings at this setting (listen to the captures around them):", ""]
            lines += [f"- {wd.stamp(t)}" for t in fa_times[(head.name, best["threshold"], best["patience"])][:50]]
    else:
        lines += [f"No setting stays within {args.max_per_week:g} room firing a week."]
    data.update({"grid": grid, "room_hours": hours, "public_hours": pub_hours, "takes": take_rows,
                 "today_room_per_week": {"detector": per_week(counts[("today", "detector", 1)]),
                                         "verifier": per_week(counts[("today", "verifier", 1)])}})
    print("\n".join(lines))
    print("\n->", write(head.name, lines, data))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("baseline")
    b.add_argument("--rounds", type=int, nargs="+", default=[1, 2])
    b.add_argument("--no-second-opinion", action="store_true", help="skip Vosk/microWakeWord offline")
    m = sub.add_parser("model")
    m.add_argument("onnx")
    m.add_argument("--rounds", type=int, nargs="+", default=[2], help="enrolment rounds to test on (default: 2)")
    m.add_argument("--log-since", help='"YYYY-MM-DD HH:MM": start of the room-log span (the validation week)')
    m.add_argument("--log-until")
    m.add_argument("--captures-since", help="YYYY-MM-DD: only real captures from this day (after the training cut-off)")
    m.add_argument("--thresholds", default="0.3,0.4,0.5,0.6,0.7,0.8,0.9,0.95")
    m.add_argument("--patience", default="1,2,3")
    m.add_argument("--max-per-week", type=float, default=1.0)
    args = ap.parse_args()
    return {"baseline": cmd_baseline, "model": cmd_model}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
