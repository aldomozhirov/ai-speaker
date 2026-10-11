# AEC reference was starved by the XVF3800's own playback volume

Session date: 2026-08-24. Follows `WAKE_WORD_SENSITIVITY_INVESTIGATION.md`,
whose Session 2 ended blocked. **Two of that document's conclusions are wrong
and are corrected below** — read this before acting on its "next step" list.

## The bug

The XVF3800's USB *playback* sink was sitting at PipeWire volume **0.40**, and
PipeWire's volume for this device **is** the hardware mixer (verified:
`wpctl set-volume <xvf> 1.0` moves `amixer -c Array PCM,0` to `0.00dB`). At
0.40 the mixer read **-23dB**.

That attenuation lands squarely on the AEC far-end reference, because the
reference is delivered *to that sink*. Measured with the debug mux, with a
-20 dBFS pink-noise stimulus playing through the normal speaker path:

| `AUDIO_MGR_OP_L` | before fix | after fix |
|---|---|---|
| `0 0` silence (control) | -65.0 dBFS | -89.9 dBFS |
| `3 0` mic 0 (control)   | -32.6 dBFS | -33.3 dBFS |
| `10 0` far end, native rate | -64.5 dBFS | -24.2 dBFS |
| `4 0` far end, post-SRC to 16k | -64.9 dBFS | -30.5 dBFS |
| `12 0` amplified far end -> SHF cores | -60.3 dBFS | -15.3 dBFS |

Before the fix every far-end tap sat at the silence floor: **the AEC's
reference input was, for practical purposes, silent.** That is consistent with
every long-standing symptom — `AEC_AECCONVERGED=1` at all times (a filter
converges trivially when its reference is silence), `AEC_RT60` reading
`1.4e-45` (a denormal; valid range is 0.25-0.9s, i.e. no room estimate was
ever formed), and residual echo louder than the user's voice.

**Why nobody noticed:** the XVF3800's analog output is deliberately left
unconnected (CamillaDSP owns the real output path). A low playback volume on
that device therefore silences nothing anyone can hear. It only starves the
echo canceller. Same failure class as `AUDIO_MGR_FAR_END_DSP_ENABLE` resetting
on power cycle: 100% silent, invisible to every check that existed.

## The fix

`xvf-aec-ref.sh` gained `assert_reference_gains()`, called on start and on the
slow defensive tick, alongside the existing `enable_far_end_dsp` retry. It sets
the XVF3800 sink to volume 1.0 and `PCM,1` (a second, independent playback
control on this card that the PipeWire sink volume does **not** touch) to 100%.
Follows the project convention: reapply on every start, never `SAVE_CONFIGURATION`.

Post-fix, with the real signal path and music playing, the amplified far end
into the SHF cores reads **-19.1 dBFS** — live, not floor.

`--delay` on the reference `pw-loopback` is now overridable via
`XVF_AEC_REF_DELAY` (default `0.341`, unchanged) so alignment can be swept.

## Corrections to the previous investigation

1. **"The debug mux may not be switching."** It switches. With a silence
   control included (which Session 2 never ran — it went straight to
   cross-correlation), category 0 reads -65/-89 dBFS against category 3's
   -33 dBFS, and the mic reading repeats to 0.1 dB. `xvf_register.py set`
   also demonstrably lands and reads back (`AEC_FAR_EXTGAIN`,
   `AUDIO_MGR_SYS_DELAY` both round-tripped). Session 2's real problem was
   that categories 4 and 5 were *both silent* — it read that as a broken
   measurement chain rather than an absent signal. The documented "re-validate
   the mux first" next step is unnecessary.

2. **`--delay=0.341` is not a measured value.** It is exactly
   `/proc/asound/Loopback/pcm0p/sub0/hw_params` `buffer_size 16384 / 48000` —
   the aloop *ring buffer size*, not the latency through it. Live ALSA
   pointers give the actual tap->speaker path as roughly 90-110ms
   (aloop `delay` 465 = 9.7ms, CamillaDSP capture 1064 = 22.2ms, HiFiBerry
   3222 = 67.1ms). So the reference has likely been ~230ms late for the life
   of the project. **This remains unconfirmed** — see below.

## What is still NOT established

**Whether any of this improves wake-word detection during music.** It has not
been demonstrated, and should not be assumed.

- Every accessible output tap has AGC on it (`PP_AGCMAXGAIN=64`), so output
  RMS cannot measure cancellation depth. Channel 1 (`7 3`, ASR output) read an
  identical +28.x dB excess in *every* condition tested — across 0-341ms of
  reference delay and across 43dB of `AEC_FAR_EXTGAIN` — because that is the
  AGC target, not the residual. Category `7 0` behaved the same way. A valid
  ERLE measurement needs a tap that is genuinely pre-AGC; that tap has not
  been identified yet.
- Consequently the reference-delay question is **still open**. The sweep found
  no effect, but the sweep's metric was confounded, so that is not evidence of
  no effect.
- The `0.964` music noise floor from the previous investigation **did not
  reproduce**. With The Smiths playing at -11.5 dBFS at the aloop sink and
  -20.2 dBFS at mic 0, openWakeWord's peak score over 25s was 0.000-0.010 on
  both channels, in both the starved and fixed reference states. Either the
  original measurement was taken under materially different conditions, or
  something else has changed since. Unexplained.

## Next step

Run `ai-speaker-calibrate.py wake --music`, which needs a human to actually
say "Hey Jarvis" over music — the one measurement that settles whether the
real complaint is fixed, and the one no agent can perform here. Compare the
music-phase separation against the previous run's 0.1x.

If it is still bad, the next lever is a *software* AEC cascaded on the
residual: `libpipewire-module-echo-cancel` (WebRTC AEC3) is already installed,
and its blind delay estimation sidesteps the unresolved alignment question
entirely. Point `WAKE_WORD_SOURCE` at the resulting virtual source to A/B it
with zero code change. Note the Pi runs at 81-84C with throttle-history bits
set, so added DSP load is not free.

## Files

- `xvf-aec-ref.sh` — patched; pre-session copy at `xvf-aec-ref.sh.bak-presweep`
- `~/claude-project/aec-delay-sweep.py` — reference-delay sweep harness
- `~/claude-project/aec-gain-sweep.py` — `AEC_FAR_EXTGAIN` sweep
- `~/claude-project/aec-refcheck.py` — far-end reference presence check (with controls)
- `~/claude-project/aec-refpath.py` — localises where the reference dies
- `~/claude-project/aec-erle-sweep.py` — ERLE attempt; **metric is AGC-confounded**
- Results: `aec-sweep-results.json`, `aec-sweep-results-fixed.json`,
  `aec-gain-results.json`, `aec-erle-results.json`

All harnesses stop `wake-word`/`voice-agent` and pause playback while running,
and restore registers, volumes, drop-ins and services on exit including Ctrl-C.

## Register note

`AEC_FAR_EXTGAIN` read `-43.0` at the start of this session and reads `0.0`
now. Nothing here intentionally left it at 0.0 and the change was not observed
directly (it is RAM-only and was written during the gain sweep, whose restore
reported putting `-43.0` back). `0.0` is the benign value — no attenuation on
the far-end reference — so it has been left alone, but if `-43.0` turns out to
be Seeed's intended tuning for this unit, that is where it came from.

---

## Follow-up (same day): the threshold was calibrated for the wrong condition

User reported no improvement after the reference-gain fix. Pulling their actual
test from the journal gave three utterances over music: peaks **0.308 (fired),
0.152 (missed), 0.443 (fired)** — against `WAKE_WORD_THRESHOLD=0.25`.

Measured false-positive floor over 2 minutes of music at a verified -11.7 dBFS
program level (1500 frames/channel): **ch0 max 0.0153, ch1 max 0.0018, zero
frames above 0.05.**

So the threshold sat ~16x above the noise floor while genuine speech over music
only reaches 0.15-0.44. It was derived from the *quiet-room* phase, where
utterances score 0.94-0.99 — but the quiet room is not the binding case.
Lowered to **0.10** (~7x the highest observed music floor, and comfortably below
the weakest observed real utterance). Backup:
`wake-word.service.bak-threshold-0.25`.

This is a workaround, not a cure: utterance scores still collapse from ~0.95
quiet to 0.15-0.44 over music, which is the signal-quality problem. Worth
trying next, in order: openWakeWord's `vad_threshold` +
`enable_speex_noise_suppression` (libspeexdsp1 is installed), and a
`custom_verifier_model` trained on the user's own voice — the documented remedy
for exactly this weak-utterance case.

**Caveat on the floor measurement:** 2 minutes, one genre, one volume.
Speech-heavy program material (podcasts, rap) is the plausible false-positive
risk and was not tested. If false wakes appear, raise toward 0.15 before
reverting to 0.25.

## Observability gap (this is why this took so long)

`journalctl --user -u wake-word.service` returns **"No journal files were
found"** on this box even with `XDG_RUNTIME_DIR` and
`DBUS_SESSION_BUS_ADDRESS` exported — there is no per-user journal. The user
services log to the *system* journal, reachable only as:

    journalctl _SYSTEMD_USER_UNIT=wake-word.service

Every doc in this project instructs the `--user -u` form, including
`WAKE_WORD_SENSITIVITY_INVESTIGATION.md`'s explicit note about exporting those
two variables. That note is wrong about the outcome: the variables are
necessary for `systemctl --user`, but they do not make `journalctl --user`
work here. `WAKE_WORD_DEBUG=1` has been on the whole time, writing per-frame
scores nobody could read. Helper added: `~/.local/bin/wake-scores.sh`.
