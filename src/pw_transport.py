"""Pipecat audio transport backed by `pw-cat` subprocesses.

Pipecat's built-in LocalAudioTransport uses PyAudio, which can only select a
device by numeric index -- it can't target a PipeWire node by name, and this
box's default ALSA/PipeWire source/sink don't point at the right nodes anyway
(see wake_word.py's XVF_SOURCE comment). This transport instead shells out to
`pw-cat`, targeting nodes by name exactly like wake_word.py's capture already
does, and like the AEC reference routing config does for playback.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from loguru import logger

from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pw_node import wait_for_node as _wait_for_node  # noqa: E402

CHUNK_MS = 20
# Most audio begin_capture() will hold while the pipeline is still being built.
BACKLOG_MAX_SECONDS = 10.0

# A voice session landing on the wrong node here is a fully silent failure:
# the session looks healthy but the mic is capturing the wrong thing, which
# just looks like "the assistant can't hear you." See pw_node.py.
#
# Do not "fix" that with `-P '{ node.dont-fallback = true }'`. With WirePlumber
# 0.5.8 that property destroys healthy streams the next time the policy
# re-checks them, i.e. as soon as any other stream starts or stops (tried on
# 2026-10-04: every session lost its microphone 0.2 s after opening it).


class PwCatTransportParams(TransportParams):
    """Adds PipeWire node targeting (by name) to the standard transport params."""

    input_target: str | None = None
    output_target: str | None = None
    # The XVF3800's L/R USB channels carry independently configured content
    # (confirmed during wake-word tuning), so capture is always stereo and one
    # channel is picked explicitly rather than averaged.
    input_channel_index: int = 0
    # Linear gain applied to the selected capture channel before it goes to
    # the engine, clipped to 16 bits. The XVF3800's ASR channel carries speech
    # at about -36..-42 dBFS from across the room; replaying real recordings of
    # that through Gemini (2026-10-04) went from 7 of 10 commands understood
    # to 10 of 10 with +12 dB. Applied here rather than with the chip's
    # AEC_ASROUTGAIN because wake_word.py listens to the same channel and its
    # scores do not improve with gain.
    input_gain: float = 1.0
    # Playback gain applied to the bot's own audio, 0.0-1.0. The lever that
    # decides whether voice barge-in is physically possible on this box: the
    # XVF3800's AEC residual scales with how loud the bot is, while the user's
    # voice at the mic doesn't, so turning the bot down is what moves the
    # echo-to-voice ratio. Applied in software rather than as a stream volume
    # so there's no node to resolve and no window where the first frames play
    # at full level. The AEC reference is tapped from the sink monitor, i.e.
    # downstream of this, so the reference stays consistent either way.
    output_gain: float = 1.0


class AudioGate:
    """Half-duplex mic gate: silences capture while the bot is speaking.

    Measured on this box: the XVF3800's AEC converges to about -35 dB on
    steady program material, but the first ~500 ms of every utterance leaks at
    full scale while the filter re-adapts, and even the converged residual
    (~600 peak) comes back louder than the user's own voice does at the mic
    (~400-600 peak) at normal listening volume. Ungated, Gemini Live hears its
    own output, decides it's being interrupted, and cuts off mid-sentence --
    or transcribes and answers itself.

    Zeroing the mic while the bot speaks trades voice barge-in for a
    conversation that doesn't derail. The wake button still cancels instantly.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._muted = False
        self._muted_until = 0.0
        self._mute_deadline = 0.0
        self._mute_limit = 0.0

    def mute(self, max_seconds: float = 30.0) -> None:
        """Close the gate, but never for longer than `max_seconds`.

        The gate is normally reopened by BotStoppedSpeakingFrame, which is not
        guaranteed to arrive: if the Gemini connection drops mid-utterance the
        service reconnects without ever finishing the turn, and an unbounded
        gate would leave the assistant deaf for the rest of the session. No
        legitimate bot turn runs this long, and a bit of echo is strictly
        better than a microphone that never reopens.
        """
        with self._lock:
            self._muted = True
            self._mute_limit = max_seconds
            self._mute_deadline = time.monotonic() + max_seconds

    def unmute_after(self, seconds: float) -> None:
        """Open the gate `seconds` from now -- the tail covers pw-cat's own
        output buffer, CamillaDSP's, and the room's decay, all of which are
        still audible after pipecat considers the bot done speaking."""
        with self._lock:
            self._muted = False
            self._muted_until = time.monotonic() + seconds

    def muted(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if self._muted and now >= self._mute_deadline:
                self._muted = False
                self._muted_until = 0.0
                logger.warning(
                    "AudioGate: no BotStoppedSpeaking after {:.0f}s; forcing mic open",
                    self._mute_limit,
                )
            return self._muted or now < self._muted_until


def _read_exact(stream, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class PwCatInputTransport(BaseInputTransport):
    _params: PwCatTransportParams

    def __init__(self, params: PwCatTransportParams) -> None:
        super().__init__(params)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._sample_rate = 0
        # Owned here rather than passed in so the caller can just grab
        # transport.input().gate; whoever watches the bot-speaking frames
        # drives it.
        self.gate = AudioGate()
        # Audio captured before the pipeline could take it; see begin_capture().
        self._backlog: list[InputAudioRawFrame] = []
        self._backlog_lock = threading.Lock()
        self._live = False

    def _capture_command(self) -> list[str]:
        return [
            "pw-cat",
            "--record",
            # Headerless PCM. Without --raw, pw-cat opens the stream with a
            # 24-byte AU file header, which decodes as six samples of loud
            # garbage -- the "peak amplitude = 28260" that used to start
            # every session.
            "--raw",
            "--target", self._params.input_target,
            "--rate", str(self._sample_rate),
            "--channels", "2",
            "--format", "s16",
            "--latency", "40ms",
            "-",
        ]

    def begin_capture(self, sample_rate: int | None = None) -> None:
        """Open the microphone now, ahead of the pipeline.

        The LED says "listening" the moment a session is triggered, but the
        pipeline that carries the audio takes about a second to exist: the
        engine and VAD have to be built and Gemini has to connect (1.1-1.6 s
        from trigger to connected, measured 2026-10-04). Until then nothing
        was capturing, so whoever spoke straight after "Hey Jarvis" or the
        button lost their first word or two -- "Включи монеточку" arrived as
        "Ключи монеточку", and short commands arrived as noise.

        Calling this first thing in a session starts pw-cat immediately (first
        audio about 170 ms later) and holds what it captures until start()
        has a pipeline to push it into. start() calls it itself if nobody did.
        """
        if self._proc:
            return
        self._sample_rate = sample_rate or self._params.audio_in_sample_rate or 16000
        cmd = self._capture_command()
        logger.info("PwCatInputTransport: {}", " ".join(cmd))
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._read_loop, name="pw-cat-input", daemon=True
        )
        self._thread.start()

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if self._live:
            return
        self.begin_capture(self._params.audio_in_sample_rate or frame.audio_in_sample_rate)
        target = self._params.input_target
        if target and not await _wait_for_node(target, timeout=0):
            logger.error(
                "PwCatInputTransport: target node {!r} is not in pw-dump; "
                "pw-cat has silently fallen back to the default source and "
                "this session's mic is likely wrong",
                target,
            )
        await self.set_transport_ready(frame)
        # Only now is there a queue for push_audio_frame() to put frames on;
        # the read loop releases the backlog with its next chunk.
        with self._backlog_lock:
            self._live = True

    def _read_loop(self) -> None:
        frame_samples = max(1, int(self._sample_rate * CHUNK_MS / 1000))
        frame_bytes = frame_samples * 2 * 2  # stereo, s16
        backlog_max = int(BACKLOG_MAX_SECONDS * 1000 / CHUNK_MS)
        loop = None
        channel = self._params.input_channel_index
        input_gain = self._params.input_gain
        stdout = self._proc.stdout
        assert stdout is not None
        debug = os.environ.get("PW_TRANSPORT_DEBUG", "0").lower() in {"1", "true", "yes"}
        last_log = 0.0
        peak_since_log = 0
        gated_since_log = False
        while not self._stop_event.is_set():
            raw = _read_exact(stdout, frame_bytes)
            if raw is None:
                if not self._stop_event.is_set():
                    logger.error(
                        "PwCatInputTransport: the capture ended on its own -- is {!r} "
                        "present? This session has no microphone",
                        self._params.input_target,
                    )
                break
            stereo = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2)
            mono_arr = np.ascontiguousarray(stereo[:, channel])
            peak = int(np.abs(mono_arr).max())
            gated = self.gate.muted()
            if gated:
                # Keep feeding the pipeline at the same rate -- a gap in a
                # realtime stream is ambiguous, digital silence isn't.
                mono_arr = np.zeros_like(mono_arr)
            elif input_gain != 1.0:
                mono_arr = np.clip(
                    mono_arr.astype(np.float32) * input_gain, -32768, 32767
                ).astype(np.int16)
            frame = InputAudioRawFrame(
                audio=mono_arr.tobytes(), sample_rate=self._sample_rate, num_channels=1
            )
            with self._backlog_lock:
                if self._live:
                    ready, self._backlog = self._backlog + [frame], []
                else:
                    self._backlog.append(frame)
                    del self._backlog[:-backlog_max]
                    ready = []
            if ready:
                if loop is None:
                    loop = self.get_event_loop()
                if len(ready) > 1:
                    logger.info(
                        "PwCatInputTransport: releasing {:.2f}s captured before the pipeline started",
                        (len(ready) - 1) * CHUNK_MS / 1000,
                    )
                for queued in ready:
                    asyncio.run_coroutine_threadsafe(self.push_audio_frame(queued), loop)
            if debug:
                peak_since_log = max(peak_since_log, peak)
                gated_since_log = gated or gated_since_log
                now = time.monotonic()
                if now - last_log > 1.0:
                    logger.info(
                        "PwCatInputTransport: peak amplitude last 1s = {}{}",
                        peak_since_log,
                        " (gated)" if gated_since_log else "",
                    )
                    peak_since_log = 0
                    gated_since_log = False
                    last_log = now

    def stop_capture(self) -> None:
        """Close the microphone. Safe to call twice, and the way out for a
        session that called begin_capture() and then never got as far as
        starting its pipeline."""
        self._stop_event.set()
        with self._backlog_lock:
            self._live = False
            self._backlog = []
        proc, self._proc = self._proc, None
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    async def cleanup(self) -> None:
        await super().cleanup()
        self.stop_capture()


class PwCatOutputTransport(BaseOutputTransport):
    _params: PwCatTransportParams

    def __init__(self, params: PwCatTransportParams) -> None:
        super().__init__(params)
        self._proc: subprocess.Popen | None = None
        self._sample_rate = 0
        # Single-writer executor, same rationale as pipecat's own PyAudio
        # output transport: only one task writes audio at a time.
        self._executor = ThreadPoolExecutor(max_workers=1)

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if self._proc:
            return
        self._sample_rate = self._params.audio_out_sample_rate or frame.audio_out_sample_rate
        target = self._params.output_target
        if target and not await _wait_for_node(target):
            logger.error(
                "PwCatOutputTransport: target node {!r} never appeared in "
                "pw-dump; pw-cat will silently fall back to the default sink",
                target,
            )
        cmd = self._playback_command()
        logger.info("PwCatOutputTransport: {}", " ".join(cmd))
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=0)
        await self.set_transport_ready(frame)

    def _playback_command(self) -> list[str]:
        return [
            "pw-cat",
            "--playback",
            "--raw",
            "--target", self._params.output_target,
            "--rate", str(self._sample_rate),
            "--channels", "2",
            "--format", "s16",
            "--latency", "40ms",
            "-",
        ]

    async def cleanup(self) -> None:
        await super().cleanup()
        proc, self._proc = self._proc, None
        if proc:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._executor.shutdown(wait=False)

    def _write(self, data: bytes) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(data)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        proc = self._proc
        if not proc or not proc.stdin:
            return False
        # Gemini Live outputs mono; duplicate into interleaved stereo for the
        # loopback sink, same convention as the smartspk ALSA route's
        # "duplicate" policy for mono TTS elsewhere in this system.
        mono = np.frombuffer(frame.audio, dtype=np.int16)
        gain = self._params.output_gain
        if gain < 1.0:
            # Gain is capped at 1.0 by the caller, so there is nothing to clip.
            mono = (mono * gain).astype(np.int16)
        stereo = np.repeat(mono, 2).tobytes()
        try:
            await self.get_event_loop().run_in_executor(self._executor, self._write, stereo)
        except (BrokenPipeError, OSError):
            return False
        return True


class PwCatTransport(BaseTransport):
    """Complete pw-cat-backed transport with input and output capabilities."""

    def __init__(self, params: PwCatTransportParams) -> None:
        super().__init__()
        self._params = params
        self._input: PwCatInputTransport | None = None
        self._output: PwCatOutputTransport | None = None

    def input(self) -> PwCatInputTransport:
        if not self._input:
            self._input = PwCatInputTransport(self._params)
        return self._input

    def output(self) -> PwCatOutputTransport:
        if not self._output:
            self._output = PwCatOutputTransport(self._params)
        return self._output
