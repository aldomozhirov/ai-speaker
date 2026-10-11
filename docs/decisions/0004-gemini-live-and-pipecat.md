# 0004. Gemini Live (native audio) through pipecat, free tier

- Status: accepted
- Date: 2026-08-21

## Context
A spoken assistant needs speech recognition, a language model, speech
synthesis, interruption handling and tool calls. The project's constraint:
personal use, free tiers only, no paid subscription required.

## Decision
Gemini Live's native speech-to-speech model does all of it in one connection
on the free tier. pipecat orchestrates it, with a custom PipeWire transport.
The engine sits behind `VOICE_ENGINE` (implemented 2026-08-23) because the
model is a preview and will be retired.

## Consequences
- Low latency and natural speech; Russian and English without configuration.
- Session-shaped limits (15 min audio, ~10 min per connection, 128k context):
  context compression had to be switched on (pipecat leaves it off).
- No distinct "thinking" phase to show on the LEDs.
- Quota is shared with experiments: replaying recordings has hit 429s.
- Behaviour varies run to run (confirmation before or after a tool call,
  occasional claimed-but-not-performed actions).
