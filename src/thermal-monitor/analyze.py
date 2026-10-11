#!/usr/bin/env python3
"""Summarizes stats.csv from collector.py and writes a self-contained HTML report
(no external libraries or network access required).

Usage:
    python3 analyze.py --csv stats.csv --out report.html
"""
import argparse
import csv
import json
import statistics as st
from datetime import datetime


def load_rows(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                r["cpu_temp_c"] = float(r["cpu_temp_c"]) if r["cpu_temp_c"] else None
                r["rp1_temp_c"] = float(r["rp1_temp_c"]) if r["rp1_temp_c"] else None
                r["cpu_percent_total"] = float(r["cpu_percent_total"]) if r["cpu_percent_total"] else None
                r["cpu_freq_mhz_avg"] = float(r["cpu_freq_mhz_avg"]) if r["cpu_freq_mhz_avg"] else None
            except ValueError:
                continue
            if r["cpu_temp_c"] is not None and r["cpu_percent_total"] is not None:
                rows.append(r)
    return rows


def pearson(xs, ys):
    if len(xs) < 2:
        return None
    try:
        return st.correlation(xs, ys)
    except Exception:
        mx, my = st.mean(xs), st.mean(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5
        return num / den if den else None


def build_report_html(rows, source_label):
    """Builds the self-contained HTML report from already-loaded rows.

    Shared by the analyze.py CLI and server.py's /history endpoint so both
    produce the exact same report from the exact same logic.
    """
    temps = [r["cpu_temp_c"] for r in rows]
    cpus = [r["cpu_percent_total"] for r in rows]
    freqs = [r["cpu_freq_mhz_avg"] for r in rows if r["cpu_freq_mhz_avg"] is not None]
    n = len(rows)

    try:
        t0 = datetime.fromisoformat(rows[0]["timestamp"])
        t1 = datetime.fromisoformat(rows[-1]["timestamp"])
        duration_minutes = round((t1 - t0).total_seconds() / 60, 1)
    except Exception:
        duration_minutes = None

    corr = pearson(cpus, temps)
    soft_limit_n = sum(1 for r in rows if r.get("soft_temp_limit_now") == "True")
    throttled_n = sum(1 for r in rows if r.get("throttled_now") == "True")
    freq_capped_n = sum(1 for r in rows if r.get("freq_capped_now") == "True")
    undervolt_n = sum(1 for r in rows if r.get("under_voltage_now") == "True")

    summary = {
        "samples": n,
        "duration_minutes": duration_minutes,
        "temp_min_c": round(min(temps), 1),
        "temp_max_c": round(max(temps), 1),
        "temp_mean_c": round(st.mean(temps), 1),
        "temp_stdev_c": round(st.pstdev(temps), 2) if n > 1 else 0,
        "cpu_min_pct": round(min(cpus), 1),
        "cpu_max_pct": round(max(cpus), 1),
        "cpu_mean_pct": round(st.mean(cpus), 1),
        "freq_min_mhz": round(min(freqs)) if freqs else None,
        "freq_max_mhz": round(max(freqs)) if freqs else None,
        "correlation_cpu_vs_temp": round(corr, 3) if corr is not None else None,
        "pct_time_soft_temp_limit": round(100 * soft_limit_n / n, 1),
        "pct_time_hard_throttled": round(100 * throttled_n / n, 1),
        "pct_time_freq_capped": round(100 * freq_capped_n / n, 1),
        "pct_time_undervoltage": round(100 * undervolt_n / n, 1),
    }

    verdict = []
    if summary["temp_max_c"] >= 85:
        verdict.append("Hit >=85C: hard thermal throttling territory for Pi 5 -- cooling is undersized for observed load.")
    elif summary["temp_max_c"] >= 80:
        verdict.append("Hit >=80C: soft throttle zone -- cooling is marginal.")
    if summary["pct_time_soft_temp_limit"] > 0 or summary["pct_time_hard_throttled"] > 0:
        verdict.append("Throttling was actually engaged during this run -- current heatsink/fan is not keeping pace with this workload.")
    if summary["pct_time_undervoltage"] > 0:
        verdict.append("Under-voltage detected -- check power supply/cable, not just cooling.")
    if not verdict:
        verdict.append("No throttling observed in this run; temps stayed below the soft limit (80C).")

    multi_day = duration_minutes is not None and duration_minutes > 24 * 60
    label_fmt = "%m-%d %H:%M" if multi_day else "%H:%M:%S"

    def fmt_ts(raw):
        try:
            return datetime.fromisoformat(raw).strftime(label_fmt)
        except Exception:
            return raw

    # Downsample for the chart so long runs (a day at a few-second interval
    # is tens of thousands of points) stay a legible, responsive canvas draw.
    # Stats above are computed on the full, non-decimated row set.
    max_points = 1000
    stride = max(1, n // max_points)
    chart_rows = rows[::stride]
    if chart_rows[-1] is not rows[-1]:
        chart_rows.append(rows[-1])

    labels = [fmt_ts(r["timestamp"]) for r in chart_rows]
    chart_temps = [r["cpu_temp_c"] for r in chart_rows]
    chart_cpus = [r["cpu_percent_total"] for r in chart_rows]
    chart_freqs = [r["cpu_freq_mhz_avg"] for r in chart_rows]
    data_js = json.dumps({
        "labels": labels,
        "temp": chart_temps,
        "cpu": chart_cpus,
        "freq": chart_freqs if all(f is not None for f in chart_freqs) else [None] * len(chart_rows),
    })

    # Hourly breakdown so a day-long run shows how temp tracks time of day
    # (e.g. warm afternoon room temp vs. overnight idle) rather than just an
    # overall min/max/mean.
    buckets = {}
    for r in rows:
        try:
            hour_key = datetime.fromisoformat(r["timestamp"]).strftime(
                "%Y-%m-%d %H:00" if multi_day else "%H:00"
            )
        except Exception:
            continue
        b = buckets.setdefault(hour_key, {"temps": [], "cpus": [], "throttled": 0, "n": 0})
        b["temps"].append(r["cpu_temp_c"])
        b["cpus"].append(r["cpu_percent_total"])
        b["throttled"] += 1 if r.get("throttled_now") == "True" else 0
        b["n"] += 1

    hourly_rows_html = "".join(
        f"<tr><td>{hour}</td><td>{min(b['temps']):.1f}</td><td>{st.mean(b['temps']):.1f}</td>"
        f"<td>{max(b['temps']):.1f}</td><td>{st.mean(b['cpus']):.1f}</td>"
        f"<td>{round(100 * b['throttled'] / b['n'], 1)}%</td></tr>"
        for hour, b in sorted(buckets.items())
    )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Thermal report</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background:#0b0d10; color:#e6e6e6; }}
h1 {{ font-size: 1.3rem; }}
.grid {{ display:grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap:0.75rem; margin: 1rem 0 2rem; }}
.card {{ background:#161a1f; border-radius:10px; padding:0.9rem 1rem; }}
.card .v {{ font-size:1.4rem; font-weight:600; }}
.card .l {{ font-size:0.75rem; color:#9aa4af; text-transform:uppercase; letter-spacing:.04em; }}
.verdict {{ background:#22252b; border-left:4px solid #e0a800; padding:0.8rem 1rem; margin-bottom:1.5rem; border-radius:6px; }}
.verdict li {{ margin:0.3rem 0; }}
canvas {{ background:#12151a; border-radius:10px; }}
</style></head>
<body>
<h1>Raspberry Pi thermal report ({n} samples)</h1>
<div class="verdict"><strong>Assessment</strong><ul>{''.join(f'<li>{v}</li>' for v in verdict)}</ul></div>
<div class="grid">
<div class="card"><div class="v">{summary['temp_min_c']} - {summary['temp_max_c']}&deg;C</div><div class="l">Temp range (mean {summary['temp_mean_c']})</div></div>
<div class="card"><div class="v">{summary['cpu_min_pct']} - {summary['cpu_max_pct']}%</div><div class="l">CPU range (mean {summary['cpu_mean_pct']})</div></div>
<div class="card"><div class="v">{summary['correlation_cpu_vs_temp']}</div><div class="l">Correlation CPU% vs Temp</div></div>
<div class="card"><div class="v">{summary['pct_time_soft_temp_limit']}%</div><div class="l">Time in soft temp limit</div></div>
<div class="card"><div class="v">{summary['pct_time_hard_throttled']}%</div><div class="l">Time hard throttled</div></div>
<div class="card"><div class="v">{summary['freq_min_mhz']} - {summary['freq_max_mhz']} MHz</div><div class="l">CPU freq range</div></div>
</div>
<canvas id="chart" width="1100" height="380"></canvas>
<script>
const d = {data_js};
const c = document.getElementById('chart');
const ctx = c.getContext('2d');
const W = c.width, H = c.height, pad = 40;
const n = d.labels.length;
function xAt(i) {{ return pad + (i/(n-1)) * (W - 2*pad); }}
function drawSeries(vals, min, max, color) {{
  ctx.beginPath();
  vals.forEach((v,i) => {{
    if (v === null || v === undefined) return;
    const y = H - pad - ((v - min)/(max - min || 1)) * (H - 2*pad);
    const x = xAt(i);
    if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  }});
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
}}
ctx.strokeStyle = '#333'; ctx.strokeRect(pad,pad, W-2*pad, H-2*pad);
const tMin = Math.min(...d.temp), tMax = Math.max(...d.temp);
drawSeries(d.temp, tMin, tMax, '#ff6b6b');
drawSeries(d.cpu, 0, 100, '#4dabf7');
ctx.fillStyle = '#ff6b6b'; ctx.fillText('Temp (C): ' + tMin.toFixed(1) + '-' + tMax.toFixed(1), pad, 20);
ctx.fillStyle = '#4dabf7'; ctx.fillText('CPU %', pad + 220, 20);
// Sparse x-axis ticks: labeling every sample is unreadable once a run spans
// hours/days, so only draw a handful evenly spaced across the chart width.
const tickCount = Math.min(10, n);
ctx.fillStyle = '#9aa4af';
ctx.font = '11px system-ui, sans-serif';
for (let k = 0; k < tickCount; k++) {{
  const i = Math.round(k * (n - 1) / (tickCount - 1 || 1));
  const x = xAt(i);
  ctx.beginPath();
  ctx.strokeStyle = '#333';
  ctx.moveTo(x, H - pad);
  ctx.lineTo(x, H - pad + 5);
  ctx.stroke();
  ctx.fillText(d.labels[i], Math.min(x - 20, W - pad - 60), H - pad + 18);
}}
</script>
<p style="color:#9aa4af;font-size:0.85rem">Red = CPU temperature (scaled to its own min/max). Blue = CPU utilization % (0-100 scale). Chart downsampled to {len(chart_rows)} of {n} samples for legibility; stats above use all {n}. Source: {source_label}.</p>
<h2 style="font-size:1.05rem;margin-top:2rem">Hourly breakdown</h2>
<table style="border-collapse:collapse;width:100%;font-size:0.85rem">
<thead><tr style="text-align:left;color:#9aa4af"><th style="padding:4px 10px">Hour</th><th style="padding:4px 10px">Min &deg;C</th><th style="padding:4px 10px">Mean &deg;C</th><th style="padding:4px 10px">Max &deg;C</th><th style="padding:4px 10px">Mean CPU%</th><th style="padding:4px 10px">Time throttled</th></tr></thead>
<tbody>{hourly_rows_html}</tbody>
</table>
</body></html>
"""
    return html, summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="stats.csv")
    ap.add_argument("--out", default="report.html")
    args = ap.parse_args()

    rows = load_rows(args.csv)
    if not rows:
        print("No usable rows found.")
        return

    html, summary = build_report_html(rows, args.csv)
    print(json.dumps(summary, indent=2))

    with open(args.out, "w") as f:
        f.write(html)
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
