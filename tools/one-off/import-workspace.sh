#!/usr/bin/env bash
# One-off migration (2026-10-11): bring the measurement tools, tests and the
# original working notes from the old ad-hoc workspace (~/claude-project and a
# few folders in ~) into this repository. Kept for the record; running it again
# only re-copies the same files.
#
# Notes are copied into docs/history/ unchanged except for two redactions for
# the public repo: LAN addresses and the Wi-Fi network name.
set -euo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
WS=${WS:-$HOME/claude-project}
H=$REPO/docs/history

# The Wi-Fi network names saved on this machine, so they can be redacted
# without writing them into this (public) script.
SSIDS=$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: '$2=="802-11-wireless"{print $1}' | paste -sd'|')

redact() {
  if [ -n "$SSIDS" ]; then
    sed -E -e 's/192\.168\.[0-9]{1,3}\.[0-9]{1,3}/<pi-address>/g' -e "s/($SSIDS)/<wifi-ssid>/g" "$1" > "$2"
  else
    sed -E -e 's/192\.168\.[0-9]{1,3}\.[0-9]{1,3}/<pi-address>/g' "$1" > "$2"
  fi
}

mkdir -p "$REPO"/tools/{aec,wake,session,wake-bench} "$REPO/tests" "$H/originals"

# --- measurement and replay tools (run from the checkout, not installed) ---
cp -p "$WS"/aec-{align-check,delay-sweep,echo-track,erle-sweep,gain-sweep,refcheck,ref-diag,refpath,ref-timing-probe,start-spread}.py \
      "$WS"/aec-test-cpu-load.sh "$WS"/dsp-switch-check.py "$REPO/tools/aec/"
cp -p "$WS"/wake-replay.py "$WS"/wake-verifier-train.py \
      "$WS"/staged-2026-10-09-second-opinion-blocks/second-opinion-check.py "$REPO/tools/wake/"
cp -rp "$WS"/wake-test-clips "$REPO/tools/wake/"          # synthetic (Piper TTS) clips only
cp -p "$WS"/voice-session-replay.py "$REPO/tools/session/"
cp -p "$WS"/wake-bench/{bench.py,report.py,loosen_eval.py} "$REPO/tools/wake-bench/"
cp -p "$WS"/staged-2026-10-09-second-opinion-blocks/test_*.py "$REPO/tests/"

# --- the working notes, by date -------------------------------------------
while IFS='|' read -r src dst; do
  [ -e "$src" ] && redact "$src" "$H/originals/$dst"
done <<EOF
$HOME/raspberry-pi-smart-speaker-config/README.txt|2026-08-15-first-dsp-config.txt
$WS/hifiberry-amp4-rpi5-config.md|2026-08-15-hifiberry-amp4.md
$WS/Camilla DSP Setup/smart-speaker-camilladsp-setup.md|2026-08-16-camilladsp-bundle.md
$HOME/xvf-volume-led/README.md|2026-08-17-xvf-volume-led.md
$WS/raspberry-pi-5-spotifyd-camilladsp-setup.md|2026-08-18-spotifyd.md
$WS/PHASE-6-7-PLAN.md|2026-08-21-phase-6-7-plan.md
$WS/PHASE-8.3-SPOTIFY-SEARCH-PLAN.md|2026-08-22-phase-8.3-spotify-search.md
$WS/WAKE_WORD_GEMINI_INTEGRATION.md|2026-08-23-wake-word-gemini-integration.md
$WS/AI-SPEAKER-FLEXIBILITY-IMPROVEMENTS.md|2026-08-23-flexibility-improvements.md
$WS/TIMER-ALARM-IMPLEMENTATION.md|2026-08-23-timer-alarm.md
$WS/WAKE_WORD_SENSITIVITY_INVESTIGATION.md|2026-08-24-wake-word-sensitivity.md
$WS/AEC-REFERENCE-GAIN-FINDING.md|2026-08-24-aec-reference-gain.md
$WS/WIFI-BOOT-FAILURE-FIX.md|2026-08-29-wifi-boot-failure.md
$WS/WAKE-WORD-RELIABILITY-CONTEXT-2026-08-30.md|2026-08-30-wake-word-reliability-context.md
$WS/WAKE-WORD-RELIABILITY-FOLLOWUP-2026-10-04.md|2026-10-04-wake-word-reliability-followup.md
$WS/parked-2026-10-04-ref-clock/README.md|2026-10-04-ref-clock-parked.md
$WS/AI-SPEAKER-SETUP-NOTES.md|2026-10-05-setup-notes.md
EOF

# One-off fix scripts of that time (their effect now lives in system/).
mkdir -p "$H/originals/scripts"
for f in fix-ai-speaker.sh fix-wifi-boot.sh; do
  [ -e "$WS/$f" ] && redact "$WS/$f" "$H/originals/scripts/$f"
done

find "$REPO/tools" "$REPO/tests" -name __pycache__ -prune -exec rm -rf {} +
echo "Imported. Review with: git -C '$REPO' status"
