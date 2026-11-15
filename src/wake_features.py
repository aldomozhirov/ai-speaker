"""A continuous log of openWakeWord's speech features, for training and
evaluating wake-word models on everything the speaker really hears.

Why it exists: a wake-word model is only as good as the false alarms it was
shown. Captures keep audio around events only (score > 0.05), so they say
nothing about the hours of room sound that never came near the threshold --
which is exactly what a new model must stay quiet on. openWakeWord turns every
80 ms of audio into 96 numbers (an embedding of the last ~1 s of speech
sounds); any model built on openWakeWord, including its own pretrained heads,
reads only those. Logging them lets a new model be replayed over weeks of
this room and its false alarms counted, without keeping any audio. The
numbers cannot be played back as sound. They still stay on this machine
(docs/plans/wake-word-custom-model.md, stage A2).

Format: headerless little-endian float16 rows of 97 values -- the 96 features
of one 80 ms frame, then that frame's raw base-model score. One file per
segment, named after the wall-clock time of its first row:

    WAKE_WORD_FEATURE_LOG_DIR/YYYYMMDD-HHMMSS[_n].f16

Rows inside a file are consecutive 80 ms frames of one capture stream with
none missing, so row n is at (file time + n * 0.08 s) and any 16 consecutive
rows are exactly the window openWakeWord's heads score. A new file starts
every hour, at every capture (re)start, and after any dropped batch.

Nothing here may block the capture loop: push() appends to a list; a batch
of rows goes to a writer thread through a bounded queue that drops (and
starts a new file) rather than waits.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import threading
import time

import numpy as np

LOG = logging.getLogger("wake-word.features")

ENABLED = os.environ.get("WAKE_WORD_FEATURE_LOG", "0").lower() in {"1", "true", "yes"}
DIRECTORY = os.environ.get(
    "WAKE_WORD_FEATURE_LOG_DIR", os.path.expanduser("~/.local/share/ai-speaker/wake-features")
)
# About 210 MB a day (97 x 2 bytes x 12.5 rows a second); 12000 is about 8 weeks.
MAX_MB = float(os.environ.get("WAKE_WORD_FEATURE_LOG_MAX_MB", "12000"))

FEATURES = 96
ROW_VALUES = FEATURES + 1
SEGMENT_SECONDS = 3600.0
BATCH_ROWS = 125  # 10 s of frames per hand-over to the writer
SUFFIX = ".f16"


def read(path: str) -> np.ndarray:
    """A log file as an (n, 97) float32 array: features in [:, :96], score in [:, 96]."""
    data = np.fromfile(path, dtype="<f2")
    usable = len(data) // ROW_VALUES * ROW_VALUES
    return data[:usable].reshape(-1, ROW_VALUES).astype(np.float32)


class FeatureLog:
    """push() is called from the capture thread only; the rest is internal."""

    def __init__(
        self,
        frame_seconds: float,
        directory: str = DIRECTORY,
        max_mb: float = MAX_MB,
        segment_seconds: float = SEGMENT_SECONDS,
        batch_rows: int = BATCH_ROWS,
        clock=time.time,
    ) -> None:
        self._frame_seconds = frame_seconds
        self._dir = directory
        self._max_bytes = int(max_mb * 1024 * 1024)
        self._segment_rows = max(1, round(segment_seconds / frame_seconds))
        self._batch_rows = max(1, batch_rows)
        self._clock = clock

        # (time stamp, serial) of the file being filled; the serial keeps two
        # segments started in the same second apart.
        self._segment: tuple[str, int] | None = None
        self._serial = 0
        self._names: dict = {}  # writer thread only: segment -> path
        self._segment_count = 0
        self._rows: list[np.ndarray] = []
        self._last_drop_log = 0.0
        self.dropped_batches = 0

        self._queue: queue.Queue = queue.Queue(maxsize=16)
        self._writer = threading.Thread(target=self._write_loop, name="wake-features-writer", daemon=True)
        self._writer.start()

    # -- capture thread -----------------------------------------------------

    def push(self, features: np.ndarray, score: float) -> None:
        """One frame: the newest feature row (96 values) and its base score."""
        if self._segment is None:
            self._start_segment()
        row = np.empty(ROW_VALUES, dtype="<f2")
        row[:FEATURES] = features
        row[FEATURES] = score
        self._rows.append(row)
        self._segment_count += 1
        if len(self._rows) >= self._batch_rows:
            self._hand_over()
        if self._segment_count >= self._segment_rows:
            self.flush()

    def flush(self) -> None:
        """End the current file. Called when the capture stream ends, so a
        restart does not splice two streams into one file."""
        if self._rows:
            self._hand_over()
        self._segment = None
        self._segment_count = 0

    def _start_segment(self) -> None:
        self._serial += 1
        self._segment = (time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock())), self._serial)
        self._segment_count = 0

    def _hand_over(self) -> None:
        batch = np.stack(self._rows)
        self._rows = []
        try:
            self._queue.put_nowait((self._segment, batch))
        except queue.Full:
            # A gap inside a file would break "row n is at n * 80 ms".
            self.dropped_batches += 1
            now = time.monotonic()
            if now - self._last_drop_log > 600:
                self._last_drop_log = now
                LOG.warning("Feature log writer is backed up; dropped %d s of features",
                            round(len(batch) * self._frame_seconds))
            self._segment = None
            self._segment_count = 0

    # -- writer thread ------------------------------------------------------

    def _write_loop(self) -> None:
        current = None
        while True:
            item = self._queue.get()
            if item is None:
                return
            segment, batch = item
            try:
                os.makedirs(self._dir, mode=0o700, exist_ok=True)
                path = self._path_for(segment)
                with open(path, "ab") as f:
                    f.write(batch.tobytes())
                if segment != current:
                    current = segment
                    self._prune()
            except Exception:
                LOG.exception("Could not write the feature log")
            finally:
                self._queue.task_done()

    def _path_for(self, segment: tuple[str, int]) -> str:
        # A segment is named once, when its first batch arrives; one that
        # starts in the same second as another gets a suffix.
        names = self._names
        if segment not in names:
            base = os.path.join(self._dir, segment[0])
            path, n = base + SUFFIX, 1
            while os.path.exists(path):
                n += 1
                path = f"{base}_{n}{SUFFIX}"  # "_" sorts after ".", so names stay in time order
            names[segment] = path
            if len(names) > 64:
                names.pop(next(iter(names)))
        return names[segment]

    def _prune(self) -> None:
        files = []
        with os.scandir(self._dir) as it:
            for entry in it:
                if entry.name.endswith(SUFFIX) and entry.is_file():
                    st = entry.stat()
                    files.append((entry.name, st.st_size, entry.path))
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):  # names sort by time
            if total <= self._max_bytes:
                break
            with contextlib.suppress(OSError):
                os.unlink(path)
            total -= size

    def drain(self, timeout: float = 5.0) -> None:
        """Wait until everything handed over is on disk (tests and shutdown)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)


class NullFeatureLog:
    """Stand-in when the log is off, so callers never branch."""

    def push(self, features, score: float) -> None:
        pass

    def flush(self) -> None:
        pass
