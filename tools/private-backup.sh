#!/usr/bin/env bash
# Everything the speaker needs that must NOT go into the public repository:
# API keys, the Spotify login, the household's voice data and the models
# trained on it.
#
#   tools/private-backup.sh create [--with-captures] [OUT.tar.gz]
#   tools/private-backup.sh restore ARCHIVE.tar.gz
#
# Keep the archive somewhere private (not in this repo; .gitignore blocks it).
# The Wi-Fi profile is not included: it holds the Wi-Fi password and is
# recreated by Raspberry Pi Imager or nmcli on a new device.
set -euo pipefail

ITEMS=(
  .config/ai-speaker                       # voice-agent.env (Gemini key), spotify.json (Web API token)
  .cache/spotifyd/zeroconf                 # spotifyd's Spotify Connect login
  .cache/spotifyd/oauth
  .local/share/ai-speaker/wake-verifier    # verifier model + the sorted captures it was trained on
  .local/share/ai-speaker/timer-alarm-state.json
)

case "${1:-}" in
  create)
    shift
    extra=()
    if [ "${1:-}" = --with-captures ]; then extra=(.local/share/ai-speaker/wake-captures); shift; fi
    out=${1:-$HOME/private-backup-$(hostname)-$(date +%Y%m%d).tar.gz}
    present=()
    for i in "${ITEMS[@]}" "${extra[@]}"; do [ -e "$HOME/$i" ] && present+=("$i"); done
    umask 077
    tar -C "$HOME" -czf "$out" "${present[@]}"
    echo "wrote $out ($(du -h "$out" | cut -f1)); contains secrets, keep it private"
    ;;
  restore)
    [ -f "${2:-}" ] || { echo "usage: $0 restore ARCHIVE" >&2; exit 2; }
    tar -C "$HOME" -xzf "$2"
    chmod 700 "$HOME/.config/ai-speaker"
    chmod 600 "$HOME"/.config/ai-speaker/* 2>/dev/null || true
    echo "restored into $HOME"
    ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
