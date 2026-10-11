# 0017. A verifier trained on the household's own captures

- Status: accepted
- Date: 2026-10-05

## Context
At threshold 0.10 a meeting in the room fired the wake word about 50 times in
50 minutes. False triggers scored 0.10–0.50, real ones 0.12–0.81: no
threshold separates them.

## Decision
A logistic regression on openWakeWord's embedding window, trained on
hand-sorted captures from this device (positive: real "Hey Jarvis";
negative: everything else that fired), must agree before a detection counts.

## Evidence
Offline held-out: 93 % of real kept, 0 % of known false passed; about 23 % of
unseen kinds of sound passed. Live: "seems to be better" (owner, same day), but
about six false wakes a day still got through.

## Consequences
The model encodes one voice; others may be rejected. It must be retrained
when that happens. The training data is household audio and stays out of git.
