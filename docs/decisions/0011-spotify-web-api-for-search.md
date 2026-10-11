# 0011. Spotify Web API for "play X", MPRIS for transport

- Status: accepted
- Date: 2026-08-22

## Context
"Play the Beatles" needs search. MPRIS `OpenUri` exists in spotifyd, but its
MPRIS name only appears while a Spotify Connect session is live; from cold
there is no local player.

## Decision
`spotify_web.py` (Authorization Code flow, token in
`~/.config/ai-speaker/spotify.json`) searches and starts playback on this
speaker's Connect device, which survives idle because spotifyd restores its
cached login at start. Transport commands stay on `playerctl`.

## Consequences
- Needs a Spotify developer app under the same account spotifyd plays as.
- The refresh token can rotate at runtime, so it lives in a file the code
  rewrites, not in an EnvironmentFile.
- Transport from cold still fails (a Web API fallback is not built).
