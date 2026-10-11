#!/usr/bin/env python3
"""Localise where the far-end reference dies inside the XVF3800.

aec-refcheck.py established that the AEC's far-end tap (categories 4/5) reads
the same as the silence control while PipeWire delivers program audio to the
device's USB playback endpoint at -21 dBFS. This walks the reference further
upstream to find the break:

  cat 10 = far end at native rate ("data passed from I2S logical core to Audio
           Manager logical core") -- furthest upstream tap available
  cat  4 = far end post sample-rate conversion to 16kHz
  cat  5 = ...plus system delay
  cat 12 = ...plus fixed gain; "the reference signal passed to the SHF cores"

If cat 10 is live and cat 4 is silent, the break is inside the chip's audio
manager. If cat 10 is also silent, USB playback never enters the reference path
at all in this firmware configuration.

Also A/Bs I2S_DAC_DSP_ENABLE, whose vendor description ("use the I2S signal as
input to the audio pipeline, even in the UA configuration") is the one
documented switch that plausibly changes this routing.
"""
from __future__ import annotations

import atexit, importlib.util, os, signal, sys, time

_spec = importlib.util.spec_from_file_location(
    "sweep", os.path.join(os.path.dirname(os.path.abspath(__file__)), "aec-delay-sweep.py"))
sweep = importlib.util.module_from_spec(_spec)
sys.modules["sweep"] = sweep
_spec.loader.exec_module(sweep)

calib, sh, sysctl, unit_active, register = (
    sweep.calib, sweep.sh, sweep.sysctl, sweep.unit_active, sweep.register)

CASES = [
    ("0 0",  "silence (control)"),
    ("3 0",  "mic 0 (control)"),
    ("10 0", "far end at native rate, src 0"),
    ("10 1", "far end at native rate, src 1"),
    ("4 0",  "far end, post SRC to 16k"),
    ("5 0",  "far end + system delay"),
    ("12 0", "amplified far end -> SHF cores"),
]


def set_reg(name, value):
    code, out = sh([sweep.XVF_PY, sweep.XVF_REG, "set", name, *str(value).split()])
    if code != 0:
        raise RuntimeError(f"set {name} {value} failed: {out}")


def main():
    orig_opl = register("AUDIO_MGR_OP_L")
    orig_i2s = register("I2S_DAC_DSP_ENABLE")
    print(f"[state] AUDIO_MGR_OP_L={orig_opl} I2S_DAC_DSP_ENABLE={orig_i2s} "
          f"I2S_INACTIVE={register('I2S_INACTIVE')}")
    stopped = []
    was_playing = sh(["playerctl", "status"])[1] == "Playing"
    stim = sweep.Stimulus()

    def restore():
        print("\n[restore] putting everything back...")
        stim.stop()
        for name, val in (("AUDIO_MGR_OP_L", orig_opl or "8 0"),
                          ("I2S_DAC_DSP_ENABLE", orig_i2s or "0")):
            try:
                set_reg(name, val)
            except Exception as e:
                print(f"[restore] WARNING {name}: {e}")
        for u in stopped:
            sysctl("start", u)
        if was_playing:
            sh(["playerctl", "play"])
        print(f"[restore] AUDIO_MGR_OP_L={register('AUDIO_MGR_OP_L')} "
              f"I2S_DAC_DSP_ENABLE={register('I2S_DAC_DSP_ENABLE')}")

    atexit.register(restore)

    for u in ("wake-word.service", "voice-agent.service"):
        if unit_active(u):
            sysctl("stop", u, check=True)
            stopped.append(u)
    if was_playing:
        sh(["playerctl", "pause"])
        time.sleep(1.5)

    stim.start()
    time.sleep(3)
    try:
        with calib.Listener([0, 1]) as listener:
            listener.measure(1.0)
            for i2s_val in ("0", "1"):
                set_reg("I2S_DAC_DSP_ENABLE", i2s_val)
                time.sleep(2)
                print(f"\n=== I2S_DAC_DSP_ENABLE={i2s_val} "
                      f"(readback {register('I2S_DAC_DSP_ENABLE')}) ===")
                print(f"{'OP_L':>7}  {'ch0(L) dBFS':>12}  description")
                print("-" * 60)
                for value, desc in CASES:
                    set_reg("AUDIO_MGR_OP_L", value)
                    time.sleep(2.0)
                    listener.measure(0.8)     # flush buffered audio from prior mux
                    m = sweep.measure(listener, 3.0)
                    print(f"{value:>7}  {m[0]['rms_dbfs']:12.1f}  {desc}")
    finally:
        stim.stop()


if __name__ == "__main__":
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(1))
    sys.exit(main() or 0)
