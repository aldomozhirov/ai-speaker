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
