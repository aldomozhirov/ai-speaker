#!/usr/bin/env bash
# Show recent wake-word scores.
#
# NB: `journalctl --user -u wake-word.service` returns "No journal files were
# found" on this box even with XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS set --
# there is no per-user journal here. The user units log into the *system*
# journal instead, reachable only via the _SYSTEMD_USER_UNIT= match. Every doc
# in this project that says `journalctl --user -u ...` is wrong for that reason.
#
#   wake-scores.sh            # last 15 min
#   wake-scores.sh -30 min    # custom window
#   wake-scores.sh -f         # follow live
SINCE="${1:--15} ${2:-min}"
if [ "$1" = "-f" ]; then
  exec journalctl _SYSTEMD_USER_UNIT=wake-word.service -f -o cat
fi
journalctl _SYSTEMD_USER_UNIT=wake-word.service --since "$SINCE" -o short-iso --no-pager 2>/dev/null \
  | grep -E "raw_score|relay|Relaying" \
  | sed -E 's/^([0-9-]+)T([0-9:]+)[^ ]* [^ ]+ [^:]+: /\2  /'
