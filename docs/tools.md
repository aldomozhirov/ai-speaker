# Tools and tests

Installed with the speaker (`~/.local/bin`):

| Tool | Use |
|---|---|
| `ai-speaker-status.sh` | One-shot health check: run after every reboot and audio change |
| `ai-speaker-calibrate.py check \| wake [--music] \| all` | Registers and routing; interactive wake-word measurement per channel (see `src/README-calibrate.md`) |
| `xvf_register.py get\|set\|ensure NAME [VAL…]` | Read/write XVF3800 registers (run with `~/.venvs/xvf3800/bin/python`); refuses write-only reads |
| `wake-live.py` | Live wake-word score meter: speak and watch the number |
| `wake-scores.sh` | Recent wake-word scores from the journal |
| `duck.py` | Shows which streams a voice session would duck, and their volume |
| `spotify_web.py auth` | One-time Spotify Web API authorisation |

Run from the checkout (`tools/`), not installed. Most need a venv's Python;
the docstring at the top of each file has the exact usage. Heavy ones: run
with `OMP_NUM_THREADS=1 nice -n 19`.
Measurement results and kept recordings go to
`~/.local/state/ai-speaker/measurements/`, outside the repo.

## Echo cancellation (`tools/aec/`)

| Tool | Audible? | Answers |
|---|---|---|
| `aec-align-check.py [--session-switch new,new] [--compare d1,d2]` | Yes (~50 s of noise) | Where the reference sits relative to the echo, and how many dB are removed. The reference measurement |
| `aec-echo-track.py` | No (analyses the last run) | Did the echo stand still during that run? |
| `aec-ref-timing-probe.py lead \| play` | No | Is the reference timing steady, and how far ahead of the echo is it? |
| `aec-ref-diag.py` | No | A second look at one burst, ±300 ms search, keeps the capture |
| `aec-start-spread.py` | No | How much the lead varies from one start of the XVF3800 playback to the next |
| `dsp-switch-check.py units \| live \| session` | No | Does a voice session's DSP switch leave the speaker path's timing alone? |
| `aec-test-cpu-load.sh [CPUS] [SECONDS]` | No | Background load for timing tests |
| `aec-delay-sweep.py`, `aec-erle-sweep.py`, `aec-gain-sweep.py`, `aec-refcheck.py`, `aec-refpath.py` | Yes | The 2026-08-24 investigation harnesses. Kept for the record: the "residual" they measure is AGC'd and does not show cancellation depth (see CHANGELOG 2026-10-04) |

## Wake word (`tools/wake/`, `tools/wake-bench/`)

| Tool | Answers |
|---|---|
| `wake-replay.py selftest` | Regression test of the detection loop with synthetic clips: one utterance, one detection; capture recorder; mic watchdog |
| `wake-replay.py score DIR [--thresholds …] [--gain-db …]` | Peak score per channel of real captures, and what each threshold would do |
| `wake-verifier-train.py [--report-only]` | (Re)train the household verifier from sorted captures |
| `second-opinion-check.py` | Replay captures through openWakeWord + second opinion as the live loop does |
| `wake-bench/bench.py`, `report.py`, `loosen_eval.py` | The 2026-10-08 engine comparison (needs its own venv with the candidate engines) |

`wake-test-clips/` are synthetic: Piper TTS voices saying "Hey Jarvis" and a
few commands, plus openWakeWord's sample clips. No household recordings are in
the repo.

## Voice sessions (`tools/session/`)

`voice-session-replay.py` runs the real session code against real Gemini with
a recording as the microphone; speaker, LEDs, CamillaDSP and tools are
stubbed.

```bash
~/.venvs/voice-agent/bin/python tools/session/voice-session-replay.py --module-dir ~/.local/bin capture FILE.wav --native --channel 1 --gain-db 12
~/.venvs/voice-agent/bin/python tools/session/voice-session-replay.py --module-dir src session --real-mic
```

`--module-dir` picks the code under test (the installed copy or the checkout).

## Unit tests (`tests/`)

```bash
cd tests
PYTHONPATH=../src ~/.venvs/wake-word/bin/python   -m unittest test_wake_confirm test_wake_trigger
PYTHONPATH=../src ~/.venvs/voice-agent/bin/python -m unittest test_tool_guard test_voice_agent_confirm
```

30 tests, a few seconds, no hardware touched.

## Repository maintenance

| Tool | Use |
|---|---|
| `install.sh` | diff / apply / capture / bootstrap ([operations.md](operations.md)) |
| `tools/private-backup.sh create \| restore` | Secrets and private data, outside git |
| `tools/one-off/import-workspace.sh` | The 2026-10-11 migration of the old workspace into this repo (kept for the record) |
