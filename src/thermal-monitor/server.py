#!/usr/bin/env python3
"""Local web dashboard for Pi thermal stats: a live view plus a historical
report, both served from one process so you don't need a separate collector
running in the background.

Usage:
    python3 server.py [--interval SECONDS] [--csv FILE] [--host HOST] [--port PORT]

Then open http://127.0.0.1:8000/ (live) and http://127.0.0.1:8000/history
(full report, same format as analyze.py, generated fresh from the CSV on
each request). Binds to localhost only by default -- pass --host 0.0.0.0
if you actually want it reachable from other devices on your LAN.
"""
import argparse
import csv
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import collector
from analyze import build_report_html, load_rows

LIVE_BUFFER_LEN = 720  # e.g. 1 hour of history at 5s interval, for the mini chart

buffer = deque(maxlen=LIVE_BUFFER_LEN)
buffer_lock = threading.Lock()
csv_stem, csv_ext = "stats_live", ".csv"  # set from --csv in main()
active_csv_path = None  # path currently being written to; read by /history
interval = 5.0


def _week_key(dt):
    """ISO week identity as its Monday's date, e.g. 2026-08-17."""
    monday = dt.date() - timedelta(days=dt.isoweekday() - 1)
    return monday.isoformat()


def _path_for_week(week_key):
    return f"{csv_stem}-{week_key}{csv_ext}"


def collector_loop(stop_event):
    global active_csv_path
    collector.psutil.cpu_percent(percpu=True)  # prime the counter

    current_week = _week_key(datetime.now())
    active_csv_path = _path_for_week(current_week)
    f = open(active_csv_path, "a", newline="")
    writer = csv.DictWriter(f, fieldnames=collector.FIELDS)
    if f.tell() == 0:
        writer.writeheader()
        f.flush()

    try:
        while not stop_event.wait(interval):
            week = _week_key(datetime.now())
            if week != current_week:
                # New calendar week: close last week's file, start a fresh
                # one so no single CSV grows unbounded across weeks.
                f.close()
                current_week = week
                active_csv_path = _path_for_week(current_week)
                f = open(active_csv_path, "a", newline="")
                writer = csv.DictWriter(f, fieldnames=collector.FIELDS)
                if f.tell() == 0:
                    writer.writeheader()
                    f.flush()

            row = collector.sample()
            writer.writerow(row)
            f.flush()
            with buffer_lock:
                buffer.append(row)
    finally:
        f.close()


LIVE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Pi thermal - live</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background:#0b0d10; color:#e6e6e6; }}
h1 {{ font-size: 1.3rem; }}
a {{ color:#4dabf7; }}
.grid {{ display:grid; grid-template-columns: repeat(auto-fit,minmax(160px,1fr)); gap:0.75rem; margin: 1rem 0 1.5rem; }}
.card {{ background:#161a1f; border-radius:10px; padding:0.9rem 1rem; }}
.card .v {{ font-size:1.4rem; font-weight:600; }}
.card .l {{ font-size:0.75rem; color:#9aa4af; text-transform:uppercase; letter-spacing:.04em; }}
.flag-ok {{ color:#69db7c; }}
.flag-bad {{ color:#ff6b6b; }}
canvas {{ background:#12151a; border-radius:10px; }}
#stale {{ color:#e0a800; display:none; margin-bottom:1rem; }}
</style></head>
<body>
<h1>Raspberry Pi thermal monitor -- live</h1>
<p><a href="/history">View historical report &rarr;</a></p>
<p id="stale">No fresh sample yet -- waiting for the collector loop's first tick...</p>
<div class="grid" id="cards"></div>
<canvas id="chart" width="1100" height="300"></canvas>
<script>
const POLL_MS = {poll_ms};
function flag(v) {{ return v ? '<span class="flag-bad">YES</span>' : '<span class="flag-ok">no</span>'; }}
function renderCards(row) {{
  document.getElementById('cards').innerHTML = `
    <div class="card"><div class="v">${{row.cpu_temp_c ?? '--'}}&deg;C</div><div class="l">CPU temp</div></div>
    <div class="card"><div class="v">${{row.rp1_temp_c ?? '--'}}&deg;C</div><div class="l">RP1 temp</div></div>
    <div class="card"><div class="v">${{row.cpu_percent_total ?? '--'}}%</div><div class="l">CPU load</div></div>
    <div class="card"><div class="v">${{row.cpu_freq_mhz_avg ?? '--'}} MHz</div><div class="l">Freq</div></div>
    <div class="card"><div class="v">${{row.load1 ?? '--'}}</div><div class="l">Load avg (1m)</div></div>
    <div class="card"><div class="v">${{flag(row.soft_temp_limit_now)}}</div><div class="l">Soft temp limit</div></div>
    <div class="card"><div class="v">${{flag(row.throttled_now)}}</div><div class="l">Hard throttled</div></div>
    <div class="card"><div class="v">${{flag(row.under_voltage_now)}}</div><div class="l">Under-voltage</div></div>
  `;
}}
function drawChart(recent) {{
  const c = document.getElementById('chart');
  const ctx = c.getContext('2d');
  const W = c.width, H = c.height, pad = 40;
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = '#333'; ctx.strokeRect(pad, pad, W - 2*pad, H - 2*pad);
  const n = recent.length;
  if (n < 2) return;
  const temps = recent.map(r => r.cpu_temp_c).filter(v => v != null);
  const cpus = recent.map(r => r.cpu_percent_total);
  const tMin = Math.min(...temps), tMax = Math.max(...temps);
  function xAt(i) {{ return pad + (i/(n-1)) * (W - 2*pad); }}
  function drawSeries(vals, min, max, color) {{
    ctx.beginPath();
    vals.forEach((v, i) => {{
      if (v == null) return;
      const y = H - pad - ((v - min)/(max - min || 1)) * (H - 2*pad);
      const x = xAt(i);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }});
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
  }}
  drawSeries(recent.map(r => r.cpu_temp_c), tMin, tMax, '#ff6b6b');
  drawSeries(cpus, 0, 100, '#4dabf7');
  ctx.fillStyle = '#ff6b6b'; ctx.fillText('Temp: ' + tMin.toFixed(1) + '-' + tMax.toFixed(1) + 'C', pad, 20);
  ctx.fillStyle = '#4dabf7'; ctx.fillText('CPU %', pad + 200, 20);
  ctx.fillStyle = '#9aa4af'; ctx.fillText(recent[0].timestamp.slice(11), pad, H - pad + 15);
  ctx.fillText(recent[n-1].timestamp.slice(11), W - pad - 50, H - pad + 15);
}}
async function poll() {{
  try {{
    const res = await fetch('/api/live');
    const data = await res.json();
    document.getElementById('stale').style.display = data.latest ? 'none' : 'block';
    if (data.latest) renderCards(data.latest);
    drawChart(data.recent);
  }} catch (e) {{ /* server briefly unreachable between polls; ignore */ }}
}}
poll();
setInterval(poll, POLL_MS);
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # default per-request logging is just noise for a polled dashboard

    def _send(self, status, content_type, body):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", LIVE_PAGE.format(poll_ms=int(interval * 1000)))
        elif path == "/api/live":
            with buffer_lock:
                recent = list(buffer)
            payload = {"latest": recent[-1] if recent else None, "recent": recent}
            self._send(200, "application/json", json.dumps(payload))
        elif path == "/history":
            path_now = active_csv_path
            rows = load_rows(path_now) if path_now and os.path.exists(path_now) else []
            if not rows:
                self._send(200, "text/html; charset=utf-8", "<p>No data collected yet -- check back in a bit.</p>")
                return
            html, _ = build_report_html(rows, path_now)
            self._send(200, "text/html; charset=utf-8", html)
        else:
            self._send(404, "text/plain", "not found")


def main():
    global csv_stem, csv_ext, interval
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument(
        "--csv", default="stats_live.csv",
        help="Base filename; actual files are rotated weekly as '<stem>-<week-start-date><ext>'",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    csv_stem, csv_ext = os.path.splitext(args.csv)
    interval = args.interval

    stop_event = threading.Event()
    thread = threading.Thread(target=collector_loop, args=(stop_event,), daemon=True)
    thread.start()
    time.sleep(0.2)  # let the loop set active_csv_path before we log/serve

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving on http://{args.host}:{args.port}/  (history: /history)")
    print(f"Logging every {interval}s to {active_csv_path} (rotates weekly)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        stop_event.set()
        thread.join(timeout=interval + 2)
        server.server_close()


if __name__ == "__main__":
    main()
