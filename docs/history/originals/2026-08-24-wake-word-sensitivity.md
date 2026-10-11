# Wake-Word / Mic Sensitivity Investigation — In Progress

Session handoff notes. The device is being physically disconnected and moved
to another room mid-investigation, which may kill this Claude session. This
file has everything needed to resume cold: what was found, what was already
changed, and the exact next step.

## Original complaint

Wake word only works from very close to the mic, unstable, sometimes doesn't
trigger at all. User confirmed (when asked) this happens even in a silent
room, not just with music — that's what pointed the investigation away from
just re-tuning the threshold and toward the signal path itself.

## What was already done

1. **`ai-speaker-calibrate.py check`** — passed clean. `AUDIO_MGR_FAR_END_DSP_ENABLE=1`,
   `AEC_AECCONVERGED=1`, AEC reference routing correct, all 6 services active,
   lingering enabled, no crash-loop history in `journalctl --user -u wake-word.service`
   over the last 7 days.

   Note: running `systemctl --user` / `journalctl --user` in a fresh shell on
   this box requires `export XDG_RUNTIME_DIR=/run/user/$(id -u)` (and
   `DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus` for `systemctl`)
   — a non-login shell doesn't have these set, and the commands fail
   silently/confusingly without them (`journalctl` reports "No journal files
   were found" rather than an auth error).

2. **User ran `ai-speaker-calibrate.py wake --music` interactively.** Results:

   **Quiet room** — excellent, both channels:
   - ch0: noise floor 0.000, utterances 0.996/0.961/0.953/0.943 (1/5 missed
     entirely, i.e. scored 0.000 — not a threshold issue, the word just
     wasn't recognized in the signal that one time), separation 943x
   - ch1: noise floor 0.001, utterances 0.995/0.987/0.966/0.978 (1/5 missed),
     separation 966x
   - Recommendation: threshold 0.25, channel 1

   **With music playing** — broken:
   - ch0: noise floor 0.722 (!), zero utterances detected at all (5/5 missed)
   - ch1: noise floor 0.964 (near openWakeWord's max score, from music alone,
     before any speech), utterances mostly undetected, one weak 0.065 hit —
     separation 0.1x
   - `ai-speaker-calibrate.py`'s own `compare()` step, which would print an
     `AEC_AECCONVERGED=0` warning if that were the cause, printed nothing —
     meaning the register still reported converged even while this was
     happening. Convergence is not proof of usable cancellation depth.

   **Conclusion**: the reported symptom is not really "unstable in silence,"
   it's "silence works great, music/program audio breaks it completely" —
   consistent with this project's own prior, never-fully-resolved notes
   (`AI-SPEAKER-SETUP-NOTES.md`, `WAKE_WORD_GEMINI_INTEGRATION.md`) about AEC
   topping out around -35dB with an unmeasured reference delay.

3. **Applied the safe, quiet-room-based fix** (pre-approved by user: "apply
   automatically if clean"):
   - Edited `~/.config/systemd/user/wake-word.service`:
     `WAKE_WORD_CHANNEL=0→1`, `WAKE_WORD_THRESHOLD=0.35→0.25`
   - Original backed up to `~/.config/systemd/user/wake-word.service.bak`
   - `daemon-reload` + `restart wake-word.service` — confirmed active,
     `check` still passes clean afterward.
   - This does **not** address the music-related failure — that needs the
     work below.

## Root cause hypothesis (not yet confirmed) — AEC reference delay

Found via the vendored XVF3800 host-control docs
(`~/src/reSpeaker_XVF3800_USB_4MIC_ARRAY/host_control/README.md`) plus a web
search of the XMOS `fwk_xvf` tuning guide (direct WebFetch of
`xmos.com/documentation/.../04_tuning_the_application.html` returned HTTP 406
— use WebSearch instead, it surfaced the content fine):

- `AUDIO_MGR_SYS_DELAY` (int32, currently reads **12**) is "Delay, measured
  in samples [at 16kHz], applied to the reference signal before passing to
  the SHF [AEC] algorithm." Seeed's factory default is also 12.
- XMOS's own tuning guidance: **the delay between reference and mic signal
  should end up at or below ~40 samples (2.5ms at 16kHz) after this
  correction is applied** for good AEC performance. This register exists
  specifically to compensate "excessive internal delay" between when audio
  is sent to the speaker and when it acoustically arrives back at the mic.
- This project's own docs already flagged the *pw-loopback* `--delay=0.341`
  (341ms) figure in `xvf-aec-ref.sh` as "a theoretical estimate, never
  empirically confirmed by correlation" and called measuring it "the
  most-cited remaining lever." That pw-loopback flag is a different thing
  (client-side buffering on the reference feed, not the XVF3800's own
  alignment register) but the underlying problem is the same: nobody has
  ever actually measured the true reference↔mic delay on this box.
  `AUDIO_MGR_SYS_DELAY=12` (0.75ms) is almost certainly a large
  under-correction if the real pipeline delay (aloop sink → CamillaDSP →
  USB DAC → speaker → room → mic → USB capture) is anywhere close to that
  341ms figure — which would fully explain music-only wake-word failure:
  reference and mic are far too misaligned for the AEC's adaptive filter to
  usefully cancel anything, even though it reports "converged."

## The tool that makes measuring this possible

`AUDIO_MGR_OP_L` / `AUDIO_MGR_OP_R` (currently `8 0` / `7 3` — i.e. **neither
channel right now is raw mic or raw reference**, both are already-processed
outputs; `8 0` = auto-select beamformed voice, `7 3` = ASR output of
auto-select beam. This is *why* wake_word.py's own comment says channels 0/1
"are NOT a plain stereo pair" — confirmed, they're two different processed
outputs, not stereo mic pickup).

This register is a full debug mux — any of ~13 internal signal categories can
be routed to either USB output channel (see the category table in
`host_control/README.md`, categories 0-12). Relevant ones for delay
measurement:
- Category 3 or 1, source 0 = mic channel 0 (amplified / raw)
- Category 4, source 0 = far-end reference **before** system delay is applied
  (what we want to correlate against, to measure the true uncorrected offset)
- Category 5, source 0 = far-end reference **after** system delay — useful
  afterward, to confirm the correction actually landed the residual delay
  under ~40 samples

Register writes are **RAM-only unless `SAVE_CONFIGURATION` is explicitly
called** (confirmed: it's in `xvf_host.py`'s register map as a write-only
"save current config to flash" command). This matches why
`AUDIO_MGR_FAR_END_DSP_ENABLE` resets on every power cycle — nothing in this
project's setup ever calls `SAVE_CONFIGURATION`. **Follow that same pattern
for `AUDIO_MGR_SYS_DELAY`**: don't flash-save it, instead add it to
`xvf-aec-ref.sh`'s existing reapply-on-start logic (right next to the
`enable_far_end_dsp` retry loop) so the systemd unit file / shell script
stays the single source of truth, not opaque chip flash state.

## Session 2 (2026-08-23) — delay measurement attempted, BLOCKED

Device had been reconnected and was already settled (both services active
23+ min) when this session picked up. Ran the plan's steps 1-6 as written,
plus extra validation once the first result looked suspicious. Full detail
below; short version: **the debug-mux delay measurement does not currently
produce a trustworthy number, and the likely reason is that the mux itself
may not be switching sources the way the vendor docs describe** — this needs
to be confirmed/refuted before anyone tries to measure `AUDIO_MGR_SYS_DELAY`
again. Nothing was written to `xvf-aec-ref.sh`; all registers and services
were restored to the known-good baseline at the end of the session.

**What was done:**

1. `check` passed clean after reconnect (`AEC_AECCONVERGED=0` warning was
   expected/benign — nothing had played yet). `AUDIO_MGR_OP_L`/`OP_R`
   confirmed still `8 0` / `7 3` (RAM state survived the replug).
2. Stopped both services, set `OP_L=3 0` (mic ch0, category 3) /
   `OP_R=4 0` (far-end ref pre-delay, category 4), per the plan.
3. **First attempt failed silently**: used
   `pw-play --raw ... /tmp/noise.raw` (file as argument). This exits
   instantly (~0s) doing nothing — `--raw` is only honored when the PCM is
   read from **stdin** (`cat file | pw-play --raw ... -`). The first
   "capture" was mostly silence on the reference channel as a result. Not a
   hardware issue, just a `pw-play` CLI gotcha — worth remembering if anyone
   else scripts against it.
4. Fixed the invocation, regenerated white noise (chosen over music because
   its autocorrelation is impulse-like — sharp, unambiguous peak, no
   periodicity aliasing), and captured mic (category 3) vs reference
   (category 4) at three different signal levels (weak → loud → moderate),
   using both plain cross-correlation and GCC-PHAT (band-limited
   200-3400Hz, standard technique for acoustic delay/TDOA estimation, much
   more robust to reverb/bandlimiting than raw correlation).
5. **Built in a sanity check**: category 4 (ref, no delay) vs category 5
   (ref, *with* the chip's own 12-sample delay already applied) should, if
   the measurement pipeline is valid, show lag values differing by exactly
   12 samples — that's a known, deterministic, register-driven fact, not an
   acoustic unknown. **This check failed at every signal level tried**: cat4
   and cat5 always produced the identical best lag (0), never off by ~12.
6. Went further and did a **pure DSP self-test with zero acoustic
   ambiguity**: routed the *same* physical mic (index 0) to both channels
   simultaneously — `OP_L=3 0` (mic0 with delay) vs `OP_R=11 0` (mic0
   before delay). Per the vendor category table these should be identical
   waveforms save for a small time shift (near-equal RMS). Instead: **RMS
   differed by ~30x (1566 vs 52) and the correlation peak sat at lag=0**,
   not offset by 12 samples. That amplitude mismatch has no explanation
   under the documented category semantics.

**Conclusion**: four independent attempts (2 correlation methods x weak/
loud/moderate signal, plus a no-acoustics self-test) all produced
internally-contradictory results and never showed the one signature that
*must* be present if the measurement chain were sound — the register-known
12-sample offset between categories 4/5 (or equivalently 3/11). The most
likely explanation is that `AUDIO_MGR_OP_L`/`AUDIO_MGR_OP_R` writes via
`xvf_register.py set` are not actually changing what's live on the USB
debug output the way the README describes (possibly a settle-time issue,
a source-index quirk specific to this firmware, or a caching behavior) —
but that's a hypothesis, not confirmed. Per the original plan's own
built-in escape hatch: *"if it's still far off, something in the
measurement itself is wrong ... and needs re-examining, not just
re-guessing the number."* Nothing was written into `AUDIO_MGR_SYS_DELAY`
or `xvf-aec-ref.sh` as a result — an unvalidated guess there risks making
wake-word-during-music worse, not better.

**State at end of session** (restored, verified clean):
- `AUDIO_MGR_OP_L` = `8 0`, `AUDIO_MGR_OP_R` = `7 3` (back to baseline)
- `AUDIO_MGR_SYS_DELAY` untouched, still `12`
- Sink volume (`Loopback Analog Stereo`, id 36) restored to `0.30`
- `wake-word.service` and `voice-agent.service` restarted, both active
- `ai-speaker-calibrate.py check` passes clean
- All scratch files in `/tmp` (noise WAVs, captures, pw-play logs) removed

**Persistence check (does any of this survive a reboot?)** — verified live,
not assumed:
- `AUDIO_MGR_OP_L`/`OP_R`/`SYS_DELAY` are XVF3800 chip **RAM** registers,
  never flash-saved (project convention: `SAVE_CONFIGURATION` is never
  called). Only `AUDIO_MGR_FAR_END_DSP_ENABLE` has a reapply-on-boot retry
  loop (in `xvf-aec-ref.service`) — `OP_L`/`OP_R` have **no such loop**.
  They empirically survived today's real physical disconnect/move/reconnect
  (confirmed still `8 0`/`7 3` after replug), but that's the chip happening
  to retain RAM state, not a guaranteed self-healing mechanism the way
  `FAR_END_DSP_ENABLE` is. If the chip ever does lose that RAM state
  (deeper power loss than a simple USB replug), `OP_L`/`OP_R` could come
  back at factory defaults and nothing would currently restore the `7 3`
  right-channel routing automatically — worth adding to `xvf-aec-ref.sh`'s
  reapply-on-start block at the same time `SYS_DELAY` eventually is (see
  next-step item 5 below).
- Sink volume (`Loopback Analog Stereo`) is **not a static setting** —
  `volume-sync.service` continuously re-syncs it against another source of
  truth (physical volume buttons / MPRIS). Confirmed live: it was manually
  set back to `0.30` at end of session but was already reading `0.80` again
  minutes later, on its own. Not a regression, just how that service works
  — irrelevant to reboot behavior since it self-manages either way.
- The one genuinely **permanent, disk-based** change from Session 1 is
  `wake-word.service`'s `WAKE_WORD_CHANNEL=1` / `WAKE_WORD_THRESHOLD=0.25`
  — a systemd unit file edit, confirmed still in place, survives reboot
  normally (pre-session values backed up in `wake-word.service.bak`).

## SUPERSEDED (2026-08-24) — see `AEC-REFERENCE-GAIN-FINDING.md`

Two conclusions below are now known to be wrong:

- **The debug mux does switch.** Session 2 inferred it might not, because
  categories 4 and 5 always read identical. They were both *silent* — the
  far-end reference was absent, not the mux broken. A silence control
  (category 0) distinguishes these and was never run. `xvf_register.py set`
  also round-trips correctly.
- **`--delay=0.341` was never a measurement.** It is the aloop ring buffer
  size (`16384/48000`), not the latency through it, which live ALSA pointers
  put at ~90-110ms.

Root cause found: the XVF3800's own playback volume sat at 0.40 (-23dB),
starving the AEC far-end reference to its noise floor. Fixed in
`xvf-aec-ref.sh`. Steps 1 and 2 below are unnecessary; step 6 (the spoken
`wake --music` test) is still the outstanding one.

## Exact next step (revised — the delay measurement itself needs re-validating first)

**Before attempting the cross-correlation measurement again**, confirm the
debug mux is actually switching live, with a dead-simple test that doesn't
depend on any statistics:

1. Set `AUDIO_MGR_OP_L` = `0 0` (category 0 = silence) and capture a couple
   seconds of the USB debug stream — channel 0 (left) should read as
   near-zero/flat. Then set it to `3 0` (mic) and capture again — it should
   now show real signal (room noise, RMS clearly nonzero). If left stays
   flat regardless of which category is set, the mux isn't switching and
   `xvf_register.py set` (or the register write path generally) needs
   investigating before anything else — check whether these mux registers
   need a settle delay, a second confirming read, or behave differently
   under this firmware version than the README documents.
2. If that basic switch test passes, redo the category-3-vs-11
   (same-mic, with-delay-vs-without) self-test from this session and
   confirm it now *does* show the expected ~12-sample shift with matching
   RMS. Don't proceed to the acoustic reference-vs-mic measurement until
   this internal check passes — it's the only ground-truth check available
   that doesn't depend on room acoustics.
3. Only once step 2 passes: repeat the original plan — `OP_L=3 0` /
   `OP_R=4 0`, play noise or music, capture, cross-correlate mic vs
   reference to get the candidate `AUDIO_MGR_SYS_DELAY`. Use GCC-PHAT
   (band-limited ~200-3400Hz) rather than plain correlation — it was
   noticeably more robust in this session's tests. Keep playback volume
   moderate (this session saw correlation quality *drop* at max volume,
   consistent with clipping/distortion — sink volume ~0.5-0.6 with
   noise amplitude well under full-scale worked better than either extreme).
4. Restore `AUDIO_MGR_OP_L`/`AUDIO_MGR_OP_R` to `8 0` / `7 3`, restart both
   services.
5. Add the measured delay to `xvf-aec-ref.sh`'s reapply-on-start block
   (pattern-match the existing `enable_far_end_dsp` retry loop), matching
   the "reapply every start, never flash-save" convention.
6. Re-run `ai-speaker-calibrate.py wake --music` (interactive, needs the
   user) to confirm the music-phase separation actually improved before
   calling this done.

## Key file/register reference

- `~/.config/systemd/user/wake-word.service` — live tuning env vars, `.bak`
  has the pre-session originals (`WAKE_WORD_THRESHOLD=0.35`,
  `WAKE_WORD_CHANNEL=0`)
- `~/.local/bin/ai-speaker-calibrate.py` / `~/.local/bin/README-calibrate.md`
  — the diagnostic/calibration tool, already used extensively above
- `~/.local/bin/xvf_register.py` — quiet CLI over vendored register library
  (`get`/`set`/`ensure`), must run under `~/.venvs/xvf3800/bin/python`
- `~/.local/bin/xvf-aec-ref.sh` — AEC reference routing + register
  reapply-on-start logic; this is where the `AUDIO_MGR_SYS_DELAY` fix should
  land once measured
- `~/src/reSpeaker_XVF3800_USB_4MIC_ARRAY/host_control/README.md` — vendor
  register/category docs (third-party, not ours to modify)
- Current known-good register baseline (captured this session, before any
  further changes): `AUDIO_MGR_MIC_GAIN=90.0`, `AUDIO_MGR_REF_GAIN=8.0`,
  `AUDIO_MGR_SYS_DELAY=12`, `AUDIO_MGR_OP_L=8 0`, `AUDIO_MGR_OP_R=7 3`,
  `AUDIO_MGR_FAR_END_DSP_ENABLE=1`, `AEC_AECCONVERGED=1`
