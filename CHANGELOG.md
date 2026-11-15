# Changelog

Every change to the speaker, newest first: what changed, why, and the
evidence. Entries before 2026-10-11 were reconstructed from the working notes
(`docs/history/`), the `.bak` copies and staged installs left on the device,
shell and apt history, and the system journal; times are local (CEST).
Decision records are linked as [D00NN](docs/decisions/README.md).

## How the project went

| Period | Stage | In one line |
|---|---|---|
| 15–20 Aug | A speaker | Amp, active crossover in CamillaDSP, Spotify Connect, buttons, one volume |
| 21–23 Aug | A voice | Wake word, Gemini Live conversation, ducking, tools, chimes, timers |
| 24–30 Aug | Reality | The wake word fails over music; the echo canceller turns out never to have had a usable reference; heat, Wi-Fi and logging problems |
| 21 Sep | Hardware | A fan; the web UI fixed |
| 4 Oct | The long day | Six root causes in one day, from a retrigger loop to PipeWire's clock; echo cancellation finally measured working (28–29 dB) |
| 5–9 Oct | False wake-ups | A verifier trained on the household's voice, an engine benchmark, a second opinion that blocks |
| 11 Oct | This repository | Everything versioned, documented, installable |

---

## 2026-11-15 — Wake-word enrolment recorder (plan stage B1)

- **What:** `tools/wake/wake-enroll.py`: cued "Hey Jarvis" recordings at
  measured positions, in quiet, over music and over talk. The LED ring lights
  for each of 12 takes per position; voice-agent is stopped meanwhile and
  started again at the end; wake-word keeps running so the journal records
  the live verdict on every take. Recordings stay on the device
  (`~/.local/share/ai-speaker/wake-enroll/`, in `.gitignore`).
- **Why:** plan stage B; the owner gave the positions: right next to it
  (0.5 m), kitchen (3 m right), corridor (4 m behind), entrance (6 m forward
  right), balcony (3 m left).
- **Evidence:** dry run of four short blocks: files, skip, cue times (3.04 s
  for a cue planned at 3.0 s) as expected.

## 2026-11-15 — Wake word: keep the evidence (plan stage A)

- **What:** stage A of [the custom-model plan](docs/plans/wake-word-custom-model.md),
  live since 14:56.
  - Captures rotate in two pools: near-misses (score below the threshold)
    beyond 200 MB (`WAKE_WORD_CAPTURE_NEAR_MAX_MB`), everything else beyond
    `WAKE_WORD_CAPTURE_MAX_MB=4000` (was one shared 200 MB).
  - New `src/wake_features.py`: openWakeWord's 96 features of every 80 ms
    frame and its score, float16, in `~/.local/share/ai-speaker/wake-features/`
    (`WAKE_WORD_FEATURE_LOG=1`; ~210 MB a day, capped at 12 GB).
  - New `src/wake_context.py`: whether music is playing, for how long, and
    the volume, on every `wake event:` line and in every capture's JSON.
  - `wake-events-report.py` reads them (`music`, `music_s`, `volume`
    columns; a "music playing at the time" table).
  - Detection, thresholds and the session path are unchanged.
- **Why:** owner, on the plan: "You can push it to main any begin with step
  A". The 28-day review found that the 200 MB cap kept only about two days of
  audio (near-misses pushed detections out) and no event recorded whether
  music was playing; training and judging a new model needs weeks of the
  room's sound.
- **Evidence:** unit tests (`tests/test_wake_features.py`, 10 checks) and
  `wake-replay.py selftest` pass, including a new check that the logged rows
  are the window the model scores (largest difference 0.016, float16). A
  second instance next to the live one: 12.0 / 11.9 % of a core with the
  logging off, 12.9 / 12.5 % on; one feature file filled at exactly 12.5 rows
  a second; status all OK before and after the install.

## 2026-11-15 — Plan: a wake-word model trained on this household

- **What:** [docs/plans/wake-word-custom-model.md](docs/plans/wake-word-custom-model.md),
  a step-by-step plan in ten stages: keep the evidence (longer capture
  retention, a log of openWakeWord's features, music state on every event);
  record wake-ups at measured distances in quiet, over music and over talk;
  label captures; measure the baseline; train a small classifier on
  openWakeWord's features from the household's voices and the room's own
  sound, on the Pi; run it log-only for a week; switch; then retire the
  verifier and demote the second opinion. Documentation only: no code and no
  setting changed.
- **Why:** owner, asked for a plan to make the wake word reliable from a
  distance without false wake-ups from nearby talk: "I'm fine with any
  approach which can produce a significantly improved result", then "Agree
  with a plan let's detailed it to exact steps to be done and write to docs."
- **Evidence for the direction:** the history below. Since August every step
  has compensated for a first stage that scores this household low: threshold
  0.5 → 0.10, then a verifier, then a second opinion. In the 28 days the 63
  useful sessions peaked at a median of 0.37 (17 below 0.20); room speech
  reaches 0.10 about 116 times a day; the second opinion's engines, which
  heard 27 and 21 of 42 real wake-ups as detectors, block 77 % of what
  reaches them; about 1.4 unwanted sessions a day still open. Gain, channel
  0, other engines, a stricter verifier and a verifier override were all
  measured and ruled out earlier.
- **Found on the way:** the second opinion heard "Jarvis" in 167 events (125
  utterances) that the verifier rejected; unexamined, queued for labelling in
  the plan. Checked for the plan: the public negative features for
  openWakeWord are a 17.28 GB download (2 000 hours) plus 185 MB (11 hours);
  pip offers torch and piper-tts for this Pi (not installed).

## 2026-11-15 — Wake-word evidence collected for an investigation

- **What:** new `tools/wake/wake-events-report.py` joins the wake-word and
  voice-agent journals into one record per wake event (openWakeWord peak,
  verifier, second-opinion verdict, capture, and what the session did) and
  flags likely false wake-ups and likely misses. Run over 18 Oct 16:27 –
  15 Nov 13:20; the report, `events.csv`, `sessions.jsonl` and copies of the
  38 flagged captures still on disk are in
  `~/.local/state/ai-speaker/measurements/wake-events-2026-11-15/` (household
  speech, not in git). Results in
  [docs/wake-word.md](docs/wake-word.md#in-daily-use-18-oct--15-nov-2026).
  No setting changed.
- **Why:** owner: "Collect the information about false positive activations
  of wake word and when it was not triggered. We will run investigation based
  on those."
- **Evidence:** 506 triggers passed the verifier in 28 days; 390 (77 %) were
  blocked by the second opinion (Vosk 0.00 on 385 of them), 115 opened a
  session. Of 113 voice sessions, 64 did something; 30 heard nothing
  intelligible (6 of them right after failed triggers), 5 were cancelled with
  the button within 15 s, 14 heard speech but called no tool (9 room
  conversation, 1 real command left undone). Misses, as lower bounds: 9
  blocked and 13 verifier-rejected triggers within 45 s before a wanted
  session, 12 pairs of blocked triggers within 30 s with nothing after, 14
  music requests made with the button. Verifier medians: useful sessions
  0.98, empty 0.81, blocked 0.67; a verifier at 0.8 would keep 57 of 64
  useful sessions and still pass 15 of 29 empty or cancelled ones.
- **Found on the way:** the 200 MB capture cap holds only about two days
  (356 of 390 captures were near-misses below 0.10), so the audio of
  everything before 13 Nov is gone; only journal lines remain.

## 2026-10-18 — Second opinion hears quiet wake-ups

- **What:** `wake_confirm.py` normalises the 3 s window to half of full scale
  (gain at most 16×, `WAKE_WORD_SECOND_OPINION_MAX_GAIN`) before Vosk and
  microWakeWord judge it. Thresholds unchanged: openWakeWord 0.10, verifier
  0.5, Vosk 0.5, microWakeWord 0.5.
- **Why:** owner: "we need to increase Hey Jarvis sensitivity a bit as it
  hears not very good, but be cautious about false positives". The journal
  showed the second opinion, not the detector, losing them: blocked/confirmed
  per day went from 6/14 (10 Oct) to 17/2 (17 Oct) and 8/0 (18 Oct until
  16:00). Owner on the blocked ones of 17–18 Oct: "at least half it was me";
  no session in that time was cancelled with the button, the owner's sign of
  a false wake-up. Vosk scored 0.00 on nearly all of them, live and in
  replay; their windows peak low (RMS ~40–200).
- **Not done:** letting a very confident verifier override the second opinion.
  Known false wake-ups reach verifier 0.94–0.975 (9 Oct 09:04, 10 Oct 22:55,
  and empty sessions of 5–8 Oct), while most of the blocked real ones on 17–18
  Oct scored 0.5–0.8.
- **Evidence (replay, channel 1):** hand-sorted real 30 → 37 of 39; false
  0 → 0 of 61 that reach the 0.10 threshold (one more of 62 passes, but its
  detector peak is 0.058, so it never reaches this stage); recent blocked
  1 → 6 of 26; verifier-rejected room triggers of 16–18 Oct 1 → 5 of 175
  passed by the second opinion alone, 0 of 175 by verifier and second opinion
  together. Judging still takes ~100 ms. Other variants (later window,
  channel 0, stricter cut-offs) were no better. Some 20 of the 26 are still
  heard by neither engine; the next step for those is retraining the
  verifier on recent captures or a custom openWakeWord model.

## 2026-10-12 — spotifyd watchdog

- **What:** `spotifyd-watchdog.timer` checks every 5 min that Spotify's Connect
  device list still contains "AI Speaker". If the speaker is missing twice,
  20 s apart, the watchdog restarts spotifyd (at most once per 15 min).
  `spotify_web.py` now looks the device up fresh for every "play X" and
  starts the watchdog at once when the speaker is missing. The assistant then
  says the speaker dropped out of Spotify and is being reconnected.
- **Why:** at 09:23–09:24 three voice requests to play Валерий Меладзе
  failed. Each was confirmed aloud, then failed silently with `404` →
  `no Connect device named 'AI Speaker'`. spotifyd (0.4.2) had logged
  `Websocket peer does not respond.` at 02:00:32 and never reconnected. The
  process stayed up, so systemd saw nothing wrong, but the Web API device
  list was empty. A manual restart at 09:32 brought the speaker back at once.
  The cached device id hid the problem until after the confirmation.
- **Safety:** the watchdog restarts spotifyd only when Spotify doesn't list the
  speaker, so the speaker can't be the active Connect device at that moment
  (the rule in AGENTS.md). It does nothing if Spotify can't be reached.
- **Evidence:** tested on the device: no restart while the speaker is listed;
  with a missing device name and a fresh stamp it waits through the cooldown
  instead of restarting.

## 2026-10-11 — Repository

- Agent instructions moved to `AGENTS.md` so that any coding agent finds them;
  `CLAUDE.md` only imports it (Claude Code reads that name).
- Created this repository from the live device: code, units, WirePlumber,
  spotifyd, CamillaDSP and system files at their real paths; installer with
  `diff`/`apply`/`capture`/`bootstrap`; living documentation; decision
  records; this changelog. [D0020](docs/decisions/0020-repository-as-source-of-truth.md)
- **Why:** two months of changes lived only on the Pi as ~30 `.bak-*` copies,
  six stacked `staged-*` installs that only undid in reverse order, and notes
  in a working folder; parallel AI sessions had overwritten each other's
  installs.
- Not in the repo by design: API keys and tokens, household voice recordings
  and the verifier trained on them (`tools/private-backup.sh` covers them).
- MIT licence. The chime, timer and alarm sounds are stock sound effects and
  are included.
- CamillaDSP profiles were reconstructed from the original bundle plus the
  documented 16 Aug corrections and checked identical to the live ones through
  CamillaDSP's own parser. The local spotifyd patch was recovered from the
  build tree.

## 2026-10-09 — The second opinion blocks

- `WAKE_WORD_SECOND_OPINION=block` (20:20). `wake_word.py` now relays
  `voice-pending` and then `voice-confirm`/`voice-reject`; `voice_agent.py`
  opens only the mic until the verdict (1.5 s timeout = go ahead; a failed
  judgement confirms). The chime comes ~0.35 s later than before.
- Tool guard extended to `cancel_timer`/`cancel_alarm`.
- **Why:** owner, after a day of log-only: "still a few false positive
  activations". That day: 12 sessions, 2 real (both passed), 10 false (all
  would have been blocked). [D0018](docs/decisions/0018-second-opinion-blocks.md)
- Expected cost: about one real wake-up in five.

## 2026-10-08 — Engine benchmark, second opinion (log only), tool journal

- Benchmarked Porcupine (keyless 1.9.5; the free tier ended 2026-06-30),
  microWakeWord, sherpa-onnx KWS and Vosk on 485 captures from the device
  (`tools/wake-bench/`). None replaced openWakeWord; "Vosk or microWakeWord on
  channel 1" kept 34 of 42 real wake-ups and passed 0 of 62 known false ones
  and 0 of the 19 recent wordless sessions. [D0005](docs/decisions/0005-openwakeword.md)
- Installed that pair as `wake_confirm.py`, journal only
  (`WAKE_WORD_SECOND_OPINION=log`). Added `pymicro-wakeword` to the wake-word venv.
- `tool_guard.py`: every tool call is journalled (`Tool call:`); timers and
  alarms are refused in sessions where nothing intelligible was heard.
- **Why:** the owner asked for "a more foundational approach" after false
  wakes that sometimes started timers.

## 2026-10-06 — Music quiet, assistant loud

- `spotifyd.service`: `PULSE_PROP=state.restore-props=false`; the saved stream
  volume was reset to 1.0.
- **Why:** a "stop the music" while ducked (2026-10-05 21:58) ended the stream
  at 0.20; WirePlumber remembered that under spotifyd's (empty)
  application name and applied it to every later stream: 42 dB down whatever
  the master volume said.
- Lesson: restarting spotifyd while it is the active Connect device leaves a
  stale session at Spotify (Web API play → 502 for 15+ minutes).

## 2026-10-05 — Household verifier; far-field problem found

- `Verifier` in `wake_word.py` (10:56): a logistic regression on
  openWakeWord's embeddings, trained on 33 positive / 62 negative hand-sorted
  captures (`tools/wake/wake-verifier-train.py`). Retrained 12:46 with six
  far-field wake-ups (39 / 62). [D0017](docs/decisions/0017-household-verifier.md)
- **Why:** a meeting in the room fired the wake word ~50 times in 50 minutes
  at scores no threshold could separate from real ones. Offline: 93 % of real
  kept, 0 % of known false passed. Owner: "seems to be better".
- Open: from across the room over ducked music, the wake word fires but
  Gemini hears nothing (4 of 4 attempts; 0 of ~40 replays fixed by gain,
  channel 0, Gemini's start sensitivity or denoising).

## 2026-10-04 — The long day

The 30 Aug complaint (wake word poor in a quiet room, nearly impossible over
music, false wake-ups, commands misunderstood) was worked through end to end.
Six separate causes, in the order they were found:

1. **12:42 — Offline intent path off.** One "Hey Jarvis" fired up to ten
   times (93 triggers from 41 utterances): the wake-word loop blocked 1–6 s
   while Vosk listened, and every session started ~4 s late; Vosk's English
   grammar matched 0 of 93 mostly Russian commands. `LOCAL_INTENT_ENABLED=0`.
   [D0013](docs/decisions/0013-offline-intents-off.md)
2. **Write-only register incident.** Reading `PP_NL_MODEL_CMD_ABORT` wedged
   the XVF3800; the mic delivered zeros with every service active. Recovered
   by a chip reboot; `xvf_register.py` now refuses such reads, and
   `ai-speaker-status.sh` fails on a silent mic.
3. **18:52 — Detection loop and session start rewritten** (third install of
   the day): one detection per utterance, `pw-cat --raw`, capture watchdog,
   health file, audio captured around every near-trigger; the session mic
   opens at the trigger and held audio is sent once Gemini is ready; Gemini
   gets channel 1 with +12 dB and a silent lead-in.
   [D0014](docs/decisions/0014-mic-channel-1-with-gain.md)
   - Install 1 (13:40) was rolled back in two minutes: `node.dont-fallback`
     made WirePlumber destroy every capture as soon as a second stream
     started. Offline tests had passed; the lesson became "test on the live
     graph".
   - Install 2 (18:05) was rolled back: "play Beatles on Spotify" never
     understood. Replaying the five recordings reproduced it exactly (0 of
     10) and showed why: channel 0 and a stream starting mid-speech. Fixed
     version: 14 of 15 replays, 2 of 2 live.
4. **19:14 — Echo canceller alignment.** First real measurement
   (`aec-align-check.py`): with the 0.341 s loopback delay set since August
   the reference arrived 264 ms *late* and 0.2 dB was cancelled. 0 → 67 ms
   early, 12 dB and creeping; 0.04 → 36 ms early, 29 dB in 14 s; 0.055 → 20 ms
   early, 29 dB in 10 s. Set 0.04. Also corrected: the August "ERLE" sweeps
   measured an AGC'd output, not cancellation.
5. **19:21 — Replies in English by default**, Russian only when certain.
   [D0019](docs/decisions/0019-reply-language.md)
6. **19:38 — One confirmation, then the music** (a parallel session):
   `complete_quick_action` defers the action until the reply has been spoken.
   11 of 11 replays. [D0016](docs/decisions/0016-confirm-then-act.md)
7. **19:49 — Voice sessions keep CamillaDSP's buffering.** Loading
   `voice.yml` whole cut 52 ms of buffering and put the echo ahead of its
   reference for every session. Now only the processing is swapped; 28 dB held
   through a session (19:57); mute tail 0.4 → 0.46 s. Three real sessions over
   music at 19:59 all acted on correctly.
   [D0010](docs/decisions/0010-voice-dsp-profile-over-websocket.md)
8. **21:49 — Reboot** (user added to the `pipewire` group for realtime audio
   threads). Every check green, but cancellation **0 dB** and 2 of 7 commands
   right.
9. **22:31 — Clock master.** The XVF3800's output had become the graph's
   driver (a tie in `priority.driver`, decided by boot order); the speaker path
   was resampled and the echo's delay saw-toothed. `priority.driver = 2500` on
   the loopback sink.
10. **23:00 — Reference not resampled.** Rate matching of the XVF3800 sink
    slid the reference 2–3 ms now and then (4 of 8 bursts even with realtime
    priority). Shared `clock.name`, `api.alsa.headroom = 1792`, loopback delay
    0. [D0015](docs/decisions/0015-clock-master-and-unresampled-reference.md)
11. **23:28 — Measured:** 28.6–29.2 dB, reference 32 ms early, echo and
    reference steady to 0.004 ms over 42 s, held through a session switch.
    **23:40 — Owner's test:** five of five detected and played, verdict
    "wake word sensitivity could be better, but overall good enough".
    Threshold 0.05 offered (data supports it) and declined: stays 0.10.
    **23:57 — Reboot test passed** with the new settings.

## 2026-09-21 — Fan, web UI

- A fan on the Pi 5 fan header, verified ramping (56 °C → 1616 rpm, 67 °C →
  ~3540 rpm, plateau 68–69 °C under load, `get_throttled` 0x0). The Amp4 does
  not block the header.
- CamillaGUI bound to `0.0.0.0` instead of a fixed LAN address: DHCP had moved
  the Pi and the UI had crash-looped 1132 times.
- Confirmed Wi-Fi power saving has been off since 30 Aug (`powersave=2`) and no
  unexpected disconnects in 13 days.

## 2026-08-30 — Timers and alarms live; reliability complaint

- `timer-alarm.service` enabled, `timer.mp3`/`alarm.mp3` supplied; offline
  path on (`LOCAL_INTENT_ENABLED=1`, switched off again 4 Oct).
- First voice test failed ("I can't set timers"). Three fixes: utterances
  split by the 0.6 s silence cutoff (continuation pass), "for" heard as
  "four" (4 minutes instead of 1), and the button path never reached the
  offline parser: timer tools added to Gemini (`timer_tools.py`) so timers work
  on both paths. `timer-alarm.service` pinned to mic channel 1.
- The owner reported four symptoms (S1 quiet-room misses, S2 music, S3 false
  wakes, S4 misrecognition); everything known was collected in
  `docs/history/originals/2026-08-30-wake-word-reliability-context.md`.
  Addressed on 4 Oct.

## 2026-08-29 — Wi-Fi at boot, persistent journal

- The Pi sometimes booted without Wi-Fi. Two managers claimed `wlan0`
  (cloud-init's netplan and NetworkManager): cloud-init's network config
  disabled, stale netplan stubs removed, NetworkManager profile hardened
  (Wi-Fi power saving off). The SDIO radio may not survive a warm reboot:
  `wifi-recover.timer` checks 75 s after boot and every 2 min and escalates up
  to reloading the driver.
- Journal made persistent (`99-persistent.conf`, 200 MB): every crash had
  destroyed its own logs. An earlier attempt named `10-…` lost to the stock
  `40-rpi-volatile-storage.conf`; drop-ins merge by file name.
- `fix-ai-speaker.sh` from 24 Aug turned out never to have been run.

## 2026-08-24 — The echo canceller had no usable reference

- **AEC reference gain.** The XVF3800's playback volume sat at 0.40 (−23 dB),
  and that volume is the chip's hardware mixer: the reference reached the
  canceller ~40 dB down, at its noise floor. Echo cancellation had never
  worked. `xvf-aec-ref.sh` now forces both playback controls to 0 dB on every
  start and periodically.
- **Threshold 0.25 → 0.10.** Over music, real "Hey Jarvis" scored 0.15–0.44
  while the music-only false-positive floor was 0.002–0.015.
- **Overheating found:** 83–87 °C, permanently throttled, two hard crashes.
  (Resolved by the fan, 21 Sep.)
- Found: CamillaGUI crash-looping on a stale bind address; Wi-Fi power saving
  on; no per-user journal (all docs had the wrong `journalctl` command).
  `wake-scores.sh` and `wake-live.py` added for reading scores.

## 2026-08-23 — Flexibility, timers built, wake-word channel

- `pw_node.py`: one shared "wait for the named PipeWire node" helper instead
  of four hand-rolled ones (the wake-word capture had none).
- `ai-speaker-status.sh`: one health check for units, lingering, registers,
  routing, Gemini reachability.
- `voice_engine.py`: the `VOICE_ENGINE` switch the plan always intended
  (`openai_realtime` wired up, never tested live).
- `local_intent.py`/`local_actions.py`: offline Vosk command matching, shipped
  off.
- Timer and alarm built (`timer_alarm.py`, client, LED ringing state and
  countdown arc).
- LED "listening" changed from a direction-chasing arc to whole-ring breathing.
- Thermal monitor service.
- Wake word: channel 0 → 1 and threshold 0.35 → 0.25 after a calibration run
  (quiet room excellent on both channels; over music the score floor was
  0.72–0.96 and nothing was detected).

## 2026-08-22 — Ducking, tools, chimes

- **Ducking** (`duck.py`): music streams to 0.20 for the whole session,
  ramped, restored in `finally` and after crashes; passive nodes excluded.
  Verified against real spotifyd (it matches on `application.process.binary`;
  its `application.name` is empty). [D0009](docs/decisions/0009-ducking.md)
- **Tools:** Spotify transport via MPRIS, volume via `wpctl`, and
  `spotify_play_track` via the Spotify Web API (search, then play on this
  Connect device). [D0011](docs/decisions/0011-spotify-web-api-for-search.md)
- **Quick-action close:** a one-shot command ends the session after its
  confirmation.
- **Chimes** (`sound_fx.py`): session start/end, boot welcome
  (`welcome-sound.service`, waits for the sink), Spotify Connect (spotifyd
  hook). The Connect chime first listened for `session_connected`, which
  spotifyd never emits; it is `sessionconnected`. Session-start chime briefly
  blocking (mic dead for 2 s after the LED said "listening"); reverted the same
  day.
- `xvf_register.py` (quiet CLI for the vendor register library) and
  `ai-speaker-calibrate.py`; threshold 0.5 → 0.35 from measured separation.
- LED ring: assistant states over a second socket; direction-of-arrival
  offset calibrated (180°).
- Barge-in test (full duplex, assistant at half volume): reverted.
  [D0008](docs/decisions/0008-half-duplex.md)

## 2026-08-21 — The voice: wake word, Gemini Live, robustness

- Python environments for the voice agent, the wake word and the XVF3800
  library. [D0004](docs/decisions/0004-gemini-live-and-pipecat.md),
  [D0005](docs/decisions/0005-openwakeword.md),
  [D0007](docs/decisions/0007-two-processes.md)
- **Echo-canceller reference feed** (`xvf-aec-ref`): after three approaches
  failed silently, a supervisor that keeps re-making the correct link and
  re-applies `AUDIO_MGR_FAR_END_DSP_ENABLE` (chip RAM, resets on every
  re-enumeration). Loopback delay set to 0.341 s, an estimate (it turned out
  to make the reference 264 ms late, 4 Oct).
  [D0006](docs/decisions/0006-hardware-echo-cancellation.md),
  [D0012](docs/decisions/0012-registers-at-runtime.md)
- `wake_word.py`: openWakeWord `hey_jarvis` on the XVF3800's processed
  capture; the GPIO wake button through the same path.
- `voice_agent.py` with a custom PipeWire transport (`pw_transport.py`),
  half-duplex gate, voice DSP profile over CamillaDSP's websocket
  ([D0010](docs/decisions/0010-voice-dsp-profile-over-websocket.md)), LED
  states.
- Session robustness: context-window compression on (pipecat leaves it off; it
  is what lets a conversation outlive the free tier's 15-minute limit), mute
  gate ceiling of 30 s, `ErrorFrame` closes the session, absolute session limit
  of 30 min, crash-safe DSP restore. Verified by `kill -9` mid-session.

## 2026-08-16 – 2026-08-20 — Spotify, the mic array, one volume

- **16 Aug:** CamillaDSP bundle finalised (version `2026.08.16-final`):
  capture from the cross-connected loopback side (`hw:Loopback,1,0`; the other
  side was silent), chunk 1024 / target level 3072, no mmap on the PipeWire
  loopback sink, +4 dB bass shelf at 150 Hz, CamillaGUI 4.1.0 with a bass
  shortcut, service accounts. raspotify installed.
- **16–17 Aug:** XVF3800 flashed with the 2-channel 48 kHz USB firmware;
  control library cloned; udev rule for the `plugdev` group; LED ring shows
  the volume (`volume_led.py`).
- **18–20 Aug:** raspotify replaced by spotifyd 0.4.2 built from source
  (rustup; Debian's Rust was too old) with a patch so its no-mixer controller
  remembers the volume; `volume-sync` keeps PipeWire and the Spotify slider in
  step. [D0002](docs/decisions/0002-one-volume-control.md),
  [D0003](docs/decisions/0003-spotifyd.md)

## 2026-08-15 — A speaker

- Raspberry Pi OS 64-bit on a Pi 5; HiFiBerry Amp4 (`hifiberry-dacplus-std`,
  on-board and HDMI audio off).
- snd-aloop as card 0, CamillaDSP 4.1.3 as a hardened system service owning
  the amplifier, first profiles (music, voice, four measurement profiles):
  mono sum, LR4 crossover at 2.8 kHz, 80 Hz woofer protection, −3.5 dB tweeter
  trim, −1 dBFS clip guard. Start gain −20 dB, raised to −7 dB the same day.
  [D0001](docs/decisions/0001-camilladsp-owns-the-amplifier.md)
- GPIO buttons (`volume-buttons.py`): volume up/down, play/pause/next.
- systemd lingering for the user, so user services start at boot.
