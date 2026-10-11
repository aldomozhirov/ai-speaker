# 0014. Everything listens on XVF3800 channel 1, with gain for Gemini

- Status: accepted
- Date: 2026-10-04 (wake word on channel 1 since 2026-08-23)

## Context
The XVF3800's two USB channels are different outputs: channel 0 is the
automatic-gain "conference" output, channel 1 the ASR beam at a fixed, low
level. Sessions sent channel 0 to Gemini. Five recordings of a real command
were understood 0 of 10 times.

## Decision
Wake word, timer "stop" listener and Gemini all use channel 1; Gemini's input
gets +12 dB and starts with half a second of silence (a stream starting in
mid-speech got no reply at all). Result on the same recordings: 10 of 10, then
14 of 15 through the final code, and 2 of 2 on the speaker.

## Consequences
Every new listener must pin `WAKE_WORD_CHANNEL=1`: the code default is 0.
