# Operations

## The rule

**The repository is the source of truth.** Change files here, install them with
`install.sh`, check, commit. Do not edit live files under `~/.local/bin`,
`~/.config` or `/etc` directly. If that happened anyway (a quick fix at
night, another session on the Pi), adopt it with `capture` before doing
anything else, so it is neither lost nor silently overwritten.

The checkout lives at `~/ai-speaker` on the speaker.

## Making a change

```bash
cd ~/ai-speaker
git pull
./install.sh diff all          # must be clean before you start; if not, capture first
$EDITOR src/voice_agent.py     # change
./install.sh diff user --show  # review against live
./install.sh apply user        # install; the old files go to ~/.local/state/ai-speaker/backups/<time>/
systemctl --user daemon-reload && systemctl --user restart voice-agent   # as printed by apply
ai-speaker-status.sh           # check
git commit -am "voice-agent: …"  && git push
```

`apply` prints which units are affected: unit files that changed, units whose
`ExecStart` names a changed file, and units whose main script imports a
changed module directly. Modules imported indirectly are not detected; when in
doubt, restart `wake-word` and `voice-agent`. `--restart` restarts the user
units it found. WirePlumber and system services are never restarted
automatically.

Root-owned files: `sudo ./install.sh diff system`, `sudo ./install.sh apply system`.
(`diff` without sudo shows `NO-ACCESS` for `/etc/camilladsp` and `/etc/camillagui`.)

Every change gets a CHANGELOG entry: what changed, why, and the evidence. A
decision that future work should not undo without a reason also gets a record
in `docs/decisions/`.

### Audio changes need a live test first

Anything that changes how a PipeWire stream is opened, the WirePlumber
config, or timing must be exercised on the real audio graph before it is
installed for real, with other streams starting and stopping alongside it.
Offline harnesses have passed code that then broke on the device
(`node.dont-fallback`, 2026-10-04). How: [extending.md](extending.md#testing-audio-changes).

### Rolling back

```bash
git log --oneline                       # find the last good commit or tag
git checkout <commit> -- src/wake_word.py user/.config/systemd/user/wake-word.service
./install.sh apply user --restart
```

or restore the copy `apply` made under `~/.local/state/ai-speaker/backups/`.
Verified states are tagged `YYYY-MM-DD-<what>` once the change has been heard
working on the speaker.

## Health check

```bash
ai-speaker-status.sh
```

Checks every unit, lingering, the XVF3800 registers and convergence, the
echo-canceller link and gains, the graph's clock master and rate matching,
that the mic delivers non-zero audio, the wake-word health file and that
Gemini is reachable. **Run it after every reboot and every WirePlumber
restart**, before trusting anything audio-related.

Detailed checks: `ai-speaker-calibrate.py check` (registers, routing),
`ai-speaker-calibrate.py wake [--music]` (interactive: say "Hey Jarvis" a few
times, prints scores per channel), `pw-top` (the loopback sink must be the line
without `+`), `wpctl status`.

## Logs

The user services log to the system journal; there is **no per-user journal**
on this Pi, so `journalctl --user -u …` says "No journal files were found".
Use:

```bash
journalctl _SYSTEMD_USER_UNIT=wake-word.service -f
journalctl _SYSTEMD_USER_UNIT=voice-agent.service --since "-1h"
wake-scores.sh              # recent wake-word scores
```

Useful lines: `second opinion: … -> pass|block`, `Wake word blocked by the
second opinion`, `verifier=`, `Tool call:`, `Deferred action failed`,
`Previous session left CamillaDSP on …`. The journal is persistent (200 MB
cap), so it survives reboots and crashes.

In a non-login shell (SSH command, cron) export the session variables first:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
```

## After a reboot

1. `ai-speaker-status.sh` all OK (in particular "program sink is the clock
   master").
2. Play music, say "Hey Jarvis, pause" from across the room.
3. If echo cancellation is in doubt: `tools/aec/aec-ref-timing-probe.py lead`
   (silent) or `tools/aec/aec-align-check.py` (about 50 s of hiss; expect
   ~32 ms lead and 28–29 dB).

## Secrets and private data

Never in git (blocked by `.gitignore` as a second line of defence):

| What | Where | Recreate |
|---|---|---|
| Gemini API key | `~/.config/ai-speaker/voice-agent.env` | aistudio.google.com |
| Spotify Web API app + refresh token | `~/.config/ai-speaker/spotify.json` | `spotify_web.py auth` |
| spotifyd's Spotify Connect login | `~/.cache/spotifyd/zeroconf/` | select "AI Speaker" once in the Spotify app |
| Verifier model and its training captures | `~/.local/share/ai-speaker/wake-verifier/` | retrain from captures |
| Wake captures | `~/.local/share/ai-speaker/wake-captures/` | accumulate again |
| Wi-Fi profile (password) | `/etc/NetworkManager/system-connections/` | Raspberry Pi Imager or `nmcli` |

`tools/private-backup.sh create` puts the first five (captures only with
`--with-captures`) into one archive; keep it off this repo, e.g. on a private
drive. Do it after retraining the verifier or changing a key.

## New device

1. Flash Raspberry Pi OS 64-bit (trixie) with Raspberry Pi Imager: hostname,
   user, Wi-Fi, SSH.
2. Add the lines of `system/boot/firmware/config.txt.fragment` to
   `/boot/firmware/config.txt`, connect the Amp4, the XVF3800 and the buttons,
   reboot. `aplay -l` lists the HiFiBerry.
3. `git clone https://github.com/aldomozhirov/ai-speaker ~/ai-speaker && cd ~/ai-speaker`
4. `sudo ./install.sh bootstrap-system` (packages, CamillaDSP, CamillaGUI,
   service accounts, system files, groups, lingering, Wi-Fi power saving off).
   `sudo reboot`.
5. Flash the XVF3800 if it is new ([hardware.md](hardware.md#xvf3800-firmware)).
6. `./install.sh bootstrap-user` (Python environments, XVF3800 library,
   Vosk model, spotifyd built with the patch, user files, units enabled).
7. `tools/private-backup.sh restore <archive>`, or fill in
   `~/.config/ai-speaker/*` from the templates and run `spotify_web.py auth`.
8. Reboot, `ai-speaker-status.sh`, then the after-reboot checks above. The
   echo-canceller timing (`api.alsa.headroom`) was measured on this hardware;
   on different hardware measure it again (`tools/aec/aec-align-check.py`).

Device-specific values (home directory, user id, XVF3800 serial number) are
replaced by `install.sh` automatically. The XVF3800 serial is read from USB;
override with `XVF_SERIAL=…`.

## Working with AI coding agents on the Pi

Several AI coding sessions have worked on this device at the same time and
overwritten each other's installs. Rules (in full in `AGENTS.md`): work in the
repo, start with `./install.sh diff all`, keep changes small and committed,
and never install a whole file over a live file you have not compared.
