# AI Speaker

A home smart speaker built on a Raspberry Pi 5: a two-way active loudspeaker
with Spotify Connect, a "Hey Jarvis" wake word, and a spoken conversation with
Google's Gemini Live that can play music, change the volume and set timers and
alarms. It answers in English or Russian.

Everything here runs on stock Raspberry Pi OS. This repository holds every
file that was added or changed on top of it, an installer that puts them on a
Pi, the documentation of how the system works, and the history of how it got
this way.

```
 Spotify app ──Connect──▶ spotifyd ─┐
                                    ├─▶ PipeWire ─▶ snd-aloop ─▶ CamillaDSP ─▶ HiFiBerry Amp4 ─▶ woofer + tweeter
 Gemini Live ◀──▶ voice-agent ──────┘       │         (crossover, protection)
      ▲                                     └─ copy of the program audio ──▶ XVF3800 echo canceller
      │                                                                              │
      └──────────── wake-word (openWakeWord + verifier + second opinion) ◀── mic array (beamformed, echo-cancelled)
```

## Hardware

Raspberry Pi 5 · HiFiBerry Amp4 · SB Acoustics SB12PFC25-4 woofer · Dayton
Audio ND25FA-4 tweeter · Seeed ReSpeaker XVF3800 USB 4-mic array (with LED
ring) · four GPIO buttons · Pi 5 active cooler. Details:
[docs/hardware.md](docs/hardware.md).

## Quick start

**Update the speaker after changing something in this repo** (no sudo needed
for user files):

```bash
./install.sh diff user          # what would change
./install.sh apply user         # install it; prints which services to restart
sudo ./install.sh apply system  # the same for /etc and /usr/local
```

**See what was changed on the device by hand** and adopt it:

```bash
./install.sh diff all --show
./install.sh capture user && git diff
```

**Set up a new Pi** (64-bit Raspberry Pi OS, HiFiBerry overlay in
`config.txt`, XVF3800 plugged in):

```bash
git clone https://github.com/aldomozhirov/ai-speaker && cd ai-speaker
sudo ./install.sh bootstrap-system && sudo reboot
./install.sh bootstrap-user
tools/private-backup.sh restore private-backup-….tar.gz   # keys, Spotify login, verifier
```

The full procedure, including firmware and secrets, is in
[docs/operations.md](docs/operations.md#new-device).

## Documentation

Living documentation. It describes the system as it is now and is updated in
the same commit as the change it describes.

| Document | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | The components, the services and how they talk to each other; what happens between "Hey Jarvis" and the reply |
| [docs/hardware.md](docs/hardware.md) | Parts, wiring, GPIO pins, boot configuration, XVF3800 firmware, cooling |
| [docs/audio-pipeline.md](docs/audio-pipeline.md) | PipeWire, snd-aloop, CamillaDSP profiles, the single volume control, ducking, and the echo canceller with the eight conditions it depends on |
| [docs/wake-word.md](docs/wake-word.md) | Detection, the household verifier, the second opinion, captures, thresholds, retraining |
| [docs/voice-agent.md](docs/voice-agent.md) | Sessions with Gemini Live, half-duplex, the tools (Spotify, volume, timers), languages, chimes and LEDs |
| [docs/configuration.md](docs/configuration.md) | Every setting, per service, with its value and meaning |
| [docs/operations.md](docs/operations.md) | Install, update, roll back, health checks, logs, reboot checklist, secrets, new device |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Symptoms and their known causes; traps that have already cost hours |
| [docs/extending.md](docs/extending.md) | How to add a voice tool, a service, a setting or a DSP profile, and how to test audio changes safely |
| [docs/tools.md](docs/tools.md) | The measurement, replay and test tools under `tools/` and `tests/` |
| [docs/decisions/](docs/decisions/README.md) | Architecture decision records: why each major choice was made and what it cost |
| [docs/specs/](docs/specs/self-extension.md) | Designs that are not built yet (draft). They describe plans, not the system as it is |
| [docs/history/](docs/history/README.md) | The original working notes from each stage, kept as written |
| [CHANGELOG.md](CHANGELOG.md) | Dated history of every change, with the reasoning and the evidence |

## Repository layout

```
install.sh, manifest.txt   installer and the map of repo file -> live path
src/                       code installed to ~/.local/bin (and ~/thermal-monitor)
user/                      files under ~ at their real paths (systemd user units, WirePlumber, spotifyd, sounds)
system/                    root-owned files at their real paths (/etc, /usr/local, boot fragment)
deps/                      apt packages, pinned Python requirements, the spotifyd patch
config/                    templates for the secrets (the real ones never enter git)
tools/                     measurement and replay tools, run from the checkout
tests/                     unit tests for the wake-word and tool-guard code
docs/                      the documentation above
AGENTS.md                  rules for AI coding agents working on this repo
```

## Status (2026-10-11)

Working day to day. Known open items: wake-word sensitivity at a distance and
over music is "good enough" rather than good; far-field commands over ducked
music are often not heard by Gemini; the second-opinion check costs about one
real wake-up in five. See [CHANGELOG.md](CHANGELOG.md) for the latest state and
[docs/troubleshooting.md](docs/troubleshooting.md) for what to check first.

Personal, non-commercial project. Free tiers only: no paid service is required.
MIT licence ([LICENSE](LICENSE)); the sound effects are stock sounds.
