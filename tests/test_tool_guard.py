"""Unit tests for tool_guard.

    ~/.venvs/voice-agent/bin/python -m unittest test_tool_guard -v
"""

import asyncio
import unittest

import tool_guard


class Params:
    def __init__(self):
        self.results = []

    async def result_callback(self, result):
        self.results.append(result)


def make_tool(calls):
    async def set_timer(params, duration_seconds: int) -> None:
        """Start a countdown timer.

        Args:
            duration_seconds: How long, in seconds.
        """
        calls.append(duration_seconds)
        await params.result_callback({"status": "ok"})

    return set_timer


GUARDED = frozenset({"set_timer"})


class IntelligibleTest(unittest.TestCase):
    def test_noise_and_punctuation_are_not_words(self):
        for text in ("", None, ".", " . ", "<noise>", "<noise> .", "a"):
            self.assertFalse(tool_guard.is_intelligible(text), text)

    def test_any_language_counts(self):
        for text in ("Play listen to your heart.", "включи Макс Каржа.", "Kötü maks", "ть <noise> да"):
            self.assertTrue(tool_guard.is_intelligible(text), text)


class GuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_guarded_tool_runs_after_real_words(self):
        calls, params, hearing = [], Params(), tool_guard.Hearing()
        hearing.note("set a timer for five minutes")
        await tool_guard.guard(make_tool(calls), hearing, GUARDED, 0)(params=params, duration_seconds=300)
        self.assertEqual(calls, [300])
        self.assertEqual(params.results, [{"status": "ok"}])

    async def test_guarded_tool_is_refused_on_noise(self):
        calls, params, hearing = [], Params(), tool_guard.Hearing()
        hearing.note("<noise>")
        hearing.note(".")
        with self.assertLogs("voice-agent.tools", level="INFO") as logs:
            await tool_guard.guard(make_tool(calls), hearing, GUARDED, 0)(params=params, duration_seconds=300)
        self.assertEqual(calls, [])
        self.assertEqual(params.results, [tool_guard.REFUSAL])
        self.assertIn("Tool call: set_timer(duration_seconds=300) heard=['<noise>', '.']", logs.output[0])
        self.assertIn("Refused set_timer", logs.output[1])

    async def test_guarded_tool_is_refused_when_nothing_was_transcribed(self):
        calls, params = [], Params()
        await tool_guard.guard(make_tool(calls), tool_guard.Hearing(), GUARDED, 0)(params=params, duration_seconds=5)
        self.assertEqual(calls, [])

    async def test_unguarded_tool_runs_on_noise_and_says_so(self):
        calls, params, hearing = [], Params(), tool_guard.Hearing()
        hearing.note(".")
        with self.assertLogs("voice-agent.tools", level="INFO") as logs:
            await tool_guard.guard(make_tool(calls), hearing, frozenset(), 0)(params=params, duration_seconds=7)
        self.assertEqual(calls, [7])
        self.assertIn("Would refuse set_timer", logs.output[1])

    async def test_waits_for_a_transcription_that_trails_the_call(self):
        calls, params, hearing = [], Params(), tool_guard.Hearing()
        asyncio.get_running_loop().call_later(0.05, hearing.note, "таймер на пять минут")
        await tool_guard.guard(make_tool(calls), hearing, GUARDED, 1.0)(params=params, duration_seconds=300)
        self.assertEqual(calls, [300])

    async def test_the_model_is_told_the_same_about_a_guarded_tool(self):
        try:
            from pipecat.adapters.schemas.direct_function import DirectFunctionWrapper
            from timer_tools import set_alarm, set_timer
        except ImportError:
            self.skipTest("needs the voice agent's environment and ~/.local/bin on the path")
        for tool in (set_timer, set_alarm):
            plain = DirectFunctionWrapper(tool).to_function_schema()
            wrapped = DirectFunctionWrapper(tool_guard.guard(tool, tool_guard.Hearing())).to_function_schema()
            self.assertEqual(plain.to_default_dict(), wrapped.to_default_dict())


if __name__ == "__main__":
    unittest.main()
