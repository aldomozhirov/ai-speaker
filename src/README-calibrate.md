# `ai-speaker-calibrate.py`

Ad-hoc health check and mic/wake-word sensitivity calibration for the AI
speaker (ReSpeaker XVF3800 + openWakeWord). Reproduces, as a repeatable
command, the manual measurement process originally used to tune this system
by hand.

## Why this exists

Wake-word sensitivity depends on things that change: which USB capture
channel performs best, how far the noise floor sits from a real "Hey
Jarvis," and whether the echo canceller is actually converging when music
plays. All of that was previously measured by hand — start a capture
stream, read raw openWakeWord scores off it, compare channels, eyeball a
threshold. This script does the same measurements, but as a script you can
re-run whenever something changes, rather than a one-off investigation.

Run it after:

- The ReSpeaker is unplugged/replugged or moved to a different spot.
- The speaker itself is repositioned.
- The room changes (furniture, new noise source, different acoustics).
- Wake-word detection just starts feeling worse for no obvious reason.
- Any XVF3800 chip-register weirdness is suspected (see
  `AUDIO_MGR_FAR_END_DSP_ENABLE` below — it resets on every power cycle).

## What it does *not* do

It doesn't touch CamillaDSP EQ, doesn't retrain or replace the openWakeWord
model, and doesn't change anything about the voice-agent conversation logic
— it's scoped to the mic-in / wake-word-detection side only.

## Usage

```
ai-speaker-calibrate.py check          # fast, non-interactive health check
ai-speaker-calibrate.py wake           # interactive wake-word calibration
ai-speaker-calibrate.py wake --music   # ...with music playing (the hard case)
ai-speaker-calibrate.py all            # check, then wake in silence + music
ai-speaker-calibrate.py wake --apply   # ...and write the result into the unit file
```

Useful flags on `wake`/`all`:

- `--attempts N` — how many "Hey Jarvis" utterances to prompt for per phase
  (default 5).
- `--channels 0,1` — which XVF3800 USB capture channels to measure
  (default both; they are independently configured, not a stereo pair, so
  measure both unless you already know one is dead).
- `--quiet-seconds N` — how long to sample the noise floor (default 15;
  raise it in a room with intermittent noise — a short sample understates
  the real floor).
- `--apply` — write the recommended `WAKE_WORD_THRESHOLD` and
  `WAKE_WORD_CHANNEL` into `wake-word.service` and restart it. Without this
  flag the script only prints a recommendation.

### `check`

Fast, non-interactive. Verifies the things in this stack that fail
*silently* — the failure mode is not "an error," it's "wake-word
detection just quietly gets worse":

- ReSpeaker present on USB.
- `AUDIO_MGR_FAR_END_DSP_ENABLE` reads 1. This register lives in chip RAM
  and resets to 0 (echo cancellation structurally off) on every power
  cycle — everything else in the pipeline can look completely healthy
  with this one silently off.
- `AEC_AECCONVERGED` reads 1 (warning, not failure, if 0 — this is normal
  if nothing has played recently; it needs real program audio to
  converge).
- Mic gain / AEC reference gain, informational.
- The AEC reference feed is correctly wired to the XVF3800's playback
  input (and specifically checks for the known WirePlumber
  fallback-to-aloop-sink failure mode, which is a self-referential loop
  back into its own capture source).
- The XVF3800's processed capture source is present in the PipeWire graph.
- All 6 core services are active.
- systemd lingering is enabled (required for these services to start at
  boot without a login).

Every failure line includes the fix command.

### `wake`

Interactive. Opens one continuous capture stream from the XVF3800's
processed (post-AEC/beamform) output and scores it live with one
openWakeWord model per channel being measured, so multiple channels are
calibrated in a single pass without losing streaming context between
phases. For each phase it:

1. Measures the background noise floor for `--quiet-seconds` (stay quiet).
2. Prompts you to say "Hey Jarvis" `--attempts` times, from your normal
   distance/position — not up close.
3. Reports, per channel: noise floor, each utterance's peak score, the
   weakest utterance, and the separation ratio between them.

`wake-word.service` is stopped for the duration of the test (it would
otherwise start real voice-agent sessions off your test utterances) and is
always restarted afterward, including on error or Ctrl-C.

With `--music`, it repeats the whole measurement with music playing and
prints a direct comparison against the quiet-room numbers — this is the
harder, more realistic condition, since the AEC has to remove the
speaker's own output from the mic signal before openWakeWord ever sees it.
If `AEC_AECCONVERGED` reads 0 during the music phase, it calls that out
specifically as the likely cause of music-only wake-word failure.

The final recommendation prefers the music-phase result when it still has
usable separation (≥2x), since that's the harder condition to tune for;
otherwise it falls back to the quiet-room number.

### `all`

Runs `check` first and aborts if any problems are found — calibrating
against a broken signal path (e.g. echo cancellation silently off)
produces a number that looks fine but is measuring the wrong thing. If the
check passes, continues straight into `wake --music`.

### Reading the recommendation

The suggested threshold sits between the noise floor and the weakest
utterance, weighted toward the utterance (not a plain midpoint) — a short
silence sample tends to understate the real-world noise tail (a cough, a
door, a phrase from a podcast), so tuning too close to a low measured floor
buys false sensitivity rather than real range. It's also clamped to
`[0.20, 0.60]`: below 0.20, openWakeWord starts firing on ordinary speech
regardless of what was measured.

Three outcomes are reported explicitly rather than always producing a
number:

- **Usable gap** — a threshold is printed and can be applied.
- **Poor separation** (< 2x between floor and weakest utterance) — no
  threshold is both reliable and false-trigger-free; the fix is physical
  (move the speaker, reduce the noise source), not a config change.
- **Utterances too weak to tune around** — the *recommended* threshold
  would land at or above the weakest utterance actually measured, meaning
  it would silently never fire on your quietest attempts. Reported as a
  warning rather than applied blindly.

## Notes

- Runs under the `wake-word` venv (needs `openwakeword`, `numpy`); shells
  out to the `xvf3800` venv (via `xvf_register.py`) for register reads,
  since `pyusb` only lives there.
- `--apply` edits `~/.config/systemd/user/wake-word.service` directly via
  targeted regex substitution on the `Environment=WAKE_WORD_THRESHOLD=...`
  and `Environment=WAKE_WORD_CHANNEL=...` lines, saves the previous file to
  `wake-word.service.bak` first, then runs `daemon-reload` + `restart`.
- See `docs/ in the ai-speaker repository` for the broader system
  history this script's measurements are built on (why the AEC reference
  routing needed hardening, why `AUDIO_MGR_FAR_END_DSP_ENABLE` needs
  reapplying, etc).
