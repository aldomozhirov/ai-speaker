# 0002. One volume control: the PipeWire default sink

- Status: accepted
- Date: 2026-08-18

## Context
Volume can be changed from the buttons, from the Spotify app's slider and
(later) by voice. With several gain stages they drift apart: Spotify at 30 %
with the sink at 100 % sounds the same as the reverse but the controls disagree.

## Decision
The default sink's volume is the only user volume. spotifyd runs with no
mixer (`volume_controller = "none"`), and `volume-sync.py` mirrors the sink
volume and spotifyd's MPRIS volume both ways. spotifyd 0.4.2 is patched so its
no-mixer controller remembers the logical volume instead of reporting 100 %
when playback starts (which overwrote the synchronised value).

## Consequences
- Buttons, app slider, voice and the LED ring always show one value.
- A locally patched spotifyd that has to be rebuilt from source
  (`deps/patches/`).
- Temporary per-stream changes (ducking) must be careful not to leave a stream
  at a lower level: WirePlumber's per-stream volume restore had to be turned
  off for spotifyd (2026-10-06).
