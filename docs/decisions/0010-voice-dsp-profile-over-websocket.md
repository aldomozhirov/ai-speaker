# 0010. Voice DSP profile applied over CamillaDSP's websocket, keeping the buffering

- Status: accepted (amended 2026-10-04)
- Date: 2026-08-21

## Context
Speech benefits from a different profile (higher woofer high-pass, a little
compression). The existing `smartspk-profile` needs root and restarts
CamillaDSP.

## Decision
`DspProfileSwitcher` changes the configuration over CamillaDSP's local
websocket, no sudo, no restart, saving the previous one to a runtime file so
a crash is undone. **Since 2026-10-04 it applies only the voice profile's
processing and keeps the running `devices` section.**

## Why the amendment
Loading `voice.yml` whole restarted both audio devices (a 0.1 s gap at the
start and end of every session) and ran them with 52 ms less buffering, which
moved the echo ahead of the echo canceller's reference for the whole session.
Measured: old switch, echo 54.7 ms earlier and reference 18.6 ms late; new
switch, echo does not move and 28 dB is held.

## Consequences
No passwordless sudo for a background service. The voice profile's own
`chunksize`/`target_level` are dead settings.
