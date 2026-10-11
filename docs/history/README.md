# History: the original working notes

The documents in `originals/` were written while the speaker was being built,
mostly as hand-over notes between working sessions. They are kept unchanged
(except that LAN addresses and the Wi-Fi network name are replaced by
placeholders) because they record what was believed at the time, including
the parts that later turned out to be wrong. For how the system works **now**,
read the documents in `docs/`; for the timeline, `CHANGELOG.md`.

Several of these notes contain claims later corrected. The biggest ones:
the 0.341 s reference delay was an estimate that made the reference 264 ms late
(corrected 2026-10-04); the August "ERLE" sweeps measured an AGC'd output, not
cancellation (2026-10-04); "journalctl --user" does not work on this Pi
(2026-08-24); "no active cooling" (a fan was fitted, 2026-09-21).

| Date | Note | About |
|---|---|---|
| 2026-08-15 | [first-dsp-config](originals/2026-08-15-first-dsp-config.txt) | The first CamillaDSP configuration bundle |
| 2026-08-15 | [hifiberry-amp4](originals/2026-08-15-hifiberry-amp4.md) | Enabling the Amp4 on the Pi 5 |
| 2026-08-16 | [camilladsp-bundle](originals/2026-08-16-camilladsp-bundle.md) | The final DSP bundle: install, verify, tune |
| 2026-08-17 | [xvf-volume-led](originals/2026-08-17-xvf-volume-led.md) | The XVF3800 LED ring as a volume display |
| 2026-08-18 | [spotifyd](originals/2026-08-18-spotifyd.md) | spotifyd, the patch, volume sync, the control architecture |
| 2026-08-21 | [phase-6-7-plan](originals/2026-08-21-phase-6-7-plan.md) | Session robustness and ducking: findings and exit criteria |
| 2026-08-22 | [phase-8.3-spotify-search](originals/2026-08-22-phase-8.3-spotify-search.md) | Spotify search through the Web API |
| 2026-08-23 | [wake-word-gemini-integration](originals/2026-08-23-wake-word-gemini-integration.md) | The build guide for the whole voice layer, steps 1–8 |
| 2026-08-23 | [flexibility-improvements](originals/2026-08-23-flexibility-improvements.md) | pw_node, the status script, the engine switch, offline intents |
| 2026-08-23 | [timer-alarm](originals/2026-08-23-timer-alarm.md) | Timers and alarms, offline parsing, LED states |
| 2026-08-24 | [wake-word-sensitivity](originals/2026-08-24-wake-word-sensitivity.md) | The first sensitivity investigation (superseded the same day) |
| 2026-08-24 | [aec-reference-gain](originals/2026-08-24-aec-reference-gain.md) | The starved echo-canceller reference |
| 2026-08-29 | [wifi-boot-failure](originals/2026-08-29-wifi-boot-failure.md) | No Wi-Fi after reboot: diagnosis and fix |
| 2026-08-30 | [wake-word-reliability-context](originals/2026-08-30-wake-word-reliability-context.md) | The four symptoms and everything measured about them |
| 2026-10-04 | [wake-word-reliability-followup](originals/2026-10-04-wake-word-reliability-followup.md) | Causes, measurements and fixes, 4–9 Oct: the most detailed record of the project |
| 2026-10-04 | [ref-clock-parked](originals/2026-10-04-ref-clock-parked.md) | The "parked" clock design, installed the same night |
| 2026-10-05 | [setup-notes](originals/2026-10-05-setup-notes.md) | The running overview of setup decisions, last updated 5 Oct |

`originals/scripts/` holds two one-off fix scripts of that period; their
effect is in `system/` now.

Not kept: the `staged-*` install folders (their final contents are the
current code), measurement result files, and household recordings.
