# Hardware

## Parts

| Part | Role | Notes |
|---|---|---|
| Raspberry Pi 5, 8 GB | Everything | Raspberry Pi OS 64-bit (Debian 13 "trixie") |
| HiFiBerry Amp4 (HAT) | DAC + stereo class-D amplifier | ALSA card `sndrpihifiberry`, overlay `hifiberry-dacplus-std` |
| SB Acoustics SB12PFC25-4 | Woofer / midwoofer | Amp4 **left** output |
| Dayton Audio ND25FA-4 | Tweeter | Amp4 **right** output |
| Seeed ReSpeaker XVF3800 USB 4-Mic Array | Microphones, beamforming, echo cancellation, 12-LED ring | USB `2886:001a`, ALSA card `Array`, firmware 2.1.0 (USB, 48 kHz, 2 ch) |
| Pi 5 fan on the fan header | Cooling | PWM-controlled by the firmware; fits under the Amp4 |
| 4 push buttons on GPIO | Volume, playback, wake | Pins below |

The two drivers form an active two-way speaker: the Amp4's two channels are
not left/right stereo but woofer/tweeter, split by CamillaDSP (mono sum, 2.8 kHz
Linkwitz-Riley crossover). **Never play directly to `hw:sndrpihifiberry`**:
full-range audio on the tweeter channel can damage it. See
[audio-pipeline.md](audio-pipeline.md).

## Sound cards

```
0 Loopback          snd-aloop: the virtual cable between PipeWire and CamillaDSP
1 sndrpihifiberry   the amplifier, owned by CamillaDSP only
2 Array             the XVF3800 (capture: processed mic; playback: echo-canceller reference only)
```

The loopback is card 0 so that its number is stable
(`/etc/modprobe.d/snd-aloop.conf`). The XVF3800's own analog output is not
connected to anything: its playback input is used only to give the echo
canceller a copy of what the speaker is playing.

## Buttons (BCM numbering)

| Button | BCM GPIO | Physical pin | Action |
|---|---|---|---|
| Volume up | 24 | 18 | +5 % on the PipeWire default sink |
| Volume down | 25 | 22 | −5 % |
| Playback | 22 | 15 | single press: play/pause, double press: next track |
| Wake | 23 | 16 | start a voice session, cancel a running one, stop a ringing timer/alarm |

Buttons connect the pin to ground (internal pull-ups). Pins are set in
`volume-buttons.py` and can be overridden with `VOLUME_UP_GPIO` etc. (the
commented-out lines in `volume-buttons.service` mention 17/27; those are stale,
the code's defaults above are what is used).

## Boot configuration

`/boot/firmware/config.txt` needs the lines in
[system/boot/firmware/config.txt.fragment](../system/boot/firmware/config.txt.fragment):
the KMS driver without HDMI audio (`vc4-kms-v3d,noaudio`) and the HiFiBerry
overlay. The installer only checks for them: a wrong boot configuration can
leave a headless Pi unreachable, so this is a manual step. The kernel command
line sets the Wi-Fi regulatory domain (`cfg80211.ieee80211_regdom=DE`).

## XVF3800 firmware

The array is flashed once with the USB firmware (2 channels, 48 kHz) from
Seeed's repository, which is also where the register library comes from:

```bash
git clone https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY ~/src/reSpeaker_XVF3800_USB_4MIC_ARRAY
sudo dfu-util -l     # the array must be listed
sudo dfu-util -R -e -a 1 -D ~/src/reSpeaker_XVF3800_USB_4MIC_ARRAY/xmos_firmwares/usb/respeaker_xvf3800_usb_dfu_firmware_v2.1.0_48k2ch.bin
```

`xvf_register.py get VERSION` prints `2 1 0` afterwards. The udev rule
`70-respeaker-xvf3800.rules` lets the `plugdev` group use the vendor control
interface (registers, LEDs) without root.

**Register settings are not saved to the chip's flash.** The ones the speaker
needs are re-applied by `xvf-aec-ref.sh` every time it starts and checked every
few minutes, because they reset whenever the array re-enumerates. This is a
deliberate convention: the repo, not the chip, is the source of truth.

**Never read a write-only register.** A `get` on a register marked "wo" in
`xvf_host.py` wedges the chip's control interface and silences the mic until
the array is rebooted. `xvf_register.py` refuses such reads; code that calls
`xvf_host` directly is not protected. Recovery is in
[troubleshooting.md](troubleshooting.md#mic-delivers-silence).

## Cooling

Without the fan the Pi sat at 83–87 °C and crashed twice (2026-08-24). With
the fan it stays around 50 °C idle and plateaus at 68–69 °C under load. The
Pi 5 firmware drives the fan by itself (steps at 50/60/67/75 °C); no overlay is
needed. Check with `vcgencmd measure_temp`, `vcgencmd get_throttled` (should
be `0x0`) and `/sys/class/hwmon/*/fan1_input`. `pinctrl get 29` reporting
`none` does not mean the fan is missing. The thermal monitor
(`http://<pi-address>:8000/`) keeps a weekly CSV of temperature and
throttling.
