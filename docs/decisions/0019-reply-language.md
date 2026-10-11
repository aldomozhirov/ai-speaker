# 0019. Replies in English unless the request was certainly Russian

- Status: accepted
- Date: 2026-10-04

## Context
Most commands are spoken in Russian, some in English; short far-field Russian
fragments were sometimes transcribed as Korean or nonsense and answered in odd
languages.

## Decision
The owner's choice: the system prompt says to reply in English by default and
in Russian only when certain the request was Russian. A softer "if unsure,
English" wording was tried first and did not work.
