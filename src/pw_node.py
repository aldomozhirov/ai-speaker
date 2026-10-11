"""Shared PipeWire node-readiness check.

`pw-cat`/`pw-loopback` do not error out when `--target`/`--capture`/`--playback`
fails to resolve a node by name -- they silently fall back to the default
source/sink instead. That failure mode has independently bitten every
integration point in this stack that targets a named node by string (the
welcome chime, the wake-word capture, the voice-agent transport's input and
output) -- each of which re-implemented its own "poll pw-dump until the node
name shows up" guard. This module is the one shared implementation; callers
should use this instead of rolling another copy.

Not used by xvf-aec-ref.sh: that one deliberately does *not* wait-then-trust,
because the AEC reference node appears in the graph before WirePlumber has
finished negotiating it, so "the node exists" doesn't mean "it's safe to
link to" there. It uses a supervise-and-reconcile loop instead (see
AI-SPEAKER-SETUP-NOTES.md). This helper is for the simpler case -- a node
that either already exists (device is up) or never will (unplugged) -- which
covers the other four call sites.
"""

from __future__ import annotations

import asyncio
import subprocess
import time


async def wait_for_node(name: str, timeout: float = 15.0, interval: float = 0.5) -> bool:
    """Poll `pw-dump` for a node named `name`. True once seen, False on timeout."""
    needle = f'"node.name": "{name}"'.encode()
    deadline = time.monotonic() + timeout
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pw-dump", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            out, _ = await proc.communicate()
        except OSError:
            out = b""
        if needle in out:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)


def wait_for_node_sync(name: str, timeout: float = 15.0, interval: float = 0.5) -> bool:
    """Blocking wrapper for non-asyncio callers (used via the CLI below)."""
    return asyncio.run(wait_for_node(name, timeout=timeout, interval=interval))


def _main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description=(
            "Block until a PipeWire node with this node.name appears in "
            "`pw-dump`, or exit 1 after --timeout seconds."
        )
    )
    parser.add_argument("name")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()
    ok = wait_for_node_sync(args.name, timeout=args.timeout, interval=args.interval)
    if not ok:
        print(f"pw_node: node {args.name!r} never appeared after {args.timeout}s", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(_main())
