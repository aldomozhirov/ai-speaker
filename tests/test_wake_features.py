"""Unit tests for the feature log (wake_features.py), the two capture pools
(wake_capture.py) and the playback context (wake_context.py).

    cd tests && PYTHONPATH=../src ~/.venvs/wake-word/bin/python -m unittest test_wake_features -v
"""

import json
import os
import queue
import re
import tempfile
import time
import unittest

import numpy as np

import wake_capture
import wake_context
import wake_features

FRAME = 0.08


class Clock:
    def __init__(self, t=1_763_200_000.0):
        self.t = t

    def __call__(self):
        return self.t


def rows(n, start=0):
    return [(np.full(96, i, dtype=np.float32), i / 1000.0) for i in range(start, start + n)]


class FeatureLogTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = self._dir.name
        self.clock = Clock()

    def log(self, **kw):
        kw.setdefault("batch_rows", 10)
        return wake_features.FeatureLog(FRAME, directory=self.dir, clock=self.clock, **kw)

    def files(self):
        return sorted(f for f in os.listdir(self.dir) if f.endswith(".f16"))

    def test_rows_are_written_in_order_with_their_score(self):
        log = self.log()
        for f, s in rows(25):
            log.push(f, s)
        log.flush()
        log.drain()
        (name,) = self.files()
        self.assertEqual(name, time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock.t)) + ".f16")
        data = wake_features.read(os.path.join(self.dir, name))
        self.assertEqual(data.shape, (25, 97))
        np.testing.assert_allclose(data[:, 0], np.arange(25))
        np.testing.assert_allclose(data[:, 96], np.arange(25) / 1000.0, atol=1e-3)

    def test_a_new_file_every_segment_and_at_every_flush(self):
        log = self.log(segment_seconds=10 * FRAME)
        for f, s in rows(25):
            log.push(f, s)
            self.clock.t += FRAME
        log.flush()  # capture restart
        self.clock.t += 5
        for f, s in rows(3, 100):
            log.push(f, s)
        log.flush()
        log.drain()
        sizes = [len(wake_features.read(os.path.join(self.dir, n))) for n in self.files()]
        self.assertEqual(sizes, [10, 10, 5, 3])

    def test_two_segments_in_the_same_second_stay_apart(self):
        log = self.log()
        for f, s in rows(4):
            log.push(f, s)
        log.flush()
        for f, s in rows(6, 50):
            log.push(f, s)
        log.flush()
        log.drain()
        names = self.files()
        self.assertEqual(len(names), 2)
        self.assertTrue(names[1].endswith("_2.f16"), names)

    def test_a_dropped_batch_starts_a_new_file_instead_of_leaving_a_gap(self):
        log = self.log()
        log._queue = queue.Queue()  # the second hand-over is refused below
        real_put = log._queue.put_nowait
        calls = []

        def put(item):
            calls.append(item)
            if len(calls) == 2:
                raise queue.Full
            real_put(item)

        log._queue.put_nowait = put
        log._writer = None
        for f, s in rows(30):
            log.push(f, s)
        self.assertEqual(log.dropped_batches, 1)
        segments = [seg for seg, _ in calls]
        self.assertEqual(segments[0], segments[1])
        self.assertNotEqual(segments[1], segments[2])

    def test_oldest_files_are_pruned_at_the_cap(self):
        log = self.log(max_mb=3 * 10 * 97 * 2 / 1024 / 1024)  # room for three batches
        for i in range(5):
            for f, s in rows(10, i * 10):
                log.push(f, s)
            log.flush()
            log.drain()
            self.clock.t += 60
        names = self.files()
        self.assertEqual(len(names), 3)
        first = wake_features.read(os.path.join(self.dir, names[0]))
        self.assertEqual(first[0, 0], 20)


class CapturePoolTest(unittest.TestCase):
    def test_near_miss_names(self):
        near = wake_capture.is_near_miss
        self.assertTrue(near("20261115-101010-near-0072", 0.10))
        self.assertTrue(near("20261115-101010-near-0099-2", 0.10))
        self.assertFalse(near("20261115-101010-near-0100", 0.10))  # verifier-rejected trigger
        self.assertFalse(near("20261115-101010-near-0302", 0.10))
        self.assertFalse(near("20261115-101010-wake-0050", 0.10))
        self.assertFalse(near("20261115-101010-button-0000", 0.10))
        self.assertFalse(near("20261115-101010-near-0072", 0.0))  # pools off

    def test_near_misses_rotate_without_touching_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "kept"))
            open(os.path.join(d, "kept", "20200101-000000-near-0060.wav"), "wb").write(b"x" * 1000)
            stems = [f"20261115-10{m:02d}00-near-0070" for m in range(6)]
            stems += [f"20261115-11{m:02d}00-wake-0300" for m in range(3)]
            for i, stem in enumerate(stems):
                for ext in (".wav", ".json"):
                    path = os.path.join(d, stem + ext)
                    open(path, "wb").write(b"x" * 500)
                    os.utime(path, (1000 + i, 1000 + i))
            rec = wake_capture.CaptureRecorder.__new__(wake_capture.CaptureRecorder)
            rec._dir = d
            rec._near_below = 0.10
            rec._near_max_bytes = 2 * 1000
            rec._max_bytes = 100 * 1000
            rec._prune()
            left = sorted(f[:-4] for f in os.listdir(d) if f.endswith(".wav"))
            self.assertEqual(left, stems[4:])
            self.assertTrue(os.path.exists(os.path.join(d, "kept", "20200101-000000-near-0060.wav")))


class ContextTest(unittest.TestCase):
    def node(self, state, passive=False, **props):
        props = {"media.class": "Stream/Output/Audio", **props}
        if passive:
            props["node.passive"] = True
        return {"type": "PipeWire:Interface:Node", "info": {"state": state, "props": props}}

    def test_music_is_a_running_matching_stream(self):
        pattern = re.compile("spotifyd|librespot", re.I)
        playing = wake_context.music_playing
        self.assertTrue(playing([self.node("running", **{"application.process.binary": "spotifyd"})], pattern))
        self.assertFalse(playing([self.node("idle", **{"application.process.binary": "spotifyd"})], pattern))
        self.assertFalse(playing([self.node("running", **{"application.name": "voice-agent"})], pattern))
        self.assertFalse(playing([self.node("running", passive=True, **{"node.name": "spotifyd"})], pattern))
        self.assertFalse(playing([], pattern))

    def test_volume(self):
        self.assertEqual(wake_context.parse_volume("Volume: 0.34\n"), 0.34)
        self.assertEqual(wake_context.parse_volume("Volume: 0.34 [MUTED]\n"), 0.34)
        self.assertIsNone(wake_context.parse_volume(None))

    def test_snapshot_tracks_when_music_started(self):
        ctx = wake_context.Context(interval=2)
        answers = iter([
            (json.dumps([]), "Volume: 0.30"),
            (json.dumps([self.node("running", **{"node.name": "spotifyd"})]), "Volume: 0.30"),
        ])
        orig = wake_context._run
        current = {}

        def fake_run(argv):
            if argv[0] == wake_context.PW_DUMP:
                current["pair"] = next(answers)
                return current["pair"][0]
            return current["pair"][1]

        wake_context._run = fake_run
        self.addCleanup(setattr, wake_context, "_run", orig)
        self.assertEqual(ctx.describe(), "")
        ctx.poll_once()
        self.assertEqual(ctx.snapshot(), {"music": False, "volume": 0.30})
        self.assertEqual(ctx.describe(), " music=no volume=0.30")
        ctx.poll_once()
        snap = ctx.snapshot()
        self.assertTrue(snap["music"])
        self.assertLessEqual(snap["music_seconds"], 1)
        self.assertIn("music=yes music_s=", ctx.describe())


if __name__ == "__main__":
    unittest.main()
