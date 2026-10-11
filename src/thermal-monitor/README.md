# Thermal monitor

Investigation into Raspberry Pi 5 (`ai-speaker`, <pi-address>) thermal
throttling: how much it throttles under load, whether the stock cooling is
adequate, and how temperature behaves over a full day of normal use.

## Findings so far

- **Cooling is undersized.** Confirmed by a real stress test: idle-ish
  (~10-25% CPU) already hits `vcgencmd get_throttled = 0xe0006`
  (frequency-capped + throttled) at 84-86°C. Under a 4-core stress ramp
  (2.5 min), temp hit 92°C, throttled 97.5% of samples, CPU%↔temp
  correlation 0.85 (strong).
- **Not a power problem.** No under-voltage flag was ever seen — this is a
  heatsink/fan inadequacy, not a PSU/cable issue.
- **Verdict:** better cooling is needed; the open question is *how much*
  (stock passive case vs. active cooler/fan), not *whether*.

## What's here

- `collector.py` — samples CPU temp, RP1 (southbridge) temp, per-core CPU%,
  avg freq, load average, and all four `vcgencmd` throttle flags
  (under-voltage / freq-capped / throttled / soft-temp-limit) to CSV every
  N seconds. Can be run standalone (`nohup python3 collector.py ...`).
- `stress.py` — built-in multiprocessing CPU burner for controlled load
  steps (no `stress-ng` install needed).
- `analyze.py` — reads a stats CSV and writes a self-contained
  `report.html` (dark theme, inline canvas chart, no CDN/network deps):
  summary stats, Pearson correlation, an hourly breakdown table (temp/CPU/
  throttle % per hour of day), and a downsampled chart so day-plus runs
  stay legible.
- `server.py` — combines the collector and a local web dashboard in one
  process: a live-updating page (current temp/CPU/throttle flags + a
  rolling ~1h chart) and a `/history` page (same report as `analyze.py`,
  generated fresh from the CSV on each request). Pure stdlib + `psutil`,
  no extra dependencies.
- `stats.csv`, `report.html` — demo data/report from the initial stress
  test above.
- `stats_live-<week-start-date>.csv` — ongoing data from the running
  dashboard service, one file per ISO calendar week (ISO week's Monday
  date as suffix, e.g. `stats_live-2026-08-17.csv`). `server.py` rotates
  to a new file automatically at each week boundary so no single CSV
  grows unbounded; old weeks' files are kept as-is (not auto-deleted) and
  can be fed to `analyze.py` individually. `/history` on the dashboard
  always reflects the current week's file only.

Everything is pure stdlib + `psutil` — no `pandas`/`matplotlib`/network
dependency, on purpose, so it runs on a bare Pi image.

## Running it

Standalone one-off report:
```
python3 collector.py --interval 5 --out stats.csv &
python3 stress.py --cores 4 --duration 120   # optional controlled load
python3 analyze.py --csv stats.csv --out report.html
```

Live dashboard (what's actually deployed, see below):
```
python3 server.py --interval 5 --csv stats_live.csv --host 0.0.0.0 --port 8000
```
Open `http://<pi-ip>:8000/` for the live view, `/history` for the full
report. Binds to `127.0.0.1` only unless `--host 0.0.0.0` is given —
`0.0.0.0` has no auth, fine on a trusted home LAN only.

## Deployment

Runs as a systemd **user** service (no root needed — the Pi user account
already has `video`/`gpio` group access for `vcgencmd`):

- Unit file: `~/.config/systemd/user/thermal-monitor.service`
- `loginctl enable-linger aldom` was run so the user's systemd instance
  (and this service) starts at boot even without an interactive login.

Useful commands:
```
systemctl --user status thermal-monitor
systemctl --user restart thermal-monitor   # after editing server.py
journalctl --user -u thermal-monitor -f    # tail logs
```

Reachable on the LAN at `http://<pi-address>:8000/` (this Pi's current
`wlan0` IP — check `hostname -I` if it changes).

## Open items / ideas not yet done

- **Old weekly files aren't pruned.** Rotation caps each file at one week
  but doesn't delete anything, so disk usage still grows slowly forever.
  Not an issue for a long time (CSV is tiny), but could add a retention
  policy (e.g. keep last N weeks) later if it matters.
- **Before/after cooling comparison**: rerun `stress.py --cores 4
  --duration 120` with the collector running, once on current cooling and
  once after swapping the heatsink/fan, to quantify which upgrade is
  actually enough — not yet built as a repeatable script.
- **Reboot resilience of the *collector* itself** is solved (systemd +
  linger); resilience of *data continuity* across reboots (e.g. marking
  gaps, or resuming into a new dated file per day) is not handled.
