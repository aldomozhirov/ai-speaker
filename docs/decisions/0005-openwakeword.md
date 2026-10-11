# 0005. openWakeWord "hey_jarvis" as the wake word

- Status: accepted (re-examined 2026-10-08, kept)
- Date: 2026-08-21

## Context
A local, always-on wake word is needed. Picovoice Porcupine had no
personal/non-commercial plan (and its free tier ended entirely 2026-06-30).

## Decision
openWakeWord (Apache-2.0) with the pretrained `hey_jarvis` model, running on
the XVF3800's processed capture in its own service.

## Consequences
- Free, local, runs comfortably on the Pi 5.
- The pretrained model scores this household's voice low (0.12–0.5) and fires
  on ordinary speech at similar scores. That led to a low threshold plus two
  filters ([0017](0017-household-verifier.md), [0018](0018-second-opinion-blocks.md)).
- 2026-10-08 benchmark on 485 real captures: Porcupine (keyless 1.9.5),
  microWakeWord, sherpa-onnx and Vosk all heard fewer real wake-ups as primary
  detectors. Next step if needed: a custom openWakeWord model for this voice.
