"""Unit tests for WakeTrigger's tentative relay (wake_word.py).

    PYTHONPATH=~/.local/bin ~/.venvs/wake-word/bin/python -m unittest test_wake_trigger -v
"""

import os
import socket
import tempfile
import threading
import unittest

import wake_confirm
import wake_word


class Agent(threading.Thread):
    """Stands in for voice_agent.py's session socket."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.received = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(path)
        self._server.listen(4)
        self._server.settimeout(0.3)

    def run(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                self.received.append(conn.recv(256).decode().strip())


def opinion(passed):
    return wake_confirm.Opinion(vosk=0.0, micro=0.0, passed=passed, seconds=0.1)


class WakeTriggerTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        path = os.path.join(self._dir.name, "session.sock")
        self.agent = Agent(path)
        self.agent.start()
        self._old = wake_word.VOICE_SESSION_SOCKET
        wake_word.VOICE_SESSION_SOCKET = path
        self.addCleanup(setattr, wake_word, "VOICE_SESSION_SOCKET", self._old)
        wake_word.timer_alarm_client.is_ringing = lambda: False
        self.trigger = wake_word.WakeTrigger(cooldown_seconds=0.0)

    def relayed(self):
        self.agent.join()
        return self.agent.received

    def test_plain_voice_relay_is_unchanged(self):
        self.assertTrue(self.trigger.fire("voice", 0.3))
        self.trigger.settle(opinion(False))  # nothing tentative: says nothing
        self.assertEqual(self.relayed(), ["voice"])

    def test_tentative_relay_then_confirm(self):
        self.assertTrue(self.trigger.fire("voice", 0.3, tentative=True))
        self.trigger.settle(opinion(True))
        self.assertEqual(self.relayed(), ["voice-pending", "voice-confirm"])

    def test_tentative_relay_then_reject(self):
        self.trigger.fire("voice", 0.3, tentative=True)
        self.trigger.settle(opinion(False))
        self.assertEqual(self.relayed(), ["voice-pending", "voice-reject"])

    def test_a_second_opinion_that_could_not_judge_lets_the_session_through(self):
        self.trigger.fire("voice", 0.3, tentative=True)
        self.trigger.settle(None)
        self.assertEqual(self.relayed(), ["voice-pending", "voice-confirm"])

    def test_settles_only_once(self):
        self.trigger.fire("voice", 0.3, tentative=True)
        self.trigger.settle(opinion(False))
        self.trigger.settle(opinion(False))
        self.assertEqual(self.relayed(), ["voice-pending", "voice-reject"])

    def test_a_detection_that_silenced_a_ringing_timer_is_not_settled(self):
        wake_word.timer_alarm_client.is_ringing = lambda: True
        wake_word.timer_alarm_client.stop_ring = lambda: (True, "")
        self.trigger.fire("voice", 0.3, tentative=True)
        self.trigger.settle(opinion(False))
        self.assertEqual(self.relayed(), [])


if __name__ == "__main__":
    unittest.main()
