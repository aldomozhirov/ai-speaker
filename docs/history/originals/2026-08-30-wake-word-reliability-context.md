# "Hey Jarvis" reliability — collected context (2026-08-30)

**Purpose of this file:** a single place holding everything currently known
about the wake-word reliability complaint, plus a snapshot of the device's
*actual* live configuration as measured on 2026-08-30 ~15:00 CEST. This is a
**collection document only** — no analysis, no hypotheses, no proposed fixes.
Those belong in a follow-up document.

Everything below marked "measured" was read off the running device in this
session. Everything marked "historical" is carried over from the earlier
documents listed in [§9](#9-related-documents) and is only as good as those
documents were.

---

## 1. The reported symptoms (user, 2026-08-30)

Four distinct complaints, stated together. They are listed separately because
they are not obviously the same failure:

| # | Symptom | Condition |
|---|---|---|
| S1 | Wake word "hears very badly" — needs repeating / close range | **Quiet room**, no program audio |
| S2 | Wake word almost impossible to trigger; sometimes works only very close to the device | **While music is playing on the device** |
| S3 | **False activations** — the speaker starts listening when nobody is talking | Ambient / unspecified |
| S4 | Once listening is active, **speech recognition makes mistakes** when the user is far away | During an open session |

Notes on how these relate to prior reports:

- S2 matches the long-standing, never-closed "music kills the wake word"
  issue tracked since the first sessions (see §9).
- **S1 is a change from the last recorded measurement.** On 2026-08-22 the
  quiet room measured *excellent*: utterance scores 0.94–0.99 against a
  0.000–0.001 noise floor (separation ~950x). The user now reports the quiet
  room as bad too. Either the environment/placement changed, or something
  regressed since; **not established which**.
- **S3 (false activations) has never been measured on this device.**
  `WAKE_WORD_GEMINI_INTEGRATION.md` line 302 records the false-accept rate as
  an explicit open item: *"Formal false-accept rate (<1/hour target) — never
  formally measured; threshold was tuned by separation ratio instead."*
- S4 concerns a different subsystem from S1–S3: after the wake word fires,
  audio goes either to offline Vosk (`local_intent.py`) or to cloud Gemini
  Live (`voice_agent.py`). Neither is openWakeWord. See §5.

### Timeline context that may matter

`WAKE_WORD_THRESHOLD` was lowered **0.35 → 0.25 → 0.10** over the past week
(see §3 and §7). The 0.10 value has been live since 2026-08-24. S3 (false
activations) is a new complaint in the same window; the false-positive floor
measurement that justified 0.10 covered **2 minutes, one genre, one volume**,
and its own author flagged speech-heavy program material as untested.

---

## 2. Hardware / platform snapshot (measured)

```
host          ai-speaker  (Raspberry Pi 5)
kernel        6.18.34+rpt-rpi-2712 aarch64, Debian 1:6.18.34-1+rpt1
uptime        40 min at time of capture (booted 2026-08-30 14:21 CEST)
temp          50.5 °C
throttled     0x0          <-- no throttling, and no throttle history bits
cooling fan   /proc/device-tree/cooling_fan/status = disabled
              /sys/class/thermal/cooling_device* = absent
```

**This is a material change from the recorded history.** As of 2026-08-24 this
Pi sat at 83–87 °C continuously with `get_throttled=0xe0008` and was
hard-crashing (see `ai-speaker-overheating` memory). It now reads ~48–51 °C
with a clean `0x0`. Live thermal-monitor samples confirm the low readings:

```
2026-08-30T15:03:30, 51.25 °C, ..., 0x0
2026-08-30T15:03:35, 48.50 °C, ..., 0x0
2026-08-30T15:03:40, 47.95 °C, ..., 0x0
```

The device tree still reports no fan, so **why it now runs cool is unexplained
and unverified** (possible: added cooling, different ambient, lower load).
Recorded here because prior docs instruct "check temperature first" — that
check now passes.

### Audio hardware chain

```
Mic in :  Seeed reSpeaker XVF3800 4-Mic Array (USB)
          firmware VERSION = 2 1 0   (vendor README examples show 2 0 2)
          analog output deliberately UNCONNECTED
Speaker:  HiFiBerry AMP4 HAT  (dtoverlay=hifiberry-dacplus-std)
Path   :  app -> snd-aloop (ALSA `default`/`smartspk`, dmix, 48 kHz)
             -> CamillaDSP (--gain=-7, /etc/camilladsp/active.yml)
             -> hw:sndrpihifiberry -> AMP4 -> drivers
AEC ref:  aloop sink monitor -> pw-loopback (--delay=0.341, mono FL)
             -> XVF3800 USB playback sink (this is the far-end reference)
```

`/etc/camilladsp/` is root-only (`Permission denied` for the `aldom` user), so
the active DSP config could not be read in this session. The unit file is
readable: `/etc/systemd/system/camilladsp.service`, running as user
`camilladsp`, `--gain=-7`.

`/boot/firmware/config.txt` relevant lines: `dtoverlay=vc4-kms-v3d,noaudio`,
`dtoverlay=dwc2,dr_mode=host`, `dtoverlay=nospi10`,
`dtoverlay=hifiberry-dacplus-std`.

---

## 3. Wake-word configuration (measured, live)

`~/.config/systemd/user/wake-word.service`:

```ini
ExecStart=%h/.venvs/wake-word/bin/python -u %h/.local/bin/wake_word.py

Environment=WAKE_WORD_MODEL=hey_jarvis
Environment=WAKE_WORD_THRESHOLD=0.10
Environment=WAKE_WORD_PATIENCE=1
Environment=WAKE_WORD_COOLDOWN=1.5
Environment=WAKE_WORD_CHANNEL=1
Environment=WAKE_WORD_DEBUG=1
Environment=LOCAL_INTENT_ENABLED=1
```

Backups present, showing the tuning history:

| File | Contents |
|---|---|
| `wake-word.service.bak` | `THRESHOLD=0.35`, `CHANNEL=0` (original) |
| `wake-word.service.bak-threshold-0.25` | `THRESHOLD=0.25`, `CHANNEL=1` |
| `wake-word.service.bak-preenable` | pre-`LOCAL_INTENT_ENABLED=1` |
| *(live)* | `THRESHOLD=0.10`, `CHANNEL=1`, local intent on |

### openWakeWord runtime (measured)

```
openwakeword   0.4.0
onnxruntime    1.29.0   (providers: AzureExecutionProvider, CPUExecutionProvider)
numpy          2.5.2
vosk           0.3.45
model          hey_jarvis_v0.1.onnx  (bundled pretrained, 1,271,370 bytes)
```

Bundled models also available but unused: `alexa_v0.1`, `hey_mycroft_v0.1`,
`timer_v0.1`, `weather_v0.1`.

### openWakeWord features present in the installed version but NOT in use

Read directly from the installed `openwakeword/model.py` `Model.__init__`
signature. `wake_word.py` passes **only** `wakeword_model_paths`; all of these
are left at their defaults:

| Parameter | Default | Currently |
|---|---|---|
| `enable_speex_noise_suppression` | `False` | off |
| `vad_threshold` | `0` (VAD disabled) | off |
| `custom_verifier_models` | `{}` | none |
| `custom_verifier_threshold` | `0.1` | n/a |

Dependency status for those: `libspeexdsp1` **is** installed at the system
level (`1.2.1-3`), but the Python binding `speexdsp_ns` is **not** installed in
the `wake-word` venv (`ModuleNotFoundError`).

### Detection loop mechanics (from `~/.local/bin/wake_word.py`)

- Capture: `pw-cat --record --target <XVF3800 source> --rate 16000
  --channels 2 --format s16 --latency 40ms -`
- Frame: 1280 samples (80 ms), s16 interleaved stereo, then **one channel is
  selected by index** (`CHANNEL_INDEX`), never downmixed — the code comments
  record that averaging the two measurably hurt detection because the XVF3800's
  L/R are two *different processed signals*, not a stereo pair.
- `model.predict(frame, patience={...}, threshold={...})` — `predict()` zeroes
  the returned score unless `PATIENCE_FRAMES` consecutive frames cleared the
  threshold. With `PATIENCE=1` that gate is effectively a single frame.
- `WAKE_WORD_DEBUG=1` logs `raw_score`/`gated_score` **only when
  `raw_score > 0.05`** — anything below that is invisible in the journal.
  (`wake-live.py` exists specifically to see sub-0.05 frames.)
- Firing path: `score >= THRESHOLD` → `_handle_voice_detection()` →
  if local intent enabled, buffer command audio and try Vosk grammar match;
  on no-match, relay to `voice_agent.py` over `VOICE_SESSION_SOCKET`.
- `WakeTrigger` debounces voice relays by `COOLDOWN=1.5 s`. Button presses are
  **not** debounced.
- A ringing timer/alarm short-circuits everything: a wake trigger silences the
  ring instead of opening a session.

---

## 4. XVF3800 chip state (measured, live)

```
AUDIO_MGR_MIC_GAIN            90.0        (Seeed default: 90)
AUDIO_MGR_REF_GAIN            8.0         (Seeed default: 8.0)
AUDIO_MGR_SYS_DELAY           12          (Seeed default: 12; = 0.75 ms @16k)
AUDIO_MGR_OP_L                8 0         (user chosen ch = auto-select beam)
AUDIO_MGR_OP_R                7 3         (AEC residual / ASR output, beam 3)
AUDIO_MGR_FAR_END_DSP_ENABLE  1           (echo cancellation structurally ON)
AEC_AECCONVERGED              0           (nothing was playing at read time)
AEC_FAR_EXTGAIN               0.0
AEC_RT60                      1.401e-45   (denormal -> no room estimate formed)
PP_AGCONOFF                   1           (AGC ON)
PP_AGCMAXGAIN                 64.0        (Seeed default)
PP_AGCGAIN                    13.33       (Seeed default is 2.0)
PP_AGCDESIREDLEVEL            0.0045
PP_MIN_NS                     0.15
PP_MIN_NN                     0.51
GPO_READ_VALUES               0 0 0 1 0   (X0D30 mic-mute pin LOW = not muted)
```

Live beamformer telemetry, sampled 3x in a quiet room with nothing playing:

```
AEC_AZIMUTH_VALUES   4.1058  5.7715  0.7070..0.7569  0.7070..0.7569
                     (beam1)(beam2) (free-run)      (auto-select)
AEC_SPENERGY_VALUES  0.0  0.0  0.0  0.0     <- no speech detected, as expected
```

Register names that do **not** exist on this firmware (tried, rejected by the
vendored register map): `PP_GAMMA_NS`, `PP_GAMMA_NN`, `AEC_ERLE`,
`AUDIO_MGR_OP_GAIN_ASR`, `AUDIO_MGR_OP_GAIN_COMMS`, `GPI_READ_VALUES`.

### Standing conventions for this chip (from project docs, still in force)

- All register writes are **RAM-only**; `SAVE_CONFIGURATION` is deliberately
  never called. Anything that must survive a power cycle is reapplied on every
  service start by `xvf-aec-ref.sh`.
- `AUDIO_MGR_FAR_END_DSP_ENABLE` resets to 0 on every power cycle / USB
  re-enumeration. `xvf-aec-ref.sh` reapplies it with a 30-attempt retry loop,
  plus a re-check every ~5 min.
- `AUDIO_MGR_OP_L` / `AUDIO_MGR_OP_R` have **no** reapply-on-start loop. They
  survived a physical replug once, but nothing restores them if chip RAM is
  ever lost. Flagged as a gap in `WAKE_WORD_SENSITIVITY_INVESTIGATION.md`.

### Vendor documentation on hand

`~/src/reSpeaker_XVF3800_USB_4MIC_ARRAY/host_control/README.md` (250 lines,
third-party, not ours to modify). Contains the full `AUDIO_MGR_OP_L/R` debug-mux
category table (categories 0–12), the LED/GPIO/save-configuration command sets,
DoA and speech-energy documentation, and Seeed's stated default tuning values.
Its "Tuning" section points at the XMOS `fwk_xvf` guide for
`AUDIO_MGR_SYS_DELAY` (system delay), `PP_DTSENSITIVE` (echo suppression),
noise suppression and output EQ. Note: direct `WebFetch` of xmos.com returns
**HTTP 406**; a web search surfaces the same content fine.

Debug-mux categories, transcribed for convenience:

```
 0 Silence                                     (source 0)
 1 Raw mic data, before amplification          (sources 0-3, no system delay)
 2 Unpacked mic data                           (sources 0-3)
 3 Amplified mic data WITH system delay        (sources 0-3) <- fed to SHF cores
 4 Far-end reference, post-SRC to 16 kHz       (source 0)
 5 Far-end reference + system delay            (source 0)
 6 Processed/beamformed  0,1 slow  2 fast  3 auto-select
 7 AEC residual / ASR data                     (sources 0-3)
 8 User chosen channels                        (0,1 = copy of 6/3; L default)
 9 Post-SHF DSP channels                       (0-3)
10 Far end at native rate                      (0-5)
11 Amplified mic data BEFORE system delay      (0-3)
12 Amplified far end + gain + system delay     (source 0) <- ref into SHF cores
```

---

## 5. What happens *after* the wake word fires (relevant to S4)

The device has **two entry points that share almost nothing**:

```
voice path :  wake_word.py  --(local_intent, Vosk, grammar-constrained)-->
                            local_actions.py    [offline, no cloud]
                            ...on no-match, falls through to:
both paths :  voice_agent.py -> Gemini Live (cloud)
button path:  volume-buttons.py -> WakeTrigger.fire("button") -> voice_agent.py
```

### Offline recognition (`local_intent.py` + Vosk)

```
model                                 vosk-model-small-en-us-0.15
                                      (~/.local/share/ai-speaker/)
LOCAL_INTENT_ENABLED                  1   (live)
LOCAL_INTENT_WINDOW_SECONDS           5.0
LOCAL_INTENT_SILENCE_CUTOFF_SECONDS   0.6
LOCAL_INTENT_SILENCE_PEAK             300   (int16 peak = silence threshold)
LOCAL_INTENT_CONTINUE_SECONDS         3.0   (truncation continuation pass)
LOCAL_INTENT_CONTINUE_LEAD_SECONDS    1.2
```

Known behaviour of this layer, already documented:
- Utterances get split across recognition windows by the 0.6 s silence cutoff;
  a `looks_truncated()` continuation pass was added 2026-08-30 to compensate.
- Homophone collisions in the grammar produce **silently wrong** results, not
  misses. `"for"` → `"four"` was found and fixed; `"to"/"two"`, `"ate"/"eight"`,
  `"one"/"won"` are the same shape of trap and are unaudited.
- A `local_intent` miss can mean three different things: nothing recognized,
  recognized-but-truncated, or recognized-but-misparsed. The actual
  `local_intent: ...` journal line distinguishes them.

### Cloud session (`voice_agent.service`, measured)

```
VOICE_MODEL              models/gemini-2.5-flash-native-audio-preview-12-2025
VOICE_NAME               Puck
VOICE_INPUT_CHANNEL      0        <-- NB: channel 0, while wake-word uses 1
VOICE_IDLE_SECONDS       8.0
VOICE_HALF_DUPLEX        1        (mic muted while bot speaks)
VOICE_MUTE_TAIL_SECONDS  0.4
VOICE_MAX_MUTE_SECONDS   30
VOICE_CONTEXT_COMPRESSION 1
VOICE_MAX_SESSION_SECONDS 1800
VOICE_DUCK               1
VOICE_DUCK_LEVEL         0.20
VOICE_DUCK_RAMP_MS       200
VOICE_DUCK_MATCH         spotifyd|librespot
SOUND_FX_ENABLED         1
```

`timer-alarm.service` sets `WAKE_WORD_CHANNEL=1` explicitly, with a comment
noting `timer_alarm.py`'s own default of 0 would listen on the weaker channel.
**`voice-agent.service` still uses `VOICE_INPUT_CHANNEL=0`** — recorded here as
a measured fact; whether that is intentional is not established anywhere in the
docs.

Half-duplex rationale, from the unit file itself: *"residual echo from the
XVF3800 AEC is louder than the user's voice, so full duplex makes the assistant
interrupt and answer itself."*

---

## 6. PipeWire / routing state (measured)

```
PipeWire 1.4.2, WirePlumber 1.4.2

Sinks:
  * 35. Loopback Analog Stereo                       [vol: 0.30]   <- default
    97. reSpeaker XVF3800 4-Mic Array Analog Stereo  [vol: 1.00]   <- AEC ref
Sources:
    52. Loopback Analog Stereo                       [vol: 1.00]
  * 98. reSpeaker XVF3800 4-Mic Array Analog Stereo  [vol: 1.00]   <- default
```

Links (`pw-link -l`), confirming the AEC reference is correctly wired and is
**not** in the known-bad self-referential fallback state:

```
output.xvf-aec-ref:output_FL -> alsa_output.usb-...XVF3800...:playback_FL   OK
input.xvf-aec-ref:input_FL  <- alsa_output.platform-snd_aloop.0...:monitor_FL
input.xvf-aec-ref:input_FR  <- alsa_output.platform-snd_aloop.0...:monitor_FR
```

Processes holding the audio taps, identified by cgroup (verified, not
inferred):

```
pid 1853  wake-word.service   pw-cat --record --target <XVF3800 in>
                              --rate 16000 --channels 2 --latency 40ms
pid 1333  volume-led.service  pw-record --target <XVF3800 in>
                              --rate 48000 --channels 2 --latency 10ms
pid 1345  volume-led.service  pw-record --target <aloop sink monitor>
                              --rate 48000 --channels 1 --latency 10ms
pid 1256  xvf-aec-ref.service pw-loopback (the AEC reference feed)
```

So **two services read the XVF3800 mic concurrently** — `wake-word` at 16 kHz
and `volume-led` at 48 kHz. `timer-alarm.service` is running but is *not*
holding a mic tap at idle, despite its `WAKE_WORD_CHANNEL=1` setting.

ALSA mixer on the XVF3800 card (`amixer -c Array`), all at maximum, as
`assert_reference_gains()` intends:

```
'PCM',0   Front L/R  60 [100%] [0.00dB] [on]
'PCM',1   Mono       60 [100%] [0.00dB] [on]
'Headset',0 capture   60 [100%] [0.00dB] [on]
'Headset',1 capture   60 [100%] [0.00dB] [on]
```

### Ambient capture level measured this session

5 seconds of room tone, nothing playing, captured through the exact path
`wake_word.py` uses:

```
ch0:  rms = -39.7 dBFS    peak = -20.3 dBFS
ch1:  rms = -46.9 dBFS    peak = -26.2 dBFS      <- the channel in use
```

---

## 7. Journal evidence available (measured)

**Journald is now persistent.** `/etc/systemd/journald.conf.d/99-persistent.conf`
sets `Storage=persistent` and sorts above the stock
`/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`
(`Storage=volatile`). `/var/log/journal/9683fe07a56.../` exists and holds data.
Earliest surviving entry: **2026-08-30T00:20:27**. Everything before that date
is lost.

> This corrects the `ai-speaker-known-config-bugs` memory, which recorded
> journald as still volatile and the drop-in as misnamed `10-persistent.conf`.
> Both have since been fixed on the box.

**Reading the logs — the one command that works here:**

```bash
journalctl _SYSTEMD_USER_UNIT=wake-word.service
```

`journalctl --user -u wake-word.service` returns *"No journal files were found"*
on this box even with `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` exported
— there is no per-user journal. Several project docs still give the wrong form.
Helpers: `~/.local/bin/wake-scores.sh`, `~/.local/bin/wake-live.py`.

For `systemctl --user` and `pw-cli`, these two exports *are* required:

```bash
export XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
```

`pactl` is not installed; use `pw-cli ls Node` / `wpctl status`.

### All wake-word activity in the surviving journal

Score distribution for **all 18** `raw_score` lines logged today (remember: only
frames above 0.05 are logged at all):

```
0.0–0.1 :  8
0.1–0.2 :  5
0.2–0.3 :  3
0.3–0.4 :  1
0.4–0.5 :  1     <- single highest score in the whole log, 0.445
```

Voice relays that actually fired (i.e. crossed `THRESHOLD=0.10`):

```
Aug 30 00:40:12  score 0.139   local_intent heard 'hours'
Aug 30 00:40:16  score 0.119   local_intent heard 'set the timer four'
Aug 30 00:40:22  score 0.445   local_intent heard ''
Aug 30 00:40:27  score 0.302   local_intent heard ''
Aug 30 00:40:30  score 0.225   local_intent heard ''
Aug 30 14:45:29  score 0.144   local_intent heard ''
Aug 30 14:45:33  score 0.255   local_intent heard ''
Aug 30 14:45:38  score 0.199   local_intent heard ''
Aug 30 14:45:43  score 0.106   local_intent heard ''
```

Plus 8 `source: button` relays (undebounced, from the GPIO wake button).

Two facts worth carrying forward exactly as stated:

1. **No wake-word score in the surviving log has exceeded 0.445.** The
   quiet-room measurement from 2026-08-22 recorded 0.94–0.99.
2. The four detections at 14:45 all report `local_intent: no grammar match
   (heard: '')` — Vosk transcribed **nothing** after the trigger. Whether those
   were real utterances, or S3 false activations, **is not established** and
   requires asking the user what happened at 14:45 on 2026-08-30.

---

## 8. Measurements and conclusions carried over from prior sessions

Reproduced here so this document stands alone. Each entry notes its status.

### 8.1 Quiet-room baseline (2026-08-22) — historical, contradicts S1

Via `ai-speaker-calibrate.py wake`:

| Channel | Noise floor | Utterance peaks | Separation |
|---|---|---|---|
| ch0 | 0.000 | 0.996 / 0.961 / 0.953 / 0.943 (1 of 5 missed) | 943x |
| ch1 | 0.001 | 0.995 / 0.987 / 0.966 / 0.978 (1 of 5 missed) | 966x |

Result: channel 1 selected, threshold recommended 0.25.

### 8.2 With-music baseline (2026-08-22) — historical, **did not reproduce**

| Channel | Noise floor | Utterances | Separation |
|---|---|---|---|
| ch0 | 0.722 | 0 of 5 detected | — |
| ch1 | 0.964 | mostly undetected, one 0.065 | 0.1x |

On 2026-08-24 the same measurement was re-run with music at a verified
-11.5 dBFS at the aloop sink / -20.2 dBFS at mic 0: openWakeWord's peak over
25 s was **0.000–0.010** on both channels, in both the starved and fixed
reference states. **The 0.964 figure has never been explained or reproduced.**

### 8.3 AEC far-end reference was starved — FIXED 2026-08-24

The XVF3800's USB *playback* sink sat at PipeWire volume 0.40, and that volume
**is** the hardware mixer (verified: `wpctl set-volume <xvf> 1.0` moves
`amixer -c Array PCM,0` to 0.00 dB). At 0.40 it read -23 dB, and since the AEC
reference is delivered *to that sink*, the reference reached the chip ~40 dB
down — at the far-end taps' noise floor.

Measured with the debug mux, -20 dBFS pink noise through the normal path:

| `AUDIO_MGR_OP_L` | before | after |
|---|---|---|
| `0 0` silence (control) | -65.0 dBFS | -89.9 dBFS |
| `3 0` mic 0 (control) | -32.6 dBFS | -33.3 dBFS |
| `10 0` far end, native rate | -64.5 dBFS | -24.2 dBFS |
| `4 0` far end, post-SRC 16k | -64.9 dBFS | -30.5 dBFS |
| `12 0` amplified far end → SHF | -60.3 dBFS | -15.3 dBFS |

Fix lives in `xvf-aec-ref.sh::assert_reference_gains()`, reapplied on every
start. **Confirmed still holding today** (§6: sink 97 at vol 1.00, both PCM
mixers at 0.00 dB).

Corroborating symptoms this explained: `AEC_AECCONVERGED=1` at all times (a
filter converges trivially against silence) and `AEC_RT60 = 1.4e-45` (a
denormal — no room estimate ever formed). **`AEC_RT60` still reads 1.4e-45
today** (§4).

> **Explicitly not established:** that this fix improved wake-word detection
> during music. The doc says so in as many words. Every accessible output tap
> has AGC on it (`PP_AGCMAXGAIN=64`), so output RMS cannot measure cancellation
> depth — channel 1 read an identical +28.x dB in *every* condition across
> 0–341 ms of reference delay and 43 dB of reference gain, because that is the
> AGC target, not the residual. A genuinely pre-AGC tap has not been identified.

### 8.4 Threshold was calibrated for the wrong condition — CHANGED 2026-08-24

User's own test pulled from the journal gave three utterances over music:
peaks **0.308 (fired), 0.152 (missed), 0.443 (fired)** — against
`THRESHOLD=0.25`.

Measured false-positive floor over 2 minutes of music at a verified -11.7 dBFS
program level (1500 frames per channel): **ch0 max 0.0153, ch1 max 0.0018,
zero frames above 0.05.**

Threshold lowered to **0.10**. The author's own caveats, verbatim in spirit:

- This is *a workaround, not a cure* — utterance scores still collapse from
  ~0.95 quiet to 0.15–0.44 over music, which is a signal-quality problem.
- The floor measurement was **2 minutes, one genre, one volume**. Speech-heavy
  program material (podcasts, rap) is the plausible false-positive risk and
  **was not tested**.
- *"If false wakes appear, raise toward 0.15 before reverting to 0.25."*
- Suggested next levers, untried: openWakeWord's `vad_threshold`,
  `enable_speex_noise_suppression`, and a `custom_verifier_model` trained on
  the user's own voice.

### 8.5 Reference-delay alignment — OPEN, measurement invalid

- `AUDIO_MGR_SYS_DELAY = 12` samples = 0.75 ms at 16 kHz (Seeed's default).
- XMOS guidance (via web search of the `fwk_xvf` tuning guide): residual
  reference↔mic delay should end up **at or below ~40 samples (2.5 ms)**.
- The `pw-loopback --delay=0.341` figure in `xvf-aec-ref.sh` was long assumed
  to be a measured path latency. **It is not** — it is exactly the aloop ring
  *buffer size* (`buffer_size 16384 / 48000`). Live ALSA pointers put the real
  tap→speaker path at roughly **90–110 ms** (aloop delay 465 = 9.7 ms,
  CamillaDSP capture 1064 = 22.2 ms, HiFiBerry 3222 = 67.1 ms).
- So the reference has *plausibly* been ~230 ms late for the life of the
  project. **Unconfirmed.**
- A delay sweep was run and found no effect — but its metric was
  AGC-confounded (§8.3), so that is **not** evidence of no effect.
- Overridable via `XVF_AEC_REF_DELAY` (default `0.341`, unchanged).

### 8.6 Debug-mux measurement chain — RESOLVED (a prior conclusion was wrong)

Session 2 (2026-08-23) concluded the `AUDIO_MGR_OP_L/R` debug mux "may not be
switching," because categories 4 and 5 always read identical. **That was
wrong** — both were *silent* (the reference was starved, §8.3), and the session
had no silence control (category 0) to tell the two apart. With a silence
control included, category 0 reads -65/-89 dBFS against category 3's -33 dBFS,
and the mic reading repeats to 0.1 dB. `xvf_register.py set` round-trips
correctly. The "re-validate the mux first" next step in
`WAKE_WORD_SENSITIVITY_INVESTIGATION.md` is unnecessary.

### 8.7 Things ruled out or already correct

- Channel swap (0 ↔ 1) was tried and **ruled out** as a fix for the music case.
- AEC reference *routing* is correct and actively supervised (§6).
- `AUDIO_MGR_FAR_END_DSP_ENABLE=1` is on and reapplied with retries.
- All 8 services active, zero failed units, lingering enabled, Gemini API
  reachable (`ai-speaker-status.sh` = **OK with 1 WARN**; the warn is
  `AEC_AECCONVERGED=0`, expected with nothing playing).
- Mic is not hardware-muted (`GPO` pin X0D30 low).
- Thermal throttling — currently clean (§2), unlike the 2026-08-24 state.

---

## 9. Related documents and code

### Project documents (`~/claude-project/`)

| File | What it holds |
|---|---|
| `WAKE_WORD_SENSITIVITY_INVESTIGATION.md` | Sessions 1–2. **Read its "SUPERSEDED" banner** — two of its conclusions are corrected by the next file. |
| `AEC-REFERENCE-GAIN-FINDING.md` | The reference-gain root cause, the threshold-0.10 follow-up, and the observability gap |
| `AI-SPEAKER-SETUP-NOTES.md` | Whole-stack setup history and decisions; "Still open" section |
| `WAKE_WORD_GEMINI_INTEGRATION.md` | Build guide; Step 3 covers wake-word tuning, incl. the never-measured false-accept rate |
| `AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md` | `pw_node.py`, status script, `VOICE_ENGINE` factory, local intent |
| `TIMER-ALARM-IMPLEMENTATION.md` | Timer/alarm; the two-entry-point lesson |
| `hifiberry-amp4-rpi5-config.md`, `raspberry-pi-5-spotifyd-camilladsp-setup.md` | Audio chain build notes |

### Measurement harnesses already written (`~/claude-project/`)

```
aec-delay-sweep.py     reference-delay sweep       -> aec-sweep-results{,-fixed}.json
aec-gain-sweep.py      AEC_FAR_EXTGAIN sweep       -> aec-gain-results.json
aec-refcheck.py        far-end presence, w/ controls
aec-refpath.py         localises where the reference dies
aec-erle-sweep.py      ERLE attempt -- METRIC IS AGC-CONFOUNDED, do not trust
```

All of these stop `wake-word`/`voice-agent` and pause playback while running,
and restore registers, volumes, drop-ins and services on exit including Ctrl-C.

### Live code (`~/.local/bin/`)

```
wake_word.py             openWakeWord loop, trigger socket, local-intent dispatch
local_intent.py          Vosk grammar matching (+ .bak-cancelfix, .bak-voicefix)
local_actions.py         offline action handlers
voice_agent.py           pipecat + Gemini Live session owner
voice_engine.py          VOICE_ENGINE factory (gemini | openai_realtime)
xvf-aec-ref.sh           AEC reference loopback + register/gain reassertion
xvf_register.py          quiet CLI over vendored xvf_host (needs ~/.venvs/xvf3800)
ai-speaker-calibrate.py  check / wake / wake --music / all   (+ README-calibrate.md)
ai-speaker-status.sh     one-shot health check
wake-scores.sh           recent scores from the journal
wake-live.py             live per-second score meter, both channels, incl. <0.05
timer_alarm.py, timer_alarm_client.py, timer_tools.py
sound_fx.py, pw_node.py, pw_transport.py, session_signal.py
volume_sync.py, volume_led.py, volume-buttons.py, spotify_*.py, duck.py
```

### The interactive test that has been outstanding for a week

```bash
ai-speaker-calibrate.py wake --music
```

Needs a human to say "Hey Jarvis" over music. It is named as the settling
measurement in both `AEC-REFERENCE-GAIN-FINDING.md` and the
`ai-speaker-aec-reference-gain` memory, and has never been run since the
2026-08-24 fixes.

---

## 10. Open questions this collection could not answer

Recorded so they can be put to the user or measured next, **not** speculated on:

1. **What changed about the quiet-room case (S1)?** Last measurement said
   0.94–0.99; the user now reports it as bad. Nothing in the surviving journal
   shows a quiet-room score above 0.445.
2. **Were the 14:45 detections real or false?** Four triggers, scores
   0.106–0.255, all with Vosk hearing `''`. Only the user knows.
3. **How often do false activations happen, and during what?** Never measured.
   The 0.10 threshold's justification explicitly excludes speech-heavy program
   material.
4. **Has the device been physically moved, or the room changed?** The
   calibration README lists exactly this as a reason to re-run calibration.
5. **Why does the Pi now run at 50 °C with no fan in the device tree?**
   Unexplained change from 85 °C on 2026-08-24.
6. **Is `VOICE_INPUT_CHANNEL=0` intentional**, given wake-word and timer-alarm
   were both deliberately pinned to channel 1 as the better channel?
7. **What is in `/etc/camilladsp/active.yml`?** Not readable as `aldom`;
   needs root. Relevant because CamillaDSP sits between the AEC reference tap
   and the speaker.
8. **Real reboot test** — still never performed (`sudo` here needs an
   interactive password). Carried on this project's open list since 2026-08-23.

---

*Collected 2026-08-30. Live readings in §2, §3, §4, §6, §7 are from a single
session at ~15:00 CEST with the room quiet and nothing playing.*
