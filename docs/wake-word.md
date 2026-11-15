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

The window is first normalised to half of full scale (gain at most 16×,
`WAKE_WORD_SECOND_OPINION_MAX_GAIN`). Since 2026-10-18: before that, a quiet
or far "Hey Jarvis" reached both engines too faint to be heard at all.

Either one is enough. `WAKE_WORD_SECOND_OPINION=block` (since 2026-10-09):
the session is opened only on agreement; `log` only journals the verdict; `0`
switches it off.

Measured over 485 captures (2026-10-08): it keeps 34 of 42 real wake-ups and
passes 0 of 62 known false ones; on the first live day it blocked 10 of 10
false wakes and passed 2 of 2 real ones. **Cost: about one real wake-up in
five is blocked.** That figure did not hold: from 13 Oct the blocked share
grew, and on 17–18 Oct 25 of 26 wake-ups were blocked, by the owner's account
mostly real. Normalising the window (2026-10-18) recovers 6 of those 26 in
replay and lifts the hand-sorted real ones from 30 to 37 of 39, with false
ones still 0 of 61 that can reach this stage. If someone reports that the speaker ignores them, look for
"Wake word blocked by the second opinion" in the journal first.

The voice agent does not wait idle for this verdict: on "voice-pending" it
opens the mic immediately and buffers, and starts the session (chime, Gemini)
on "voice-confirm" or after `VOICE_CONFIRM_TIMEOUT_SECONDS` (1.5 s). A failed
judgement counts as a confirmation.

## In daily use: 18 Oct – 15 Nov 2026

Collected from the journal with `tools/wake/wake-events-report.py` (28 days
since the normalised second opinion went live). Labels are heuristics from
what each session did, not confirmed by listening; the per-event lists are in
`~/.local/state/ai-speaker/measurements/wake-events-2026-11-15/` on the
device (they contain household speech and stay there).

| | Count | Per day |
|---|---|---|
| Wake events logged (score > 0.05) | 6 544 | ~234 |
| Rejected by the verifier (score ≥ 0.10) | 2 741 | ~98 |
| Triggers that passed the verifier | 506 | ~18 |
| … blocked by the second opinion | 390 (77 %) | ~14 |
| … confirmed, voice session opened | 115 | ~4 |
| Sessions started with the button | 22 | |

**False wake-ups.** Of the 113 voice sessions:

| Outcome | Sessions | Verifier median |
|---|---|---|
| Did something (tool call or closing phrase) | 64 | 0.98 |
| Nothing intelligible heard, no tool, idle timeout | 24 | 0.81 |
| … the same, but failed triggers just before it (the owner trying) | 6 | 0.72 |
| Cancelled with the button within 15 s | 5 | 0.77 |
| Speech but no tool call | 14 | 0.87 |

Of the 14 with speech, reading the transcripts: 9 are room conversation
(Russian and English talk, once a fairy tale read aloud) picked up as a
command, 2 heard only a single word and noise, 1 was a real command ("включи группу кино", 10 Nov) that Gemini
answered without calling a tool, 2 are unclear. So roughly 40 unwanted
sessions in 28 days, about 1.4 a day, against 64 useful ones. The 24 empty
sessions are either false wake-ups or commands Gemini did not hear (see
README, far-field over music); without the audio the two cannot be told
apart, and most of their captures were already rotated away.

A stricter verifier would not separate them cleanly: at 0.8 it keeps 57 of 64
useful sessions and still passes 15 of 29 empty/cancelled ones; at 0.9, 50 of
64 and 8 of 29.

**Misses.** The second opinion blocks about 14 triggers a day and Vosk scored
0.00 on 385 of the 390; microWakeWord reached 0.2 on 16. How many of them were
real cannot be read from the journal alone. Lower bounds from the pattern of
retries:

- 9 blocked triggers came within 45 s before a session that was then
  wanted (did something, heard nothing, or started with the button), and
  24 more came in pairs within 30 s with no session after (12 pairs:
  repeated attempts that gave up, or a burst of speech).
- 13 triggers rejected by the verifier came within 45 s before a wanted
  session; one scored 0.963 on openWakeWord and 0.25 on the verifier.
- 24 near-misses (0.05–0.10) came within 45 s before a wanted session.
- 14 of the 22 button sessions were music requests that worked: the owner
  going round the wake word. The other 8 were cancelled or heard nothing.

Blocked triggers peak at 19–20 h, when the household talks most; their
verifier median is 0.67 (useful sessions 0.98).

Two more readings of the same journal (15 Nov):

- The detector's own score for the 63 useful sessions: median 0.37, 17 below
  0.20, 7 below 0.15, against the 0.10 threshold. Real wake-ups sit on the
  threshold, so some must fall below 0.05 and leave no trace.
- The second opinion judges every event at 0.10 or more, also those the
  verifier rejects. It heard "Jarvis" in 167 such events (125 separate
  utterances, 65 of them not inside a burst of talk). Whether those were real
  wake-ups from a voice the verifier does not know, or the second opinion's
  own mistakes on room speech (in replay it passes about 3 % of it), is not
  known: nobody has listened to them.

**What happens next:** the owner agreed on 15 Nov to replace the weak first
stage with a model trained on this household instead of tuning the three
stages further. The steps are in
[plans/wake-word-custom-model.md](plans/wake-word-custom-model.md).

**Evidence is short-lived.** The 200 MB capture cap holds only about two days:
356 of the 390 captures on disk on 15 Nov were near-miss captures (score
0.05–0.10), and the oldest was from 13 Nov. The
captures of the flagged events of 13–15 Nov (38) were copied next to the
report; everything older survives only as journal lines. Run the report
again before the next investigation, it copies whatever is still on disk.

## Captures

With `WAKE_WORD_CAPTURE=1`, every event where the score passes 0.05, and every
button press, saves both channels from 3 s before to 5 s after (10 s for the
button) plus a JSON file with the per-frame scores, what was decided, and
whether music was playing, into `~/.local/share/ai-speaker/wake-captures/`.
These recordings are the basis for every tuning decision: thresholds,
channel, gain and the verifier were all chosen on them, not on journal lines.
They are the household's voice and never leave the device (not in git).

Rotation since 2026-11-15, in two pools: near-misses (score 0.05–0.10, nine
in ten files) beyond 200 MB, a few days' worth; everything else beyond
4 000 MB, about two months. Before, one shared 200 MB cap let the near-misses
push every detection out within two days. Labelled captures go to `kept/`,
which is never rotated.

## Feature log and playback context

Since 2026-11-15, for the [custom-model plan](plans/wake-word-custom-model.md):

- **Feature log** (`wake_features.py`): openWakeWord's 96 speech features of
  every 80 ms frame and the frame's score, as float16 rows in
  `~/.local/share/ai-speaker/wake-features/YYYYMMDD-HHMMSS.f16` (one file an
  hour or per capture restart; row n is n × 80 ms after the time in the
  name; any 16 consecutive rows are the window a model scores;
  `wake_features.read()` loads one). About 210 MB a day, kept about eight
  weeks. It is not audio and cannot be played back, but it is derived from
  the room's sound and stays on the device. It lets any new model be checked
  against weeks of what the speaker really heard.
- **Playback context** (`wake_context.py`): every 3 s it reads from PipeWire
  whether a music stream is running and the volume; each `wake event:` line
  ends with `music=yes music_s=42 volume=0.34` (or `music=no …`), and each
  capture's JSON has the same under `context`.

Cost, measured on a second instance next to the live one: about 0.5–0.9 % of
one core on top of ~12 %.

## Tuning and retraining

```bash
# What happened to every wake event since a date: false wake-ups, misses,
# per-day counts, music at the time; copies the flagged captures
tools/wake/wake-events-report.py --since "2026-10-18 16:27"

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
