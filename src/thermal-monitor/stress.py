#!/usr/bin/env python3
"""Simple stdlib CPU load generator (no stress-ng needed).

Runs busy-loop worker processes for a fixed duration so you can watch the
temperature response to a known load, ideally while collector.py is running
in another terminal.

Usage:
    python3 stress.py --cores 4 --duration 120
"""
import argparse
import multiprocessing
import time


def burn(duration):
    end = time.time() + duration
    x = 0.0001
    while time.time() < end:
        for _ in range(200000):
            x = x * 1.0000001 + 1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cores", type=int, default=multiprocessing.cpu_count())
    ap.add_argument("--duration", type=float, default=120.0)
    args = ap.parse_args()

    print(f"Burning {args.cores} core(s) for {args.duration}s...")
    procs = [multiprocessing.Process(target=burn, args=(args.duration,)) for _ in range(args.cores)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    print("Done.")


if __name__ == "__main__":
    main()
