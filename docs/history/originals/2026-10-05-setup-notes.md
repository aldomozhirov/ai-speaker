# AI Speaker: Setup Decisions and History

Working notes on the ReSpeaker XVF3800 + PipeWire + wake-word + Gemini Live
voice agent stack running on this Raspberry Pi. Written so the same setup can
be reproduced or debugged later without re-deriving everything from scratch.
Covers decisions made across this session and the one before it.

## System overview

Eight systemd `--user` services, in the order they should come up:

| Unit | Purpose |
|---|---|
| `xvf-aec-ref.service` | Feeds program audio into the XVF3800's echo canceller as the "far end" reference |
| `volume-led.service` | Drives the ReSpeaker's LED ring to show volume/listening state |
| `volume-sync.service` | Mirrors the system volume (wpctl) into whatever media player is active over MPRIS |
| `volume-buttons.service` | Reads the Pi's GPIO volume buttons |
| `welcome-sound.service` | Oneshot: plays a welcome chime once at boot (see "Sound feedback" below) |
| `wake-word.service` | Runs openWakeWord (`hey_jarvis`) against the XVF3800's processed capture, triggers the voice agent |
| `voice-agent.service` | Pipecat + Gemini Live conversation engine |
| `spotifyd.service` | Spotify Connect receiver, the primary audio source |

All are `WantedBy=default.target` under the `aldom` user, and **systemd
lingering is required** (`loginctl show-user aldom -p Linger` must say
`Linger=yes`) for user services to start at boot without an interactive
login. This is easy to lose track of because it isn't recorded in any
`.service` file — it's set once via `sudo loginctl enable-linger aldom` and
persists in `/var/lib/systemd/linger/aldom`. If a fresh Pi image or user
migration ever loses this, none of the above will start on boot even though
every unit is correctly enabled.

Code lives in `~/.local/bin/`. Python services each get their own venv under
`~/.venvs/` (`wake-word`, `voice-agent`, `xvf3800`) rather than sharing one,
because the XVF3800 control library needs `pyusb` and openWakeWord/pipecat
pull in enough of their own dependency weight that keeping them isolated
avoids version fights.

## The XVF3800 echo-cancellation problem (the throughline of both sessions)

The ReSpeaker XVF3800 does on-chip beamforming and acoustic echo cancellation
(AEC), but only if it's told what its own output sounds like — the "far end"
reference signal. Getting that reference signal routed correctly, and kept
routed correctly across reboots, was most of the work.

### Why a reference feed is needed at all

CamillaDSP owns `hw:sndrpihifiberry` (the real speaker output) and applies
DSP/EQ before the XVF3800 ever sees the signal. The XVF3800's AEC needs a
copy of what's actually playing, tapped **before** CamillaDSP's processing
(from the aloop sink's monitor), fed into the **left channel only** of the
XVF3800's own USB playback input (it discards the right channel). The
XVF3800's own analog/USB audio output must stay disconnected — CamillaDSP
remains the sole owner of the physical speaker.

### Three failed approaches, and why (see comments in `xvf-aec-ref.sh`)

1. **Static `pipewire.conf.d` module-loopback**, loaded inline at PipeWire
   daemon startup. Failed because it loads before WirePlumber has created
   the XVF3800 USB node, so `target.object` never resolves and it silently
   falls back to the default sink. Broken on every reboot.
2. **`pw-loopback` client stream**, assuming client streams get
   WirePlumber's late-binding retry for named targets. Same failure.
3. **Wait loop polling `pw-cli ls Node`** before starting `pw-loopback`.
   Still failed: the node object appears in the graph before WirePlumber has
   finished probing/negotiating the USB ALSA device, so a stream started
   right after "node exists" can still resolve too early — and once
   WirePlumber commits a link (right or wrong), it never revisits it.

### Current approach: supervise the link state, don't predict readiness

`xvf-aec-ref.sh` stopped trying to predict when WirePlumber is ready and
instead treats the observed failure mode (falling back to
`alsa_output.platform-snd_aloop.0.analog-stereo`, the system default sink —
looping its own output back into its own capture source) as a known-bad
state to actively correct:

```
reconcile() {
  pw-link -d "output.xvf-aec-ref:output_FL" "$CAPTURE_TARGET:playback_FL"
  pw-link -d "output.xvf-aec-ref:output_FR" "$CAPTURE_TARGET:playback_FR"
  pw-link "output.xvf-aec-ref:output_FL" "$PLAYBACK_TARGET:playback_FL"
}
```

Disconnecting a link that doesn't exist and reconnecting one that's already
correct are both harmless no-ops, so this can run repeatedly and cheaply.
Reconciled every 1s for the first 60s after start (while the graph is still
settling right after boot), then every 30s for the life of the service (to
catch a USB replug racing the same way later).

Target node names are resolved from `wpctl status` at write-time — they are
**not** stable IDs and must not be hardcoded as numbers.

### The register that also resets: `AUDIO_MGR_FAR_END_DSP_ENABLE`

Routing the reference audio correctly is necessary but not sufficient — the
XVF3800 also has a chip register, `AUDIO_MGR_FAR_END_DSP_ENABLE`, that
actually turns echo cancellation on. **This register lives in chip RAM, not
flash**, so it resets to 0 (echo cancellation structurally off) on every
power cycle or USB re-enumeration. It was found off and fixed by hand once
early on, then found off again after a later reboot — the routing can be
100% correct and echo cancellation will still silently do nothing if this
register hasn't been reapplied.

`xvf-aec-ref.sh` now reapplies it on every start, with a **retry loop**
(up to 30 attempts, 2s apart) rather than a single write — a single attempt
can lose the race against USB enumeration at boot, and unlike other
failures in this stack, a failed write here is silent and total: the rest
of the audio graph still looks completely healthy with echo cancellation
off underneath it. There's also a periodic re-check every ~5 minutes for
the life of the service, to catch a USB replug that resets the register
without killing the `pw-loopback` process (so the startup-only retry
wouldn't otherwise run again).

`AEC_AECCONVERGED` is a **read-only** status register that reports whether
the echo model has actually converged. Through the entire previous session
it read `0` no matter what was tried (external delay tuning,
`AUDIO_MGR_SYS_DELAY`, waiting longer) and was concluded to be "genuinely
blocked" pending real XMOS documentation. **As of this session it reads
`1`** — a change from every prior observation. It's not fully understood why
(most likely: extended natural convergence time now that
`AUDIO_MGR_FAR_END_DSP_ENABLE` reliably stays on, rather than flapping).
This matters because it may materially change the still-open "wake word
becomes far less sensitive when music is playing" problem (see below) —
that problem was suspected to trace back to AEC not converging under real
program audio, which had never actually been observed working until now.

### Everything echo cancellation depends on (as of 2026-10-04)

Routing and the register above turned out to be two of eight conditions.
Most of the others also failed silently at some point: every unit active,
reference present, little or nothing cancelled. All of them, with where each
is enforced and how to check it:

| Condition | Enforced by | Check |
|---|---|---|
| Reference reaches the XVF3800's playback input | `xvf-aec-ref.sh` (link supervision, above) | `ai-speaker-status.sh` |
| `AUDIO_MGR_FAR_END_DSP_ENABLE` = 1 | `xvf-aec-ref.sh` (above) | `ai-speaker-status.sh` |
| Reference not attenuated: the XVF3800 sink's volume is the chip's hardware mixer and must be 1.0 | `xvf-aec-ref.sh` (`assert_reference_gains`) | `wpctl get-volume` on that sink reads 1.00; background in `AEC-REFERENCE-GAIN-FINDING.md` |
| Reference arrives a little *before* the echo (20-36 ms works; late cancels nothing, 67 ms early converges very slowly) | `api.alsa.headroom = 1792` on the XVF3800 sink in `~/.config/wireplumber/wireplumber.conf.d/51-smart-speaker.conf`, plus `XVF_AEC_REF_DELAY=0` in `xvf-aec-ref.service`; the two add up | `aec-ref-timing-probe.py lead` (inaudible), `aec-align-check.py` (audible; also says how many dB are removed) |
| Reference does not move in time: PipeWire must not rate-match (resample) the XVF3800 sink | the same `clock.name` on both sinks in `51-smart-speaker.conf` | `ai-speaker-status.sh` (WARN), `aec-ref-diag.py` |
| Echo does not move in time: the program sink (snd-aloop) must be the PipeWire graph's clock master, so that nothing on the speaker path is resampled | `priority.driver = 2500` on the program sink in `51-smart-speaker.conf` | `ai-speaker-status.sh` (FAIL), `pw-top` |
| Speaker-path buffering does not change during a voice session | `DspProfileSwitcher` in `voice_agent.py` (applies the voice profile's processing, keeps the running devices) | `dsp-switch-check.py`, `aec-align-check.py --session-switch new,new` |
| Audio threads have realtime priority | user `aldom` in group `pipewire` (`/etc/security/limits.d/25-pw-rlimits.conf`) | `ps -eLo cls,rtprio,comm \| grep data-loop` |

"A little before" and "does not move" are tight: the chip's canceller takes
on the order of ten seconds to converge and loses its solution when either
signal shifts by a few samples. With all eight in place it removes 28-29 dB of pink noise
(measured 2026-10-04 23:28); with the clock master wrong it removed 0 dB.
How each was found, with the measurements:
**`WAKE-WORD-RELIABILITY-FOLLOWUP-2026-10-04.md`**.

### `xvf_register.py`: a quiet CLI for the vendored register library

The vendored `xvf_host.py`
(`~/src/reSpeaker_XVF3800_USB_4MIC_ARRAY/python_control/`) has no CLI of its
own and prints every ReadCMD/WriteCMD to stdout unconditionally, which
previously meant every caller re-implemented "open device, touch one
register, close" plus grep-based output filtering from scratch.
`~/.local/bin/xvf_register.py` wraps that once:

```
xvf_register.py get    NAME              print current value(s)
xvf_register.py set    NAME VAL [VAL..]  write value(s)
xvf_register.py ensure NAME VAL          write only if different; prints ok/fixed
```

Must run under `~/.venvs/xvf3800/bin/python` (needs `pyusb`). Used by both
`xvf-aec-ref.sh` (the boot-time/periodic register fix-up, via `ensure`) and
`ai-speaker-calibrate.py` (read-only health checks, via `get`). One bug
worth remembering if this is extended: `device.read(name)` returns `None`
for an unrecognized register name rather than raising — `get` explicitly
checks for `None` and raises `KeyError` so a typo doesn't produce a
confusing `'NoneType' object is not iterable`.

## Wake word and voice-agent tuning decisions

- `WAKE_WORD_THRESHOLD=0.35`, `WAKE_WORD_CHANNEL=0`, model `hey_jarvis`,
  patience 1, cooldown 1.5s — current tuned values in
  `wake-word.service`. The XVF3800's L/R USB capture channels are
  **independently configured on this unit, not a true stereo pair** —
  which channel performs better is a per-unit, per-environment question,
  which is why the calibration script always measures both at once rather
  than assuming channel 0 is correct.
- `VOICE_HALF_DUPLEX=1` in `voice-agent.service`: the mic is muted while the
  bot is speaking. This was deliberately chosen over full-duplex barge-in —
  residual echo from the XVF3800's AEC is louder than the user's actual
  voice when both are active, so full duplex caused the assistant to
  interrupt and effectively answer itself. A prior attempt at enabling
  barge-in via a systemd drop-in was tested and reverted; there are
  currently no drop-ins under `~/.config/systemd/user/*.service.d/`.
- `VOICE_DUCK=1`, `VOICE_DUCK_LEVEL=0.20`: ducking is stream-level only
  (never CamillaDSP's global volume, never the default sink/physical
  volume owned by `volume-sync.service`), and applies for the whole
  session rather than per bot utterance, because unducked music trips the
  session's Silero VAD.
- `VOICE_CONTEXT_COMPRESSION=1`, `VOICE_MAX_SESSION_SECONDS=1800`: sliding
  context-window compression is what lets a conversation outlive Gemini's
  free-tier 15-minute audio-session cap and 128k context window; pipecat
  does not enable this by default.
- **"Quick action" session close**: volume and Spotify function calls (in
  `volume_tools.py`, `spotify_tools.py`) call
  `session_signal.mark_quick_action()` on success, and `voice_agent.py`
  checks/consumes that signal (`session_signal.consume_quick_action()`) in
  its `BotStoppedSpeakingFrame` handler to end the session immediately
  after a simple command like "turn it up" or "pause", rather than sitting
  open waiting for more conversation. Deployed and code-verified present,
  but **not yet live-tested by voice** — worth confirming with an actual
  "hey jarvis, pause" style command.

## Still open

- **Music-under-wake-word sensitivity**: wake-word detection range drops
  to roughly 10cm when music is playing, versus normal range in a quiet
  room. Root cause was suspected to be AEC not converging under real
  program audio (channel-swap was tried and ruled out as a fix). Given
  `AEC_AECCONVERGED` now reads `1` for the first time this whole
  investigation, this needs to be **re-measured**, not assumed fixed or
  assumed still broken — run `ai-speaker-calibrate.py all` (see the
  calibration script's own README) to get a current number.
- **Quick-action session close**: deployed, not yet confirmed working by
  the user via an actual voice interaction.
- **Phase 8.3** (Spotify search + `spotify_play_track(query)`) is queued
  from the original 3-phase voice-agent plan but not started.
- **Spotify Connect chime**: deployed via `spotifyd`'s `on_song_change_hook`
  (fires on every `PLAYER_EVENT`, not just song changes). Initial guess at
  the event name (`session_connected`, from binary strings alone) was
  **wrong and silently never fired** in real testing — confirmed live
  2026-08-22 via `journalctl -t spotifyd-hook` that spotifyd actually emits
  `sessionconnected` (no underscore; its event-to-string conversion strips
  underscores from librespot's `SessionConnected` variant name). Fixed in
  `spotifyd-event-hook.sh` and reconfirmed working.

## Sound feedback (chimes)

`sound_fx.py` plays four short confirmation sounds — session start,
session end, Spotify Connect, boot welcome — from
`~/.local/share/ai-speaker/sounds/*.mp3` (moved there from `~/sounds`).
Playback path: `ffmpeg` decodes to raw PCM, piped into `pw-cat --raw`
targeting `VOICE_SINK` (`alsa_output.platform-snd_aloop.0.analog-stereo`)
— the same aloop sink CamillaDSP reads from and the XVF3800 AEC reference
is tapped from, so a chime is echo-cancelled out of mic capture the same
way bot speech and music already are, and never touches
`hw:sndrpihifiberry` directly. `ffmpeg`/`pw-cat` are invoked by absolute
path (`/usr/bin/ffmpeg`, `/usr/bin/pw-cat`), matching `duck.py`'s
convention of not trusting `$PATH` — cheap insurance since this module runs
under both `voice-agent.service` and, via the hook script below, as a
child of `spotifyd.service`.

Two entry points:

- `play()` — fire-and-forget (backgrounded in a daemon thread with its own
  reaper, no zombie children left behind). Used for **all three** chimes:
  session-started, session-ended, and the Spotify Connect chime from
  `spotifyd-event-hook.sh`.
- `play_blocking()` — exists but currently unused by anything in this
  stack; kept for a caller that genuinely needs to wait for playback to
  finish.

**session-started was originally wired to `play_blocking()`, and that was a
regression found and reverted the same day (2026-08-22).** The idea was
"play while the mic isn't listening" as a hard guarantee rather than relying
on AEC (this box's residual is measured louder than the user's own voice,
see `AudioGate` in `pw_transport.py`), by blocking `_run_session` for the
chime's ~2s duration before constructing the Pipecat transport at all. In
practice this blocked *only* mic-open, not the LED: `send_led_state
("listening")` already fires before ducking, ~2s earlier. A user who trusts
the LED and starts talking immediately gets nothing but silence recorded,
the session sits on `VOICE_IDLE_SECONDS=8.0` of near-zero peak amplitude,
and times out having heard nothing — confirmed in
`journalctl --user -u voice-agent.service` (peak amplitude readings around
1-2, essentially digital silence, for the full 8s). Switched to `play()`:
the chime's tail now overlaps the very start of mic capture instead, which
the AEC reference (tapped from the same sink) at least partially cancels,
same as it does for music -- imperfect, but strictly better than a
guaranteed-dead mic during the exact window right after the wake trigger.

`SOUND_FX_ENABLED=1` (default on) in `voice-agent.service` is the kill
switch for both voice-agent chimes; `SOUND_FX_VOLUME` (default `0.6`,
stream volume via `pw-cat --volume`) and `SOUND_FX_DIR`/`SOUND_FX_SINK`/
`SOUND_FX_RATE` are read directly from the environment by `sound_fx.py`
itself (not yet promoted to `voice-agent.service`'s env block — add there
if they need tuning without editing the module).

### Boot welcome chime — the one that actually needs a boot-race guard

`welcome-sound.service` (new, oneshot, `WantedBy=default.target`) runs
`welcome-sound.sh`, which plays `welcome.mp3` once per boot. This is a
different risk class from the other three chimes: session-start/end and
Spotify Connect only ever fire well after boot from a real user trigger, by
which point `VOICE_SINK` is already proven to exist because
`voice-agent.service` and `spotifyd.service` depend on it too. A boot-time
chime has no such guarantee.

Confirmed by hand (2026-08-22): `pw-cat` does **not** error out when
`--target` doesn't resolve to a real node -- it silently falls back to
whatever the default sink is, the same failure class as the XVF3800 AEC
reference routing problem (§ above). Ordering the unit `After=` PipeWire
alone would repeat that mistake. Instead `welcome-sound.sh` polls
`pw-dump` for the literal `"node.name": "<target>"` string (up to 30 x
0.5s = 15s) before ever invoking `sound_fx.py`, and exits 0 either way --
a missed welcome chime must not show up as a failed unit and pollute the
`systemctl --user --failed` health check the rest of this stack relies on.
In practice the aloop sink (a kernel ALSA loopback, not a USB device) has
never been observed to take anywhere near 15s, unlike the XVF3800's USB
negotiation delay, but the guard costs nothing to keep.

**Verified 2026-08-22 by a full dependency-order service-restart** (stop
everything, start `xvf-aec-ref` → `volume-led` → `volume-sync` →
`volume-buttons` → `welcome-sound` → `wake-word` → `voice-agent` →
`spotifyd`, matching the existing reboot-safety method in this doc): zero
failed units, `welcome-sound.service` reports `Result=success` /
`ExecMainStatus=0`, and the Spotify Connect hook and voice-agent's
`SOUND_FX_ENABLED` env both still resolved correctly afterward. This is a
service-restart test, not a literal power cycle, same caveat as the rest of
this section -- an actual reboot has not been performed for this feature
specifically.

## Persistence / reboot-safety verification

Confirmed this session by restarting the full service stack in dependency
order (`xvf-aec-ref` → `volume-led` → `volume-sync` → `volume-buttons` →
`wake-word` → `voice-agent`, plus `spotifyd`) and checking:

- All units report `active`, `systemctl --user --failed` is empty.
- `WAKE_WORD_THRESHOLD=0.35`, `VOICE_HALF_DUPLEX=1`, `VOICE_DUCK_LEVEL=0.20`,
  `WAKE_WORD_CHANNEL=0` all read back correctly from the unit files (no
  drop-ins, no stray `.bak` files under `~/.config/systemd/user/`).
- `AUDIO_MGR_FAR_END_DSP_ENABLE` reads back `1` after the restart (the
  chip-RAM register survives the service restart because
  `xvf-aec-ref.sh`'s startup retry loop reapplies it).
- The AEC reference audio link resolves correctly (not falling back to the
  aloop sink).
- `loginctl show-user aldom -p Linger` → `Linger=yes`.

This is a service-restart test, not an actual power cycle, so it doesn't
prove USB re-enumeration timing behaves identically — but combined with
confirmed lingering and the register/link self-healing logic described
above, it gives reasonable confidence the stack is reboot-safe. A real
reboot test has not been performed (would end the working session) and is
a reasonable thing to do once, as final confirmation, at a convenient time.

**2026-10-04: a real reboot, and what it showed.** Every unit came up and
every check was green, but the echo canceller removed 0 dB: the reboot had
changed which node clocks PipeWire's graph (the program sink and the
XVF3800's USB output had equal `priority.driver`, so start-up order
decided). Fixed the same evening, and `ai-speaker-status.sh` now checks the
clock master and that the reference is not rate-matched (see "Everything echo
cancellation depends on" above). The settings in this list are also out of
date; the current ones are in the section on wake-word reliability below. A
reboot with the fixed configuration has not been done yet: run
`ai-speaker-status.sh` after the next one.

## Timer & alarm (2026-08-23)

Two new offline tools on top of local_intent.py's grammar-constrained
command matching: "set a timer for 10 minutes" / "set an alarm for 7:15",
scheduled and rung by a new `timer-alarm.service`, stopped by saying "stop"
(open-mic, no wake word) or the wake button. Built, not yet enabled or
live-tested. Full detail: **`TIMER-ALARM-IMPLEMENTATION.md`**.

## Flexibility/reliability improvements (2026-08-23)

A separate pass consolidated the "pw-cat silently falls back to the default
node" bug pattern into one shared helper (`pw_node.py`, now used by
`pw_transport.py`, `welcome-sound.sh`, and — newly guarded, previously
unprotected — `wake_word.py`), added a one-shot health check script
(`ai-speaker-status.sh`), actually implemented the `VOICE_ENGINE` switch
this doc's companion (`WAKE_WORD_GEMINI_INTEGRATION.md`) said was the
intent from day one, and added optional offline command matching
(`local_intent.py`, off by default). Full detail, verification status, and
what's still unconfirmed: **`AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md`**.

## Wake-word reliability and echo-cancellation timing (2026-08-30, 2026-10-04)

The user's complaint of 2026-08-30 (wake word unreliable in a quiet room and
worse over music, false activations, commands not understood) was worked
through on 2026-10-04. In short: one utterance used to fire the wake word up
to ten times and every session started four seconds late (fixed by turning
the offline intent path off and rewriting the wake-word loop); Gemini was
being fed the wrong microphone channel and a stream that began in mid-speech
(now channel 1, +12 dB, with a silent lead-in); and the echo canceller was
not cancelling, for the timing reasons in the table above. Settings as that
day left them: `WAKE_WORD_THRESHOLD=0.10`, `WAKE_WORD_CHANNEL=1`,
`LOCAL_INTENT_ENABLED=0`, `WAKE_WORD_CAPTURE=1` (near-trigger recordings in
`~/.local/share/ai-speaker/wake-captures`), `VOICE_INPUT_CHANNEL=1`,
`VOICE_INPUT_GAIN_DB=12`, `XVF_AEC_REF_DELAY=0`.

State at the end of 2026-10-04: 28-29 dB of echo cancellation, five of five
spoken commands acted on in the user's last test; the user's verdict, "wake
word sensitivity could be better, but overall good enough". The detector
scores this voice at 0.12-0.5 against the 0.10 threshold; lowering it to
0.05 was offered and declined for now.

Full detail: **`WAKE-WORD-RELIABILITY-CONTEXT-2026-08-30.md`** (the
complaint and the state before) and
**`WAKE-WORD-RELIABILITY-FOLLOWUP-2026-10-04.md`** (causes, measurements,
what changed, what is open).

### Wake-word verifier (2026-10-05)

`wake_word.py` asks a second, small model before a detection counts: it is
trained on this household's own captures (`wake-verifier-train.py`, data and
model in `~/.local/share/ai-speaker/wake-verifier/`), because at threshold
0.10 ordinary speech in the room fired the stock model about once a minute.
Unit settings: `WAKE_WORD_VERIFIER` (0 = off), `WAKE_WORD_VERIFIER_THRESHOLD`
(0.5). Numbers, limits and how to retrain: last section of
`WAKE-WORD-RELIABILITY-FOLLOWUP-2026-10-04.md`.

## Related files

- `~/.config/wireplumber/wireplumber.conf.d/51-smart-speaker.conf` — the
  program sink's format, its place as clock master, and the unresampled
  echo-canceller reference; commented with the measurements behind each.
- `~/claude-project/aec-align-check.py` (audible), `aec-ref-timing-probe.py`,
  `aec-ref-diag.py`, `aec-start-spread.py` (inaudible), `aec-echo-track.py`
  (analysis of the last audible run), `dsp-switch-check.py` — the
  echo-cancellation measurements; each has its usage in its docstring.
- `~/claude-project/wake-replay.py`, `voice-session-replay.py` — replay
  recorded wake captures through the detector and through a real session.
- `~/.local/bin/xvf-aec-ref.sh` — AEC reference routing + register fix-up,
  heavily commented with the failure history above.
- `~/.local/bin/xvf_register.py` — quiet CLI over the vendored XVF3800
  register library.
- `~/.local/bin/ai-speaker-calibrate.py` — ad-hoc mic/sensitivity
  calibration tool; see `~/.local/bin/README-calibrate.md` for details.
- `~/src/reSpeaker_XVF3800_USB_4MIC_ARRAY/python_control/xvf_host.py` —
  vendored register access library (third-party, not ours to modify).
- `~/.local/bin/pw_node.py` — shared PipeWire node-readiness poll (see
  `AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md` § 1).
- `~/.local/bin/ai-speaker-status.sh` — one-shot health check covering every
  unit above plus AEC registers, PipeWire routing, and Gemini reachability
  (see `AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md` § 2).
- `~/.local/bin/timer_alarm.py` / `timer_alarm_client.py` — offline timer &
  alarm service and its socket client (see `TIMER-ALARM-IMPLEMENTATION.md`).
