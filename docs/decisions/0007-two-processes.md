# 0007. Wake word and conversation in two processes

- Status: accepted
- Date: 2026-08-21

## Context
The original plan had detection and conversation in one process.

## Decision
`wake-word.service` (light, always listening) and `voice-agent.service`
(pipecat, Gemini, heavy) are separate and talk over a UNIX socket. The GPIO
wake button goes through the wake-word service's own socket so both triggers
share one debounced path.

## Consequences
- Either can crash and restart without the other; the detector is never
  blocked by a slow network.
- The trigger protocol grew over time (`voice-pending`, `voice-confirm`,
  `voice-reject`, 2026-10-09) without merging the processes.
- Two venvs with independent dependencies.
