"""One silent burst like aec-ref-timing-probe.py's `play`, for when that probe's
readings need a second look. Differences:

- it searches +-300 ms for the played noise in every half second, not +-20 ms,
  so a jump of the reference (or of the probe's own streams) shows up as a
  jump instead of as random numbers;
- it prints only the half seconds that are off the median by more than 0.1 ms
  or unclear, with the tap's level and channel 1's;
- DIAG_RT=1 gives the probe's own pw-cat player and capture realtime priority
  (needs the pipewire group's rtprio limit). Without it they are ordinary
  processes, and under CPU load a late one moves echo and reference together
  by whole graph cycles (5.33 ms) -- the probe then reports steps that are not
  in the reference path;
- it keeps the capture (OUT.npy, both channels) and the XVF3800 playback
  buffer readings (OUT.npy.fills.npy).

    ~/.venvs/voice-agent/bin/python tools/aec/aec-ref-diag.py SECONDS OUT.npy
"""
import importlib.util, os, sys, time, threading
import numpy as np
P = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("probe", os.path.join(P, "aec-ref-timing-probe.py"))
probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)
aec, FS = probe.aec, probe.FS
seconds = float(sys.argv[1]); out = sys.argv[2]
noise = aec.pink_noise(seconds, probe.LEVEL, seed=1000)
fills = []
with probe.Tap() as tap:
    stop = threading.Event()
    def poll():
        while not stop.is_set():
            f = probe.fill()
            fills.append((tap.now(), -1 if f is None else f)); time.sleep(0.005)
    threading.Thread(target=poll, daemon=True).start()
    proc = probe.player(noise); start = tap.now()
    if os.environ.get("DIAG_RT"):   # keep the instrument's own streams from being late under load
        import subprocess
        for pid in (proc.pid, tap.capture.proc.pid):
            r = subprocess.run(["chrt", "-f", "-p", "60", str(pid)], capture_output=True, text=True)
            if r.returncode: print("chrt failed:", r.stderr.strip())
    try: proc.wait(timeout=seconds + 6)
    except Exception: aec.stop_process(proc)
    time.sleep(1.0); stop.set()
data = tap.capture.data
both = np.frombuffer(bytes(data[: len(data) // 4 * 4]), dtype=np.int16).reshape(-1, 2).astype(np.float64)
np.save(out, both.astype(np.int16)); np.save(out + ".fills.npy", np.array(fills))
sig = both[:, 0]; played = aec.to_capture_rate(noise)
coarse, sharp = probe.onset(sig, played, start)
print(f"start {start:.2f}s coarse {coarse*1000:.1f} ms (peak/bg {sharp:.0f}); captured {len(sig)/FS:.1f}s")
rows = []; t = 0.25
while t + 0.5 <= seconds - 0.3:
    a = int((start + coarse + t) * FS)
    seg = sig[a: a + FS // 2]; src = played[int(t * FS): int(t * FS) + FS // 2]
    lag, sh = aec.gcc_phat(seg, src, 0.3)
    rows.append((t, aec.dbfs(seg), (coarse + lag) * 1000, sh, aec.dbfs(both[a: a + FS // 2, 1])))
    t += 0.5
import statistics
mode = statistics.median(r[2] for r in rows)
odd = [r for r in rows if abs(r[2] - mode) > 0.1 or r[3] < 300]
print(f"{len(rows)} half-second windows; median delay {mode:.2f} ms; {len(odd)} windows off by >0.1 ms or unclear (peak <300x):")
for i in range(0, len(odd), 4):
    print("  " + "   ".join(f"{t:5.1f}s {lv:6.1f}dB {d:7.2f}ms {sh:4.0f}x ch1 {c1:5.1f}" for t, lv, d, sh, c1 in odd[i:i + 4]))
