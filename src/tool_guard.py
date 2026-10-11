"""Journals every tool call of a voice session, and refuses the ones that
would ring later when nothing intelligible was heard.

Why it exists: a false wake-up opens a session on room noise, and the model
sometimes answers noise with a tool call -- the user found timers running that
nobody had set. Tool calls were not journalled at all, so those could not even
be traced afterwards.

The check is deliberately narrow. The transcription is a side channel of the
speech-to-speech model and fails on real commands too (7 Oct 2026, 19:34: '.'
transcribed, volume correctly raised), so by default only the timer and
alarm tools are refused without it; for every other tool the journal just says
that it would have been (cancel_timer and cancel_alarm were added on
2026-10-09: cancel_alarm was called on '<noise>'). Room *speech* still gets
through: it is transcribed
as words. Stopping that is the wake word's job (see wake_confirm.py).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re

LOG = logging.getLogger("voice-agent.tools")

# Tools refused when the session has no intelligible transcription; "" = none.
GUARDED = frozenset(
    name.strip()
    for name in os.environ.get(
        "VOICE_TOOL_GUARD", "set_timer,set_alarm,cancel_timer,cancel_alarm"
    ).split(",")
    if name.strip()
)
# The transcription can trail the tool call; give it this long to turn up.
WAIT_SECONDS = float(os.environ.get("VOICE_TOOL_GUARD_WAIT_SECONDS", "1.0"))
MIN_LETTERS = 2
REFUSAL = {
    "status": "error",
    "error": "Nothing the user said was understood, so nothing was done. "
             "Do not claim it was; ask them briefly to repeat.",
}


def is_intelligible(text: str | None) -> bool:
    """Whether a transcription holds words, not just '.' or '<noise>'."""
    words = re.sub(r"<[^>]*>", " ", text or "")
    return sum(c.isalpha() for c in words) >= MIN_LETTERS


class Hearing:
    """What the user has been transcribed as saying in one session."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self._intelligible = asyncio.Event()

    def note(self, text: str | None) -> None:
        self.texts.append(text or "")
        if is_intelligible(text):
            self._intelligible.set()

    async def has_words(self, wait_seconds: float = 0.0) -> bool:
        if not self._intelligible.is_set() and wait_seconds > 0:
            try:
                await asyncio.wait_for(self._intelligible.wait(), wait_seconds)
            except asyncio.TimeoutError:
                pass
        return self._intelligible.is_set()


def guard(tool, hearing: Hearing, guarded: frozenset[str] = GUARDED,
          wait_seconds: float = WAIT_SECONDS):
    """Wrap one of the session's tool functions; its name, signature and
    docstring -- what the model is told about it -- stay the tool's own."""

    @functools.wraps(tool)
    async def guarded_tool(params, **arguments):
        name = tool.__name__
        heard = await hearing.has_words(wait_seconds)
        LOG.info("Tool call: %s(%s) heard=%r", name,
                 ", ".join(f"{k}={v!r}" for k, v in arguments.items()), hearing.texts)
        if not heard:
            if name in guarded:
                LOG.info("Refused %s: nothing intelligible was transcribed", name)
                await params.result_callback(REFUSAL)
                return
            LOG.info("Would refuse %s (nothing intelligible transcribed); "
                     "it is not in VOICE_TOOL_GUARD, so it runs", name)
        await tool(params, **arguments)

    return guarded_tool
