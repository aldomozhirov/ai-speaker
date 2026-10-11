# 0016. One spoken confirmation, then the action

- Status: accepted
- Date: 2026-10-04

## Context
"Play Beatles on Spotify" started the music, then the assistant said
"Playing Beatles on Spotify" over it, then said it again. The tool started
playback itself; Gemini sometimes speaks and calls the tool in the same turn,
then confirms again when the result arrives; pipecat held the result until a
3 s silence timeout.

## Decision
`session_signal.complete_quick_action(params, result, then=...)` is the one
path for successful one-shot actions: the session closes after the
confirmation and `then` runs afterwards. If Gemini already spoke before the
tool call, the result is returned without asking for another reply and the
turn is finished.

## Evidence
11 of 11 replays: one reply, then the music.

## Consequences
New action tools must use this helper. A failure after the confirmation is
only visible as the error LED and a journal line.
