#!/usr/bin/env python3
"""Where does the AEC reference sit in time relative to the echo, and how much
echo does the XVF3800 actually remove?

Two questions the earlier harnesses (aec-delay-sweep.py, aec-erle-sweep.py)
could not answer:

1. Alignment. An echo canceller can only subtract what it has already been
   told about: the far-end reference has to reach the chip *before* the echo
   reaches the microphones. The reference here goes through
   `pw-loopback --delay=<XVF_AEC_REF_DELAY>` (0.341 s, never measured -- it is
   the snd-aloop buffer size), and the old sweep stepped that in 50 ms jumps
   while watching an output level that does not respond to cancellation.
   This captures both signals exactly as the canceller sees them --
       L = AUDIO_MGR_OP_L 3 0    amplified mic 0, with system delay
       R = AUDIO_MGR_OP_R 12 0   amplified reference, with system delay
   -- and cross-correlates them (GCC-PHAT). A positive lag means the echo
   arrives after the reference (good, as long as it is short); a negative lag
   means the reference is late and nothing can be cancelled.

2. ERLE. aec-erle-sweep.py read "7 0" as the AEC residual, but with
   AEC_ASROUTONOFF=1 (the firmware default) category 7 is the ASR beam
   output, not a residual. Half way through the noise this sets
   AEC_ASROUTONOFF=0 and switches R to "7 0", which is then the true per-mic
   residual, so ERLE = RMS(mic 0) - RMS(residual 0).

One run plays ~16 s of pink noise through the normal program path (the aloop
sink, so CamillaDSP, the amp and the reference tap all see it), stops
wake-word.service for the duration, and puts every register back afterwards.
Nothing is written to the chip's flash.

    ~/.venvs/wake-word/bin/python tools/aec/aec-align-check.py
    ~/.venvs/wake-word/bin/python tools/aec/aec-align-check.py --dry-run

--dry-run is a rehearsal of everything except the measurement itself: it plays
digital silence, changes no register and leaves wake-word.service alone.

    ... aec-align-check.py --compare 0,0.04,0.055 --seconds 24

--compare measures several reference delays in one go: for each, the reference
service is restarted with that delay (a temporary systemd drop-in), a burst is
played and measured, and at the end the service goes back to its own setting.
Since 2026-10-04 23:00 the service's own setting is 0 and these delays come on
top of the 37 ms that the XVF3800 sink's headroom provides
(~/.config/wireplumber/wireplumber.conf.d/51-smart-speaker.conf): the values
in the example above now put the reference behind the echo. Compare small
steps instead, e.g. --compare 0,0.005,0.01.

    ~/.venvs/voice-agent/bin/python tools/aec/aec-align-check.py --session-switch new,old

--session-switch rehearses what a voice session does to CamillaDSP in the
middle of a 44 s burst: 20 s on the profile that is running, 10 s with the
voice profile, 14 s back. "new" applies the voice profile's processing and
keeps the buffering (voice_agent.py since 2026-10-04); "old" loads voice.yml
as a file, which restarts the audio devices with less buffering. For each it
reports how far the echo moved in time and how much of it the chip removed
before, during and after. Needs the voice-agent venv (for the camilladsp
module); CamillaDSP is put back as it was found.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import threading
import time

import numpy as np

XVF_PY = "/home/aldom/.venvs/xvf3800/bin/python"
XVF_REG = "/home/aldom/.local/bin/xvf_register.py"
MIC = "alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993700254500350-00.analog-stereo"
SINK = "alsa_output.platform-snd_aloop.0.analog-stereo"
FS = 16000
PLAY_FS = 48000
RESULTS = os.path.join(os.path.expanduser("~/.local/state/ai-speaker/measurements"), "aec-align-results.json")

ENV = dict(os.environ)
ENV.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
ENV.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={ENV['XDG_RUNTIME_DIR']}/bus")


def run(cmd: list[str], check: bool = True) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, env=ENV)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def reg_get(name: str) -> str:
    return run([XVF_PY, XVF_REG, "get", name])


def reg_set(name: str, *values) -> None:
    run([XVF_PY, XVF_REG, "set", name, *[str(v) for v in values]])


def dbfs(x: np.ndarray) -> float:
    return float(20 * np.log10(max(float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))), 1e-9) / 32768))


def pink_noise(seconds: float, rms_dbfs: float, seed: int = 20261004) -> np.ndarray:
    """Mono pink-ish noise, 80 Hz - 8 kHz, with short fades, as int16."""
    n = int(seconds * PLAY_FS)
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / PLAY_FS)
    shape = np.zeros_like(f)
    band = (f >= 80) & (f <= 8000)
    shape[band] = 1 / np.sqrt(f[band])
    x = np.fft.irfft(spectrum * shape, n)
    x *= 10 ** (rms_dbfs / 20) * 32768 / np.sqrt(np.mean(x**2))
    fade = int(0.3 * PLAY_FS)
    x[:fade] *= np.linspace(0, 1, fade)
    x[-fade:] *= np.linspace(1, 0, fade)
    return np.clip(x, -23000, 23000).astype(np.int16)


def gcc_phat(mic: np.ndarray, ref: np.ndarray, max_lag_s: float, band=(150.0, 7000.0)):
    """Lag (s) of `mic` behind `ref`, plus how much the peak stands out."""
    nfft = 1 << (len(mic) + len(ref) - 1).bit_length()
    cross = np.fft.rfft(mic, nfft) * np.conj(np.fft.rfft(ref, nfft))
    f = np.fft.rfftfreq(nfft, 1 / FS)
    cross /= np.abs(cross) + 1e-12
    cross[(f < band[0]) | (f > band[1])] = 0
    cc = np.fft.irfft(cross, nfft)
    m = int(max_lag_s * FS)
    cc = np.concatenate((cc[-m:], cc[: m + 1]))
    mag = np.abs(cc)
    peak = int(np.argmax(mag))
    # Peak-to-background: the peak against the median of everything else.
    return (peak - m) / FS, float(mag[peak] / (np.median(mag) + 1e-12))


def program_audio_level() -> float:
    """RMS of whatever is already playing into the sink (read off its monitor)."""
    proc = subprocess.Popen(
        ["pw-cat", "--record", "--raw", "--target", SINK, "-P", "{ stream.capture.sink = true }",
         "--rate", str(FS), "--channels", "2", "--format", "s16", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=ENV, bufsize=0,
    )
    data, deadline = b"", time.monotonic() + 4.0
    try:
        while len(data) < FS * 4:  # one second
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([proc.stdout], [], [], remaining)[0]:
                break
            chunk = proc.stdout.read(FS * 4 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        proc.terminate()
        proc.wait()
    if len(data) < 4:
        return -200.0
    return dbfs(np.frombuffer(data[: len(data) // 4 * 4], dtype=np.int16))


class Capture:
    """pw-cat capture read into memory on a thread.

    No node.dont-fallback here: WirePlumber 0.5.8 destroys a stream carrying
    it the moment another stream starts -- and this script starts one (the
    noise) two seconds later. That is what broke the first version.
    """

    def __init__(self) -> None:
        self.data = bytearray()
        self.first_byte_at: float | None = None
        self.proc = subprocess.Popen(
            ["pw-cat", "--record", "--raw", "--target", MIC, "--rate", str(FS), "--channels", "2",
             "--format", "s16", "--latency", "40ms", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=ENV, bufsize=0,
        )
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        while True:
            chunk = self.proc.stdout.read(4096)
            if not chunk:
                return
            if self.first_byte_at is None:
                self.first_byte_at = time.monotonic()
            self.data += chunk

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        stop_process(self.proc)
        self._thread.join(timeout=2)


def stop_process(proc: subprocess.Popen | None) -> None:
    """Terminate, and kill if that is ignored (a hung pw-cat ignores SIGTERM)."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


XVF_SINK = "alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993700254500350-00.analog-stereo"
DROPIN_DIR = os.path.expanduser("~/.config/systemd/user/xvf-aec-ref.service.d")
DROPIN = os.path.join(DROPIN_DIR, "zz-measure-delay.conf")
BANDS = ((100, 300), (300, 1000), (1000, 2000), (2000, 4000), (4000, 7500))


def current_ref_delay() -> str:
    return run(["bash", "-c", "pgrep -a pw-loopback | grep -o -- '--delay=[0-9.]*' | head -1"], check=False)


def reference_linked() -> bool:
    """Is the loopback's output connected to the XVF3800's playback input?"""
    lines = run(["pw-link", "-l"], check=False).splitlines()
    for i, line in enumerate(lines):
        if line.strip() == f"{XVF_SINK}:playback_FL":
            for peer in lines[i + 1:]:
                if not peer.startswith(" "):
                    break
                if "output.xvf-aec-ref:output_FL" in peer:
                    return True
    return False


def restart_reference(delay: float | None) -> str:
    """Restart xvf-aec-ref.service with `delay` seconds as a temporary override
    (a systemd drop-in), or with the unit's own setting when `delay` is None.
    Returns the delay pw-loopback ends up running with."""
    if delay is None:
        if os.path.exists(DROPIN):
            os.remove(DROPIN)
        try:
            os.rmdir(DROPIN_DIR)
        except OSError:
            pass
    else:
        os.makedirs(DROPIN_DIR, exist_ok=True)
        with open(DROPIN, "w") as f:
            f.write("# Temporary: written by aec-align-check.py --compare and removed when it finishes.\n"
                    f"[Service]\nEnvironment=XVF_AEC_REF_DELAY={delay:g}\n")
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "restart", "xvf-aec-ref.service"])
    deadline = time.monotonic() + 25
    while not reference_linked():
        if time.monotonic() > deadline:
            raise RuntimeError("the reference loopback did not link to the XVF3800 within 25 s")
        time.sleep(0.5)
    time.sleep(2.0)
    return current_ref_delay()


VOICE_PROFILE = "/etc/camilladsp/profiles/voice.yml"
SESSION_STATE = os.path.join(ENV["XDG_RUNTIME_DIR"], "voice-agent-state.json")
# --session-switch timeline, seconds after the noise starts.
VOICE_ON, VOICE_OFF, SESSION_SECONDS = 20.0, 30.0, 44.0


class DspSwitch:
    """The two ways a voice session can give CamillaDSP the voice profile."""

    def __init__(self) -> None:
        try:
            from camilladsp import CamillaClient
        except ImportError:
            raise SystemExit("--session-switch needs the camilladsp module: run this with "
                             "~/.venvs/voice-agent/bin/python") from None
        self.client = CamillaClient("127.0.0.1", 1234)
        self.client.connect()
        self.base = self.client.config.active()
        self.base_path = self.client.config.file_path()
        self.voice = self.client.config.read_and_parse_file(VOICE_PROFILE)

    def on_voice(self) -> bool:
        return all(self.base.get(k) == v for k, v in self.voice.items() if k != "devices")

    def to_voice(self, mode: str) -> None:
        if mode == "new":
            self.client.config.set_active(dict(self.voice, devices=self.base["devices"]))
        else:
            self.client.config.set_file_path(VOICE_PROFILE)
            self.client.general.reload()

    def restore(self) -> None:
        """Back to what was found, whichever way it was left."""
        if self.client.config.file_path() != self.base_path:
            self.client.config.set_file_path(self.base_path)
            self.client.general.reload()
            deadline = time.monotonic() + 4.0
            while self.client.config.active() != self.base and time.monotonic() < deadline:
                time.sleep(0.1)
        if self.client.config.active() != self.base:
            self.client.config.set_active(self.base)
            time.sleep(0.3)

    def as_found(self) -> bool:
        return self.client.config.active() == self.base and self.client.config.file_path() == self.base_path


def to_capture_rate(x: np.ndarray) -> np.ndarray:
    """48 kHz -> 16 kHz. The noise has nothing above 8 kHz, so dropping the
    upper two thirds of its spectrum loses nothing."""
    n = len(x) // 6 * 6
    return np.fft.irfft(np.fft.rfft(x[:n].astype(np.float64))[: n // 6 + 1], n // 3) / 3


def measure_once(noise: bytes, seconds: float, dry: bool, before: dict, schedule=()) -> tuple[bytes, dict]:
    """One noise burst: R carries the reference for the first six seconds,
    then the AEC residual. `schedule` is a list of (seconds after the noise
    starts, name, action) to carry out on the way. Returns the capture and
    the timeline."""
    marks: dict[str, float] = {}
    capture = player = None
    if not dry:
        reg_set("AEC_ASROUTONOFF", *before["AEC_ASROUTONOFF"].split())
        reg_set("AUDIO_MGR_OP_L", 3, 0)
        reg_set("AUDIO_MGR_OP_R", 12, 0)
    time.sleep(0.5)
    try:
        capture = Capture()
        deadline = time.monotonic() + 5
        while capture.first_byte_at is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if capture.first_byte_at is None:
            raise RuntimeError("the microphone capture delivered nothing in 5 s")
        t0 = capture.first_byte_at
        time.sleep(2.0)

        print(f"[play] {seconds:.0f} s of {'digital silence (dry run)' if dry else 'pink noise'} ...", flush=True)
        # Fed through stdin, like sound_fx.py does: given a file name instead,
        # pw-cat's raw mode hung here for an hour and ignored SIGTERM.
        player = subprocess.Popen(
            ["pw-cat", "--playback", "--raw", "--target", SINK, "--rate", str(PLAY_FS),
             "--channels", "2", "--format", "s16", "-"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, env=ENV,
        )
        feeder = threading.Thread(target=lambda: (player.stdin.write(noise), player.stdin.close()), daemon=True)
        feeder.start()
        marks["play"] = time.monotonic() - t0
        time.sleep(6.0)
        marks["switch_start"] = time.monotonic() - t0
        if not dry:
            reg_set("AEC_ASROUTONOFF", 0)
            reg_set("AUDIO_MGR_OP_R", 7, 0)
        marks["switch_done"] = time.monotonic() - t0
        for at, name, action in schedule:
            wait = marks["play"] + at - (time.monotonic() - t0)
            if wait > 0:
                time.sleep(wait)
            marks[name] = time.monotonic() - t0
            action()
            print(f"[play] {at:.0f} s: {name.replace('_', ' ')}", flush=True)
        try:
            player.wait(timeout=max(1.0, marks["play"] + seconds + 4.0 - (time.monotonic() - t0)))
        except subprocess.TimeoutExpired:
            print("[play] the player did not exit by itself; stopping it")
        marks["play_end"] = time.monotonic() - t0
        time.sleep(1.0)
        if not capture.alive():
            raise RuntimeError("the microphone capture stopped during the measurement")
    finally:
        stop_process(player)
        if capture is not None:
            capture.stop()
    return bytes(capture.data[: len(capture.data) // 4 * 4]), marks


def band_erle(mic: np.ndarray, res: np.ndarray) -> dict[str, float]:
    """ERLE per frequency band (dB), from the power spectra of the two signals."""
    f = np.fft.rfftfreq(len(mic), 1 / FS)
    pm, pr = np.abs(np.fft.rfft(mic)) ** 2, np.abs(np.fft.rfft(res)) ** 2
    out = {}
    for lo, hi in BANDS:
        sel = (f >= lo) & (f < hi)
        out[f"{lo}-{hi}"] = round(float(10 * np.log10((pm[sel].sum() + 1e-9) / (pr[sel].sum() + 1e-9))), 1)
    return out


def analyse_session(result: dict, left: np.ndarray, right: np.ndarray, marks: dict, t_on: float, t_end: float,
                    played: np.ndarray | None) -> None:
    """--session-switch: the echo's timing and the ERLE before, during and
    after the voice profile. Adds to `result` and prints."""
    on, off = marks["voice_profile_on"], marks["voice_profile_off"]

    def erle_over(a: float, b: float) -> float | None:
        s0, s1 = int(a * FS), min(int(b * FS), len(left))
        if s1 - s0 < FS // 2:
            return None
        return round(dbfs(left[s0:s1]) - dbfs(right[s0:s1]), 1)

    phases = {
        "before": erle_over(on - 3.5, on - 0.5),
        "during": erle_over(on + 2.0, off - 0.5),
        "first_3s_after": erle_over(off + 0.5, off + 3.5),
        "end": erle_over(t_end - 3.4, t_end - 0.4),
    }
    result["erle_phases_db"] = phases
    result["voice_profile_seconds"] = [round(float(on - t_on), 2), round(float(off - t_on), 2)]

    if played is not None:
        # Where the echo of the (known) noise sits in the mic channel, phase
        # by phase. Only the differences mean anything: the common offset
        # depends on where the onset detection put t_on.
        origin = int(round(t_on * FS))

        def echo_at(a: float, b: float) -> float:
            i0, i1 = int(a * FS), int(b * FS)
            lag, _ = gcc_phat(left[i0:i1], played[i0 - origin: i1 - origin], 0.4)
            return lag * 1000

        before = echo_at(max(marks["switch_done"] + 1.0, on - 7.0), on - 0.5)
        during = echo_at(on + 2.0, off - 0.5)
        after = echo_at(off + 2.0, min(off + 9.0, t_end - 0.8))
        shift = during - (before + after) / 2
        result.update({
            "echo_shift_during_ms": round(shift, 2),
            "echo_after_minus_before_ms": round(after - before, 2),
            "lead_during_ms": round(result["lag_ms"] + shift, 2),
        })
        lead = result["lead_during_ms"]
        print(f"[session]   with the voice profile the echo arrives {shift:+.1f} ms compared with before and after "
              f"(after minus before: {after - before:+.1f} ms)")
        print(f"            -> the reference then leads the echo by {lead:+.1f} ms"
              + ("" if lead > 0 else "  NEGATIVE: the reference is late, nothing can be cancelled"))
    print(f"[session]   ERLE before {phases['before']} dB | with the voice profile {phases['during']} dB | "
          f"first 3 s after {phases['first_3s_after']} dB | at the end {phases['end']} dB")


def analyse(raw: bytes, marks: dict, seconds: float, dry: bool, played: np.ndarray | None = None) -> dict | None:
    """Print and return the result of one burst; None when there is nothing to
    analyse (a dry run, or a capture that came up short). `played` is the
    noise at the capture rate, for --session-switch."""
    data = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2).astype(np.float64)
    left, right = data[:, 0], data[:, 1]
    captured = len(left) / FS
    expected = marks["play_end"] + 1.0
    if captured < expected - 1.0:
        print(f"[result] Only {captured:.1f} s captured where {expected:.1f} s were expected; not analysing.")
        return None

    # Noise onset as the canceller saw it: where the reference comes up out of the floor.
    win = int(0.05 * FS)
    env = np.sqrt(np.mean(right[: len(right) // win * win].reshape(-1, win) ** 2, axis=1))
    floor = float(np.median(env[: int(1.0 / 0.05)])) + 1.0
    above = np.nonzero(env > floor * 10)[0]
    result = {"marks": {k: round(v, 2) for k, v in marks.items()}, "captured_seconds": round(captured, 2)}
    if len(above) == 0:
        if dry:
            print(f"[dry run] {captured:.1f} s captured; player and capture both ran to the end.")
            return None
        print("[result] The reference never rose above its idle floor: the AEC is getting no far-end signal.")
        result["reference_present"] = False
        return result

    t_on = above[0] * 0.05
    a0, a1 = int((t_on + 1.0) * FS), int(min(t_on + 5.0, marks["switch_start"] - 0.2) * FS)
    mic_a, ref_a = left[a0:a1], right[a0:a1]
    lag, sharp = gcc_phat(mic_a, ref_a, 0.8)
    clipped = float(np.mean(np.abs(ref_a) >= 32700))
    quiet = slice(0, int(1.5 * FS))
    result.update({
        "reference_present": True, "noise_onset_s": round(t_on, 2),
        "lag_ms": round(lag * 1000, 2), "peak_to_background": round(sharp, 1),
        "mic_dbfs": round(dbfs(mic_a), 1), "ref_dbfs": round(dbfs(ref_a), 1),
        "ref_peak": int(np.max(np.abs(ref_a))), "ref_clipped_fraction": round(clipped, 5),
        "mic_idle_dbfs": round(dbfs(left[quiet]), 1), "ref_idle_dbfs": round(dbfs(right[quiet]), 1),
    })
    # ERLE, second by second, after the residual tap is live.
    t_end = t_on + seconds
    erle = []
    t = marks["switch_done"] + 0.7
    while t + 1.0 <= t_end - 0.4:
        s0, s1 = int(t * FS), int((t + 1.0) * FS)
        erle.append(round(dbfs(left[s0:s1]) - dbfs(right[s0:s1]), 1))
        t += 1.0
    result["erle_db_per_second"] = erle
    result["erle_db"] = round(float(np.mean(erle[-4:])), 1) if erle else None
    b0, b1 = int((t_end - 4.4) * FS), int((t_end - 0.4) * FS)
    if b0 > marks["switch_done"] * FS:
        result["erle_db_by_band"] = band_erle(left[b0:b1], right[b0:b1])

    print(f"[alignment] echo at mic 0 arrives {result['lag_ms']:+.1f} ms relative to the reference "
          f"(peak/background {sharp:.0f}x)")
    if lag < 0:
        print("            NEGATIVE: the reference reaches the chip AFTER the echo -- it cannot be cancelled.")
    print(f"[levels]    mic 0 {result['mic_dbfs']} dBFS (idle {result['mic_idle_dbfs']}), reference "
          f"{result['ref_dbfs']} dBFS (idle {result['ref_idle_dbfs']}), reference peak {result['ref_peak']}, "
          f"clipped {clipped * 100:.2f}% of samples")
    print(f"[erle]      per second after the residual tap went live: {erle} dB -> {result['erle_db']} dB")
    if "erle_db_by_band" in result:
        print(f"[erle]      last 4 s by band (Hz): {result['erle_db_by_band']}")
    if "voice_profile_on" in marks and "voice_profile_off" in marks:
        analyse_session(result, left, right, marks, t_on, t_end, played)
    if dry:
        print("[dry run] a signal showed up on the reference channel although only silence was played; "
              "something else is using the speaker.")
        return None
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=16.0, help="noise duration per measurement")
    ap.add_argument("--level", type=float, default=-23.0, help="noise RMS, dBFS")
    ap.add_argument("--force", action="store_true", help="run even if something is already playing")
    ap.add_argument("--note", default="", help="free text stored with the result")
    ap.add_argument("--compare", default="",
                    help="comma-separated reference delays in seconds, e.g. 0,0.04,0.055: xvf-aec-ref.service is "
                         "restarted with each in turn, measured, and put back on its own setting at the end")
    ap.add_argument("--session-switch", default="", metavar="new,old",
                    help="apply the voice profile in the middle of a 44 s burst, the way voice_agent.py does it "
                         "now (new: processing only) and/or the way it used to (old: voice.yml as a file); "
                         "--seconds is ignored")
    ap.add_argument("--dry-run", action="store_true",
                    help="rehearsal: plays digital silence, touches no register, leaves wake-word running, "
                         "stores nothing (with --compare the reference service is still restarted, with "
                         "--session-switch CamillaDSP is still switched)")
    args = ap.parse_args()
    dry = args.dry_run
    delays: list[float | None] = [float(x) for x in args.compare.split(",") if x.strip()] or [None]
    modes = [m.strip() for m in args.session_switch.split(",") if m.strip()]
    if any(m not in ("new", "old") for m in modes):
        ap.error("--session-switch takes new, old or new,old")
    if modes and args.compare:
        ap.error("--compare and --session-switch cannot be combined")
    seconds = SESSION_SECONDS if modes else args.seconds
    runs: list[tuple[str, object]] = [("session", m) for m in modes] or [("delay", d) for d in delays]

    playing = program_audio_level()
    if playing > -70 and not args.force:
        print(f"Program audio is playing ({playing:.0f} dBFS on the sink monitor); not adding noise on "
              "top of it. Pause it, or pass --force.")
        return 2

    dsp = None
    if modes:
        if os.path.exists(SESSION_STATE):
            print("A voice session is active; run this again when it has ended.")
            return 2
        dsp = DspSwitch()
        if dsp.on_voice():
            print("CamillaDSP is already running the voice profile; there is nothing to switch to.")
            return 2

    before = {name: reg_get(name) for name in ("AUDIO_MGR_OP_L", "AUDIO_MGR_OP_R", "AEC_ASROUTONOFF")}
    context = {name: reg_get(name) for name in (
        "AUDIO_MGR_SYS_DELAY", "AUDIO_MGR_REF_GAIN", "AUDIO_MGR_MIC_GAIN", "AEC_FAR_EXTGAIN",
        "AUDIO_MGR_FAR_END_DSP_ENABLE")}
    print(f"[state] {before}  {context}  pw-loopback {current_ref_delay() or '(not found)'}")

    mono = pink_noise(seconds, args.level)
    played = to_capture_rate(mono) if modes and not dry else None
    if dry:
        mono[:] = 0
    noise = np.repeat(mono, 2).tobytes()

    wake_was_active = (not dry and
                       run(["systemctl", "--user", "is-active", "wake-word.service"], check=False) == "active")
    results: list[dict] = []
    problems: list[str] = []
    last_raw = b""
    try:
        if wake_was_active:
            run(["systemctl", "--user", "stop", "wake-word.service"])
        for index, (kind, value) in enumerate(runs):
            schedule = []
            if kind == "delay" and value is not None:
                print(f"\n=== reference delay {value:g} s ===", flush=True)
                running = restart_reference(value)
                print(f"[state] pw-loopback {running}")
            if kind == "session":
                print(f"\n=== voice profile applied the {str(value).upper()} way, "
                      f"{VOICE_ON:.0f}-{VOICE_OFF:.0f} s into the noise ===", flush=True)
                schedule = [(VOICE_ON, "voice_profile_on", lambda mode=value: dsp.to_voice(mode)),
                            (VOICE_OFF, "voice_profile_off", dsp.restore)]
            last_raw, marks = measure_once(noise, seconds, dry, before, schedule)
            result = analyse(last_raw, marks, seconds, dry, played)
            if result is not None:
                result.update({
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "note": args.note,
                    "registers": {**before, **context}, "pw_loopback": current_ref_delay(),
                    "noise_dbfs": args.level, "seconds": seconds,
                })
                if kind == "session":
                    result["session_switch"] = value
                results.append(result)
            if index < len(runs) - 1:
                time.sleep(2.0)
    finally:
        if dsp is not None:
            try:
                dsp.restore()
                if not dsp.as_found():
                    problems.append("CamillaDSP is not on the configuration it was found with")
            except Exception as exc:
                problems.append(f"CamillaDSP: {exc}")
        if not dry:
            for name, value in (("AEC_ASROUTONOFF", before["AEC_ASROUTONOFF"]),
                                ("AUDIO_MGR_OP_L", before["AUDIO_MGR_OP_L"]),
                                ("AUDIO_MGR_OP_R", before["AUDIO_MGR_OP_R"])):
                try:
                    reg_set(name, *value.split())
                    now = reg_get(name)
                    if now != value:
                        problems.append(f"{name} reads {now!r}, wanted {value!r}")
                except Exception as exc:
                    problems.append(f"{name}: {exc}")
        if delays != [None]:
            try:
                print(f"\n[restore] reference service back on its own setting: pw-loopback {restart_reference(None)}")
            except Exception as exc:
                problems.append(f"xvf-aec-ref.service: {exc}")
        if wake_was_active:
            run(["systemctl", "--user", "start", "wake-word.service"], check=False)
        if problems:
            print("[restore] PROBLEM -- not everything is back as it was: " + "; ".join(problems))
        elif not dry:
            print(f"[restore] registers back to {before}; wake-word "
                  f"{run(['systemctl', '--user', 'is-active', 'wake-word.service'], check=False)}")

    if dry:
        print("[dry run] nothing played, no register changed, nothing stored. The real run should work.")
        return 1 if problems else 0

    if modes:
        print("\n  voice profile   echo moved   reference lead   ERLE before    during   first 3 s after   at the end")
        for r in results:
            if "erle_phases_db" not in r:
                continue
            p = r["erle_phases_db"]
            cell = lambda v: "    n/a" if v is None else f"{v:5.1f} dB"  # noqa: E731
            print(f"  {r['session_switch']:<13s}  {r.get('echo_shift_during_ms', float('nan')):+7.1f} ms   "
                  f"{r.get('lead_during_ms', float('nan')):+8.1f} ms     {cell(p['before'])}  {cell(p['during'])}   "
                  f"{cell(p['first_3s_after'])}        {cell(p['end'])}")
    elif len(results) > 1:
        print("\n  delay       lead     ERLE after   ERLE at end   300-1000 Hz   1000-2000 Hz")
        for r in results:
            if not r.get("reference_present"):
                continue
            e = r.get("erle_db_per_second") or [float("nan")]
            bands = r.get("erle_db_by_band", {})
            print(f"  {r['pw_loopback'].replace('--delay=', ''):>6s} s  {r['lag_ms']:+7.1f} ms   "
                  f"{e[min(3, len(e) - 1)]:5.1f} dB (4 s)   {r['erle_db']:5.1f} dB      "
                  f"{bands.get('300-1000', float('nan')):5.1f} dB       {bands.get('1000-2000', float('nan')):5.1f} dB")

    history = []
    if os.path.exists(RESULTS):
        with open(RESULTS) as f:
            history = json.load(f)
    history.extend(results)
    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    with open(RESULTS, "w") as f:
        json.dump(history, f, indent=1)
    keep = os.path.join(os.path.expanduser("~/.local/state/ai-speaker/measurements"), "aec-align-last-capture.raw")
    with open(keep, "wb") as f:
        f.write(last_raw)
    print(f"\n[saved] {RESULTS}; last capture kept at {keep} (16 kHz, 2 ch, s16)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
