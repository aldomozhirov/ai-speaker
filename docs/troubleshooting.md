# Troubleshooting

Start with `ai-speaker-status.sh`. Then find the symptom below. Every entry
comes from something that actually happened on this device.

## Nothing starts after boot

- `loginctl show-user $USER -p Linger` must say `Linger=yes`; without it no
  user service starts until someone logs in. Fix: `sudo loginctl enable-linger $USER`.
- `systemctl --user` failing in an SSH command or cron: export
  `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` ([operations.md](operations.md#logs)).

## The wake word does not react

1. **Blocked by the second opinion?** `journalctl _SYSTEMD_USER_UNIT=wake-word.service | grep "blocked by the second opinion"`.
   It costs about one real wake-up in five. Softer: `WAKE_WORD_SECOND_OPINION=log`.
2. **Rejected by the verifier?** Look for `verifier=` with a low value. Common
   for a voice it was not trained on: add that person's captures to
   `wake-verifier/positive` and retrain ([wake-word.md](wake-word.md#tuning-and-retraining)).
3. **Score below 0.10?** Captures with scores 0.05–0.10 are in
   `wake-captures/`. Score them with `tools/wake/wake-replay.py score`.
4. **Mic silent?** See next section.
5. **Only over music?** Echo cancellation, see below.

## Mic delivers silence

Symptom: every service active, nothing triggers, `ai-speaker-status.sh` FAILs
on "digital silence". Known cause: something read a **write-only** XVF3800
register (2026-10-04), which wedges the chip's control interface. Recovery:

```bash
systemctl --user stop wake-word volume-led xvf-aec-ref
~/.venvs/xvf3800/bin/python ~/.local/bin/xvf_register.py set REBOOT 1   # array re-enumerates in ~2 s
systemctl --user start xvf-aec-ref && sleep 3 && systemctl --user start volume-led wake-word
ai-speaker-status.sh
```

`xvf-aec-ref` must come first: it re-applies the registers the reboot reset.
Stopping the three first keeps them from falling back to another device during
re-enumeration.

## The wake word is poor over music / echo cancellation is gone

- After a reboot or WirePlumber restart: `ai-speaker-status.sh` must report
  the loopback sink as **clock master** and the reference **not rate-matched**.
  If the XVF3800's output became the driver, cancellation drops from ~28 dB to
  0 dB while everything looks healthy (2026-10-04).
- `AUDIO_MGR_FAR_END_DSP_ENABLE` must be 1 and the XVF3800 sink volume 1.00
  (an XVF3800 sink at 0.40 starved the reference by ~40 dB for weeks, 2026-08-24).
- Measure: `tools/aec/aec-ref-timing-probe.py lead` (silent) or
  `tools/aec/aec-align-check.py` (audible). The full list of eight conditions
  is in [audio-pipeline.md](audio-pipeline.md#echo-cancellation).
- Right after music starts the canceller needs 10–14 s to converge; a wake
  word in the first seconds of a song is harder.

## False wake-ups / timers set by themselves

- Look at the second opinion and verifier lines for the false trigger; sort
  its capture into `wake-verifier/negative` and retrain.
- A timer or alarm set on noise is refused by the tool guard when nothing
  intelligible was transcribed (`Tool call:` lines show what was asked).

## Commands are not understood

**Replay before changing anything.** The audio of each attempt is in
`wake-captures/`. Feed it through the real session code and a real Gemini
session:

```bash
~/.venvs/voice-agent/bin/python tools/session/voice-session-replay.py capture <file.wav> --native --channel 1 --gain-db 12
```

Run each variant 2–3 times (answers vary). Known causes so far: wrong mic
channel (channel 0 is useless for Gemini here), a stream starting in mid-speech
(gets no reply), speaking from far away over music (open problem).

## Music is very quiet but the assistant is loud

The spotifyd stream is stuck at the duck level (0.20). Check with
`python3 ~/.local/bin/duck.py` while music plays. Cause (2026-10-06):
WirePlumber restored a volume saved while ducked; `spotifyd.service` now sets
`PULSE_PROP=state.restore-props=false`. If it happens anyway, reset with
`wpctl set-volume <stream id> 1.0` and look at
`~/.local/state/wireplumber/stream-properties`.

## "Play …" fails with Spotify errors (502/500)

Restarting spotifyd while the speaker is the active Spotify Connect device
leaves a stale session at Spotify: Web API play returns 502 for a long time.
Select "AI Speaker" again in the Spotify app. Avoid restarting spotifyd while
it is in use.

## The speaker vanishes from the Spotify app / network drops

- Wi-Fi power saving must be off: `iw dev wlan0 get power_save` → `off`
  (`powersave=2` in the NetworkManager profile).
- The DHCP address can change; CamillaGUI binds to all addresses for that
  reason. A reservation on the router is the real fix.

## No Wi-Fi after a reboot

Two causes found (2026-08-29): cloud-init's netplan and NetworkManager both
configured `wlan0` (cloud-init's network config is disabled now), and the
SDIO radio does not always come back after a warm reboot.
`wifi-recover.timer` checks 75 s after boot and every 2 min, and escalates
from reconnecting to reloading the Wi-Fi driver. Its log:
`journalctl -u wifi-recover`.

## CamillaGUI does not load

- Crash-looping on "could not bind": it was bound to a LAN address that DHCP
  had moved (fixed 2026-09-21, now 0.0.0.0).
- `/api/getconfig` returns 500 "Not connected to CamillaDSP" right after a
  restart until something calls `/api/status`. Not a fault.
- `/etc/camillagui` is readable only by root and `camillaconfig`; "Permission
  denied" is not a missing file.

## Crashes, freezes, the speaker reboots by itself

`vcgencmd measure_temp`, `vcgencmd get_throttled` (`0x0` is good). Without the
fan it ran at 85 °C, throttled and crashed. The thermal monitor
(`http://<pi-address>:8000/`) keeps history; runs of NUL bytes in its CSV mark
unclean power-offs.

## Traps for whoever changes the code

- **`pw-cat` with a target that does not exist** does not fail: it records
  from or plays to the default device. Wait for the node (`pw_node.py`) and
  check the audio.
- **Never set `node.dont-fallback = true`** on a stream here: WirePlumber
  destroys healthy streams carrying it as soon as another stream starts.
- `pw-cat --record` without `--raw` prepends a 24-byte header that looks like
  loud audio.
- `pw-cat --playback --raw <file>` (file argument) can hang and ignore SIGTERM;
  pipe the data on stdin instead and keep a kill fallback.
- A new capture delivers its first audio about 170 ms after spawn.
- `pgrep -f`/`pkill -f` with a pattern also matches your own shell; use
  `[x]yz` or a PID.
- `xvf_host`'s `read()` returns `None` for an unknown register name instead of
  raising.
- systemd drop-ins in `/etc` and `/usr/lib` merge **by file name order**: a
  `10-…` drop-in loses to a packaged `40-…` one (journald, 2026-08-29).
