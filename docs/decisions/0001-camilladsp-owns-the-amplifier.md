# 0001. CamillaDSP is the only owner of the amplifier

- Status: accepted
- Date: 2026-08-15

## Context
The speaker is a two-way active design: the HiFiBerry Amp4's left channel
drives the woofer, the right channel the tweeter. Any program that played
straight to the card would send full-range audio to the tweeter. Several
programs need to play at once (music, the assistant, chimes).

## Decision
All audio goes into a virtual cable (snd-aloop). PipeWire mixes into its
playback side; CamillaDSP reads the capture side, applies mono summing,
crossover, protection filters and a clip guard, and is the only process that
opens `hw:sndrpihifiberry`. Every DSP profile, including the voice and
measurement profiles, carries the full protection.

## Consequences
- Driver safety does not depend on any application behaving.
- One place for tuning (CamillaGUI on port 5005).
- Extra latency (CamillaDSP buffering) and an extra clock domain; both later
  mattered for echo cancellation ([0015](0015-clock-master-and-unresampled-reference.md)).
- CamillaDSP's volume is fixed headroom, never a user control.

## Alternatives
ALSA dmix with a plugin chain (the first `asound.conf`, still present for
plain ALSA programs): no per-stream control, fragile with PipeWire. A DSP HAT:
more hardware for something the Pi 5 does easily.
