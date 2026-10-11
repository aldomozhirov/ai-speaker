# XVF3800 central volume LED indicator

This setup makes the Raspberry Pi the single owner of the ReSpeaker XVF3800
LED ring. The indicator watches the actual PipeWire default-sink state through
PipeWire-Pulse, so it reacts to GPIO-button `wpctl` changes, desktop volume
controls, and other changes to the same system sink.

Default behaviour:

- blue-to-cyan 12-pixel volume arc, including a fractional final pixel;
- faint first dot for unmuted 0% volume;
- dim red ring for mute;
- 0.90 second hold after the last change;
- 0.55 second smooth fade to completely black;
- no LED indication at service startup until the volume actually changes.

## 1. Record the current playback default

Run this before connecting the XVF3800 directly:

```bash
wpctl inspect @DEFAULT_AUDIO_SINK@ | grep -E 'node.name|node.description'
wpctl get-volume @DEFAULT_AUDIO_SINK@
wpctl status
```

The new XVF USB firmware exposes both capture and playback endpoints. After the
firmware change, keep the existing CamillaDSP/Loopback path as the default
playback sink; do not make the XVF USB playback endpoint the system default.

## 2. Erase the obsolete XIAO application

This deliberately removes the ESPHome/Home Assistant application and settings
from the XIAO ESP32S3. It can be recovered later only by flashing firmware to
the XIAO again. It does not erase the XVF3800.

Disconnect every cable from the ReSpeaker. Connect only the XIAO-side USB-C
port to a Raspberry Pi USB host port.

Install `esptool` in an isolated environment:

```bash
sudo apt update
sudo apt install -y python3-venv

python3 -m venv "$HOME/.local/share/xiao-esptool"
"$HOME/.local/share/xiao-esptool/bin/pip" install --upgrade pip esptool
```

Put the XIAO into its ROM download mode: hold the small XIAO **BOOT** button,
press and release the small XIAO **RESET** button, then release **BOOT**. Find
the newly created serial port:

```bash
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
```

Set the port to the actual value shown above and confirm the chip before
erasing. For example:

```bash
XIAO_PORT=/dev/ttyACM0

"$HOME/.local/share/xiao-esptool/bin/python" -m esptool \
  --chip esp32s3 --port "$XIAO_PORT" chip-id
```

The output must identify an ESP32-S3. Then erase it:

```bash
"$HOME/.local/share/xiao-esptool/bin/python" -m esptool \
  --chip esp32s3 --port "$XIAO_PORT" erase-flash
```

Wait for a successful completion message, disconnect the XIAO USB cable, and
wait five seconds. Do not reconnect the XIAO-side USB port for the remaining
steps.

## 3. Enter XVF3800 Safe Mode

Install the firmware utility and clone the complete official repository:

```bash
sudo apt install -y dfu-util git

mkdir -p "$HOME/src"
cd "$HOME/src"
git clone --depth 1 \
  https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY.git
```

If the repository already exists, update it instead:

```bash
git -C "$HOME/src/reSpeaker_XVF3800_USB_4MIC_ARRAY" pull --ff-only
```

Enter Safe Mode:

1. Make sure the ReSpeaker has no other power connection.
2. Hold the ReSpeaker's main **Mute** button—not a XIAO button.
3. While holding Mute, connect the XMOS-side USB-C port, nearest the 3.5 mm
   jack, to a Raspberry Pi USB host port using a data cable.
4. Continue holding until the red mute LED blinks, then release.

Verify DFU detection:

```bash
sudo dfu-util -l
```

Do not proceed unless it lists `2886:001a` with both:

```text
alt=1, name="reSpeaker DFU Upgrade"
alt=0, name="reSpeaker DFU Factory"
```

If it lists nothing, repeat the complete power-off/hold/reconnect sequence,
try a different data cable or Raspberry Pi USB-A port, and inspect
`sudo dmesg -w` while reconnecting.

## 4. Flash the USB audio/control firmware

Confirm the selected firmware exists:

```bash
ls -lh \
  "$HOME/src/reSpeaker_XVF3800_USB_4MIC_ARRAY/xmos_firmwares/usb/respeaker_xvf3800_usb_dfu_firmware_v2.1.0_48k2ch.bin"
```

Flash only the upgrade partition:

```bash
sudo dfu-util -R -e -a 1 -D \
  "$HOME/src/reSpeaker_XVF3800_USB_4MIC_ARRAY/xmos_firmwares/usb/respeaker_xvf3800_usb_dfu_firmware_v2.1.0_48k2ch.bin"
```

Do not disconnect power during the write. The `-a 1` target leaves the Factory
Safe Mode partition intact. After the automatic reset, unplug and reconnect
only the XMOS-side USB cable normally.

Verify the result:

```bash
lsusb -d 2886:001a
arecord -l
wpctl status
```

The XVF should now appear as a two-channel capture device. If PipeWire selected
the XVF playback endpoint as the default, find the numeric ID of the existing
`Loopback` sink in `wpctl status` and restore it:

```bash
wpctl set-default LOOPBACK_SINK_ID
```

Replace `LOOPBACK_SINK_ID` with the actual current number. PipeWire IDs can
change across reboots, so do not place that numeric ID in a script.

## 5. Install USB permissions

From the directory containing this package:

```bash
sudo groupadd -f plugdev
sudo usermod -aG plugdev aldom

sudo install -Dm644 70-respeaker-xvf3800.rules \
  /etc/udev/rules.d/70-respeaker-xvf3800.rules

sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb
```

Reboot after completing the software installation below. The new group
membership will not apply to the existing login or user manager until a new
session starts.

## 6. Install the volume LED daemon

Install the Python dependencies in a dedicated environment:

```bash
sudo apt install -y python3-venv libusb-1.0-0 libpulse0

mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
python3 -m venv "$HOME/.local/share/volume-led/venv"

"$HOME/.local/share/volume-led/venv/bin/pip" install --upgrade pip
"$HOME/.local/share/volume-led/venv/bin/pip" install pyusb pulsectl
```

Install the script and user service:

```bash
install -Dm755 volume_led.py "$HOME/.local/bin/volume_led.py"
install -Dm644 volume-led.service \
  "$HOME/.config/systemd/user/volume-led.service"

systemctl --user daemon-reload
```

Ensure the user service manager is allowed to run at boot even before a GUI
login:

```bash
sudo loginctl enable-linger aldom
```

Now reboot:

```bash
sudo reboot
```

## 7. Test the hardware animation

After reboot, confirm access and run the demonstration before enabling the
background service:

```bash
groups
lsusb -d 2886:001a

"$HOME/.local/share/volume-led/venv/bin/python" \
  "$HOME/.local/bin/volume_led.py" --demo
```

The demonstration fills the blue/cyan ring, fades it out, displays the red
mute ring, and fades to black. It must run without `sudo`. If it reports
permission denied, verify that `groups` contains `plugdev`, then unplug and
reconnect the XMOS USB cable and rerun the test.

## 8. Enable the central service

```bash
systemctl --user enable --now volume-led.service
systemctl --user status volume-led.service --no-pager
```

Test all three paths:

```bash
wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%+
wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%-
wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle
wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle
```

Also move the desktop system-volume slider and use the GPIO volume buttons.
Every path should produce the same indicator because the service observes the
resulting default-sink state, not the source of the command.

Follow its log when diagnosing problems:

```bash
journalctl --user -u volume-led.service -f
```

## 9. Adjust the appearance

Edit:

```text
~/.config/systemd/user/volume-led.service
```

Useful settings in its `[Service]` section:

```ini
Environment=VOLUME_LED_HOLD=0.90
Environment=VOLUME_LED_FADE=0.55
Environment=VOLUME_LED_BRIGHTNESS=0.32
Environment=VOLUME_LED_LOW_COLOUR=0068FF
Environment=VOLUME_LED_HIGH_COLOUR=00D8C0
Environment=VOLUME_LED_MUTE_COLOUR=FF180C
Environment=VOLUME_LED_START_PIXEL=0
Environment=VOLUME_LED_REVERSE=0
```

- Brightness is from `0.0` to `1.0`.
- Colours are six-digit `RRGGBB` values without `#`.
- `START_PIXEL` rotates the beginning of the arc from 0 through 11.
- Set `REVERSE=1` if the arc runs in the undesired direction.

Apply changes:

```bash
systemctl --user daemon-reload
systemctl --user restart volume-led.service
```

## 10. Audio notes

- Keep the GPIO button script changing `@DEFAULT_AUDIO_SINK@` with `wpctl`.
  Do not add LED calls to it.
- Update any assistant configuration that referred to the old XIAO capture
  source so it uses the new `reSpeaker XVF3800` PipeWire source.
- Direct XVF USB preserves processed microphone capture. For effective AEC
  while the HiFiBerry speaker is playing, a later PipeWire configuration
  should also send the XVF USB playback endpoint a synchronized copy of the
  far-end signal while keeping the real speaker output on the existing
  CamillaDSP/HiFiBerry path.
