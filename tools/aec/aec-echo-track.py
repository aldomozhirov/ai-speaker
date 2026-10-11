"""After an audible `aec-align-check.py --session-switch ...` run: is the echo
standing still? Tracks, second by second, where the echo (L) and, for the
first seconds, the reference tap (R) sit relative to the noise that was
played, in aec-align-last-capture.raw (the last burst of the run; marks and
levels are taken from the last entry of aec-align-results.json).

A canceller that removes nothing although the reference leads the echo is
usually looking at a moving target: on 2026-10-04 this showed the echo
sliding 0.07 ms/s and snapping back 1.4 ms every 25 s (the XVF3800 output had
become the graph's clock master) while the reference stayed put.

    ~/.venvs/voice-agent/bin/python tools/aec/aec-echo-track.py [CAPTURE.raw]
"""
import importlib.util, json, os, sys
import numpy as np
P = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.expanduser("~/.local/state/ai-speaker/measurements")
spec = importlib.util.spec_from_file_location("aec", os.path.join(P, "aec-align-check.py"))
aec = importlib.util.module_from_spec(spec); spec.loader.exec_module(aec)
FS = aec.FS
raw = open(sys.argv[1] if len(sys.argv) > 1 else os.path.join(DATA, "aec-align-last-capture.raw"), "rb").read()
res = json.load(open(os.path.join(DATA, "aec-align-results.json")))[-1]
marks, t_on, seconds = res["marks"], None, res["seconds"]
data = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2).astype(np.float64)
left, right = data[:, 0], data[:, 1]
win = int(0.05 * FS)
env = np.sqrt(np.mean(right[: len(right) // win * win].reshape(-1, win) ** 2, axis=1))
floor = float(np.median(env[: int(1.0 / 0.05)])) + 1.0
t_on = np.nonzero(env > floor * 10)[0][0] * 0.05
played = aec.to_capture_rate(aec.pink_noise(seconds, res["noise_dbfs"]))
origin = int(round(t_on * FS))
print(f"run {res['time']} switch={res.get('session_switch')} t_on={t_on:.2f}s marks={marks}")

def lag_ms(sig, a, b, max_lag=0.4):
    i0, i1 = int(a * FS), int(b * FS)
    x, y = sig[i0:i1], played[i0 - origin: i1 - origin]
    nfft = 1 << (len(x) + len(y) - 1).bit_length()
    cross = np.fft.rfft(x, nfft) * np.conj(np.fft.rfft(y, nfft))
    f = np.fft.rfftfreq(nfft, 1 / FS)
    cross /= np.abs(cross) + 1e-12
    cross[(f < 150) | (f > 7000)] = 0
    cc = np.fft.irfft(cross, nfft)
    m = int(max_lag * FS)
    cc = np.concatenate((cc[-m:], cc[: m + 1]))
    mag = np.abs(cc); k = int(np.argmax(mag))
    d = 0.0
    if 0 < k < len(mag) - 1:
        y0, y1, y2 = mag[k - 1], mag[k], mag[k + 1]
        d = 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2)
    return (k - m + d) / FS * 1000, mag[k] / (np.median(mag) + 1e-12)

W, STEP = 1.0, 0.5
print("\nreference tap (R) vs the played noise, first seconds:")
t = t_on + 0.5
while t + W <= marks["switch_start"] - 0.1:
    l, s = lag_ms(right, t, t + W); print(f"  {t - t_on:5.1f}s  {l:8.3f} ms  (peak {s:5.0f}x)"); t += STEP
print("\necho at mic 0 (L) vs the played noise:")
rows = []
t = t_on + 0.5
while t + W <= t_on + seconds - 0.4:
    l, s = lag_ms(left, t, t + W); rows.append((t - t_on, l, s)); t += STEP
base = rows[0][1]
for i in range(0, len(rows), 4):
    print("  " + "   ".join(f"{a:5.1f}s {l - base:+7.3f}ms({s:3.0f}x)" for a, l, s in rows[i:i + 4]))
ls = np.array([r[1] for r in rows])
print(f"\necho position: first window {base:.3f} ms; min {ls.min() - base:+.3f}, max {ls.max() - base:+.3f}, "
      f"range {ls.max() - ls.min():.3f} ms; largest change between adjacent 0.5 s steps {np.abs(np.diff(ls)).max():.3f} ms")
