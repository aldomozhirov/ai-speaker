#!/usr/bin/env python3
"""Live wake-word score meter. Run it, speak whenever you like, watch the number.

Exists because the two ways to observe this system both hide the failure case:
`wake_word.py`'s WAKE_WORD_DEBUG only journals frames scoring above 0.05, and
the service only acts above WAKE_WORD_THRESHOLD -- so an utterance that scores
0.03 is invisible in both. This prints every second's peak on both channels
regardless of level, so "scored low" is distinguishable from "never arrived".

  wake-live.py            # run until Ctrl-C
  wake-live.py 60         # run for 60 seconds

Also appends to /tmp/wake-live.log for later inspection.
Safe to run alongside wake-word.service -- it opens its own capture stream.
"""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.expanduser("~/.local/bin"))
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "calib", os.path.expanduser("~/.local/bin/ai-speaker-calibrate.py"))
calib = importlib.util.module_from_spec(_spec); sys.modules["calib"] = calib
_spec.loader.exec_module(calib)

THRESH = float(os.environ.get("WAKE_WORD_THRESHOLD", "0.10"))
limit = float(sys.argv[1]) if len(sys.argv) > 1 else None

print(f"Listening. Say 'Hey Jarvis' whenever you like. Threshold = {THRESH}")
print("Bar shows ch1 (the channel the service uses). Ctrl-C to stop.\n")
print(f"{'time':>8}  {'ch0':>6}  {'ch1':>6}  {'lvl dBFS':>8}")

log = open("/tmp/wake-live.log", "a")
log.write(f"\n--- session {time.strftime('%H:%M:%S')} threshold={THRESH} ---\n")
t0 = time.time()
try:
    with calib.Listener([0, 1]) as L:
        L.measure(1.0)
        sec_a = sec_b = 0.0
        sec_l = -99.0
        cur = -1
        while limit is None or time.time() - t0 < limit:
            st = L._read_frame()
            if st is None:
                break
            a = b = 0.0
            for c, name in ((0, 'a'), (1, 'b')):
                L.models[c].predict(np.ascontiguousarray(st[:, c]))
                s = float(L.models[c].prediction_buffer[L.model_key][-1])
                if c == 0: a = s
                else: b = s
            lvl = 20*np.log10(max(float(np.sqrt(np.mean((st[:,1].astype(np.float64)/32768)**2))), 1e-9))
            sec_a, sec_b, sec_l = max(sec_a, a), max(sec_b, b), max(sec_l, lvl)
            now = int(time.time() - t0)
            if now != cur:
                if cur >= 0:
                    hit = "  ** WOULD FIRE **" if max(sec_a, sec_b) >= THRESH else ""
                    bar = "#" * min(40, int(sec_b * 40 / max(THRESH, 0.01)))
                    line = f"{cur:7d}s  {sec_a:6.3f}  {sec_b:6.3f}  {sec_l:8.1f}  {bar}{hit}"
                    print(line, flush=True)
                    log.write(line + "\n"); log.flush()
                cur, sec_a, sec_b, sec_l = now, 0.0, 0.0, -99.0
except KeyboardInterrupt:
    print("\nstopped")
