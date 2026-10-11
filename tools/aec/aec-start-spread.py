"""Silent: how much does the reference's lead differ from one start of the
XVF3800 playback device to the next? Proxy for the lead: audio buffered on
the speaker path (snd-aloop both ends + DAC) minus audio buffered in the
XVF3800 playback device, from ALSA's status files, while -75 dBFS noise plays.
Usage: start_spread.py STARTS [PLAY_SECONDS] [IDLE_SECONDS]"""
import importlib.util, os, re, sys, time
import numpy as np
P = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("probe", os.path.join(P, "aec-ref-timing-probe.py"))
probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)
aec = probe.aec
N = int(sys.argv[1]); PLAY = float(sys.argv[2]) if len(sys.argv) > 2 else 9.0; IDLE = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0
FILES = ["/proc/asound/card0/pcm0p/sub0/status", "/proc/asound/card0/pcm1c/sub0/status",
         "/proc/asound/card2/pcm0p/sub0/status", "/proc/asound/card1/pcm0p/sub0/status"]
pat = re.compile(r"delay\s*:\s*(-?\d+)")
def read(p):
    try:
        with open(p) as f: m = pat.search(f.read())
        return int(m.group(1)) if m else np.nan
    except OSError: return np.nan
if aec.program_audio_level() > -85: sys.exit("something is playing; not probing")
noise = aec.pink_noise(PLAY, probe.LEVEL, seed=77)
out = []
for i in range(N):
    proc = probe.player(noise); t0 = time.monotonic(); rows = []
    while time.monotonic() - t0 < PLAY - 0.5:
        if time.monotonic() - t0 > 3.0: rows.append([read(p) for p in FILES])
        time.sleep(0.004)
    proc.wait(timeout=10)
    a = np.nanmean(np.array(rows, dtype=float), axis=0) / 48.0
    out.append((a[0] + a[1] + a[2], a[3]))
    print(f"start {i+1}: speaker path {out[-1][0]:7.2f} ms   XVF buffer {a[3]:7.2f} ms   difference {out[-1][0]-a[3]:7.2f} ms", flush=True)
    time.sleep(IDLE)
d = np.array([s - x for s, x in out]); x = np.array([x for _, x in out]); s = np.array([s for s, _ in out])
print(f"difference: mean {d.mean():.2f} ms, min {d.min():.2f}, max {d.max():.2f}, spread {d.max()-d.min():.2f} ms; "
      f"XVF buffer spread {np.nanmax(x)-np.nanmin(x):.2f} ms; speaker path spread {s.max()-s.min():.2f} ms")
