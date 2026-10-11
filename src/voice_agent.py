#!/usr/bin/env python3
"""Pipecat + Gemini Live conversation engine for the AI speaker (Phase 5).

Runs as a long-lived daemon that owns the actual voice conversation session.
wake_word.py relays every wake trigger (voice detection or button press) here
over a UNIX socket (VOICE_SESSION_SOCKET); this service decides whether that
means "start a new session" or "cancel the active one" (button press while
already listening/speaking = cancel, matching the existing wake-button
behaviour), gives CamillaDSP the voice profile's processing for the
session's duration, runs one Pipecat pipeline per conversation against Gemini
Live, and drives every LED state from "listening" onward via the same
assistant-led socket wake_word.py already uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import socket as socket_module
import sys
import threading

from camilladsp import CamillaClient
from loguru import logger as pc_logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMRunFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_signal  # noqa: E402
import sound_fx  # noqa: E402
import tool_guard  # noqa: E402
import voice_engine  # noqa: E402
from duck import Ducker  # noqa: E402
from pw_transport import PwCatTransport, PwCatTransportParams  # noqa: E402
from spotify_tools import (  # noqa: E402
    spotify_next,
    spotify_pause,
    spotify_play,
    spotify_play_track,
    spotify_previous,
)
from volume_tools import adjust_volume, set_volume  # noqa: E402
from timer_tools import (  # noqa: E402
    cancel_alarm,
    cancel_timer,
    get_timer_alarm_status,
    set_alarm,
    set_timer,
)

LOG = logging.getLogger("voice-agent")

# --- config ------------------------------------------------------------------

# Which LLM/speech-to-speech engine to use -- see voice_engine.py. Default
# "gemini" keeps existing deployments' behaviour unchanged; this is the
# switch that makes the engine actually swappable via env var (previously
# only Gemini's constructor was ever wired up here, despite that being the
# stated intent from day one).
VOICE_ENGINE = os.environ.get("VOICE_ENGINE", "gemini")
_engine_defaults = voice_engine.ENGINE_DEFAULTS.get(VOICE_ENGINE, {})
VOICE_API_KEY = voice_engine.resolve_api_key(VOICE_ENGINE)
VOICE_MODEL = os.environ.get("VOICE_MODEL", _engine_defaults.get("model", ""))
VOICE_NAME = os.environ.get("VOICE_NAME", _engine_defaults.get("voice", ""))
VOICE_SYSTEM_PROMPT = os.environ.get(
    "VOICE_SYSTEM_PROMPT",
    "You are the voice assistant built into a home smart speaker. You can "
    "control Spotify playback (play, pause, next, previous), play a "
    "specific song, album, artist or playlist the user asks for, set "
    "the speaker's volume, and set or cancel timers and alarms, all via "
    "your tools. Timers and alarms ring out loud on this speaker and are "
    "silenced by saying \"stop\" or pressing the button, so you never need "
    "to tell the user you cannot set one. Keep replies brief "
    "and conversational -- one to three spoken sentences -- since they're "
    "spoken aloud through a single small speaker. If the user says goodbye "
    "or indicates they're done, say a brief goodbye. "
    # Added 2026-10-04. Commands here come in Russian and in English, and the
    # journal has the user asking "А ты можешь говорить по-русски?" after an
    # answer. English is the fallback when the language is unclear, at the
    # user's request: an English "play Beatles on Spotify" from across the
    # room had been confirmed in Russian.
    "Reply in English by default. The people in this home also speak "
    "Russian: reply in Russian only when you are certain the request was "
    "spoken in Russian, and never use any other language. What you hear "
    "may begin with the wake phrase \"Hey Jarvis\" or a short chime; "
    "ignore both and act on the request that follows.",
)
VOICE_IDLE_SECONDS = float(os.environ.get("VOICE_IDLE_SECONDS", "8.0"))
VOICE_INPUT_CHANNEL = int(
    os.environ.get("VOICE_INPUT_CHANNEL", os.environ.get("WAKE_WORD_CHANNEL", "0"))
)
# Keep what the user says between the trigger and the moment Gemini can take
# audio (about a second -- see PwCatInputTransport.begin_capture and
# voice_engine._preroll_gemini_service). This is the cap on how much is held.
# 0 turns it off and restores the old behaviour, where that audio was dropped.
VOICE_PREROLL_SECONDS = float(os.environ.get("VOICE_PREROLL_SECONDS", "10.0"))
# How long a session opened by a "voice-pending" trigger waits for wake_word.py
# to confirm or reject it (normally about 0.3 s) before going ahead regardless.
VOICE_CONFIRM_TIMEOUT_SECONDS = float(os.environ.get("VOICE_CONFIRM_TIMEOUT_SECONDS", "1.5"))
# Gain applied to the microphone channel before it is sent to the engine, in
# dB (see PwCatTransportParams.input_gain). 0 leaves the audio as captured.
VOICE_INPUT_GAIN_DB = float(os.environ.get("VOICE_INPUT_GAIN_DB", "0"))
# Mute the mic while the bot speaks. See AudioGate in pw_transport.py for the
# measurements behind this: the XVF3800's residual echo is louder than the
# user's voice at the mic, so full duplex means the assistant interrupts and
# answers itself. Set to 0 to get voice barge-in back once echo suppression is
# good enough to support it.
VOICE_HALF_DUPLEX = os.environ.get("VOICE_HALF_DUPLEX", "1").lower() in {"1", "true", "yes"}
VOICE_MUTE_TAIL_SECONDS = float(os.environ.get("VOICE_MUTE_TAIL_SECONDS", "0.4"))
# Backstop for the mute gate. A bot turn never legitimately lasts this long, so
# reaching it means the BotStoppedSpeakingFrame that normally reopens the mic
# was lost -- most plausibly to a mid-utterance Gemini reconnect.
VOICE_MAX_MUTE_SECONDS = float(os.environ.get("VOICE_MAX_MUTE_SECONDS", "30.0"))
# How loud the bot plays back, 0.0-1.0. Only interesting when half-duplex is
# off: the AEC residual scales with the bot's own level but the user's voice at
# the mic does not, so this is the knob that decides whether barge-in works.
# Default 1.0 leaves half-duplex behaviour exactly as it was.
VOICE_OUTPUT_GAIN = min(1.0, max(0.0, float(os.environ.get("VOICE_OUTPUT_GAIN", "1.0"))))

# Sliding-window context compression. Without it the free tier's 15-minute
# audio-session cap and 128k context window both apply hard -- pipecat only
# sends the compression config when enabled, and its default is off.
VOICE_CONTEXT_COMPRESSION = os.environ.get("VOICE_CONTEXT_COMPRESSION", "1").lower() in {
    "1",
    "true",
    "yes",
}
_trigger_tokens = os.environ.get("VOICE_COMPRESSION_TRIGGER_TOKENS", "").strip()
# None lets Gemini use its own default (80% of the context window).
VOICE_COMPRESSION_TRIGGER_TOKENS = int(_trigger_tokens) if _trigger_tokens else None

# Absolute ceiling on one conversation, independent of the idle timeout. This
# is a safety net against a wedged session holding the voice DSP profile and
# the mic open indefinitely, not the API's session limit -- compression above
# handles that one.
VOICE_MAX_SESSION_SECONDS = float(os.environ.get("VOICE_MAX_SESSION_SECONDS", "1800"))

VOICE_SOURCE = os.environ.get(
    "VOICE_SOURCE",
    "alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993700254500350-00.analog-stereo",
)
VOICE_SINK = os.environ.get("VOICE_SINK", "alsa_output.platform-snd_aloop.0.analog-stereo")

# Duck the music for the whole session, not just while the bot speaks. Two
# reasons: per-utterance ducking pumps the music several times per exchange,
# and the session's Silero VAD listens to the same mic -- unducked music during
# the listening half reads as speech and manufactures turn boundaries.
VOICE_DUCK = os.environ.get("VOICE_DUCK", "1").lower() in {"1", "true", "yes"}
VOICE_DUCK_LEVEL = float(os.environ.get("VOICE_DUCK_LEVEL", "0.20"))
VOICE_DUCK_RAMP_MS = float(os.environ.get("VOICE_DUCK_RAMP_MS", "200"))
VOICE_DUCK_MATCH = os.environ.get("VOICE_DUCK_MATCH", "spotifyd|librespot")

# Chimes on session start/end (see sound_fx.py for the sink/volume/dir knobs).
# Both play while ducked, if ducking is on, so a full-volume chime doesn't
# have to fight full-volume music.
SOUND_FX_ENABLED = os.environ.get("SOUND_FX_ENABLED", "1").lower() in {"1", "true", "yes"}

CAMILLA_HOST = os.environ.get("CAMILLA_HOST", "127.0.0.1")
CAMILLA_PORT = int(os.environ.get("CAMILLA_PORT", "1234"))
CAMILLA_PROFILES_DIR = os.environ.get("CAMILLA_PROFILES_DIR", "/etc/camilladsp/profiles")
VOICE_PROFILE = os.environ.get("VOICE_PROFILE", "voice")

# Said by the user, these end the session at once instead of after the idle
# timeout. Matched as whole words anywhere in what was heard; a trailing "$"
# means "only at the very end". That matters for "пока": on its own or after
# "спасибо" it is a goodbye, but it is also how "покажи" starts and a plain
# conjunction ("включи музыку, пока я готовлю").
CLOSING_PHRASES = tuple(
    p.strip().lower()
    for p in os.environ.get(
        "VOICE_CLOSING_PHRASES",
        "goodbye,good bye,stop listening,that's all,that is all,never mind,nevermind,"
        "до свидания,пока$,хватит,на этом всё,больше ничего,ничего не надо,"
        "ничего не нужно,неважно,не важно,отбой",
    ).split(",")
    if p.strip()
)


def _normalise(text: str) -> str:
    """Lower-case, with the spelling variations a transcript can vary on
    (ё/е, curly apostrophes, runs of whitespace) folded together."""
    text = text.lower().replace("ё", "е").replace("’", "'")
    return re.sub(r"\s+", " ", text).strip()


def _closing_patterns(phrases: tuple[str, ...]) -> list[re.Pattern]:
    patterns = []
    for phrase in phrases:
        at_end = phrase.endswith("$")
        words = _normalise(phrase.rstrip("$")).split()
        if not words:
            continue
        body = r"\s+".join(re.escape(word) for word in words)
        patterns.append(re.compile(rf"(?<!\w){body}(?!\w)" + (r"\W*$" if at_end else "")))
    return patterns


_CLOSING_PATTERNS = _closing_patterns(CLOSING_PHRASES)


def is_closing_phrase(text: str | None) -> bool:
    text = _normalise(text or "")
    return bool(text) and any(p.search(text) for p in _CLOSING_PATTERNS)

ASSISTANT_LED_SOCKET = os.environ.get(
    "ASSISTANT_LED_SOCKET",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "assistant-led.sock"),
)
VOICE_SESSION_SOCKET = os.environ.get(
    "VOICE_SESSION_SOCKET",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "voice-session.sock"),
)
VOICE_STATE_FILE = os.environ.get(
    "VOICE_STATE_FILE",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "voice-agent-state.json"),
)


def send_led_state(state: str) -> None:
    try:
        with socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(ASSISTANT_LED_SOCKET)
            s.sendall((state + "\n").encode())
    except OSError as exc:
        LOG.debug("Could not reach assistant-led socket: %s", exc)


# --- crash-recoverable session state -----------------------------------------


class RuntimeState:
    """Small JSON file recording what an active session changed *outside* this
    process, so that a crash-restart can undo it.

    Restart=on-failure brings the daemon back with no memory of the session it
    was running, which would otherwise leave CamillaDSP stuck on the voice
    profile. Lives in XDG_RUNTIME_DIR on purpose: a reboot restarts CamillaDSP
    from its own configured default anyway, so this state must not outlive one.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            with open(self._path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._path)
        except OSError as exc:
            LOG.warning("Could not persist runtime state to %s: %s", self._path, exc)

    def get(self, key: str, default=None):
        with self._lock:
            return self._read().get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            data = self._read()
            data[key] = value
            self._write(data)

    def clear(self, key: str) -> None:
        with self._lock:
            data = self._read()
            if data.pop(key, None) is None:
                return
            if data:
                self._write(data)
            else:
                with contextlib.suppress(OSError):
                    os.unlink(self._path)


# --- CamillaDSP profile switching --------------------------------------------


class DspProfileSwitcher:
    """Gives CamillaDSP the voice profile's sound for the length of a session,
    over its websocket control API.

    smartspk-profile needs root (it restarts camilladsp.service) and there's
    no passwordless sudo configured for it; adding that just for this feature
    would be a new standing privilege escalation. CamillaDSP's own websocket
    API (already used by CamillaGUI) applies a new config in place, with no
    restart and no sudo.

    Only the voice profile's processing is applied: its filters, mixers,
    processors and pipeline. Its `devices` section is replaced with the one
    that is already running, because that section sets how much audio
    CamillaDSP buffers (chunksize, target_level), and the XVF3800's echo
    canceller is aligned to exactly that: its reference (xvf-aec-ref.service)
    has to reach the chip just ahead of the echo. Loading voice.yml as a file,
    which is what this did until 2026-10-04, restarted both audio devices (a
    0.1 s hole in whatever was playing, at the start and again at the end of
    every session) and ran them with 52 ms less in the buffer -- more than
    the 36 ms the reference leads by, so the echo overtook its reference for
    the whole session. Swapping the processing alone takes effect in about
    15 ms and leaves the devices and their timing untouched.
    """

    STATE_KEY = "dsp_previous_config"

    def __init__(
        self, host: str, port: int, profiles_dir: str, voice_profile: str, state: RuntimeState
    ) -> None:
        self._client = CamillaClient(host, port)
        self._voice_path = os.path.join(profiles_dir, f"{voice_profile}.yml")
        # What was running before the session, and what the session put in its
        # place. Both are whole configurations, not file names: CamillaDSP's
        # own config path stays where it was throughout.
        self._previous: dict | None = None
        self._applied: dict | None = None
        self._state = state

    def _ensure_connected(self) -> None:
        if not self._client.is_connected():
            self._client.connect()

    def _running(self) -> dict:
        config = self._client.config.active()
        if not config:
            raise RuntimeError("CamillaDSP has no active configuration")
        return config

    def _voice_config(self, running: dict) -> dict:
        """The voice profile's processing on top of `running`'s buffering."""
        voice = self._client.config.read_and_parse_file(self._voice_path)
        voice["devices"] = running["devices"]
        return voice

    @staticmethod
    def _name(config: dict) -> str:
        return repr(config.get("title") or "untitled configuration")

    def to_voice(self) -> None:
        self._ensure_connected()
        current = self._running()
        voice = self._voice_config(current)
        if voice == current:
            # Already there. Either someone selected the voice profile by hand,
            # and there is nothing to undo; or the last session could not put
            # the previous one back, and _previous still says what to retry.
            return
        self._previous, self._applied = current, voice
        self._state.set(self.STATE_KEY, current)
        self._client.config.set_active(voice)
        LOG.info("CamillaDSP -> %s (processing only; buffering unchanged)", self._name(voice))

    def restore(self) -> None:
        if self._previous is None:
            return
        self._ensure_connected()
        if self._running() == self._applied:
            self._client.config.set_active(self._previous)
            LOG.info("CamillaDSP -> %s", self._name(self._previous))
        else:
            # Changed by someone else while the session ran (a manual
            # smartspk-profile call, CamillaDSP restarting): what was recorded
            # is stale, same reasoning as recover().
            LOG.info("CamillaDSP configuration changed during the session; leaving it as it is")
        self._previous = self._applied = None
        self._state.clear(self.STATE_KEY)

    def recover(self) -> None:
        """Undo a profile switch left behind by a session that died mid-flight.

        Only acts if CamillaDSP is *still* running the voice profile's
        processing -- if something else has changed it since (a manual
        smartspk-profile call, say), the recorded configuration is stale and
        stomping it would be worse than doing nothing.
        """
        previous = self._state.get(self.STATE_KEY)
        if not previous:
            return
        try:
            self._ensure_connected()
            current = self._running()
            if current == self._voice_config(current):
                LOG.warning(
                    "Previous session left CamillaDSP on %s; restoring %s",
                    self._name(current),
                    self._name(previous),
                )
                self._client.config.set_active(previous)
            else:
                LOG.info("Stale DSP state (now on %s); discarding", self._name(current))
        except Exception:
            LOG.exception("Could not recover CamillaDSP profile; leaving state file in place")
            return
        self._state.clear(self.STATE_KEY)


# --- LED / closing-phrase pipeline observer ----------------------------------


class SessionMonitor(FrameProcessor):
    """Watches pipeline frames to drive LED state (listening <-> speaking) and
    to detect a spoken closing phrase, without altering the frames themselves.

    "thinking" isn't wired up: Gemini Live's native speech-to-speech doesn't
    expose a clean "processing" phase as a frame the way a cascaded STT/LLM/TTS
    pipeline would (no separate LLM-thinking step to hang it off of).
    """

    def __init__(
        self,
        on_closing_phrase,
        gate=None,
        mute_tail: float = 0.0,
        mute_max: float = 30.0,
        hearing: "tool_guard.Hearing | None" = None,
    ) -> None:
        super().__init__()
        self._on_closing_phrase = on_closing_phrase
        self._hearing = hearing
        self._gate = gate
        self._mute_tail = mute_tail
        self._mute_max = mute_max

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            if self._gate is not None:
                self._gate.mute(self._mute_max)
            send_led_state("speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if session_signal.consume_quick_action():
                # A transport/volume command just completed -- close the
                # session instead of reopening the mic for another turn.
                LOG.info("Quick action completed; closing session")
                self._on_closing_phrase()
            else:
                if self._gate is not None:
                    self._gate.unmute_after(self._mute_tail)
                send_led_state("listening")
        elif isinstance(frame, TranscriptionFrame):
            LOG.info("User transcription: %r", frame.text)
            if self._hearing is not None:
                self._hearing.note(frame.text)
            if is_closing_phrase(frame.text):
                LOG.info("Closing phrase detected: %r", frame.text)
                self._on_closing_phrase()
        await self.push_frame(frame, direction)


def _log_assistant_turns(context) -> None:
    """Journal what the assistant said during a session.

    The user's side has always been logged ("User transcription"); the
    assistant's never was, which left "did it answer in Russian?" and "what
    did it think I asked for?" unanswerable from the journal afterwards.
    """
    if context is None:
        return
    try:
        for message in context.get_messages():
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                ).strip()
            if content:
                LOG.info("Assistant said: %r", content)
    except Exception:
        LOG.debug("Could not read the assistant's turns from the context", exc_info=True)


# --- session lifecycle --------------------------------------------------------


class VoiceAgent:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._task: PipelineTask | None = None
        # Set when the button is pressed while a session is still starting up.
        self._cancel_pending = False
        # Set when the button ends a session: whatever a tool had lined up for
        # after the confirmation (session_signal.run_deferred) is dropped too.
        self._button_cancelled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # The verdict a tentatively opened session is waiting for, if any.
        self._confirmation: asyncio.Future | None = None
        self._state = RuntimeState(VOICE_STATE_FILE)
        self._dsp = DspProfileSwitcher(
            CAMILLA_HOST, CAMILLA_PORT, CAMILLA_PROFILES_DIR, VOICE_PROFILE, self._state
        )
        self._ducker = (
            Ducker(
                self._state,
                level=VOICE_DUCK_LEVEL,
                ramp_ms=VOICE_DUCK_RAMP_MS,
                pattern=VOICE_DUCK_MATCH,
            )
            if VOICE_DUCK
            else None
        )

    def recover(self) -> None:
        """Undo anything a previous, crashed session left behind."""
        self._dsp.recover()
        if self._ducker is not None:
            self._ducker.recover()

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def handle_trigger(self, source: str) -> None:
        """Called from the socket-listener thread; hands off to the event loop."""
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(self._handle_trigger_async(source), self._loop)

    def _settle(self, source: str) -> None:
        """wake_word.py's verdict on the session it opened tentatively."""
        confirmation = self._confirmation
        if confirmation is None or confirmation.done():
            LOG.info("Ignoring %s; no session is waiting for it", source)
            return
        confirmation.set_result(source == "voice-confirm")

    async def _await_confirmation(self, confirmation: asyncio.Future) -> bool:
        """Whether a tentatively opened session may go ahead. No answer in
        time counts as yes: a stuck second opinion must not cost a wake-up."""
        try:
            return await asyncio.wait_for(confirmation, VOICE_CONFIRM_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            LOG.warning(
                "No verdict on the wake word within %.1fs; starting the session anyway",
                VOICE_CONFIRM_TIMEOUT_SECONDS,
            )
            return True

    async def _handle_trigger_async(self, source: str) -> None:
        if source in ("voice-confirm", "voice-reject"):
            self._settle(source)
            return
        confirmation = None
        with self._lock:
            if self._active:
                if source != "button":
                    LOG.info("Ignoring %s trigger; session already active", source)
                    return
                LOG.info("Button pressed while active -- cancelling session")
                # Tell the LED right away: the actual teardown below (DSP
                # profile restore, duck restore) is blocking and can take a
                # second or more, and the ring shouldn't sit frozen on the
                # last frame (often solid "speaking") for all of that -- it
                # should start fading out the instant the cancel is issued.
                send_led_state("idle")
                self._button_cancelled = True
                task = self._task
                if task is None:
                    # The session is still being set up, so there is no
                    # pipeline to cancel yet. Falling through used to start a
                    # *second* session next to the first; leave a note for
                    # _run_session to pick up instead.
                    self._cancel_pending = True
                    return
            else:
                LOG.info("Starting session (trigger=%s)", source)
                self._active = True
                self._button_cancelled = False
                task = None
                if source.endswith("-pending"):
                    confirmation = self._confirmation = asyncio.get_running_loop().create_future()
        if task is not None:
            await task.cancel(reason="button cancel")
        else:
            asyncio.ensure_future(self._run_session(confirmation))

    async def _run_session(self, confirmation: asyncio.Future | None = None) -> None:
        loop = asyncio.get_event_loop()
        # Discard any stale flag from a previous session that ended without
        # ever reaching the BotStoppedSpeakingFrame check (e.g. idle timeout
        # or watchdog cancel right after a tool call) -- it must not cause
        # this new session's first turn to close early.
        session_signal.consume_quick_action()
        session_signal.discard_deferred()

        # The microphone comes first. Everything below takes about a second
        # before Gemini can accept audio, and the user is already talking.
        params = PwCatTransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=16000,
            audio_in_channels=1,
            audio_out_enabled=True,
            audio_out_channels=1,
            input_target=VOICE_SOURCE,
            output_target=VOICE_SINK,
            input_channel_index=VOICE_INPUT_CHANNEL,
            input_gain=10 ** (VOICE_INPUT_GAIN_DB / 20),
            output_gain=VOICE_OUTPUT_GAIN,
        )
        transport = PwCatTransport(params)
        if VOICE_PREROLL_SECONDS > 0:
            transport.input().begin_capture()

        # A wake word the second opinion has not settled yet (see
        # wake_confirm.py): the microphone above is already recording, so none
        # of the command is lost, but nothing is shown, played or sent until
        # the verdict is in -- and if it is "no", nothing ever is.
        if confirmation is not None and not await self._await_confirmation(confirmation):
            transport.input().stop_capture()
            with self._lock:
                self._active = False
                self._cancel_pending = False
            LOG.info("Wake word not confirmed; no session")
            return

        try:
            await loop.run_in_executor(None, self._dsp.to_voice)
        except Exception:
            LOG.exception("Failed to switch CamillaDSP to voice profile")
            transport.input().stop_capture()
            send_led_state("error")
            with self._lock:
                self._active = False
                self._cancel_pending = False
            return

        send_led_state("listening")

        context = None
        watchdog = None
        # One try around set-up *and* the run: anything that fails in between
        # used to escape with _active still set, after which every later wake
        # word was "ignored; session already active" until the service was
        # restarted.
        try:
            if SOUND_FX_ENABLED:
                # Fire-and-forget, not play_blocking(): blocking session
                # start-up on the chime kept the mic closed right after the
                # wake word/button -- exactly when a user who trusts the LED
                # starts talking. Started before ducking rather than after it:
                # the chime needs ~0.3 s to become audible anyway, which is
                # about what the duck ramp takes, and every 100 ms it comes
                # earlier is 100 ms less of it on top of the user's command.
                sound_fx.play("session-started")

            if self._ducker is not None:
                # Best-effort: music left at full volume degrades the
                # conversation but doesn't break it, so a ducking failure must
                # not abort the session it was meant to help.
                try:
                    await loop.run_in_executor(None, self._ducker.duck)
                except Exception:
                    LOG.exception("Ducking failed; continuing at full volume")

            engine = voice_engine.build_engine(
                VOICE_ENGINE,
                voice_engine.EngineConfig(
                    api_key=VOICE_API_KEY,
                    model=VOICE_MODEL,
                    voice=VOICE_NAME,
                    system_prompt=VOICE_SYSTEM_PROMPT,
                    context_compression_enabled=VOICE_CONTEXT_COMPRESSION,
                    context_compression_trigger_tokens=VOICE_COMPRESSION_TRIGGER_TOKENS,
                    preroll_seconds=VOICE_PREROLL_SECONDS,
                ),
            )
            llm = engine.service
            params.audio_out_sample_rate = engine.audio_out_sample_rate

            # Every tool call is journalled, and a timer or alarm is not set
            # on a session in which no words were heard (see tool_guard.py).
            hearing = tool_guard.Hearing()
            context = LLMContext(
                tools=[
                    tool_guard.guard(tool, hearing)
                    for tool in (
                        spotify_play,
                        spotify_pause,
                        spotify_next,
                        spotify_previous,
                        spotify_play_track,
                        set_volume,
                        adjust_volume,
                        set_timer,
                        cancel_timer,
                        set_alarm,
                        cancel_alarm,
                        get_timer_alarm_status,
                    )
                ]
            )
            user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
                context,
                # Gemini Live's own turn-taking is server-side and doesn't emit
                # UserSpeakingFrame/UserStarted/StoppedSpeakingFrame on its own; a
                # local VAD analyzer supplies those so idle_timeout_secs below
                # (which resets on Bot/UserSpeakingFrame) reflects real silence
                # rather than just "bot isn't talking".
                user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
            )
            monitor = SessionMonitor(
                on_closing_phrase=self._request_close,
                gate=transport.input().gate if VOICE_HALF_DUPLEX else None,
                mute_tail=VOICE_MUTE_TAIL_SECONDS,
                mute_max=VOICE_MAX_MUTE_SECONDS,
                hearing=hearing,
            )

            pipeline = Pipeline(
                [
                    transport.input(),
                    user_aggregator,
                    monitor,
                    llm,
                    transport.output(),
                    assistant_aggregator,
                ]
            )

            task = PipelineTask(
                pipeline,
                params=PipelineParams(
                    audio_in_sample_rate=16000, audio_out_sample_rate=engine.audio_out_sample_rate
                ),
                idle_timeout_secs=VOICE_IDLE_SECONDS,
            )
            with self._lock:
                self._task = task
                cancelled = self._cancel_pending
                self._cancel_pending = False

            @task.event_handler("on_pipeline_error")
            async def _on_pipeline_error(pipeline_task, frame):
                LOG.error("Pipeline error (fatal=%s): %s", frame.fatal, frame.error)
                send_led_state("error")
                if not frame.fatal:
                    # Pipecat only auto-cancels on fatal errors, but
                    # GeminiLiveLLMService reports even a permanently dead
                    # connection (3 consecutive reconnect failures) as non-fatal.
                    # Left alone that leaves the session up, silent and deaf, with
                    # the voice profile held until the idle timeout eventually
                    # notices. Close it now and let the LED say why.
                    asyncio.ensure_future(pipeline_task.cancel(reason="pipeline error"))

            @task.event_handler("on_idle_timeout")
            async def _on_idle_timeout(pipeline_task):
                LOG.info("Idle timeout: %.1fs without speech", VOICE_IDLE_SECONDS)

            if cancelled:
                LOG.info("Button pressed during start-up -- not running this session")
            else:
                # GeminiLiveLLMService only accepts audio input once it's seen an
                # LLMContextFrame go by (it gates on an internal
                # "ready for realtime input" flag set the first time _handle_context
                # runs). The context here starts empty on purpose -- we don't want
                # the bot greeting first -- but without this nudge every frame of the
                # user's actual speech is silently dropped before it ever reaches
                # the engine, since the gate never opens. Per-engine (see
                # voice_engine.py): Gemini needs this; not yet confirmed one way or
                # the other for other engines, so it's opt-out, not opt-in.
                if engine.prime_with_run_frame:
                    await task.queue_frames([LLMRunFrame()])

                runner = PipelineRunner(handle_sigint=False)
                watchdog = asyncio.ensure_future(self._session_watchdog(task))
                await runner.run(task)
        except Exception:
            LOG.exception("Voice session ended abnormally")
            send_led_state("error")
        finally:
            if watchdog is not None:
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog
            # A no-op after a normal run, where the pipeline's own teardown has
            # already closed the microphone; the only thing that closes it if
            # the set-up above failed or was cancelled.
            transport.input().stop_capture()
            _log_assistant_turns(context)
            with self._lock:
                self._task = None
                self._active = False
                self._cancel_pending = False
                button_cancelled = self._button_cancelled
            # What a tool prepared but held back until the assistant had
            # confirmed it -- the music the user asked for. Started now, and
            # waited for below, so that it does not hold up the teardown.
            if button_cancelled:
                session_signal.discard_deferred()
            deferred = asyncio.ensure_future(session_signal.run_deferred())
            if SOUND_FX_ENABLED:
                # Fire-and-forget: mic capture for this session has already
                # stopped by this point (pipeline/transport teardown already
                # ran), and nothing downstream waits on this, so it doesn't
                # need to block like the session-started chime does. Played
                # before duck restore so it's heard over quiet music rather
                # than competing with full volume.
                sound_fx.play("session-ended")
            # Like the ducking below: a failure here must not stop the rest of
            # the teardown, or the music stays ducked and the LED stays lit.
            # The switcher keeps what it could not put back and tries again.
            try:
                await loop.run_in_executor(None, self._dsp.restore)
            except Exception:
                LOG.exception("Could not restore the CamillaDSP configuration")
            if self._ducker is not None:
                try:
                    await loop.run_in_executor(None, self._ducker.restore)
                except Exception:
                    LOG.exception("Could not restore ducked stream volume")
            if not await deferred:
                # The assistant has already said it was done; the LED is the
                # only way left to say that it was not.
                send_led_state("error")
                await asyncio.sleep(2.0)
            send_led_state("idle")
            LOG.info("Session ended")

    async def _session_watchdog(self, task: PipelineTask) -> None:
        """Hard ceiling on a single conversation.

        Independent of the idle timeout, which only fires when the user goes
        quiet -- this covers the case where the pipeline is still passing
        frames but the conversation has stopped being useful, so a wedged
        session can't hold the voice DSP profile and the mic open forever.
        """
        await asyncio.sleep(VOICE_MAX_SESSION_SECONDS)
        LOG.warning("Session exceeded %.0fs; closing", VOICE_MAX_SESSION_SECONDS)
        await task.cancel(reason="max session duration")

    def _request_close(self) -> None:
        with self._lock:
            task = self._task
        if task is not None:
            asyncio.ensure_future(task.stop_when_done())


# --- socket server -------------------------------------------------------------


class VoiceSessionListener(threading.Thread):
    """UNIX socket server; wake_word.py relays every trigger here."""

    def __init__(self, socket_path: str, agent: VoiceAgent) -> None:
        super().__init__(name="voice-session-socket", daemon=True)
        self._socket_path = socket_path
        self._agent = agent
        self._stop_event = threading.Event()
        self._server: socket_module.socket | None = None

    def stop(self) -> None:
        self._stop_event.set()
        with contextlib.suppress(Exception):
            if self._server is not None:
                self._server.close()

    def _handle_client(self, conn: socket_module.socket) -> None:
        with conn, conn.makefile("r") as f:
            line = f.readline()
        source = line.strip() or "external"
        self._agent.handle_trigger(source)

    def run(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._socket_path)
        os.makedirs(os.path.dirname(self._socket_path), exist_ok=True)

        server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        self._server = server
        try:
            server.bind(self._socket_path)
            os.chmod(self._socket_path, 0o660)
            server.listen(4)
            server.settimeout(0.5)
            LOG.info("Listening for session triggers on %s", self._socket_path)
            while not self._stop_event.is_set():
                try:
                    conn, _ = server.accept()
                except socket_module.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
        finally:
            with contextlib.suppress(Exception):
                server.close()
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._socket_path)


async def main_async() -> int:
    if not VOICE_API_KEY:
        env_name = _engine_defaults.get("api_key_env", "GOOGLE_API_KEY")
        LOG.error(
            "No API key for VOICE_ENGINE=%s: set VOICE_API_KEY or %s; cannot start",
            VOICE_ENGINE,
            env_name,
        )
        return 1

    agent = VoiceAgent()
    agent.set_loop(asyncio.get_event_loop())
    # Before accepting any trigger: undo whatever a crashed predecessor left
    # behind. Restart=on-failure means this is the only chance to notice.
    await asyncio.get_event_loop().run_in_executor(None, agent.recover)

    listener = VoiceSessionListener(VOICE_SESSION_SOCKET, agent)
    listener.start()

    send_led_state("idle")
    LOG.info(
        "voice-agent ready: engine=%s model=%s voice=%s idle=%.1fs max_session=%.0fs "
        "half_duplex=%s out_gain=%.2f compression=%s duck=%s preroll=%.0fs "
        "in_channel=%d in_gain=%+.0fdB source=%s",
        VOICE_ENGINE,
        VOICE_MODEL,
        VOICE_NAME,
        VOICE_IDLE_SECONDS,
        VOICE_MAX_SESSION_SECONDS,
        VOICE_HALF_DUPLEX,
        VOICE_OUTPUT_GAIN,
        VOICE_CONTEXT_COMPRESSION,
        f"{VOICE_DUCK_LEVEL:.2f}" if VOICE_DUCK else "off",
        VOICE_PREROLL_SECONDS,
        VOICE_INPUT_CHANNEL,
        VOICE_INPUT_GAIN_DB,
        VOICE_SOURCE,
    )

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        listener.stop()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    pc_logger.remove()
    pc_logger.add(sys.stderr, level="INFO")
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
