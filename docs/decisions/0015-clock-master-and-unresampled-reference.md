# 0015. Loopback sink is PipeWire's clock master; the reference is not resampled

- Status: accepted
- Date: 2026-10-04

## Context
After a reboot every check was green but the echo canceller removed 0 dB.
The loopback sink and the XVF3800 sink had equal `priority.driver`, so boot
order decided which clocked the graph; the XVF3800 won, PipeWire resampled the
speaker path and the echo's delay slid ~2 ms every 25 s. Separately, PipeWire's
rate matching of the XVF3800 sink chased snd-aloop's uneven cycle timing and
slid the reference 2–3 ms within two seconds now and then. The canceller loses
its solution when either signal moves by a few samples.

## Decision
In `51-smart-speaker.conf`: `priority.driver = 2500` on the loopback sink
(above every capture device too); the same `clock.name` on both sinks so
PipeWire copies samples to the XVF3800 unchanged; `api.alsa.headroom = 1792`
on the XVF3800 sink for slack, which also provides the reference's lead
(replacing pw-loopback's 40 ms delay, now 0).

## Evidence
28.6–29.2 dB of pink noise removed, reference 32 ms early, echo and reference
steady to 0.004 ms over 42 s, held through a voice session (23:28). The two
devices run within ~3 ppm; PipeWire resynchronises only beyond 21 ms off
target, about two hours of unbroken playback.

## Consequences
`ai-speaker-status.sh` FAILs on the wrong clock master and WARNs on rate
matching. Realtime priority (pipewire group, same evening) was necessary but
not the cause.
