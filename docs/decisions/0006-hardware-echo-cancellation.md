# 0006. Hardware echo cancellation on the XVF3800, with a supervised reference feed

- Status: accepted
- Date: 2026-08-21

## Context
The wake word must work while music plays. The XVF3800 cancels echo on-chip
if it receives a copy of what the speaker plays ("far-end reference") on its
USB playback input. CamillaDSP owns the real output, so the copy has to come
from PipeWire.

## Decision
`xvf-aec-ref.sh` runs `pw-loopback` from the loopback sink's monitor into the
XVF3800's playback input (left channel) and **supervises the link** instead of
predicting when WirePlumber is ready: every second for a minute, then every 30 s,
it removes the known wrong link and makes the right one. It also re-applies
the chip's `AUDIO_MGR_FAR_END_DSP_ENABLE` and the reference gain at every
start and periodically.

## Consequences
- Robust across boots and USB replugs.
- Three approaches failed first, each silently: a static module-loopback in
  `pipewire.conf.d`, a plain `pw-loopback` relying on late binding, and
  waiting for the node to exist. Once WirePlumber links a stream wrongly it
  never revisits it.
- "Routed" turned out to be one condition of eight
  ([audio-pipeline.md](../audio-pipeline.md#echo-cancellation)); the others
  were found over the following six weeks.
