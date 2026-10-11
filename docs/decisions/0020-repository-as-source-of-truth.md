# 0020. This repository is the source of truth for the device

- Status: accepted
- Date: 2026-10-11

## Context
Two months of changes lived only on the Pi: about 30 `.bak-*` copies in
`~/.local/bin` and `~/.config/systemd/user`, six stacked `staged-*` folders
with install/rollback scripts that only undid in reverse order, notes in a
working folder, and several AI sessions editing the same live files (one
install once silently dropped another session's change).

## Decision
A public git repository with every added or changed file at its real path,
an installer that renders and copies them (`diff`, `apply`, `capture`,
`bootstrap`), living documentation, decision records and a changelog.
Files are copies, not symlinks (root-owned files cannot point into a home
directory, and copies make drift visible). Device-specific values are replaced
at install time so the repo stays identical to the reference unit. Secrets,
household recordings and models trained on them stay out, with a private backup script instead.

## Consequences
- Rollback is git, not `.bak` files.
- Edits made directly on the device must be captured or they will be
  overwritten by the next `apply`.
- Git history starts at 2026-10-11 with the state of that day; the history
  before it is reconstructed in the CHANGELOG from the notes, backups and
  journals, and is not replayed as commits.
