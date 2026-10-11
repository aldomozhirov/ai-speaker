# 0008. Half-duplex conversation instead of voice barge-in

- Status: accepted
- Date: 2026-08-21

## Context
With the mic open while the assistant speaks, the residual echo of its own
voice (first ~500 ms of every utterance before the canceller re-adapts, and
about as loud as a person even after) reached Gemini. It interrupted and
answered itself.

## Decision
`VOICE_HALF_DUPLEX=1`: the mic is muted while the assistant speaks plus a short
tail, with a hard ceiling so a lost "stopped speaking" event cannot leave it
deaf. The wake button always interrupts.

## Consequences
- Conversations do not derail; you cannot talk over the assistant.
- Tried and reverted (2026-08-22): full duplex with the assistant at half
  volume. Echo cancellation was later found to be far from working at that
  time (2026-10-04, 0.2 dB); barge-in could be retried now that it removes
  28–29 dB, but has not been.
