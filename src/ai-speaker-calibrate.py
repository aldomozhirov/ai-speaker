#!/home/aldom/.venvs/wake-word/bin/python3
"""Ad-hoc calibration + health check for the AI speaker's mic path.

Run this after anything physical or environmental changes: the ReSpeaker was
replugged or moved, the speaker was repositioned, the room changed, or wake
word detection just started feeling worse. It reproduces, as a repeatable
script, the manual measurements used to tune this system originally.

    ai-speaker-calibrate.py check          # fast, non-interactive health check
    ai-speaker-calibrate.py wake           # interactive wake-word calibration
    ai-speaker-calibrate.py wake --music   # ...with music playing (the hard case)
    ai-speaker-calibrate.py all            # check, then wake in silence + music
    ai-speaker-calibrate.py wake --apply   # ...and write the result to the unit

What `wake` measures, per mic channel, on both of the XVF3800's USB capture
channels at once:

  * the background noise floor -- the highest openWakeWord score reached while
    nobody is speaking. Anything at or below this will false-trigger.
  * the peak score of several deliberate "Hey Jarvis" utterances.

A good threshold sits in the gap between those two. Measuring both channels in
one pass matters because the XVF3800's L/R USB capture channels are configured
independently on this unit (they are NOT a stereo pair), so which one is better
is a per-unit, per-environment question rather than a fixed answer.

Runs under the wake-word venv (openwakeword, numpy); shells out to the xvf3800
venv for register reads, since pyusb only lives there.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass, field

import numpy as np

# onnxruntime warns about the absent CUDA provider on every model load; there
# is no GPU on this box and never will be.
warnings.filterwarnings("ignore", message=".*CUDAExecutionProvider.*")

os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

HOME = os.path.expanduser("~")
XVF_PYTHON = f"{HOME}/.venvs/xvf3800/bin/python"
XVF_REGISTER = f"{HOME}/.local/bin/xvf_register.py"
WAKE_UNIT = f"{HOME}/.config/systemd/user/wake-word.service"

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80 ms, openWakeWord's native chunk size
CAPTURE_CHANNELS = 2
FRAME_BYTES = FRAME_SAMPLES * CAPTURE_CHANNELS * 2  # s16 interleaved
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

XVF_SOURCE = (
    "alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array"
    "_114993700254500350-00.analog-stereo"
)
XVF_SINK = (
    "alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array"
    "_114993700254500350-00.analog-stereo"
)
ALOOP_SINK = "alsa_output.platform-snd_aloop.0.analog-stereo"

# Scores below this are treated as "nothing was said" rather than a weak
# detection -- openWakeWord idles in the low thousandths, so this is well
# clear of the floor while still catching a badly-degraded utterance.
HEARD_FLOOR = 0.05

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    ("\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m")
    if sys.stdout.isatty()
    else ("", "", "", "", "", "")
)


def say(message: str = "") -> None:
    print(message, flush=True)


def ok(message: str) -> None:
    say(f"  {GREEN}OK{RESET}    {message}")


def warn(message: str) -> None:
    say(f"  {YELLOW}WARN{RESET}  {message}")


def bad(message: str) -> None:
    say(f"  {RED}FAIL{RESET}  {message}")


def heading(message: str) -> None:
    say(f"\n{BOLD}{message}{RESET}")


def run(argv: list[str], timeout: float = 10.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def register(name: str) -> str | None:
    code, out = run([XVF_PYTHON, XVF_REGISTER, "get", name])
    return out if code == 0 else None


def unit_active(unit: str) -> bool:
    code, _ = run(["systemctl", "--user", "is-active", "--quiet", unit])
    return code == 0


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def check_hardware() -> int:
    """Verify the parts of the mic path that are silently breakable.

    Returns the number of problems found.
    """
    problems = 0

    heading("Hardware and registers")
    code, _ = run(["lsusb", "-d", "2886:0018"])
    if code != 0:
        code, out = run(["lsusb"])
        if "XVF3800" in out or "reSpeaker" in out:
            ok("ReSpeaker XVF3800 present on USB")
        else:
            bad("ReSpeaker XVF3800 not found on USB -- check the cable")
            return problems + 1
    else:
        ok("ReSpeaker XVF3800 present on USB")

    far_end = register("AUDIO_MGR_FAR_END_DSP_ENABLE")
    if far_end is None:
        bad("cannot read XVF3800 registers (is another process holding the device?)")
        problems += 1
    elif far_end.strip() == "1":
        ok("AUDIO_MGR_FAR_END_DSP_ENABLE = 1 (echo cancellation running)")
    else:
        # This register lives in chip RAM and resets to 0 on every power cycle;
        # xvf-aec-ref.sh reapplies it on start, so finding it off means that
        # service failed or hasn't run since the last replug.
        bad(
            f"AUDIO_MGR_FAR_END_DSP_ENABLE = {far_end} -- echo cancellation is OFF. "
            "Fix: systemctl --user restart xvf-aec-ref.service"
        )
        problems += 1

    converged = register("AEC_AECCONVERGED")
    if converged is None:
        warn("could not read AEC_AECCONVERGED")
    elif converged.strip() == "1":
        ok("AEC_AECCONVERGED = 1 (echo model has converged)")
    else:
        warn(
            "AEC_AECCONVERGED = 0 -- the echo canceller has not converged. This is "
            "normal if nothing has played through the speaker recently; play music "
            "for ~30s and re-check before treating it as a fault."
        )

    for name, label in (
        ("AUDIO_MGR_MIC_GAIN", "mic gain"),
        ("AUDIO_MGR_REF_GAIN", "AEC reference gain"),
    ):
        value = register(name)
        say(f"  {DIM}info{RESET}  {label} ({name}) = {value if value else '?'}")

    heading("Audio routing")
    code, links = run(["pw-link", "-l"], timeout=15)
    if code != 0:
        bad("pw-link failed -- is PipeWire running?")
        return problems + 1

    if f"output.xvf-aec-ref:output_FL\n  |-> {XVF_SINK}:playback_FL" in links:
        ok("AEC reference feed is wired to the XVF3800's playback input")
    else:
        bad(
            "AEC reference feed is NOT wired to the XVF3800 -- the echo canceller "
            "has no reference signal. Fix: systemctl --user restart xvf-aec-ref.service"
        )
        problems += 1

    if f"output.xvf-aec-ref:output_FL\n  |-> {ALOOP_SINK}:playback_FL" in links:
        bad(
            "AEC reference feed fell back to the aloop sink (the known WirePlumber "
            "race) -- it is feeding audio back into its own source"
        )
        problems += 1

    if f"{XVF_SOURCE}:capture_FL" in links:
        ok("XVF3800 processed capture source is available")
    else:
        bad(f"capture source not found in the graph: {XVF_SOURCE}")
        problems += 1

    heading("Services")
    for unit in (
        "xvf-aec-ref.service",
        "wake-word.service",
        "voice-agent.service",
        "volume-sync.service",
        "volume-buttons.service",
        "volume-led.service",
    ):
        if unit_active(unit):
            ok(f"{unit} active")
        else:
            bad(f"{unit} is not running")
            problems += 1

    code, out = run(["loginctl", "show-user", os.environ.get("USER", "aldom"), "-p", "Linger"])
    if "Linger=yes" in out:
        ok("systemd lingering enabled (services start at boot without a login)")
    else:
        bad(
            "systemd lingering is OFF -- services will not start until someone logs in. "
            f"Fix: sudo loginctl enable-linger {os.environ.get('USER', 'aldom')}"
        )
        problems += 1

    return problems


# ---------------------------------------------------------------------------
# Wake-word measurement
# ---------------------------------------------------------------------------


@dataclass
class ChannelStats:
    index: int
    background_peak: float = 0.0
    utterance_peaks: list[float] = field(default_factory=list)

    @property
    def heard(self) -> list[float]:
        return [p for p in self.utterance_peaks if p >= HEARD_FLOOR]

    @property
    def misses(self) -> int:
        return len(self.utterance_peaks) - len(self.heard)

    @property
    def weakest(self) -> float:
        return min(self.heard) if self.heard else 0.0

    def recommended_threshold(self) -> float | None:
        """A point between the noise floor and the weakest real utterance,
        weighted toward the utterance rather than a plain midpoint: a short
        silence sample understates the noise tail (a door, a cough, a phrase
        from a podcast), so sitting close to the measured floor buys
        sensitivity that a longer sample would have shown to be false
        triggers. Clamped below at 0.20 -- openWakeWord fires on ordinary
        speech-like audio underneath that regardless of what was measured.

        None if nothing was heard at all.
        """
        if not self.heard:
            return None
        floor = max(self.background_peak, 0.02)
        value = floor**0.35 * self.weakest**0.65
        return round(min(max(value, 0.20), 0.60), 2)

    @property
    def separation(self) -> float:
        """How many times louder the weakest utterance scored than the noise
        floor. Below ~2x there is no threshold that is both reliable and
        false-trigger-free."""
        if not self.heard:
            return 0.0
        return self.weakest / max(self.background_peak, 0.001)


class Listener:
    """One continuous pw-cat capture, scored by one openWakeWord model per
    channel. Kept open across all phases so the models never lose their
    streaming context between measurements."""

    def __init__(self, channels: list[int]) -> None:
        import openwakeword
        from openwakeword.model import Model

        model_name_env = os.environ.get("WAKE_WORD_MODEL", "hey_jarvis")
        paths = [
            p
            for p in openwakeword.get_pretrained_model_paths()
            if os.path.basename(p).startswith(model_name_env)
        ]
        if not paths:
            raise SystemExit(f"no openWakeWord model matching {model_name_env!r}")

        self.model_key = os.path.basename(paths[0])[:-5]  # strip ".onnx"
        self.channels = channels
        self.models = {c: Model(wakeword_model_paths=[paths[0]]) for c in channels}
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> Listener:
        self.proc = subprocess.Popen(
            [
                "pw-cat", "--record", "--target", XVF_SOURCE,
                "--rate", str(SAMPLE_RATE),
                "--channels", str(CAPTURE_CHANNELS),
                "--format", "s16",
                "--latency", "40ms",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=FRAME_BYTES * 4,
        )
        return self

    def __exit__(self, *exc_info) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def _read_frame(self) -> np.ndarray | None:
        assert self.proc is not None and self.proc.stdout is not None
        buf = b""
        while len(buf) < FRAME_BYTES:
            chunk = self.proc.stdout.read(FRAME_BYTES - len(buf))
            if not chunk:
                return None
            buf += chunk
        return np.frombuffer(buf, dtype=np.int16).reshape(-1, CAPTURE_CHANNELS)

    def measure(self, seconds: float, progress: bool = False) -> dict[int, float]:
        """Score every frame for `seconds` and return the peak per channel."""
        peaks = {c: 0.0 for c in self.channels}
        frames = max(1, int(seconds / FRAME_SECONDS))
        for i in range(frames):
            stereo = self._read_frame()
            if stereo is None:
                raise SystemExit("capture stream ended unexpectedly")
            for channel in self.channels:
                model = self.models[channel]
                model.predict(np.ascontiguousarray(stereo[:, channel]))
                score = float(model.prediction_buffer[self.model_key][-1])
                peaks[channel] = max(peaks[channel], score)
            if progress and i % 6 == 0:
                remaining = (frames - i) * FRAME_SECONDS
                print(f"\r    listening... {remaining:4.1f}s ", end="", flush=True)
        if progress:
            print("\r" + " " * 32 + "\r", end="", flush=True)
        return peaks


def music_playing() -> bool:
    code, out = run(["playerctl", "status"])
    return code == 0 and out.strip() == "Playing"


def measure_phase(
    listener: Listener,
    stats: dict[int, ChannelStats],
    attempts: int,
    label: str,
    quiet_seconds: float,
) -> None:
    """One full noise-floor + utterance measurement round."""
    heading(f"Phase: {label}")

    say(f"  Measuring the background noise floor. Please stay quiet for {quiet_seconds:.0f} seconds.")
    for count in (3, 2, 1):
        print(f"\r    starting in {count}... ", end="", flush=True)
        time.sleep(1)
    print("\r" + " " * 32 + "\r", end="", flush=True)

    peaks = listener.measure(quiet_seconds, progress=True)
    for channel, peak in peaks.items():
        stats[channel].background_peak = max(stats[channel].background_peak, peak)
    say("    noise floor: " + "  ".join(f"ch{c}={p:.3f}" for c, p in sorted(peaks.items())))

    times = "once" if attempts == 1 else f"{attempts} times"
    say(f"\n  Now say {BOLD}\"Hey Jarvis\"{RESET} {times}, from where you normally")
    say("  stand or sit -- not up close. Wait for each prompt.")
    for attempt in range(1, attempts + 1):
        for count in (2, 1):
            print(f"\r    attempt {attempt}/{attempts} in {count}... ", end="", flush=True)
            time.sleep(1)
        print(f"\r    attempt {attempt}/{attempts}: {BOLD}say it now{RESET}      ", end="", flush=True)
        peaks = listener.measure(3.0)
        print("\r" + " " * 48 + "\r", end="", flush=True)
        for channel, peak in peaks.items():
            stats[channel].utterance_peaks.append(peak)
        detail = "  ".join(f"ch{c}={p:.3f}" for c, p in sorted(peaks.items()))
        best = max(peaks.values())
        mark = f"{GREEN}heard{RESET}" if best >= HEARD_FLOOR else f"{RED}nothing{RESET}"
        say(f"    attempt {attempt}: {detail}   [{mark}]")


def report(stats: dict[int, ChannelStats], phase_label: str) -> ChannelStats | None:
    heading(f"Results ({phase_label})")

    best: ChannelStats | None = None
    for channel in sorted(stats):
        s = stats[channel]
        say(f"  {BOLD}channel {channel}{RESET}")
        say(f"    noise floor       {s.background_peak:.3f}")
        if s.heard:
            peaks = ", ".join(f"{p:.3f}" for p in s.heard)
            say(f"    utterance peaks   {peaks}")
            say(f"    weakest utterance {s.weakest:.3f}")
            say(f"    separation        {s.separation:.1f}x above the noise floor")
        if s.misses:
            say(f"    {YELLOW}not detected at all: {s.misses} of {len(s.utterance_peaks)} attempts{RESET}")
        recommended = s.recommended_threshold()
        say(f"    suggested threshold  {recommended if recommended else 'n/a'}")
        if best is None or s.separation > best.separation:
            best = s

    if best is None or not best.heard:
        say(f"\n  {RED}No utterance was detected on any channel.{RESET}")
        say("  That is a signal-path problem, not a threshold problem. Run")
        say("  `ai-speaker-calibrate.py check` and confirm the mic is not obstructed.")
        return None

    say(f"\n  {BOLD}Best channel: {best.index}{RESET} (separation {best.separation:.1f}x)")
    recommended = best.recommended_threshold()
    if recommended is not None and recommended >= best.weakest:
        say(
            f"  {YELLOW}Your utterances are scoring too low to tune around.{RESET} The weakest "
            f"was\n  {best.weakest:.3f}, under the {recommended} floor below which openWakeWord "
            "false-fires on\n  ordinary speech. Get closer, speak more distinctly, or reduce "
            "what is\n  competing with your voice -- lowering the threshold further would trade\n"
            "  one problem for a worse one."
        )
    elif best.separation < 2.0:
        say(
            f"  {YELLOW}Separation is poor.{RESET} There is no threshold that both catches "
            "your\n  voice reliably and avoids false triggers. Move the speaker away from\n"
            "  noise sources, or accept using the button in this condition."
        )
    elif best.misses:
        say(
            f"  {YELLOW}Some attempts were missed entirely{RESET} -- lowering the threshold "
            "will not\n  help those; the wake word simply was not recognised in the signal."
        )
    return best


# ---------------------------------------------------------------------------
# Applying results
# ---------------------------------------------------------------------------


def apply_settings(threshold: float, channel: int) -> None:
    with open(WAKE_UNIT) as f:
        original = f.read()

    updated = original
    for key, value in (("WAKE_WORD_THRESHOLD", threshold), ("WAKE_WORD_CHANNEL", channel)):
        pattern = rf"^Environment={key}=.*$"
        replacement = f"Environment={key}={value}"
        if re.search(pattern, updated, flags=re.MULTILINE):
            updated = re.sub(pattern, replacement, updated, flags=re.MULTILINE)
        else:
            raise SystemExit(f"{WAKE_UNIT} has no Environment={key}= line to update")

    if updated == original:
        say("\n  Unit file already matches the recommendation -- nothing to change.")
        return

    backup = WAKE_UNIT + ".bak"
    shutil.copy2(WAKE_UNIT, backup)
    with open(WAKE_UNIT, "w") as f:
        f.write(updated)

    heading("Applying")
    say(f"  WAKE_WORD_THRESHOLD = {threshold}")
    say(f"  WAKE_WORD_CHANNEL   = {channel}")
    say(f"  previous unit saved to {backup}")

    for argv in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "restart", "wake-word.service"],
    ):
        code, out = run(argv, timeout=30)
        if code != 0:
            raise SystemExit(f"{' '.join(argv)} failed: {out}")
    say(f"  {GREEN}wake-word.service restarted with the new settings{RESET}")


# ---------------------------------------------------------------------------


def calibrate_wake(args: argparse.Namespace) -> int:
    channels = [int(c) for c in args.channels.split(",")]

    # wake-word.service holds its own capture and would start real voice
    # sessions off our test utterances, ducking music and talking back
    # mid-measurement. Stop it for the duration and always put it back.
    was_running = unit_active("wake-word.service")
    if was_running:
        say(f"{DIM}Pausing wake-word.service for the duration of the test.{RESET}")
        run(["systemctl", "--user", "stop", "wake-word.service"], timeout=30)

    try:
        with Listener(channels) as listener:
            silence_stats = {c: ChannelStats(c) for c in channels}
            measure_phase(
                listener, silence_stats, args.attempts, "quiet room", args.quiet_seconds
            )
            best = report(silence_stats, "quiet room")

            music_stats = None
            if args.music:
                heading("Phase: with music playing")
                if not music_playing():
                    say("  Start music at your normal listening volume, then press Enter.")
                    input("  > ")
                else:
                    say("  Music is already playing -- leave it at a normal volume.")
                music_stats = {c: ChannelStats(c) for c in channels}
                measure_phase(
                    listener,
                    music_stats,
                    args.attempts,
                    "with music playing",
                    args.quiet_seconds,
                )
                music_best = report(music_stats, "with music playing")
                compare(silence_stats, music_stats)
                # Tune for the harder condition when it still has usable
                # separation; otherwise the quiet-room number is the honest one.
                if music_best and music_best.separation >= 2.0:
                    best = music_best
    finally:
        if was_running:
            run(["systemctl", "--user", "start", "wake-word.service"], timeout=30)
            say(f"\n{DIM}wake-word.service restarted.{RESET}")

    if best is None:
        return 1

    threshold = best.recommended_threshold()
    heading("Recommendation")
    say(f"  WAKE_WORD_THRESHOLD = {threshold}")
    say(f"  WAKE_WORD_CHANNEL   = {best.index}")

    if args.apply:
        apply_settings(threshold, best.index)
    else:
        say(f"\n  {DIM}Re-run with --apply to write this into {WAKE_UNIT}.{RESET}")
    return 0


def compare(silence: dict[int, ChannelStats], music: dict[int, ChannelStats]) -> None:
    """Quantify how much music playback costs, which is the failure mode this
    system is most prone to -- the AEC has to remove the speaker's own output
    from the mic signal before openWakeWord ever sees it."""
    heading("Effect of music on detection")
    for channel in sorted(silence):
        quiet_peak = silence[channel].weakest
        music_peak = music[channel].weakest
        if not quiet_peak:
            continue
        if not music_peak:
            say(f"  ch{channel}: {RED}undetectable{RESET} with music (was {quiet_peak:.3f} in silence)")
            continue
        ratio = music_peak / quiet_peak
        say(
            f"  ch{channel}: weakest utterance {quiet_peak:.3f} -> {music_peak:.3f} "
            f"({ratio * 100:.0f}% of the quiet-room score)"
        )

    converged = register("AEC_AECCONVERGED")
    if converged is not None and converged.strip() != "1":
        say(
            f"\n  {YELLOW}AEC_AECCONVERGED = 0 while music was playing.{RESET} The echo "
            "canceller\n  is not removing the speaker's own output, which is the usual cause "
            "of\n  music-only wake-word failure. Try: systemctl --user restart xvf-aec-ref.service"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="fast, non-interactive hardware/routing health check")

    for name, help_text in (
        ("wake", "interactive wake-word sensitivity calibration"),
        ("all", "health check, then wake-word calibration in silence and with music"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--attempts", type=int, default=5, help="utterances per phase (default 5)")
        p.add_argument("--channels", default="0,1", help="mic channels to measure (default 0,1)")
        p.add_argument(
            "--quiet-seconds",
            type=float,
            default=15.0,
            help="noise-floor sample length (default 15; raise it in a room with "
            "intermittent noise, where a short sample understates the floor)",
        )
        p.add_argument("--apply", action="store_true", help="write the result to wake-word.service")
        if name == "wake":
            p.add_argument("--music", action="store_true", help="also measure with music playing")

    args = parser.parse_args()

    if args.command == "check":
        problems = check_hardware()
        heading("Summary")
        if problems:
            say(f"  {RED}{problems} problem(s) found.{RESET} See the FAIL lines above.")
            return 1
        say(f"  {GREEN}Everything checks out.{RESET}")
        return 0

    if args.command == "all":
        args.music = True
        problems = check_hardware()
        if problems:
            heading("Summary")
            say(
                f"  {RED}{problems} problem(s) found.{RESET} Fix those before calibrating -- "
                "a broken\n  signal path will produce meaningless threshold numbers."
            )
            return 1
        say(f"\n  {GREEN}Health check passed.{RESET} Continuing to wake-word calibration.")

    return calibrate_wake(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nInterrupted.")
        sys.exit(130)
