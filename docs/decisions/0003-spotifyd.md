# 0003. spotifyd as the Spotify Connect receiver

- Status: accepted
- Date: 2026-08-18 (replaced raspotify, installed 2026-08-16)

## Context
The speaker should appear in the Spotify app, be controllable locally
(buttons, later voice) and play through PipeWire without opening any hardware.

## Decision
spotifyd 0.4.2 built from source with the PulseAudio backend and MPRIS on the
session bus, as a user service. raspotify was removed.

## Consequences
- MPRIS gives play/pause/next and volume to `playerctl`, the buttons and
  `volume-sync` with no Spotify-specific code.
- MPRIS exists only while a Spotify session is active, so "play X" from cold
  needs the Web API ([0011](0011-spotify-web-api-for-search.md)).
- Building takes a while on the Pi and needs a rustup toolchain (Debian's Rust
  was too old).
