"""LLM engine adapters for the voice agent.

voice_agent.py used to construct GeminiLiveLLMService directly, inline, with
Gemini-specific settings (context_window_compression, voice="Puck",
inference_on_context_initialization) hardcoded into the session-setup code.
The project's own build notes stated the intent to keep "the engine
swappable behind env vars" from day one, but that switch was never actually
implemented -- this module is it. Select an engine via VOICE_ENGINE (default
"gemini", so existing behaviour is unchanged unless this is set explicitly).

Each engine builder takes the same generic config (model, voice, system
prompt, context-compression toggle) and returns an EngineHandle: the
constructed Pipecat LLMService, plus the two pieces of engine-specific
behaviour the rest of voice_agent.py needs:

- audio_out_sample_rate: the rate the service's output audio frames use, so
  the transport/pipeline get configured to match.
- prime_with_run_frame: whether the pipeline needs a queued LLMRunFrame
  right after startup to make the service start accepting audio input, even
  though the context is empty and no inference should actually run on it.
  GeminiLiveLLMService needs this nudge (see _build_gemini below); recorded
  per-engine here rather than assumed, since it's a real per-service quirk.

Adding a new engine means adding one function here plus one entry in
ENGINE_DEFAULTS/ENGINE_BUILDERS -- nothing in voice_agent.py itself should
need to change.

Only `gemini` has been run against a live conversation on this box.
`openai_realtime` is wired up because pipecat ships it and the tool-calling
layer (spotify_tools.py/volume_tools.py) is already provider-agnostic, but
it is code-verified only -- treat it as a starting point, not a proven
config, until it's been tried with a real OpenAI Realtime account.
"""

from __future__ import annotations

import asyncio
import collections
import os
import time
from dataclasses import dataclass
from typing import Callable

from loguru import logger
from pipecat.services.llm_service import LLMService

# Per-engine defaults for the generic VOICE_MODEL/VOICE_NAME env vars (used
# only when those env vars are unset, so switching VOICE_ENGINE alone is
# enough to get a working config) and which env var holds that engine's API
# key, so VOICE_API_KEY can stay generic across engines while old deployments
# that already set GOOGLE_API_KEY keep working unchanged.
ENGINE_DEFAULTS: dict[str, dict[str, str]] = {
    "gemini": {
        "model": "models/gemini-2.5-flash-native-audio-preview-12-2025",
        "voice": "Puck",
        "api_key_env": "GOOGLE_API_KEY",
    },
    "openai_realtime": {
        "model": "gpt-realtime-2",
        "voice": "marin",
        "api_key_env": "OPENAI_API_KEY",
    },
}


@dataclass
class EngineConfig:
    api_key: str
    model: str
    voice: str
    system_prompt: str
    context_compression_enabled: bool
    context_compression_trigger_tokens: int | None
    # How much user audio to hold while the engine is not yet able to take it
    # (see _preroll_gemini_service). 0 = drop it, the service's own behaviour.
    preroll_seconds: float = 0.0
    # Digital silence sent ahead of that held audio.
    preroll_lead_seconds: float = 0.5


@dataclass
class EngineHandle:
    service: LLMService
    audio_out_sample_rate: int
    prime_with_run_frame: bool


def _preroll_gemini_service():
    """GeminiLiveLLMService that keeps the audio it cannot send yet.

    The stock service drops every input audio frame until its session is
    connected and has been marked ready for realtime input, and it is not
    connected until about a second after the wake word (it even connects
    twice: tools arriving in the first context frame force a reconnect).
    That is exactly when the user is saying their command. This holds those
    frames, bounded, and sends them ahead of live audio the moment the
    session is ready -- the same thing again if the connection drops and
    comes back in the middle of a conversation.

    The held audio is preceded by half a second of digital silence. Someone
    who says the command in one breath is already talking when the capture
    opens, and a stream that *starts* in mid-speech gets no response from
    Gemini at all: three of the user's five real attempts on 2026-10-04 came
    back with no transcription, live and again when replayed, and all three
    were understood once the same audio followed a short silence.

    It also notes whether Gemini had already begun speaking when it called a
    tool (spoke_before_tool_call), and can end such a turn without handing the
    result back -- see session_signal.complete_quick_action for why.

    Relies on private parts of pipecat 1.7's service: three attributes
    (_session, _ready_for_realtime_input, _audio_input_paused), the same ones
    its own _send_user_audio() tests, and the _handle_msg_* methods plus
    _bot_is_responding / _set_bot_is_responding. Re-check them on a pipecat
    upgrade.
    """
    from pipecat.frames.frames import LLMFullResponseEndFrame, TTSStoppedFrame
    from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

    class PrerollGeminiLiveLLMService(GeminiLiveLLMService):
        # Allowance for the output path (pw-cat, CamillaDSP) when waiting for
        # speech that has been handed over to finish playing.
        _PLAYOUT_MARGIN = 0.3

        def __init__(self, *args, preroll_seconds: float = 0.0,
                     preroll_lead_seconds: float = 0.0, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._preroll_limit = preroll_seconds
            self._preroll_lead = preroll_lead_seconds
            self._preroll: collections.deque = collections.deque()
            self._preroll_held = 0.0
            # Bytes of speech Gemini has produced in its current turn.
            self._reply_audio = 0
            self._reply_started = 0.0
            self.spoke_before_tool_call = False

        async def _handle_msg_model_turn(self, msg):
            # Counted here rather than read off _bot_is_responding, which a
            # text part (a "thought") sets as well.
            part = msg.server_content.model_turn.parts[0]
            data = part.inline_data if part else None
            if data and data.data:
                if not self._reply_audio:
                    self._reply_started = time.monotonic()
                self._reply_audio += len(data.data)
            await super()._handle_msg_model_turn(msg)

        async def _handle_msg_turn_complete(self, message):
            self._reply_audio = 0
            await super()._handle_msg_turn_complete(message)

        async def _handle_msg_tool_call(self, message):
            self.spoke_before_tool_call = self._reply_audio > 0
            await super()._handle_msg_tool_call(message)

        async def finish_turn_without_result(self):
            """Close a turn whose tool result is being withheld.

            Gemini sends turn_complete only after it has the result, and
            everything downstream waits for that: the output transport to
            report that the bot stopped speaking (it otherwise takes 3 s of
            silence to decide), and this service to let an EndFrame through.

            Gemini delivers speech much faster than it plays (1.8 s arrives
            in 0.4 s) and holds its own turn_complete back until the speech
            would have been heard. This does the same; ending the turn at
            once would close the session, and the player with it, in the
            middle of the sentence.
            """
            spoken = self._reply_audio / (2 * self._sample_rate)
            self._reply_audio = 0
            remaining = self._reply_started + spoken + self._PLAYOUT_MARGIN - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            if self._bot_is_responding:
                await self.push_frame(TTSStoppedFrame())
                await self.push_frame(LLMFullResponseEndFrame())
                await self._set_bot_is_responding(False)

        @staticmethod
        def _seconds(frame) -> float:
            return len(frame.audio) / (2 * frame.sample_rate * frame.num_channels)

        async def _send_user_audio(self, frame):
            if not self._session or not self._ready_for_realtime_input:
                if not self._audio_input_paused:
                    self._preroll.append(frame)
                    self._preroll_held += self._seconds(frame)
                    while self._preroll_held > self._preroll_limit and self._preroll:
                        self._preroll_held -= self._seconds(self._preroll.popleft())
                return
            if self._preroll:
                held, self._preroll_held = self._preroll_held, 0.0
                pending = list(self._preroll)
                self._preroll.clear()
                logger.info("Sending {:.2f}s of audio captured before Gemini was ready", held)
                if self._preroll_lead > 0:
                    first = pending[0]
                    samples = int(self._preroll_lead * first.sample_rate) * first.num_channels
                    await super()._send_user_audio(
                        type(first)(
                            audio=bytes(2 * samples),
                            sample_rate=first.sample_rate,
                            num_channels=first.num_channels,
                        )
                    )
                for early in pending:
                    await super()._send_user_audio(early)
            await super()._send_user_audio(frame)

    return PrerollGeminiLiveLLMService


def _build_gemini(cfg: EngineConfig) -> EngineHandle:
    from pipecat.services.google.gemini_live.llm import (
        ContextWindowCompressionParams,
        GeminiLiveLLMService,
    )

    service_class = GeminiLiveLLMService
    extra = {}
    if cfg.preroll_seconds > 0:
        service_class = _preroll_gemini_service()
        extra["preroll_seconds"] = cfg.preroll_seconds
        extra["preroll_lead_seconds"] = cfg.preroll_lead_seconds

    service = service_class(
        api_key=cfg.api_key,
        settings=GeminiLiveLLMService.Settings(
            model=cfg.model,
            voice=cfg.voice,
            system_instruction=cfg.system_prompt,
            # Sliding-window compression. Pipecat defaults this off, and
            # without it the free tier's 15-minute audio-session cap and
            # 128k context window are hard walls rather than something a
            # long conversation slides past.
            context_window_compression=ContextWindowCompressionParams(
                enabled=cfg.context_compression_enabled,
                trigger_tokens=cfg.context_compression_trigger_tokens,
            ),
        ),
        # With an empty starting context, the default (True) would seed the
        # context with system_instruction and trigger inference on it -- the
        # bot would proactively greet as soon as the session opens. We want
        # it to wait silently for the user to speak first (the wake
        # word/button only opens the session).
        inference_on_context_initialization=False,
        **extra,
    )
    return EngineHandle(service=service, audio_out_sample_rate=24000, prime_with_run_frame=True)


def _build_openai_realtime(cfg: EngineConfig) -> EngineHandle:
    from pipecat.services.openai.realtime import events
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    service = OpenAIRealtimeLLMService(
        api_key=cfg.api_key,
        settings=OpenAIRealtimeLLMService.Settings(
            model=cfg.model,
            system_instruction=cfg.system_prompt,
            session_properties=events.SessionProperties(
                audio=events.AudioConfiguration(output=events.AudioOutput(voice=cfg.voice)),
            ),
        ),
    )
    # Not confirmed against a live session whether this service also needs
    # the empty-context LLMRunFrame nudge Gemini does, or whether it starts
    # accepting audio on connect regardless. Defaulting to True (nudge sent)
    # since that's a harmless no-op if unneeded and a silent hang if skipped
    # when it was actually required -- the same asymmetry that motivated
    # sending it unconditionally for Gemini.
    return EngineHandle(service=service, audio_out_sample_rate=24000, prime_with_run_frame=True)


ENGINE_BUILDERS: dict[str, Callable[[EngineConfig], EngineHandle]] = {
    "gemini": _build_gemini,
    "openai_realtime": _build_openai_realtime,
}


def build_engine(name: str, cfg: EngineConfig) -> EngineHandle:
    try:
        builder = ENGINE_BUILDERS[name]
    except KeyError:
        raise ValueError(
            f"Unknown VOICE_ENGINE={name!r}; choices are {sorted(ENGINE_BUILDERS)}"
        ) from None
    return builder(cfg)


def resolve_api_key(engine_name: str) -> str:
    """VOICE_API_KEY wins if set (generic, engine-agnostic); otherwise falls
    back to the engine's own conventional env var (GOOGLE_API_KEY,
    OPENAI_API_KEY, ...) so an existing gemini deployment's env block doesn't
    need to change to pick this up."""
    generic = os.environ.get("VOICE_API_KEY")
    if generic:
        return generic
    env_name = ENGINE_DEFAULTS.get(engine_name, {}).get("api_key_env", "GOOGLE_API_KEY")
    return os.environ.get(env_name, "")
