#!/usr/bin/env python3
"""Measure real AEC cancellation depth (ERLE) vs reference delay.

The earlier sweeps used the RMS of the XVF3800's *processed* output channels as
a residual proxy. That was wrong: channel 1 is category 7/3 (ASR output), which
has AGC on it (PP_AGCMAXGAIN=64), so its level is pinned to the AGC target
regardless of how much echo the AEC removes -- which is exactly why it read an
identical +28.x dB in every condition tested.

This measures the AEC directly instead, using the debug mux to capture both
sides of it simultaneously in one stereo stream:

  L = AUDIO_MGR_OP_L 3 0  -- amplified mic 0 with system delay (SHF input)
  R = AUDIO_MGR_OP_R 7 0  -- AEC residual for mic 0 (SHF output, pre-postproc)

  ERLE = RMS(L) - RMS(R), in dB. Higher = more echo removed.

Capturing both in one stream removes ordering/level-drift confounds entirely.
"""
from __future__ import annotations

import argparse, atexit, importlib.util, json, os, signal, sys, time
import numpy as np

_spec = importlib.util.spec_from_file_location(
    "sweep", os.path.join(os.path.dirname(os.path.abspath(__file__)), "aec-delay-sweep.py"))
sweep = importlib.util.module_from_spec(_spec)
sys.modules["sweep"] = sweep
_spec.loader.exec_module(sweep)

calib, sh, sysctl, unit_active, register = (
    sweep.calib, sweep.sh, sweep.sysctl, sweep.unit_active, sweep.register)


def set_reg(name, value):
    code, out = sh([sweep.XVF_PY, sweep.XVF_REG, "set", name, *str(value).split()])
    if code != 0:
        raise RuntimeError(f"set {name} {value} failed: {out}")


def rms_pair(listener, seconds):
    m = sweep.measure(listener, seconds)
    return m[0]["rms_dbfs"], m[1]["rms_dbfs"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delays", default="0.341,0.20,0.15,0.10,0.05,0.0,0.341")
    ap.add_argument("--settle", type=float, default=15.0)
    ap.add_argument("--measure", type=float, default=10.0)
    ap.add_argument("--out", default=os.path.expanduser("~/.local/state/ai-speaker/measurements/aec-erle-results.json"))
    args = ap.parse_args()
    delays = [d.strip() for d in args.delays.split(",") if d.strip()]

    orig_l, orig_r = register("AUDIO_MGR_OP_L"), register("AUDIO_MGR_OP_R")
    stopped = []
    was_playing = sh(["playerctl", "status"])[1] == "Playing"
    stim = sweep.Stimulus()
    print(f"[state] OP_L={orig_l} OP_R={orig_r} "
          f"far_extgain={register('AEC_FAR_EXTGAIN')} temp={sweep.temp_c():.1f}C")

    def restore():
        print("\n[restore] putting everything back...")
        stim.stop()
        for f in (sweep.DROPIN,):
            if os.path.exists(f):
                os.remove(f)
        try:
            os.rmdir(sweep.DROPIN_DIR)
        except OSError:
            pass
        sysctl("daemon-reload")
        for n, v in (("AUDIO_MGR_OP_L", orig_l or "8 0"), ("AUDIO_MGR_OP_R", orig_r or "7 3")):
            try:
                set_reg(n, v)
            except Exception as e:
                print(f"[restore] WARNING {n}: {e}")
        sysctl("restart", "xvf-aec-ref.service")
        sweep.wait_for_ref_link()
        for u in stopped:
            sysctl("start", u)
        if was_playing:
            sh(["playerctl", "play"])
        print(f"[restore] OP_L={register('AUDIO_MGR_OP_L')} OP_R={register('AUDIO_MGR_OP_R')} "
              f"ref_link_ok={sweep.ref_link_ok()}")

    atexit.register(restore)

    for u in ("wake-word.service", "voice-agent.service"):
        if unit_active(u):
            sysctl("stop", u, check=True)
            stopped.append(u)
    if was_playing:
        sh(["playerctl", "pause"])
        time.sleep(1.5)

    set_reg("AUDIO_MGR_OP_L", "3 0")   # mic 0 into SHF
    set_reg("AUDIO_MGR_OP_R", "7 0")   # AEC residual for mic 0
    time.sleep(2)

    results = []
    with calib.Listener([0, 1]) as listener:
        listener.measure(1.5)

        # sanity: with nothing playing, mic and residual should both sit low
        q_mic, q_res = rms_pair(listener, 4.0)
        print(f"\n[silence] mic={q_mic:.1f} dBFS  residual={q_res:.1f} dBFS "
              f"(ERLE meaningless here)")

        # confirm the reference actually reaches SHF in the live configuration
        set_reg("AUDIO_MGR_OP_L", "12 0")
        stim.start(); time.sleep(4)
        ref_lvl, _ = rms_pair(listener, 4.0)
        stim.stop(); time.sleep(1)
        set_reg("AUDIO_MGR_OP_L", "3 0")
        print(f"[reference] amplified far-end into SHF = {ref_lvl:.1f} dBFS "
              f"({'LIVE' if ref_lvl > q_mic + 6 else 'AT FLOOR'})")

        print(f"\n{'delay':>7}  {'mic dBFS':>9}  {'residual dBFS':>13}  {'ERLE dB':>8}  conv")
        print("-" * 52)
        for d in delays:
            sweep.set_delay(d)
            stim.start()
            time.sleep(args.settle)
            conv = register("AEC_AECCONVERGED")
            mic, res = rms_pair(listener, args.measure)
            stim.stop()
            erle = mic - res
            print(f"{d:>7}  {mic:9.1f}  {res:13.1f}  {erle:8.1f}  {conv}")
            results.append({"delay": float(d), "mic_dbfs": mic,
                            "residual_dbfs": res, "erle_db": erle,
                            "converged": conv, "temp_c": sweep.temp_c()})
            time.sleep(2)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"reference_level_dbfs": ref_lvl, "points": results}, f, indent=2)
    print(f"\n[done] -> {args.out}")


if __name__ == "__main__":
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(1))
    sys.exit(main() or 0)
