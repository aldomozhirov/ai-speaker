#!/usr/bin/env python3
"""Checks for the voice session's CamillaDSP switch (DspProfileSwitcher).

Since 2026-10-04 a session applies only the voice profile's processing and
keeps the buffering that is already running, so the echo canceller's reference
stays aligned (see the class docstring in voice_agent.py). Nothing here plays a
sound.

    dsp-switch-check.py units
        The switcher against a fake CamillaDSP: apply, restore, the cases
        where it must leave things alone, a failed restore, crash recovery.

    dsp-switch-check.py live
        The switcher against the real CamillaDSP. Applies the voice processing
        and puts the previous configuration back, three times over, and checks
        that the DAC was never restarted (its ALSA trigger time and buffer
        geometry stay the same). Refuses to run while audio is playing, since a
        change of filters is audible then; --force overrides.

    dsp-switch-check.py session
        One real voice session on the live PipeWire graph: real microphone,
        real Gemini connection, the real switcher -- speaker written to a file,
        tools, LED, chimes and ducking stubbed. Nobody has to say anything; it
        ends on the idle timeout. Checks what `live` checks, plus that the
        voice processing is in effect while the session runs.

Run with the voice-agent venv:
    ~/.venvs/voice-agent/bin/python tools/aec/dsp-switch-check.py \\
        --module-dir src units
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import os
import statistics
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DAC = "/proc/asound/sndrpihifiberry/pcm0p/sub0"
LOOPBACK_CAPTURE = "/proc/asound/Loopback/pcm1c/sub0"
LIVE_STATE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "voice-agent-state.json")

failed = 0


def check(ok: bool, label: str, detail: str = "") -> bool:
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label}{'  -- ' + detail if detail else ''}", flush=True)
    return ok


def load(module_dir: str, idle: float = 6.0):
    """The session code from `module_dir`, imported the way the replay harness
    imports it: API key from the env file, LED and tools stubbed, its state
    file in a scratch directory."""
    spec = importlib.util.spec_from_file_location("voice_session_replay", os.path.join(HERE, "voice-session-replay.py"))
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    scratch = harness.load_modules(module_dir, idle)
    return harness, harness.va, scratch


# --- units ---------------------------------------------------------------------

MUSIC = {
    "title": "music",
    "devices": {"chunksize": 1024, "target_level": 3072, "samplerate": 48000},
    "filters": {"bass_shelf": {"gain": 4.0}, "woofer_highpass": {"freq": 80.0}},
    "processors": None,
    "pipeline": [{"type": "Filter", "names": ["bass_shelf", "woofer_highpass"]}],
}
VOICE_FILE = {
    "title": "voice",
    "devices": {"chunksize": 512, "target_level": 1024, "samplerate": 48000},
    "filters": {"woofer_highpass": {"freq": 105.0}},
    "processors": {"voice_leveler": {"factor": 1.7}},
    "pipeline": [{"type": "Processor", "name": "voice_leveler"}, {"type": "Filter", "names": ["woofer_highpass"]}],
}
NIGHT = dict(MUSIC, title="night", filters={"woofer_highpass": {"freq": 60.0}})
VOICE_PATH = "/profiles/voice.yml"


class FakeDsp:
    """As much of CamillaClient as the switcher uses."""

    def __init__(self, running: dict | None) -> None:
        self.running = copy.deepcopy(running)
        self.down = False
        self.applied: list[str] = []
        self.forbidden: list[str] = []
        self.config, self.general = self, self

    def _alive(self) -> None:
        if self.down:
            raise IOError("Lost connection to CamillaDSP")

    def is_connected(self) -> bool:
        return True

    def connect(self) -> None:
        self._alive()

    def active(self):
        self._alive()
        return copy.deepcopy(self.running)

    def read_and_parse_file(self, path: str) -> dict:
        self._alive()
        assert path == VOICE_PATH, path
        return copy.deepcopy(VOICE_FILE)

    def set_active(self, config: dict) -> None:
        self._alive()
        self.running = copy.deepcopy(config)
        self.applied.append(config["title"])

    # The way of switching that restarts the audio devices; must not be used.
    def file_path(self):
        self.forbidden.append("file_path")
        return "/etc/camilladsp/active.yml"

    def set_file_path(self, path) -> None:
        self.forbidden.append("set_file_path")

    def reload(self) -> None:
        self.forbidden.append("reload")


def units(va, scratch: str) -> None:
    def make(running, name):
        state = va.RuntimeState(os.path.join(scratch, f"{name}.json"))
        switcher = va.DspProfileSwitcher("unused", 0, "/profiles", "voice", state)
        switcher._client = dsp = FakeDsp(running)
        return switcher, dsp, state

    key = va.DspProfileSwitcher.STATE_KEY
    voice_on_music = dict(VOICE_FILE, devices=MUSIC["devices"])

    sw, dsp, state = make(MUSIC, "apply")
    sw.to_voice()
    check(dsp.running == voice_on_music and dsp.running["devices"] == MUSIC["devices"],
          "to_voice: the voice profile's processing with the buffering that was running",
          f"devices now {dsp.running['devices']}")
    check(state.get(key) == MUSIC, "to_voice: what was running is recorded for crash recovery")
    sw.restore()
    check(dsp.running == MUSIC and state.get(key) is None and dsp.applied == ["voice", "music"],
          "restore: the previous configuration is back and the record is cleared", f"applied {dsp.applied}")
    check(not dsp.forbidden, "neither direction touches the config file path or reloads", f"{dsp.forbidden}")

    sw, dsp, state = make(VOICE_FILE, "by-hand")
    sw.to_voice()
    sw.restore()
    check(dsp.applied == [] and dsp.running == VOICE_FILE and state.get(key) is None,
          "voice profile already selected by hand: nothing applied, nothing to undo", f"applied {dsp.applied}")

    sw, dsp, state = make(MUSIC, "changed")
    sw.to_voice()
    dsp.running = copy.deepcopy(NIGHT)
    sw.restore()
    check(dsp.running == NIGHT and state.get(key) is None,
          "configuration changed by someone else during the session: left alone, record cleared")
    sw.to_voice()
    sw.restore()
    check(dsp.running == NIGHT and dsp.applied[-2:] == ["voice", "night"],
          "... and the next session switches and restores normally from there", f"applied {dsp.applied}")

    sw, dsp, state = make(MUSIC, "restore-fails")
    sw.to_voice()
    dsp.down = True
    try:
        sw.restore()
        raised = False
    except Exception:
        raised = True
    check(raised and state.get(key) == MUSIC, "restore while CamillaDSP is unreachable: raises, record kept")
    dsp.down = False
    sw.to_voice()   # the next session: still on the voice processing
    sw.restore()
    check(dsp.running == MUSIC and dsp.applied == ["voice", "music"] and state.get(key) is None,
          "... and the next session's restore puts the music profile back", f"applied {dsp.applied}")

    sw, dsp, state = make(MUSIC, "crash")
    sw.to_voice()
    after_crash = va.DspProfileSwitcher("unused", 0, "/profiles", "voice", state)
    after_crash._client = dsp
    after_crash.recover()
    check(dsp.running == MUSIC and state.get(key) is None,
          "recover: a session that died leaves the voice processing on; the restart puts the music profile back")

    sw, dsp, state = make(MUSIC, "crash-stale")
    sw.to_voice()
    dsp.running = copy.deepcopy(NIGHT)
    after_crash = va.DspProfileSwitcher("unused", 0, "/profiles", "voice", state)
    after_crash._client = dsp
    after_crash.recover()
    check(dsp.running == NIGHT and state.get(key) is None,
          "recover: configuration changed since the crash: left alone, record discarded")

    sw, dsp, state = make(MUSIC, "crash-down")
    sw.to_voice()
    dsp.down = True
    after_crash = va.DspProfileSwitcher("unused", 0, "/profiles", "voice", state)
    after_crash._client = dsp
    after_crash.recover()
    check(state.get(key) == MUSIC, "recover while CamillaDSP is unreachable: no exception, record kept")

    sw, dsp, state = make(None, "inactive")
    try:
        sw.to_voice()
        raised = False
    except RuntimeError:
        raised = True
    check(raised and dsp.applied == [], "CamillaDSP without an active configuration: to_voice raises, nothing applied")


# --- the real CamillaDSP ---------------------------------------------------------


def proc(path: str) -> dict:
    out = {}
    with open(path) as f:
        for line in f:
            if ":" in line:
                name, value = line.split(":", 1)
                out[name.strip()] = value.strip()
    return out


def devices() -> dict:
    """What identifies the running ALSA streams: when each was started and
    with which buffer geometry. A restart of CamillaDSP's devices changes it."""
    hw = proc(DAC + "/hw_params")
    return {
        "dac_started": proc(DAC + "/status").get("trigger_time"),
        "loopback_started": proc(LOOPBACK_CAPTURE + "/status").get("trigger_time"),
        "period": hw.get("period_size"),
        "buffer": hw.get("buffer_size"),
    }


def dac_fill(seconds: float = 1.0) -> float:
    """Mean audio waiting in the DAC's buffer, ms."""
    values, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            values.append(int(proc(DAC + "/status").get("delay", "0")))
        except (OSError, ValueError):
            pass
        time.sleep(0.011)
    return statistics.mean(values) / 48 if values else float("nan")


class Dsp:
    """Read-only view of the real CamillaDSP, on a connection of its own."""

    def __init__(self, va) -> None:
        from camilladsp import CamillaClient

        self.client = CamillaClient(va.CAMILLA_HOST, va.CAMILLA_PORT)
        self.client.connect()
        self.voice_file = self.client.config.read_and_parse_file(
            os.path.join(va.CAMILLA_PROFILES_DIR, f"{va.VOICE_PROFILE}.yml"))

    def active(self) -> dict:
        return self.client.config.active()

    def has_voice_processing(self, config: dict | None = None) -> bool:
        config = config or self.active()
        return all(config.get(k) == v for k, v in self.voice_file.items() if k != "devices")

    def input_level(self) -> float:
        return max(self.client.levels.capture_rms())


def preconditions(dsp: Dsp, force: bool) -> bool:
    if os.path.exists(LIVE_STATE):
        print("A voice session is active (its state file exists); not touching the DSP.")
        return False
    level = dsp.input_level()
    if level > -90 and not force:
        print(f"Audio is playing ({level:.0f} dB at the DSP input): a change of filters would be audible. "
              "Pause it, or pass --force.")
        return False
    return True


def put_back(dsp: Dsp, base: dict, base_path: str) -> None:
    """Whatever happened, leave the DSP as it was found."""
    try:
        if dsp.client.config.file_path() != base_path:
            dsp.client.config.set_file_path(base_path)
            dsp.client.general.reload()
            time.sleep(1.5)
        if dsp.active() != base:
            dsp.client.config.set_active(base)
            time.sleep(0.5)
    except Exception as exc:
        print(f"!! could not verify the DSP configuration afterwards: {exc}")
    check(dsp.active() == base and dsp.client.config.file_path() == base_path,
          "afterwards: CamillaDSP has the configuration and config path it started with")


def live(va, scratch: str, force: bool) -> None:
    dsp = Dsp(va)
    if not preconditions(dsp, force):
        global failed
        failed += 1
        return
    base, base_path = dsp.active(), dsp.client.config.file_path()
    before, fill_before = devices(), dac_fill()
    print(f"found: {base.get('title')!r} from {base_path}; devices {before}; DAC holds {fill_before:.1f} ms")
    state = va.RuntimeState(os.path.join(scratch, "live-state.json"))
    key = va.DspProfileSwitcher.STATE_KEY

    def switcher():
        return va.DspProfileSwitcher(va.CAMILLA_HOST, va.CAMILLA_PORT, va.CAMILLA_PROFILES_DIR, va.VOICE_PROFILE,
                                     state)

    try:
        sw = switcher()
        took = []
        for round_ in (1, 2, 3):
            t = time.monotonic()
            sw.to_voice()
            took.append(time.monotonic() - t)
            time.sleep(0.3)
            now = dsp.active()
            ok = dsp.has_voice_processing(now) and now["devices"] == base["devices"]
            fill = dac_fill()
            check(ok and devices() == before and abs(fill - fill_before) < 3.0,
                  f"round {round_}: voice processing in effect, buffering as before, DAC not restarted",
                  f"title {now.get('title')!r}, DAC holds {fill:.1f} ms, devices {devices()}")
            check(state.get(key) == base, f"round {round_}: previous configuration recorded")
            t = time.monotonic()
            sw.restore()
            took.append(time.monotonic() - t)
            time.sleep(0.3)
            check(dsp.active() == base and devices() == before and state.get(key) is None,
                  f"round {round_}: previous configuration back, DAC not restarted, record cleared")
        check(max(took) < 0.5, "each switch returns quickly",
              "to_voice/restore took " + ", ".join(f"{1000 * t:.0f}" for t in took) + " ms")

        sw.to_voice()
        time.sleep(0.3)
        after_crash = switcher()   # as if the daemon had been restarted mid-session
        after_crash.recover()
        time.sleep(0.3)
        check(dsp.active() == base and devices() == before and state.get(key) is None,
              "crash recovery: a new switcher finds the record and puts the previous configuration back")
        check(dsp.client.config.file_path() == base_path, "the config path never moved", base_path)
    finally:
        put_back(dsp, base, base_path)
        check(not os.path.exists(LIVE_STATE), "no real voice session started during the check (else: run it again)")


async def session(harness, va, scratch: str, force: bool) -> None:
    dsp = Dsp(va)
    if not preconditions(dsp, force):
        global failed
        failed += 1
        return
    base, base_path = dsp.active(), dsp.client.config.file_path()
    before, fill_before = devices(), dac_fill()
    print(f"found: {base.get('title')!r}; devices {before}; DAC holds {fill_before:.1f} ms")

    # As the live unit runs it, except for what must stay silent.
    va.VOICE_INPUT_CHANNEL, va.VOICE_INPUT_GAIN_DB = 1, 12.0
    speaker = harness.patch_transport(None, scratch)   # real microphone, speaker to a file
    agent = va.VoiceAgent()                            # with its real DspProfileSwitcher
    agent._ducker = None
    agent.set_loop(asyncio.get_event_loop())
    state_file = os.environ["VOICE_STATE_FILE"]

    seen: list[tuple[float, bool, dict, bool]] = []
    stop = threading.Event()

    def watch() -> None:
        watcher = Dsp(va)
        while not stop.is_set():
            config = watcher.active()
            seen.append((time.time() - harness.T0[0], watcher.has_voice_processing(config),
                         config["devices"], os.path.exists(state_file)))
            stop.wait(0.25)

    harness.events.clear()
    harness.T0[0] = time.time()
    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    agent._active = True
    try:
        await asyncio.wait_for(agent._run_session(), timeout=60)
    finally:
        await asyncio.sleep(0.6)
        stop.set()
        thread.join(timeout=2)
        try:
            events = [(t, m) for t, m in harness.events]

            def at(text):
                return next((t for t, m in events if text in m), None)

            for t, m in events:
                if any(s in m for s in ("CamillaDSP", "PwCatInputTransport: pw-cat", "releasing", "Connected to Gemini",
                                        "Idle timeout", "Session ended", "ERROR", "abnormally", "Could not")):
                    print(f"    +{t:5.2f}s {m[:140]}")
            voice = [t for t, is_voice, _, _ in seen if is_voice]
            check(bool(voice) and at("Connected to Gemini") is not None and at("Session ended") is not None,
                  "a real session ran: microphone opened, Gemini connected, ended by itself",
                  f"voice processing seen from +{voice[0]:.2f}s to +{voice[-1]:.2f}s" if voice else "never saw it")
            check(all(d == base["devices"] for _, _, d, _ in seen),
                  "buffering (the devices section) never changed while it ran", f"{len(seen)} samples")
            check(any(recorded for _, is_voice, _, recorded in seen if is_voice),
                  "the previous configuration was on record while the session ran")
            check(not any("abnormally" in m or "Could not restore" in m or "Failed to switch" in m for _, m in events),
                  "no error from the session code")
            fill = dac_fill()
            check(devices() == before and abs(fill - fill_before) < 3.0,
                  "DAC not restarted across the whole session",
                  f"DAC holds {fill:.1f} ms (was {fill_before:.1f}), devices {devices()}")
            check(dsp.active() == base and not os.path.exists(state_file),
                  "after the session: previous configuration back, record cleared")
            size = os.path.getsize(speaker) if os.path.exists(speaker) else 0
            print(f"    (speaker output went to a file: {size} bytes; "
                  f"heard: {[m.split('User transcription: ', 1)[1] for _, m in events if 'User transcription: ' in m]})")
        finally:
            put_back(dsp, base, base_path)
            check(not os.path.exists(LIVE_STATE), "no real voice session started during the check (else: run it again)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--module-dir", default="~/.local/bin")
    ap.add_argument("--force", action="store_true", help="run `live`/`session` although audio is playing")
    ap.add_argument("cmd", choices=("units", "live", "session"))
    args = ap.parse_args()
    harness, va, scratch = load(args.module_dir)
    print(f"session code: {os.path.abspath(va.__file__)}")
    if args.cmd == "units":
        units(va, scratch)
    elif args.cmd == "live":
        live(va, scratch, args.force)
    else:
        asyncio.run(session(harness, va, scratch, args.force))
    print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
