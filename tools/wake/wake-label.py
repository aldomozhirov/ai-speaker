#!/usr/bin/env python3
"""Label wake-word captures as real / false / unsure, in a browser page
(docs/plans/wake-word-custom-model.md, stage C).

    tools/wake/wake-label.py serve [--since "2026-10-18"] [--port 8765]
    tools/wake/wake-label.py import-verifier      # the 103 hand-sorted clips of Oct
    tools/wake/wake-label.py stats

`serve` prints an address with a random token; open it on a phone or laptop
on the home network. Each clip plays in the browser (channel 1, the one the
detector hears, made louder), with what the speaker made of it and what the
session heard. Keys: r = real, f = false, u = unsure, s = skip, space = play
again. Only while it runs is anything served.

Clips come in the order that is most useful to label:
  0. shadow-model disagreements (stage F)
  1. sessions and button presses
  2. blocked by the second opinion
  3. the second opinion heard "Jarvis" but the verifier rejected it
  4. other retries
  5. a random sample of verifier rejections (checking they are false)

Labels go to ~/.local/share/ai-speaker/wake-labels.jsonl (latest line per
capture wins); every labelled capture is copied to wake-captures/kept/,
which rotation never deletes. Household audio: never leaves the device.
Needs numpy (any of the venvs): ~/.venvs/wake-word/bin/python tools/wake/wake-label.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import html
import importlib.util
import io
import json
import os
import random
import secrets
import shutil
import socket
import sys
import time
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wake_data as wd  # noqa: E402


def load_report():
    spec = importlib.util.spec_from_file_location("wake_events_report", os.path.join(HERE, "wake-events-report.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wake_events_report"] = mod
    spec.loader.exec_module(mod)
    return mod


PRIORITY = {
    "shadow": 0,
    "ok": 1, "fp-empty": 1, "fp-cancelled": 1, "fp-review": 1, "empty-after-retry": 1,
    "button-session": 1, "button-cancel": 1, "button": 1,
    "blocked": 2, "miss-retry": 2, "miss-repeat": 2,
    "so-heard": 3,
    "miss-retry-verifier": 4, "near-retry": 4,
    "verifier-rejected": 5,
}
RANDOM_REJECTED = 200


def captures_on_disk() -> dict[str, tuple[float, float]]:
    """stem -> (start, end) of every capture still on disk."""
    out = {}
    for d in (wd.CAPTURES, wd.KEPT):
        for p in glob.glob(os.path.join(d, "*.json")):
            stem = os.path.basename(p)[:-5]
            if stem in out:
                continue
            try:
                with open(p) as f:
                    meta = json.load(f)
                start = float(meta.get("start_time") or wd.capture_time(stem))
                out[stem] = (start, start + len(meta.get("scores", [])) * wd.FRAME_SECONDS)
            except (OSError, ValueError):
                continue
    return out


def build_queue(since: str) -> list[dict]:
    rep = load_report()
    events, sessions = rep.parse(rep.journal(since, None))
    rep.label(events, sessions)
    disk = captures_on_disk()
    labels = wd.load_labels()

    def stem_at(t: float) -> str | None:
        for stem, (a, b) in disk.items():
            if a - 1.5 <= t <= b + 1.5:
                return stem
        return None

    items: dict[str, dict] = {}

    def add(stem, why, info):
        if stem is None or stem in labels or stem not in disk:
            return
        prio = PRIORITY.get(why, 6)
        if stem not in items or prio < items[stem]["priority"]:
            items[stem] = {"capture": stem, "why": why, "priority": prio, **info}

    for e in events:
        stem = os.path.basename(e.capture)[:-4] if e.capture else stem_at(e.t)
        s = sessions[e.session] if e.session is not None else None
        info = {
            "time": wd.stamp(e.t), "peak": e.peak, "verifier": e.verifier, "decision": e.decision,
            "vosk": e.vosk, "micro": e.micro, "music": e.music,
            "heard": " / ".join(s.user)[:300] if s else "", "tools": ", ".join(s.tools) if s else "",
            "session_end": s.end_reason if s else "", "note": e.note,
        }
        if e.label in PRIORITY:
            add(stem, e.label, info)
    # The second opinion heard "Jarvis" where the verifier said no.
    for t, _, msg in wd.journal(since, units=("wake-word",), grep="second opinion: vosk=.*-> pass.*detector=rejected"):
        add(stem_at(t), "so-heard", {"time": wd.stamp(t), "note": msg.split("second opinion: ")[-1][:120]})
    # Shadow-model captures (stage F) come first.
    for stem in disk:
        if "-shadow-" in stem:
            add(stem, "shadow", {"time": wd.stamp(disk[stem][0])})
    queue = sorted(items.values(), key=lambda i: (i["priority"], i.get("time", "")))
    rejected = [i for i in queue if i["why"] == "verifier-rejected"]
    keep_rejected = set(i["capture"] for i in random.Random(0).sample(rejected, min(RANDOM_REJECTED, len(rejected))))
    return [i for i in queue if i["why"] != "verifier-rejected" or i["capture"] in keep_rejected]


def listenable(stem: str) -> bytes:
    """Channel 1 of a capture, peak-normalised, as a mono WAV."""
    path = wd.capture_path(stem)
    audio = wd.read_wav(path)
    mono = audio[:, min(1, audio.shape[1] - 1)].astype(np.float32)
    peak = float(np.abs(mono).max()) or 1.0
    mono = np.clip(mono * min(20.0, 0.7 * 32767 / peak), -32767, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(wd.RATE)
        w.writeframes(mono.tobytes())
    return buf.getvalue()


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Wake labels</title><style>
:root{color-scheme:light dark;--bg:#fafafa;--fg:#222;--mut:#777;--card:#fff;--line:#ddd}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--mut:#999;--card:#222;--line:#333}}
body{font:16px system-ui,sans-serif;margin:0;padding:16px;background:var(--bg);color:var(--fg);max-width:720px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}
.mut{color:var(--mut);font-size:14px} audio{width:100%;margin:10px 0}
.btns{display:flex;gap:8px;flex-wrap:wrap} button{font-size:18px;padding:12px 16px;border-radius:8px;border:1px solid var(--line);flex:1;min-width:90px;background:var(--card);color:var(--fg)}
.r{background:#2e7d32;color:#fff}.f{background:#c62828;color:#fff}
input{font-size:16px;padding:8px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
dt{float:left;width:90px;color:var(--mut)} dd{margin:0 0 4px 90px;overflow-wrap:anywhere}
</style></head><body>
<h2>Wake-word labels</h2>
<div class=mut id=progress></div>
<p>Who is speaking (if real): <input id=who placeholder="name"></p>
<div class=card id=card>Loading…</div>
<p class=mut>Keys: r real · f false · u unsure · s skip · space replay. "Real" = someone said "Hey Jarvis" to the speaker.</p>
<script>
const T=new URLSearchParams(location.search).get('t');let Q=[],i=0,done=0;
const who=document.getElementById('who');try{who.value=localStorage.getItem('who')||''}catch(e){}
who.onchange=()=>{try{localStorage.setItem('who',who.value)}catch(e){}};
function esc(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function show(){const c=document.getElementById('card');
document.getElementById('progress').textContent=`${done} labelled this time · ${Q.length-i} to go`;
if(i>=Q.length){c.innerHTML='Nothing left to label. Thank you.';return}
const q=Q[i];const rows=[['why',q.why],['time',q.time],['peak',q.peak],['verifier',q.verifier],['decision',q.decision],
['vosk/micro',q.vosk==null?'':q.vosk+' / '+q.micro],['music',q.music==null?'':(q.music?'yes':'no')],['heard',q.heard],['tools',q.tools],['session',q.session_end],['note',q.note]]
.filter(r=>r[1]!==''&&r[1]!=null&&r[1]!==undefined);
c.innerHTML=`<b>${esc(q.capture)}</b><audio id=au controls autoplay src="audio/${encodeURIComponent(q.capture)}?t=${T}"></audio>
<div class=btns><button class=r onclick="lab('real')">Real (r)</button><button class=f onclick="lab('false')">False (f)</button>
<button onclick="lab('unsure')">Unsure (u)</button><button onclick="skip()">Skip (s)</button></div>
<dl>${rows.map(r=>`<dt>${esc(r[0])}</dt><dd>${esc(r[1])}</dd>`).join('')}</dl>`}
async function lab(v){const q=Q[i];const r=await fetch('api/label?t='+T,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({capture:q.capture,label:v,who:v==='real'?who.value:''})});
if(r.ok){done++;i++;show()}else alert('Could not save: '+r.status)}
function skip(){i++;show()}
document.onkeydown=e=>{if(e.target===who)return;const k=e.key.toLowerCase();
if(k==='r')lab('real');else if(k==='f')lab('false');else if(k==='u')lab('unsure');else if(k==='s')skip();
else if(k===' '){e.preventDefault();const a=document.getElementById('au');if(a){a.currentTime=0;a.play()}}};
fetch('api/queue?t='+T).then(r=>r.json()).then(q=>{Q=q;show()});
</script></body></html>"""


def serve(args) -> int:
    print("Building the queue from the journal…", flush=True)
    queue = build_queue(args.since)
    token = secrets.token_urlsafe(12)
    print(f"{len(queue)} clips to label.", flush=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("t", [""])[0] != token:
                self.send_error(403)
                return False
            return True

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._ok():
                return
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                self._send(PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/queue":
                fresh = wd.load_labels()
                self._send(json.dumps([q for q in queue if q["capture"] not in fresh], default=str).encode(),
                           "application/json")
            elif path.startswith("/audio/"):
                stem = urllib.parse.unquote(path[len("/audio/"):])
                if "/" in stem or not wd.capture_path(stem):
                    self.send_error(404)
                    return
                self._send(listenable(stem), "audio/wav")
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._ok():
                return
            if urllib.parse.urlparse(self.path).path != "/api/label":
                self.send_error(404)
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                stem, label = body["capture"], body["label"]
                if "/" in stem or not wd.capture_path(stem):
                    raise ValueError("unknown capture")
                wd.set_label(stem, label, str(body.get("who", ""))[:40])
            except (ValueError, KeyError) as exc:
                self.send_error(400, str(exc))
                return
            self._send(b"{}", "application/json")

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    host = socket.gethostname()
    print(f"Open on a phone or laptop at home (Ctrl-C to stop):\n"
          f"  http://{host}.local:{args.port}/?t={token}\n"
          f"  (or the speaker's address instead of {host}.local)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print(f"\n{len(wd.load_labels())} captures labelled in all.")
    return 0


def import_verifier(args) -> int:
    """Labels for the clips sorted by hand into wake-verifier/ in Oct 2026."""
    os.makedirs(wd.KEPT, mode=0o700, exist_ok=True)
    have = wd.load_labels()
    n = 0
    for folder, label in (("positive", "real"), ("negative", "false"), ("unlabelled-ambiguous", "unsure")):
        for wav in sorted(glob.glob(os.path.join(wd.VERIFIER_DIR, folder, "*.wav"))):
            stem = os.path.basename(wav)[:-4]
            dst = os.path.join(wd.KEPT, stem + ".wav")
            if not os.path.exists(dst):
                shutil.copy2(wav, dst)
            src_json = wav[:-4] + ".json"
            if os.path.exists(src_json) and not os.path.exists(dst[:-4] + ".json"):
                shutil.copy2(src_json, dst[:-4] + ".json")
            if stem not in have:
                wd.set_label(stem, label, source=f"wake-verifier/{folder}")
                n += 1
    print(f"{n} labels added from {wd.VERIFIER_DIR}; audio copied to {wd.KEPT}")
    return 0


def stats(args) -> int:
    labels = wd.load_labels()
    by = {}
    for rec in labels.values():
        by.setdefault(rec["label"], []).append(rec)
    print(f"{len(labels)} captures labelled ({wd.LABELS})")
    for k in wd.LABEL_VALUES:
        people = {}
        for r in by.get(k, []):
            people[r.get("who") or "?"] = people.get(r.get("who") or "?", 0) + 1
        extra = ("  by " + ", ".join(f"{p} {n}" for p, n in sorted(people.items()))) if k == "real" else ""
        print(f"  {k:7} {len(by.get(k, [])):5}{extra}")
    missing = sum(1 for s in labels if not wd.capture_path(s))
    if missing:
        print(f"  ({missing} labelled captures have no audio left)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve")
    sp.add_argument("--since", default=(dt.date.today() - dt.timedelta(days=60)).isoformat())
    sp.add_argument("--port", type=int, default=8765)
    sub.add_parser("import-verifier")
    sub.add_parser("stats")
    args = ap.parse_args()
    return {"serve": serve, "import-verifier": import_verifier, "stats": stats}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
