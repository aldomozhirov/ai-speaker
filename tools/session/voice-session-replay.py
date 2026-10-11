#!/usr/bin/env python3
"""Run real voice sessions against Gemini with a recording as the microphone.

The session code can't be tested by ear from here, and the failures that
matter are timing failures: what the user says in the first second after
"Hey Jarvis" either reaches Gemini or it doesn't. This drives the real
voice_agent.py session code (transport, pipeline, engine, tools schema, system
prompt) with two substitutions:

  * the microphone is an emulator process that behaves like a live mic: it is
    told when the session was triggered, and whatever "was said" before the
    capture actually opened is gone, exactly as it would be in the room;
  * the speaker is a file, so nothing is played.

CamillaDSP, the LED, ducking, chimes and every tool are stubbed out: no
profile switch, no volume change, no Spotify, no timers.

    voice-session-replay.py units
        Closing-phrase matching, the pre-roll buffer, and a button press
        during start-up. No network.

    voice-session-replay.py session CLIP.wav [--delay 0.3] [--preroll 10]
                                             [--prompt new|old] [--runs 2]
        One or more real Gemini Live sessions (uses the API key from
        ~/.config/ai-speaker/voice-agent.env). CLIP is what the user says,
        starting --delay seconds after the trigger. --preroll 0 reproduces
        the behaviour before 2026-10-04.

Run with the voice-agent venv, e.g. against the staged files:
    ~/.venvs/voice-agent/bin/python tools/session/voice-session-replay.py \\
        --module-dir src units
"""

from __future__ import annotations

import sys
import time

BYTES_PER_SECOND = 16000 * 2 * 2  # 16 kHz, 2 channels, s16


def emulate_mic(path: str, t0: float, latency: float, backlog: float = 0.0) -> int:
    """Stream `path` as a live microphone that has been "on air" since t0.

    The file starts `backlog` seconds before t0. With backlog 0 this is a
    capture opened at the trigger: whatever was said before it opened is gone.
    With backlog > 0 it is a capture that was already running and kept a
    buffer: everything up to "now" is handed over at once, then real time.
    """
    time.sleep(latency)  # a real pw-cat needs ~170 ms before audio flows
    data = open(path, "rb").read()
    start = time.time()
    offset = int((start - t0 + backlog) * BYTES_PER_SECOND) // 4 * 4
    chunk = BYTES_PER_SECOND // 50
    sent = 0
    out = sys.stdout.buffer
    try:
        if backlog > 0:
            out.write(data[:offset])
            out.flush()
        while offset + sent < len(data):
            out.write(data[offset + sent: offset + sent + chunk])
            out.flush()
            sent += chunk
            wait = start + sent / BYTES_PER_SECOND - time.time()
            if wait > 0:
                time.sleep(wait)
    except BrokenPipeError:
        pass
    return 0


if len(sys.argv) > 1 and sys.argv[1] == "--emulate-mic":
    sys.exit(emulate_mic(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]),
                         float(sys.argv[5]) if len(sys.argv) > 5 else 0.0))

import argparse  # noqa: E402
import asyncio  # noqa: E402
import functools  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402
import wave  # noqa: E402

import numpy as np  # noqa: E402

ENV_FILE = os.path.expanduser("~/.config/ai-speaker/voice-agent.env")
va = None  # the voice_agent module
events: list[tuple[float, str]] = []
T0 = [0.0]


def note(message: str) -> None:
    events.append((time.time() - T0[0], message))


def load_modules(module_dir: str, idle_seconds: float):
    """Import voice_agent.py from `module_dir` with every side effect stubbed."""
    global va
    for line in open(ENV_FILE):
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.strip().partition("=")
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))
    scratch = tempfile.mkdtemp(prefix="voice-replay-")
    os.environ.update({
        "VOICE_DUCK": "0",
        "SOUND_FX_ENABLED": "0",
        "PW_TRANSPORT_DEBUG": "0",
        "VOICE_IDLE_SECONDS": str(idle_seconds),
        "VOICE_STATE_FILE": os.path.join(scratch, "state.json"),
        "VOICE_SESSION_SOCKET": os.path.join(scratch, "session.sock"),
        "ASSISTANT_LED_SOCKET": os.path.join(scratch, "no-led.sock"),
        "VOICE_MODEL": "models/gemini-2.5-flash-native-audio-preview-12-2025",
        "VOICE_NAME": "Puck",
        "VOICE_INPUT_CHANNEL": "0",
        "VOICE_HALF_DUPLEX": "1",
    })
    sys.path.insert(0, os.path.expanduser("~/.local/bin"))
    sys.path.insert(0, os.path.abspath(os.path.expanduser(module_dir)))
    import voice_agent

    va = voice_agent
    va.send_led_state = lambda state: note(f"LED {state}")
    va.sound_fx.play = lambda name: None

    calls: list = []

    def stub(real):
        @functools.wraps(real)
        async def fake(params, **kwargs):
            calls.append((real.__name__, kwargs))
            note(f"TOOL {real.__name__}({kwargs})")
            # Like the real tools: a completed action ends the session once
            # the assistant has confirmed it, and playback starts after that.
            complete = getattr(va.session_signal, "complete_quick_action", None)
            if real.__name__ == "get_timer_alarm_status":
                await params.result_callback({"status": "ok"})
            elif complete is None:  # session code from before 2026-10-04 19:30
                if real.__name__ in ("spotify_play", "spotify_play_track"):
                    note("MUSIC STARTS")
                va.session_signal.mark_quick_action()
                await params.result_callback({"status": "ok"})
            elif real.__name__ in ("spotify_play", "spotify_play_track"):
                async def start():
                    note("MUSIC STARTS")

                await complete(params, {"status": "ok"}, then=start)
            else:
                await complete(params, {"status": "ok"})

        return fake

    for name in ("spotify_play", "spotify_pause", "spotify_next", "spotify_previous",
                 "spotify_play_track", "set_volume", "adjust_volume", "set_timer",
                 "cancel_timer", "set_alarm", "cancel_alarm", "get_timer_alarm_status"):
        setattr(va, name, stub(getattr(va, name)))
    va.replay_tool_calls = calls

    class Capture(logging.Handler):
        def emit(self, record):
            note(record.getMessage())

    logging.getLogger("voice-agent").addHandler(Capture())
    logging.getLogger("voice-agent").setLevel(logging.INFO)
    va.pc_logger.remove()
    va.pc_logger.add(lambda m: note(m.record["message"]), level="INFO")
    return scratch


class FakeDsp:
    def __init__(self, delay: float = 0.0) -> None:
        self._delay = delay

    def to_voice(self):
        time.sleep(self._delay)

    def restore(self):
        pass

    def recover(self):
        pass


def make_agent():
    agent = va.VoiceAgent()
    agent._dsp = FakeDsp()
    agent._ducker = None
    agent.set_loop(asyncio.get_event_loop())
    return agent


def prepare_mic(clip_path: str, delay: float, scratch: str) -> str:
    """[delay of room tone][the clip][40 s of room tone], as 2-channel raw PCM."""
    with wave.open(clip_path, "rb") as f:
        assert f.getframerate() == 16000 and f.getsampwidth() == 2, clip_path
        clip = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).reshape(-1, f.getnchannels())[:, 0]
    rng = np.random.default_rng(5)
    tone = lambda seconds: (rng.standard_normal(int(seconds * 16000)) * 30).astype(np.int16)  # noqa: E731
    mono = np.concatenate([tone(delay), clip, tone(40.0)])
    path = os.path.join(scratch, "mic.raw")
    np.repeat(mono, 2).tofile(path)
    return path


def patch_transport(mic_path: str | None, scratch: str, backlog: float = 0.0) -> str:
    import pw_transport

    out_path = os.path.join(scratch, "speaker.raw")
    if mic_path is not None:
        pw_transport.PwCatInputTransport._capture_command = lambda self: [
            sys.executable, os.path.abspath(__file__), "--emulate-mic", mic_path, repr(T0[0]), "0.17",
            repr(backlog)]
    pw_transport.PwCatOutputTransport._playback_command = lambda self: ["sh", "-c", f"cat > '{out_path}'"]
    return out_path


# --- units -------------------------------------------------------------------


async def units(scratch: str) -> int:
    failed = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failed
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}{'  -- ' + detail if detail else ''}", flush=True)

    # 1. Closing phrases.
    closing = ["Пока.", "Спасибо, пока", "Пока, пока!", "До свидания!", "Всё, хватит", "на этом все",
               "На этом всё, спасибо.", "Больше ничего", "Goodbye!", "that’s all, thanks", "Never mind.",
               "ничего не надо", "Отбой"]
    staying = ["Покажи погоду", "Включи музыку, пока я готовлю", "Пока не надо", "Какая сейчас погода?",
               "Прибавить музыку погромче.", "Включи Монеточку на Spotify", "", "goodbyes are hard", "показать",
               "<noise>"]
    wrong = [t for t in closing if not va.is_closing_phrase(t)] + [t for t in staying if va.is_closing_phrase(t)]
    check(not wrong, f"closing phrases: {len(closing)} goodbyes end the session, {len(staying)} other sentences don't",
          f"wrong: {wrong}" if wrong else "")

    # 2. The pre-roll buffer in the Gemini service, with a fake session.
    from pipecat.frames.frames import InputAudioRawFrame

    service = va.voice_engine.build_engine("gemini", va.voice_engine.EngineConfig(
        api_key="unit-test", model=va.VOICE_MODEL, voice="Puck", system_prompt="x",
        context_compression_enabled=False, context_compression_trigger_tokens=None,
        preroll_seconds=1.01)).service

    class FakeSession:
        def __init__(self):
            self.sent = []

        async def send_realtime_input(self, audio=None, **kw):
            self.sent.append(audio.data)

    frames = [InputAudioRawFrame(audio=bytes([i % 256]) * 640, sample_rate=16000, num_channels=1) for i in range(100)]
    service._session, service._ready_for_realtime_input = None, False
    for frame in frames[:99]:
        await service._send_user_audio(frame)
    session = FakeSession()
    service._session, service._ready_for_realtime_input = session, True
    await service._send_user_audio(frames[99])
    # half a second of silence, then the last second held (50 frames), then the live one
    expected = [bytes(16000)] + [f.audio for f in frames[49:]]
    check(session.sent == expected,
          "pre-roll: 0.5 s of silence, then the held audio in order (capped at the limit), then live",
          f"{len(session.sent)} chunks sent, expected {len(expected)}")
    await service._send_user_audio(frames[0])
    check(len(session.sent) == len(expected) + 1, "pre-roll: live audio passes straight through afterwards")

    plain = va.voice_engine.build_engine("gemini", va.voice_engine.EngineConfig(
        api_key="unit-test", model=va.VOICE_MODEL, voice="Puck", system_prompt="x",
        context_compression_enabled=False, context_compression_trigger_tokens=None)).service
    check(type(plain).__name__ == "GeminiLiveLLMService", "pre-roll: preroll_seconds=0 uses the stock service")

    # 3. The transport holds audio until the pipeline can take it, and a
    #    session cancelled during start-up leaves nothing behind.
    mic = prepare_mic(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wake", "wake-test-clips", "ru_cmd_volume.wav"),
                      0.0, scratch)
    patch_transport(mic, scratch)
    import pw_transport

    T0[0] = time.time()
    transport = pw_transport.PwCatTransport(pw_transport.PwCatTransportParams(
        audio_in_enabled=True, audio_in_sample_rate=16000, audio_in_channels=1, input_target="unit",
        input_channel_index=0))
    inp = transport.input()
    inp.begin_capture()
    await asyncio.sleep(1.0)
    held = len(inp._backlog)
    inp.stop_capture()
    check(35 <= held <= 45 and inp._proc is None and not inp._backlog,
          "transport: begin_capture() holds what it hears (about 0.83 s after 1 s), stop_capture() cleans up",
          f"{held * 0.02:.2f} s held")

    agent = make_agent()
    # Start-up only yields to other events while CamillaDSP (and ducking) are
    # being switched; stretch that so the second press reliably lands in it.
    agent._dsp = FakeDsp(delay=0.3)
    events.clear()
    T0[0] = time.time()
    await agent._handle_trigger_async("button")
    await asyncio.sleep(0.05)
    await agent._handle_trigger_async("button")  # pressed again while still starting up
    for _ in range(100):
        await asyncio.sleep(0.1)
        if not agent._active:
            break
    started = sum("Starting session" in m for _, m in events)
    skipped = any("not running this session" in m for _, m in events)
    connected = any("Connecting to Gemini" in m for _, m in events)
    check(started == 1 and skipped and not connected and not agent._active,
          "button during start-up: one session, cancelled before it connects, agent idle again",
          f"sessions started={started}, connected={connected}, active={agent._active}")
    # Children of this process only, and "[-]" so the shell running pgrep
    # doesn't match its own command line.
    leftovers = os.popen(f"pgrep -P {os.getpid()} -f -- '[-]-emulate-mic' | wc -l").read().strip()
    check(leftovers == "0", "no capture process left running", f"{leftovers} left")

    print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
    return 1 if failed else 0


# --- real sessions -------------------------------------------------------------


OLD_PROMPT_END = "say a brief goodbye."


async def one_session(mic_path: str, preroll: float, prompt: str, scratch: str, extra: str = "",
                      backlog: float = 0.0) -> dict:
    va.VOICE_PREROLL_SECONDS = preroll
    full = va.VOICE_SYSTEM_PROMPT
    saved = full
    if prompt == "old":
        va.VOICE_SYSTEM_PROMPT = full[: full.index(OLD_PROMPT_END) + len(OLD_PROMPT_END)]
    if extra:
        va.VOICE_SYSTEM_PROMPT += " " + extra
    agent = make_agent()
    events.clear()
    va.replay_tool_calls.clear()
    T0[0] = time.time()
    patch_transport(mic_path, scratch, backlog)
    agent._active = True
    try:
        await asyncio.wait_for(agent._run_session(), timeout=90)
    finally:
        va.VOICE_SYSTEM_PROMPT = saved

    def first(pattern):
        return next((t for t, m in events if re.search(pattern, m)), None)

    connected = [t for t, m in events if "Connected to Gemini service" in m]
    return {
        "connected_at": connected[-1] if connected else None,
        "released": next((m for _, m in events if "captured before" in m and "releasing" in m), None),
        "sent": next((m for _, m in events if "captured before Gemini was ready" in m), None),
        "user": [m.split("User transcription: ", 1)[1] for _, m in events if "User transcription: " in m],
        "assistant": [m.split("Assistant said: ", 1)[1] for _, m in events if "Assistant said: " in m],
        "tools": list(va.replay_tool_calls),
        "ended_at": first("Session ended"),
        "errors": [m for _, m in events if "ERROR" in m or "error" in m.lower() and "Pipeline error" in m],
    }


async def sessions(args, scratch: str) -> int:
    # --real-mic: leave the transport's own pw-cat capture command in place,
    # so the session records the actual microphone on the live PipeWire graph.
    mic = None if args.real_mic else prepare_mic(args.clip, args.delay, scratch)
    with_tool = 0
    for run in range(args.runs):
        r = await one_session(mic, args.preroll, args.prompt, scratch, args.prompt_extra)
        with_tool += bool(r["tools"])
        print(f"--- run {run + 1}: preroll={args.preroll:g}s prompt={args.prompt} "
              + ("REAL microphone" if args.real_mic else f"speech starts {args.delay:g}s after the trigger"),
              flush=True)
        if args.real_mic:
            for t, m in events:
                if "PwCatInputTransport" in m or "capture" in m.lower() or "ERROR" in m:
                    print(f"    +{t:5.2f}s {m[:150]}")
        if r["connected_at"] is not None:
            print(f"    Gemini connected {r['connected_at']:.2f}s after the trigger")
        for key in ("released", "sent"):
            if r[key]:
                print(f"    {r[key]}")
        print(f"    heard:     {' | '.join(r['user']) or '(nothing)'}")
        print(f"    tools:     {r['tools'] or '(none)'}")
        print(f"    answered:  {' | '.join(r['assistant']) or '(nothing)'}")
        if r["errors"]:
            print(f"    errors:    {r['errors']}")
        await asyncio.sleep(1.0)
    print(f"=== a tool was called in {with_tool} of {args.runs} session(s)")
    return 0


def prepare_capture(path: str, channel: int, pre: float, gain_db: float, scratch: str, lead: float = 0.0) -> str:
    """A wake capture (wake_capture.py) as microphone audio starting `pre`
    seconds before the moment the wake word fired."""
    import json

    with wave.open(path, "rb") as f:
        audio = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).reshape(-1, f.getnchannels())
    meta = json.load(open(path[:-4] + ".json"))
    at = (meta["fired_frames"][0] if meta["fired_frames"] else round(meta["peak_seconds"] / meta["frame_seconds"]))
    start = max(0, int((at * meta["frame_seconds"] - pre) * 16000))
    if channel < 0:  # native: both channels exactly as recorded
        # Followed by the room as it actually sounded: the quietest second of
        # the recording, repeated (synthetic noise would not survive the gain
        # the session code applies).
        win = 16000
        energies = [float(np.mean(audio[i:i + win].astype(np.float64) ** 2))
                    for i in range(0, len(audio) - win, 4000)]
        quiet = int(np.argmin(energies)) * 4000
        tone = np.tile(audio[quiet:quiet + win], (30, 1))
        out = os.path.join(scratch, "mic.raw")
        np.concatenate([audio[start:], tone]).astype(np.int16).tofile(out)
        return out
    mono = audio[start:, channel].astype(np.float64) * 10 ** (gain_db / 20)
    rng = np.random.default_rng(5)
    tone = (rng.standard_normal(30 * 16000) * 20)
    # `lead` seconds of digital silence in front, as the session code could add itself
    mono = np.clip(np.concatenate([np.zeros(int(lead * 16000)), mono, tone]), -32768, 32767).astype(np.int16)
    out = os.path.join(scratch, "mic.raw")
    np.repeat(mono, 2).tofile(out)
    return out


async def captures(args, scratch: str) -> int:
    for path in args.files:
        if args.native:
            # The session code picks the channel and applies gain and lead itself.
            va.VOICE_INPUT_CHANNEL, va.VOICE_INPUT_GAIN_DB = args.channel, args.gain_db
            mic = prepare_capture(path, -1, args.pre, 0.0, scratch, 0.0)
        else:
            mic = prepare_capture(path, args.channel, args.pre, args.gain_db, scratch, args.lead)
        name = os.path.basename(path)[9:-4]
        for run in range(args.runs):
            r = await one_session(mic, args.preroll, args.prompt, scratch, args.prompt_extra,
                                  args.pre + (0.0 if args.native else args.lead))
            print(f"{name} ch{args.channel} pre={args.pre:g}s preroll={args.preroll:g}s: "
                  f"heard {' | '.join(r['user']) or '(nothing)'} || tools {r['tools'] or '-'} || "
                  f"said {' | '.join(r['assistant']) or '(nothing)'}", flush=True)
            await asyncio.sleep(0.5)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--module-dir", default="~/.local/bin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("units")
    sp = sub.add_parser("session")
    sp.add_argument("clip", nargs="?")
    sp.add_argument("--real-mic", action="store_true",
                    help="record the real microphone instead of replaying CLIP (speaker and tools still stubbed)")
    sp.add_argument("--delay", type=float, default=0.3)
    sp.add_argument("--preroll", type=float, default=10.0)
    sp.add_argument("--prompt", choices=("new", "old"), default="new")
    sp.add_argument("--runs", type=int, default=1)
    sp.add_argument("--prompt-extra", default="", help="text appended to the system prompt, for trying wording")
    sp.add_argument("--idle", type=float, default=6.0)
    cp = sub.add_parser("capture", help="replay wake captures as the microphone")
    cp.add_argument("files", nargs="+")
    cp.add_argument("--channel", type=int, default=0)
    cp.add_argument("--pre", type=float, default=0.0,
                    help="seconds before the trigger to include, as an already-running capture would have")
    cp.add_argument("--gain-db", type=float, default=0.0)
    cp.add_argument("--lead", type=float, default=0.0, help="seconds of digital silence put in front of the audio")
    cp.add_argument("--preroll", type=float, default=10.0)
    cp.add_argument("--prompt", choices=("new", "old"), default="new")
    cp.add_argument("--prompt-extra", default="")
    cp.add_argument("--runs", type=int, default=1)
    cp.add_argument("--native", action="store_true",
                    help="feed both recorded channels as they are; --channel and --gain-db then set the "
                         "session code's own VOICE_INPUT_CHANNEL / VOICE_INPUT_GAIN_DB")
    cp.add_argument("--idle", type=float, default=8.0)
    args = ap.parse_args()

    if args.cmd == "session" and not args.clip and not args.real_mic:
        ap.error("session needs a CLIP or --real-mic")
    logging.basicConfig(level=logging.WARNING)
    scratch = load_modules(args.module_dir, getattr(args, "idle", 6.0))
    if args.cmd == "units":
        return asyncio.run(units(scratch))
    if args.cmd == "capture":
        return asyncio.run(captures(args, scratch))
    return asyncio.run(sessions(args, scratch))


if __name__ == "__main__":
    sys.exit(main())
