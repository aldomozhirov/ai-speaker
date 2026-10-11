#!/usr/bin/env python3
"""Logs CPU/thermal stats to CSV for cooling-requirement analysis.

Usage:
    python3 collector.py [--interval SECONDS] [--out FILE] [--duration SECONDS]

Runs until interrupted (Ctrl-C) or --duration elapses. Safe to run in the
background with nohup:
    nohup python3 collector.py --out stats.csv > collector.log 2>&1 &
"""
import argparse
import csv
import os
import subprocess
import time

import psutil

FIELDS = [
    "timestamp",
    "cpu_temp_c",
    "rp1_temp_c",
    "cpu_percent_total",
    "cpu_percent_per_core",
    "cpu_freq_mhz_avg",
    "load1",
    "load5",
    "load15",
    "throttled_hex",
    "under_voltage_now",
    "freq_capped_now",
    "throttled_now",
    "soft_temp_limit_now",
]


def read_temps():
    temps = psutil.sensors_temperatures()
    cpu = temps.get("cpu_thermal", [{}])[0].current if temps.get("cpu_thermal") else None
    rp1 = temps.get("rp1_adc", [{}])[0].current if temps.get("rp1_adc") else None
    return cpu, rp1


def read_throttled():
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2
        ).stdout.strip()
        val = int(out.split("=")[1], 16)
    except Exception:
        return None, None, None, None, None
    return (
        hex(val),
        bool(val & 0x1),  # under-voltage now
        bool(val & 0x2),  # arm freq capped now
        bool(val & 0x4),  # currently throttled
        bool(val & 0x8),  # soft temp limit now
    )


def sample():
    cpu_temp, rp1_temp = read_temps()
    per_core = psutil.cpu_percent(percpu=True)
    total = sum(per_core) / len(per_core)
    freqs = [f.current for f in psutil.cpu_freq(percpu=True)] or []
    freq_avg = sum(freqs) / len(freqs) if freqs else None
    load1, load5, load15 = os.getloadavg()
    hexval, uv, fc, th, stl = read_throttled()
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cpu_temp_c": cpu_temp,
        "rp1_temp_c": rp1_temp,
        "cpu_percent_total": round(total, 1),
        "cpu_percent_per_core": ";".join(f"{p:.1f}" for p in per_core),
        "cpu_freq_mhz_avg": round(freq_avg) if freq_avg else None,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "throttled_hex": hexval,
        "under_voltage_now": uv,
        "freq_capped_now": fc,
        "throttled_now": th,
        "soft_temp_limit_now": stl,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--out", default="stats.csv")
    ap.add_argument("--duration", type=float, default=None, help="seconds; default = run until Ctrl-C")
    args = ap.parse_args()

    new_file = not os.path.exists(args.out)
    psutil.cpu_percent(percpu=True)  # prime the counter
    start = time.time()

    with open(args.out, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
            f.flush()
        print(f"Logging every {args.interval}s to {args.out} (Ctrl-C to stop)")
        try:
            while args.duration is None or (time.time() - start) < args.duration:
                time.sleep(args.interval)
                row = sample()
                writer.writerow(row)
                f.flush()
                print(
                    f"{row['timestamp']} temp={row['cpu_temp_c']}C "
                    f"cpu={row['cpu_percent_total']}% freq={row['cpu_freq_mhz_avg']}MHz "
                    f"throttled={row['throttled_hex']}"
                )
        except KeyboardInterrupt:
            print("Stopped.")


if __name__ == "__main__":
    main()
