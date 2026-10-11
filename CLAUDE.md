# Working on the AI speaker

This repo is the source of truth for the speaker it is checked out on
(`~/ai-speaker` on the Pi). Read `README.md`; the docs it links describe the
system as it is.

## Rules

- Start every task with `./install.sh diff all`. If something differs, the
  device was changed outside the repo: `./install.sh capture`, review, commit
  that first, then start your change.
- Change files in the repo, never the live copies. Install with
  `./install.sh apply user` (`sudo … system` for /etc), restart what it
  names, check with `ai-speaker-status.sh`.
- Other Claude sessions may be working on this Pi. Check `ListAgents` and
  `git status`; if a peer is active, agree on who touches which file.
- Audio changes (stream opening, WirePlumber, timing) must be exercised on the
  live PipeWire graph next to other streams before the real install
  (`docs/extending.md`, "Testing audio changes"). Offline tests have passed
  code that broke on the device.
- When a command was misunderstood, replay its capture
  (`tools/session/voice-session-replay.py`) before changing code.
- Never read a write-only XVF3800 register. Never set
  `node.dont-fallback`. Never play to `hw:sndrpihifiberry`.
- `sudo` needs a password here; ask the owner to run root steps.
- Do not change `WAKE_WORD_THRESHOLD` or other tuned values unasked; their
  history is in the unit comments and the CHANGELOG.
- Do not restart spotifyd while it is the active Spotify Connect device.
- Run heavy offline work with `OMP_NUM_THREADS=1 nice -n 19`.

## Every change

- Update the doc in `docs/` that describes the changed behaviour, in the same
  commit.
- Add a CHANGELOG entry: date, what, why, evidence (measurements, the owner's
  words when they decided).
- A decision others should not casually undo gets a record in
  `docs/decisions/` and a row in its README.
- Never commit secrets, household recordings or models trained on them.
- Tag a state once the owner has confirmed it working on the speaker:
  `git tag YYYY-MM-DD-<what>`.
