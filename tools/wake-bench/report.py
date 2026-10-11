#!/usr/bin/env python3
"""Summarise results.json from bench.py: how often each engine hears the wake
word in each group of captures (throwaway comparison)."""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Fired captures of 6-8 Oct whose session carried a coherent command.
REAL_FIRED = ("20261006-095747", "20261007-082453", "20261007-082856")


def group_of(path: str) -> str:
    name = os.path.basename(path)
    if "/wake-verifier/positive/" in path:
        return "real"
    if "/wake-verifier/negative/" in path:
        return "false"
    if "/wake-verifier/" in path:
        return "skip"
    if "-wake-" in name:
        return "real" if name.startswith(REAL_FIRED) else "fired?"
    # A near capture that reached the 0.10 threshold was stopped by the verifier.
    # (Taken from the file name: the capture store prunes its oldest files.)
    return "blocked" if int(name[:-4].rsplit("-", 1)[1]) >= 100 else "below"


def detectors() -> dict:
    """name -> function(result for one capture) -> heard the wake word."""
    found = {}
    for channel in (0, 1):
        for threshold in (0.05, 0.10, 0.20, 0.30, 0.50):
            found[f"oww ch{channel} >={threshold}"] = (
                lambda r, c=channel, t=threshold: r["oww"][c]["peak"] >= t)
        for sensitivity in ("0.3", "0.5", "0.7", "0.9", "1.0"):
            found[f"porcupine ch{channel} s={sensitivity}"] = (
                lambda r, c=channel, s=sensitivity: r["porcupine"][c]["hits"][s] > 0)
        for threshold in (0.5, 0.8, 0.9, 0.97):
            found[f"micro ch{channel} >={threshold}"] = (
                lambda r, c=channel, t=threshold: r["micro"][c]["peak"] >= t)
        for setting in ("1.0/0.25", "1.5/0.15", "2.0/0.1", "3.0/0.05"):
            found[f"sherpa ch{channel} {setting}"] = (
                lambda r, c=channel, s=setting: bool(r["sherpa"][c]["hits"][s]))
        for threshold in (0.5, 0.9, 1.0):
            found[f"vosk ch{channel} conf>={threshold}"] = (
                lambda r, c=channel, t=threshold: any(x >= t for x in r["vosk"][c]["jarvis_conf"]))
    return found


def main() -> int:
    with open(os.path.join(HERE, "results.json")) as handle:
        results = json.load(handle)
    groups: dict[str, list[dict]] = {}
    for path, result in results.items():
        if all(k in result for k in ("oww", "porcupine", "micro", "sherpa", "vosk")):
            groups.setdefault(group_of(path), []).append(result)
    order = [g for g in ("real", "false", "blocked", "below", "fired?") if g in groups]
    print("captures: " + ", ".join(f"{g}={len(groups[g])}" for g in order))
    print(f"{'detector':30s}" + "".join(f"{g:>10s}" for g in order))
    checks = detectors()
    for name, heard in checks.items():
        cells = ""
        for g in order:
            count = sum(1 for r in groups[g] if heard(r))
            cells += f"{count:5d}/{len(groups[g]):<4d}"
        print(f"{name:30s}{cells}")
    for extra in sys.argv[1:]:  # e.g. "oww ch1 >=0.1 & porcupine ch0 s=0.7"
        parts = [p.strip() for p in extra.split("&")]
        cells = ""
        for g in order:
            count = sum(1 for r in groups[g] if all(checks[p](r) for p in parts))
            cells += f"{count:5d}/{len(groups[g]):<4d}"
        print(f"{'AND: ' + extra:30s}\n{'':30s}{cells}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
