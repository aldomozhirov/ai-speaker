"""What the room sounded like from the speaker's side: is music playing, since
when, and at what volume. Attached to every wake event and capture.

Why it exists: "the wake word is poor over music" has been reported since
August, and the echo canceller needs 10-14 s to converge after music starts,
but no wake event ever recorded whether music was playing. With this on every
event, misses can be split by music / no music and by how long the music had
been running (docs/plans/wake-word-custom-model.md, stages A3 and I).

A background thread asks PipeWire every WAKE_WORD_CONTEXT_SECONDS (pw-dump
for the playback streams, wpctl for the default sink's volume, ~45 ms of CPU
together) and keeps the answer; snapshot() only reads that cached answer, so
the capture loop never waits on a subprocess. Read-only: nothing here changes
the audio graph.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time

LOG = logging.getLogger("wake-word.context")

ENABLED = os.environ.get("WAKE_WORD_CONTEXT", "1").lower() in {"1", "true", "yes"}
INTERVAL_SECONDS = float(os.environ.get("WAKE_WORD_CONTEXT_SECONDS", "3"))
# Same streams voice_agent.py ducks (VOICE_DUCK_MATCH).
MUSIC_MATCH = os.environ.get("WAKE_WORD_MUSIC_MATCH", "spotifyd|librespot")
PW_DUMP = "/usr/bin/pw-dump"
WPCTL = "/usr/bin/wpctl"
_MATCH_PROPS = ("application.process.binary", "application.name", "node.name", "media.name")


def _run(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def music_playing(dump: list, pattern: re.Pattern) -> bool:
    """True when a matching, non-passive playback stream is running."""
    for node in dump:
        if node.get("type") != "PipeWire:Interface:Node":
            continue
        info = node.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio" or props.get("node.passive"):
            continue
        if info.get("state") != "running":
            continue
        if any(isinstance(props.get(k), str) and pattern.search(props[k]) for k in _MATCH_PROPS):
            return True
    return False


def parse_volume(text: str | None) -> float | None:
    match = re.search(r"Volume:\s*([\d.]+)", text or "")
    return float(match.group(1)) if match else None


class Context(threading.Thread):
    def __init__(self, interval: float = INTERVAL_SECONDS, match: str = MUSIC_MATCH) -> None:
        super().__init__(name="wake-context", daemon=True)
        self._interval = interval
        self._pattern = re.compile(match, re.I)
        self._lock = threading.Lock()
        self._music: bool | None = None
        self._music_since: float | None = None
        self._volume: float | None = None

    def snapshot(self) -> dict:
        """Cached, never blocks. Keys are absent when unknown."""
        with self._lock:
            music, since, volume = self._music, self._music_since, self._volume
        out: dict = {}
        if music is not None:
            out["music"] = music
            if music and since is not None:
                out["music_seconds"] = round(time.time() - since)
        if volume is not None:
            out["volume"] = volume
        return out

    def describe(self) -> str:
        """For the journal's `wake event:` line: '' when unknown."""
        snap = self.snapshot()
        parts = []
        if "music" in snap:
            parts.append("music=%s" % ("yes" if snap["music"] else "no"))
            if "music_seconds" in snap:
                parts.append("music_s=%d" % snap["music_seconds"])
        if "volume" in snap:
            parts.append("volume=%.2f" % snap["volume"])
        return (" " + " ".join(parts)) if parts else ""

    def poll_once(self) -> None:
        out = _run([PW_DUMP])
        music = None
        if out is not None:
            try:
                music = music_playing(json.loads(out), self._pattern)
            except ValueError:
                music = None
        volume = parse_volume(_run([WPCTL, "get-volume", "@DEFAULT_AUDIO_SINK@"]))
        now = time.time()
        with self._lock:
            if music and not self._music:
                # Started somewhere in the last interval; the middle is the best guess.
                self._music_since = now - self._interval / 2 if self._music is not None else None
            elif not music:
                self._music_since = None
            self._music = music
            self._volume = volume

    def run(self) -> None:
        while True:
            try:
                self.poll_once()
            except Exception:
                LOG.exception("Could not read the playback state")
            time.sleep(self._interval)


class NullContext:
    def snapshot(self) -> dict:
        return {}

    def describe(self) -> str:
        return ""
