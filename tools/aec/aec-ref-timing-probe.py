#!/usr/bin/env python3
"""Inaudible probes of the AEC reference: is its timing steady, and how far
ahead of the echo does it arrive?

Found 2026-10-04: now and then the reference reached the XVF3800 a few
milliseconds earlier or later than it had a moment before, and the echo
canceller lost its solution until it had re-converged. These show that
without making a sound anyone can hear.

Both play pink noise at -75 dBFS (52 dB below aec-align-check.py's hiss, about
20 dB under the microphones' own noise floor) and read one of the chip's debug
taps on capture channel 0, which nothing listens to since the wake word and
the sessions both use channel 1. AUDIO_MGR_OP_L is switched to the tap for the
duration and put back. No service is stopped.

    aec-ref-timing-probe.py [play:20 gap:5.6 play:26 ...]
        Bursts with pauses in between. For every burst it prints, twice a
        second, how far the reference at the chip (tap "12 0") lags the stream
        that was played. Within one burst that number should not move; between
        bursts it is not comparable (it includes how long the player took to
        start). The figure in brackets is the audio waiting in the XVF3800's
        playback buffer on the host.

        Reading it: the number is relative to the stream the probe itself
        plays. If the probe's own player is scheduled late, the whole stream
        shifts by one graph cycle (5.33 ms) and so does the echo -- a step of
        exactly that size with the host buffer unchanged is usually the
        instrument, not the reference path. A slide of a few ms over seconds,
        or a step together with a change in the host buffer, is the reference
        path. To take the instrument out of it on a busy machine, shield it:
        PROBE_PIN_CPUS=3 keeps the probe's player and capture on CPU 3; move
        the PipeWire daemon there too and everything else off it (taskset),
        as in the 2026-10-04 tests with aec-test-cpu-load.sh.

    aec-ref-timing-probe.py lead [REF_SECONDS MIC_SECONDS]
        One burst: first the reference tap, then microphone 0 (tap "3 0").
        The echo is far below the room's noise at this level, so its arrival
        time is recovered by averaging over MIC_SECONDS (default 90). Prints
        the lead: how long before the echo the reference reaches the chip --
        the number aec-align-check.py measures with audible noise.

Run with the voice-agent or wake-word venv (needs numpy):
    ~/.venvs/voice-agent/bin/python tools/aec/aec-ref-timing-probe.py lead
"""

from __future__ import annotations

import importlib.util
import os
import statistics
import subprocess
import sys
import threading
import time

import numpy as np

spec = importlib.util.spec_from_file_location(
    "aec", os.path.join(os.path.dirname(os.path.abspath(__file__)), "aec-align-check.py"))
aec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aec)

FS, LEVEL = aec.FS, -75.0
FILL = "/proc/asound/Array/pcm0p/sub0/status"
DEFAULT_PLAN = [("play", 20.0), ("gap", 5.6), ("play", 26.0), ("gap", 12.0), ("play", 20.0), ("gap", 2.0),
                ("play", 20.0)]


# For telling apart what disturbs what: with PROBE_PIN_CPUS=3 the probe's own
# player and capture run on that CPU only, so a busy machine cannot make the
# instrument itself stutter (a late player moves the echo and the reference
# together, which is of no interest here).
PIN_CPUS = os.environ.get("PROBE_PIN_CPUS", "")


def pin(pid: int, after: float = 0.0) -> None:
    if PIN_CPUS:
        time.sleep(after)
        subprocess.run(["taskset", "-a", "-cp", PIN_CPUS, str(pid)], capture_output=True)


def fill() -> int | None:
    try:
        with open(FILL) as f:
            for line in f:
                if line.startswith("delay"):
                    return int(line.split(":")[1])
    except OSError:
        pass
    return None


def player(mono: np.ndarray) -> subprocess.Popen:
    """Plays `mono` (48 kHz int16) into the program sink, fed through stdin."""
    proc = subprocess.Popen(["pw-cat", "--playback", "--raw", "--target", aec.SINK, "--rate", str(aec.PLAY_FS),
                             "--channels", "2", "--format", "s16", "-"],
                            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, env=aec.ENV)
    data = np.repeat(mono, 2).tobytes()
    threading.Thread(target=lambda: (proc.stdin.write(data), proc.stdin.close()), daemon=True).start()
    pin(proc.pid, 0.15)
    return proc


class Tap:
    """Capture channel 0 switched to a debug tap, and back when done."""

    def __init__(self) -> None:
        if os.path.exists(aec.SESSION_STATE):
            sys.exit("a voice session is active; not probing")
        if aec.program_audio_level() > -85:
            sys.exit("something is playing; not probing")
        self.before = aec.reg_get("AUDIO_MGR_OP_L")
        print("AUDIO_MGR_OP_L before:", self.before)
        self.capture = None
        self.t0 = 0.0

    def __enter__(self) -> "Tap":
        aec.reg_set("AUDIO_MGR_OP_L", 12, 0)
        time.sleep(0.5)
        self.capture = aec.Capture()
        while self.capture.first_byte_at is None:
            time.sleep(0.05)
        self.t0 = self.capture.first_byte_at
        pin(self.capture.proc.pid)
        # Wall-clock time of capture second 0, for lining this up with journals.
        epoch = time.time() - (time.monotonic() - self.t0)
        print(f"capture second 0 = {time.strftime('%H:%M:%S', time.localtime(epoch))} (epoch {epoch:.3f})")
        time.sleep(1.5)
        return self

    def now(self) -> float:
        return time.monotonic() - self.t0

    def __exit__(self, *exc) -> None:
        if self.capture is not None:
            self.capture.stop()
        aec.reg_set("AUDIO_MGR_OP_L", *self.before.split())
        print("AUDIO_MGR_OP_L after:", aec.reg_get("AUDIO_MGR_OP_L"))

    def channel0(self) -> np.ndarray:
        data = self.capture.data
        return np.frombuffer(bytes(data[: len(data) // 4 * 4]), dtype=np.int16).reshape(-1, 2)[:, 0].astype(np.float64)


def onset(tap: np.ndarray, played: np.ndarray, start: float) -> tuple[float, float]:
    """Where a burst begins in the capture, relative to when its player was started."""
    i0 = int((start + 1.0) * FS)
    return aec.gcc_phat(tap[i0:i0 + 4 * FS], played[FS: 5 * FS], 0.6)


def series(tap: np.ndarray, played: np.ndarray, start: float, coarse: float, seconds: float, fills=()) -> list:
    """(time into the burst, reference delay ms, host buffer fill ms), twice a second."""
    out, t = [], 0.25
    while t + 0.5 <= seconds - 0.3:
        a = int((start + coarse + t) * FS)
        lag, _ = aec.gcc_phat(tap[a: a + FS // 2], played[int(t * FS): int(t * FS) + FS // 2], 0.02)
        f = [v for (ft, v) in fills if start + t <= ft < start + t + 0.5]
        out.append((t, (coarse + lag) * 1000, statistics.mean(f) / 48 if len(f) > 10 else float("nan")))
        t += 0.5
    return out


def bursts(plan: list[tuple[str, float]]) -> int:
    noises = {i: aec.pink_noise(s, LEVEL, seed=1000 + i) for i, (k, s) in enumerate(plan) if k == "play"}
    plays, fills = [], []
    with Tap() as tap:
        stop = threading.Event()

        def poll() -> None:
            while not stop.is_set():
                f = fill()
                if f is not None:
                    fills.append((tap.now(), f))
                time.sleep(0.005)

        threading.Thread(target=poll, daemon=True).start()
        for index, (kind, seconds) in enumerate(plan):
            if kind == "gap":
                time.sleep(seconds)
                continue
            proc = player(noises[index])
            plays.append((tap.now(), seconds, index))
            try:
                proc.wait(timeout=seconds + 6)
            except subprocess.TimeoutExpired:
                aec.stop_process(proc)
        time.sleep(1.0)
        stop.set()
    signal = tap.channel0()
    print(f"captured {len(signal) / FS:.1f} s; tap level idle {aec.dbfs(signal[:FS]):.1f} dBFS")
    worst = 0.0
    for start, seconds, index in plays:
        played = aec.to_capture_rate(noises[index])
        coarse, sharp = onset(signal, played, start)
        rows = series(signal, played, start, coarse, seconds, fills)
        level = aec.dbfs(signal[int((start + coarse + 2) * FS): int((start + coarse + 6) * FS)])
        print(f"\nplay at {start:.1f} s for {seconds} s: tap level {level:.1f} dBFS, "
              f"onset offset {coarse * 1000:.1f} ms (peak/bg {sharp:.0f})")
        print("  t(s): reference delay ms [host fill ms]")
        for i in range(0, len(rows), 8):
            print("   " + "  ".join(f"{t:5.1f}: {d:6.2f} [{f:5.2f}]" for t, d, f in rows[i:i + 8]))
        moved = max(d for _, d, _ in rows) - min(d for _, d, _ in rows)
        worst = max(worst, moved)
        # Single readings, not the half-second means above: how far a late
        # graph cycle let the buffer run down.
        raw = [v / 48 for (ft, v) in fills if start + 1.0 <= ft < start + seconds]
        if raw:
            print(f"  host buffer: lowest single reading {min(raw):.1f} ms, highest {max(raw):.1f} ms, "
                  f"median {statistics.median(raw):.1f} ms")
        print(f"  reference timing moved by {moved:.2f} ms within this burst")
    print(f"\nlargest movement within a burst: {worst:.2f} ms")
    return 0


def lead(ref_seconds: float, mic_seconds: float) -> int:
    total = ref_seconds + mic_seconds + 2.0
    mono = aec.pink_noise(total, LEVEL, seed=2000)
    played = aec.to_capture_rate(mono)
    with Tap() as tap:
        proc = player(mono)
        start = tap.now()
        time.sleep(ref_seconds)
        aec.reg_set("AUDIO_MGR_OP_L", 3, 0)      # microphone 0 instead of the reference
        switched = tap.now()
        try:
            proc.wait(timeout=mic_seconds + 10)
        except subprocess.TimeoutExpired:
            aec.stop_process(proc)
        time.sleep(0.5)
    signal = tap.channel0()

    coarse, sharp = onset(signal, played, start)
    rows = series(signal, played, start, coarse, ref_seconds - 1.0)
    delays = [d for _, d, _ in rows]
    reference = statistics.median(delays)
    print(f"reference tap: {reference:.2f} ms after the stream (peak/bg {sharp:.0f}), "
          f"moved {max(delays) - min(delays):.2f} ms in {ref_seconds:.0f} s")

    # Microphone part: the echo's impulse response, from the cross-spectrum
    # with the known noise over the whole stretch, 300 Hz - 5 kHz.
    t1 = switched - start + 1.5                      # seconds into the burst
    n = int((total - 1.5 - t1) * FS)
    a = int((start + coarse + t1) * FS)
    mic, src = signal[a: a + n], played[int(t1 * FS): int(t1 * FS) + n]
    n = min(len(mic), len(src))
    spectrum_mic, spectrum_src = np.fft.rfft(mic[:n]), np.fft.rfft(src[:n])
    f = np.fft.rfftfreq(n, 1 / FS)
    response = spectrum_mic * np.conj(spectrum_src) / (np.abs(spectrum_src) ** 2 + 1e-9)
    response[(f < 300) | (f > 5000)] = 0
    h = np.abs(np.fft.irfft(response, n))
    window = int(0.25 * FS)                           # the echo is within 250 ms after the stream
    peak = int(np.argmax(h[:window]))
    background = float(np.median(h[window: 4 * window]))
    echo = coarse * 1000 + peak / FS * 1000
    print(f"microphone 0:  {echo:.2f} ms after the stream (peak {h[peak] / background:.0f}x the background, "
          f"{n / FS:.0f} s averaged, level {aec.dbfs(mic):.1f} dBFS)")
    if h[peak] < 6 * background:   # noise alone peaks at about 3.5x the median
        print("  the echo did not stand out clearly; do not trust this lead")
    print(f"\nlead: the reference reaches the chip {echo - reference:+.1f} ms before the echo")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args and args[0] == "lead":
        return lead(float(args[1]) if len(args) > 1 else 20.0, float(args[2]) if len(args) > 2 else 90.0)
    plan = [(k, float(v)) for k, v in (a.split(":") for a in args)] or DEFAULT_PLAN
    return bursts(plan)


if __name__ == "__main__":
    sys.exit(main())
