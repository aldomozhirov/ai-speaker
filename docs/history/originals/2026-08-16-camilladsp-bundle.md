# Raspberry Pi Smart Speaker Setup

This guide reproduces the working audio path:

~~~text
Chromium -> PipeWire -> ALSA Loopback -> CamillaDSP -> HiFiBerry Amp4
~~~

It assumes:

- Raspberry Pi OS on a 64-bit Raspberry Pi
- HiFiBerry Amp4 configured as **sndrpihifiberry**
- Woofer on the Amp4 left output
- Tweeter on the Amp4 right output
- The prepared Camilla bundle ZIP is available

CamillaDSP does not need to be installed beforehand. The bundle installer
downloads and installs the tested CamillaDSP version itself.

> Important: This is an active crossover. Never play audio directly to the
> Amp4, because that bypasses tweeter protection.

## 1. Install CamillaDSP and the Bundle

The prepared archive is:

**smart-speaker-software-only-config.zip**

Copy it and its checksum from your computer to the Pi:

~~~bash
scp smart-speaker-software-only-config.zip \
  smart-speaker-software-only-config.zip.sha256 \
  aldom@<pi-address>:~/
~~~

Connect to the Pi:

~~~bash
ssh aldom@<pi-address>
~~~

Install the tools needed to inspect the sound card and unpack the archive:

~~~bash
sudo apt update
sudo apt install -y unzip alsa-utils
~~~

Confirm the HiFiBerry card identifier:

~~~bash
aplay -l
cat /proc/asound/cards
~~~

It will normally be **sndrpihifiberry**. Then unpack and install the bundle:

~~~bash
cd ~
sha256sum -c smart-speaker-software-only-config.zip.sha256
unzip -o smart-speaker-software-only-config.zip
cd smart-speaker-software-only
sudo HIFIBERRY_CARD=sndrpihifiberry ./install.sh
sudo reboot
~~~

If **aplay -l** reports another HiFiBerry identifier, replace
**sndrpihifiberry** with that identifier. Internet access is required during
installation. The installer:

- installs the required ALSA packages and plugins,
- downloads the pinned CamillaDSP 4.1.3 AArch64 binary,
- verifies its official SHA-256 checksum,
- configures ALSA Loopback,
- generates all six profiles with the corrected loopback and buffer settings,
- installs the final PipeWire no-mmap rule and persistent default-sink helper,
- installs and enables the CamillaDSP and CamillaGUI services,
- selects the safe music profile and starts CamillaDSP at -7 dB.

### What the installer changes and why

| Installation action | Why it is needed |
|---|---|
| Checks for 64-bit ARM, the desktop user, and the HiFiBerry card | Stops before changing the system if the hardware or OS does not match the bundle. |
| Installs ALSA, PipeWire, WirePlumber, download, and certificate packages | Provides the complete audio route and secure release downloads on a clean OS. |
| Backs up existing DSP, GUI, service, module, and WirePlumber files | Makes previous settings recoverable under `/var/backups/smart-speaker-TIMESTAMP`. |
| Removes the old `smartspk` `/etc/asound.conf` only when detected | Prevents the previous ALSA-default route from competing with the final PipeWire route. |
| Downloads pinned CamillaDSP and CamillaGUI releases and verifies SHA-256 | Ensures the installed programs are the exact tested builds and were not corrupted. |
| Creates restricted `camilladsp`, `camillagui`, and `camillaconfig` identities | Gives each service only the device and configuration access it needs. |
| Loads `snd-aloop` as card 0 | Creates the virtual cable between PipeWire playback and CamillaDSP capture. |
| Generates six profiles from one routing template | Keeps music, voice, and measurement profiles on the corrected device and buffer settings. |
| Installs the music crossover and protection filters | Splits mono audio safely between the left woofer and right tweeter. |
| Adds the WirePlumber no-mmap rule and fixed audio parameters | Prevents the silent/erroring loopback capture seen with mmap and keeps both ends at S32_LE, 48 kHz, stereo. |
| Enables user lingering and the default-sink helper | Starts the user audio session at boot and reselects Loopback by stable node name instead of a changing numeric ID. |
| Runs CamillaDSP as a restarting system service | Restores processing automatically and retries if the user audio session is not ready yet. |
| Installs CamillaGUI on the Pi's LAN address | Provides tuning and meters while keeping the DSP websocket itself on `127.0.0.1`. |
| Enables both services and requests a reboot | Applies the fixed ALSA card order and verifies the same route can return after a cold start. |

The generated profiles share `48 kHz`, `S32_LE`, chunk size `1024`, target
level `3072`, and capture device `hw:Loopback,1,0`. Only the music profile
receives the final `+4 dB` bass shelf. Measurement profiles retain driver
protection and mute the unused output where appropriate.

After reboot, reconnect and verify the bundle installation:

~~~bash
sudo smartspk-profile status
sudo smartspk-profile list
systemctl status camilladsp --no-pager
systemctl status camillagui --no-pager
journalctl -u camilladsp -n 50 --no-pager
~~~

Select the normal profile if needed:

~~~bash
sudo smartspk-profile music
~~~

The final tested tuning is:

~~~text
Sample rate:       48000 Hz
Chunk size:        1024
Target level:      3072
Camilla volume:    -7 dB
DSP headroom:      -5 dB
Bass shelf:        +4 dB at 150 Hz
Woofer protection: LR4 high-pass at 80 Hz
Crossover:         LR4 at 2800 Hz
Tweeter trim:      -3.5 dB
Limiter:           -1 dBFS
~~~

## 2. Confirm ALSA Loopback

~~~bash
lsmod | grep snd_aloop
aplay -l
arecord -l
~~~

Confirm that an ALSA card named **Loopback** appears. If it is missing, load
the driver once with `sudo modprobe snd-aloop`, then repeat the checks above.

## 3. Confirm PipeWire

The installer adds the tested WirePlumber rule, including the critical
`api.alsa.disable-mmap = true` setting. Confirm it is present:

~~~bash
grep -E 'disable-mmap|period-size|period-num' \
  ~/.config/wireplumber/wireplumber.conf.d/51-smart-speaker.conf
systemctl --user is-active pipewire wireplumber
~~~

## 4. Confirm the Default Sink

The installed user service selects **Loopback Analog Stereo** by stable node
name after every boot. Apply it immediately and verify:

~~~bash
smartspk-set-default
systemctl --user is-enabled smartspk-default-sink.service
wpctl status
~~~

The Loopback sink should have the `*` marker and volume `1.00`. If Chromium
was open during installation, restart it once:

~~~bash
pkill chromium
~~~

Start Chromium again and play audio.

## 5. Verify the Route

~~~bash
wpctl status
sudo fuser -v /dev/snd/pcmC0D0p /dev/snd/pcmC0D1c /dev/snd/pcmC1D0p
~~~

Expected ownership:

~~~text
/dev/snd/pcmC0D0p  pipewire
/dev/snd/pcmC0D1c  camilladsp
/dev/snd/pcmC1D0p  camilladsp
~~~

While audio is playing:

~~~bash
cat /proc/asound/Loopback/pcm0p/sub0/hw_params
~~~

Expected values:

~~~text
format: S32_LE
channels: 2
rate: 48000
period_size: 1024
buffer_size: 16384
~~~

Check for fresh DSP errors:

~~~bash
journalctl -u camilladsp --since "-5 minutes" --no-pager
~~~

There should be no capture, playback, overrun, or underrun errors.

## 6. Use CamillaGUI

Open:

**http://PI_ADDRESS:5005/**

For the tested device:

**http://<pi-address>:5005/**

Expected status:

~~~text
State: RUNNING
Profile: music.yml
Volume: -7 dB
Bass boost: 4 dB
Sample rate: 48000
Clipped samples: 0
~~~

Safe controls:

- Use **Shortcuts -> Bass boost** for bass adjustment.
- Keep bass boost between 0 and 5 dB.
- Click **Apply and save** to persist profile changes.
- Use the top volume control for live volume.
- Reboot restores the safe -7 dB startup volume.

CamillaGUI uses plain HTTP without authentication. Keep port 5005 restricted
to the trusted home network.

## 7. Reboot Test

~~~bash
sudo reboot
~~~

After reconnecting:

~~~bash
systemctl is-active camilladsp
systemctl is-active camillagui
wpctl get-volume @DEFAULT_AUDIO_SINK@
sudo fuser -v /dev/snd/pcmC0D0p /dev/snd/pcmC0D1c /dev/snd/pcmC1D0p
PI_ADDRESS=$(hostname -I | awk '{print $1}')
curl -sS -o /dev/null -w '%{http_code}\n' "http://$PI_ADDRESS:5005/gui/index.html"
~~~

The two services should be active, the default sink should be at volume
1.00, the three PCM devices should have the expected owners, and the GUI
should return HTTP 200.
