# Building Wake-Word + Gemini Live Voice Control on the AI Speaker

A build guide for the voice conversation layer on top of the existing
ReSpeaker XVF3800 + PipeWire + CamillaDSP smart-speaker stack: wake word
detection, speech-to-speech via Gemini Live, barge-in/ducking, and the
first function-calling tools. Written as a walkthrough of the steps in the
order you'd actually do them, with the reasoning and dead ends kept in
place as callouts — most of the hard-won knowledge here is in *why* the
obvious first approach didn't work, not just in the final config.

**Status: Steps 1–8.2 are done and running in production. Step 8.3
(Spotify catalog search) and Home Assistant integration are the only
pieces not yet built.**

Companion docs:
- `AI-SPEAKER-SETUP-NOTES.md` — a narrower, deeper dive specifically on
  the AEC/PipeWire routing problem (Step 1 below) and this session's
  reboot-persistence verification.
- `~/.local/bin/README-calibrate.md` — the `ai-speaker-calibrate.py` tool
  referenced in Step 3.
- `PHASE-6-7-PLAN.md` — the original, more detailed working notes for
  Steps 6 and 7, including a full findings table and exit-criteria
  checklists.
- `AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md` — the `VOICE_ENGINE` switch
  (Step 5) and local intent matching (Step 8.3) below are now actually
  implemented; this doc has the detail and verification status.

---

## Architecture at a glance

Seven systemd `--user` services, running as `aldom` (UID 1000), in the
order they should come up:

| Unit | Purpose |
|---|---|
| `xvf-aec-ref.service` | Feeds program audio into the XVF3800's echo canceller as the "far end" reference |
| `volume-led.service` | Drives the ReSpeaker's LED ring — volume state, and (Step 4) assistant state |
| `volume-sync.service` | Mirrors the system volume (`wpctl`) into whatever media player is active over MPRIS |
| `volume-buttons.service` | Reads the Pi's GPIO volume + wake buttons |
| `wake-word.service` | Runs openWakeWord against the XVF3800's processed capture, triggers the voice agent |
| `voice-agent.service` | Pipecat + Gemini Live conversation engine |
| `spotifyd.service` | Spotify Connect receiver, the primary audio source |

All are `WantedBy=default.target`. Code lives in `~/.local/bin/`; each
Python service gets its own venv under `~/.venvs/` (`wake-word`,
`voice-agent`, `xvf3800`) rather than sharing one, because the XVF3800
control library needs `pyusb` and openWakeWord/pipecat each pull in
enough dependency weight that isolating them avoids version fights.

**Prerequisite, easy to forget**: systemd lingering must be enabled
(`loginctl show-user aldom -p Linger` → `Linger=yes`, set once via
`sudo loginctl enable-linger aldom`) or none of this starts at boot
without an interactive login. It isn't recorded in any `.service` file —
only in `/var/lib/systemd/linger/aldom` — so it's invisible until you
specifically check for it.

### Decisions locked in before you start

Don't re-litigate these without a reason:

| Choice | Decision | Why |
|---|---|---|
| Engine | Gemini Live, native speech-to-speech (currently `models/gemini-2.5-flash-native-audio-preview-12-2025`, env-configurable) | Handles STT+LLM+TTS+barge-in+function-calling in one API; free tier |
| Wake word | openWakeWord (Apache-2.0), `hey_jarvis` pretrained | Picovoice/Porcupine has no personal/non-commercial plan |
| Orchestration | Pipecat, custom PipeWire transport (see Step 5) | Engine is swappable behind `VOICE_ENGINE` (implemented 2026-08-23, see below); the model is `preview` and will churn |
| AEC | XVF3800 hardware | Needs a reference feed that didn't exist at project start — Step 1 |
| Spotify | `spotifyd`, not raspotify/librespot | Raspotify is only a leftover apt source on this box; nothing is installed from it |
| Scope | Personal, non-commercial, free tiers only | No paid subscription may be required for the build to work |

---

## Step 1 — Get the AEC reference feed working ⚠️ do this first, nothing else works without it

The XVF3800 does on-chip beamforming and echo cancellation, but only if
it's told what its own output sounds like. CamillaDSP owns
`hw:sndrpihifiberry` and applies DSP/EQ before the XVF3800 ever sees the
signal, so the AEC needs a *separate* copy of the program audio — tapped
**before** CamillaDSP's processing, from the aloop sink's monitor — fed
into the **left channel only** of the XVF3800's own USB playback input
(it discards the right channel). The XVF3800's own analog/USB output must
stay disconnected the whole time: CamillaDSP remains the sole owner of
the physical speaker, always.

### Pitfalls hit along the way (worth knowing before you try your own variant)

Three approaches were tried and failed before landing on the one below.
If you find yourself reaching for one of these, stop — it's been tried:

1. **A static `pipewire.conf.d` module-loopback**, loaded inline at
   PipeWire daemon startup. Fails because it loads before WirePlumber has
   created the XVF3800 USB node, so `target.object` never resolves and it
   silently falls back to the default sink. Broken on every reboot.
2. **A `pw-loopback` client stream**, on the assumption that client
   streams get WirePlumber's late-binding retry for named targets. Same
   failure as #1.
3. **A wait loop polling `pw-cli ls Node`** before ever starting
   `pw-loopback`. Still fails: the node object appears in the graph
   *before* WirePlumber has finished probing/negotiating the USB ALSA
   device, so a stream created right after "node exists" can still
   resolve too early — and once WirePlumber commits a link (right or
   wrong), it never revisits it.

### The approach that works: supervise the link, don't predict readiness

Stop trying to predict exactly when WirePlumber is ready. Instead,
supervise the actual link state after the fact — the observed fallback
target is always the same known sink
(`alsa_output.platform-snd_aloop.0.analog-stereo`, the system default),
so explicitly disconnect that specific wrong link and (re)connect the
correct one, repeatedly, until it sticks:

```bash
reconcile() {
  pw-link -d "output.xvf-aec-ref:output_FL" "$CAPTURE_TARGET:playback_FL"
  pw-link -d "output.xvf-aec-ref:output_FR" "$CAPTURE_TARGET:playback_FR"
  pw-link "output.xvf-aec-ref:output_FL" "$PLAYBACK_TARGET:playback_FL"
}
```

Both operations are harmless no-ops when already correct (disconnecting a
link that doesn't exist, reconnecting one that's already right), so this
is cheap to run on a loop forever. Shipped as `~/.local/bin/xvf-aec-ref.sh`
/ `xvf-aec-ref.service`: reconciled every 1s for the first 60s after
start (while the graph is still settling right after boot), then every
30s for the service's whole lifetime (to catch a later USB replug racing
the same way). Resolve node names from `wpctl status` output at
write-time — never hardcode numeric IDs, they aren't stable across
reboots.

### A second gotcha: the register also resets

Correct routing is necessary but not sufficient. The XVF3800 has a chip
register, `AUDIO_MGR_FAR_END_DSP_ENABLE`, that actually turns echo
cancellation on — and **it lives in chip RAM, not flash**, so it resets to
0 (echo cancellation structurally off) on every power cycle or USB
re-enumeration. It was fixed by hand once, then found off again after a
later reboot: the routing can be 100% correct and cancellation will still
silently do nothing if this register hasn't been reapplied.

Fix: reapply it on every service start, with a retry loop rather than one
write — a single attempt can lose the race against USB enumeration at
boot, and a failed write here is uniquely dangerous because it's
**silent and total**: the rest of the audio graph still looks completely
healthy with cancellation off underneath it.

```bash
enable_far_end_dsp() {
  "${XVF_REGISTER[@]}" ensure AUDIO_MGR_FAR_END_DSP_ENABLE 1 >/dev/null 2>&1
}
for attempt in $(seq 30); do
  enable_far_end_dsp && break
  sleep 2
done
```

Plus a periodic re-check every ~5 minutes for the service's whole
lifetime, since a USB replug can reset the register without killing the
`pw-loopback` process — so the startup-only retry wouldn't otherwise run
again.

`AEC_AECCONVERGED` is a **read-only** status register reporting whether
the echo model has actually converged. **Note for anyone reading old
session logs**: it read `0` for an entire prior debugging session no
matter what was tried (external delay tuning, `AUDIO_MGR_SYS_DELAY`
adjustments, waiting longer) and was written off as "genuinely blocked."
It now reads `1` — likely from extended natural convergence time once
`AUDIO_MGR_FAR_END_DSP_ENABLE` started reliably staying on instead of
flapping. If you're debugging a wake-word-under-music sensitivity
problem, **check this register first** before assuming the AEC is broken
— it may have quietly started working.

### Tooling: `xvf_register.py`

The vendored `xvf_host.py`
(`~/src/reSpeaker_XVF3800_USB_4MIC_ARRAY/python_control/`) has no CLI and
prints every ReadCMD/WriteCMD to stdout unconditionally. `xvf_register.py`
wraps it once, quietly:

```
xvf_register.py get    NAME              print current value(s)
xvf_register.py set    NAME VAL [VAL..]  write value(s)
xvf_register.py ensure NAME VAL          write only if different; prints ok/fixed
```

Must run under `~/.venvs/xvf3800/bin/python` (needs `pyusb`). Used by
`xvf-aec-ref.sh` for the register fix-up above, and by
`ai-speaker-calibrate.py` for read-only health checks.

### Verify

- [x] Music at normal listening volume + speaking → the XVF3800's
      processed capture channel is recognisably clean
- [x] The delay figure survives a reboot (it's a static config value, not
      derived at runtime, so this is automatic)
- [ ] **Not done**: a proper `mic_ref_correlate` delay measurement. The
      shipped delay (`--delay=0.341`, i.e. 341ms) is a **theoretical
      estimate** (`16384/48000 samples`, the Loopback+CamillaDSP buffer
      depth), never empirically confirmed by correlation. This is the
      most-cited remaining lever for improving barge-in (see Step 5) —
      worth doing if you pick this back up. Also worth re-deriving against
      `voice.yml`'s actual buffer size (512), not the older, larger buffer
      this estimate was based on.

---

## Step 2 — Add a low-latency voice DSP profile

The full music profile's buffer depth is too laggy for a snappy
conversation. Add a second CamillaDSP profile,
`~/smart-speaker-software-only/profiles/voice.yml`, alongside `music.yml`:
same crossover and driver protection (**non-negotiable — tweeter
safety**, via the `mono_split` mixer, -6dB gain per side), smaller
`chunksize`/`target_level` (512/1024, vs. music's larger buffer).

### How switching works — and why not the obvious way

The obvious mechanism is the existing `smartspk-profile` CLI. **Don't use
it here**: it needs root and restarts `camilladsp.service`, and there's
no passwordless sudo configured for it — wiring that up just for this
feature would add a new standing privilege escalation for a background
service to hold.

Instead, `DspProfileSwitcher` in `voice_agent.py` hot-swaps CamillaDSP's
config directly over its **websocket control API** (`CamillaClient`, port
1234 — the same API CamillaGUI already uses), which applies a new config
file in place with no restart and no sudo. It switches per-conversation
(to `voice.yml` on session start, back to whatever was active before on
session end) rather than running `voice.yml` permanently — simpler than
juggling two delay calibrations, at the cost of a brief profile-switch
gap at the start of every session.

### Crash safety

If `voice-agent.service` crashes mid-session, `Restart=on-failure` brings
it back with no memory of the switch, which would otherwise strand
CamillaDSP on `voice.yml` indefinitely. `DspProfileSwitcher._previous_path`
is persisted to `$XDG_RUNTIME_DIR/voice-agent-state.json` (runtime dir,
not `~/.config` — a reboot legitimately resets this, since CamillaDSP
restarts from its own default anyway) and restored on daemon startup via
`VoiceAgent.recover()`. The recovery is guarded to only act if CamillaDSP
is *still* on the voice profile — if something else already changed it,
the recorded value is stale and stomping it would be worse than doing
nothing.

### Verify

- [x] Profile switches cleanly, no under/overruns in
      `journalctl -u camilladsp`
- [x] AEC stays clean under `voice.yml`
- [x] Crash → restart → CamillaDSP correctly restored (see Step 6's crash
      test)

---

## Step 3 — Run the wake-word service

`wake_word.py` / `wake-word.service` runs openWakeWord's `hey_jarvis`
pretrained model against 16kHz mono PCM read via a `pw-cat --record`
subprocess targeting the XVF3800's **processed** capture node by name —
post-AEC, post-beamform, never raw mics.

### The channel gotcha

The XVF3800's L/R USB capture channels are **independently configured on
this unit — not a true stereo pair**. Naively downmixing 2ch→1ch
(averaging) measurably hurts detection range, because it mixes two
different signals together. Pick one channel explicitly instead
(`WAKE_WORD_CHANNEL`, currently `0`/left) — and treat "which channel is
better" as a per-unit question you re-check, not a fixed answer, since
it's specific to how this individual board happened to enumerate.

### Tuning

Threshold (`WAKE_WORD_THRESHOLD`, currently `0.35`, down from the code
default of `0.5`) and patience (`WAKE_WORD_PATIENCE`, currently `1`) were
arrived at by measuring the gap between the background noise floor and
real "Hey Jarvis" utterance scores, not guessed. `ai-speaker-calibrate.py`
(see its own README) automates this measurement — run it whenever
hardware moves, the room changes, or detection starts feeling off.

### Why two processes, not one

The plan's original instinct was same-process (wake word and agent
sharing a process, fewer moving parts). **What shipped is two separate
services** — `wake-word.service` and `voice-agent.service` — talking over
a UNIX socket (`VOICE_SESSION_SOCKET`). Deliberate: it lets the
lightweight wake-word detector and the much heavier
Pipecat/Gemini-dependent conversation engine crash and restart
independently. `wake_word.py` also runs a second socket
(`WAKE_TRIGGER_SOCKET`) so the physical GPIO wake button funnels through
the same debounced relay path as voice detections, before both get
forwarded on to the agent.

### Verify

- [x] Reliable trigger while music plays — **status per Step 1's
      `AEC_AECCONVERGED` finding: previously ~10cm range under music, not
      yet re-measured now that the AEC appears to be converging for the
      first time**. Re-run `ai-speaker-calibrate.py all` before trusting
      the old number.
- [ ] Formal false-accept rate (<1/hour target) — never formally measured;
      threshold was tuned by separation ratio instead

---

## Step 4 — Wire up LED feedback

Extend the existing `volume_led.py` / `volume-led.service` (don't build a
separate component) with a second UNIX socket (`ASSISTANT_LED_SOCKET`)
accepting newline-delimited state names:
`idle, listening, thinking, speaking, error`. Assistant states take
rendering priority over the ordinary volume display when active, falling
back to volume display at `idle`.

- `listening` → white, on `BotStoppedSpeakingFrame`. **Updated 2026-08-23**:
  all 12 LEDs breathe together on a slow (~3s) cycle; loud mic input
  overrides the breathing brightness in sync across the whole ring. An
  earlier version instead lit a single rotating "comet" arc that chased the
  XVF3800's direction-of-arrival estimate and locked onto it once speech was
  confidently detected — removed for being hard to follow at a glance and
  for having no per-pixel signal that actually mattered once mic-loudness
  reactivity was added on top of it.
- `speaking` → green (`20D060`), on `BotStartedSpeakingFrame`
- `error` → red (`FF2A1E`), on pipeline error events
- `thinking` → now a rotating green (`00E676`) comet arc — reusing the
  animation removed from `listening` above, since it fits a "working on it"
  cue better than a direction indicator ever did. Still **defined but never
  triggered**: Gemini Live's native speech-to-speech doesn't expose a clean
  "processing" phase as a distinct frame the way a cascaded STT→LLM→TTS
  pipeline would, so there's nothing in `voice_agent.py` to hang it off.
  Leave it wired for now in case a future engine swap (e.g. a cascaded
  pipeline behind `VOICE_ENGINE`) exposes that signal.

### Verify

- [x] States visibly distinct
- [x] No contention with existing volume-LED behavior (assistant state
      simply takes priority)

---

## Step 5 — Build the Pipecat + Gemini Live agent

```bash
sudo apt install -y python3-venv portaudio19-dev
python3 -m venv ~/.venvs/voice-agent && source ~/.venvs/voice-agent/bin/activate
pip install "pipecat-ai[google,silero,local]" openwakeword
```

Keep the engine behind `VOICE_ENGINE`/`VOICE_MODEL` env vars from day
one — the model is a `preview` release and will be retired with modest
notice.

**Update, 2026-08-23**: this was stated as intent but not actually wired
up until this date — `voice_agent.py` constructed `GeminiLiveLLMService`
directly, inline, with no env-var switch. `~/.local/bin/voice_engine.py`
now provides it: `VOICE_ENGINE=gemini` (default, behavior-identical to the
old hardcoded construction) or `VOICE_ENGINE=openai_realtime` (wired up
against pipecat's `OpenAIRealtimeLLMService`, but **code-verified only** —
no live OpenAI Realtime session has been run against it). Adding a further
engine means adding one builder function in `voice_engine.py`, nothing in
`voice_agent.py`. Full detail: `AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md`.

### Use a custom transport, not `LocalAudioTransport`

Pipecat's built-in `LocalAudioTransport` is PyAudio-backed and can only
select a device by numeric index — it can't target a PipeWire node by
name, and this box's default ALSA/PipeWire source/sink don't point at the
right nodes anyway. Instead, `pw_transport.py`'s `PwCatTransport` shells
out to `pw-cat`, targeting nodes by name exactly like the wake-word
capture (Step 3) and the AEC reference routing (Step 1) already do — one
consistent mechanism for "reach a specific named PipeWire node," reused
three times rather than reinvented per component.

### Session lifecycle

Wake word opens the session; ~8s of silence (`VOICE_IDLE_SECONDS=8.0`) or
a spoken closing phrase (`VOICE_CLOSING_PHRASES`, env-configurable) closes
it. Never hold a session open idle.

One addition beyond the original scope: **quick-action session close**. A
one-shot command like "turn it up" or "pause" now closes the session
immediately after acknowledging, instead of sitting open waiting for more
conversation. Mechanism: a module-level flag (`session_signal.py`) that
tool functions (Step 8) set on success via `mark_quick_action()`, which
`SessionMonitor` checks and consumes on `BotStoppedSpeakingFrame`. One flag
is enough because only one session runs at a time.

System prompt stays static (`VOICE_SYSTEM_PROMPT`, one env string) — it
caches, and a dynamically assembled prompt wouldn't.

Service runs as `aldom` with the same `XDG_RUNTIME_DIR`/`PULSE_SERVER`
pattern already proven by the Raspotify/spotifyd drop-in on this host.
The chain runs at 48kHz internally; 16k↔48k↔24k resampling is PipeWire's
job — just budget the latency, don't try to do it yourself.

### The barge-in finding — read this before assuming full duplex will "just work"

The plan's original success criterion for this step was "you can
interrupt it mid-sentence." **On this hardware, that does not hold as
originally hoped, and it's worth understanding why before you spend time
re-chasing it.**

Measured: the XVF3800's AEC converges to about -35dB on steady program
material, but the first ~500ms of *every* bot utterance leaks through at
full scale while the filter re-adapts — and even the converged residual
(~600 peak, measured) comes back **louder** than the user's own voice at
the mic (~400–600 peak) at normal listening volume. In full duplex,
Gemini Live hears its own output, decides it's being interrupted, and
either cuts itself off mid-sentence or transcribes-and-answers itself.

**Shipped fix**: `VOICE_HALF_DUPLEX=1` (default). `AudioGate` in
`pw_transport.py` zeroes the mic while the bot is speaking — trading real
voice barge-in for a conversation that doesn't derail. The physical wake
button still cancels instantly regardless; it's a separate signal path,
not gated by this.

**If you want to re-attempt barge-in**: a test was run with
`VOICE_HALF_DUPLEX=0` + `VOICE_OUTPUT_GAIN=0.5` (halving the bot's own
playback level, on the theory that the AEC residual scales with the bot's
own output level while the user's voice doesn't, so turning the bot down
should push the residual clearly under the user's voice). `VOICE_OUTPUT_GAIN`
is applied in software specifically so there's no PipeWire stream node to
resolve and no window where early frames play at full level before the
gain takes effect. **It was tested and reverted** — not confirmed good
enough to keep as the default. If you pick this back up, the next lever
to try is Step 1's still-unmeasured delay calibration
(`mic_ref_correlate`) — the current 341ms figure is a theoretical
estimate and is the likeliest reason the AEC needs ~500ms to converge and
tops out at ~35dB rather than doing better.

### Verify

- [x] End-to-end conversation works
- [x] "Interrupt mid-sentence" works via the half-duplex/button-cancel
      mechanism, not true overlapping voice barge-in

---

## Step 6 — Harden session robustness ⚠️ do this before adding tools

Do this before Step 8 — session bugs are far harder to diagnose once
function calls are in the mix. Free-tier Gemini Live limits are
session-shaped, not billing-shaped (15-min audio-session cap, ~10-min
single connection, 128k context window) — full detail in
`PHASE-6-7-PLAN.md`.

**Before implementing, investigate the actual installed pipecat source
and live system rather than assuming the limits table applies as-is.**
That's how the real gap was found:

- **The real fix**: context-window compression was simply never enabled —
  `voice_agent.py` never set it, and pipecat defaults it off. This was
  the single biggest gap and a two-line fix:

  ```python
  context_window_compression=ContextWindowCompressionParams(
      enabled=VOICE_CONTEXT_COMPRESSION,       # default on
      trigger_tokens=VOICE_COMPRESSION_TRIGGER_TOKENS,  # None = 80% of window
  ),
  ```

  Without it, the 15-minute audio-session cap and 128k context window are
  hard walls, not something a long conversation slides past.
- **Already working, don't rebuild**: session resumption
  (`SessionResumptionConfig`) was already implemented in the installed
  pipecat version — handles sent unconditionally, stored and replayed on
  reconnect. Just verify the handle arrives in logs; no code needed.

### Bugs found during that investigation, fixed here

- **Unbounded mute gate**: `AudioGate.mute()` had no timeout and was only
  released by `BotStoppedSpeakingFrame` — if the Gemini connection drops
  mid-utterance, that frame might never arrive, leaving the mic
  permanently gated. Fixed with a hard ceiling
  (`VOICE_MAX_MUTE_SECONDS=30`): no legitimate bot turn runs that long, so
  hitting it means a frame was lost, and a deaf assistant is worse than a
  briefly echoey one.
- **Silent non-fatal death**: pipecat only auto-cancels on *fatal* errors,
  but `GeminiLiveLLMService` reports even a permanently dead connection (3
  consecutive reconnect failures) as non-fatal — left alone, the session
  would sit up, silent and deaf, holding the voice DSP profile until the
  idle timeout eventually noticed. Fixed: `ErrorFrame` now drives LED
  `error` and closes the session.
- **Crash-safe DSP restore** — see Step 2.

### Extra safety net

`VOICE_MAX_SESSION_SECONDS=1800`: an absolute ceiling independent of both
the idle timeout and the 15-min API limit — protects against a session
that's still technically passing frames but has stopped being useful, so
it can't hold the mic and voice profile forever.

### Soak-test procedure

```
systemctl --user edit voice-agent.service   # Environment=VOICE_IDLE_SECONDS=3600
```

Speak every minute or two for 25+ minutes, then revert the drop-in. Watch
`systemctl --user status voice-agent.service -l -n 200 --no-pager` for
`session_resumption_handle` reconnect log lines.

### Verify

Code-level, verified 2026-08-21:
- [x] Compression enabled; both scripts compile; service restarts clean
- [x] `AudioGate` deadline unit-tested
- [x] Live session connects with compression enabled
- [x] `kill -9` mid-session → restart → CamillaDSP auto-restored, state
      file cleared
- [x] Stale-state branch correctly discards without touching a profile
      someone else already changed

**Still needs a person talking — not yet run:**
- [ ] ≥25 min continuous session, ≥2 logged reconnects
- [ ] Bot still hears/remembers correctly across each reconnect
- [ ] Reconnect audio gap measured (expected ~1-2s, since there's no
      `GoAway` pre-warning handling)
- [ ] No >30s stretch of `(gated)` in the debug log

---

## Step 7 — Add ducking

Drop the media player's stream volume for the duration of a voice
session, then restore it.

### Correcting a stale assumption

Whatever plan doc you're reading probably says "librespot stream node" —
**that's wrong for this box.** Spotify playback here is `spotifyd`, not
raspotify/librespot (raspotify is an unused leftover apt source). Point
your match pattern at `spotifyd|librespot` — a regex over several
PipeWire node props, most-specific-first, since spotifyd embeds
librespot's PulseAudio backend and which prop actually carries an
identifiable name can vary.

### Mechanism

`~/.local/bin/duck.py`'s `Ducker`, wired into `voice_agent.py`:

- Resolve the stream node fresh via `pw-dump` **every time** — never
  cache the ID, the stream is destroyed and recreated on every
  track-session change.
- Use `wpctl get-volume`/`set-volume` (matching `volume_sync.py`'s
  existing convention, `WPCTL_PATH`, rather than adding a new dependency
  like `pulsectl`).
- Ramp over `VOICE_DUCK_RAMP_MS` (200ms / 8 steps) — a single
  `set-volume` call is an instantaneous gain change and audibly clicks.
- On restore, only restore streams that still exist *and* are still at
  the level this session ducked them to. If Spotify stopped mid-session,
  restore is a silent no-op; that's the common case and must not be
  treated as an error.

### Decision: whole-session, not per-utterance

The obvious instinct is to duck only while the bot is actually speaking
(bot-started/stopped-speaking events). **Don't do that here — duck for
the whole session instead.** Two reasons: per-utterance ducking pumps the
music several times per exchange, and the session's Silero VAD listens to
the same mic — unducked music during the *listening* half is speech-like
enough to manufacture false turn boundaries.

### A correction worth remembering: ducking does not help barge-in

An earlier working note claimed ducking would help voice barge-in. That's
mostly wrong, and worth stating plainly so it doesn't get re-assumed:
ducking lowers spotifyd's stream, but the echo that breaks full duplex
(Step 5) is the **assistant's own voice**, which ducking doesn't touch at
all. Ducking only helps the narrower case where music itself is adding
broadband echo on top. The actual barge-in lever is `VOICE_OUTPUT_GAIN`
(Step 5).

### Guardrails (non-negotiable)

- Never duck via CamillaDSP volume — that's global tweeter-safety
  headroom, not a media control.
- Never duck the default sink — that's the user's physical volume knob,
  and `volume_sync.py`'s exclusive domain.
- Stream-level only. `Ducker._find_streams()` explicitly excludes
  `node.passive` nodes — this is what keeps it from ever matching the
  XVF3800 AEC reference loopback itself, which is technically a
  `Stream/Output/Audio` node too and would otherwise match a loosened
  name pattern. (Tested deliberately with a loose `pw-cat|loopback`
  pattern to confirm the guard holds.)

### Verify

Code-level, verified 2026-08-22 (using a surrogate `pw-cat` stream since
spotifyd was idle at test time):
- [x] Duck/restore cycle correct; default sink untouched; AEC reference
      unduckable; crash-restart restores both DSP and duck; stale-guard
      correct; silent no-op when nothing playing; no turn-up on restore

- [x] Match pattern actually catches spotifyd's real stream — confirmed
      2026-08-22 with real music: `duck.py` printed
      `node 124: application.process.binary=spotifyd volume=1.0` and
      ignored the AEC reference. Note it matches on
      `application.process.binary`, **not** `application.name`, which
      spotifyd leaves empty.

**Still needs real spotifyd + real music — not yet run:**
- [ ] No audible click on either ramp direction
- [ ] Wake word still triggers over ducked music
- [ ] `VOICE_DUCK_LEVEL` (currently `0.20`) tuned for comfort

---

## Step 8 — Add function-calling tools

Establishes the seam future tools plug into.

### 8.1 — Spotify transport control (done)

`spotify_tools.py`: `spotify_play`, `spotify_pause`, `spotify_next`,
`spotify_previous`. **Route through `playerctl`/MPRIS, not the Spotify
Web API** — spotifyd registers as an MPRIS-controllable player directly
(the same mechanism the physical transport button already uses), so
these four commands need no OAuth, no token storage, no Web API round
trip. Reserve the Web API specifically for 8.3 below, where MPRIS
genuinely can't help.

### 8.2 — Volume control (done)

`volume_tools.py`: `set_volume(level_percent)`,
`adjust_volume(direction, step_percent)`, both via `wpctl` against
`@DEFAULT_AUDIO_SINK@` — the same authority `volume-buttons.py` and
`volume_sync.py` already use, so voice-driven changes stay consistent
with the physical button and mirror into the Spotify app.

Both 8.1 and 8.2's tools call `session_signal.mark_quick_action()` on
success — this is what actually triggers the quick-action session close
described in Step 5.

### 8.3 — Spotify catalog search (done, 2026-08-22)

`spotify_tools.spotify_play_track(query, kind="track")` on top of a new
`spotify_web.py` (Web API client: token manager, search, device
resolution, play). `kind` accepts track/album/artist/playlist.

**Why the Web API and not MPRIS `OpenUri`** (spotifyd 0.4.2 does implement
it): spotifyd only claims the `org.mpris.MediaPlayer2.spotifyd*` bus name
while a Connect *client session* is live — `dbus_mpris.rs` calls
`request_name` on `PlayerEvent::SessionConnected` and `release_name` on
`SessionDisconnected`. With nothing playing there is no local player to
hand a URI to, so search wasn't the only thing MPRIS couldn't do. What
does survive idle is the Connect *device*: spotifyd restores its cached
login at startup (`setup.rs` seeds `CredentialsProvider::Discovery` from
`~/.cache/spotifyd/zeroconf/credentials.json`, so it never waits on
zeroconf), which is what `PUT /me/player/play?device_id=…` targets.

Verified live: playback started this way makes the MPRIS name appear, so
8.1's `playerctl` transport tools come alive once music is running.

Auth: Authorization Code flow, one-time via `spotify_web.py auth` (local
127.0.0.1:8888 listener, or paste the callback URL back — a headless Pi
may have neither browser nor forwarded port). Client id/secret/refresh
token in `~/.config/ai-speaker/spotify.json` (0600) rather than an
EnvironmentFile: the refresh token can rotate at runtime and systemd only
reads env files at unit start. The developer app must belong to the same
Spotify account spotifyd streams as, or search works while
`/me/player/devices` comes back without this speaker.

**Update, 2026-08-23**: this is now done — `~/.local/bin/local_intent.py`
+ `local_actions.py`, wired into `wake_word.py`. Offline, grammar-constrained
Vosk matching against exactly this set of trivially-matchable commands
("next track", "volume up", etc.), so a match skips opening a Gemini
session entirely. **Shipped off by default** (`LOCAL_INTENT_ENABLED=0`) —
verified against live ambient mic audio (correctly ignores background
noise) but never verified with an actual spoken command. Full detail:
`AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md`.

### 8.4 — Web API fallback for transport (not started)

Because MPRIS is absent until something is playing, `spotify_play` /
`pause` / `next` / `previous` still answer "no active media player" from
cold. Falling back to `PUT/POST /me/player/{play,pause,next,previous}
?device_id=…` when `playerctl` fails is a small change now that a user
token exists.

### `hass_call_service` (not started)

Home Assistant REST API with a long-lived access token. No work done yet.

---

## Guardrails checklist (keep these true forever)

- CamillaDSP stays the sole owner of `hw:sndrpihifiberry,0`. No
  exceptions — `DspProfileSwitcher` only ever changes *which config file*
  CamillaDSP loads, never routes anything around it.
- Crossover + driver protection present in **every** DSP profile,
  including `voice.yml`.
- Never hardcode PipeWire node IDs — resolve by name every time
  (`wpctl status` for the AEC reference, `pw-dump` fresh on every call
  for ducking).
- Keep the system prompt static so it caches.
- systemd drop-ins over editing packaged units. (One barge-in experiment
  used a drop-in and was cleanly deleted afterward rather than left as
  unit-file cruft — check `~/.config/systemd/user/*.service.d/` is empty
  if you're auditing for leftovers.)
- EEA free-tier data-residency terms are tied to device location — note
  it in config if the build ever moves. **Not re-verified recently.**

---

## What to pick up next, in priority order

1. **Re-measure music-under-wake-word sensitivity** now that
   `AEC_AECCONVERGED` reads 1 for the first time (`ai-speaker-calibrate.py
   all`) — directly relevant to Step 3's still-open false-accept/range
   question.
2. **Live-test the quick-action session close** (Step 5/8) — deployed,
   not yet confirmed by an actual spoken command.
3. **Step 6's soak test and Step 7's real-music checklist items** — both
   need a person talking/playing music for an extended period, not
   further code changes.
4. **Step 1's actual delay measurement** (`mic_ref_correlate`) — the
   341ms figure has never been empirically confirmed and is the
   most-cited remaining lever for improving barge-in.
5. **Step 8.4** (Web API fallback so transport commands work from cold)
   and **`hass_call_service`** — net-new feature work, not blocked on
   anything above.
6. **Live-test local intent matching** (Step 8.3,
   `AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md` § 4) — code-verified and
   verified against ambient noise, but no real spoken command has been
   tried yet. Set `LOCAL_INTENT_ENABLED=1` and say "hey jarvis, pause".
7. **Live-test `VOICE_ENGINE=openai_realtime`**
   (`AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md` § 3) — needs an OpenAI
   Realtime API key and a real conversation; currently code-verified only.
8. **A real `sudo reboot`** — every persistence check in this project
   (including the 2026-08-23 flexibility work) has so far only gone as far
   as a full service-restart cycle, never an actual power cycle.
