# Wake-word reliability: findings and fixes (2026-10-04)

Follow-up to `WAKE-WORD-RELIABILITY-CONTEXT-2026-08-30.md` (symptoms S1 quiet
room, S2 over music, S3 false activations, S4 far-field recognition).

## Where things stand

| | State |
|---|---|
| One "Hey Jarvis" firing many times, 4 s dead window, phantom sessions | **Fixed and live** since 12:42 (`LOCAL_INTENT_ENABLED=0`) |
| Mic health visible; register guard | **Live** (`ai-speaker-status.sh`, `xvf_register.py`) |
| Fixed wake-word loop, audio capture, mic watchdog | **Live since 18:52** (third version) |
| Commands not understood at session start | **Live since 18:52**: channel 1, +12 dB, silent lead-in; 2 of 2 real attempts understood |
| Echo cancellation alignment (S2) | **Reference 32 ms early, 28.6-29.2 dB of cancellation on the music profile, measured 23:28** (was 0.2 dB until 19:14). 19:14-23:00 the lead came from a 40 ms loopback delay (36 ms early, 29 dB); since 23:00 from the XVF3800 sink's headroom, loopback delay 0 |
| Alignment kept during voice sessions | **Live since 19:49, measured 19:57**: echo does not move, 28 dB held through a session (old switch: reference 18.6 ms late) |
| Echo cancellation gone after the 21:49 reboot (0 dB) | Cause: the XVF3800's USB output had become the graph's clock master (a tie in `priority.driver`). **Fixed 22:31** (`priority.driver = 2500` on the program sink); `ai-speaker-status.sh` now checks it. **Measured 23:28: 28.6-29.2 dB again** (last section) |
| Reference feed slips by ~3 ms now and then (found 20:05) | Realtime priority is on since the reboot and did **not** stop it (4 of 8 bursts). Real cause: PipeWire's rate matching of the reference chasing snd-aloop's uneven clock. **Live since 23:00**: reference no longer rate-matched (shared `clock.name`, 1792 frames of headroom, loopback delay 0); 0 of 6 bursts moved, 0 in 170 s under CPU load. **Measured 23:28: echo and reference both steady to 0.004 ms over 42 s, 28.6-29.2 dB held through a session switch** |
| Detector margin for the user's voice (0.12-0.5 typical against a 0.10 threshold) | Open. 23:40: the user finds it "good enough" though it "could be better"; 6 of 7 logged near-misses (0.05-0.10) were real attempts, so 0.05 is the cheap next step. **Offered and declined for now (23:45): the threshold stays at 0.10** (last section) |

Three changes are stacked on the live files, each with its own folder,
`install.sh` and `rollback.sh`. They undo in reverse order only, and each
script refuses to run out of order:

1. `staged-2026-10-04/` (18:52) -- wake word and session start;
2. `staged-2026-10-04-reply-order/` (19:38, a parallel Claude session) -- one
   confirmation, then the music;
3. `staged-2026-10-04-dsp-timing/` (19:49) -- sessions keep the buffering.

Outside that stack, edited in place with backups next to them (22:31 and
23:00, last section): `~/.config/wireplumber/wireplumber.conf.d/51-smart-speaker.conf`
(clock master, reference not rate-matched), `~/.config/systemd/user/xvf-aec-ref.service`
(loopback delay 0) and `~/.local/bin/ai-speaker-status.sh` (two new checks).

To re-measure the echo cancellation (audible, about 50 s of hiss per run):
`~/.venvs/voice-agent/bin/python ~/claude-project/aec-align-check.py --session-switch new,new`,
then `aec-echo-track.py` to see whether the echo stood still. Expect about
32 ms of lead and 28-29 dB. Inaudible: `aec-ref-timing-probe.py lead`.
`--compare` still works, but since 23:00 its delays come on top of the 37 ms
the XVF3800 sink's headroom provides: 0.04 and 0.055 now put the reference
*behind* the echo. Compare small steps (`--compare 0,0.005,0.01`).
First of all, after any reboot or restart of WirePlumber: `ai-speaker-status.sh`.

## What was actually wrong

Source: the persistent journal, 2026-09-20 to 10-03 (93 wake triggers, 64
sessions), plus offline replay with the installed openWakeWord 0.4.0.

**1. One utterance fired repeatedly (S3, part of S1).** With
`LOCAL_INTENT_ENABLED=1` (switched on 2026-08-30 00:32, the day the complaint
was written), `wake_word.py` reacted to a detection by reading 1-6 s of command
audio straight off the capture pipe for Vosk. Those frames never reached
openWakeWord, so its sliding window stayed frozen on the wake word and the
next frame it was given scored high again. 93 triggers came from 41
utterances, in chains of up to ten, 1-6 s apart; 6 of 47 voice sessions opened
with nobody speaking. It does not depend on the threshold: replaying one clip
gives 6-15 triggers at 0.10, 0.25 and 0.5 alike.

**2. Every wake-up was followed by ~4 s of nothing (S1, S4).** The same Vosk
step held the session back (median 4 s, up to 6 s) with no LED or chime, and
whatever was said in that window was thrown away. Its grammar is English; the
commands here are Russian. It matched 0 of 93. Only 7 of 47 voice-triggered
sessions captured a command at all.

**3. The first second of every session is lost (S4).** After the fix above,
five real wake-ups on 2026-10-04 12:53-12:55 each produced exactly one
trigger; four opened a session in the same second and the fifth came while a
session was already open. But Gemini was only connected 1.1-1.6 s after the
trigger, and its service drops all audio until then. What arrived was
"Включи е у вас облом", Arabic script, `<noise>` -- and the question
"А ты можешь говорить по-русски?". Replaying a recorded command 0.3 s after
the trigger through the real session code reproduces it exactly (see Testing).

**4. The detector's margin for this voice is thin (S1, S2).** Those five real
utterances peaked at 0.285, 0.430, 0.146, 0.335 and 0.300 against a threshold
of 0.10; synthetic English voices score 0.99. Nothing recorded exists to tune
against, which is why the capture feature comes before any threshold change.

**5. Echo cancellation has never been measured (S2).** The old ERLE harness
read mux category 7, which is the ASR beam output while `AEC_ASROUTONOFF=1`
(the default), not an AEC residual. By configuration arithmetic the reference
(delayed 341 ms by `pw-loopback`) reaches the chip roughly 250 ms *after* the
echo, which no causal filter can cancel. Unmeasured; that is what
`aec-align-check.py` is for.

Also found:

- Every capture stream began with a 24-byte AU file header that was being
  decoded as audio (the constant "peak amplitude = 28260" at the top of each
  session). `pw-cat --raw` removes it.
- With the array missing, `pw-cat` silently records the default source, which
  here is the snd-aloop capture: permanent silence, service "active". That is
  what happened for the whole 2026-09-08..20 boot.
- **Do not use `-P '{ node.dont-fallback = true }'` against that.** It does make
  a missing target fail, but with WirePlumber 0.5.8 it also destroys healthy
  streams: every linked stream gets `target.node = -1` metadata, and the next
  time the policy re-checks it (whenever any other stream starts or stops)
  that `-1` plus the property is "defined target not found". See "First
  install" below.
- Reading a write-only XVF3800 register wedges the chip's DSP until it is
  rebooted (done by mistake during this investigation; the mic was silent for
  about 15 minutes).
- Read from the code, not observed: a button press during the first ~250 ms
  of session start-up would start a second session next to the first.
- The start chime is effectively 0.3 s long (a 250 ms tone and a decaying
  tail), not 1.8 s; it did not need shortening.

Corrections to the context document: "no score above 0.445" was a logging
artifact (the loop stopped scoring at the first crossing), and the four
triggers at 14:45 on 2026-08-30 were one utterance.

## Changed on the device (live)

- `~/.config/systemd/user/wake-word.service`: `LOCAL_INTENT_ENABLED=0`.
  Backup: `wake-word.service.bak-prechainfix`. Timers, alarms, Spotify and
  volume still work through the Gemini session's tools.
- `~/.local/bin/xvf_register.py`: refuses to read a write-only register or
  write a read-only one, before opening the device. Backup:
  `xvf_register.py.bak-premodeguard`.
- `~/.local/bin/ai-speaker-status.sh`: new "microphone signal" section --
  records one second and fails on digital silence; shows the wake-word
  service's own health file when present.

## Staged in `staged-2026-10-04/` (not installed)

`wake_word.py`, `wake_capture.py`, `wake-word.service`:

- Every captured frame goes through one place (`Listener.ingest`), so nothing
  can starve the detector again, with local intent on or off.
- `DetectionGate`: one detection per utterance; re-arms after 2 s and once the
  score is back under half the threshold.
- Capture opened with `--raw`; no capture is opened while the mic node is
  absent, and a stream that ends up on the silent fallback is caught by the
  watchdog.
- Watchdog: 10 s of digital silence, or a stream that stops delivering, is
  logged, published in `$XDG_RUNTIME_DIR/wake-word-health.json`, and the
  capture is reopened.
- `WAKE_WORD_CAPTURE=1`: every score above 0.05, and every button press, saves
  the surrounding audio (3 s before, 5 s after; 10 s before a button press),
  both channels, with the per-frame scores, to
  `~/.local/share/ai-speaker/wake-captures/`. Local only, capped at 200 MB.
- One journal line per utterance with its true peak
  (`wake event: peak=... fired=...`).

`pw_transport.py`, `voice_engine.py`, `voice_agent.py`:

- The microphone opens the moment a session is triggered; the audio is held
  until the pipeline exists, and then until Gemini is connected and ready,
  and sent ahead of live audio (`VOICE_PREROLL_SECONDS`, default 10, 0 = off).
- System prompt: answer in the language spoken, Russian when unsure; ignore a
  leading "Hey Jarvis" or chime.
- Closing phrases in Russian, matched as whole words ("пока" only at the end
  of what was said, so "покажи" and "пока я готовлю" don't end the session).
- The assistant's replies are journalled (`Assistant said: ...`).
- A failure during session set-up can no longer leave the agent permanently
  "already active"; a button press during set-up cancels instead of starting
  a second session.

## Testing

- `wake-replay.py selftest` (49 checks, all pass against the staged files):
  one detection per utterance for six synthetic clips at thresholds
  0.10/0.25/0.5 with local intent off and on; local handling of an English
  timer command; watchdog; stall detection; recorder (pre-roll, both channels,
  button snapshot, pruning).
- `voice-session-replay.py units` (7 checks, all pass): closing phrases,
  pre-roll buffer, early capture, button during start-up.
- `voice-session-replay.py session`: 28 real Gemini Live sessions with a
  recording as the microphone (speaker, LED, CamillaDSP and tools stubbed).
  A Russian command starting 0.3 s after the trigger:

  | | Old behaviour | Staged |
  |---|---|---|
  | "Включи Монеточку на Spotify" heard as | "на статифри" (2 of 5), "и / че / Те монеточку на…" (3 of 5) | whole phrase, 5 of 5 |
  | "Прибавь звук" | nothing, or an English "What would you like to do with the volume?" | understood in 7 of 8 |
  | Reply language | English when only a fragment arrived | Russian |

  Not fixed by any of this: with the whole command heard, the model still
  said it had done something without calling the tool in 3 of 21 sessions.
  An extra prompt instruction made no measurable difference (a tool was
  called in 5 of 6 sessions without it, 4 of 6 with it).

## First install (13:40) and what it taught

The first staged version was installed at 13:40 and rolled back at 13:42. It
could not understand anything: its captures were opened with
`node.dont-fallback`, and at every wake word the session's own capture started,
WirePlumber re-checked the streams, and both the wake-word capture and the
session's microphone were destroyed ("defined target not found") 0.2 s later.
Sessions ran with no audio and idled out. The offline tests could not see it
because they replace the capture process; the one live check of the property
had used a single stream with nothing else starting.

The same property broke `aec-align-check.py` (its capture died when the noise
player started), and its player, given a file name, hung in pw-cat's raw mode
and ignored SIGTERM. The script now feeds the player through stdin, reads the
capture into memory, always stops both, and has a `--dry-run` rehearsal.

The second staged version drops the property. In addition to the offline
suites it was tested on the live graph, without sound:

- two captures side by side, the second started and stopped three times:
  with the property the first dies, without it both run (5.9 s of audio in 6 s);
- the staged `wake_word.py` as an isolated second instance on the real
  microphone for 48 s while two extra captures, a voice session and the status
  probe came and went: no capture restart, health "ok"; a button press saved a
  15 s two-channel capture;
- the staged session code on the real microphone (speaker, LED, CamillaDSP and
  tools stubbed): microphone open at the trigger and still open at the end,
  0.64 s released when the pipeline started, 1.04 s sent when Gemini was ready.

`install.sh` now also opens and closes extra captures after installing and
rolls back if the wake-word capture did not survive them.

The first install left five captures. The two real utterances in them (cut
short by the dying stream, so their peaks are underestimates) score 0.44 and
0.46 on channel 1 and 0.21 and 0.33 on channel 0; speech sits at about
-39 dBFS on channel 1 and -21..-26 dBFS on channel 0; adding 12 or 24 dB of
input gain does not raise the channel-1 score. So channel 1 is the right
channel and level is not the problem -- the model simply scores this voice low.

## Second install (18:05) and what the recordings showed

The second version was installed at 18:04 and rolled back at 18:07. Its
mechanics worked: five wake words gave five single triggers, no capture died,
0.6-1.0 s of pre-trigger-to-connect audio was delivered each time, and six
full captures were saved (one more "Hey Jarvis" scored 0.086 and did not
trigger). But "Hey Jarvis, play Beatles on Spotify" was never understood:
three sessions got no transcription at all, two were heard as "50,000 500"
and as Romanian.

Replaying those five recordings through the session code offline reproduces
this exactly (0 of 10 runs correct), which made it possible to vary one thing
at a time:

| Variant | Correct action (Beatles played) |
|---|---|
| As installed: channel 0, audio from the trigger | 0 of 10 |
| Channel 1 instead of channel 0 | 5-6 of 10; the three one-breath attempts still mostly silent |
| Channel 1 + 0.5 s of silence in front of the held audio | 7 of 10 |
| Channel 1 + silence + 12 dB gain | 10 of 10 |
| Final staged code doing all three itself | 14 of 15 |

What that says:

- **Channel 0 is the wrong channel for Gemini.** It is the XVF3800's AGC'd
  "conference" output; channel 1 is its ASR beam, the one the wake word already
  uses. (Open question #6 of the context document.)
- **A stream that starts in mid-speech gets no response.** In the one-breath
  attempts the command begins 0.0-0.2 s after the trigger, so the capture
  opens with speech already under way. Half a second of digital silence ahead
  of the held audio fixes it.
- **Channel 1 is quiet** (speech about -36..-42 dBFS); +12 dB in software
  helps Gemini and leaves the wake word's own input alone.

Third staged version: `VOICE_INPUT_CHANNEL=1`, `VOICE_INPUT_GAIN_DB=12`
(`voice-agent.service`), `preroll_lead_seconds=0.5` (`voice_engine.py`), and
"never any other language" in the prompt (one replay was answered in Dutch).
Still seen in replays: English commands sometimes confirmed in Russian, and
one run in fifteen resumed playback instead of playing the Beatles.

Evidence base: five recordings of one English phrase by one speaker. No
Russian command has been replayed on channel 1 from a real recording.

## Echo cancellation: measured

`aec-align-check.py`, 2026-10-04 18:07, music profile, `--delay=0.341`:

- the echo reaches microphone 0 **263.9 ms before** the reference
  (correlation peak 159x the background);
- reference level -20.7 dBFS, not clipping; echo at the mic -34.4 dBFS against
  an idle floor of -66 dBFS;
- ERLE **0.2 dB** in every one-second window: the canceller removes nothing.

So with the loopback delay at 0 the reference leads the echo by about 77 ms.
The voice profile (chunksize 512, target level 1024, against 1024 / 3072 for
music) is about 59 ms faster, which leaves the reference about 18 ms early
there. `XVF_AEC_REF_DELAY=0` is now set in `xvf-aec-ref.service` (backup
`xvf-aec-ref.service.bak-delay-0.341`) and live since 18:39. It has not been
re-measured; if 77 ms of lead turns out to be too much for the chip's filter
length, the next knob is `AUDIO_MGR_SYS_DELAY` (per profile).

## Next

1. Install the staged files; say "Hey Jarvis" plus the command in one breath.
2. Run `aec-align-check.py` (16 s of pink noise). A negative lag confirms the
   late reference; the fix is then `XVF_AEC_REF_DELAY` in
   `xvf-aec-ref.service`, checked by running it again. CamillaDSP's voice
   profile has a different latency from the music profile, so both need a look.
3. After a few days of captures: `wake-replay.py score
   ~/.local/share/ai-speaker/wake-captures` shows real peaks per channel and
   what each threshold, channel or input gain would have done. That decides
   between a threshold/patience change, a different channel, more gain, and a
   custom verifier or model for this voice. Do not retune the threshold before.

## Third install (18:52): working

Installed at 18:52 and left in place. Two real attempts, both understood:
"Hey Jarvis, play Beatles on Spotify" was heard as 'Play Beatles un spawn.'
and 'Beatles on Spotify.', the tool was called both times, and the assistant
confirmed (once in Russian, once in English). Wake scores 0.277 and 0.472.

## Echo cancellation after the delay change

`aec-align-check.py`, 18:54, `--delay=0`:

- the echo now arrives **67.9 ms after** the reference (predicted 77; the
  ~9 ms difference is what a restart of the loopback can move it by, so keep
  at least 15 ms of margin in any setting);
- ERLE per second after the residual tap went live: 1.8, 2.1, 2.6, 2.6, 3.2,
  3.5, 3.8, 3.8 dB -- still rising when the 16 s burst ended;
- by band over the last 4 s: 15 dB at 100-300 Hz, about 4 dB at 300-2000 Hz
  (where nearly all the echo energy is), 7-12 dB above 2 kHz.

From the saved capture (`aec-align-last-capture.raw`):

- the alignment is steady to 0.003 ms over 4 s, so clock drift between the
  USB array and the HiFiBerry is not the limit here;
- a fixed linear filter fitted on 2.5 s of reference and microphone removes
  18-19 dB on the *following* 2.5 s, and 13 dB with only 4 ms of filter after
  the echo's arrival. The echo is therefore cancellable; the chip is simply
  far from converged at this alignment.

Working assumption: 68 ms of lead leaves the chip too little of its filter
for the room, which slows its adaptation. `--compare 0,0.04,0.055
--seconds 24` measures leads of about 68, 28 and 13 ms in one run. If a
smaller lead converges properly, the catch is the voice profile, which is
about 59 ms faster than the music profile: a lead under ~59 ms on music is a
late reference during a session. Options then are to stop switching
CamillaDSP profiles for sessions, or to move the alignment per profile.

## Delay comparison (19:08) and the setting chosen

`aec-align-check.py --compare 0,0.04,0.055 --seconds 24`, music profile:

| `XVF_AEC_REF_DELAY` | Reference ahead of the echo | ERLE, second by second after the tap went live | At the end |
|---|---|---|---|
| 0 | 66.6 ms | 0.4, 0.5, 1.0, 1.5, 1.8, 2.2, 2.7, 3.5, 3.9, 4.8, 5.5, 6.6, 7.6, 8.5, 10.1, 12.4 | 12 dB and still rising |
| 0.04 | 35.6 ms | 3.2, 3.8, 5.8, 7.8, 11.4, 18.1, 24.6, 27.9, 28.3, 29.0 ... 29.5 | 29.4 dB |
| 0.055 | 20.2 ms | 7.7, 14.0, 23.1, 27.5, 28.0, 28.5 ... 29.2 | 29.1 dB |

The assumption held: the chip needs the reference early, but only just. With
36 ms or 20 ms of lead it reaches about 29 dB (31 dB at 1-2 kHz, 28 dB at
300-1000 Hz); the smaller lead gets there faster (about 10 s of noise against
14 s, each starting from the previous alignment). The loopback's `--delay` is
not exactly additive: 0.04 moved the lead by 31 ms and 0.055 by 46 ms.

`XVF_AEC_REF_DELAY=0.04` is set in `xvf-aec-ref.service` and live since
19:14: the same 29 dB with 15 ms more margin than 0.055 against the lead
moving between restarts or boots (not yet checked across a reboot).

Not solved: the voice profile. It differs from the music profile in more
than buffering -- no bass shelf, 105 Hz instead of 80 Hz woofer protection,
and a compressor on the input -- and it is about 59 ms faster, so during a
session the reference is roughly 23 ms *late* and nothing is cancelled, and
after the session the chip needs ~15 s of music to converge again. One way
out that keeps the profile's sound: apply the voice profile with the music
profile's `chunksize` / `target_level` (through CamillaDSP's API, the file
stays as it is), so the timing never changes; as a side effect the device
would no longer be reconfigured at every session start and end.

## Reply language

At the user's request English is now the fallback. A first wording ("answer
in the language spoken; if unsure, English") changed nothing: 3 of 10 replays
of English recordings were still answered in Russian, because the model
decides it heard Russian rather than being unsure. The wording now live
("Reply in English by default ... Russian only when you are certain the
request was spoken in Russian") gave English in 13 of 15 replays of English
recordings (the two Russian replies were both the one hard recording) and
Russian in 6 of 6 replays of synthetic Russian commands. Live since 19:21;
previous file kept as `voice_agent.py.bak-ru-default`.

## Sessions keep the buffering (19:49)

What a session did to CamillaDSP until now, measured silently on the device
(ALSA's own status of the DAC, nothing played):

| | Before the session | Voice profile loaded as a file (old) | Voice processing only (new) |
|---|---|---|---|
| DAC period / buffer | 512 / 4096 frames | 256 / 2048 | 512 / 4096 |
| Audio waiting in the DAC | 73.6 ms | 21.9 ms | 73.3 ms |
| DAC restarted | | yes, output stalled 0.06 s (0.12 s on the way back) | no |
| Time to take effect | | 0.08-0.13 s | 0.014 s |

So the old switch moved the echo about 52 ms earlier against a reference that
leads by 36 ms, twice per session, with a short hole in whatever was playing
each time. (CamillaDSP reports "STARTING" for about a second after such a
reload; that is its status flag lagging, the DAC is back after 0.1 s.) The
earlier estimate of 59 ms was close; the conclusion is the same.

`DspProfileSwitcher` in `voice_agent.py` now reads voice.yml through the API,
replaces its `devices` section with the one that is running and applies the
result with `config.set_active()`; at the end it applies the configuration it
found. CamillaDSP's config path never moves. The previous configuration is
kept in the runtime state file for crash recovery, under a new key
(`dsp_previous_config`). Two smaller things came with it:

- the restore in the session's teardown is wrapped like the ducking next to
  it: a failure there used to skip un-ducking and the LED;
- `VOICE_MUTE_TAIL_SECONDS` 0.4 -> 0.46, because the assistant's speech now
  leaves the speaker about 0.06 s later than it did on the faster profile.

Checks, all silent (`~/claude-project/dsp-switch-check.py`):

- `units`, 13 checks against a fake CamillaDSP: apply, restore, voice profile
  already selected by hand, configuration changed by someone else meanwhile,
  a restore that fails and is retried by the next session, crash recovery.
  The same checks fail 9 of 13 against the previous code;
- `live`, against the real CamillaDSP: three rounds of apply and restore plus
  a crash recovery, the DAC's start time and buffer geometry unchanged
  throughout, each call 21-49 ms;
- `session`: one real session on the live graph (real microphone, real Gemini
  connection, speaker to a file): voice processing in effect from +0.09 s,
  the DAC never restarted, previous configuration back at the end.

Not measured yet, because it takes sound: how much echo the chip removes
during a session and straight after one. `aec-align-check.py --session-switch
new,old` plays two 44 s bursts and switches the profile in the middle of each,
the new way and the old way. Expected: the echo does not move with the new
switch and the cancellation holds; with the old one the echo moves about
52-57 ms earlier, the cancellation collapses and then needs 10-15 s to come
back. The analysis was checked on simulated captures; the real run is the
user's (the hiss is audible).

Side effects to expect by ear: no short dropout in the music when a session
starts and ends, and the assistant's voice about 0.05 s later than before.

A parallel Claude session installed its reply-order fix into the same files
at 19:38. This change was built on that version and applied to the live file
in place. Both sides' scripts now refuse to copy over a file they do not
recognise; before that, this afternoon's `install.sh` would have dropped both
later changes, and the reply-order `rollback.sh` would have restored six of
its seven files and stopped.

## Session switch measured (19:57) and first real use (19:59)

`aec-align-check.py --session-switch new,old`, run by the user:

| | New switch (live) | Old switch |
|---|---|---|
| Echo moved while the voice profile was on | 0.0 ms | 54.7 ms earlier |
| Reference ahead of the echo meanwhile | 35.1 ms | **18.6 ms late** |
| ERLE before / during / first 3 s after / at the end | 27.8 / 28.1 / 26.3 / 28.0 dB | 0.3 / -0.3 / 5.3 / 25.6 dB |

New switch: the cancellation holds through the session; each switch costs a
dip to 22 dB for about a second (the filters change) and it is back at 28 dB
within two. Old switch: the timing is as predicted (estimate was 52-59 ms),
and after switching back the canceller needed about 6 s to reach 27 dB.

The old burst is not a clean before/after: its ERLE was already 0.3 dB during
the 20 s *before* the switch. See the next section.

Voice test, three sessions at 19:59 with music playing: all three commands
acted on ("Blake Beatles on Spike.", "Play Beatles on Spotify.", "Vai, Beatles
on s'ha fet." as transcribed; "Playing The Beatles on Spotify." each time,
said once). The switch logged `CamillaDSP -> 'Smart speaker - voice clarity'
(processing only; buffering unchanged)` 60-70 ms after each trigger and the
music profile back at the end; no errors. Wake word: three triggers (peaks
0.35, 0.29, 0.35; relayed at 0.18, 0.11, 0.35) and three near-misses of
0.05-0.07 a few seconds before the first two, which look like earlier
attempts. PipeWire logged a `resync` of the XVF3800 playback device 0.4-2 s
after each session started.

## The reference feed slips now and then (found 20:05-20:40)

Why the old burst started at 0 dB: in its saved capture the echo's timing is
fixed to one sample for the whole 20 s, while the reference tap slid 2.2 ms
earlier during the first 4.5 s. The canceller cannot follow that.

`aec-ref-timing-probe.py` (new) watches the reference's timing without
audible sound: pink noise at -75 dBFS, about 20 dB under the microphones' own
noise floor, with the chip's reference tap on capture channel 0 (unused).
Eleven bursts, 463 s in all:

- eight bursts (12-45 s): the reference did not move by more than one sample
  (0.06 ms);
- three bursts: starting about 15 s in (14.8 s and 15 s where timed), the
  reference slid 2.8-3.3 ms earlier over ~10 s, swung back past its starting
  point by about 1.2 ms, and had settled 25-40 s after it began. In the
  200 s burst there was one such slip and then nothing for 160 s.

What is established:

- the echo path does not move (speaker side is steady to 0.06 ms);
- both XVF3800 USB endpoints are synchronous (`/proc/asound/Array/stream0`),
  so the chip adds no rate matching of its own, and its clock agrees with the
  DAC's to within about 3 ppm;
- the audio waiting in the XVF playback buffer on the host follows the slip,
  but its ordinary 1 ms wander (a 25 s cycle that tracks CamillaDSP's rate
  control: -39 to -77 ppm) does not move the reference;
- the XVF playback device is closed 5 s after the last stream stops and
  reopened when the next one starts; a restart 0.6 s after closing did not by
  itself cause a slip (one of three such restarts slipped).

Not established: what triggers a slip. Suspect: PipeWire's rate matching for
the XVF playback device, which follows the aloop sink's clock. Each slip
costs the canceller roughly 20 s (the slide plus re-convergence), so with a
slip every minute or two of playback it is out a noticeable part of the time.
The sample is small.

Next step when this is picked up: raise PipeWire's log level at run time
(`pw-metadata -n settings 0 log.level 4`) while the probe runs, to see the
follower's rate-matching state during a slip. Candidate fixes to try
afterwards, each to be checked with the probe and then with
`aec-align-check.py`: keep the XVF sink running permanently (no reopening),
more headroom on it, or a shared clock name so it is not rate-matched at all.

## Why the reference slips: no realtime priority (21:15-21:35)

*Superseded on the main point: realtime priority was worth having (it removes
the whole-cycle drop-outs under load) but the slides had another cause; see
"After the reboot" at the end.*

The trigger left open above is now established, by controlled tests with the
inaudible probe under a defined CPU load (`aec-test-cpu-load.sh`: two short
CPU-bound processes and some process spawns every 2 s):

| Who is shielded from the load (alone on CPU 3) | Reference timing changes in 170 s | Lowest reading of the XVF playback buffer |
|---|---|---|
| nobody | 158, four resyncs logged by PipeWire | 31.7 ms (normally ~62) |
| PipeWire daemon and the probe's own streams; `pw-loopback` exposed | 4 steps of one cycle (5.3 ms) plus one resync | 36.5 ms |
| PipeWire daemon, the probe's streams and `pw-loopback` | **0** | 60.3 ms |

(These three ran with the experimental settings of the next paragraph.)

None of the audio threads has realtime priority: PipeWire's `data-loop`,
`pw-loopback`, `pw-cat` and CamillaDSP all run as ordinary `SCHED_OTHER`
threads (`ps -eLo cls,rtprio,comm`), `RLIMIT_RTPRIO` is 0, and the journal has
`mod.rt: RTKit error: ... ServiceUnknown` from start-up. The reference passes
through `pw-loopback`, a separate process; when its audio thread is scheduled
late, one graph cycle is dropped from the reference. PipeWire's rate matching
on the XVF3800 sink then pulls the buffer back to its target over about 30 s,
which is the "slide" seen earlier. `pw-top` counts such late cycles on other
clients too (the wake word's capture: 20 in 32 minutes).

A buffering redesign was tried at run time and reverted (kept in
`parked-2026-10-04-ref-clock/` with a note): rate matching off through a
shared `clock.name`, 1792 frames of headroom on the XVF3800 sink, loopback
delay 0. It makes the reference rigid and immune to a late PipeWire daemon,
but a late `pw-loopback` then leaves a 5.3 ms step that does not heal until
the music pauses. It treats the symptom.

Caveat on the probe, learnt the hard way: its lag is measured against the
stream it plays itself, so when its own player is late the number steps by
one cycle although echo and reference moved together. A first 13-minute soak
of the redesign looked bad for that reason (the steps coincided with commands
run on the machine); the shielded tests above are the ones to trust.

The fix is the one Debian provides and this box never switched on:
`/etc/security/limits.d/25-pw-rlimits.conf` grants members of the group
`pipewire` rtprio 95 and nice -19, `pam_limits` is in the user manager's PAM
stack (`/usr/lib/pam.d/systemd-user`), and the group has no members. So:

    sudo usermod -aG pipewire aldom     # needs the password
    sudo reboot

After the reboot, to check (all inaudible):

    ps -eLo cls,rtprio,comm | grep data-loop        # expect FF or RR, priority 83-88
    journalctl --user -b -u pipewire | grep -i rtkit   # expect nothing
    ~/claude-project/aec-test-cpu-load.sh 0-3 170 &
    ~/.venvs/voice-agent/bin/python ~/claude-project/aec-ref-timing-probe.py play:170
                                                    # expect no slides; no shielding needed
    ~/.venvs/voice-agent/bin/python ~/claude-project/aec-ref-timing-probe.py lead
                                                    # expect about 36 ms (37.1 measured 21:33)

Then the audible check, `aec-align-check.py --session-switch new,new`, and
"Hey Jarvis" over music.

State left on the device at 21:35: exactly as after the 19:49 install (rate
matching on, `XVF_AEC_REF_DELAY=0.04`, lead 37.1 ms measured inaudibly);
nothing from the experiment remains.

## After the reboot (21:49-23:45): two clock problems, both in PipeWire's graph

The user added the group and rebooted at 21:49. Realtime priority is in
place: the `data-loop` threads of PipeWire (FF 88), WirePlumber,
pipewire-pulse and `pw-loopback` (FF 83), `RLIMIT_RTPRIO` 95, no RTKit error
from PipeWire. `pw-cat`/`pw-record` clients remain single ordinary threads.

Then the user's tests went badly:

- `aec-align-check.py --session-switch new,new` at 22:13: **1.9 dB and
  -2.5 dB** removed (28 dB at 19:57), reference 41.5 ms ahead, correlation
  peak 55x and 50x the background (239x before);
- seven "Hey Jarvis, play Beatles on Spotify" over music, 22:16-22:18: two
  not detected (peaks 0.053, 0.063), and of the five sessions two played the
  Beatles, one BTS, one answered "OK." and one "Хорошо, включаю фонк".
  Transcriptions: `Păi, toți sunt spațiu.`, `Play Beatles on space.`,
  `<noise>`, `.`, `.`. Most attempts came within seconds of the music
  restarting.

### 1. The wrong node was the clock master

PipeWire makes one node of every group of linked nodes its driver, by
`priority.driver`. The program sink (snd-aloop) and the XVF3800's USB output
both had WirePlumber's default, 1009, so start-up order decided. Until the
reboot the aloop sink drove and the XVF3800 output followed (the previous
boot's debug journal, 21:33:13); after it, the other way round.

With the XVF3800 output driving, PipeWire rate-matches the aloop sink, and
snd-aloop's clock is too coarse to match against. In the recording of the
22:13:53 run (`aec-echo-track.py`, new: position of the echo and of the
reference relative to the played noise, second by second):

- reference tap: 35.256 ms, steady to 0.001 ms;
- echo: slides earlier by 0.07 ms/s (about the -39..-77 ppm CamillaDSP puts
  on the loopback) for some 22 s, snaps back 1.4 ms within 3 s, and again;
  2.0 ms peak to peak, 25 s period.

The canceller cannot follow that. Fix, live 22:31: `priority.driver = 2500`
for the aloop sink in `~/.config/wireplumber/wireplumber.conf.d/51-smart-speaker.conf`
(above the capture devices' 2009 as well), then
`systemctl --user restart wireplumber`; every stream relinked by itself.
`ai-speaker-status.sh` now has "program sink is the clock master of its
graph" (FAIL otherwise). Silent check afterwards: `aec-ref-timing-probe.py
lead` found the echo 21x above the background (5x in both runs before, where
it warned not to trust the number).

### 2. The slides were not a scheduling problem

With the aloop sink driving again and realtime priority on, the reference
still slid: 4 of 8 silent bursts (3.4 ms; 2.1 ms; +3.2 and +2.9 ms within
one burst; one starting at 48 s), each time 2-3 ms within about two seconds.
Lined up with ALSA's own buffer readings (`aec-ref-diag.py`, new):

- the aloop sink's fill saw-tooths by ~1.3 ms every 5-15 s, i.e. the moment
  each graph cycle runs shifts by a millisecond, again and again; and
  CamillaDSP's pacing of the loopback adds a slower wander of 3-4 ms over
  minutes (DAC buffer 71-75 ms). (My explanation for the saw-tooth, not
  checked in the sources: snd-aloop's position advances in kernel jiffies,
  and PipeWire's timer drifts against it and snaps back.)
- none of that moves the speaker path, which is not resampled: sample k
  leaves the DAC at DAC time k;
- it does move the XVF3800 playback buffer's fill, which the rate matching is
  there to hold. In the burst with two slides the fill fell by 2.2 ms over
  7 s, uncorrected, and was then pulled back by 2.8 ms within two seconds;
  that correction is the slide, to the half second, both times. (It looks
  like a control loop that narrows while the error is small and opens up once
  it has grown; again not checked in PipeWire's source.)

So the earlier conclusion (late `pw-loopback` cycles) explained the
whole-cycle steps of the experimental no-rate-matching setup under load, and
was wrongly carried over to the slides of the live setup. Realtime priority
cures the steps, not the slides.

Fix, live 23:00: the parked design, now that realtime priority covers its one
weakness. Both sinks get `clock.name = "smartspk.program"` (no rate matching,
samples copied through), the XVF3800 sink 1792 frames of headroom, and
`XVF_AEC_REF_DELAY=0` in `xvf-aec-ref.service`. Backups:
`51-smart-speaker.conf.bak-rate-matched-reference` and
`xvf-aec-ref.service.bak-delay-0.04-rate-matched` (restore both, `systemctl
--user daemon-reload`, restart wireplumber, then xvf-aec-ref). The status
script has "echo canceller reference is not rate-matched" (WARN otherwise).

Measured silently with it:

| | Result |
|---|---|
| Reference timing, 4 x 50 s bursts + 2 x 20 s | 0 of 392 half seconds off by more than 0.1 ms in the bursts, 0.00 ms in both 20 s runs (4 of 8 bursts slid before) |
| 170 s under `aec-test-cpu-load.sh 0-3`, probe streams at realtime priority | 0 of 338 half seconds off; `pw-top` ERR 0 on both sinks and the loopback |
| Speaker-path buffering minus XVF3800 buffering within a 110 s burst | 36.9 +- 0.3 ms although each moved by 3 ms: the two move together |
| Same, over 8 separate starts (`aec-start-spread.py`, new) | 33.2-34.5 ms, i.e. a lead of about 35 ms, 1.3 ms spread |
| `aec-ref-timing-probe.py lead` | 37.8 ms, echo 11x the background (26.7 ms one minute after the restarts, while CamillaDSP's buffer was still settling: wait a few minutes after restarting WirePlumber before measuring) |

What is left of drift: the XVF3800's and the DAC's crystals, about 3 ppm,
0.2 ms a minute. PipeWire resynchronises the XVF3800 sink once it is 21 ms
off target, which that takes about two hours of unbroken playback to reach;
every pause of 5 s or more starts it afresh anyway.

Notes for whoever measures next:

- `aec-ref-timing-probe.py play` searches only +-20 ms and its streams are
  ordinary processes. Under load a late player shows as steps of 5.33 ms
  (nine in the 21:52 run), and a bigger jump as random numbers (22:35, and
  twice in a run where I had pinned the probe to the CPU the wake word runs
  on). `aec-ref-diag.py` searches +-300 ms and takes `DIAG_RT=1`.
- The aloop sink's own fill wanders by 1.5 ms whether it drives or follows;
  it says nothing about which arrangement is running. `pw-top` does.
- The graph's quantum (256) is set by volume-led's two 10 ms `pw-record`
  streams. The reference's delay is quantum + headroom, so without that
  service the lead would shrink by 5 ms or more. Not pinned yet.
- A start of the XVF3800 sink places the reference afresh, within 1.3 ms of
  the last time. The canceller therefore re-converges when music starts after
  a pause (10-14 s). Keeping that sink running through pauses would avoid it;
  not tried.

### Audible check on the new setup (23:27)

`aec-align-check.py --session-switch new,new`, run from the session at the
user's request:

| Run | Reference lead | Before | With the voice profile | First 3 s after | At the end | Echo moved |
|---|---|---|---|---|---|---|
| 23:27:56 | 32.4 ms (peak 234x) | 28.9 dB | 28.9 dB | 26.8 dB | 28.6 dB | 0.0 ms |
| 23:28:46 | 32.0 ms (peak 200x) | 28.2 dB | 29.0 dB | 27.1 dB | 29.2 dB | 0.0 ms |

(22:13, before the two fixes: 1.9 and -2.5 dB, peak 55x and 50x.)
`aec-echo-track.py` on the second recording: reference 8.932 ms +- 0.001,
echo within 0.004 ms over 42 s (2.014 ms at 22:13).

The first run was at 29 dB by the time the residual tap went live (about
7 s into the noise). The second, 8 s after the first ended, started at 6 dB
there and reached 27 dB three seconds later: the XVF3800 sink had been
closed and reopened in between and the lead had moved by 0.4 ms. That is the
re-convergence after a pause noted above, measured.

### The user's test on the new setup (23:38-23:40)

Five "Hey Jarvis, play Beatles on Spotify", the first in a quiet room, four
over music: all five detected (peaks 0.127, 0.285, 0.452, 0.253, 0.281) and
all five called the play tool. Four replies name the Beatles (one of them in
Russian, "Включаю The Beatles."); the reply of the second session was not
logged. The user: "wake word sensitivity could be better, but overall good
enough". Tries that were not picked up, if any, scored under 0.05 and left no
trace.

The transcription lines of those sessions read `Play Beatles.`,
`طيب طاسات صوتك`, `.`, `Play wieder zum Spaß.`, `.` although the model acted
correctly each time: the logged transcription comes from a separate, weaker
recogniser than the model's own hearing and is no measure of what was
understood.

The detector's margin is what is left. All 29 wake events logged since 18:05:
22 fired, with peaks from 0.116 to 0.996 (median 0.29) against the 0.10
threshold; 7 stayed between 0.05 and 0.10, six of them real attempts by the
user during tests (18:05, three at 19:59, two at 22:17) and one unexplained
(20:35). A threshold of 0.05 would have caught those six for at most one
extra activation in about ten hours of listening, much of it with music.
Offered to the user at 23:45 and declined for now ("let's stay here now"):
the threshold stays at 0.10. Beyond a lower threshold it needs a model or
verifier for this voice.

### Where this session leaves things

Live and confirmed by measurement: realtime priority for the audio threads;
the program sink as clock master; the reference passed through unresampled,
32 ms ahead of the echo; 28.6-29.2 dB of cancellation, held through a
session's profile switch. Confirmed by the user's ear: five of five commands
over music and in quiet.

Open, none of it started:

- A reboot with the new settings. `ai-speaker-status.sh` afterwards says
  whether the clock master and the shared clock name held; the lead may come
  out a few ms different (it moved by 5 ms across the 21:49 reboot), so one
  `aec-ref-timing-probe.py lead` is worth running too.
- Wake-word sensitivity: threshold 0.05 (declined for now, see above), then a
  model or verifier for this voice. The captures of 22:16-22:18 were recorded
  with the canceller at 0 dB and are no basis for tuning; those from 23:38 on
  are.
- The canceller re-converges for up to 10 s when music starts after a pause,
  because the XVF3800 sink is closed after 5 s of silence and reopened a
  fraction of a millisecond off. Keeping it running would avoid that; then
  the 3 ppm drift is bounded only by PipeWire's resync at 21 ms.
- The graph's quantum is not pinned (it rests on volume-led's streams).
- `xvf-aec-ref.sh` still defaults to a delay of 0.341 s when the unit does
  not set `XVF_AEC_REF_DELAY`; with the headroom in place the right default
  is 0. Harmless while the unit sets it.
- One reply in five came in Russian to an English command, against the
  user's preference for English by default.
- Under the synthetic CPU load the wake word's and volume-led's capture
  streams (ordinary `pw-cat`/`pw-record` processes) lose a cycle now and
  then. It does not touch the echo canceller; whether it costs wake-word
  detections under real load has not been looked at.

## 2026-10-05: false wake-ups from speech in the room, and the verifier

Complaint, the morning after: "quite a few false activations of Hey, Jarvis".

What the journal and the captures show (09:20-10:55):

- 11 wake-ups were real (a command followed), 2 are unclear (09:48:36,
  09:50:36), 1 came from a song with vocals (09:23:49), and about 50 came
  between 10:05 and 10:55 from English speech in the room: a meeting or
  video about revenue figures, with the speaker's own music paused. That is
  one false wake-up every minute or so.
- Scores: real 0.116-0.807, false 0.101-0.497. Offline, over four frame
  phases, threshold 0.10 keeps 95% of the real ones and passes 77% of the
  false ones; 0.20 keeps 77% / passes 30%; 0.30 keeps 58% / passes 18%.
  Patience does not separate them either. The threshold is at 0.10 because
  the real phrase scores low here, and at 0.10 the stock model is far outside
  the range it was meant for.
- A false wake-up is not harmless: with only noise to go on the assistant
  set a 5-minute timer (09:23), "played Nirvana" (10:13) and set an alarm for
  10:00 (10:38).

Fix, live since 10:56: a second-stage check (`Verifier` in `wake_word.py`).
A frame at or above the threshold only counts when a logistic regression over
the same 16 x 96 embedding window agrees (probability >= 0.5). It is trained
by `~/claude-project/wake-verifier-train.py` from captures sorted by hand
into `~/.local/share/ai-speaker/wake-verifier/positive|negative` (copies, so
the 200 MB rotation of wake-captures cannot delete them); the model is
`hey_jarvis_v0.1.pkl` next to them.

Evidence, all offline on recordings:

| test | real kept | false passed |
|---|---|---|
| no verifier, threshold 0.10 | 95% | 58-77% |
| 6-fold held out, 33 real / 62 false, verifier >= 0.5 | 93% | 0% |
| trained without any of the meeting, tested on 22 meeting triggers | - | 23% (5% at 0.7) |
| trained on Oct 4 only (no false examples at all), tested on Oct 5 | 98% | 41% |
| staged Listener, 15 captures recorded after training | - | 0 of 6 that fire without it |

Limits: the real examples are one person's voice, almost all "Hey Jarvis,
play Beatles on Spotify" plus this morning's commands; the false examples are
nearly all one meeting. Rows 3 and 4 say what to expect from a kind of sound
it has never seen: better than before, not solved, until examples of it are
added. Another person's "Hey Jarvis" has not been tested and may be turned
down. The two unclear captures are rejected; if 09:50:36 was a real attempt,
that is a miss.

Maintenance: every rejected event is still captured (as `near`, with
`"verifier"` in the sidecar's notes) and journalled (`verifier=0.012
rejected`). Copy new real or false captures into the two folders and rerun
the training script; restart `wake-word.service`. Off switch:
`WAKE_WORD_VERIFIER=0` in the unit. Undo:
`staged-2026-10-05-wake-verifier/rollback.sh` (fourth stacked change; it goes
first).

Not done: the assistant acting on noise (timer, alarm, music from
`<noise>`); lowering the threshold to 0.05 now that the verifier guards it.

### Same day, 12:00-12:50: commands from a distance (open)

Four attempts from across the room over ducked music (12:04:48, 12:13:30,
12:13:58, 12:14:23, Russian): wake word fired, session timed out with no
transcription. One far attempt in a quiet room (12:00:45, English) worked.
Far speech on channel 1 is 8-12 dB over the floor in the voice band, near
speech 12-20 dB. `voice-session-replay.py` reproduces the failures (0 of 8)
and none of these changed that, about 40 runs in all: +24 dB gain, channel 0,
Gemini start-of-speech sensitivity HIGH, spectral denoising of channel 1.
The batch Gemini model did transcribe one of them from channel 0, so the
words are recoverable. Not yet tried: server VAD off with explicit activity
start/end. The experiments ran the API key into rate limits (429).

Verifier retrained at 12:46 with six far wake-ups added (39 real / 62 false;
held out 96% kept, 0% passed) after it turned down one real far attempt at
12:13:33 (0.443).

## 2026-10-08: other wake-word engines compared; second opinion and tool guard

The user: still false wake-ups, "sometimes randomly starts the timer", and
asked whether a more precise wake-word framework would fix it at the root.

**The three days since the verifier** (5 Oct 12:50 - 8 Oct 17:40): 266
triggers blocked by the verifier, 40 passed. Of the 39 sessions, 22 ended
with nothing heard, 4 with gibberish, 13 with an action.

**Comparison** (`wake-bench/`: `bench.py` -> `results.json`, `report.py`; own
venv; 485 captures). Porcupine's free tier ended on 2026-06-30 and there is no
personal plan (the key is checked at every start), so only the keyless 1.9.5
was tried. Real = 42 confirmed wake-ups, false = 62 hand-sorted; channel 1:

| engine | real heard | false passed |
|---|---|---|
| Porcupine 1.9.5 "jarvis", sensitivity 0.5 / 1.0 | 11 / 31 | 0 / 9 |
| microWakeWord "hey_jarvis", cutoff 0.97 / 0.5 | 6 / 21 | 0 / 0 |
| sherpa-onnx KWS (gigaspeech 3.3M), most eager | 5 | 0 |
| Vosk small-en, grammar [hey jarvis, jarvis, unk] | 27 | 0 |

No engine can replace openWakeWord here: every pretrained model hears this
voice on this microphone poorly. As a second opinion they are strong: "Vosk
or microWakeWord >= 0.5" passed 34 of 42 real, 0 of 62 false, and 0 of the 19
sessions of 6-8 Oct that fired without a command -- so those were false
wake-ups, about six a day through the verifier. Limits: the real set is what
openWakeWord had already caught; every capture had already excited
openWakeWord; the rule was chosen on these same captures.

**Installed 19:44** (`staged-2026-10-08-second-opinion/`, fifth stacked
change, undo first):

- `wake_confirm.py` + `wake_word.py`, `WAKE_WORD_SECOND_OPINION=log`: 0.4 s
  after a frame reaches the threshold, the last 3 s are judged on a thread of
  their own; the journal gets `second opinion: vosk=.. micro=.. -> pass|block
  (.. ms) raw_score=.. detector=ok|rejected` and the capture's sidecar a
  `second_opinion` note. It blocks nothing. Replayed frame by frame
  (`second-opinion-check.py`): 31 of 41 real, 0 of 62 false, 0 of 17 fired
  without a command, 4 of 198 verifier-blocked; about 100 ms per judgement.
  Waiting 0.8 s instead of 0.4 s gained one real wake-up.
- `tool_guard.py` + `voice_agent.py`, `VOICE_TOOL_GUARD=set_timer,set_alarm`:
  every tool call is journalled (`Tool call: name(args) heard=[...]`), and a
  timer or alarm is refused in a session with no intelligible transcription.
  Narrower than first planned, on purpose: two sessions acted on a
  transcription of '.' (6 Oct 08:24 Amy Winehouse, 7 Oct 19:34 volume) and
  both look like real commands whose transcription failed, so other tools
  only log `Would refuse`. Room speech is transcribed as words and is not
  stopped by this.

**Next:** after a few days, count `second opinion:` lines against what
happened (`detector=ok ... -> block` followed by a real command = a wake-up it
would have cost; `-> block` followed by an idle session = a false wake-up it
would have saved), then decide whether it may block. If it costs too many
real wake-ups: a model trained on this voice (about 100 recordings).

## 2026-10-09: the second opinion blocks

One day of log-only (8 Oct 19:44 - 9 Oct 20:00): 12 voice sessions. Two were
real commands (08:16, 09:14), second opinion `pass` on both. Ten were false,
`block` on all ten (for 08:24 and 11:23 the verdict was logged against the
verifier-rejected frame just before the one that fired). Two of the false
sessions listened to a conversation in the room for 80 s and 96 s and answered
aloud; in one, `cancel_alarm` was called on `<noise>`. User: "still a few
false positive activations. Can we improve?", then "Yes" to letting it block.

**Installed 20:20** (`staged-2026-10-09-second-opinion-blocks/`, sixth stacked
change, undo first; softer: `WAKE_WORD_SECOND_OPINION=log`):

- `wake_word.py` relays a detection as `voice-pending`; `SecondOpinion.decide()`
  judges the 3 s ending about 0.24 s after it (delay 0.16 s: same result on the
  captures as 0.4 s, 31 of 39 real) and `WakeTrigger.settle()` relays
  `voice-confirm` or `voice-reject`. A judgement that fails confirms.
- `voice_agent.py`: on `voice-pending` only the microphone opens; chime, LED,
  DSP switch, ducking and Gemini wait for the verdict (`VOICE_CONFIRM_TIMEOUT_SECONDS`
  1.5 s, then it goes ahead). On reject the capture is dropped: "Wake word not
  confirmed; no session". Cost to a real wake-up: about 0.35 s later chime.
- `VOICE_TOOL_GUARD` now also holds cancel_timer and cancel_alarm.

Checked: unit tests in the staged folder (`test_wake_confirm`, `test_wake_trigger`,
`test_voice_agent_confirm`, `test_tool_guard`); the staged agent on the real
graph with its own socket, and then the live one, given `voice-pending` +
`voice-reject` (microphone opened and closed, nothing else); one recorded
command replayed through the staged session code. NOT yet seen: a real spoken
wake-up through the confirm path.

**Expect:** about one real wake-up in five blocked (recordings; live 2 of 2
passed). A blocked real one shows as `Wake word blocked by the second opinion`
in the wake-word journal and its capture's sidecar has `second_opinion.passed
= false`, `tentative = true`. If that happens too often: loosen
`WAKE_WORD_SECOND_OPINION_MICRO_CUTOFF` / `_VOSK_CONFIDENCE` after checking the
false ones against them, or train a model on this voice.
