#!/usr/bin/env python3
"""Sweep AEC_FAR_EXTGAIN (far-end reference gain, dB) and measure echo residual.

Follows the delay sweep, which found the residual completely insensitive to
reference alignment (identical to 3 s.f. from 0ms to 341ms). Reading the full
XVF3800 register map turned up AEC_FAR_EXTGAIN = -43.0 dB -- the far-end
reference is attenuated ~140x before reaching the adaptive filter, which would
make alignment irrelevant because there is effectively no reference to align.
AEC_RT60 reading ~0 (denormal, valid range 0.25-0.9s) corroborates.

Same methodology/metric as aec-delay-sweep.py so numbers are comparable.
Restores the register (RAM-only, but not left to chance) on exit.
"""
from __future__ import annotations

import argparse, atexit, importlib.util, json, os, signal, sys, time

_spec = importlib.util.spec_from_file_location(
    "sweep", os.path.join(os.path.dirname(os.path.abspath(__file__)), "aec-delay-sweep.py"))
sweep = importlib.util.module_from_spec(_spec)
sys.modules["sweep"] = sweep
_spec.loader.exec_module(sweep)

calib, sh, sysctl, unit_active = sweep.calib, sweep.sh, sweep.sysctl, sweep.unit_active
register, temp_c, sink_volume = sweep.register, sweep.temp_c, sweep.sink_volume
XVF_PY, XVF_REG = sweep.XVF_PY, sweep.XVF_REG


def set_register(name, value):
    code, out = sh([XVF_PY, XVF_REG, "set", name, str(value)])
    if code != 0:
        raise RuntimeError(f"failed to set {name}={value}: {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gains", default="-43.0,-30.0,-20.0,-10.0,0.0,-43.0")
    ap.add_argument("--settle", type=float, default=15.0)
    ap.add_argument("--measure", type=float, default=12.0)
    ap.add_argument("--channels", default="0,1")
    ap.add_argument("--out", default=os.path.expanduser("~/.local/state/ai-speaker/measurements/aec-gain-results.json"))
    args = ap.parse_args()

    gains = [g.strip() for g in args.gains.split(",") if g.strip()]
    channels = [int(c) for c in args.channels.split(",")]

    orig_gain = register("AEC_FAR_EXTGAIN")
    stopped = []
    was_playing = sh(["playerctl", "status"])[1] == "Playing"
    print(f"[state] AEC_FAR_EXTGAIN={orig_gain} sink_vol={sink_volume():.2f} temp={temp_c():.1f}C")

    stim = sweep.Stimulus()

    def restore():
        print("\n[restore] putting everything back...")
        stim.stop()
        try:
            set_register("AEC_FAR_EXTGAIN", orig_gain)
        except Exception as e:
            print(f"[restore] WARNING could not restore gain: {e}")
        for u in stopped:
            sysctl("start", u)
        if was_playing:
            sh(["playerctl", "play"])
        print(f"[restore] AEC_FAR_EXTGAIN={register('AEC_FAR_EXTGAIN')} "
              f"far_end_dsp={register('AUDIO_MGR_FAR_END_DSP_ENABLE')} "
              f"ref_link_ok={sweep.ref_link_ok()} temp={temp_c():.1f}C")
        for u in stopped:
            print(f"[restore] {u} active={unit_active(u)}")

    atexit.register(restore)

    for u in ("wake-word.service", "voice-agent.service"):
        if unit_active(u):
            sysctl("stop", u, check=True)
            stopped.append(u)
            print(f"[quiesce] stopped {u}")
    if was_playing:
        sh(["playerctl", "pause"])
        time.sleep(1.5)
        print("[quiesce] paused music")

    if not os.path.exists(sweep.STIMULUS):
        raise SystemExit("stimulus missing; run aec-delay-sweep.py first")

    results = []
    with calib.Listener(channels) as listener:
        listener.measure(2.0)

        print("\n[baseline] silence (no playback)")
        silence = sweep.measure(listener, args.measure)
        for c in channels:
            print(f"  ch{c}: rms={silence[c]['rms_dbfs']:6.1f} dBFS")

        for idx, g in enumerate(gains):
            print(f"\n[{idx+1}/{len(gains)}] AEC_FAR_EXTGAIN={g} dB")
            set_register("AEC_FAR_EXTGAIN", g)
            stim.start()
            time.sleep(args.settle)
            conv, rt60 = register("AEC_AECCONVERGED"), register("AEC_RT60")
            m = sweep.measure(listener, args.measure)
            stim.stop()
            for c in channels:
                excess = m[c]["rms_dbfs"] - silence[c]["rms_dbfs"]
                print(f"  ch{c}: rms={m[c]['rms_dbfs']:6.1f} dBFS  "
                      f"excess_over_silence={excess:+5.1f} dB  "
                      f"oww_peak={m[c]['oww_peak']:.3f}")
            print(f"  converged={conv} rt60={rt60} temp={temp_c():.1f}C")
            results.append({
                "far_extgain_db": float(g), "order": idx, "converged": conv,
                "rt60": rt60, "temp_c": temp_c(),
                "channels": {str(c): {
                    **m[c],
                    "excess_over_silence_db": m[c]["rms_dbfs"] - silence[c]["rms_dbfs"],
                } for c in channels},
            })
            time.sleep(2)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"silence": {str(c): silence[c] for c in channels},
                   "points": results}, f, indent=2)
    print(f"\n[done] results -> {args.out}")


if __name__ == "__main__":
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(1))
    sys.exit(main() or 0)
