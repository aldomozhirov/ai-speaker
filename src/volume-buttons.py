#!/usr/bin/env python3

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from functools import partial

import socket

from gpiozero import Button, Device
from gpiozero.pins.lgpio import LGPIOFactory


# ---------------------------------------------------------------------------
# GPIO configuration
# ---------------------------------------------------------------------------
# BCM GPIO numbering, NOT physical pin numbers.

VOLUME_UP_GPIO = int(os.getenv("VOLUME_UP_GPIO", "24"))       # physical pin 18
VOLUME_DOWN_GPIO = int(os.getenv("VOLUME_DOWN_GPIO", "25"))   # physical pin 22

PLAYBACK_GPIO = int(os.getenv("PLAYBACK_GPIO", "22"))         # physical pin 15
WAKE_GPIO = int(os.getenv("WAKE_GPIO", "23"))                 # physical pin 16


# ---------------------------------------------------------------------------
# Volume configuration
# ---------------------------------------------------------------------------

VOLUME_STEP_PERCENT = int(os.getenv("VOLUME_STEP_PERCENT", "5"))
MAX_VOLUME = float(os.getenv("VOLUME_MAX", "1.0"))

SINK = os.getenv("VOLUME_SINK", "@DEFAULT_AUDIO_SINK@")

WPCTL = os.getenv("WPCTL_PATH", "/usr/bin/wpctl")
PLAYERCTL = os.getenv("PLAYERCTL_PATH", "/usr/bin/playerctl")

# ---------------------------------------------------------------------------
# Wake button configuration
# ---------------------------------------------------------------------------

WAKE_TRIGGER_SOCKET = os.getenv(
    "WAKE_TRIGGER_SOCKET",
    os.path.join(os.getenv("XDG_RUNTIME_DIR", "/tmp"), "wake-trigger.sock"),
)

ASSISTANT_LED_SOCKET = os.getenv(
    "ASSISTANT_LED_SOCKET",
    os.path.join(os.getenv("XDG_RUNTIME_DIR", "/tmp"), "assistant-led.sock"),
)


# ---------------------------------------------------------------------------
# Playback button configuration
# ---------------------------------------------------------------------------

# Maximum interval between presses to count them as a double press.
DOUBLE_PRESS_SECONDS = float(
    os.getenv("DOUBLE_PRESS_SECONDS", "0.35")
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


# ---------------------------------------------------------------------------
# Volume handling
# ---------------------------------------------------------------------------

volume_lock = threading.Lock()


def change_volume(direction: str) -> None:
    if direction not in {"+", "-"}:
        logging.error("Invalid volume direction: %r", direction)
        return

    amount = f"{VOLUME_STEP_PERCENT}%{direction}"

    command = [
        WPCTL,
        "set-volume",
        SINK,
        amount,
    ]

    if direction == "+":
        command.extend(["--limit", f"{MAX_VOLUME:g}"])

    try:
        with volume_lock:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )

            if result.returncode != 0:
                message = (result.stderr or result.stdout).strip()
                logging.error(
                    "wpctl failed with code %d: %s",
                    result.returncode,
                    message or "no error text",
                )
                return

            current = subprocess.run(
                [WPCTL, "get-volume", SINK],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )

        if current.returncode == 0 and current.stdout.strip():
            logging.info("%s", current.stdout.strip())
        else:
            logging.info("Volume changed by %s", amount)

    except FileNotFoundError:
        logging.error("wpctl not found at %s", WPCTL)

    except subprocess.TimeoutExpired:
        logging.error("wpctl timed out")


# ---------------------------------------------------------------------------
# Media playback handling
# ---------------------------------------------------------------------------

def run_playerctl(command: str) -> bool:
    """Returns whether the command actually reached and changed a player --
    used by the play/pause handler to only flash the LED ring when it truly
    activated or deactivated playback, not when there's nothing to control."""
    try:
        result = subprocess.run(
            [PLAYERCTL, command],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )

        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()

            logging.warning(
                "playerctl %s failed: %s",
                command,
                message or "no active media player",
            )
            return False

        logging.info("Playback command: %s", command)
        return True

    except FileNotFoundError:
        logging.error(
            "playerctl not found at %s",
            PLAYERCTL,
        )
        return False

    except subprocess.TimeoutExpired:
        logging.error("playerctl %s timed out", command)
        return False


def send_led_flash() -> None:
    """One-shot request to volume_led.py's assistant-state socket for the
    same end-of-session camera-flash pulse, reused here for a play/pause
    button press that actually toggled playback."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(ASSISTANT_LED_SOCKET)
            s.sendall(b"flash\n")
    except OSError as exc:
        logging.debug("Could not reach assistant-led socket: %s", exc)


# ---------------------------------------------------------------------------
# Wake button handling
# ---------------------------------------------------------------------------

def trigger_wake() -> None:
    """Push a manual wake trigger to wake_word.py's socket, so a button press
    goes through the exact same listening/LED/cooldown path as a voice
    detection."""

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(WAKE_TRIGGER_SOCKET)
            s.sendall(b"button\n")

        logging.info("Wake button: triggered")

    except OSError as exc:
        logging.warning(
            "Wake button pressed but could not reach %s: %s",
            WAKE_TRIGGER_SOCKET,
            exc,
        )


class PlaybackButtonHandler:
    """
    AirPods-style playback button:

        single press -> play/pause
        double press -> next track
    """

    def __init__(self, double_press_seconds: float) -> None:
        self.double_press_seconds = double_press_seconds

        self.lock = threading.Lock()
        self.pending_single_press: threading.Timer | None = None

    def pressed(self) -> None:
        with self.lock:

            # A timer already exists, so this is the second press.
            if self.pending_single_press is not None:

                self.pending_single_press.cancel()
                self.pending_single_press = None

                logging.info("Playback button: double press")

                threading.Thread(
                    target=run_playerctl,
                    args=("next",),
                    daemon=True,
                ).start()

                return

            # First press:
            # wait briefly before executing play/pause in case
            # another press arrives.
            timer = threading.Timer(
                self.double_press_seconds,
                self._single_press_timeout,
            )

            timer.daemon = True

            self.pending_single_press = timer
            timer.start()

    def _single_press_timeout(self) -> None:
        with self.lock:
            self.pending_single_press = None

        logging.info("Playback button: single press")

        if run_playerctl("play-pause"):
            send_led_flash()

    def close(self) -> None:
        with self.lock:
            if self.pending_single_press is not None:
                self.pending_single_press.cancel()
                self.pending_single_press = None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    pin_factory = LGPIOFactory()
    Device.pin_factory = pin_factory

    # -----------------------------------------------------------------------
    # Volume buttons
    # -----------------------------------------------------------------------

    volume_up_button = Button(
        VOLUME_UP_GPIO,
        pull_up=True,
        bounce_time=0.08,
        hold_time=0.30,
        hold_repeat=True,
    )

    volume_down_button = Button(
        VOLUME_DOWN_GPIO,
        pull_up=True,
        bounce_time=0.08,
        hold_time=0.30,
        hold_repeat=True,
    )

    volume_up_callback = partial(change_volume, "+")
    volume_down_callback = partial(change_volume, "-")

    volume_up_button.when_pressed = volume_up_callback
    volume_up_button.when_held = volume_up_callback

    volume_down_button.when_pressed = volume_down_callback
    volume_down_button.when_held = volume_down_callback


    # -----------------------------------------------------------------------
    # Playback button
    # -----------------------------------------------------------------------

    playback_button = Button(
        PLAYBACK_GPIO,
        pull_up=True,
        bounce_time=0.08,
    )

    playback_handler = PlaybackButtonHandler(
        DOUBLE_PRESS_SECONDS
    )

    playback_button.when_pressed = playback_handler.pressed


    # -----------------------------------------------------------------------
    # Wake-word / assistant button
    # -----------------------------------------------------------------------

    wake_button = Button(
        WAKE_GPIO,
        pull_up=True,
        bounce_time=0.08,
    )

    wake_button.when_pressed = trigger_wake


    # -----------------------------------------------------------------------
    # Shutdown handling
    # -----------------------------------------------------------------------

    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logging.info("Stopping after signal %d", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


    logging.info(
        "Controller ready: "
        "volume-up=GPIO%d, "
        "volume-down=GPIO%d, "
        "playback=GPIO%d, "
        "wake=GPIO%d",
        VOLUME_UP_GPIO,
        VOLUME_DOWN_GPIO,
        PLAYBACK_GPIO,
        WAKE_GPIO,
    )

    try:
        stop_event.wait()

    finally:
        playback_handler.close()

        volume_up_button.close()
        volume_down_button.close()
        playback_button.close()
        wake_button.close()

        pin_factory.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
