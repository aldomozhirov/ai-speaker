# HiFiBerry Amp4 on Raspberry Pi 5

On current Raspberry Pi OS releases, the boot configuration file is:

```text
/boot/firmware/config.txt
```

Open it as an administrator:

```bash
sudo nano /boot/firmware/config.txt
```

Add these lines, preferably beneath the `[all]` section if that section exists:

```ini
# Disable the Raspberry Pi's onboard audio
dtparam=audio=off

# Enable HiFiBerry Amp4 (recommended for modern kernels)
dtoverlay=hifiberry-dacplus-std
```

If `dtparam=audio=on` is already present, replace it with `dtparam=audio=off`. Remove or comment out any conflicting HiFiBerry audio overlay so that only the intended overlay is active.

> **Older kernels:** If `hifiberry-dacplus-std` is unavailable, use `dtoverlay=hifiberry-dacplus` instead.

Save the file, then reboot:

```bash
sudo reboot
```

After the Pi restarts, verify that the Amp4 is detected:

```bash
aplay -l
cat /proc/asound/cards
```

The output should list a HiFiBerry/DAC+ sound card.
