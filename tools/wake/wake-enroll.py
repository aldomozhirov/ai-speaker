#!/usr/bin/env python3
"""Record "Hey Jarvis" at known positions, for training and testing a wake-word
model (docs/plans/wake-word-custom-model.md, stage B).

The live speaker only keeps audio of wake-ups it nearly heard; the ones it
missed completely leave nothing. Here every take is cued, so the recording
says exactly when someone said the phrase, where they stood and in what
conditions -- the only way to measure recall by distance.

    tools/wake/wake-enroll.py positions                     # show them
    tools/wake/wake-enroll.py positions --set near=0.5:"right next to it" ...
    tools/wake/wake-enroll.py record --person NAME --round 1
    tools/wake/wake-enroll.py status

Run it over SSH from a phone or laptop you carry around the room. For each
condition (quiet, music, talk) and each position it says where to stand,
waits for Enter, then lights the LED ring white for one second twelve times,
five seconds apart: say "Hey Jarvis" once each time the ring lights. Takes
1-8 in a normal voice, 9-10 quietly, 11-12 quickly. Then 20 s in which nobody
says it (background for training). About 20-25 minutes in all.

While it runs, voice-agent is stopped (no session opens, nothing chimes,
music is not ducked) and started again at the end, also after Ctrl-C.
wake-word keeps running, so the journal holds the live pipeline's verdict on
every take. Nothing is played through the speaker.

Output (household audio, stays on the device, never in git):

    ~/.local/share/ai-speaker/wake-enroll/positions.json
    ~/.local/share/ai-speaker/wake-enroll/<person>-r<round>/<condition>-<position>.wav
                                                         .../takes.json

A block that is already recorded is skipped, so an interrupted round can be
resumed with the same command; --redo CONDITION-POSITION records one again.
Standard library only; no venv needed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))
import wake_context  # noqa: E402
from pw_node import wait_for_node  # noqa: E402

ROOT = os.path.expanduser("~/.local/share/ai-speaker/wake-enroll")
POSITIONS_FILE = os.path.join(ROOT, "positions.json")
SOURCE = os.environ.get(
    "WAKE_WORD_SOURCE",
    "alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993700254500350-00.analog-stereo",
)
RUNTIME = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
LED_SOCKET = os.environ.get("ASSISTANT_LED_SOCKET", os.path.join(RUNTIME, "assistant-led.sock"))
RATE, CHANNELS = 16000, 2

TAKES = 12
TAKE_INTERVAL = 5.0
CUE_SECONDS = 1.0
LEAD_SECONDS = 3.0  # recording before the first cue
BED_SECONDS = 20.0
MUSIC_SETTLE_SECONDS = 20.0  # echo canceller converging after music starts

CONDITIONS = {
    "quiet": "Quiet room: no music, no TV, nobody else talking.",
    "music": "Music from the speaker at your usual volume (start it from the Spotify app).",
    "talk": "Talk in the room: a TV, podcast or video from ANOTHER device, at a normal volume.",
}


def style(take: int) -> str:
    return "normal" if take <= 8 else ("quiet" if take <= 10 else "fast")


STYLE_HINT = {"normal": "normal voice", "quiet": "QUIETLY", "fast": "QUICKLY"}


# --- positions ---------------------------------------------------------------


def load_positions() -> list[dict]:
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def cmd_positions(args) -> int:
    if args.set:
        positions = []
        for item in args.set:
            m = re.fullmatch(r"([a-z0-9_]+)=([\d.]+)(?::(.*))?", item)
            if not m:
                print(f"bad position {item!r}; use name=metres[:where]", file=sys.stderr)
                return 2
            positions.append({"name": m[1], "metres": float(m[2]), "where": m[3] or ""})
        os.makedirs(ROOT, mode=0o700, exist_ok=True)
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=1)
    positions = load_positions()
    if not positions:
        print("No positions yet. Set them with: positions --set near=0.5:\"next to it\" ...")
        return 1
    for p in positions:
        print(f"  {p['name']:10} {p['metres']:4.1f} m  {p['where']}")
    return 0


# --- the parts that touch the speaker ---------------------------------------


def led(state: str) -> None:
    with contextlib.suppress(OSError):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(LED_SOCKET)
            s.sendall((state + "\n").encode())


def session_active() -> bool:
    """True when the voice agent's journal shows a session that has not ended."""
    out = subprocess.run(
        ["journalctl", "--user", "-u", "voice-agent", "--since", "-40min", "-o", "cat", "--no-pager", "-q"],
        capture_output=True, text=True,
    ).stdout
    last = None
    for line in out.splitlines():
        if "Starting session" in line:
            last = "start"
        elif "Session ended" in line:
            last = "end"
    return last == "start"


def music_playing() -> bool:
    out = subprocess.run([wake_context.PW_DUMP], capture_output=True, text=True).stdout
    try:
        return wake_context.music_playing(json.loads(out), re.compile(wake_context.MUSIC_MATCH, re.I))
    except ValueError:
        return False


class Recorder:
    """One continuous 2-channel 16 kHz recording from the XVF3800 into a WAV."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.frames = 0
        self._wav = wave.open(path + ".tmp", "wb")
        self._wav.setnchannels(CHANNELS)
        self._wav.setsampwidth(2)
        self._wav.setframerate(RATE)
        self._proc = subprocess.Popen(
            ["/usr/bin/pw-cat", "--record", "--raw", "--target", SOURCE, "--rate", str(RATE),
             "--channels", str(CHANNELS), "--format", "s16", "--latency", "40ms", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.started = time.time()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        chunk = 1280 * CHANNELS * 2
        while True:
            data = self._proc.stdout.read(chunk)
            if not data:
                return
            with self._lock:
                self._wav.writeframes(data)
                self.frames += len(data) // (CHANNELS * 2)

    def seconds(self) -> float:
        with self._lock:
            return self.frames / RATE

    def wait_for_audio(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while self.seconds() == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.seconds() > 0

    def close(self, keep: bool) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._thread.join(timeout=2)
        with self._lock:
            self._wav.close()
        if keep:
            os.replace(self.path + ".tmp", self.path)
        else:
            with contextlib.suppress(OSError):
                os.unlink(self.path + ".tmp")


def sleep_until(rec: Recorder, at: float) -> None:
    """Wait until the recording is `at` seconds long (audio time, not wall time)."""
    while rec.seconds() < at:
        time.sleep(0.01)


def record_block(path: str, condition: str, position: dict, args) -> dict:
    rec = Recorder(path)
    keep = False
    try:
        if not rec.wait_for_audio():
            raise RuntimeError("no audio from the microphone (is the XVF3800 node there?)")
        takes = []
        t = LEAD_SECONDS
        for n in range(1, args.takes + 1):
            sleep_until(rec, t)
            cue = rec.seconds()
            hint = STYLE_HINT[style(n)]
            print(f"\r  take {n:2}/{args.takes}  say \"Hey Jarvis\" ({hint})        ", end="", flush=True)
            if not args.dry_run:
                led("listening")
            sleep_until(rec, t + CUE_SECONDS)
            if not args.dry_run:
                led("idle")
            takes.append({"take": n, "style": style(n), "cue_seconds": round(cue, 3),
                          "cue_time": round(rec.started + cue, 3)})
            t += args.interval
        print(f"\r  now {args.bed:.0f} s in which nobody says it...                    ", end="", flush=True)
        bed_start = t
        sleep_until(rec, t + args.bed)
        print("\r  done.                                                          ")
        keep = True
        return {
            "file": os.path.basename(path), "condition": condition, "position": position["name"],
            "metres": position["metres"], "where": position["where"],
            "start_time": round(rec.started, 3),
            "start_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(rec.started)),
            "seconds": round(rec.seconds(), 2), "takes": takes,
            "bed_seconds": [round(bed_start, 2), round(bed_start + args.bed, 2)],
        }
    finally:
        if not keep and not args.dry_run:
            led("idle")
        rec.close(keep)


def cmd_record(args) -> int:
    positions = load_positions()
    if not positions:
        print("Set the positions first: tools/wake/wake-enroll.py positions --set ...", file=sys.stderr)
        return 1
    person = re.sub(r"[^a-z0-9_]", "", args.person.lower())
    if not person:
        print("--person needs letters or digits", file=sys.stderr)
        return 2
    out = os.path.join(args.root or ROOT, f"{person}-r{args.round}")
    os.makedirs(out, mode=0o700, exist_ok=True)
    meta_path = os.path.join(out, "takes.json")
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {"person": person, "round": args.round, "blocks": {}}

    conditions = [c for c in CONDITIONS if not args.only or c in args.only]
    plan = [(c, p) for c in conditions for p in positions]
    todo = [(c, p) for c, p in plan
            if f"{c}-{p['name']}" in (args.redo or []) or f"{c}-{p['name']}" not in meta["blocks"]]
    if not todo:
        print(f"Everything for {person}, round {args.round} is recorded ({len(plan)} blocks).")
        return 0
    block_s = LEAD_SECONDS + args.takes * args.interval + args.bed
    print(f"{person}, round {args.round}: {len(todo)} of {len(plan)} blocks to record, "
          f"{block_s:.0f} s each, about {len(todo) * (block_s + 20) / 60:.0f} min with walking.")

    if not asyncio.run(wait_for_node(SOURCE, timeout=5)):
        print(f"Microphone node {SOURCE} is not there.", file=sys.stderr)
        return 1
    stopped = False
    if not args.dry_run:
        if session_active():
            print("A voice session is active; try again when it has ended.", file=sys.stderr)
            return 1
        subprocess.run(["systemctl", "--user", "stop", "voice-agent"], check=True)
        stopped = True
        print("voice-agent stopped for the recording (it starts again at the end).")

    def on_term(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, on_term)
    try:
        current = None
        for condition, position in todo:
            if condition != current:
                current = condition
                print(f"\n=== {condition.upper()}: {CONDITIONS[condition]}")
                if condition == "music" and not args.dry_run:
                    input("Start the music, then press Enter... ")
                    while not music_playing():
                        input("No music stream is playing yet. Start it, then press Enter... ")
                    print(f"Waiting {MUSIC_SETTLE_SECONDS:.0f} s for the echo canceller to settle...")
                    time.sleep(MUSIC_SETTLE_SECONDS)
                elif condition == "quiet" and not args.dry_run and music_playing():
                    input("Music is playing. Pause it, then press Enter... ")
            name = f"{condition}-{position['name']}"
            while True:
                answer = input(f"\nGo to {position['name']} ({position['metres']:g} m, {position['where']}) "
                               f"and press Enter (s = skip)... ").strip().lower()
                if answer == "s":
                    break
                if condition == "music" and not args.dry_run and not music_playing():
                    print("The music has stopped; start it again first.")
                    continue
                block = record_block(os.path.join(out, name + ".wav"), condition, position, args)
                again = input("Enter = next, r = record this position again... ").strip().lower()
                if again == "r":
                    continue
                meta["blocks"][name] = block
                with open(meta_path + ".tmp", "w") as f:
                    json.dump(meta, f, indent=1)
                os.replace(meta_path + ".tmp", meta_path)
                break
        print(f"\nAll done. {len(meta['blocks'])} blocks in {out}")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Run the same command again to carry on where it left off.")
        return 130
    finally:
        if not args.dry_run:
            led("idle")
        if stopped:
            subprocess.run(["systemctl", "--user", "start", "voice-agent"])
            print("voice-agent started again.")


def cmd_status(args) -> int:
    root = args.root or ROOT
    positions = load_positions()
    if not os.path.isdir(root):
        print("Nothing recorded yet.")
        return 0
    for d in sorted(os.listdir(root)):
        path = os.path.join(root, d, "takes.json")
        if not os.path.exists(path):
            continue
        with open(path) as f:
            blocks = json.load(f)["blocks"]
        total = len(CONDITIONS) * len(positions)
        takes = sum(len(b["takes"]) for b in blocks.values())
        print(f"  {d:16} {len(blocks):2}/{total} blocks, {takes} takes")
        missing = [f"{c}-{p['name']}" for c in CONDITIONS for p in positions if f"{c}-{p['name']}" not in blocks]
        if missing:
            print(f"  {'':16} missing: {', '.join(missing)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=None, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("positions", help="show or set the positions")
    sp.add_argument("--set", nargs="+", metavar="NAME=METRES[:WHERE]")
    sr = sub.add_parser("record", help="record one round for one person")
    sr.add_argument("--person", required=True)
    sr.add_argument("--round", type=int, required=True, choices=(1, 2),
                    help="1 = training, 2 = test only (record it on another day)")
    sr.add_argument("--only", nargs="+", choices=list(CONDITIONS), help="only these conditions")
    sr.add_argument("--redo", nargs="+", metavar="CONDITION-POSITION", help="record these blocks again")
    sr.add_argument("--takes", type=int, default=TAKES, help=argparse.SUPPRESS)
    sr.add_argument("--interval", type=float, default=TAKE_INTERVAL, help=argparse.SUPPRESS)
    sr.add_argument("--bed", type=float, default=BED_SECONDS, help=argparse.SUPPRESS)
    sr.add_argument("--dry-run", action="store_true",
                    help="record, but leave voice-agent running, the LEDs alone and music unchecked")
    sub.add_parser("status", help="what has been recorded")
    args = ap.parse_args()
    return {"positions": cmd_positions, "record": cmd_record, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
