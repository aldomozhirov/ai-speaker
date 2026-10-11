#!/usr/bin/env bash
# Fixes 2-4 from the 2026-08-24 overheating/crash investigation.
#   2. make journald persistent so logs survive reboots
#   3. stop camillagui crash-looping on a stale hardcoded IP
#   4. disable WiFi power save
# Idempotent: safe to re-run. Backs up every file it touches.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
STAMP=$(date +%Y%m%d-%H%M%S)
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "2/3 journald: make logs persistent"
install -d -m 2755 -o root -g systemd-journal /var/log/journal
install -d -m 755 /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/10-persistent.conf <<'EOF'
# Added 2026-08-24: journal was volatile, so every reboot destroyed the evidence
# needed to diagnose crashes. Capped to protect the SD card.
[Journal]
Storage=persistent
SystemMaxUse=200M
SystemMaxFileSize=20M
EOF
systemd-tmpfiles --create --prefix /var/log/journal
systemctl restart systemd-journald
echo "Storage=persistent; journal now at $(journalctl --disk-usage 2>/dev/null | tail -1)"

say "3/3 camillagui: bind all interfaces instead of a stale IP"
YML=/etc/camillagui/camillagui.yml
if [[ ! -f $YML ]]; then
  echo "!! $YML not found - skipping" >&2
else
  echo "-- current bind-related lines --"
  grep -nE 'bind|address|port|host' "$YML" || true
  if grep -q '192\.168\.1\.188' "$YML"; then
    cp -a "$YML" "$YML.bak-$STAMP"
    # Only rewrite the *bind* address. camilla_host (127.0.0.1) must not change.
    sed -i -E '/(bind|listen)/ s/192\.168\.1\.188/0.0.0.0/' "$YML"
    # If .188 was on a bare `address:` key, catch that too.
    sed -i -E '/^[[:space:]]*address:/ s/192\.168\.1\.188/0.0.0.0/' "$YML"
    echo "-- after --"
    grep -nE 'bind|address|port|host' "$YML" || true
    if grep -q '192\.168\.1\.188' "$YML"; then
      echo "!! .188 still present - inspect manually; backup at $YML.bak-$STAMP" >&2
    fi
  else
    echo "no <pi-address> in $YML - already fixed or configured elsewhere"
  fi
  systemctl reset-failed camillagui.service 2>/dev/null || true
  systemctl restart camillagui.service
  sleep 4
  systemctl is-active camillagui.service && \
    echo "restarts now: $(systemctl show -p NRestarts --value camillagui.service)"
fi

say "4/3 wifi: disable power save"
CON=$(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless"{print $1; exit}')
if [[ -n ${CON:-} ]]; then
  # 2 = disable. Persists across reboots (written back to /etc/netplan).
  nmcli connection modify "$CON" 802-11-wireless.powersave 2
  echo "persisted on '$CON': $(nmcli -f 802-11-wireless.powersave connection show "$CON")"
fi
# Apply now WITHOUT re-activating the connection, so the link does not drop.
iw dev wlan0 set power_save off
echo "live state: $(iw dev wlan0 get power_save)"

say "done - current thermal state (the real problem, needs a fan)"
vcgencmd measure_temp; vcgencmd get_throttled
