#!/usr/bin/env python3
"""Sweep the XVF3800 AEC reference delay and measure echo residual.

Hypothesis under test: `pw-loopback --delay=0.341` in xvf-aec-ref.sh over-delays
the far-end reference by ~250ms (0.341 is the aloop *ring buffer size*, not the
tap->speaker latency, which measures ~90-110ms from ALSA pointers). A reference
that arrives after the echo cannot be cancelled by a causal adaptive filter,
which would explain AEC_AECCONVERGED=1 alongside useless cancellation depth.

Metric: RMS of the XVF3800's *processed* capture channels while a fixed pink
noise stimulus plays, referenced to the same channels' RMS in silence. Lower
excess-over-silence = deeper cancellation. openWakeWord peak score is recorded
alongside as the operational metric (the number that read 0.964 with music).

Restores every piece of state it touches on exit, including on Ctrl-C.
"""
from __future__ import annotations

import argparse, atexit, importlib.util, json, os, signal, subprocess, sys, time
import numpy as np

HOME = os.path.expanduser("~")
ENV = dict(os.environ)
ENV["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
ENV["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"

ALOOP_SINK = "alsa_output.platform-snd_aloop.0.analog-stereo"
XVF_PLAYBACK = ("alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array"
                "_114993700254500350-00.analog-stereo")
DROPIN_DIR = f"{HOME}/.config/systemd/user/xvf-aec-ref.service.d"
DROPIN = f"{DROPIN_DIR}/zz-sweep.conf"
STIMULUS = "/tmp/aec-sweep-pink.wav"
XVF_PY = f"{HOME}/.venvs/xvf3800/bin/python"
XVF_REG = f"{HOME}/.local/bin/xvf_register.py"

# Reuse the calibration tool's Listener so the openWakeWord numbers are
# methodologically identical to the 0.964 baseline it produced.
_spec = importlib.util.spec_from_file_location(
    "calib", f"{HOME}/.local/bin/ai-speaker-calibrate.py")
calib = importlib.util.module_from_spec(_spec)
sys.modules["calib"] = calib  # dataclasses resolves annotations via sys.modules
_spec.loader.exec_module(calib)


def sh(argv, timeout=30, check=False):
    p = subprocess.run(argv, capture_output=True, text=True, env=ENV, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{argv} failed: {p.stderr.strip()}")
    return p.returncode, p.stdout.strip()


def sysctl(*args, **kw):
    return sh(["systemctl", "--user", *args], **kw)


def unit_active(u):
    return sysctl("is-active", u)[1] == "active"


def register(name):
    code, out = sh([XVF_PY, XVF_REG, "get", name])
    return out if code == 0 else None


def temp_c():
    _, out = sh(["vcgencmd", "measure_temp"])
    try:
        return float(out.split("=")[1].split("'")[0])
    except Exception:
        return float("nan")


def sink_volume():
    _, out = sh(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
    try:
        return float(out.split()[1])
    except Exception:
        return float("nan")


def ref_link_ok():
    _, out = sh(["pw-link", "-l"])
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == "output.xvf-aec-ref:output_FL":
            for nxt in lines[i + 1:]:
                if not nxt.startswith(" "):
                    break
                if XVF_PLAYBACK in nxt and "|->" in nxt:
                    return True
    return False


def wait_for_ref_link(timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ref_link_ok():
            return True
        time.sleep(1)
    return False


def set_delay(delay):
    """Restart the AEC reference feed with a given --delay, via a drop-in."""
    os.makedirs(DROPIN_DIR, exist_ok=True)
    with open(DROPIN, "w") as f:
        f.write("[Service]\n"
                f"Environment=XVF_AEC_REF_DELAY={delay}\n")
    sysctl("daemon-reload", check=True)
    sysctl("restart", "xvf-aec-ref.service", check=True)
    if not wait_for_ref_link():
        raise RuntimeError(f"AEC reference link did not come up for delay={delay}")


class Stimulus:
    """Loop the pink-noise file into the aloop sink (the real speaker path)."""
    def __init__(self):
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            ["pw-play", "--target", ALOOP_SINK, STIMULUS],
            env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None


def measure(listener, seconds):
    """Per-channel RMS (dBFS) and peak openWakeWord score over `seconds`."""
    frames = max(1, int(seconds / calib.FRAME_SECONDS))
    peaks = {c: 0.0 for c in listener.channels}
    sumsq = {c: 0.0 for c in listener.channels}
    n = {c: 0 for c in listener.channels}
    for _ in range(frames):
        stereo = listener._read_frame()
        if stereo is None:
            raise RuntimeError("capture stream ended unexpectedly")
        for c in listener.channels:
            x = np.ascontiguousarray(stereo[:, c])
            listener.models[c].predict(x)
            peaks[c] = max(peaks[c], float(
                listener.models[c].prediction_buffer[listener.model_key][-1]))
            xf = x.astype(np.float64) / 32768.0
            sumsq[c] += float(np.sum(xf * xf))
            n[c] += xf.size
    out = {}
    for c in listener.channels:
        rms = (sumsq[c] / max(n[c], 1)) ** 0.5
        out[c] = {"rms_dbfs": 20 * np.log10(max(rms, 1e-9)), "oww_peak": peaks[c]}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delays", default="0.341,0.15,0.10,0.05,0.0,0.341",
                    help="sweep points; repeat the first one last as a repeatability check")
    ap.add_argument("--settle", type=float, default=20.0, help="AEC convergence time per point")
    ap.add_argument("--measure", type=float, default=15.0, help="measurement window per point")
    ap.add_argument("--channels", default="0,1")
    ap.add_argument("--out", default=f"{HOME}/.local/state/ai-speaker/measurements/aec-sweep-results.json")
    args = ap.parse_args()

    delays = [d.strip() for d in args.delays.split(",") if d.strip()]
    channels = [int(c) for c in args.channels.split(",")]

    # ---- save state -------------------------------------------------------
    stopped = []
    was_playing = sh(["playerctl", "status"])[1] == "Playing"
    vol0 = sink_volume()
    print(f"[state] playerctl={'Playing' if was_playing else 'not playing'} "
          f"sink_vol={vol0:.2f} temp={temp_c():.1f}C")

    stim = Stimulus()

    def restore():
        print("\n[restore] putting everything back...")
        stim.stop()
        for f in (DROPIN,):
            if os.path.exists(f):
                os.remove(f)
        try:
            os.rmdir(DROPIN_DIR)
        except OSError:
            pass
        sysctl("daemon-reload")
        sysctl("restart", "xvf-aec-ref.service")
        wait_for_ref_link()
        for u in stopped:
            sysctl("start", u)
        if was_playing:
            sh(["playerctl", "play"])
        print(f"[restore] ref_link_ok={ref_link_ok()} "
              f"far_end_dsp={register('AUDIO_MGR_FAR_END_DSP_ENABLE')} "
              f"sink_vol={sink_volume():.2f} temp={temp_c():.1f}C")
        for u in stopped:
            print(f"[restore] {u} active={unit_active(u)}")

    atexit.register(restore)

    # ---- quiesce ----------------------------------------------------------
    # wake-word would false-trigger on the stimulus (its music noise floor is
    # 0.964 vs a 0.25 threshold) and voice-agent would open a Gemini session
    # and duck the audio mid-measurement.
    for u in ("wake-word.service", "voice-agent.service"):
        if unit_active(u):
            sysctl("stop", u, check=True)
            stopped.append(u)
            print(f"[quiesce] stopped {u}")
    if was_playing:
        sh(["playerctl", "pause"])
        time.sleep(1.5)
        print("[quiesce] paused music")

    # ---- stimulus ---------------------------------------------------------
    if not os.path.exists(STIMULUS):
        sh(["ffmpeg", "-y", "-f", "lavfi", "-i",
            "anoisesrc=color=pink:amplitude=0.5:duration=90:sample_rate=48000",
            "-af", "pan=stereo|c0=c0|c1=c0", "-c:a", "pcm_s16le", STIMULUS],
           timeout=120, check=True)
    import wave
    with wave.open(STIMULUS) as w:
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    stim_rms = 20 * np.log10(max(float(np.sqrt(np.mean((raw / 32768.0) ** 2))), 1e-9))
    print(f"[stimulus] pink noise, {stim_rms:.1f} dBFS RMS")

    results = []
    with calib.Listener(channels) as listener:
        listener.measure(2.0)  # prime the streaming models

        print("\n[baseline] silence (no playback)")
        silence = measure(listener, args.measure)
        for c in channels:
            print(f"  ch{c}: rms={silence[c]['rms_dbfs']:6.1f} dBFS  "
                  f"oww_peak={silence[c]['oww_peak']:.3f}")

        for idx, d in enumerate(delays):
            print(f"\n[{idx+1}/{len(delays)}] --delay={d}")
            set_delay(d)
            fed = register("AUDIO_MGR_FAR_END_DSP_ENABLE")
            stim.start()
            time.sleep(args.settle)
            conv = register("AEC_AECCONVERGED")
            vol = sink_volume()
            m = measure(listener, args.measure)
            stim.stop()
            still_alive = True
            t = temp_c()
            for c in channels:
                excess = m[c]["rms_dbfs"] - silence[c]["rms_dbfs"]
                print(f"  ch{c}: rms={m[c]['rms_dbfs']:6.1f} dBFS  "
                      f"excess_over_silence={excess:+5.1f} dB  "
                      f"oww_peak={m[c]['oww_peak']:.3f}")
            print(f"  far_end_dsp={fed} converged={conv} vol={vol:.2f} temp={t:.1f}C")
            results.append({
                "delay": float(d), "order": idx, "far_end_dsp": fed,
                "converged": conv, "sink_volume": vol, "temp_c": t,
                "channels": {str(c): {
                    **m[c],
                    "excess_over_silence_db": m[c]["rms_dbfs"] - silence[c]["rms_dbfs"],
                } for c in channels},
            })
            time.sleep(2)

    payload = {
        "stimulus_rms_dbfs": stim_rms,
        "settle_s": args.settle, "measure_s": args.measure,
        "silence": {str(c): silence[c] for c in channels},
        "points": results,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\n[done] results -> {args.out}")


if __name__ == "__main__":
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(1))
    sys.exit(main() or 0)
