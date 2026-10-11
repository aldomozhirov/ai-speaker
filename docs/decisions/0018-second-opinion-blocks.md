# 0018. A second detector must confirm before a session opens; risky tools are guarded

- Status: accepted
- Date: 2026-10-08 (log only), 2026-10-09 (blocking)

## Context
After the verifier, false wakes still happened several times a day, and some
opened sessions in which the model set timers on noise. The owner asked for
"a more foundational approach".

## Decision
1. Benchmark other engines on the device's own captures
   ([0005](0005-openwakeword.md)). None was a better primary detector; Vosk
   (grammar "hey jarvis") or microWakeWord at 0.5 together kept 34 of 42 real
   and passed 0 of 62 false.
2. Run that pair as a second opinion on every trigger, first log-only for a
   day (10 of 10 false would have been blocked, 2 of 2 real passed), then
   blocking. The agent opens the mic immediately on "pending" so blocking
   costs no audio, and proceeds after 1.5 s without a verdict.
3. Journal every tool call and refuse timer/alarm tools when nothing
   intelligible was heard.

## Consequences
About one real wake-up in five is expected to be blocked; that is the
trade-off chosen. Softer setting: `WAKE_WORD_SECOND_OPINION=log`.
