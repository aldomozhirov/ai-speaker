#!/usr/bin/env python3
"""Thin CLI over the XVF3800's USB control registers.

xvf_host.py is a vendored library with no CLI of its own and very chatty
stdout (it prints every ReadCMD/WriteCMD), so callers end up re-implementing
"open device, touch one register, close" plus output filtering every time.
This wraps that once, quietly, for the two callers that need it:
xvf-aec-ref.sh (reapplying AUDIO_MGR_FAR_END_DSP_ENABLE on every start) and
ai-speaker-calibrate.py (reading the AEC's health registers).

Must run under ~/.venvs/xvf3800/bin/python -- xvf_host needs pyusb.

Usage:
    xvf_register.py get   NAME              print values, space-separated
    xvf_register.py set   NAME VAL [VAL..]  write values
    xvf_register.py ensure NAME VAL         write only if it doesn't match;
                                            prints "ok" / "fixed", exit 0
"""

from __future__ import annotations

import contextlib
import io
import os
import sys

sys.path.insert(0, "/home/aldom/src/reSpeaker_XVF3800_USB_4MIC_ARRAY/python_control")


@contextlib.contextmanager
def _quiet():
    """xvf_host prints protocol chatter to stdout unconditionally."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield


def _coerce(text: str) -> int | float:
    try:
        return int(text)
    except ValueError:
        return float(text)


def _same(actual, wanted) -> bool:
    if len(actual) != len(wanted):
        return False
    return all(abs(float(a) - float(w)) < 1e-6 for a, w in zip(actual, wanted))


def _check_mode(parameters: dict, action: str, name: str) -> None:
    """Refuse a transfer the register's access mode doesn't allow, before the
    device is opened.

    xvf_host.read() does not look at the mode column, and the chip does not
    shrug off a read of a write-only register: on 2026-10-04 a single
    `get PP_NL_MODEL_CMD_ABORT` left the control servicers for resids 17/33/35
    answering RETRY forever and the USB capture stream delivering digital
    silence, with every service still "active". Only a chip REBOOT cleared it.
    """
    entry = parameters.get(name)
    if entry is None:
        raise KeyError(f"no such register: {name}")
    mode = entry[3]
    if mode == "wo" and action in ("get", "ensure"):
        raise PermissionError(
            f"{name} is write-only; reading it wedges the XVF3800's DSP until the chip is rebooted"
        )
    if mode == "ro" and action in ("set", "ensure"):
        raise PermissionError(f"{name} is read-only")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    action, name, args = argv[1], argv[2], [_coerce(a) for a in argv[3:]]

    with _quiet():
        import xvf_host

    _check_mode(xvf_host.PARAMETERS, action, name)

    with _quiet():
        device = xvf_host.find()

    try:
        with _quiet():
            if action == "get":
                values = device.read(name)
                if values is None:
                    raise KeyError(f"no such register: {name}")
            elif action == "set":
                device.write(name, args)
                values = args
            elif action == "ensure":
                current = device.read(name)
                if _same(current, args):
                    outcome = "ok"
                else:
                    device.write(name, args)
                    outcome = "fixed"
                values = args
            else:
                print(f"unknown action: {action!r}", file=sys.stderr)
                return 2
    finally:
        with _quiet(), contextlib.suppress(Exception):
            device.close()

    if action == "ensure":
        print(outcome)
    else:
        print(" ".join(str(v) for v in values))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:  # device absent, register name unknown, USB error
        print(f"{os.path.basename(sys.argv[0])}: {exc}", file=sys.stderr)
        sys.exit(1)
