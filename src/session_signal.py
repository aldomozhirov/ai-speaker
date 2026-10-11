"""Cross-module signal for "quick action" tool calls (Phase 8).

Tool functions (spotify_*, volume_*) are plain functions with no access to
the session object -- LLMContext binds them directly by reference, and
Pipecat's DirectFunction introspects their signature into the tool's
LLM-facing schema, so we can't just add a session parameter without it
leaking into that schema. A module-level flag lets a tool mark "don't wait
for another turn after this" as a side effect instead.

One session runs at a time (VoiceAgent rejects a new trigger while
self._active), so a single process-wide flag is enough -- no per-session
keying needed.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

LOG = logging.getLogger("voice-agent.session")

_quick_action = False
# Work a tool has prepared but that must not happen until the assistant has
# said its confirmation -- starting the music, which would otherwise play
# under (and before) the reply. Run once by voice_agent.py when the session
# closes; see complete_quick_action().
_deferred: Callable[[], Awaitable[None]] | None = None


def mark_quick_action() -> None:
    global _quick_action
    _quick_action = True


def consume_quick_action() -> bool:
    global _quick_action
    value = _quick_action
    _quick_action = False
    return value


def discard_deferred() -> None:
    global _deferred
    _deferred = None


async def run_deferred() -> bool:
    """Run the action a tool left for after the confirmation, if any.
    Returns False if it failed (already logged)."""
    global _deferred
    action, _deferred = _deferred, None
    if action is None:
        return True
    try:
        await action()
    except Exception:  # noqa: BLE001 -- the session is over; nothing to tell the model
        LOG.exception("Deferred action failed")
        return False
    return True


async def complete_quick_action(
    params: Any, result: dict, then: Callable[[], Awaitable[None]] | None = None
) -> None:
    """Report a successful one-shot action so that it is confirmed exactly once.

    `then`, if given, runs after the confirmation has been spoken.

    Gemini answers a command in one of two ways. Usually it calls the tool
    silently and speaks once it has the result. About one time in three
    (2026-10-04, replaying a real "play Beatles on Spotify") it says the
    confirmation first and calls the tool in the same breath -- and then
    confirms a second time when the result arrives. In that case the result
    is kept from it: it has already said what there was to say, and the
    session is closing anyway.
    """
    global _deferred
    mark_quick_action()
    _deferred = then
    llm = params.llm
    if getattr(llm, "spoke_before_tool_call", False):
        from pipecat.frames.frames import FunctionCallResultProperties

        LOG.info("Already confirmed aloud before %s; not asking for a second reply",
                 params.function_name)
        await params.result_callback(
            result, properties=FunctionCallResultProperties(run_llm=False)
        )
        await llm.finish_turn_without_result()
    else:
        await params.result_callback(result)
