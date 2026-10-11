# Audio pipeline

## Signal path

```
spotifyd ──┐  (PulseAudio protocol)
voice-agent┼──▶ PipeWire ──▶ "Loopback Analog Stereo" sink ──▶ snd-aloop ──▶ CamillaDSP ──▶ hw:sndrpihifiberry
chimes ────┘    (mixer,       alsa_output.platform-snd_aloop    hw:Loopback,   (crossover,     (Amp4: L woofer,
alarms          volume)       .0.analog-stereo, default sink     1,0 capture    protection)      R tweeter)
                                     │
                                     └─ monitor ──▶ pw-loopback (xvf-aec-ref) ──▶ XVF3800 playback, left channel
                                                                                  = echo-canceller reference
```

Every source plays into one PipeWire sink: the playback side of the ALSA
loopback. CamillaDSP records the other side and is the only process that opens
the amplifier. All of it runs at 48 kHz, S32_LE, stereo.

Configuration involved:

| File | Sets |
|---|---|
| `/etc/modprobe.d/snd-aloop.conf`, `/etc/modules-load.d/snd-aloop.conf` | Load snd-aloop as card 0, 8 substreams |
| `~/.config/wireplumber/wireplumber.conf.d/51-smart-speaker.conf` | Loopback sink format, no mmap (mmap capture was silent), large periods, never suspends, is the preferred default sink (`priority.session`), is the **clock master**; XVF3800 sink shares its clock and gets extra headroom (see below) |
| `/etc/camilladsp/profiles/*.yml`, `/etc/camilladsp/active.yml` | DSP profiles; `active.yml` points at the one loaded at start |
| `/etc/systemd/system/camilladsp.service` | Starts CamillaDSP at −7 dB, control websocket on 127.0.0.1:1234 only |
| `/etc/asound.conf` | Legacy ALSA route (`smartspk` dmix into the loopback) from before PipeWire took over mixing; kept so plain ALSA programs still land in the loopback |

## CamillaDSP profiles

All profiles share the same routing and protection: stereo is summed to mono
(−6 dB per side so the sum cannot clip), −5 dB fixed headroom, Linkwitz-Riley
4th-order crossover at 2.8 kHz, a woofer high-pass, −3.5 dB tweeter trim and a
−1 dBFS clip guard.

| Profile | Use | Differences |
|---|---|---|
| `music` | Normal playback (`active.yml`) | +4 dB low shelf at 150 Hz, woofer high-pass 80 Hz; chunk 1024, target level 3072 |
| `voice` | Applied during voice sessions | Woofer high-pass 105 Hz, mild compressor ("voice_leveler") for TTS level swings, no bass shelf; its own buffer settings (512/1024) are **not** used, see below |
| `measure-woofer`, `measure-tweeter` | Measuring one driver | The other driver muted |
| `measure-sum-normal`, `measure-sum-inverted` | Measuring the crossover sum | Tweeter polarity normal / inverted |

Switching:

- **Permanently** (root): `sudo smartspk-profile music` checks the file,
  points `active.yml` at it and restarts CamillaDSP.
- **In the web UI**: `http://<pi-address>:5005/` (CamillaGUI). Shortcut
  "Bass boost" changes the music shelf within 0–5 dB.
- **During a voice session** (`DspProfileSwitcher` in `voice_agent.py`):
  over the websocket, no root, no restart. Only the voice profile's
  *processing* (filters, mixers, processors, pipeline) is applied; the running
  `devices` section is kept. Loading `voice.yml` as a whole used to restart
  both audio devices and run them with 52 ms less buffering, which put the
  echo ahead of the echo-canceller's reference for the whole session. The
  previous configuration is saved in `$XDG_RUNTIME_DIR/voice-agent-state.json`
  so a crash mid-session is undone when the agent restarts.

The profiles here are the ones from the original DSP bundle (2026-08-15) with
the corrections made on 2026-08-16: capture from `hw:Loopback,1,0` (the
cross-connected side), chunk size 1024, target level 3072, adjust period 5,
and the bass shelf.

## Volume

There is exactly one volume control: the PipeWire default sink (the loopback
sink).

- Buttons and the `set_volume`/`adjust_volume` voice tools change it with
  `wpctl`.
- `volume-sync` mirrors it both ways with spotifyd's MPRIS volume, so the
  slider in the Spotify app follows the buttons and vice versa (250 ms poll).
- spotifyd runs with `volume_controller = "none"` so it never attenuates.
  Upstream spotifyd's "no mixer" then always reports 100 % and overwrote the
  synchronised value when playback started; the local patch
  `deps/patches/spotifyd-0.4.2-no-mixer-volume.patch` makes it remember the
  value without applying it.
- CamillaDSP's own volume (−7 dB at start) is safety headroom for the tweeter,
  not a user control. Nothing changes it at runtime.
- The XVF3800's playback volume must stay at 1.0: it is the chip's hardware
  mixer for the echo-canceller reference (see below). `xvf-aec-ref.sh` enforces
  it.

## Ducking

While a voice session is open, every playing stream whose application or
binary matches `VOICE_DUCK_MATCH` (`spotifyd|librespot`) is lowered to
`VOICE_DUCK_LEVEL` (0.20) with a 200 ms ramp, and restored at the end.

- Whole session, not only while the assistant speaks: unducked music during
  the listening half trips the session's voice-activity detector.
- Per stream only. Never the default sink (that is the user's volume) and
  never CamillaDSP's volume (tweeter headroom).
- Passive nodes are skipped, so the echo-canceller loopback can never be
  ducked.
- Streams are looked up fresh each time (`pw-dump`); spotifyd's stream is
  recreated on every track change. A stream whose volume moved during the
  session is left alone on restore.
- `spotifyd.service` sets `PULSE_PROP=state.restore-props=false`. Without it,
  a stream that ended while ducked was remembered by WirePlumber at 0.20 and
  every later Spotify stream started 42 dB down (2026-10-06; PipeWire
  stream volumes are cubic, 0.20³ ≈ −42 dB).

## Echo cancellation

The XVF3800 removes the speaker's own sound from the microphones, which is
what lets the wake word work over music. It can only cancel what it is told
the speaker is playing, so `xvf-aec-ref` copies the loopback sink's monitor
(the program audio before CamillaDSP) into the left channel of the XVF3800's
USB playback input.

The canceller adapts over about ten seconds and loses its solution when the
reference or the echo shifts by a few samples. It needs **all eight** of these
conditions. Each one has failed silently at some point, with every service
active and nothing cancelled:

| # | Condition | Enforced by | Checked by |
|---|---|---|---|
| 1 | The reference reaches the XVF3800's playback input (not the default sink) | `xvf-aec-ref.sh` re-links every 1 s for the first minute, then every 30 s | `ai-speaker-status.sh` |
| 2 | `AUDIO_MGR_FAR_END_DSP_ENABLE` = 1 (chip RAM, resets on re-enumeration) | `xvf-aec-ref.sh`: up to 30 tries at start, re-checked every ~5 min | `ai-speaker-status.sh` |
| 3 | The reference is not attenuated: the XVF3800 sink volume is 1.0 | `xvf-aec-ref.sh` (`assert_reference_gains`) | `wpctl get-volume` on that sink |
| 4 | The reference arrives a little **before** the echo (20–36 ms early works; late cancels nothing; 67 ms early converges very slowly) | `api.alsa.headroom = 1792` on the XVF3800 sink (51-smart-speaker.conf) + `XVF_AEC_REF_DELAY=0` (xvf-aec-ref.service); the two add up | `tools/aec/aec-ref-timing-probe.py lead` (silent), `tools/aec/aec-align-check.py` (audible) |
| 5 | The reference is not resampled: the XVF3800 sink is not rate-matched | the same `clock.name` on both sinks (51-smart-speaker.conf) | `ai-speaker-status.sh` (WARN) |
| 6 | The echo is not resampled: the loopback sink is the graph's clock master | `priority.driver = 2500` on the loopback sink (51-smart-speaker.conf) | `ai-speaker-status.sh` (FAIL), `pw-top` |
| 7 | Speaker-path buffering does not change during a voice session | `DspProfileSwitcher` keeps the running `devices` section | `tools/aec/dsp-switch-check.py` |
| 8 | Audio threads run with realtime priority | user in the `pipewire` group (`/etc/security/limits.d/25-pw-rlimits.conf` ships with PipeWire) | `ps -eLo cls,rtprio,comm \| grep data-loop` |

With all eight in place it removes 28–29 dB of pink noise (measured
2026-10-04, reference 32 ms early, steady to 0.004 ms over 42 s). With the clock
master wrong it removed 0 dB.

**Change any of these together with a measurement.** The comments in
`51-smart-speaker.conf` and `xvf-aec-ref.service` carry the numbers behind
each value. After a reboot or a WirePlumber restart, run
`ai-speaker-status.sh` before trusting anything audio-related.

What the canceller cannot do: residual echo of the assistant's own voice is
still louder than a person's voice at the mic, which is why sessions are
half-duplex ([voice-agent.md](voice-agent.md)). Each time music starts after a
pause the XVF3800 sink reopens, the reference shifts by about a millisecond,
and the canceller needs 10–14 s to converge again.

## Microphone channels

The XVF3800 delivers two processed channels, and they are different signals,
not a stereo pair:

- channel 0: the "conference" output, with automatic gain control;
- channel 1: the ASR beam, fixed gain, quieter (speech at about −36 to −42 dBFS).

Everything that listens uses **channel 1**: the wake word, the timer's "stop"
listener, and the Gemini session (with +12 dB). On channel 0, five recordings
of a real command were understood 0 times in 10; on channel 1 with gain, 10 in
10 (2026-10-04).
