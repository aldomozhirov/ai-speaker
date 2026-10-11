# Architecture

How the parts fit together. Details of each area are in the documents linked
from each section.

## Principles

These hold everywhere and are the reason for most of the design:

1. **CamillaDSP is the only thing that plays to the amplifier.** Everything
   else plays into a virtual cable (snd-aloop), and CamillaDSP applies the
   crossover and driver protection. Playing directly to the HiFiBerry would
   send full-range audio to the tweeter.
2. **One volume control.** The PipeWire default sink's volume is the speaker's
   volume. Buttons, Spotify's slider and voice commands all move that one
   value; nothing else attenuates (spotifyd has no mixer, CamillaDSP's volume
   is fixed headroom, ducking is per stream and temporary).
3. **Resolve audio devices by name, every time.** PipeWire node ids change on
   every boot; a stream whose named target does not exist silently plays to
   (or records from) the default device instead. Code waits for the named node
   (`pw_node.py`) and checks the result.
4. **Silent failures get a check.** Most problems in this project left every
   service "active" while something underneath did nothing: echo cancellation
   off, a mic delivering zeros, a stream at 20 % volume.
   `ai-speaker-status.sh` checks each one that has been found.
5. **Small services that can restart on their own.** The wake-word detector
   and the conversation engine are separate processes talking over UNIX
   sockets, so either can crash and restart without the other.
6. **User services, no root at runtime.** Everything except CamillaDSP and its
   web UI runs as the login user under `systemd --user`, which needs systemd
   lingering to start at boot.

## Services

User services (`~/.config/systemd/user`, all `WantedBy=default.target`), in
start order:

| Unit | Program | Does |
|---|---|---|
| `xvf-aec-ref` | `xvf-aec-ref.sh` | Copies the program audio into the XVF3800 as its echo-canceller reference; keeps the link and the chip registers correct for the life of the service |
| `volume-led` | `volume_led.py` | LED ring: volume, assistant state (listening, speaking, error, ringing), timer countdown |
| `volume-sync` | `volume_sync.py` | Keeps PipeWire's volume and Spotify's volume slider in step (MPRIS) |
| `volume-buttons` | `volume-buttons.py` | GPIO buttons: volume up/down, play/pause/next, wake |
| `welcome-sound` | `welcome-sound.sh` | Oneshot: a chime once per boot |
| `timer-alarm` | `timer_alarm.py` | Keeps timers and alarms, rings them, listens for "stop" |
| `wake-word` | `wake_word.py` | Listens for "Hey Jarvis", checks it twice, records near-triggers, relays the trigger |
| `voice-agent` | `voice_agent.py` | Runs a Gemini Live conversation per trigger; owns ducking, DSP profile switching, chimes, tools |
| `spotifyd` | `~/.local/bin/spotifyd` | Spotify Connect receiver ("AI Speaker") |
| `thermal-monitor` | `thermal-monitor/server.py` | Temperature/throttling log and web page on port 8000 |

System services: `camilladsp` (DSP, websocket on 127.0.0.1:1234),
`camillagui` (web UI on port 5005), `wifi-recover.timer` (brings Wi-Fi back if
it failed at boot).

Python services each have their own virtual environment under `~/.venvs/`
(`voice-agent`, `wake-word`, `xvf3800`) plus `~/.local/share/volume-led/venv`,
because pipecat, openWakeWord and the XVF3800 USB library pull in conflicting
dependencies. `volume-buttons`, `volume-sync` and the thermal monitor use the
system Python.

## How the processes talk

All sockets are in `$XDG_RUNTIME_DIR` (`/run/user/<uid>`).

| Socket | Server | Clients | Carries |
|---|---|---|---|
| `wake-trigger.sock` | wake-word | volume-buttons (wake button) | "wake now" from the button |
| `voice-session.sock` | voice-agent | wake-word | `voice`, `button`, `voice-pending` → `voice-confirm` / `voice-reject` |
| `assistant-led.sock` | volume-led | voice-agent, timer-alarm | LED state names, timer progress |
| `timer-alarm.sock` | timer-alarm | voice-agent tools, wake-word (the wake button stops a ringing alarm) | set/cancel/status, stop ringing |
| `wake-word-health.json` (file) | wake-word | ai-speaker-status.sh | detector alive, mic not silent |

Other channels: MPRIS on the session D-Bus (spotifyd ↔ playerctl,
volume-sync), the Spotify Web API (playing a named song), CamillaDSP's
websocket (voice profile during a session), and the XVF3800's USB vendor
interface (registers, LED ring, direction of arrival).

## From "Hey Jarvis" to the reply

1. The XVF3800 beamforms the four mics and removes the speaker's own sound
   (echo cancellation, fed by `xvf-aec-ref`). Its channel 1 (the ASR beam) is
   recorded continuously by `wake-word`.
2. openWakeWord scores every 80 ms frame. A score at or above the threshold
   (0.10) counts only if the **verifier**, a small model trained on this
   household's own "Hey Jarvis", agrees.
3. `wake-word` tells `voice-agent` "voice-pending". The agent opens the mic
   right away (nothing said is lost) but waits for the verdict.
4. The **second opinion** (Vosk or microWakeWord must also hear "Hey Jarvis"
   in the last three seconds) sends confirm or reject within about 0.3 s. On
   reject nothing happens; on timeout (1.5 s) the session goes ahead.
5. The session starts: chime, LED "listening", Spotify ducked to 20 %,
   CamillaDSP switched to the voice profile's processing, Gemini Live
   connected. The audio held since the trigger is sent once Gemini is ready.
6. Gemini answers by voice and may call tools. While it speaks the mic is
   muted (half duplex). Action tools ("play Beatles", "volume 50") close the
   session after one spoken confirmation; the action itself (e.g. starting the
   music) runs after the reply.
7. The session ends after 8 s of silence, a closing phrase, the wake button,
   or a hard limit. Ducking and the DSP profile are restored, even after a
   crash.

## State on disk

| Path | Contents | In git? |
|---|---|---|
| `~/.config/ai-speaker/` | Gemini key, Spotify Web API token | No (secrets) |
| `~/.local/share/ai-speaker/sounds/` | Chimes, timer and alarm sounds (stock sound effects) | Yes (`user/.local/share/ai-speaker/sounds/`) |
| `~/.local/share/ai-speaker/wake-verifier/` | Verifier model and the captures it was trained on | No (household voice) |
| `~/.local/share/ai-speaker/wake-captures/` | Audio around every near-trigger, pruned at 200 MB | No (household voice) |
| `~/.local/share/ai-speaker/vosk-model-small-en-us-0.15/` | Vosk model | No (downloaded) |
| `~/.local/share/ai-speaker/timer-alarm-state.json` | Pending timers and alarms | No (runtime) |
| `$XDG_RUNTIME_DIR/voice-agent-state.json` | What to restore if the agent crashed mid-session | No (runtime) |

All of the "No" items except runtime state are covered by
`tools/private-backup.sh`.
