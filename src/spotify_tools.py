"""Spotify tools for the voice agent's function calling (Phase 8.1 + 8.3).

Play/pause/next/previous route through spotifyd's MPRIS interface via
playerctl -- the same mechanism volume-buttons.py already uses for the
physical button. No Spotify Web API / OAuth needed for these: MPRIS talks
directly to the locally running spotifyd process.

spotify_play_track goes through the Web API (spotify_web.py) instead. Search
obviously needs a catalog lookup MPRIS can't do, but the playback leg does
too: spotifyd only claims the MPRIS bus name while a Connect client session
is live, so with nothing playing there is no local player to hand a URI to.
"""

from __future__ import annotations

import asyncio
import logging
import os

from pipecat.services.llm_service import FunctionCallParams

import session_signal
import spotify_web

LOG = logging.getLogger("voice-agent.spotify")

PLAYERCTL = os.environ.get("PLAYERCTL_PATH", "/usr/bin/playerctl")


async def _run_playerctl(command: str) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            PLAYERCTL,
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
    except FileNotFoundError:
        return False, "playerctl not found"
    except asyncio.TimeoutError:
        return False, "playerctl timed out"

    if proc.returncode != 0:
        message = (stderr or stdout).decode().strip()
        return False, message or "no active media player"
    return True, ""


async def _playerctl_action(params: FunctionCallParams, command: str, ok_status: str) -> None:
    ok, error = await _run_playerctl(command)
    if ok:
        # A transport command is a one-shot action, not the start of a
        # back-and-forth -- close the session after the assistant
        # acknowledges it instead of opening the mic for another turn.
        await session_signal.complete_quick_action(params, {"status": ok_status})
    else:
        LOG.warning("playerctl %s failed: %s", command, error)
        await params.result_callback({"status": "error", "error": error})


async def spotify_play(params: FunctionCallParams) -> None:
    """Resume or start Spotify playback on this speaker."""
    # Checked now, started after the confirmation has been spoken (see
    # session_signal.complete_quick_action): "status" fails the same way
    # "play" would if spotifyd has no session to resume.
    ok, error = await _run_playerctl("status")
    if not ok:
        LOG.warning("playerctl status failed: %s", error)
        await params.result_callback({"status": "error", "error": error})
        return

    async def start() -> None:
        ok, error = await _run_playerctl("play")
        if not ok:
            raise RuntimeError(f"playerctl play failed: {error}")

    await session_signal.complete_quick_action(params, {"status": "playing"}, then=start)


async def spotify_pause(params: FunctionCallParams) -> None:
    """Pause Spotify playback on this speaker."""
    await _playerctl_action(params, "pause", "paused")


async def spotify_next(params: FunctionCallParams) -> None:
    """Skip to the next track."""
    await _playerctl_action(params, "next", "skipped to next track")


async def spotify_previous(params: FunctionCallParams) -> None:
    """Go back to the previous track."""
    await _playerctl_action(params, "previous", "went back to previous track")


async def spotify_play_track(
    params: FunctionCallParams, query: str, kind: str = "track"
) -> None:
    """Search Spotify and play the best match on this speaker.

    Args:
        query: What to play, in the user's own words -- for example
            "bohemian rhapsody" or "the new Radiohead album".
        kind: What kind of thing to look for: "track", "album", "artist"
            or "playlist". Use "track" unless the user clearly asked for
            one of the others.
    """
    try:
        found, start = await spotify_web.prepare_query(query, kind)
    except spotify_web.SpotifyError as exc:
        LOG.warning("spotify_play_track(%r, %r) failed: %s", query, kind, exc.detail)
        await params.result_callback({"status": "error", "error": exc.message})
        return
    except Exception:  # noqa: BLE001 -- a tool must never take the session down
        LOG.exception("spotify_play_track(%r, %r) crashed", query, kind)
        await params.result_callback(
            {"status": "error", "error": "Something went wrong talking to Spotify."}
        )
        return

    # Same one-shot treatment as the transport commands: don't reopen the mic
    # over the music. Playback itself starts once the assistant has said what
    # it is about to play -- it used to start here, so the music came first
    # and the confirmation was spoken over it.
    await session_signal.complete_quick_action(
        params, {"status": "playing", **found}, then=start
    )
