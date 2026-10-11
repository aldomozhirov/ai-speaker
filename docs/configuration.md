# Configuration reference

Settings are environment variables in the systemd user units
(`user/.config/systemd/user/*.service`). The unit files carry comments with the
reasoning and the measurements behind each value; this page is the index.
"Unit" is the value set in the unit file, "default" is the code's fallback
when the unit sets nothing.

Change a setting by editing the unit **in the repo**, then
`./install.sh apply user --restart`.

## wake-word.service (`wake_word.py`, `wake_capture.py`, `wake_confirm.py`)

| Variable | Unit | Default | Meaning |
|---|---|---|---|
| `WAKE_WORD_MODEL` | hey_jarvis | hey_jarvis | openWakeWord model |
| `WAKE_WORD_THRESHOLD` | 0.10 | 0.5 | Score that counts as a detection |
| `WAKE_WORD_PATIENCE` | 1 | 2 | Frames at/above threshold needed |
| `WAKE_WORD_COOLDOWN` | 1.5 | 1.5 | Seconds between relayed triggers |
| `WAKE_WORD_REFRACTORY` | – | 2.0 | Seconds the detector is held after a trigger |
| `WAKE_WORD_REARM_SCORE` | – | threshold/2 | Score it must fall below to re-arm |
| `WAKE_WORD_CHANNEL` | 1 | 0 | XVF3800 capture channel (1 = ASR beam) |
| `WAKE_WORD_DEBUG` | 1 | 0 | Journal per-frame scores |
| `WAKE_WORD_VERIFIER` | `%h/.local/share/ai-speaker/wake-verifier/{model}.pkl` | off | Verifier model; `0` = off |
| `WAKE_WORD_VERIFIER_THRESHOLD` | 0.5 | 0.5 | Verifier probability needed |
| `WAKE_WORD_SECOND_OPINION` | block | 0 | `block`, `log` or `0` |
| `WAKE_WORD_SECOND_OPINION_VOSK_CONFIDENCE` | – | 0.5 | Vosk confidence that confirms |
| `WAKE_WORD_SECOND_OPINION_MICRO_CUTOFF` | – | 0.5 | microWakeWord probability that confirms |
| `WAKE_WORD_SECOND_OPINION_WINDOW` | – | 3.0 | Seconds of audio judged |
| `WAKE_WORD_SECOND_OPINION_DELAY` | – | 0.16 | Seconds after the trigger before judging |
| `WAKE_WORD_CAPTURE` | 1 | 0 | Save audio around near-triggers |
| `WAKE_WORD_CAPTURE_SCORE` | – | 0.05 | Score that starts a capture |
| `WAKE_WORD_CAPTURE_PRE_SECONDS` / `_POST_SECONDS` / `_BUTTON_SECONDS` | – | 3 / 5 / 10 | Capture length |
| `WAKE_WORD_CAPTURE_MAX_MB` | – | 200 | Oldest captures deleted beyond this |
| `WAKE_WORD_STALL_SECONDS` | – | 10 | Restart capture if no audio for this long |
| `WAKE_WORD_SILENCE_SECONDS` | – | 10 | Report a mic delivering digital silence |
| `LOCAL_INTENT_ENABLED` | 0 | 0 | Offline Vosk command matching (off, see wake-word.md) |
| `LOCAL_INTENT_WINDOW_SECONDS`, `…_SILENCE_CUTOFF_SECONDS`, `…_SILENCE_PEAK`, `…_CONTINUE_SECONDS`, `…_VOLUME_STEP` | – | 5.0, 0.6, 300, 3.0, 10 | Only used when local intent is on |
| `VOICE_SESSION_SOCKET`, `WAKE_TRIGGER_SOCKET` | – | `$XDG_RUNTIME_DIR/…` | Socket paths |

## voice-agent.service (`voice_agent.py` and the tool modules)

| Variable | Unit | Default | Meaning |
|---|---|---|---|
| `GOOGLE_API_KEY` | from `voice-agent.env` | – | Gemini key (secret) |
| `VOICE_ENGINE` | – | gemini | `gemini` or `openai_realtime` |
| `VOICE_MODEL` | gemini-2.5-flash-native-audio-preview-12-2025 | engine's | Model |
| `VOICE_NAME` | Puck | engine's | Voice |
| `VOICE_SYSTEM_PROMPT`, `VOICE_CLOSING_PHRASES` | – | in code | Prompt and closing phrases |
| `VOICE_IDLE_SECONDS` | 8.0 | 8.0 | Silence that ends a session |
| `VOICE_INPUT_CHANNEL` | 1 | WAKE_WORD_CHANNEL | Mic channel sent to Gemini |
| `VOICE_INPUT_GAIN_DB` | 12 | 0 | Gain on that channel |
| `VOICE_PREROLL_SECONDS` | – | 10.0 | Max audio held until Gemini is ready |
| `VOICE_CONFIRM_TIMEOUT_SECONDS` | – | 1.5 | Wait for the second opinion's verdict |
| `VOICE_HALF_DUPLEX` | 1 | 1 | Mute mic while the assistant speaks |
| `VOICE_MUTE_TAIL_SECONDS` | 0.46 | 0.4 | Mic stays muted this long after speech |
| `VOICE_MAX_MUTE_SECONDS` | 30 | 30 | Safety: mute never lasts longer |
| `VOICE_OUTPUT_GAIN` | – | 1.0 | Software gain on the assistant's voice |
| `VOICE_CONTEXT_COMPRESSION` | 1 | 1 | Sliding-window context compression |
| `VOICE_COMPRESSION_TRIGGER_TOKENS` | – | engine's (80 %) | When to compress |
| `VOICE_MAX_SESSION_SECONDS` | 1800 | 1800 | Absolute session limit |
| `VOICE_SINK` | – | loopback sink | Where speech is played |
| `VOICE_DUCK`, `VOICE_DUCK_LEVEL`, `VOICE_DUCK_RAMP_MS`, `VOICE_DUCK_MATCH` | 1, 0.20, 200, `spotifyd\|librespot` | same | Ducking |
| `VOICE_PROFILE`, `CAMILLA_HOST`, `CAMILLA_PORT`, `CAMILLA_PROFILES_DIR` | – | voice, 127.0.0.1, 1234, /etc/camilladsp/profiles | DSP profile used during sessions |
| `VOICE_TOOL_GUARD` | set_timer,set_alarm,cancel_timer,cancel_alarm | – | Tools refused when nothing intelligible was heard |
| `VOICE_TOOL_GUARD_WAIT_SECONDS` | – | 1.0 | How long a guarded tool waits for a transcript |
| `SOUND_FX_ENABLED` | 1 | 1 | Session chimes |
| `SOUND_FX_VOLUME`, `SOUND_FX_RATE`, `SOUND_FX_DIR`, `SOUND_FX_SINK` | – | 0.6, 48000, sounds dir, loopback sink | Chime playback |
| `PW_TRANSPORT_DEBUG` | 1 | 0 | Journal mic levels and gate state |
| `SPOTIFY_CONFIG`, `SPOTIFY_DEVICE_NAME`, `SPOTIFY_MARKET`, `SPOTIFY_HTTP_TIMEOUT` | – | `~/.config/ai-speaker/spotify.json`, AI Speaker, account's, 4.0 | Web API |
| `WPCTL_PATH`, `PLAYERCTL_PATH`, `VOLUME_SINK`, `VOLUME_MAX` | – | /usr/bin/…, `@DEFAULT_AUDIO_SINK@`, 1.0 | Volume and transport tools |

## xvf-aec-ref.service (`xvf-aec-ref.sh`)

| Variable | Unit | Meaning |
|---|---|---|
| `XVF_AEC_REF_DELAY` | 0 | Extra delay pw-loopback adds to the reference, seconds. Adds to the XVF3800 sink's headroom in `51-smart-speaker.conf`; change both together and re-measure |

## timer-alarm.service (`timer_alarm.py`)

| Variable | Unit | Default | Meaning |
|---|---|---|---|
| `WAKE_WORD_CHANNEL` | 1 | 0 | Channel the "stop" listener uses (must match wake-word) |
| `TIMER_ALARM_MAX_RING_SECONDS` | – | 600 | Ringing stops by itself after this |
| `TIMER_ALARM_SOCKET` | – | `$XDG_RUNTIME_DIR/timer-alarm.sock` | Socket |

## volume-led.service (`volume_led.py`)

| Variable | Unit | Default | Meaning |
|---|---|---|---|
| `VOLUME_LED_HOLD`, `VOLUME_LED_FADE` | 0.90, 0.55 | – | Seconds the volume display stays, then fades |
| `VOLUME_LED_BRIGHTNESS`, `VOLUME_LED_FPS` | 0.32, 30 | – | Brightness, frame rate |
| `VOLUME_LED_LOW_COLOUR`, `_HIGH_COLOUR`, `_MUTE_COLOUR` | FFFFFF, FFFFFF, FF180C | 0068FF, 00D8C0, FF180C | Volume colours |
| `VOLUME_LED_START_PIXEL`, `VOLUME_LED_REVERSE` | 0, 0 | – | Ring orientation |
| `ASSISTANT_LED_LISTENING_COLOUR` | FFFFFF | FFFFFF | Listening |
| `ASSISTANT_LED_SPEAKING_COLOUR`, `_THINKING_`, `_ERROR_`, `_RINGING_`, `TIMER_LED_COLOUR` | – | 20D060, 00E676, FF2A1E, FF8800, FFC800 | Other states |
| `ASSISTANT_LED_DOA_OFFSET_DEG` | 180 | – | Mic direction zero vs LED pixel 0 (calibrated 2026-08-22) |

## volume-buttons.service (`volume-buttons.py`)

`VOLUME_UP_GPIO` 24, `VOLUME_DOWN_GPIO` 25, `PLAYBACK_GPIO` 22, `WAKE_GPIO` 23
(BCM), `VOLUME_STEP_PERCENT` 5, `VOLUME_MAX` 1.0. None set in the unit; the
code defaults apply.

## spotifyd.service

`PULSE_PROP=state.restore-props=false` (WirePlumber must not restore this
stream's volume, see audio-pipeline.md). The receiver itself is configured in
`~/.config/spotifyd/spotifyd.conf`: name "AI Speaker", PulseAudio backend,
320 kbit/s, `volume_controller = "none"`, MPRIS on the session bus, event hook
`spotifyd-event-hook.sh`.

## Outside the units

| File | Settings |
|---|---|
| `~/.config/wireplumber/wireplumber.conf.d/51-smart-speaker.conf` | Loopback sink format, `priority.driver = 2500`, `priority.session = 1400`, shared `clock.name`, XVF3800 sink `api.alsa.headroom = 1792` |
| `/etc/systemd/system/camilladsp.service` | Start gain −7 dB, websocket 127.0.0.1:1234 |
| `/etc/camillagui/camillagui.yml` | Web UI on 0.0.0.0:5005 (bound to all addresses since 2026-09-21: a fixed LAN address broke when DHCP moved the Pi) |
| `/etc/camillagui/gui-config.yml` | UI options, "Bass boost" shortcut (0–5 dB) |
| XVF3800 state (applied by `xvf-aec-ref.sh`, never saved to flash) | Register `AUDIO_MGR_FAR_END_DSP_ENABLE=1`; the card's playback mixer at 0 dB (`PCM,0` through the PipeWire sink volume 1.0, `PCM,1` with amixer) |
