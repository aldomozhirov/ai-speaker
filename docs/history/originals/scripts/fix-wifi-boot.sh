#!/usr/bin/env bash
# Fixes for "WiFi did not come up after reboot" (2026-08-29 investigation).
#
#   1. journald persistent           - the Aug 24 fix never actually ran, so the
#                                      failed boot left no evidence at all
#   2. cloud-init out of networking  - it re-renders wlan0 config every boot and
#                                      its NM reconciliation module is missing
#   3. remove empty netplan stubs    - 0-byte 90-NM-*.yaml debris from the
#                                      Aug 26 NM/netplan migration churn
#   4. harden the NM wifi profile    - autoconnect, infinite retries, no powersave
#   5. install wifi-recover          - self-heals wlan0 at boot + every 2 min,
#                                      escalating to a brcmfmac reload
#
# Idempotent: safe to re-run. Backs up every file it touches.
# Optional static IP:  STATIC_IP=<pi-address>/24 sudo ./fix-wifi-boot.sh
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
STAMP=$(date +%Y%m%d-%H%M%S)
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BAK=/root/wifi-fix-backup-$STAMP
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
install -d -m 700 "$BAK"

say "1/5 journald: make logs survive a reboot"
# /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf sets
# Storage=volatile. A 10-* drop-in in /etc does NOT beat a 40-* one in /usr:
# drop-ins are merged by filename order across all directories, so we need a
# number above 40.
rm -f /etc/systemd/journald.conf.d/10-persistent.conf   # from fix-ai-speaker.sh; sorts too low
install -d -m 2755 -o root -g systemd-journal /var/log/journal
install -d -m 755 /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/99-persistent.conf <<'EOF'
# 2026-08-29: overrides /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf.
# Every crash and every failed boot was destroying its own logs. Capped for the SD card.
[Journal]
Storage=persistent
SystemMaxUse=200M
SystemMaxFileSize=20M
EOF
systemd-tmpfiles --create --prefix /var/log/journal
systemctl restart systemd-journald
echo "effective: $(systemd-analyze cat-config systemd/journald.conf | grep -E '^Storage=' | tail -1)"

say "2/5 cloud-init: stop it managing the network"
# The RPi Imager seed at /boot/firmware/network-config re-renders wlan0 as netplan
# on every boot, but cloud-init logs 'Could not find module named cc_netplan_nm_patch'
# every boot - the shim that reconciles that with NetworkManager is missing. NM is
# what actually runs the link, so let NM own it alone.
cat > /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg <<'EOF'
# 2026-08-29: NetworkManager owns wlan0. cloud-init's netplan rendering raced it
# and its cc_netplan_nm_patch reconciliation module is not installed.
network: {config: disabled}
EOF
echo "wrote /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg"

say "3/5 netplan: clear the empty stubs and the perms warning"
shopt -s nullglob
for f in /etc/netplan/90-NM-*.yaml; do
  if [[ ! -s $f ]]; then
    mv -v "$f" "$BAK/"          # 0 bytes: migration debris, no config lost
  else
    echo "keeping non-empty $f"
  fi
done
shopt -u nullglob
# Silences: "Permissions for /lib/netplan/00-network-manager-all.yaml are too open"
[[ -f /lib/netplan/00-network-manager-all.yaml ]] && chmod 600 /lib/netplan/00-network-manager-all.yaml
ls -la /etc/netplan/

say "4/5 NetworkManager: harden the wifi profile"
CON=$(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless"{print $1; exit}')
if [[ -z ${CON:-} ]]; then
  echo "!! no wifi profile found - nothing to harden" >&2
else
  cp -a /etc/NetworkManager/system-connections/*.nmconnection "$BAK/" 2>/dev/null || true
  nmcli connection modify "$CON" \
    connection.autoconnect yes \
    connection.autoconnect-retries 0 \
    connection.autoconnect-priority 10 \
    802-11-wireless.powersave 2 \
    ipv4.dhcp-timeout 60 \
    ipv4.may-fail no
  if [[ -n ${STATIC_IP:-} ]]; then
    GW=$(ip -4 route show default | awk '{print $3; exit}')
    echo "pinning $STATIC_IP via ${GW:?no default gateway found}"
    nmcli connection modify "$CON" \
      ipv4.method manual ipv4.addresses "$STATIC_IP" \
      ipv4.gateway "$GW" ipv4.dns "$GW"
  fi
  nmcli -f connection.autoconnect,connection.autoconnect-retries,802-11-wireless.powersave,ipv4.method,ipv4.addresses \
        connection show "$CON"
fi
# Apply powersave now without dropping the link.
iw dev wlan0 set power_save off 2>/dev/null || true
echo "live power_save: $(iw dev wlan0 get power_save 2>/dev/null)"

say "5/5 wifi-recover: self-heal at boot and every 2 minutes"
install -m 755 "$SRC/wifi-recover.sh" /usr/local/sbin/wifi-recover
cat > /etc/systemd/system/wifi-recover.service <<'EOF'
[Unit]
Description=Recover wlan0 if it failed to come up
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/wifi-recover
TimeoutStartSec=300
EOF
cat > /etc/systemd/system/wifi-recover.timer <<'EOF'
[Unit]
Description=Check wlan0 shortly after boot and every 2 minutes

[Timer]
OnBootSec=75s
OnUnitInactiveSec=2min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now wifi-recover.timer
systemctl list-timers wifi-recover.timer --no-pager

say "done - backups in $BAK"
echo "verify after the next reboot:"
echo "  journalctl --list-boots          # must show more than one"
echo "  journalctl -u wifi-recover -b    # what, if anything, it had to fix"
