# 0013. Offline command matching switched off

- Status: accepted
- Date: 2026-10-04 (feature built 2026-08-23, enabled 2026-08-30)

## Context
`local_intent.py` matches simple commands offline with a Vosk grammar to skip
Gemini for "pause" or "set a timer". The journal of 20 Sep – 3 Oct showed:
the wake-word loop blocked 1–6 s while it listened, frames it never saw fired
the wake word again (93 triggers from 41 utterances, up to ten per "Hey
Jarvis"), every session started ~4 s late, that audio never reached Gemini,
and the English grammar matched 0 of 93 commands, most of which were Russian.

## Decision
`LOCAL_INTENT_ENABLED=0`. Timers, alarms, music and volume go through
Gemini's tools; the detection loop was rewritten so one utterance gives one
detection whatever this setting is.

## Consequences
Everything depends on the network and Gemini. The code stays for a possible
multilingual, non-blocking version.
