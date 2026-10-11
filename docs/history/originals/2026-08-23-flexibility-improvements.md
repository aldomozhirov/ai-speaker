# AI Speaker: Flexibility & Reliability Improvements (2026-08-23)

Four targeted improvements made after an architecture review of the full
stack (see companion docs below for the stack itself): a shared fix for a
recurring PipeWire bug class, a one-shot health check, a genuinely swappable
LLM engine, and optional offline command matching. All four are live on the
`ai-speaker` Pi. This doc records what changed, why, and what's still
unverified — read it before touching any of the five new files under
`~/.local/bin/`.

Companion docs:
- `AI-SPEAKER-SETUP-NOTES.md` — the AEC/PipeWire routing deep dive these
  changes build on.
- `WAKE_WORD_GEMINI_INTEGRATION.md` — the original build guide; Steps 5 and
  8.3 are updated to point here for the engine-swap and local-intent pieces.

---

## 1. `pw_node.py` — one PipeWire node-readiness check, not four

### The problem this consolidates

`pw-cat --target <name>` does **not** error when the target node doesn't
exist yet — it silently falls back to whatever the default sink/source is.
This exact failure mode had already been hit and separately worked around
three times in this codebase (`xvf-aec-ref.sh`'s reconcile loop,
`pw_transport.py`'s wait-for-node before opening the transport,
`welcome-sound.sh`'s poll loop) before this session, and a fourth call site —
`wake_word.py`'s capture stream — had **no guard at all**.

### What shipped

`~/.local/bin/pw_node.py`: a single `wait_for_node(name, timeout=15.0,
interval=0.5)` (async, with a sync wrapper) that polls `pw-dump` for the
literal `"node.name": "<name>"` string, plus a CLI entry point
(`pw_node.py <name> --timeout N`, exits 0/1) for bash callers.

Wired in:
- `pw_transport.py` — replaced its local `_wait_for_node` with an import
  from `pw_node`.
- `welcome-sound.sh` — replaced its inline `pw-dump | grep` poll loop with a
  call to the CLI form.
- `wake_word.py` — **new**: added a `wait_for_node(XVF_SOURCE)` check before
  `open_capture()` in the main retry loop. Previously the wake-word capture
  had zero protection against silently listening to the wrong (default)
  node after a boot-time race — the same failure class Step 1 of
  `WAKE_WORD_GEMINI_INTEGRATION.md` spent an entire investigation fixing for
  the AEC reference feed, just never carried over to this call site.

**Deliberately not touched**: `xvf-aec-ref.sh`'s `reconcile()` loop. That one
isn't a "wait then proceed" check — it's a standing supervisor that
disconnects-and-reconnects on a schedule for the life of the service (see
`AI-SPEAKER-SETUP-NOTES.md` § "supervise the link state, don't predict
readiness"). Folding it into `pw_node.py`'s one-shot wait would have been a
regression, not a consolidation.

### Verify

- [x] All three call sites restart clean, `systemctl --user --failed` empty
- [x] `wake_word.py` correctly waits for the XVF3800 capture node on a full
      dependency-order service restart (see § 5 below)
- [ ] Not verified against an actual boot-time race (only against
      already-running-then-restarted services) — the scenario this exists
      for is specifically the boot race, which needs a real reboot to
      exercise (see § 5)

---

## 2. `ai-speaker-status.sh` — one-shot health check

### Why

Diagnosing the wake-word-under-music investigation (see
`WAKE_WORD_SENSITIVITY_INVESTIGATION.md`) meant manually checking eight
`systemctl --user` unit states, the AEC registers via `xvf_register.py`, and
PipeWire routing by hand, every time. No single command answered "is
everything healthy right now."

### What shipped

`~/.local/bin/ai-speaker-status.sh` (executable), checks in order:

- All 8 systemd `--user` units — `is-active` for long-running services,
  `Result=success` for the oneshot `welcome-sound.service` (an
  `is-active`-only check would have reported it as failed, since a
  successful oneshot ends up `inactive` — caught live while writing this
  script, see fix below)
- `systemctl --user --failed`
- `loginctl show-user aldom -p Linger` (`Linger=yes`)
- `AUDIO_MGR_FAR_END_DSP_ENABLE` and `AEC_AECCONVERGED` via
  `xvf_register.py get`
- PipeWire node presence for the XVF3800 capture and AEC reference nodes,
  via `pw_node.py`
- Gemini API reachability via `curl`

Prints `OK`/`WARN`/`FAIL` per check, exits non-zero only on a `FAIL`.

### Fix made while building it

Initial version used uniform `is-active` for all 8 units, which
false-failed on `welcome-sound.service` (correctly `inactive` after a
successful one-time run at boot). Fixed with a `ONESHOT_UNITS` array checked
via `systemctl --user show <unit> -p Result --value` instead. Verified live
— script now reports the full stack `OK`.

### Verify

- [x] Live-run against the full running stack: all checks report `OK`
- [x] `welcome-sound.service` false-positive fixed and reconfirmed
- [x] Exit code is 0 on a healthy stack (checked via `$?`)

---

## 3. `voice_engine.py` — the `VOICE_ENGINE` switch, actually implemented

### The gap

`WAKE_WORD_GEMINI_INTEGRATION.md` Step 5 states the intent from day one:
*"Keep the engine behind `VOICE_ENGINE`/`VOICE_MODEL` env vars... the model
is a preview release and will be retired with modest notice."* In practice,
`voice_agent.py` constructed `GeminiLiveLLMService` directly and inline,
with Gemini-specific settings (`context_window_compression`, `voice="Puck"`,
`inference_on_context_initialization`) hardcoded into session setup. The env
var never existed. This was flagged during an architecture review as the
single biggest flexibility gap in the stack.

### What shipped

`~/.local/bin/voice_engine.py`: one `EngineConfig` in, one `EngineHandle`
out (the constructed Pipecat `LLMService`, plus `audio_out_sample_rate` and
`prime_with_run_frame` — the two pieces of engine-specific behavior the rest
of `voice_agent.py` needs). `ENGINE_DEFAULTS`/`ENGINE_BUILDERS` dicts map
`VOICE_ENGINE` values to builder functions. Adding a new engine means adding
one function plus one dict entry — nothing in `voice_agent.py` itself needs
to change.

Two engines shipped:
- `gemini` (default) — same `GeminiLiveLLMService` construction as before,
  byte-for-byte: same `Settings`, same
  `inference_on_context_initialization=False`, same 24000Hz output.
  Switching this factory in does **not** change existing behavior.
- `openai_realtime` — pipecat ships `OpenAIRealtimeLLMService`; wired up
  using the current (non-deprecated) `settings=Settings(...)` constructor
  API. **Code-verified only** — no OpenAI Realtime account was available to
  test against a live session. The tool-calling layer
  (`spotify_tools.py`/`volume_tools.py`) is already provider-agnostic (plain
  async functions bound into Pipecat's `LLMContext`), so no tool-side
  changes were needed for this second engine — but the engine itself has
  never actually run a conversation.

`voice_agent.py` changes: `GOOGLE_API_KEY`/hardcoded `VOICE_MODEL`/
`VOICE_NAME` replaced with `VOICE_ENGINE` (default `"gemini"`),
per-engine-aware `VOICE_MODEL`/`VOICE_NAME` defaults, and
`VOICE_API_KEY = voice_engine.resolve_api_key(VOICE_ENGINE)` (a generic env
var that wins if set; falls back to the engine's conventional var —
`GOOGLE_API_KEY`, `OPENAI_API_KEY` — so an existing deployment's env block
doesn't need to change). The inline `GeminiLiveLLMService(...)` construction
in `_run_session()` was replaced with `voice_engine.build_engine(...)`,
moved earlier (before transport construction) so `engine.audio_out_sample_rate`
can drive both `PwCatTransportParams` and `PipelineParams` instead of the
previous hardcoded `24000` in both places. The `LLMRunFrame()` startup-nudge
priming is now conditional on `engine.prime_with_run_frame`.

### Verify

- [x] Default (`gemini`) path: service restarts clean, live conversation
      still works, no behavior change from before the refactor
- [x] `openai_realtime` path: constructs without error, no deprecation
      warnings (checked with `-W default`), correct settings shape
- [ ] **Not done**: an actual live OpenAI Realtime session. Needs an API key
      and a real conversation before this path can be trusted. Flag as
      unverified if it comes up.

---

## 4. `local_intent.py` / `local_actions.py` — optional offline command matching

### The problem

Every wake-word trigger opens a full Gemini Live cloud session, even for a
one-word command like "pause" or "volume up" — a real network round trip and
free-tier session quota spent on something that doesn't need an LLM at all.
`WAKE_WORD_GEMINI_INTEGRATION.md` Step 8.3 already flagged this as
not-yet-done: *"routing trivially-matchable commands... to local intent
matching so they never open a cloud session."*

### What shipped

- `~/.local/bin/local_intent.py`: a small offline Vosk model
  (`vosk-model-small-en-us-0.15`, downloaded to
  `~/.local/share/ai-speaker/`), grammar-constrained (via
  `KaldiRecognizer`'s `SetGrammar`, not open-vocabulary recognition) to a
  fixed ~15-phrase map covering exactly the existing
  `spotify_tools.py`/`volume_tools.py` commands. Grammar-constrained on
  purpose: far more accurate than open recognition for a tiny fixed
  command set, and cheap enough to run inline in `wake_word.py`'s main loop
  on a Pi 5.
- `~/.local/bin/local_actions.py`: reimplements the same
  `playerctl`/`wpctl` subprocess calls as `spotify_tools.py`/`volume_tools.py`,
  independently. **Deliberately does not import those modules or pipecat** —
  the `wake-word` venv intentionally excludes `pipecat` (see
  `AI-SPEAKER-SETUP-NOTES.md`'s venv-isolation rationale), and this session
  confirmed via `pip list` that it stays that way. Kept parallel by
  convention (same binaries, same env var names), not by shared code.
- `wake_word.py`: on a wake-word detection, buffers the next
  `LOCAL_INTENT_WINDOW_SECONDS` (2.2s, cut short after
  `LOCAL_INTENT_SILENCE_CUTOFF_SECONDS` of trailing near-silence) of audio
  and runs it through `local_intent.match()` before falling through to the
  normal `trigger.fire()` cloud-session path. A match runs the action
  locally via `local_actions.run_action()` and skips opening a Gemini
  session entirely; no match (or the feature being off) behaves exactly as
  before.

### Shipped OFF by default (`LOCAL_INTENT_ENABLED=0`)

This matches the project's own established convention (see
`WAKE_WORD_GEMINI_INTEGRATION.md`'s quick-action session close and the
barge-in reverts) of shipping a speculative session-shaping change disabled
until confirmed by an actual spoken test, not just code review. Two real
trade-offs a live test needs to confirm before flipping this on:

1. **Latency**: up to the full matching window is added before either
   handling locally or falling through to Gemini (shorter in practice, due
   to the silence-cutoff).
2. **Audio loss on fall-through**: if the model doesn't match, whatever was
   buffered during the matching window is **not** replayed into the Gemini
   session that opens afterward — that audio is lost, same as any other
   silent gap before the transport starts capturing. Only worth enabling
   once real commands are confirmed to match reliably enough that this case
   is rare.

### Verify

- [x] Loads correctly, end-to-end tested against live ambient mic audio:
      correctly returns "no match" on background noise (no false positives
      on room tone)
- [ ] **Never verified with an actual spoken command** — no way for an
      automated session to physically speak into the mic. Before trusting
      this, set `LOCAL_INTENT_ENABLED=1`, say a real command ("hey jarvis,
      pause"), and check `journalctl --user -u wake-word.service` for a
      `local_action` log line.

---

## 5. Persistence / reboot-safety verification for all four changes

Performed the project's own established reboot-safety check (see
`AI-SPEAKER-SETUP-NOTES.md` § "Persistence / reboot-safety verification"):
stopped all 8 services and restarted in dependency order
(`xvf-aec-ref` → `volume-led` → `volume-sync` → `volume-buttons` →
`welcome-sound` → `wake-word` → `voice-agent` → `spotifyd`).

Result:
- Zero failed units, `systemctl --user --failed` empty
- Zero crash-restarts (`NRestarts` unchanged across the cycle)
- `AUDIO_MGR_FAR_END_DSP_ENABLE`/`AEC_AECCONVERGED` read back correctly
  after `xvf-aec-ref.service`'s startup retry reapplied the register
- `VOICE_ENGINE`, `LOCAL_INTENT_ENABLED`, and the rest of the env block read
  back correctly from unit files (no stray drop-ins)
- `loginctl show-user aldom -p Linger` → `Linger=yes`
- `wake_word.py`'s new `pw_node.py` guard correctly resolved the XVF3800
  capture node rather than falling back to default

**What this does not prove**: this is a service-restart cycle, not an
actual power cycle, so it doesn't exercise real USB re-enumeration timing.
`sudo -n true` failing confirms this session has no passwordless sudo, so an
actual `sudo reboot` could not be performed non-interactively. This is the
same caveat every prior persistence check in this project has carried (see
`AI-SPEAKER-SETUP-NOTES.md`) — a real reboot, done once by hand at a
convenient time, remains the final confirmation step for all four changes
above, same as it does for the rest of the stack.

---

## What to pick up next

1. Say a real command with `LOCAL_INTENT_ENABLED=1` and confirm a
   `local_action` log line — the one live-voice test this session couldn't
   perform itself.
2. Get an OpenAI Realtime API key and run one real conversation through
   `VOICE_ENGINE=openai_realtime` — currently code-verified only.
3. A real `sudo reboot` power cycle, as final confirmation for all four
   changes (and everything else in `AI-SPEAKER-SETUP-NOTES.md` that carries
   the same caveat).
4. If `local_intent.py` proves reliable, it's a natural fit for the
   still-open Step 8.3 note in `WAKE_WORD_GEMINI_INTEGRATION.md` about
   routing trivially-matchable commands away from the cloud.
