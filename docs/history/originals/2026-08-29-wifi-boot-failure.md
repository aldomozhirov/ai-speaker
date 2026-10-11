# Wi-Fi did not come up after reboot — diagnosis and fix

Session date: 2026-08-29. Delivers `fix-wifi-boot.sh` and `wifi-recover.sh`.
Follows the 2026-08-24 overheating work; **that session's `fix-ai-speaker.sh` was
never actually run** (proof below), which is why this incident left no logs.

## What was reported

`ai-speaker` came up from a reboot with no Wi-Fi. A second reboot fixed it.
Confirmed from the boot record: a boot at **23:17** (`/var/log/boot.log.1`) and
the current, healthy one at **23:20**.

## The diagnosis is inconclusive, and that is itself the first finding

There are no logs from the failed boot. `journalctl --list-boots` returns exactly
one row. The cause:

```
/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf:2:Storage=volatile
```

`/var/log/journal/` exists but is empty — with `Storage=volatile` systemd never
writes to it. Every reboot, clean or not, destroys the evidence for whatever
just went wrong.

`fix-ai-speaker.sh` was supposed to have fixed this on Aug 24. It had not run:

| Its intended effect | Actual state on 2026-08-29 |
|---|---|
| `/etc/systemd/journald.conf.d/10-persistent.conf` | directory does not exist |
| `Storage=persistent` | `Storage=volatile` still effective |
| `802-11-wireless.powersave 2` | `0 (default)` |
| `iw dev wlan0 set power_save off` | kernel still logs `power save enabled` |

It also would not have worked as written. Drop-ins merge by **filename order
across `/etc` and `/usr` alike**, so `10-persistent.conf` sorts *below*
`40-rpi-volatile-storage.conf` and loses. This fix uses `99-`.

So the approach could not be "find the error line and fix it." It had to be:
enumerate the mechanisms that produce *this specific signature* — dead after one
boot, alive after the next — fix each cheaply, and install a net that self-heals
the next occurrence while recording what it saw.

## Mechanism 1: two managers claim wlan0

The image was flashed by RPi Imager, which left a cloud-init NoCloud seed in
`/boot/firmware/` (`meta-data`, `user-data`, `network-config`). `network-config`
declares `wlan0` and the SSID as **netplan v2**. cloud-init re-runs it every
boot. But every boot also logs:

```
modules.py[WARNING]: Could not find module named cc_netplan_nm_patch
```

`cc_netplan_nm_patch` is the shim that reconciles cloud-init's netplan output
with NetworkManager's keyfiles. It is not installed. Meanwhile NM is what
actually runs the link, from a hand-written keyfile:

```
/etc/NetworkManager/system-connections/<wifi-ssid>.nmconnection   2026-08-27 00:33
```

The residue of that conflict is still on disk — two **0-byte** files from the
Aug 26 migration churn, whose UUIDs match no live connection:

```
/etc/netplan/90-NM-75a1216a-9d1a-30cd-8aca-ace5526ec021.yaml   0 bytes
/etc/netplan/90-NM-768fb27b-41a2-3bb9-9029-9e3a74b33a71.yaml   0 bytes
```

When a generated profile and the hand-made keyfile both claim `wlan0` for the
same SSID, NM can bind the wrong one — stale PSK, `autoconnect=no` — or neither.

**Fix:** take cloud-init out of networking entirely
(`/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg`, `network: {config: disabled}`)
and quarantine the empty stubs. NM owns the link alone. Nothing is lost: the
stubs are empty, and the live profile is untouched.

## Mechanism 2: the SDIO radio may not survive a warm reboot

```
Kernel command line: reboot=w ...
mmc1: new UHS-I speed DDR50 SDIO card at address 0001
brcmfmac: brcmf_fw_alloc_request: using brcm/brcmfmac43455-sdio for chip BCM4345/6
```

`reboot=w` is a warm reboot; the radio hangs off the SDIO bus on `mmc1`. A warm
reboot can leave the chip un-power-cycled, the firmware download never happens,
and `wlan0` never appears at all. This fits the observed signature exactly —
dead after `reboot`, alive on the next boot, cleared by a cold start.

This one cannot be *proven* without logs from a failed boot, and it cannot be
prevented from userspace. It can only be **detected and recovered**, which is
what `wifi-recover.sh` step 3 does.

## Mechanism 3: IP churn, not radio failure

No DHCP reservation. The box has been `<pi-address>` and is now `.189`. If a
boot associates fine but lands on a different address, anything pinned to the
old one is unreachable and it *looks* like Wi-Fi died — the exact shape of the
camillagui bug in `ai-speaker-known-config-bugs`. Worth ruling out before
believing the radio failed at all.

## Mechanism 4: regulatory domain race (minor)

The AP is 5 GHz **channel 36**, and on the healthy boot
`CTRL-EVENT-REGDOM-CHANGE ... alpha2=DE` arrives *after* association. If the
regdomain lands late on some boot, 5 GHz channels are disallowed and association
fails until a retry. Mitigated by infinite autoconnect retries rather than
addressed directly.

## The fix

### `fix-wifi-boot.sh`

Idempotent, root-only, backs up everything it touches to
`/root/wifi-fix-backup-<stamp>/`.

| Step | Change | Mechanism |
|---|---|---|
| 1 | `99-persistent.conf`, `Storage=persistent`, capped 200 M | evidence |
| 2 | `network: {config: disabled}` for cloud-init | 1 |
| 3 | quarantine 0-byte `90-NM-*.yaml`; `chmod 600` the netplan lib file | 1 |
| 4 | NM profile: `autoconnect yes`, `autoconnect-retries 0`, `powersave 2`, `dhcp-timeout 60`, `may-fail no` | 3, 4 |
| 5 | install `wifi-recover` service + timer | 2 |

The journal cap (200 M / 20 M per file) is there because this is an SD card.

**Static IP is opt-in, not default** — `STATIC_IP=<pi-address>/24 sudo ./fix-wifi-boot.sh`.
A wrong address on a headless box locks you out with no console. A router-side
DHCP reservation for `d8:3a:dd:f5:79:6e` is the better cure for mechanism 3 and
carries none of that risk.

Only step 1 (`systemctl restart systemd-journald`) and step 5 (starting a timer)
touch running services. Nothing drops the link, so an SSH session survives the run.

### `wifi-recover.sh`

Installed to `/usr/local/sbin/wifi-recover`, driven by `wifi-recover.timer` at
`OnBootSec=75s` and every 2 min. It escalates only as far as it needs to:

| Step | Action | Recovers from |
|---|---|---|
| 0 | link up, global IPv4, gateway answers `ping` → exit 0 | — |
| 1 | `nmcli connection up` | profile present, never activated (mech. 1, 4) |
| 2 | `systemctl restart NetworkManager` | NM wedged, lost the supplicant |
| 3 | `modprobe -r brcmfmac_cyw brcmfmac; modprobe brcmfmac` | SDIO radio missing (mech. 2) |

Module order matters: `brcmfmac_cyw` holds `brcmfmac` on this kernel
(`/sys/module/brcmfmac/holders/` → `brcmfmac_cyw`), so it unloads first.

Health is defined as *reaching the gateway*, not merely `state=connected`.
Mechanism 3 produces a connected interface that is useless, and NM would happily
report success.

Everything goes to syslog under tag `wifi-recover`, so the timer doubles as the
instrument for the next occurrence — with a persistent journal, the recovery
attempt and its outcome will still be readable after the reboot.

## Verification

The health check was exercised against the live, healthy link and correctly
no-opped (exit 0, no privileges needed, nothing touched). Both scripts pass
`bash -n`. The recovery path itself is **untested** — it can only be exercised
by a genuine failure or a deliberate `nmcli con down` / `modprobe -r`.

After the next reboot:

```
journalctl --list-boots           # must now show more than one
journalctl -u wifi-recover -b     # what, if anything, it had to fix
nmcli -f 802-11-wireless.powersave connection show <wifi-ssid>
```

## What this does not fix

**The thermal problem.** The Pi 5 has no fan and hard-crashes pinned at 85 °C
(`ai-speaker-overheating`). Those unclean shutdowns are *why* there was no log
history to debug this properly. It read 65.9 °C and `throttled=0x0` during this
session, so nothing here was heat-related — but until the fan exists, incidents
will keep destroying their own evidence, and a persistent journal only helps if
the box survives long enough to flush it.

**Whether mechanism 1 or 2 was the actual cause.** Both are real and both are
now addressed, but the failed boot's logs are gone. If `wifi-recover` reports
that step 3 was what worked, it was the SDIO radio; if step 1, it was the
profile conflict. That is the experiment this fix sets up.
