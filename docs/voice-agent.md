# Voice agent

`voice_agent.py` (`voice-agent.service`) runs one spoken conversation per
wake-up, using [pipecat](https://github.com/pipecat-ai/pipecat) 1.7 and
Gemini Live's native speech-to-speech model.

## Engine

| Setting | Value |
|---|---|
| `VOICE_ENGINE` | `gemini` (default). `openai_realtime` is wired up in `voice_engine.py` but has never been run against a real account |
| `VOICE_MODEL` | `models/gemini-2.5-flash-native-audio-preview-12-2025` (a preview model: expect to change it when Google retires it) |
| `VOICE_NAME` | `Puck` |
| API key | `GOOGLE_API_KEY` in `~/.config/ai-speaker/voice-agent.env` (free tier) |

Gemini Live does recognition, reasoning, speech and tool calls in one
connection. Free-tier limits are shaped by sessions (15 min of audio, about
10 min per connection, 128k tokens of context): context-window compression
(`VOICE_CONTEXT_COMPRESSION=1`, off by default in pipecat) and session
resumption (built into pipecat) let a conversation outlive them.
`VOICE_MAX_SESSION_SECONDS=1800` is an absolute ceiling.

Engines are added in `voice_engine.py` only (one builder function); nothing in
`voice_agent.py` names a provider.

## Audio in and out

`pw_transport.py` (`PwCatTransport`) replaces pipecat's PortAudio transport,
which can only pick devices by index. It runs `pw-cat` against PipeWire nodes
by name (waiting for them with `pw_node.py`):

- **In:** XVF3800 channel 1 (`VOICE_INPUT_CHANNEL=1`), +12 dB
  (`VOICE_INPUT_GAIN_DB`). The mic opens at the trigger, and everything said
  from then on is held (up to `VOICE_PREROLL_SECONDS`) and sent once Gemini is
  ready (1.1–1.6 s after the trigger), led by half a second of silence: a
  stream that starts in mid-speech gets no reply.
- **Out:** the loopback sink (`VOICE_SINK`), like every other source, so the
  speech goes through CamillaDSP and into the echo-canceller reference.

**Half duplex** (`VOICE_HALF_DUPLEX=1`): the mic is muted while the assistant
speaks, plus `VOICE_MUTE_TAIL_SECONDS=0.46` after its last audio. The residual
echo of its own voice is louder at the mic than a person's voice, so in full
duplex it interrupted and answered itself. A stuck mute is impossible:
`VOICE_MAX_MUTE_SECONDS=30` opens the gate anyway. The wake button always
cancels, whatever the gate does.

## Session lifecycle

1. Trigger arrives on `voice-session.sock` (`voice`, `button` or
   `voice-pending` + verdict, see [wake-word.md](wake-word.md)).
2. Chime "session started", LED `listening`, Spotify ducked, CamillaDSP gets
   the voice profile's processing, Gemini connects.
3. Conversation. LED `speaking` while the assistant talks. Every user and
   assistant turn is journalled.
4. The session closes on: `VOICE_IDLE_SECONDS` (8 s) without speech, a closing
   phrase ("goodbye", "never mind", "хватит", "отбой"… `VOICE_CLOSING_PHRASES`), a completed
   one-shot action, the wake button, an `ErrorFrame` from the engine (LED
   `error`), or the absolute ceiling.
5. `finally`: deferred action (e.g. start the music), restore DSP and ducking,
   chime "session ended", LED `idle`. If the process died, the next start
   restores DSP and ducking from `voice-agent-state.json`.

## Tools

Tools are plain async functions; pipecat builds their schema from the
signature and docstring. They are listed in `voice_agent.py` (`LLMContext(tools=[...])`).

| Tool | Module | Does |
|---|---|---|
| `spotify_play`, `spotify_pause`, `spotify_next`, `spotify_previous` | `spotify_tools.py` | Transport via `playerctl` (MPRIS). Only works while a Spotify session exists |
| `spotify_play_track(query, kind)` | `spotify_tools.py` + `spotify_web.py` | Search track/album/artist/playlist and play it on this speaker via the Spotify Web API; works from cold |
| `set_volume(level_percent)`, `adjust_volume(direction, step_percent)` | `volume_tools.py` | The PipeWire default sink via `wpctl` |
| `set_timer`, `cancel_timer`, `set_alarm`, `cancel_alarm`, `get_timer_alarm_status` | `timer_tools.py` | Talk to `timer-alarm.service` over its socket |

**One confirmation, then the action.** Action tools finish with
`session_signal.complete_quick_action(params, result, then=...)`: the session
closes after the spoken confirmation, and `then` (e.g. start playback) runs
when it has closed. Gemini sometimes speaks the confirmation *and* calls the
tool in the same turn; then the result is handed back without asking for a
second reply. (Before 2026-10-04 the music started first and the assistant
then talked over it, twice.)

**Tool guard** (`tool_guard.py`): every tool call is journalled
(`Tool call: …`). Timer and alarm tools (`VOICE_TOOL_GUARD`) are refused if
nothing intelligible was transcribed in the session, because on false wakes the
model sometimes set timers on background noise.

**Spotify Web API setup** (once): create an app at developer.spotify.com
**under the same account spotifyd plays as**, put its id and secret in
`~/.config/ai-speaker/spotify.json`, then run
`~/.venvs/voice-agent/bin/python ~/.local/bin/spotify_web.py auth`. The refresh
token is kept in the same file because it can rotate at runtime.

## Language

Commands come in Russian and English; the wake phrase is English. The system
prompt says to reply in English by default and in Russian only when certain
the request was in Russian (the owner's choice, 2026-10-04; a softer "if unsure,
English" wording did not work).

## Sounds and lights

`sound_fx.py` plays chimes through `ffmpeg | pw-cat` into the loopback sink, so
they are processed and echo-cancelled like everything else. Files in
`~/.local/share/ai-speaker/sounds/`: `session-started`, `session-ended`,
`spotify-connect` (from `spotifyd-event-hook.sh` on spotifyd's
`sessionconnected` event), `welcome` (once per boot), `timer` and `alarm`
(looped while ringing). `SOUND_FX_ENABLED=0` silences the session chimes.

LED ring (`volume_led.py`, states sent over `assistant-led.sock`):

| State | Look |
|---|---|
| idle | Volume level (or the timer's countdown arc while a timer runs) |
| listening | White, the whole ring breathing, brighter with mic level |
| speaking | Green, pulsing with the speech |
| thinking | Rotating green arc (defined; Gemini Live gives no signal to show it) |
| error | Red |
| ringing | Orange, timer or alarm going off |

## Known limits

- Far-field commands over (ducked) music are often not heard by Gemini at
  all, although the words are in the recording (2026-10-05, cause open; next
  idea: explicit activity start/end instead of Gemini's own voice detection).
- In about one session in seven the model says it did something without
  calling the tool.
- `spotify_play`/`pause`/`next` answer "no active player" when no Spotify
  session exists yet (a Web API fallback is not built).
- Replaying recordings through real Gemini sessions uses the same free-tier
  quota; batch experiments have hit 429s. Pace them.
