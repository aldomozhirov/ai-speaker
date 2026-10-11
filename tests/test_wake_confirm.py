"""Unit tests for wake_confirm.SecondOpinion's scheduling (no models needed).

    ~/.venvs/wake-word/bin/python -m unittest test_wake_confirm -v
"""

import unittest

import numpy as np

import wake_confirm

FRAME_SECONDS = 0.08


class FakeJudge:
    def __init__(self, passed=True, fail=False):
        self.seen = []
        self._passed, self._fail = passed, fail

    def judge(self, pcm):
        self.seen.append(pcm)
        if self._fail:
            raise RuntimeError("boom")
        return wake_confirm.Opinion(vosk=1.0, micro=0.0, passed=self._passed, seconds=0.01)


def frame(value):
    return np.full(1280, value, dtype=np.int16)


def make(judge, opinions, window=0.4, delay=0.16):
    return wake_confirm.SecondOpinion(
        judge, FRAME_SECONDS, 0.10, opinions.append,
        window_seconds=window, delay_seconds=delay, threaded=False,
    )


class SecondOpinionTest(unittest.TestCase):
    def test_quiet_stream_is_never_judged(self):
        judge, opinions = FakeJudge(), []
        second = make(judge, opinions)
        for index in range(50):
            second.push(frame(index), 0.09)
        self.assertEqual(judge.seen, [])

    def test_judges_the_window_ending_delay_after_the_trigger(self):
        judge, opinions = FakeJudge(), []
        second = make(judge, opinions)  # 5-frame window, 2 frames of delay
        scores = [0.0] * 6 + [0.2] + [0.0] * 6
        for index, score in enumerate(scores):
            second.push(frame(index), score)
        self.assertEqual(len(judge.seen), 1)
        # Trigger at frame 6, judged after frame 8: frames 4..8.
        self.assertEqual([int(v) for v in judge.seen[0][::1280]], [4, 5, 6, 7, 8])
        self.assertTrue(opinions[0].passed)

    def test_one_utterance_over_several_frames_is_judged_once(self):
        judge, opinions = FakeJudge(), []
        second = make(judge, opinions)
        for index, score in enumerate([0.0, 0.2, 0.4, 0.3, 0.0, 0.0, 0.0]):
            second.push(frame(index), score)
        self.assertEqual(len(judge.seen), 1)

    def test_a_later_utterance_is_judged_again(self):
        judge, opinions = FakeJudge(), []
        second = make(judge, opinions)
        for index, score in enumerate([0.2, 0, 0, 0, 0, 0, 0.3, 0, 0, 0]):
            second.push(frame(index), score)
        self.assertEqual(len(judge.seen), 2)

    def test_a_failing_judge_does_not_reach_the_caller(self):
        judge, opinions = FakeJudge(fail=True), []
        second = make(judge, opinions)
        with self.assertLogs("wake-word.second-opinion", level="ERROR"):
            for index, score in enumerate([0.2, 0, 0, 0]):
                second.push(frame(index), score)
        self.assertEqual(opinions, [])

    def test_log_line_carries_the_peak_score_and_the_detectors_verdict(self):
        judge, opinions = FakeJudge(passed=False), []
        second = make(judge, opinions)
        with self.assertLogs("wake-word.second-opinion", level="INFO") as logs:
            for index, (score, verdict) in enumerate([(0.2, "rejected"), (0.4, "ok"), (0.0, ""), (0.0, "")]):
                second.push(frame(index), score, verdict)
        self.assertIn("-> block", logs.output[0])
        self.assertIn("raw_score=0.400 detector=ok", logs.output[0])

    def test_decide_judges_the_window_after_the_call_and_reports_back(self):
        judge, opinions, decisions = FakeJudge(passed=False), [], []
        second = make(judge, opinions)  # 5-frame window, 2 frames of delay
        for index in range(7):
            second.push(frame(index), 0.2 if index == 6 else 0.0)
        second.decide(0.2, decisions.append)  # as wake_word.py does, after frame 6
        for index in range(7, 12):
            second.push(frame(index), 0.0)
        self.assertEqual(len(judge.seen), 1)  # took over the window that was waiting
        self.assertEqual([int(v) for v in judge.seen[0][::1280]], [5, 6, 7, 8, 9])
        self.assertEqual([d.passed for d in decisions], [False])

    def test_decide_reports_none_when_judging_fails(self):
        judge, opinions, decisions = FakeJudge(fail=True), [], []
        second = make(judge, opinions)
        second.push(frame(0), 0.2)
        second.decide(0.2, decisions.append)
        with self.assertLogs("wake-word.second-opinion", level="ERROR"):
            for index in range(1, 6):
                second.push(frame(index), 0.0)
        self.assertEqual(decisions, [None])

    def test_a_later_plain_trigger_does_not_report_to_an_old_decision(self):
        judge, opinions, decisions = FakeJudge(), [], []
        second = make(judge, opinions)
        second.push(frame(0), 0.2)
        second.decide(0.2, decisions.append)
        for index, score in enumerate([0, 0, 0, 0, 0, 0.3, 0, 0, 0], start=1):
            second.push(frame(index), score)
        self.assertEqual(len(judge.seen), 2)
        self.assertEqual(len(decisions), 1)

    def test_off_by_default(self):
        self.assertEqual(wake_confirm.MODE, "0")
        self.assertIsNone(wake_confirm.SecondOpinion.load(FRAME_SECONDS, 0.10))


if __name__ == "__main__":
    unittest.main()
