#!/usr/bin/env python3
"""Is the XVF3800's AEC actually receiving the far-end reference?

Both the delay sweep and the gain sweep found the echo residual completely
insensitive to the reference (identical to 3 s.f. across 0-341ms of alignment
and across 43dB of reference gain), while register writes demonstrably land and
read back. That combination says the far-end reference never reaches the AEC.

This routes the XVF3800's debug mux (AUDIO_MGR_OP_L) to each signal category in
turn while a known pink-noise stimulus plays, and reports the captured RMS:

  category 0 (silence)      -- control: proves the mux switches at all
  category 3 (mic 0)        -- control: proves the stimulus is audible to a mic
  category 4 (far-end, pre-delay)  -- the question
  category 5 (far-end, post-delay) -- the question

Session 2 of the investigation stalled here because it went straight to
cross-correlation without the silence control, and couldn't tell "mux not
switching" from "no signal present". This checks the controls first.

Restores AUDIO_MGR_OP_L on exit.
"""
from __future__ import annotations

import atexit, importlib.util, os, signal, sys, time
import numpy as np

_spec = importlib.util.spec_from_file_location(
    "sweep", os.path.join(os.path.dirname(os.path.abspath(__file__)), "aec-delay-sweep.py"))
sweep = importlib.util.module_from_spec(_spec)
sys.modules["sweep"] = sweep
_spec.loader.exec_module(sweep)

calib, sh, sysctl, unit_active = sweep.calib, sweep.sh, sweep.sysctl, sweep.unit_active
register = sweep.register

CASES = [
    ("0 0", "silence (control: mux switches?)"),
    ("3 0", "mic 0 (control: stimulus audible?)"),
    ("4 0", "far-end reference, pre system delay"),
    ("5 0", "far-end reference, post system delay"),
    ("3 0", "mic 0 again (repeatability)"),
]


def set_op_l(value):
    code, out = sh([sweep.XVF_PY, sweep.XVF_REG, "set", "AUDIO_MGR_OP_L", *value.split()])
    if code != 0:
        raise RuntimeError(f"set AUDIO_MGR_OP_L {value} failed: {out}")


def main():
    orig = register("AUDIO_MGR_OP_L")
    print(f"[state] AUDIO_MGR_OP_L={orig} AUDIO_MGR_OP_R={register('AUDIO_MGR_OP_R')}")
    stopped = []
    was_playing = sh(["playerctl", "status"])[1] == "Playing"
    stim = sweep.Stimulus()

    def restore():
        print("\n[restore] putting everything back...")
        stim.stop()
        try:
            set_op_l(orig if orig else "8 0")
        except Exception as e:
            print(f"[restore] WARNING could not restore OP_L: {e}")
        for u in stopped:
            sysctl("start", u)
        if was_playing:
            sh(["playerctl", "play"])
        print(f"[restore] AUDIO_MGR_OP_L={register('AUDIO_MGR_OP_L')} "
              f"AUDIO_MGR_OP_R={register('AUDIO_MGR_OP_R')}")
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

    print("\nStimulus playing throughout. Left channel = the mux under test.\n")
    print(f"{'AUDIO_MGR_OP_L':>16}  {'ch0(L) dBFS':>12}  {'ch1(R) dBFS':>12}  description")
    print("-" * 78)

    stim.start()
    time.sleep(3)
    try:
        with calib.Listener([0, 1]) as listener:
            listener.measure(1.0)
            for value, desc in CASES:
                set_op_l(value)
                time.sleep(2.5)          # settle, and flush stale buffered audio
                listener.measure(1.0)
                m = sweep.measure(listener, 4.0)
                print(f"{value:>16}  {m[0]['rms_dbfs']:12.1f}  {m[1]['rms_dbfs']:12.1f}  {desc}")
    finally:
        stim.stop()


if __name__ == "__main__":
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(1))
    sys.exit(main() or 0)
