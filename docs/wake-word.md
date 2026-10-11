# Wake word

"Hey Jarvis" is detected by `wake_word.py` (`wake-word.service`) in three
stages, because no single detector was both sensitive enough for this voice and
quiet enough with people talking in the room.

```
mic ch1 ─▶ openWakeWord ──score ≥ 0.10──▶ verifier ──agrees──▶ "voice-pending" ──▶ voice-agent opens the mic
                                                     │
                                                     └──▶ second opinion (Vosk OR microWakeWord) ──▶ confirm / reject
```

## Stage 1: openWakeWord

- Model `hey_jarvis` (pretrained, ships with openwakeword 0.4.0), 16 kHz input
  resampled from the XVF3800's channel 1 (`WAKE_WORD_CHANNEL=1`), scored every
  80 ms.
- Threshold `WAKE_WORD_THRESHOLD=0.10`, patience 1 frame. This voice scores
  0.12–0.5 typically (up to 0.99 close by); the threshold is low on purpose
  and the next two stages take care of false triggers.
- One utterance gives one detection: after a trigger the detector is held for
  `WAKE_WORD_REFRACTORY` (2 s) and re-arms only once the score has fallen below
  half the threshold. (Before 2026-10-04 one "Hey Jarvis" could fire up to ten
  times.)
- Capture is read from `pw-cat --record --raw` targeting the XVF3800 node by
  name. A watchdog restarts the capture if audio stalls for 10 s and reports
  a mic delivering digital silence (`wake-word-health.json`, read by
  `ai-speaker-status.sh`).

## Stage 2: the verifier

A logistic regression on openWakeWord's own embedding window, trained on this
household's captures: real "Hey Jarvis" against everything else that made the
base model fire (meetings, videos, music). Without it, ordinary speech in the
room triggered about once a minute at scores (0.10–0.50) indistinguishable from
real ones (0.12–0.81).

- Model: `~/.local/share/ai-speaker/wake-verifier/hey_jarvis_v0.1.pkl`
  (`WAKE_WORD_VERIFIER`; `0` turns the stage off).
- Passes at `WAKE_WORD_VERIFIER_THRESHOLD=0.5`.
- Trained on 39 positive / 62 negative captures (2026-10-05). Offline:
  93 % of real kept, 0 % of known false passed, about 23 % of unseen kinds of
  sound passed.
- **Limitation:** only one person's voice is in the positives. Someone else's
  "Hey Jarvis" may be rejected. Add their captures and retrain.

## Stage 3: the second opinion

`wake_confirm.py`. After a trigger passes the verifier, the last 3 s of audio
are checked by two independent detectors on channel 1:

- Vosk (`vosk-model-small-en-us-0.15`) with the grammar
  `["hey jarvis", "jarvis", "[unk]"]`, confidence ≥ 0.5, or
- microWakeWord's "Hey Jarvis" model at ≥ 0.5 (its own default is 0.97).

Either one is enough. `WAKE_WORD_SECOND_OPINION=block` (since 2026-10-09):
the session is opened only on agreement; `log` only journals the verdict; `0`
switches it off.

Measured over 485 captures (2026-10-08): it keeps 34 of 42 real wake-ups and
passes 0 of 62 known false ones; on the first live day it blocked 10 of 10
false wakes and passed 2 of 2 real ones. **Cost: about one real wake-up in
five is blocked.** If someone reports that the speaker ignores them, look for
"Wake word blocked by the second opinion" in the journal first.

The voice agent does not wait idle for this verdict: on "voice-pending" it
opens the mic immediately and buffers, and starts the session (chime, Gemini)
on "voice-confirm" or after `VOICE_CONFIRM_TIMEOUT_SECONDS` (1.5 s). A failed
judgement counts as a confirmation.

## Captures

With `WAKE_WORD_CAPTURE=1`, every event where the score passes 0.05, and every
button press, saves both channels from 3 s before to 5 s after (10 s for the
button) plus a JSON file with the per-frame scores and what was decided, into
`~/.local/share/ai-speaker/wake-captures/`. The oldest files are deleted beyond
200 MB. These recordings are the basis for every tuning decision: thresholds,
channel, gain and the verifier were all chosen on them, not on journal lines.
They are the household's voice and never leave the device (not in git).

## Tuning and retraining

```bash
# Score real captures at several thresholds / gains (offline, no hardware)
~/.venvs/wake-word/bin/python tools/wake/wake-replay.py score ~/.local/share/ai-speaker/wake-captures --thresholds 0.05,0.1,0.2

# Regression test of the detection loop with synthetic clips
~/.venvs/wake-word/bin/python tools/wake/wake-replay.py selftest

# Retrain the verifier: copy captures into wake-verifier/positive or /negative, then
OMP_NUM_THREADS=1 nice -n 19 ~/.venvs/wake-word/bin/python tools/wake/wake-verifier-train.py
systemctl --user restart wake-word
```

Run heavy offline work with `nice -n 19` and `OMP_NUM_THREADS=1`: an unniced
training run once pushed the load to 8 next to the live audio.

Threshold history: 0.5 (code default) → 0.35 (2026-08-22) → 0.25 (2026-08-23) →
0.10 (2026-08-24). Lowering to 0.05 is supported by the capture data (six of
seven logged near-misses at 0.05–0.10 were real attempts) and was offered on
2026-10-04; the owner chose to stay at 0.10. Misses below 0.05 are not logged.

## Engines compared

openWakeWord stays the primary detector. Benchmarked on 42 real wake-ups and
62 false ones from this device (2026-10-08, `tools/wake-bench/`):

| Engine | Real wake-ups heard | Notes |
|---|---|---|
| Porcupine 1.9.5 (keyless) | 11–31 | Picovoice's free tier ended 2026-06-30; the old keyless build only loads on the Pi 5 by importing its module directly |
| microWakeWord | 6 at its 0.97 cutoff, 21 at 0.5 | Strong as a second opinion |
| sherpa-onnx KWS | 5 | |
| Vosk with a "hey jarvis" grammar | 27 | Strong as a second opinion |

None replaces openWakeWord, but the pair Vosk-or-microWakeWord is an
excellent filter; that became stage 3. The obvious next step, if needed, is a
custom openWakeWord model trained on this voice.

## Offline commands (off)

`local_intent.py` / `local_actions.py` can match simple commands ("pause",
"volume up", "set a timer for ten minutes") offline with a Vosk grammar,
without opening a Gemini session. It is **off** (`LOCAL_INTENT_ENABLED=0`)
since 2026-10-04: it delayed every session by its 1–6 s listening window, the
audio it heard never reached Gemini, and its English-only grammar matched 0 of
93 real commands (most are in Russian). Timers, alarms, music and volume all
work through Gemini's tools.
