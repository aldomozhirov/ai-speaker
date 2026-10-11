"""Unit tests for the tentative session start in voice_agent.py.

    PYTHONPATH=~/.local/bin ~/.venvs/voice-agent/bin/python -m unittest test_voice_agent_confirm -v
"""

import asyncio
import os
import tempfile
import unittest

_scratch = tempfile.mkdtemp(prefix="voice-agent-test-")
os.environ.update({
    "VOICE_STATE_FILE": os.path.join(_scratch, "state.json"),
    "ASSISTANT_LED_SOCKET": os.path.join(_scratch, "no-led.sock"),
    "VOICE_DUCK": "0",
    "VOICE_CONFIRM_TIMEOUT_SECONDS": "0.2",
})

import voice_agent  # noqa: E402


class Mic:
    def __init__(self):
        self.events = []

    def begin_capture(self):
        self.events.append("open")

    def stop_capture(self):
        self.events.append("close")


class Transport:
    def __init__(self, params):
        self.mic = Mic()
        Transport.last = self

    def input(self):
        return self.mic


class Stop(Exception):
    """Raised where a confirmed session would switch the DSP: far enough."""


class TentativeSessionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        voice_agent.PwCatTransport = Transport
        voice_agent.send_led_state = lambda state: self.leds.append(state)
        self.leds = []
        self.agent = voice_agent.VoiceAgent()
        self.went_ahead = False

        def to_voice():
            self.went_ahead = True
            raise Stop()

        self.agent._dsp.to_voice = to_voice

    async def settle(self):
        for _ in range(40):
            await asyncio.sleep(0.02)
            if not self.agent._active:
                return
        self.fail("session still marked active")

    async def test_rejected_wake_word_opens_and_closes_the_mic_and_nothing_else(self):
        await self.agent._handle_trigger_async("voice-pending")
        await asyncio.sleep(0.05)
        self.assertTrue(self.agent._active)
        await self.agent._handle_trigger_async("voice-reject")
        await self.settle()
        self.assertEqual(Transport.last.mic.events, ["open", "close"])
        self.assertFalse(self.went_ahead)
        self.assertEqual(self.leds, [])

    async def test_confirmed_wake_word_goes_ahead(self):
        await self.agent._handle_trigger_async("voice-pending")
        await asyncio.sleep(0.05)
        self.assertFalse(self.went_ahead)  # still waiting for the verdict
        await self.agent._handle_trigger_async("voice-confirm")
        await self.settle()
        self.assertTrue(self.went_ahead)

    async def test_no_verdict_in_time_goes_ahead(self):
        with self.assertLogs("voice-agent", level="WARNING"):
            await self.agent._handle_trigger_async("voice-pending")
            await self.settle()
        self.assertTrue(self.went_ahead)

    async def test_plain_voice_trigger_does_not_wait(self):
        await self.agent._handle_trigger_async("voice")
        await self.settle()
        self.assertTrue(self.went_ahead)

    async def test_a_verdict_nobody_waits_for_is_ignored(self):
        await self.agent._handle_trigger_async("voice-reject")
        await self.agent._handle_trigger_async("voice-confirm")
        self.assertFalse(self.agent._active)

    async def test_the_next_wake_word_works_after_a_rejected_one(self):
        await self.agent._handle_trigger_async("voice-pending")
        await self.agent._handle_trigger_async("voice-reject")
        await self.settle()
        await self.agent._handle_trigger_async("voice-pending")
        await self.agent._handle_trigger_async("voice-confirm")
        await self.settle()
        self.assertTrue(self.went_ahead)


if __name__ == "__main__":
    unittest.main()
