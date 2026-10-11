"""Stream-level ducking for the AI speaker (Phase 7).

Drops the media player's *stream* volume for the duration of a voice session,
then restores it. Deliberately stream-level:

  - CamillaDSP's volume is global safety headroom for the tweeter and must
    never be touched for ducking.
  - The default sink's volume is the user's physical volume knob and
    volume_sync.py's exclusive domain (it mirrors that value into MPRIS for
    Spotify Connect). volume_sync.py only ever drives @DEFAULT_AUDIO_SINK@, so
    per-stream changes here don't fight it and don't move the slider in the
    Spotify app.

Ducking a stream also ducks it in the XVF3800's AEC reference for free: the
reference is tapped from the aloop sink's *monitor* (20-xvf-aec-ref.conf),
which is downstream of per-stream volume, so what the echo canceller is told
was played still matches what actually was.

The media player here is spotifyd, which uses librespot's PulseAudio backend,
so the stream shows up with application.process.binary=spotifyd and an
application.name librespot chooses. Matching is a case-insensitive regex over
several props rather than one exact name, so it survives that detail changing
and covers a second player later without a code change.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time

LOG = logging.getLogger("voice-agent.duck")

# Props checked against the match pattern, most specific first.
_MATCH_PROPS = (
    "application.process.binary",
    "application.name",
    "node.name",
    "media.name",
)


def _run(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=2
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        LOG.error("%s failed: %s", argv[0], error)
        return None
    if result.returncode != 0:
        LOG.error("%s failed: %s", " ".join(argv[:2]), result.stderr.strip())
        return None
    return result.stdout


class Ducker:
    """Ducks every matching playback stream for the life of a session.

    Node IDs are resolved fresh on every duck -- the stream is destroyed and
    recreated whenever playback stops and starts, so a cached ID is wrong more
    often than it's right.
    """

    STATE_KEY = "ducked"

    def __init__(
        self,
        state,
        level: float = 0.20,
        ramp_ms: float = 200.0,
        pattern: str = "spotifyd|librespot",
        wpctl: str = "/usr/bin/wpctl",
        pw_dump: str = "/usr/bin/pw-dump",
    ) -> None:
        self._state = state
        self._level = level
        self._ramp = max(0.0, ramp_ms / 1000.0)
        self._pattern = re.compile(pattern, re.IGNORECASE)
        self._wpctl = wpctl
        self._pw_dump = pw_dump
        self._ducked: dict[int, float] = {}

    # --- PipeWire plumbing ---------------------------------------------------

    def _find_streams(self) -> list[tuple[int, str]]:
        out = _run([self._pw_dump])
        if out is None:
            return []
        try:
            nodes = json.loads(out)
        except ValueError:
            LOG.error("Could not parse pw-dump output")
            return []

        found = []
        for node in nodes:
            if node.get("type") != "PipeWire:Interface:Node":
                continue
            props = (node.get("info") or {}).get("props") or {}
            if props.get("media.class") != "Stream/Output/Audio":
                continue
            if props.get("node.passive"):
                # Infrastructure links, not something anyone is listening to.
                # The XVF3800 AEC reference loopback is one of these; ducking
                # it would silently break echo cancellation by making the
                # reference quieter than what actually reached the speakers.
                # Guarded here rather than left to the match pattern so a
                # loosened VOICE_DUCK_MATCH can't reach it.
                continue
            for key in _MATCH_PROPS:
                value = props.get(key)
                if isinstance(value, str) and self._pattern.search(value):
                    found.append((node["id"], f"{key}={value}"))
                    break
        return found

    def _get_volume(self, node_id: int) -> float | None:
        out = _run([self._wpctl, "get-volume", str(node_id)])
        if out is None:
            return None
        match = re.search(r"Volume:\s+([0-9]+(?:\.[0-9]+)?)", out)
        if match is None:
            LOG.error("Unexpected wpctl output: %r", out.strip())
            return None
        return float(match.group(1))

    def _set_volume(self, node_id: int, volume: float) -> None:
        _run([self._wpctl, "set-volume", str(node_id), f"{volume:.4f}"])

    def _ramp_to(self, targets: dict[int, float], steps: int = 8) -> None:
        """Glide each stream to its target. A single set-volume is an
        instantaneous gain change and audibly clicks on music."""
        starts = {node_id: self._get_volume(node_id) for node_id in targets}
        starts = {k: v for k, v in starts.items() if v is not None}
        if not starts:
            return
        if self._ramp <= 0:
            for node_id, target in targets.items():
                self._set_volume(node_id, target)
            return
        delay = self._ramp / steps
        for step in range(1, steps + 1):
            fraction = step / steps
            for node_id, start in starts.items():
                self._set_volume(node_id, start + (targets[node_id] - start) * fraction)
            time.sleep(delay)

    # --- session API ---------------------------------------------------------

    def duck(self) -> None:
        """Drop every matching stream to the duck level. No-op when nothing is
        playing, which is the common case and must stay silent."""
        streams = self._find_streams()
        if not streams:
            LOG.debug("Nothing to duck")
            return

        originals: dict[int, float] = {}
        for node_id, label in streams:
            volume = self._get_volume(node_id)
            if volume is None:
                continue
            if volume <= self._level:
                # Already quieter than we'd duck it to; leave it alone rather
                # than turning it *up* on restore.
                LOG.info("Stream %d (%s) already at %.2f; not ducking", node_id, label, volume)
                continue
            originals[node_id] = volume
            LOG.info("Ducking stream %d (%s): %.2f -> %.2f", node_id, label, volume, self._level)

        if not originals:
            return

        self._ducked = originals
        self._state.set(
            self.STATE_KEY,
            {"level": self._level, "streams": {str(k): v for k, v in originals.items()}},
        )
        self._ramp_to({node_id: self._level for node_id in originals})

    def restore(self) -> None:
        live_streams = self._find_streams()
        live = {node_id for node_id, _ in live_streams}
        targets = {k: v for k, v in self._ducked.items() if k in live}
        gone = set(self._ducked) - live
        if gone:
            # The stream we ducked was torn down mid-session -- e.g. a
            # track-change tool call makes spotifyd recreate its PipeWire
            # stream under a new node ID.
            LOG.info("Stream(s) %s gone before restore", sorted(gone))

        # A live matching stream sitting at the duck level isn't necessarily
        # one *this* session ducked: WirePlumber carries the last volume
        # over when a stream is torn down and recreated (e.g. by the track
        # change above), so a replacement can come up already stuck at the
        # duck level with nothing tracking it -- including in a session that
        # never called duck() at all, if duck() found it already there and
        # (correctly) left it alone. Catch those too rather than leaving
        # them silently stuck.
        fallback_original = next(iter(self._ducked.values()), 1.0)
        for node_id in live - set(targets):
            current = self._get_volume(node_id)
            if current is not None and abs(current - self._level) <= 0.02:
                targets[node_id] = fallback_original

        if targets:
            LOG.info("Restoring %d ducked stream(s)", len(targets))
            self._ramp_to(targets)
        self._ducked = {}
        self._state.clear(self.STATE_KEY)

    def recover(self) -> None:
        """Undo a duck left behind by a session that died mid-flight.

        Only restores a stream that still exists *and* is still sitting at the
        level we ducked it to -- if it has moved since, the recorded volume is
        stale and stomping it would be worse than doing nothing. Same reasoning
        as DspProfileSwitcher.recover().
        """
        record = self._state.get(self.STATE_KEY)
        if not record:
            return
        level = record.get("level", self._level)
        recorded = {int(k): v for k, v in (record.get("streams") or {}).items()}
        live = {node_id for node_id, _ in self._find_streams()}

        targets = {}
        for node_id, original in recorded.items():
            if node_id not in live:
                continue
            current = self._get_volume(node_id)
            if current is None:
                continue
            if abs(current - level) > 0.02:
                LOG.info("Stream %d moved to %.2f since; not restoring", node_id, current)
                continue
            targets[node_id] = original

        if targets:
            LOG.warning("Previous session left %d stream(s) ducked; restoring", len(targets))
            self._ramp_to(targets)
        self._state.clear(self.STATE_KEY)


class _NullState:
    """State sink for the CLI below, which must not touch the live session's
    state file."""

    def get(self, key, default=None):
        return default

    def set(self, key, value) -> None:
        pass

    def clear(self, key) -> None:
        pass


if __name__ == "__main__":
    # Self-check: show which streams VOICE_DUCK_MATCH would actually duck.
    # Run this with music playing to confirm the pattern matches the player
    # before trusting a conversation to it.
    import os
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    pattern = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "VOICE_DUCK_MATCH", "spotifyd|librespot"
    )
    ducker = Ducker(_NullState(), pattern=pattern)
    matches = ducker._find_streams()
    print(f"pattern: {pattern!r}")
    if not matches:
        print("no matching playback streams (is anything playing?)")
    for node_id, label in matches:
        print(f"  node {node_id}: {label}  volume={ducker._get_volume(node_id)}")
