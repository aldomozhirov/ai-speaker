"""Near-trigger audio capture for wake-word tuning.

Every threshold decision on this box so far was made from journal lines: a
score, with no way to hear what produced it or to replay it against a
different setting. This module keeps the audio. wake_word.py hands it every
captured frame together with that frame's raw openWakeWord score; whenever a
score rises above WAKE_WORD_CAPTURE_SCORE it saves the surrounding audio --
a few seconds before (so the whole phrase is there, not just the frame that
crossed) and a few seconds after (the command that followed) -- as a WAV with
*both* XVF3800 capture channels, plus a JSON sidecar holding the per-frame
score trajectory. That gives a corpus of real hits, near misses and false
accepts that can be replayed offline (see tools/wake/wake-replay.py).

A wake word that scores below WAKE_WORD_CAPTURE_SCORE leaves no trace by
score alone, and that is exactly the "it didn't hear me" case. The usual
fallback after a miss is the button, so an external trigger saves the last
WAKE_WORD_CAPTURE_BUTTON_SECONDS as well.

**Off by default** (WAKE_WORD_CAPTURE=0): this writes room audio to disk.
Files stay on this machine, in a 0700 directory, and the oldest are deleted
once the directory exceeds WAKE_WORD_CAPTURE_MAX_MB.

Nothing here may ever block the capture loop: push() only moves references
around, and the disk write happens on a separate thread behind a bounded
queue that drops (with a warning) rather than waits.
"""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import os
import queue
import threading
import time
import wave

LOG = logging.getLogger("wake-word.capture")

ENABLED = os.environ.get("WAKE_WORD_CAPTURE", "0").lower() in {"1", "true", "yes"}
DIRECTORY = os.environ.get(
    "WAKE_WORD_CAPTURE_DIR", os.path.expanduser("~/.local/share/ai-speaker/wake-captures")
)
# Same level WAKE_WORD_DEBUG starts journalling at, so every score line in the
# journal has audio behind it.
SCORE = float(os.environ.get("WAKE_WORD_CAPTURE_SCORE", "0.05"))
PRE_SECONDS = float(os.environ.get("WAKE_WORD_CAPTURE_PRE_SECONDS", "3.0"))
POST_SECONDS = float(os.environ.get("WAKE_WORD_CAPTURE_POST_SECONDS", "5.0"))
# 0 disables the button snapshot.
BUTTON_SECONDS = float(os.environ.get("WAKE_WORD_CAPTURE_BUTTON_SECONDS", "10.0"))
# Hard cap on one file, for a score that never settles (e.g. loud speech radio).
MAX_SECONDS = float(os.environ.get("WAKE_WORD_CAPTURE_MAX_SECONDS", "20.0"))
MAX_MB = float(os.environ.get("WAKE_WORD_CAPTURE_MAX_MB", "200"))


class CaptureRecorder:
    """Rolling pre-roll buffer plus an event recorder.

    push() and flush() are called from the capture thread only;
    request_snapshot() and annotate() may be called from any thread.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        frame_samples: int,
        meta: dict | None = None,
        directory: str = DIRECTORY,
        score: float = SCORE,
        pre_seconds: float = PRE_SECONDS,
        post_seconds: float = POST_SECONDS,
        snapshot_seconds: float = BUTTON_SECONDS,
        max_seconds: float = MAX_SECONDS,
        max_mb: float = MAX_MB,
    ) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._frame_seconds = frame_samples / sample_rate
        self._meta = dict(meta or {})
        self._dir = directory
        self._score = score

        def frames(seconds: float) -> int:
            return max(1, round(seconds / self._frame_seconds))

        self._pre = frames(pre_seconds)
        self._post = frames(post_seconds)
        self._snapshot_pre = frames(snapshot_seconds) if snapshot_seconds > 0 else 0
        self._max = frames(max_seconds)
        self._max_bytes = int(max_mb * 1024 * 1024)

        self._ring: collections.deque = collections.deque(
            maxlen=max(self._pre, self._snapshot_pre)
        )
        self._event: dict | None = None
        self._lock = threading.Lock()
        self._snapshot: str | None = None
        self._notes: dict = {}

        self._queue: queue.Queue = queue.Queue(maxsize=8)
        self._writer = threading.Thread(
            target=self._write_loop, name="wake-capture-writer", daemon=True
        )
        self._writer.start()

    # -- any thread ---------------------------------------------------------

    def request_snapshot(self, reason: str) -> None:
        """Save what is already in the pre-roll buffer, e.g. on a button
        press. Picked up by the next push()."""
        if self._snapshot_pre <= 0:
            return
        # `reason` is whatever a socket client sent and ends up in a filename.
        reason = "".join(c for c in reason.lower() if c.isalnum() or c in "-_")[:16] or "external"
        with self._lock:
            self._snapshot = reason

    def annotate(self, **notes) -> None:
        """Attach facts to the event being recorded (e.g. relayed=True)."""
        with self._lock:
            self._notes.update(notes)

    # -- capture thread -----------------------------------------------------

    def push(self, raw: bytes, score: float, fired: bool = False) -> None:
        with self._lock:
            snapshot, self._snapshot = self._snapshot, None
        hot = fired or score > self._score
        event = self._event
        if event is None and (hot or snapshot):
            pre = list(self._ring)
            if not snapshot:
                pre = pre[-self._pre:]
            event = self._event = {
                "t0": time.time() - len(pre) * self._frame_seconds,
                "frames": [r for r, _ in pre],
                "scores": [s for _, s in pre],
                "fired": [],
                "marks": [],
                "last_hot": len(pre),
            }
            with self._lock:
                self._notes = {}
        if event is not None:
            index = len(event["frames"])
            event["frames"].append(raw)
            event["scores"].append(score)
            if hot or snapshot:
                event["last_hot"] = index
            if snapshot:
                event["marks"].append([index, snapshot])
            if fired:
                event["fired"].append(index)
            if index - event["last_hot"] >= self._post or index + 1 >= self._max:
                self.flush()
        self._ring.append((raw, score))

    def flush(self) -> None:
        """Hand the event in progress (if any) to the writer. Also called when
        the capture stream ends, so a restart doesn't splice two streams into
        one file."""
        event, self._event = self._event, None
        if event is None:
            return
        with self._lock:
            event["notes"], self._notes = self._notes, {}
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            LOG.warning("Capture writer is backed up; dropping one capture")

    # -- writer thread ------------------------------------------------------

    def _write_loop(self) -> None:
        while True:
            event = self._queue.get()
            try:
                self._write(event)
                self._prune()
            except Exception:
                LOG.exception("Could not save wake capture")

    def _write(self, event: dict) -> None:
        os.makedirs(self._dir, mode=0o700, exist_ok=True)
        scores = event["scores"]
        peak = max(scores) if scores else 0.0
        kind = "wake" if event["fired"] else (event["marks"][0][1] if event["marks"] else "near")
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(event["t0"]))
        base = f"{stamp}-{kind}-{int(round(peak * 1000)):04d}"
        path = os.path.join(self._dir, base)
        suffix = 1
        while os.path.exists(path + ".wav"):
            suffix += 1
            path = os.path.join(self._dir, f"{base}-{suffix}")

        tmp = path + ".wav.tmp"
        with wave.open(tmp, "wb") as w:
            w.setnchannels(self._channels)
            w.setsampwidth(2)
            w.setframerate(self._sample_rate)
            w.writeframes(b"".join(event["frames"]))
        os.replace(tmp, path + ".wav")

        peak_frame = scores.index(peak) if scores else 0
        sidecar = {
            "version": 1,
            "kind": kind,
            "start_time": event["t0"],
            "start_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(event["t0"])),
            "sample_rate": self._sample_rate,
            "channels": self._channels,
            "frame_seconds": self._frame_seconds,
            "capture_score": self._score,
            "peak": round(peak, 4),
            "peak_seconds": round(peak_frame * self._frame_seconds, 2),
            "fired_frames": event["fired"],
            "marks": event["marks"],
            "notes": event.get("notes", {}),
            "scores": [round(float(s), 4) for s in scores],
            **self._meta,
        }
        with open(path + ".json.tmp", "w") as f:
            json.dump(sidecar, f)
        os.replace(path + ".json.tmp", path + ".json")
        LOG.info(
            "Saved capture %s.wav (%s, %.1fs, peak=%.3f at +%.2fs)",
            path,
            kind,
            len(scores) * self._frame_seconds,
            peak,
            peak_frame * self._frame_seconds,
        )

    def _prune(self) -> None:
        """Delete the oldest captures (WAV and sidecar together) until the
        directory is back under the size cap."""
        captures: dict[str, list] = {}
        with os.scandir(self._dir) as it:
            for entry in it:
                stem, ext = os.path.splitext(entry.name)
                if ext not in (".wav", ".json") or not entry.is_file():
                    continue
                st = entry.stat()
                group = captures.setdefault(stem, [st.st_mtime, 0, []])
                group[0] = min(group[0], st.st_mtime)
                group[1] += st.st_size
                group[2].append(entry.path)
        total = sum(size for _, size, _ in captures.values())
        for _, size, paths in sorted(captures.values()):
            if total <= self._max_bytes:
                break
            for path in paths:
                with contextlib.suppress(OSError):
                    os.unlink(path)
            total -= size


class NullRecorder:
    """Stand-in when capture is disabled, so callers never branch."""

    def request_snapshot(self, reason: str) -> None:
        pass

    def annotate(self, **notes) -> None:
        pass

    def push(self, raw: bytes, score: float, fired: bool = False) -> None:
        pass

    def flush(self) -> None:
        pass
