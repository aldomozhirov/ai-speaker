#!/usr/bin/env python3
"""Collect wake-word events from the journal and flag likely false wake-ups
and likely misses, as the starting point for a wake-word investigation.

Joins the wake-word and voice-agent journals into one record per wake event:
openWakeWord peak, verifier score, second-opinion verdict, the capture file
(if the capture is still on disk), and what the voice session did after it:
user transcriptions, tool calls, how it ended. Then sorts the events by
heuristic (see LABELS below). The labels are evidence for a person to check,
not ground truth: listen to the captures before you train on them.

Usage (no venv needed, reads the user journal, touches no audio):

    tools/wake/wake-events-report.py [--since "2026-10-18 16:27"] [--until …]
        [--out DIR] [--no-copy]

Writes to DIR (default ~/.local/state/ai-speaker/measurements/wake-events-<today>/):

    summary.md      counts per day and per label, and the lists to review
    events.csv      one row per wake event (all of them, near-misses included)
    sessions.jsonl  one record per voice session with its transcriptions
    captures/<label>/  copies of the captures of flagged events that still
                    exist (the live capture folder is rotated at 200 MB, which
                    is about two days, so copy before the evidence is gone)

The output contains household speech (transcriptions, recordings). It stays
on the device; never commit it.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path

CAPTURE_DIR = Path.home() / ".local/share/ai-speaker/wake-captures"
OUT_ROOT = Path.home() / ".local/state/ai-speaker/measurements"
THRESHOLD = 0.10          # WAKE_WORD_THRESHOLD
VERIFIER_THRESHOLD = 0.5  # WAKE_WORD_VERIFIER_THRESHOLD
RETRY_WINDOW = 45.0       # s before a successful start in which a failed event counts as an attempt
REPEAT_WINDOW = 30.0      # s between two failed triggers that look like a repeated attempt
QUICK_CANCEL = 15.0       # s: button cancel this soon after a voice start = rejected wake-up

LABELS = {
    "fp-cancelled": "voice session cancelled with the button within 15 s, no tool call: the owner's sign of a false wake-up",
    "fp-empty": "voice session in which nothing intelligible was transcribed and no tool was called: a false wake-up, or a command Gemini did not hear",
    "fp-review": "voice session with speech but no tool call: room conversation picked up, or a real question; read the transcript",
    "ok": "voice session that called a tool or was closed by a closing phrase",
    "empty-after-retry": "like fp-empty, but failed triggers came just before it: probably a real wake-up whose command was not heard, or the owner gave up",
    "miss-retry": "blocked by the second opinion within 45 s before a wanted session (ok, empty or button): a missed attempt",
    "miss-retry-verifier": "openWakeWord fired (>= 0.10), the verifier rejected it, within 45 s before a wanted session: a missed attempt, or the speech just before it",
    "miss-repeat": "two or more blocked triggers within 30 s with no session after: repeated attempts that gave up, or a burst of speech",
    "near-retry": "near-miss below the 0.10 threshold within 45 s before a wanted session: a too-quiet attempt, or the speech just before it",
    "blocked": "blocked by the second opinion, nothing else around it: unknown, listen to it",
    "verifier-rejected": "openWakeWord fired, the verifier said no, nothing else around it: mostly room speech",
    "near": "score 0.05-0.10, nothing else around it",
    "in-session": "event while a session was already open (speech in the session)",
    "shadow-only": "the shadow model fired where the live model logged nothing (score below 0.05): a wake-up only the new model heard, or its false alarm",
}

RX = {
    "relay_voice": re.compile(r"Relaying wake word detection \(score=([\d.]+)\)"),
    "relay_button": re.compile(r"Relaying wake trigger from button"),
    "confirmed": re.compile(r"Wake word confirmed"),
    "blocked": re.compile(r"blocked by the second opinion \(vosk=([\d.]+) micro=([\d.]+)"),
    "event": re.compile(
        r"wake event: peak=([\d.]+) frames_over_threshold=(\d+) fired=(yes|no)(?: verifier=([\d.]+))?"
        r"(?: music=(yes|no))?(?: music_s=(\d+))?(?: volume=([\d.]+))?(?: shadow=([\d.]+))?"
    ),
    "shadow": re.compile(r"Shadow model fired \(score=([\d.]+)"),
    "capture": re.compile(r"Saved capture (\S+/(\d{8}-\d{6})-(\w+)-\d+\.wav) \(\w+, ([\d.]+)s, peak=([\d.]+) at \+([\d.]+)s\)"),
    "start": re.compile(r"Starting session \(trigger=([\w-]+)\)"),
    "not_confirmed": re.compile(r"Wake word not confirmed; no session"),
    "ignored_active": re.compile(r"Ignoring voice-pending trigger; session already active"),
    "cancel": re.compile(r"Button pressed (?:while active|during start-up)"),
    "idle": re.compile(r"Idle timeout"),
    "quick": re.compile(r"Quick action completed"),
    "closing": re.compile(r"Closing phrase detected"),
    "maxsess": re.compile(r"Session exceeded"),
    "ended": re.compile(r"Session ended"),
    "user": re.compile(r"User transcription: (.*)$"),
    "assistant": re.compile(r"Assistant said: (.*)$"),
    "tool": re.compile(r"Tool call: (\w+)\((.*?)\) heard="),
}

FILLER = re.compile(r"^\s*(<noise>)?[\s.,!?…\-–—\d%]*$|^\s*(угу|ага|вот|да|ja|uh|um|mm|hm)[.!?]?\s*$", re.I)


@dataclass
class Event:
    t: float
    kind: str                      # trigger | event | button
    peak: float | None = None
    frames: int | None = None
    verifier: float | None = None
    fired: bool = False
    relay_score: float | None = None
    decision: str = ""             # confirmed | blocked | ""
    vosk: float | None = None
    micro: float | None = None
    session: int | None = None     # index into sessions
    in_session: bool = False
    capture: str = ""
    label: str = ""
    note: str = ""
    music: bool | None = None      # journalled since 2026-11-15 (wake_context.py)
    music_s: int | None = None     # how long the music had been playing
    volume: float | None = None
    shadow: float | None = None    # shadow model's peak (stage F of the custom-model plan)
    shadow_fired: bool = False
    human: str = ""                # real / false / unsure from wake-label.py, if labelled


@dataclass
class Session:
    t: float
    trigger: str
    end: float | None = None
    end_reason: str = ""
    user: list = field(default_factory=list)
    assistant: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    cancel_after: float | None = None


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def journal(since: str, until: str | None):
    cmd = ["journalctl", "--user", "-u", "wake-word", "-u", "voice-agent",
           "-o", "json", "--no-pager", "-q", "--since", since]
    if until:
        cmd += ["--until", until]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = rec.get("MESSAGE")
        if isinstance(msg, list):
            msg = bytes(msg).decode("utf-8", "replace")
        if not msg or "raw_score=" in msg or "peak amplitude" in msg:
            continue
        unit = rec.get("_SYSTEMD_USER_UNIT") or rec.get("USER_UNIT") or ""
        yield int(rec["__REALTIME_TIMESTAMP"]) / 1e6, unit, msg
    proc.wait()


def parse(lines):
    events: list[Event] = []
    sessions: list[Session] = []
    captures = []                  # (start, end, path, peak)
    open_session: Session | None = None
    last_trigger: Event | None = None
    shadow_fires = []              # (time, score)

    def recent_trigger(t, pred, window=6.0):
        for e in reversed(events):
            if t - e.t > window:
                return None
            if e.kind == "trigger" and pred(e):
                return e
        return None

    for t, unit, msg in lines:
        if m := RX["relay_voice"].search(msg):
            last_trigger = Event(t, "trigger", relay_score=float(m[1]), fired=True,
                                 in_session=open_session is not None)
            events.append(last_trigger)
        elif RX["relay_button"].search(msg):
            events.append(Event(t, "button", in_session=open_session is not None))
        elif RX["confirmed"].search(msg):
            if e := recent_trigger(t, lambda e: not e.decision):
                e.decision = "confirmed"
        elif m := RX["blocked"].search(msg):
            if e := recent_trigger(t, lambda e: not e.decision):
                e.decision, e.vosk, e.micro = "blocked", float(m[1]), float(m[2])
        elif m := RX["event"].search(msg):
            peak, frames, fired = float(m[1]), int(m[2]), m[3] == "yes"
            ver = float(m[4]) if m[4] else None
            e = recent_trigger(t, lambda e: e.peak is None, window=8.0) if fired else None
            if e is None:
                e = Event(t - 1.0, "event", fired=fired, in_session=open_session is not None)
                events.append(e)
            e.peak, e.frames, e.verifier = peak, frames, ver
            if m[5]:
                e.music = m[5] == "yes"
                e.music_s = int(m[6]) if m[6] else None
            if m[7]:
                e.volume = float(m[7])
            if m[8]:
                e.shadow = float(m[8])
        elif m := RX["shadow"].search(msg):
            shadow_fires.append((t, float(m[1])))
        elif m := RX["capture"].search(msg):
            start = dt.datetime.strptime(m[2], "%Y%m%d-%H%M%S").timestamp()
            captures.append((start, start + float(m[4]), m[1], float(m[5])))
        elif m := RX["start"].search(msg):
            open_session = Session(t, m[1])
            sessions.append(open_session)
            idx = len(sessions) - 1
            if m[1] == "button":
                for e in reversed(events):
                    if e.kind == "button" and t - e.t < 3 and e.session is None:
                        e.session, e.in_session = idx, False
                        break
            elif last_trigger is not None and t - last_trigger.t < 3:
                last_trigger.session, last_trigger.in_session = idx, False
        elif RX["not_confirmed"].search(msg):
            if open_session is not None:
                sessions.pop()
                for e in events:
                    if e.session == len(sessions):
                        e.session = None
                open_session = None
        elif open_session is not None:
            s = open_session
            if RX["cancel"].search(msg):
                s.end_reason = s.end_reason or "button"
                s.cancel_after = round(t - s.t, 1)
            elif RX["idle"].search(msg):
                s.end_reason = s.end_reason or "idle"
            elif RX["quick"].search(msg):
                s.end_reason = s.end_reason or "quick-action"
            elif RX["closing"].search(msg):
                s.end_reason = s.end_reason or "closing-phrase"
            elif RX["maxsess"].search(msg):
                s.end_reason = s.end_reason or "max-duration"
            elif m := RX["user"].search(msg):
                s.user.append(m[1].strip("'\" "))
            elif m := RX["assistant"].search(msg):
                s.assistant.append(m[1].strip("'\" "))
            elif m := RX["tool"].search(msg):
                s.tools.append(f"{m[1]}({m[2]})")
            elif RX["ended"].search(msg):
                s.end = t
                open_session = None

    attach_shadow(events, shadow_fires)
    # A capture's file name is its start; the event sits at its peak.
    for e in events:
        if e.kind == "button":
            near = [c for c in captures if c[2].endswith(".wav") and "-button-" in c[2] and abs(c[0] - e.t) < 3]
        else:
            near = [c for c in captures if c[0] - 1.5 <= e.t <= c[1] + 1.5 and "-button-" not in c[2]]
        if near:
            e.capture = min(near, key=lambda c: abs((c[3] if e.peak is None else c[3] - e.peak)))[2]
    return events, sessions


def intelligible(texts):
    return [x for x in texts if x and not FILLER.match(x)]


def attach_labels(events, path=Path.home() / ".local/share/ai-speaker/wake-labels.jsonl"):
    """Hand labels from tools/wake/wake-label.py (latest line per capture wins)."""
    labels = {}
    try:
        with open(path) as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    labels[rec["capture"]] = rec["label"]
    except FileNotFoundError:
        return 0
    n = 0
    for e in events:
        if e.capture and Path(e.capture).stem in labels:
            e.human = labels[Path(e.capture).stem]
            n += 1
    return n


def attach_shadow(events, shadow_fires):
    """Mark the event each shadow firing belongs to; a firing with no live
    event near it becomes an event of its own."""
    for t, score in shadow_fires:
        near = [e for e in events if e.kind in ("trigger", "event") and abs(e.t - t) <= 3.0]
        if near:
            e = min(near, key=lambda e: abs(e.t - t))
            e.shadow_fired = True
            e.shadow = max(e.shadow or 0.0, score)
        else:
            events.append(Event(t, "shadow", shadow=score, shadow_fired=True))
    events.sort(key=lambda e: e.t)


def label(events, sessions):
    for e in events:
        if e.kind == "shadow":
            e.label = "shadow-only"
        elif e.kind == "button":
            e.label = "button-session" if e.session is not None else ("button-cancel" if e.in_session else "button")
        elif e.session is not None:
            s = sessions[e.session]
            if s.end_reason == "button" and (s.cancel_after or 99) <= QUICK_CANCEL and not s.tools:
                e.label = "fp-cancelled"
            elif s.tools or s.end_reason == "closing-phrase":
                e.label = "ok"
            elif not intelligible(s.user):
                e.label = "fp-empty"
            else:
                e.label = "fp-review"
    # A session someone wanted: it did something, it was started with the
    # button, or it heard nothing (the owner may have given up after the
    # retries). Room conversations and cancelled sessions do not count.
    wanted = {}
    for e in events:
        if e.session is not None and e.label in ("ok", "fp-empty", "button-session"):
            wanted[e.session] = e
    starts = sorted(((sessions[i].t, sessions[i].trigger, e) for i, e in wanted.items()), key=lambda x: x[0])
    retries = []
    for e in events:
        if e.label or e.in_session:
            e.label = e.label or "in-session"
            continue
        later = [st for st in starts if 0 < st[0] - e.t <= RETRY_WINDOW]
        if later:
            t0, trig, target = later[0]
            retries.append((e, t0, trig, target))
            if e.decision == "blocked":
                e.label = "miss-retry"
            elif (e.peak or 0) >= THRESHOLD:
                e.label = "miss-retry-verifier"
            else:
                e.label = "near-retry"
            if target.label == "fp-empty" and e.label != "near-retry":
                target.label = "empty-after-retry"
        elif e.decision == "blocked":
            e.label = "blocked"
        elif e.kind == "event" and e.verifier is not None:
            e.label = "verifier-rejected"
        else:
            e.label = "near"
    for e, t0, trig, target in retries:
        e.note = f"then {trig} session after {t0 - e.t:.0f} s ({target.label})"
    # Repeated blocked triggers with no session after them.
    blocked = [e for e in events if e.label == "blocked"]
    for a, b in zip(blocked, blocked[1:]):
        if b.t - a.t <= REPEAT_WINDOW:
            a.label = b.label = "miss-repeat"


def fmt(v, spec=".3f"):
    return "" if v is None else format(v, spec)


def write(out: Path, events, sessions, since, until, copy: bool):
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "events.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "label", "kind", "peak", "frames", "verifier", "decision", "vosk", "micro",
                    "session_trigger", "session_end", "cancel_after_s", "tools", "user_said", "capture", "note",
                    "music", "music_s", "volume", "shadow", "shadow_fired", "labelled"])
        for e in events:
            s = sessions[e.session] if e.session is not None else None
            w.writerow([iso(e.t), e.label, e.kind, fmt(e.peak), e.frames if e.frames is not None else "",
                        fmt(e.verifier), e.decision, fmt(e.vosk, ".2f"), fmt(e.micro, ".2f"),
                        s.trigger if s else "", s.end_reason if s else "",
                        "" if not s or s.cancel_after is None else s.cancel_after,
                        "; ".join(s.tools) if s else "", " | ".join(s.user)[:300] if s else "",
                        Path(e.capture).name if e.capture else "", e.note,
                        "" if e.music is None else ("yes" if e.music else "no"),
                        "" if e.music_s is None else e.music_s, fmt(e.volume, ".2f"),
                        fmt(e.shadow), "yes" if e.shadow_fired else "", e.human])
    with open(out / "sessions.jsonl", "w") as f:
        for s in sessions:
            f.write(json.dumps({**asdict(s), "t": iso(s.t), "end": iso(s.end) if s.end else None},
                               ensure_ascii=False) + "\n")

    copied = Counter()
    if copy:
        for e in events:
            if not e.capture or e.label in ("near", "verifier-rejected", "in-session", "button"):
                continue
            src = Path(e.capture)
            if not src.exists():
                continue
            dst = out / "captures" / e.label
            dst.mkdir(parents=True, exist_ok=True)
            for p in (src, src.with_suffix(".json")):
                if p.exists() and not (dst / p.name).exists():
                    shutil.copy2(p, dst / p.name)
            copied[e.label] += 1

    voice = [e for e in events if e.kind not in ("button", "shadow")]
    days = sorted({iso(e.t)[:10] for e in events})
    per_day = defaultdict(Counter)
    for e in events:
        d = per_day[iso(e.t)[:10]]
        d[e.label] += 1
        if e.kind == "trigger":
            d["triggers"] += 1
            d["dec-" + (e.decision or "undecided")] += 1
    labels = Counter(e.label for e in events)

    L = []
    L.append(f"# Wake-word events {since} … {until or 'now'}\n")
    L.append(f"Generated {iso(dt.datetime.now().timestamp())} by tools/wake/wake-events-report.py. "
             "Labels are heuristics for a person to check (see the legend), not ground truth.\n")
    L.append("## Totals\n")
    trig = [e for e in voice if e.kind == "trigger"]
    L.append(f"- Wake events logged (score > 0.05): {len(voice)}")
    L.append(f"- Triggers that passed openWakeWord and the verifier: {len(trig)} "
             f"(confirmed {sum(e.decision == 'confirmed' for e in trig)}, "
             f"blocked by the second opinion {sum(e.decision == 'blocked' for e in trig)})")
    L.append(f"- Voice sessions: {sum(1 for s in sessions if s.trigger != 'button')}, "
             f"button sessions: {sum(1 for s in sessions if s.trigger == 'button')}")
    L.append(f"- Captures copied: {sum(copied.values())} (near-misses rotate within days, other captures "
             "after about two months since 2026-11-15)\n")
    L.append("## By label\n")
    L.append("| Label | Count | Meaning |\n|---|---|---|")
    for k, desc in LABELS.items():
        L.append(f"| {k} | {labels.get(k, 0)} | {desc} |")
    for k in ("button-session", "button-cancel", "button"):
        L.append(f"| {k} | {labels.get(k, 0)} | button press |")
    L.append("\n## Per day\n")
    cols = ["triggers", "dec-confirmed", "dec-blocked", "ok", "fp-cancelled", "fp-empty", "empty-after-retry",
            "fp-review", "miss-retry", "miss-retry-verifier", "miss-repeat", "button-session"]
    L.append("| Day | " + " | ".join(cols) + " |\n|" + "---|" * (len(cols) + 1))
    for d in days:
        L.append(f"| {d} | " + " | ".join(str(per_day[d].get(c, 0)) for c in cols) + " |")

    groups = {
        "Did something": ("ok",),
        "Unwanted sessions": ("fp-empty", "fp-cancelled", "fp-review", "empty-after-retry"),
        "Likely misses": ("miss-retry", "miss-retry-verifier", "miss-repeat", "near-retry"),
        "Blocked, unexplained": ("blocked",),
        "Rejected by the verifier": ("verifier-rejected",),
    }
    if any(e.music is not None for e in voice):
        L.append("\n## Music playing at the time\n")
        L.append("Journalled since 2026-11-15; events before that count as unknown. "
                 "\"First 15 s\": music had been playing for under 15 s (echo canceller still converging).\n")
        L.append("| Events | Music | … first 15 s | No music | Unknown |\n|---|---|---|---|---|")
        for title, keys in groups.items():
            rows = [e for e in voice if e.label in keys]
            on = [e for e in rows if e.music]
            L.append(f"| {title} | {len(on)} | {sum(e.music_s is not None and e.music_s < 15 for e in on)} | "
                     f"{sum(e.music is False for e in rows)} | {sum(e.music is None for e in rows)} |")

    if any(e.human for e in events):
        L.append("\n## Labelled by hand (wake-label.py)\n")
        L.append("Where a person listened, their label against this report's guess.\n")
        L.append("| Guess | real | false | unsure |\n|---|---|---|---|")
        for k in sorted({e.label for e in events if e.human}):
            rows = [e for e in events if e.label == k and e.human]
            L.append(f"| {k} | " + " | ".join(str(sum(e.human == h for e in rows)) for h in ("real", "false", "unsure")) + " |")

    if any(e.shadow is not None or e.shadow_fired for e in events):
        L.append("\n## Shadow model\n")
        L.append("Where the live pipeline and the shadow model (stage F) agree and disagree. "
                 "\"Live opened\" = a voice session started.\n")
        L.append("| Events | Shadow fired | Shadow quiet |\n|---|---|---|")
        for title, keys in groups.items():
            rows = [e for e in voice if e.label in keys]
            L.append(f"| {title} | {sum(e.shadow_fired for e in rows)} | {sum(not e.shadow_fired for e in rows)} |")
        only = [e for e in events if e.label == "shadow-only"]
        L.append(f"| Live logged nothing (score < 0.05) | {len(only)} | – |")

    def table(title, rows):
        L.append(f"\n## {title} ({len(rows)})\n")
        if not rows:
            L.append("None.")
            return
        L.append("| Time | Label | Peak | Verifier | Vosk/micro | Session | Capture | Heard / note |\n|---|---|---|---|---|---|---|---|")
        for e in rows:
            s = sessions[e.session] if e.session is not None else None
            sess = "" if not s else f"{s.end_reason or '?'}" + (f" after {s.cancel_after}s" if s.cancel_after else "")
            heard = "" if not s else (" / ".join(s.user)[:120] + (" → " + ", ".join(s.tools) if s.tools else ""))
            vm = "" if e.vosk is None else f"{e.vosk:.2f}/{e.micro:.2f}"
            cap = Path(e.capture).name if e.capture and Path(e.capture).exists() else ("(rotated)" if e.capture else "")
            L.append(f"| {iso(e.t)} | {e.label} | {fmt(e.peak)} | {fmt(e.verifier)} | {vm} | {sess} | {cap} | {(heard or e.note).replace('|', '/')} |")

    table("Likely false wake-ups", [e for e in voice if e.label in ("fp-cancelled", "fp-empty")])
    table("Empty sessions after failed attempts", [e for e in voice if e.label == "empty-after-retry"])
    table("Sessions to read (speech, no tool call)", [e for e in voice if e.label == "fp-review"])
    table("Likely misses", [e for e in voice if e.label in ("miss-retry", "miss-retry-verifier", "miss-repeat")])
    table("Blocked, unexplained", [e for e in voice if e.label == "blocked"])
    table("Shadow model only", [e for e in events if e.label == "shadow-only"])
    (out / "summary.md").write_text("\n".join(L) + "\n")
    return labels, copied


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", default="2026-10-18 16:27:28",
                    help="journal start (default: when the normalised second opinion went live)")
    ap.add_argument("--until", default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-copy", action="store_true", help="do not copy captures")
    args = ap.parse_args(argv)
    out = args.out or OUT_ROOT / f"wake-events-{dt.date.today().isoformat()}"
    events, sessions = parse(journal(args.since, args.until))
    label(events, sessions)
    attach_labels(events)
    labels, copied = write(out, events, sessions, args.since, args.until, not args.no_copy)
    print(f"{len(events)} events, {len(sessions)} sessions -> {out}")
    for k, v in labels.most_common():
        print(f"  {k:18} {v:5}  ({copied.get(k, 0)} captures copied)")


if __name__ == "__main__":
    sys.exit(main())
