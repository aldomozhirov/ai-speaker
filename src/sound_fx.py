#!/usr/bin/env python3
"""Notification chimes for the AI speaker.

Plays short confirmation sounds (session start/end, Spotify Connect) through
the same aloop sink CamillaDSP reads from -- never hw:sndrpihifiberry
directly, per the guardrail that CamillaDSP is the sole owner of the physical
speaker (see AI-SPEAKER-SETUP-NOTES.md). That sink is also where the
XVF3800's AEC reference is tapped from (xvf-aec-ref.sh), so a chime gets
echo-cancelled out of the mic capture the same way bot speech and music
already are.

ffmpeg decodes the mp3 to raw PCM piped into `pw-cat --raw`, rather than
relying on pw-cat's own (unverified) mp3 support -- matching the --raw
convention pw_transport.py already uses for every other stream in this
stack.

Two entry points:
  - play(): fire-and-forget, backgrounded in a thread with its own reaper.
    Used by all three current callers (session-started, session-ended,
    spotifyd's connect hook) -- nothing here blocks on a chime finishing.
  - play_blocking(): runs synchronously. Unused by anything currently wired
    up (session-started was blocking here originally, deliberately reverted
    2026-08-22 -- it left the mic closed for the chime's ~2s duration right
    after the LED already said "listening", so a user who started talking
    immediately got recorded into silence; see AI-SPEAKER-SETUP-NOTES.md).
    Kept for a caller that genuinely needs to wait for playback to finish.

ffmpeg and pw-cat are invoked by absolute path, not looked up on $PATH --
matching duck.py's convention for the same reason: this runs both under
voice-agent.service (systemd --user) and as a subprocess of spotifyd.service
via spotifyd-event-hook.sh, and pinning the path removes one more variable
from "did this survive a reboot".
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading

LOG = logging.getLogger("voice-agent.sound_fx")

SOUND_DIR = os.environ.get(
    "SOUND_FX_DIR", os.path.expanduser("~/.local/share/ai-speaker/sounds")
)
SOUND_SINK = os.environ.get(
    "SOUND_FX_SINK", "alsa_output.platform-snd_aloop.0.analog-stereo"
)
SOUND_RATE = int(os.environ.get("SOUND_FX_RATE", "48000"))
SOUND_VOLUME = float(os.environ.get("SOUND_FX_VOLUME", "0.6"))
FFMPEG_BIN = os.environ.get("SOUND_FX_FFMPEG", "/usr/bin/ffmpeg")
PW_CAT_BIN = os.environ.get("SOUND_FX_PW_CAT", "/usr/bin/pw-cat")

SOUNDS = {
    "session-started": "session-started.mp3",
    "session-ended": "session-ended.mp3",
    "spotify-connect": "spotify-connect.mp3",
    "welcome": "welcome.mp3",
    "timer": "timer.mp3",
    "alarm": "alarm.mp3",
}


def _run(name: str) -> None:
    filename = SOUNDS.get(name)
    if filename is None:
        LOG.warning("Unknown sound %r", name)
        return
    path = os.path.join(SOUND_DIR, filename)
    if not os.path.isfile(path):
        LOG.warning("Sound file missing: %s", path)
        return

    ffmpeg = None
    pw_cat = None
    try:
        ffmpeg = subprocess.Popen(
            [
                FFMPEG_BIN, "-v", "error", "-i", path,
                "-f", "s16le", "-ar", str(SOUND_RATE), "-ac", "2", "-",
            ],
            stdout=subprocess.PIPE,
        )
        pw_cat = subprocess.Popen(
            [
                PW_CAT_BIN, "--playback", "--raw",
                "--target", SOUND_SINK,
                "--rate", str(SOUND_RATE),
                "--channels", "2",
                "--format", "s16",
                "--volume", f"{SOUND_VOLUME:.3f}",
                "-",
            ],
            stdin=ffmpeg.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Let pw-cat own the read end -- otherwise ffmpeg never sees EPIPE/EOF
        # handled and this process keeps a dangling reference to the pipe.
        ffmpeg.stdout.close()
        pw_cat.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        LOG.exception("Could not play sound %r", name)
    finally:
        for proc in (pw_cat, ffmpeg):
            if proc is None:
                continue
            with contextlib.suppress(Exception):
                if proc.poll() is None:
                    proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)


def play(name: str) -> None:
    """Fire-and-forget: backgrounded in a thread, never blocks the caller."""
    threading.Thread(target=_run, args=(name,), name=f"sound-fx-{name}", daemon=True).start()


def play_blocking(name: str) -> None:
    """Plays synchronously -- see module docstring for why session-started
    needs this instead of play()."""
    _run(name)


class LoopHandle:
    """Handle on a looping playback started by play_loop(), so a caller that
    knows when to stop (timer_alarm.py, once "stop" is heard or the button is
    pressed) can kill it -- unlike play()/play_blocking(), which always run
    to completion on their own."""

    def __init__(self, ffmpeg: subprocess.Popen, pw_cat: subprocess.Popen) -> None:
        self._ffmpeg = ffmpeg
        self._pw_cat = pw_cat

    def stop(self) -> None:
        for proc in (self._pw_cat, self._ffmpeg):
            with contextlib.suppress(Exception):
                if proc.poll() is None:
                    proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)


def play_loop(name: str) -> "LoopHandle | None":
    """Starts `name` looping indefinitely (ffmpeg -stream_loop -1) until
    .stop() is called on the returned handle. Returns None (logging a
    warning, same as _run()) if the sound is unknown or its file is missing
    -- a caller like timer_alarm.py should still flip the LED to "ringing"
    and open the stop-listener even with no audio, rather than treat a
    missing sound file as a hard failure.
    """
    filename = SOUNDS.get(name)
    if filename is None:
        LOG.warning("Unknown sound %r", name)
        return None
    path = os.path.join(SOUND_DIR, filename)
    if not os.path.isfile(path):
        LOG.warning("Sound file missing: %s", path)
        return None

    try:
        ffmpeg = subprocess.Popen(
            [
                FFMPEG_BIN, "-v", "error", "-stream_loop", "-1", "-i", path,
                "-f", "s16le", "-ar", str(SOUND_RATE), "-ac", "2", "-",
            ],
            stdout=subprocess.PIPE,
        )
        pw_cat = subprocess.Popen(
            [
                PW_CAT_BIN, "--playback", "--raw",
                "--target", SOUND_SINK,
                "--rate", str(SOUND_RATE),
                "--channels", "2",
                "--format", "s16",
                "--volume", f"{SOUND_VOLUME:.3f}",
                "-",
            ],
            stdin=ffmpeg.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ffmpeg.stdout.close()
    except OSError:
        LOG.exception("Could not start looped playback for %r", name)
        return None
    return LoopHandle(ffmpeg, pw_cat)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    play_blocking(sys.argv[1] if len(sys.argv) > 1 else "session-started")
