# Timer & Alarm: offline tools (2026-08-23)

Two new offline capabilities on the AI speaker: "hey jarvis, set a timer for
10 minutes" and "hey jarvis, set an alarm for 7:15" — parsed, scheduled,
played, and stopped entirely without a network/Gemini dependency, on top of
the existing offline `local_intent.py` command path (see
`AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md` §4, `WAKE_WORD_GEMINI_INTEGRATION.md`).
This doc records what was built, how it fits into the existing stack, and
what's still unverified. **Enabled and verified end-to-end via the socket API
on 2026-08-30; the by-voice path is still untested** — see "What to pick up
next".

Companion docs:
- `AI-SPEAKER-SETUP-NOTES.md` — the stack this builds on (services, sockets,
  PipeWire routing, sound_fx conventions).
- `AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md` — `local_intent.py`/
  `local_actions.py`'s original design (this doc extends both).

---

## 1. New service: `timer_alarm.py` / `timer-alarm.service`

One new systemd `--user` service owns both timer and alarm state — they
share persistence, looped playback, LED signalling, and the stop-listener,
so one service made more sense than two.

- `~/.local/bin/timer_alarm.py` (asyncio), `~/.config/systemd/user/timer-alarm.service`.
- Runs under the existing `~/.venvs/wake-word` venv (already has `vosk`+`numpy` for `local_intent.py` — no new venv).
- `After=pipewire.service pipewire-pulse.service volume-led.service`, same shape as `wake-word.service`.
- **v1 scope: one active timer + one active alarm at a time.** Setting a new one replaces whichever is active. Alarm is one-shot, not recurring. Both are a small (not architectural) change to extend to multiples later.
- State persisted to `~/.local/share/ai-speaker/timer-alarm-state.json` as **absolute** `fire_at` epoch timestamps (not remaining seconds) — same "survive a restart without resetting" instinct as `xvf-aec-ref.sh`'s register/link reconciliation. Loaded on startup; an overdue `fire_at` fires immediately on the next scheduler tick.

### Socket API

`TIMER_ALARM_SOCKET` (`$XDG_RUNTIME_DIR/timer-alarm.sock` by default), newline-delimited JSON in, one JSON line back:

```
{"cmd": "set_timer", "seconds": 600}
{"cmd": "cancel_timer"}
{"cmd": "set_alarm", "hour": 13, "minute": 30}
{"cmd": "cancel_alarm"}
{"cmd": "status"}     -> {"ringing": bool, "timer": {...}|null, "alarm": {...}|null}
{"cmd": "stop_ring"}  -> stops playback+listener if ringing, else a no-op
```

`~/.local/bin/timer_alarm_client.py` wraps this: stdlib-only (socket+json), no `vosk`/`numpy`/`pipecat` — importable from any venv in the stack without dragging in weight, same rationale `local_actions.py` already established for not importing `spotify_tools.py`/`volume_tools.py` directly.

### Firing sequence

1. Scheduler tick (`Scheduler.run()`, 1s) notices `fire_at` has passed → clears that schedule entry → `Ringer.start(kind)`.
2. Push `"ringing"` to `ASSISTANT_LED_SOCKET` (new LED state, §3).
3. `sound_fx.play_loop(kind)` — `ffmpeg -stream_loop -1` piped into `pw-cat`, same aloop-sink target every other chime uses (`SOUND_FX_SINK`), never touching `hw:sndrpihifiberry` directly. Returns `None` (logs a warning, rings silently) if `timer.mp3`/`alarm.mp3` aren't present under `SOUND_FX_DIR` — **supplied 2026-08-30** (`timer.mp3` 10.6s, `alarm.mp3` 24.0s, both 48kHz stereo mp3), so rings now have real audio.
4. `pw_node.wait_for_node(XVF_SOURCE)` then a dedicated `pw-cat --record` capture on a background thread, run through its own tiny Vosk grammar (`["stop", "stop it", "stop the timer", "stop the alarm", "cancel", "cancel it", "turn it off", "[unk]"]`) — **independent of `local_intent.py`'s grammar**, active only while something is ringing, open-mic (no wake word). This is deliberate: it means "stop" doesn't change meaning the rest of the time (still "pause Spotify" via `local_intent.py`'s own grammar when nothing is ringing).
5. `TIMER_ALARM_MAX_RING_SECONDS` (default 600s) safety cutoff auto-stops the ring even if nothing hears "stop" and the button isn't pressed.
6. On stop (voice match, button, or safety cutoff): kill the loop-playback subprocess pair, join the listener thread, push `"idle"` to the LED socket.

### Button-press-during-ringing

No change to `volume-buttons.py` — it still just sends `"button\n"` to `WAKE_TRIGGER_SOCKET` as before. The interception is in `wake_word.py`'s `WakeTrigger.fire()` (`~/.local/bin/wake_word.py`): at the top, before anything else, it calls `timer_alarm_client.is_ringing()`; if true, it calls `stop_ring()` and returns *without* relaying to `voice_agent.py` at all — for both a button press and a spoken wake word. This mirrors the class's existing documented convention that an interrupt during an active state means "cancel/stop", not "start something new".

---

## 2. Offline command parsing — the novel part

Extends `local_intent.py`'s existing grammar-constrained Vosk matching (see `AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md` §4 for the original design/rationale).

- **Grammar**: added a flat vocabulary of number words (ones/teens/tens through ninety), duration units (`second(s)`/`minute(s)`/`hour(s)`), and control words (`set`/`a`/`an`/`timer`/`alarm`/`for`/`at`/`and`/`half`/`o'clock`/`am`/`pm`/…) to the same `SetGrammar` list already used for the short Spotify/volume phrases. Vosk's grammar mode recognizes free *sequences* composed from a word list, not just whole fixed phrases (the existing coexistence of multi-word entries like `"next track"` and single-word entries like `"next"` already relies on this) — so this avoids enumerating every "N minutes" / "H:MM" phrase combinatorially.
- **Parsing**: `local_intent.match()` tries the existing dict lookup first (unchanged, still the fast path for the original short commands); on a miss, it now falls through to `_parse_duration(tokens)` / `_parse_clock_time(tokens)` — small deterministic word-sequence parsers, pure Python, no new dependency — gated on the recognized text containing the literal word `"timer"` or `"alarm"` respectively.
- **Window**: `LOCAL_INTENT_WINDOW_SECONDS` default raised 2.2s → 5.0s to fit longer phrases ("set a timer for twenty five minutes"). This is a cap, not a fixed cost — `wake_word.py`'s `_buffer_command_audio` already stops early on trailing silence, so short commands aren't slowed down.
- **New local actions**: `local_actions.py` gained `set_timer`/`cancel_timer`/`set_alarm`/`cancel_alarm`, each a thin `asyncio.to_thread(timer_alarm_client.*)` call (offloaded to a thread since the client's socket calls are blocking, so they don't stall `wake_word.py`'s event loop).

**This is the single most novel, least-proven piece of the whole feature.** Everything else reuses patterns already live-verified elsewhere in this stack; Vosk's grammar-mode accuracy on compositional number phrases has never been tested here. If live accuracy turns out poor, the documented fallback is routing *just* timer/alarm command parsing through the existing cloud Gemini tool-calling path (`spotify_tools.py`/`volume_tools.py` pattern) instead — playback-looping and stop-detection stay offline exactly as built either way.

**Prerequisite that's easy to forget**: none of this is reachable by voice unless `LOCAL_INTENT_ENABLED=1` on `wake-word.service` — still `0` (default) as of this writing, and itself never live-tested (see memory / `AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md` §4's own open item).

---

## 3. LED: `"ringing"` state + timer countdown arc

`volume_led.py`'s `AssistantStateWatcher`/`AssistantAnimator` protocol (newline-delimited state names, most-recent-wins) gained two additive pieces, no behavior change to anything that existed before:

1. **`"ringing"`** added to `ASSISTANT_STATES`/`ASSISTANT_COLOURS` (default amber `FF8800`) — a fast blink (`ASSISTANT_LED_RINGING_BLINK_HZ`, default 3Hz) rendered the same way `"error"`'s blink already is, just faster and a different colour so the two are never confused. Pushed by `timer_alarm.py` on fire, `"idle"` on stop.
2. **Background countdown layer**: `AssistantStateWatcher` tracks a second field, `_timer_fraction`, set via a new non-bare-state protocol line `"timer <fraction>"` (parsed distinctly, same precedent as the pre-existing `"flash"` one-shot special case) and cleared via `"timer clear"`. `timer_alarm.py`'s scheduler pushes `"timer <fraction>"` once a second while a timer is counting down (not ringing), and `"timer clear"` the instant it's gone (fired or cancelled).
3. New `AssistantStateWatcher.effective_state()`: returns the real foreground state whenever it isn't idle (listening/speaking/thinking/error/ringing always win — this is what "only when the LED is idle" means), else `"timer"` if a countdown is registered, else `"idle"`. `run_daemon`'s one call site switched from `.state()` to this.
4. `AssistantAnimator.step()` gained an early-return `"timer"` branch — deliberately *before* the normal state-transition bookkeeping, since `"timer"` is a pure overlay, not a real member of the foreground state machine, and must not corrupt `self._state`'s transition tracking. Renders a shrinking 12-pixel arc using the identical fractional-final-pixel technique `XvfLedRing.render()`'s volume arc already uses, in its own colour (`TIMER_LED_COLOUR`, default gold `FFC800`).

**Scope note, as specified**: only the **timer** gets the idle-LED countdown. The alarm has no idle-LED indication — just `"ringing"` when it actually fires. Easy to extend later if wanted.

---

## Files touched

New:
- `~/.local/bin/timer_alarm.py`
- `~/.local/bin/timer_alarm_client.py`
- `~/.config/systemd/user/timer-alarm.service`
- `~/.local/share/ai-speaker/timer-alarm-state.json` (created at runtime; confirmed 2026-08-30 to survive a service restart with an absolute `fire_at`)

New (2026-08-30, cloud path):
- `~/.local/bin/timer_tools.py` — the five LLM tools described above

Edited:
- `~/.local/bin/voice_agent.py` — timer/alarm tools registered, system prompt mentions the capability
- `~/.local/bin/local_intent.py` — number/duration/clock-time grammar + parsers, window default
- `~/.local/bin/local_actions.py` — 4 new `ACTIONS` entries
- `~/.local/bin/wake_word.py` — `WakeTrigger.fire()` ring-check-and-stop short circuit, `timer_alarm_client` import
- `~/.local/bin/sound_fx.py` — `play_loop()`/`LoopHandle`, `timer`/`alarm` `SOUNDS` entries
- `~/.local/bin/volume_led.py` — `"ringing"` state, `_timer_fraction` background layer, `effective_state()`, animator branches
- `~/.config/systemd/user/wake-word.service` — added `timer-alarm.service` to `After=`

All seven touched/new Python files pass `python3 -m py_compile`; `systemctl --user daemon-reload` picked up the new/edited unit files with no errors.

**Verified 2026-08-30** (see "What was verified" below) — everything except recognition of actual spoken audio.

---

## What was verified (2026-08-30)

Activated: `timer-alarm.service` is `enabled` + `active`, `LOCAL_INTENT_ENABLED=1`
is set on `wake-word.service`, `systemctl --user --failed` is empty. The
08-23 code edits were already live — every service had been restarted by the
08-30 reboot, well after the edits — so no stale in-memory code remained.

Two config gaps were found and fixed while enabling:

- **`wake-word.service` was missing `LOCAL_INTENT_ENABLED=1`** (item 3 below,
  as expected).
- **`timer-alarm.service` was missing `WAKE_WORD_CHANNEL=1`.** `timer_alarm.py`
  defaults `CHANNEL_INDEX` to `0`, but the XVF3800's two capture channels are
  different signals rather than a stereo pair, and `wake-word.service`
  empirically overrides to `1`. Left alone, the stop-listener would have
  listened on the weaker channel, making "stop" less reliable than the wake
  word that shares the same mic. Now pinned in the unit.

One functional gap was found and fixed:

- **`cancel_timer`/`cancel_alarm` were unreachable by voice.** Both existed in
  `local_actions.ACTIONS`, but no `_GRAMMAR` phrase mapped to them and
  "cancel" wasn't in `_TIMER_ALARM_VOCAB` — so a user could set a timer but
  never cancel a scheduled one. Added six fixed phrases
  ("cancel/stop the timer/alarm"), put "cancel" in the vocab, and added a
  token-level `cancel` + `timer`/`alarm` check in `match()` ahead of the
  number parsers for free sequences that miss the fixed phrases. There is now
  a test asserting `ACTIONS` and the set of actions `local_intent` can emit
  are exactly equal in both directions.

Exercised live through `timer_alarm_client` (no mic involved):

- 8s timer: scheduled → 1Hz countdown → fired → `ringing` → `stop_ring` → cleared.
- Alarm at the next minute boundary: `_next_occurrence` scheduled it correctly and it fired on time.
- Both rings logged `starting ring` + `Stop-listener: starting capture` with **no** "Sound file missing" or Vosk-model warning, so the mp3s and the stop-listener grammar both loaded.
- 300s timer survived a `systemctl --user restart timer-alarm.service` with its absolute `fire_at` intact (296.9s remaining after).
- LED daemon accepted all four new protocol lines (`ringing`, `idle`, `timer 0.5`, `timer clear`) with no errors logged.
- Duration/clock parsers return correct values for every phrasing the doc flagged as risky, including "twenty five minutes" (1500s), "an hour and thirty minutes" (5400s), "seven fifteen pm" (19:15), and "seven o'clock" (7:00).

Journal note: each ring stop logs a few ffmpeg `Broken pipe` / `Error muxing a
packet` lines. That is ffmpeg reacting to `pw-cat` being killed by
`LoopHandle.stop()` — expected and cosmetic, not a failure.

## What to pick up next

1. ~~**Start the new service and pick up the edited ones**~~ — **done 2026-08-30**, see above.
2. ~~**Supply `timer.mp3`/`alarm.mp3`** under `~/.local/share/ai-speaker/sounds/`~~ — **done 2026-08-30**. `timer.mp3` 10.6s, `alarm.mp3` 24.0s, both 48kHz stereo (= `SOUND_FX_RATE`, so no resampling in the ring path).
3. ~~**Set `LOCAL_INTENT_ENABLED=1`** on `wake-word.service`~~ — **done 2026-08-30**.
4. ~~**Live-test by voice**~~ — **first attempt 2026-08-30 failed; two bugs found and fixed, needs a re-test.**

   The assistant answered "I can't set timers" — i.e. `local_intent` didn't match and the command
   fell through to the cloud Gemini session, which has no timer tool. Recognition itself was *not*
   the problem: Vosk heard the words. Two separate bugs downstream of it were:

   - **The utterance was split across two recognition windows.** The journal shows
     `recognized 'set the timer four'` at 00:40:16 and `recognized 'minute'` at 00:40:17 — two
     `match()` calls. `_buffer_command_audio`'s 0.6s `LOCAL_INTENT_SILENCE_CUTOFF_SECONDS` ends the
     buffer on the natural pause after "for", so the quantity lands in a window with no `timer`
     token in it and neither half can parse. **Fix**: `local_intent.looks_truncated()` +
     `last_text()`; when a window names a timer/alarm but yields no duration or clock time,
     `_handle_voice_detection` buffers a second pass and re-matches the joined audio instead of
     falling through. Bounded by `LOCAL_INTENT_CONTINUE_SECONDS` (3.0s) and
     `LOCAL_INTENT_CONTINUE_LEAD_SECONDS` (1.2s, how long to wait for the user to resume before
     concluding they already finished), so it is only ever paid on an already-truncated phrase —
     short commands keep their original latency, and the worst case adds 1.2s before the cloud
     fall-through.
   - **"for" is recognized as "four".** They are homophones and *both* are in the grammar. This is
     silently worse than a miss: "set a timer for a minute" heard as "...four minute" parsed as
     **4 minutes**, not 60s. **Fix**: in `_parse_duration`, `four` against a *singular* unit
     ("four minute") is read as the "for a <unit>" it must have been, since nobody says "four
     minute". Plural ("four minutes") is a genuine quantity and is untouched.

   A subtlety worth keeping: `looks_truncated()` must gate its two parsers on the `timer`/`alarm`
   token exactly as `match()` does. Running both unconditionally judged "set the timer four"
   *complete*, because `_parse_clock_time` reads the stray "four" as 4 o'clock even though
   `match()` would never consult it without an `alarm` token.

   **Second attempt also failed, for a different and more fundamental reason (2026-08-30).** The
   assistant simply didn't know the capability existed. The journal showed a single
   `Relaying wake trigger from button` and *no `local_intent` line at all* — because
   `local_intent` only runs on the **wake-word** branch (`_handle_voice_detection`). A
   **button-triggered** session calls `WakeTrigger.fire("button")`, which relays straight to
   `voice_agent.py`. So the whole offline timer/alarm path was bypassed, and the cloud LLM — whose
   tool list was Spotify + volume only, and whose system prompt never mentioned timers — answered
   perfectly honestly that it couldn't set one.

   This is the design gap the "§2 fallback" note anticipated, arriving by a different route than
   expected: the issue wasn't Vosk accuracy, it was that **one of the two entry points had no timer
   capability at all**. Fixed by giving the cloud path real tools:

   - New `~/.local/bin/timer_tools.py` — `set_timer`, `cancel_timer`, `set_alarm`, `cancel_alarm`,
     `get_timer_alarm_status`, following `volume_tools.py`'s conventions exactly (plain async
     functions bound directly by `LLMContext` so the docstrings *are* the LLM-facing schema,
     `session_signal.mark_quick_action()` on success, blocking socket calls pushed to a thread).
     They call the same `timer_alarm_client` socket the offline path uses — there is no second copy
     of the scheduling, persistence, ringing or stop-listening logic.
   - `voice_agent.py` — the five tools registered on `LLMContext`, and `VOICE_SYSTEM_PROMPT` updated
     to state the speaker can set timers and alarms. (Checked that `voice-agent.service` does **not**
     override `VOICE_SYSTEM_PROMPT`, or editing the code default would have changed nothing.)

   Verified: all five tools drive the live service correctly (set/status/cancel for both kinds),
   argument validation rejects a zero duration, a >24h duration, hour 25 and a non-numeric duration,
   and Pipecat generates correct schemas for all five including the three zero-argument ones.

   Net effect: **timers now work on both entry points** — offline with no network via the wake word,
   and via the cloud LLM on a button-triggered session or any phrasing the Vosk grammar doesn't
   cover. The offline path stays the fast path; the cloud path is the general one.

   Still to confirm by voice: (needs an actual human — no automated session can speak into the mic):
   - "hey jarvis, set a timer for 30 seconds" → confirm a `local_action` line in `journalctl --user -u wake-word.service`, the LED countdown arc, looped sound + `"ringing"` LED at 0:00.
   - Say "stop" (no wake word) while ringing → confirm it actually stops.
   - Repeat, stopping via the wake button instead.
   - Same pass for "hey jarvis, set an alarm for" a time a minute or two out.
   - Try a few phrasings ("set an alarm for seven fifteen pm", "timer for an hour and thirty minutes") to gauge the grammar/parser's real accuracy — this is the piece flagged above as genuinely untested. The parsers themselves are now confirmed correct on these exact phrasings, so anything that fails live is Vosk *recognition*, not parsing — check the `local_intent: no grammar match (heard: ...)` log line to tell the two apart.
   - Also test "cancel the timer" on a still-counting timer (the gap fixed 2026-08-30).
   - Test the **button** path too, not just the wake word — it takes a completely different route
     (cloud tools, not the offline grammar), and it is the one that was silently broken.
   - Ask "how long is left on my timer?" — exercises `get_timer_alarm_status`, which has no offline
     equivalent at all.
   - Confirm the ring is actually **audible** at a sensible volume through CamillaDSP (`SOUND_FX_VOLUME`, default 0.6) — playback was confirmed to reach the aloop sink, but nobody has heard it yet.
   - Confirm the amber `ringing` blink and the gold countdown arc actually render on the physical ring.
5. **If grammar accuracy is poor**: fall back to routing timer/alarm command parsing through the cloud Gemini tool-calling path instead (playback/stop-detection stay offline regardless) — see §2 above.
6. **A real `sudo reboot`** power cycle is still owed for the whole stack generally (pre-existing open item — see `AI-SPEAKER-SETUP-NOTES.md`'s persistence-verification section) and would also confirm `timer-alarm.service` actually comes up correctly on its own at boot once step 1 enables it.
