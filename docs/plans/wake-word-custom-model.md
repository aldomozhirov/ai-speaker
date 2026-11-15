# Plan: a wake-word model trained on this household

- **Status:** agreed by the owner on 2026-11-15. Stage A live since
  2026-11-15 14:56 (one check due 17 Nov). Tooling for B–G built and tested
  on 2026-11-15; next: the owner's recordings (B2) and labels (C3).
- **Tick a step off here** (`[x]`, with the date) in the commit that completes
  it, and put the measured result next to it.
- Background: [wake-word.md](../wake-word.md) (how it works now and the
  28-day figures), [CHANGELOG](../../CHANGELOG.md) (how it got this way).

## Goal

"Hey Jarvis" works reliably from a distance and does not open a session when
people merely talk nearby.

| Target (to be confirmed against the baseline of stage D) | Now |
|---|---|
| ≥ 95 % of wake-ups detected within 3 m in a quiet room | never measured |
| ≥ 85 % at 5 m, and over music at the usual volume | never measured |
| ≤ 1 unwanted session a week | about 10 a week (1.4 a day) |
| No position or condition worse than today | – |

These are goals, not predictions. Stage D gives the first real numbers; the
targets are revisited with the owner then.

## Why this and not more tuning

1. **The primary detector is the weak part.** The pretrained `hey_jarvis`
   model scores this household low: in the 63 useful sessions of 18 Oct –
   15 Nov the median peak was 0.37 and 17 were below 0.20, against a threshold
   of 0.10. Real wake-ups sit on the threshold; those below 0.05 leave no
   trace at all.
2. **At 0.10 ordinary room speech fires it about 116 times a day** (3 250
   events in 28 days), so two filters were stacked on top, and each adds
   misses.
3. **The filters hear worse than the detector they gate.** As primary
   detectors Vosk heard 27 and microWakeWord 21 of 42 real wake-ups
   (2026-10-08), yet together they hold the veto and block 77 % of what reaches
   them. The verifier knows 39 clips of one voice from 5 Oct.
4. **False wake-ups still leak** (about 1.4 a day) and no verifier threshold
   separates them from real ones.
5. **Already tried and ruled out:** input gain (does not raise the score),
   channel 0, four other engines as the primary detector, a stricter
   verifier, letting a confident verifier override the second opinion.
6. **The evidence is thin:** every setting rests on 42 real and 62 false
   clips from before 8 Oct; recall by distance was never measured; audio is
   kept for two days.

A model trained on the household's own voices and the room's own sound is the
"obvious next step" already named in D0005. It replaces the weak part instead
of adding a fourth patch. It is the same family as today's detector: a small
classifier ("head") on openWakeWord's speech features, so the live loop, the
capture tooling and the CPU cost stay as they are.

## Rules for the whole plan

- **Household audio, features, labels and trained models stay on the device.**
  None of it goes into git or to another machine. Training runs on the Pi.
- **Measure first, change second.** Every stage ends with a number written
  into this file.
- **New behaviour ships off or log-only first** (as the second opinion did).
- **Tuned values change only with the owner's yes.** The owner's decisions
  are marked **Owner:** below.
- Heavy work runs as `OMP_NUM_THREADS=1 nice -n 19 …`, and
  `ai-speaker-status.sh` is checked before and after.
- Usual repo rules: change the repo, `./install.sh apply user`, restart what
  it names; docs and CHANGELOG in the same commit; audio-touching changes are
  exercised on the live graph first ([extending.md](../extending.md#testing-audio-changes)).

## Overview

| Stage | What | Work | Owner's time | Elapsed |
|---|---|---|---|---|
| A | Keep the evidence: longer capture retention, feature log, context | build | – | days 1–2 |
| B | Ground-truth recordings at known distances | build + record | 2 × 20 min per person | day 3, day ~10 |
| C | Label captures | build + label | ~10 min, twice a week | from day 3 |
| D | Baseline: recall by distance, false wake-ups a day | report | read it | day ~11 |
| E | Train and evaluate offline | build + compute | go / no-go | days 12–16 |
| F | Shadow: new model runs log-only beside the old | build + 7 days | – | days 17–24 |
| G | Switch | settings | yes / no, then a week of use | day ~25 |
| H | Simplify the cascade; make leftover false wake-ups cheap | build | two decisions | after G |
| I | Microphone side | only if D or G ask for it | – | – |
| J | Keep it good: monthly retrain | routine | ~10 min a month | ongoing |

About four weeks to the switch, most of it waiting for data.

---

## Stage A — keep the evidence

**A1. Capture retention in two pools.** `src/wake_capture.py`
- [x] (2026-11-15) Captures whose peak is below `WAKE_WORD_THRESHOLD` (near-misses, nine in
  ten files today) rotate in their own pool: new setting
  `WAKE_WORD_CAPTURE_NEAR_MAX_MB`, default 200.
- [x] (2026-11-15) Everything else (score ≥ threshold, button, shadow firings of F1) uses
  `WAKE_WORD_CAPTURE_MAX_MB`, set to **4000** in `wake-word.service`
  (about 116 events a day × 0.52 MB ≈ 60 MB a day ≈ two months).
- [x] (2026-11-15) Unit test for the rotation in `tests/test_wake_features.py`; rows in
  [configuration.md](../configuration.md); the "Captures" paragraph of
  wake-word.md.
- [ ] Check: after two days, `du -sh` of the capture folder grows by about 60 MB
  a day and the oldest file with peak ≥ 0.10 is still there. (Due 17 Nov.)

**A2. Feature log.** New `src/wake_features.py`, hooked into
`Listener.ingest` in `src/wake_word.py`.
- [x] (2026-11-15) After every `predict()`, hand the newest feature row
  (`model.preprocessor.get_features(1)[0, 0]`, 96 numbers per 80 ms) and that
  frame's base score to a writer thread behind a bounded queue that drops
  rather than waits (same pattern as `wake_capture.py`: never block the
  capture loop).
- [x] (2026-11-15) Format: raw float16 rows of 97 values (96 features + score) in
  `~/.local/share/ai-speaker/wake-features/YYYYMMDD-HHMMSS.f16`. A new file
  starts every hour and at every capture (re)start, so the rows inside one
  file are exactly 80 ms apart and the file name gives the wall-clock time of
  row 0. Directory mode 0700. (Two files started in the same second: the
  second gets `_2`, which sorts after the first.) Also a new file after a
  dropped batch, so there is never a gap inside a file.
- [x] (2026-11-15) Settings: `WAKE_WORD_FEATURE_LOG` (default 0; **1** in the unit),
  `WAKE_WORD_FEATURE_LOG_DIR`, `WAKE_WORD_FEATURE_LOG_MAX_MB` (default 12000,
  oldest files deleted first). Size: 97 × 2 bytes × 12.5 a second ≈ 210 MB a
  day, so about eight weeks.
- [x] (2026-11-15) `wake-replay.py selftest`: a synthetic clip fed through the loop gives
  one logged row per frame with its score, and the last 16 rows equal
  `get_features(16)` at that frame (largest difference 0.016, float16
  rounding). Writer, segments, drops and pruning: `tests/test_wake_features.py`.
- [x] (2026-11-15) Live check before install (second instance, own directories): CPU of the
  wake-word process and `ai-speaker-status.sh` unchanged with the log on.
  Measured: 12.0 / 11.9 % of a core with the log and context off, 12.9 /
  12.5 % on (100 s each); status all OK.
- What it is for: any future model can be replayed over weeks of everything
  the speaker really heard, and its false alarms counted, without keeping
  speech. The features are not audio and cannot be played back; they still
  stay on the device.

**A3. Context on every event.**
- [x] (2026-11-15) Add to each capture's JSON and to the `wake event:` journal
  line: whether a music stream was playing (the same stream match `duck.py`
  uses), for how long, and the sink volume. As built: a thread of its own
  (`src/wake_context.py`) asks PipeWire every 3 s and caches the answer; the
  capture loop only reads the cache, so it still never waits.
- [x] (2026-11-15) `tools/wake/wake-events-report.py`: reads the fields, adds
  `music`/`music_s`/`volume` columns to `events.csv` and a "Music playing at
  the time" table (with "first 15 s of music") to `summary.md`.

**A4. Install and note the start.**
- [x] (2026-11-15) `./install.sh apply user`, restart `wake-word`, `ai-speaker-status.sh`
  (all OK).
- [x] Collection started **2026-11-15 14:56**.

---

## Stage B — ground-truth recordings

The only data that shows the wake-ups the speaker misses completely, and the
only way to get recall by distance.

**B1. Tool `tools/wake/wake-enroll.py`** (wake-word venv, run over SSH from a
phone or laptop in the room).
- [x] (2026-11-15) `wake-enroll.py positions --set name=metres:where …` stores
  them in `~/.local/share/ai-speaker/wake-enroll/positions.json`. Set from the
  owner's list: near 0.5 m, kitchen 3 m right, corridor 4 m behind, entrance
  6 m forward right, balcony 3 m left.
- [x] (2026-11-15) `wake-enroll.py record --person NAME --round N`:
  - refuses to start while a voice session is active;
  - stops `voice-agent` for its duration and starts it again in `finally`
    (so no session opens, nothing chimes, music is not ducked); `wake-word`
    keeps running, so the journal holds the live pipeline's verdict on every
    take;
  - records its own continuous 16 kHz two-channel stream from the XVF3800 node
    (by name, `pw_node.wait_for_node`, `pw-cat --record --raw`) to
    `wake-enroll/NAME-rN/<position>-<condition>.wav`;
  - for each position × condition: prints where to stand, waits for Enter,
    then cues 12 takes five seconds apart by lighting the LED ring
    (`listening` for one second on `assistant-led.sock`, then `idle`); no
    sound. Takes 1–8 in a normal voice, 9–10 quietly, 11–12 fast. Cue times
    (audio time and wall clock) go to `takes.json`. As built: after each
    block, `r` records that position again; an interrupted round resumes
    where it stopped; `--redo` records chosen blocks again;
  - conditions, one after the other across all positions: **quiet**; **music** from the speaker at the usual volume
    (the tool waits 20 s after it sees the music stream, for the echo
    canceller to converge, and refuses if nothing is playing); **talk** (a TV,
    podcast or conversation from another device, not from the speaker);
  - ends each block with 20 s in which nobody says the phrase ("bed":
    negatives, and background for mixing in E4); 15 blocks give 5 minutes.
- [x] (2026-11-15) 5 positions × 3 conditions × 12 takes = 180 takes; 83 s a
  block, about 26 minutes with the walking.
- [x] (2026-11-15) Row in [tools.md](../tools.md); the folder is listed in
  [operations.md](../operations.md) among the data that is never committed
  and in `.gitignore`. Checked with `--dry-run` (LEDs and voice-agent left
  alone): blocks, skip, redo, resume, cue times within 40 ms.

**B2. Round 1 (training).** **Owner**, and everyone else who uses the speaker.
- [ ] Date ____, people ____, takes ____.

**B3. Round 2 (test only), on a different day, about a week later.**
- [ ] Same positions. The training tool refuses to read any `-r2` folder;
  round 2 is used only by `wake-eval.py`.
- [ ] Date ____, people ____, takes ____.

**B4. Other voices through the real microphone (recommended, 2 × 15 min of
playback, nobody has to speak).**
- [x] (2026-11-15, as `tools/wake/wake-rerecord.py make --set A|B`; sets A
  and B never share a clip) writes a 15-minute WAV: 200 synthetic
  "Hey Jarvis" from the voices of E3, four seconds apart, with a sync chirp at
  the start and every minute.
- [x] (2026-11-15) Tool: `wake-rerecord.py record --set A --position … --round 1`
  stops voice-agent, records, and aligns every clip on the sweeps (a
  delayed, 100 ppm-drifting, noisy copy aligned to 0.0 ms). Takes carry the
  exact phrase end.
- [ ] **Owner** plays it from a phone or laptop speaker (not from the speaker
  itself) at two of the positions while `wake-enroll.py rerecord` records and
  cuts it into takes by the chirps.
- Why: synthetic voices otherwise never pass through the microphone array,
  its beamformer and the room; this covers guests and voices nobody recorded.

---

## Stage C — labels

**C1. One label file instead of folders.**
- [x] (2026-11-15) `~/.local/share/ai-speaker/wake-labels.jsonl`: one line per capture:
  file name, `real` / `false` / `unsure`, who spoke, when labelled. Captures
  are never moved.
- [x] (2026-11-15: 39 real, 62 false, 2 unsure imported) Import the existing hand-sorted clips (`wake-verifier/positive` 39,
  `negative` 62, `unlabelled-ambiguous` 2) and copy their audio into a
  `wake-captures/kept/` folder that rotation does not touch.
- [x] (2026-11-15) Any capture that gets a label is copied to `kept/` the same way.

**C2. Tool `tools/wake/wake-label.py`.**
- [x] (2026-11-15) Serves a page on the home network only while it runs, with a random
  token in the address printed on start; standard library only. Each clip
  plays in the browser, with three buttons and "who". Clips are played in the
  browser, not through the speaker (the speaker would hear itself).
- [x] (2026-11-15; 254 clips queued from 18 Oct on, built in ~8 s; checked at
  phone width) Queue order, most useful first:
  1. sessions (`ok`, `fp-*`, `empty-after-retry`) and button presses;
  2. blocked by the second opinion (about 14 a day);
  3. heard by the second opinion but rejected by the verifier (125 utterances
     in the 28 days, never looked at; possibly real wake-ups from a voice the
     verifier does not know);
  4. retries (`miss-*`, `near-retry`);
  5. a random 200 of the verifier-rejected ones (to confirm they are false).
- [x] (2026-11-15, as a `labelled` column and a "guess against label" table)
  `wake-events-report.py` reads the label file and uses a label instead of
  its heuristic where one exists.

**C3. Label.** **Owner**, about 10 minutes twice a week while data collects.
- [ ] First pass: the 38 flagged captures of 13–15 Nov and whatever else is
  still on disk. Count labelled: ____.

**C4. Optional interim relief** (only if the wait is too painful; it changes
the baseline, so D must be measured first).
- [ ] Retrain the existing verifier on all labels so far:
  `wake-verifier-train.py --report-only`, install only if the held-out figures
  beat the current ones. **Owner:** yes / no.

---

## Stage D — baseline

**D1. Tool `tools/wake/wake-eval.py`** (wake-word venv).
- [x] (2026-11-15, tool; tested on a made-up round) `wake-eval.py baseline`: for every take of rounds 1 and 2, from the
  journal inside the take's five seconds: base peak, verifier verdict,
  second-opinion verdict, would a session have opened. Output: a table
  position × condition × person with the share detected at each stage, and
  the takes that left no journal line at all (score below 0.05).
- [ ] False wake-ups a day and blocked-but-real a day over the collection
  weeks, from `wake-events-report.py` with the labels of C.
- [ ] Put both tables into wake-word.md ("In daily use") and the headline
  numbers into the "Now" column above.
- [ ] **Owner:** confirm or change the targets.
- [x] (2026-11-15, in the baseline's offline table) From the same recordings, for stage I: base peak per take on channel 0
  against channel 1, by position.

---

## Stage E — train and evaluate offline

**E1. Environment.**
- [x] (2026-11-15; installed and working: torch 2.15.0+cpu, piper-tts 1.8.0,
  onnx 1.23.2, onnxruntime 1.32.0) `python3 -m venv ~/.venvs/wake-train`; `deps/requirements-wake-train.txt`
  with pinned `torch` (CPU), `onnx`, `piper-tts`, `openwakeword==0.4.0`,
  `numpy`, `scipy`. A tool venv like `tools/wake-bench`: not part of
  `bootstrap-user`. (Checked 2026-11-15: pip offers torch 2.15.0 and piper-tts
  1.8.0 for this Pi; neither has been installed or run here.)
- [x] (2026-11-15; the 185 MB file is in, the 17.28 GB one downloading,
  rate-limited; RIRs are generated instead of downloaded) Download to `~/.local/share/ai-speaker/wake-train/public/`:
  `openwakeword_features_ACAV100M_2000_hrs_16bit.npy` (17.28 GB, about 2 000
  hours of general speech, music and noise as features) and
  `validation_set_features.npy` (185 MB, about 11 hours) from
  `huggingface.co/datasets/davidscripka/openwakeword_features`; plus a free
  set of room impulse responses for E4. Sizes checked 2026-11-15; layout and
  number type to be checked on load.

**E2. Two checks before any training** (an hour; they can stop the plan
early and cheaply).
- [x] (2026-11-15: **pass.** Overall mean 1.94 / 1.67, spread 15.6 / 15.0,
  ranges −69…85 / −62…73, per-feature means correlate 0.95. The public set
  is (5 625 000, 16, 96) float16 windows; the validation set (481 345, 96)
  float32 rows.) The public features and the room's own log (A2) are on the same scale:
  per-feature mean and spread of a day of each, side by side. If not, find
  the conversion before going on.
- [x] (2026-11-15: **pass**, `wake-model-train.py check`: both heads in one
  `Model`, the custom head's live score equals the offline score exactly
  after the 16-frame warm-up; 5.9 ms a frame for both) openwakeword 0.4.0 loads a second, custom ONNX head next to
  `hey_jarvis` in one `Model(wakeword_model_paths=[…, …])` and returns both
  scores from one `predict()`. Test with a dummy head exported from torch. If
  it does not: evaluate the head directly with onnxruntime on
  `get_features(16)`, the way `Verifier.confidence` does today.

**E3. Synthetic speech.** `tools/wake/wake-model-train.py synth`
- [x] (2026-11-15, tool: en_US-libritts_r (904 speakers), en_GB-vctk (109),
  four Russian voices; one worker makes about 2 clips a second) Positives: "Hey Jarvis" from every free Piper voice that fits, English
  (including the multi-speaker ones) and Russian voices saying the English
  phrase and "хей джарвис"; speed and expressiveness varied. Target 5 000
  clips.
- [x] (2026-11-15, tool) Near-phrases, as negatives: 1 000 clips of things that sound alike
  ("hey Travis", "jar of", "harvest", "nervous", "hey Jarvis" cut after the
  first word, and Russian words the labels of C show firing).
- [x] Clips go to `wake-train/synth/` (not household audio, but too large for
  git).

**E4. Features.** `wake-model-train.py features`
- [x] (2026-11-15, tool; real recordings get as-is, −6 dB, +6 dB and over room
  sound instead of reverb, since they already carry the room) Every positive clip (real and synthetic) in four versions: as is; with
  a room impulse response; mixed over a "bed" of B1 or over a false-labelled
  capture at 0–20 dB signal-to-noise; both. Real clips also at −6 and +6 dB.
- [x] (2026-11-15, changed: **streaming** features, as live, because the
  batch path differs by up to 10 on a scale of 60; clips sit at random
  sample offsets in the stream, which varies the frame phase) Features with openWakeWord's own preprocessor at the four frame phases
  the verifier trainer already uses (0, 320, 640, 960 samples).
- [x] (2026-11-15) Which windows of a positive count as "the wake word": those ending 0 to
  0.4 s after the end of the phrase. End of the phrase: known for synthetic
  clips; found by energy in quiet takes; otherwise the peak of the pretrained
  score if it reached 0.05, else the peak of the first-pass model, confirmed
  in the labelling page. All other windows of a positive clip are left out
  (neither class).

**E5. Split, fixed before training and written to `split.json`.**

| | Train | Choose the threshold | Test (never trained on) |
|---|---|---|---|
| Real wake-ups | round 1, labelled `real` captures up to the cut-off date, re-recorded voices at one position | – | round 2, `real` captures after the cut-off, re-recorded voices at the other position |
| Room sound | feature log, first week; `false` captures up to the cut-off | feature log, second week | the shadow week of stage F; public 11-hour set |
| General | public 2 000 hours; synthetic near-phrases | – | – |

Room log rows within 5 s of a session, a button press, a `real` label, a
second-opinion pass or an enrolment session are left out of the negatives.

**E6. Train.** `wake-model-train.py train`, niced, one thread, overnight.
- [x] (2026-11-15, tool) Head: the 16 × 96 window flattened, three blocks of
  (linear 64, layer norm, ReLU), one output. Start from this (it is what
  openWakeWord's own trainer uses) and change it only on evidence.
- [x] (2026-11-15, tool; public and room samples of 300 000 windows each are
  held in memory, ~2 GB) Each batch: 256 positives, 2 048 negatives (half public, a quarter room
  log, a quarter hard negatives). After every pass, all room-log training
  windows are scored and those above 0.1 join the hard negatives.
- [x] (2026-11-15, tool; held out: one real source in five) Keep the checkpoint with the best recall on the training-side real
  clips held out by fold, at the threshold that gives at most one false alarm
  in the second week of room log.
- [x] (2026-11-15, tool; exported graph matches the network to 1e-5) Export `~/.local/share/ai-speaker/wake-model/hey_jarvis_home_v1.onnx`
  and a `.json` beside it: date, counts of each kind of data, split, chosen
  threshold and patience, the figures of E7. Not in git.
- [ ] Note the run time and the load on the Pi here: ____. (A 2 000-step trial
  on the October clips took 2.3 min on one thread; a full run is 30 000 steps.)

**E7. Evaluate.** `wake-eval.py model <onnx>`
- [x] (2026-11-15, tool; run once on a trial model) Threshold and patience: grid 0.3–0.9 × 1–3 frames on the second week of
  room log; take the best recall with at most one false alarm in that week.
- [ ] Report at that setting: recall on round 2 by position × condition ×
  person, next to the baseline of D; false alarms on the public 11 hours; the
  same for the pretrained model with and without today's two filters.
- [ ] Write the tables here and in the CHANGELOG.

**E8. Go / no-go.** **Owner.**
- [ ] Go if round-2 recall meets the targets (or beats the baseline clearly
  in every cell) with at most one false alarm in the validation week.
- If not: the report says which cells fail. More recordings there (B), more
  re-recorded voices (B4), or stage I, then E again.

---

## Stage F — shadow

**F1. Code.** `src/wake_word.py`
- [x] (2026-11-15; in the repo, default off) `WAKE_WORD_SHADOW_MODEL` (path, default off), `WAKE_WORD_SHADOW_THRESHOLD`,
  `WAKE_WORD_SHADOW_PATIENCE`. Both heads sit in the one openWakeWord `Model`
  (the features are computed once), the shadow has its own `DetectionGate`
  and relays nothing.
- [x] (2026-11-15, as `Shadow model fired (score=…)` and `shadow=` on the
  `wake event:` line) Journal: `shadow: peak=… fired=yes|no` per event, and the shadow's peak
  on the existing `wake event:` line.
- [x] (2026-11-15) A shadow firing starts a capture even when the base score stayed below
  0.05 (capture kind `shadow`), so every disagreement has audio.
- [x] (2026-11-15: selftest "shadow" and "model by path" checks) Tests in `tests/test_wake_trigger.py` and `wake-replay.py selftest`;
  second-instance run on the live graph; then install.
- [x] (2026-11-15; disagreements also head the labelling queue) `wake-events-report.py`: shadow columns; a table "old fired / new
  fired" with the four combinations; disagreements go to the top of the
  labelling queue.

**F2. Seven days.** Started ____.
- [ ] Label every disagreement (C2).

**F3. Shadow report.**
- [ ] Of the sessions that did something: share the new model would also have
  opened (aim: all but at most 1 in 20).
- [ ] Of the old pipeline's blocked or rejected events labelled `real`: share
  the new model catches (this is the gain in recall).
- [ ] Of the old pipeline's unwanted sessions: share the new model would also
  have opened (aim: few).
- [ ] New-only firings labelled `false`: count in the week (aim: at most one).
- [ ] Round 2 again through the live shadow lines, not only offline.

---

## Stage G — switch

- [ ] **Owner:** yes / no on the shadow report.
- [ ] `wake-word.service`: `WAKE_WORD_MODEL` = the new model's path
  (`load_model()` takes a path as well as a bundled name: done 2026-11-15),
  `WAKE_WORD_THRESHOLD` and `WAKE_WORD_PATIENCE` = the values of E7,
  `WAKE_WORD_VERIFIER=0`, `WAKE_WORD_SECOND_OPINION=log`. Each line with a
  comment giving the date and the evidence. The pretrained model becomes the
  shadow for two weeks, so going back is backed by data too.
- [ ] Rollback, one step: `git revert` of that commit,
  `./install.sh apply user`, `systemctl --user restart wake-word`.
- [ ] Decision record `docs/decisions/0021-household-wake-word-model.md`, a
  row in its README, D0005/D0017/D0018 marked as superseded in part; rewrite
  the stage sections of wake-word.md; CHANGELOG.
- [ ] After a week of use: `wake-events-report.py` against the targets;
  **Owner** confirms; `git tag YYYY-MM-DD-household-wake-word`.

---

## Stage H — simplify, and make leftover false wake-ups cheap

After two stable weeks on the new model.

**H1. The cascade.**
- [ ] Verifier: stays off; code and trainer kept until the next retrain has
  gone well, then removed.
- [ ] Second opinion: from two weeks of `log` lines, would it have blocked
  any `real` wake-up, and any `false` one? Keep it only if it removes false
  ones at no cost, and then only for scores in a band just above the
  threshold. **Owner** decides on the figures.

**H2. Shorter wait for the first words.** `src/voice_agent.py`
- [ ] Measure first, from the journal: in useful sessions, how long after the
  session start does the first speech begin (median and the slowest).
- [ ] New setting `VOICE_FIRST_SPEECH_SECONDS`: a session in which nobody has
  started speaking by then closes (today: 8 s, `VOICE_IDLE_SECONDS`, which
  stays for pauses later in a conversation). **Owner** picks the value from
  the measurement.
- [ ] Test with `voice-session-replay.py` on real captures; docs and
  configuration row.

**H3. "Not for me".** `src/voice_agent.py`, system prompt and one tool.
- [ ] A tool that ends the session without a spoken reply, and a line in
  `VOICE_SYSTEM_PROMPT` to use it when what was heard is plainly not
  addressed to the assistant.
- [ ] Replay the room-conversation captures labelled in C (the nine sessions
  of Oct–Nov are the model case) and ten real commands, three times each:
  the conversation ones must close silently, no command may be dropped.

**H4. Feedback without effort.**
- [ ] `wake-events-report.py --queue`: a session cancelled with the button or
  ended with nothing heard is queued as a probable `false`, one that used a
  tool as a probable `real`, for one-tap confirmation in the labelling page.

---

## Stage I — the microphone side (only on evidence)

Start this only if D or G show a loss with distance or with music that the
model does not recover.

- [ ] Free, from the recordings of B (both channels are in them): train and
  evaluate a head on channel 0, and on "either channel", against channel 1.
- [ ] From A3: do misses cluster in the first 10–14 s of a song (the echo
  canceller converging)? If so, the open item "keep the XVF3800 sink running
  through pauses" moves up.
- [ ] A second beam or raw microphone on the unused capture channel would
  need XVF3800 output-routing registers. That is a proposal of its own, with
  its own measurements of the echo canceller before and after; never a
  write-only register read, nothing saved to flash.

## Stage J — keeping it good

- [ ] Monthly, or when the owner notices a change: label the queue (~10 min),
  `wake-model-train.py train` with the new labels and the latest room log,
  `wake-eval.py model` on round 2 and the newest week of room log. Install as
  the shadow first; switch only if it is not worse anywhere. One CHANGELOG
  line per retrain with the figures.
- [ ] A new person in the household: one round of B1 for them, retrain.
- [ ] Round 2 stays the fixed yardstick; it is never trained on. If the room
  changes (speaker moved, new furniture), record a new round 2 and say so.

---

## What it needs

- **Owner's time:** two recording rounds of about 20 minutes per person, two
  15-minute playbacks (B4), about 10 minutes of labelling twice a week, and
  the decisions marked above.
- **Disk** (88 GB free on 2026-11-15): captures 4.2 GB, feature log up to
  12 GB, public features 17.5 GB, synthetic clips and features ~5 GB, the
  training venv ~2 GB, recordings under 1 GB: about 42 GB.
- **Network:** one 17.5 GB download and the Piper voices; nothing is uploaded.
- **CPU:** live cost unchanged (a second head on features already computed).
  Training time on the Pi is an estimate until E6 has run once.

## Risks

| Risk | Answer |
|---|---|
| The public features do not match this openWakeWord version | E2, before anything else is built on them |
| Too few real clips of a second person | Results are reported per person; B4 adds other voices; more takes are cheap |
| Synthetic voices differ from real ones through this microphone | B4 re-records them through it; real clips are in every batch |
| The model learns this month's room and fails on next month's | 2 000 public hours in every batch; the monthly retrain; round 2 as fixed test |
| Logging or a second head disturbs live audio | Bounded queues that drop; second-instance test; status check |
| Training load disturbs live audio | One thread, niced, at night; echo-canceller check the next morning |
| The result is no better | E8 and F3 are go / no-go points before anything live changes; the old pipeline stays one revert away |

## Not in this plan

- The real music request of 10 Nov that Gemini answered without calling a
  tool: a voice-agent matter.
- Far-field commands over ducked music not heard by Gemini: the existing
  open item; the recordings of B can serve it later.
- Another wake phrase. Possible with the same tooling once it exists.
- Sending wake audio to a cloud model for confirmation: rejected (every room
  trigger would leave the house, plus delay and quota).
