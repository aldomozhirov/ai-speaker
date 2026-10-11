# 0009. Duck music per stream, for the whole session

- Status: accepted
- Date: 2026-08-22

## Decision
During a voice session, music streams are lowered to 0.20 with a 200 ms ramp
and restored at the end: per stream (never the default sink, never
CamillaDSP's volume), for the whole session rather than only while the
assistant speaks, and never touching passive nodes (the echo-canceller
reference).

## Why
Per-utterance ducking pumps the music and leaves it loud while the user
speaks, which trips the session's voice-activity detector. The default sink
is the user's volume ([0002](0002-one-volume-control.md)); CamillaDSP's volume
is tweeter headroom.

## Consequences
- Restore must be robust: done in `finally`, recovered after a crash, skipped
  for streams whose volume moved. WirePlumber's stream-volume memory once
  preserved a ducked level for every later stream (fixed 2026-10-06).
- Ducking does not help barge-in: the echo that breaks full duplex is the
  assistant's own voice.
