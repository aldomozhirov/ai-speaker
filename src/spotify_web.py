#!/home/aldom/.venvs/voice-agent/bin/python3
"""Spotify Web API client for the voice agent (Phase 8.3).

Search needs a catalog lookup MPRIS can't do, and -- less obviously -- so
does *playing* anything from cold: spotifyd only claims the MPRIS bus name
while a Spotify Connect client session is live (`dbus_mpris.rs` requests it
on PlayerEvent::SessionConnected and releases it on SessionDisconnected), so
with nothing playing there is no local player to talk to at all. What there
*is* is a logged-in Connect device -- spotifyd restores its cached login at
startup without waiting for a phone -- which the Web API can target by id.
Hence: search here, play here, and let MPRIS handle transport once music is
actually running.

Runs standalone as a CLI for testing without the voice pipeline:

    ~/.local/bin/spotify_web.py auth              # one-time, needs a browser
    ~/.local/bin/spotify_web.py devices
    ~/.local/bin/spotify_web.py search "bohemian rhapsody"
    ~/.local/bin/spotify_web.py play "bohemian rhapsody" [track|album|artist|playlist]

Credentials live in ~/.config/ai-speaker/spotify.json (0600) rather than an
EnvironmentFile because the refresh token can rotate at runtime and systemd
only reads env files at unit start.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

LOG = logging.getLogger("voice-agent.spotify.web")

CONFIG_PATH = os.path.expanduser(
    os.environ.get("SPOTIFY_CONFIG", "~/.config/ai-speaker/spotify.json")
)
TOKEN_URL = "https://accounts.spotify.com/api/token"
AUTH_URL = "https://accounts.spotify.com/authorize"
API = "https://api.spotify.com/v1"

# Must match spotifyd.conf's device_name -- how this speaker is identified in
# the account's Connect device list. Never a hardcoded device id: librespot
# regenerates one per session, so a cached id goes stale on every restart.
DEVICE_NAME = os.environ.get("SPOTIFY_DEVICE_NAME", "AI Speaker")
# Redirect URI registered on the developer app. Loopback HTTP is allowed;
# "localhost" is explicitly not, it has to be the literal 127.0.0.1.
REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
SCOPES = "user-modify-playback-state user-read-playback-state"
# Voice latency budget: a cold "play X" is two round trips (search and device
# lookup run concurrently), so this is the worst case per leg.
TIMEOUT = float(os.environ.get("SPOTIFY_HTTP_TIMEOUT", "4.0"))
# Omitted by default: with a user token the account's own country takes
# priority over this parameter anyway. Set it to force a specific market.
MARKET = os.environ.get("SPOTIFY_MARKET", "")

KINDS = ("track", "album", "artist", "playlist")


class SpotifyError(Exception):
    """An error with a sentence the assistant can read aloud as-is."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(detail or message)
        self.message = message
        self.detail = detail


# --- credentials -------------------------------------------------------------


def _load_config() -> dict[str, str]:
    try:
        with open(CONFIG_PATH) as handle:
            config = json.load(handle)
    except FileNotFoundError:
        raise SpotifyError(
            "Spotify isn't linked to this speaker yet.",
            f"{CONFIG_PATH} does not exist",
        ) from None
    except (OSError, ValueError) as exc:
        raise SpotifyError(
            "Spotify isn't linked to this speaker yet.", f"{CONFIG_PATH}: {exc}"
        ) from None

    if not config.get("client_id") or not config.get("client_secret"):
        raise SpotifyError(
            "Spotify isn't linked to this speaker yet.",
            f"{CONFIG_PATH} is missing client_id/client_secret",
        )
    return config


def _save_refresh_token(refresh_token: str) -> None:
    """Persist a rotated refresh token, keeping the rest of the file intact.

    Spotify hands back a new refresh token on some refreshes and retires the
    old one. Dropping it doesn't fail now -- it fails days later as a
    mysterious invalid_grant, so write it through immediately.
    """
    try:
        config = _load_config()
    except SpotifyError:
        config = {}
    config["refresh_token"] = refresh_token
    tmp = f"{CONFIG_PATH}.tmp"
    old_umask = os.umask(0o077)
    try:
        with open(tmp, "w") as handle:
            json.dump(config, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)
    finally:
        os.umask(old_umask)


# --- HTTP --------------------------------------------------------------------

_session: Any = None
_session_loop: Any = None
_access_token: str = ""
_access_expiry: float = 0.0
_device_id: str = ""


async def _http() -> Any:
    """Lazily create the shared aiohttp session, bound to the running loop."""
    global _session, _session_loop
    import aiohttp

    loop = asyncio.get_running_loop()
    if _session is None or _session.closed or _session_loop is not loop:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        )
        _session_loop = loop
    return _session


async def close() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def _refresh_access_token() -> str:
    global _access_token, _access_expiry

    config = _load_config()
    refresh_token = config.get("refresh_token")
    if not refresh_token:
        raise SpotifyError(
            "Spotify isn't linked to this speaker yet.",
            "no refresh_token -- run `spotify_web.py auth`",
        )

    import base64

    import aiohttp

    session = await _http()
    basic = base64.b64encode(
        f"{config['client_id']}:{config['client_secret']}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {basic}"}
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    try:
        async with session.post(TOKEN_URL, data=data, headers=headers) as response:
            body = await response.json(content_type=None)
            if response.status != 200:
                error = (body or {}).get("error", response.status)
                if error == "invalid_grant":
                    raise SpotifyError(
                        "The Spotify link has expired and needs re-authorising.",
                        f"token refresh: {body}",
                    )
                raise SpotifyError(
                    "I couldn't reach Spotify just now.", f"token refresh: {body}"
                )
    except asyncio.TimeoutError:
        raise SpotifyError(
            "I couldn't reach Spotify just now.", "token refresh timed out"
        ) from None
    except aiohttp.ClientError as exc:
        raise SpotifyError("I couldn't reach Spotify just now.", str(exc)) from None

    _access_token = body["access_token"]
    _access_expiry = time.monotonic() + float(body.get("expires_in", 3600))
    if body.get("refresh_token") and body["refresh_token"] != refresh_token:
        _save_refresh_token(body["refresh_token"])
    return _access_token


async def _token(force: bool = False) -> str:
    # 60s of slack so a token that is about to expire isn't used for a call
    # that's still in flight when it does.
    if force or not _access_token or time.monotonic() > _access_expiry - 60:
        return await _refresh_access_token()
    return _access_token


async def _api(method: str, path: str, **kwargs: Any) -> Any:
    """One Web API call, with a token retry on 401 and one retry on 429."""
    import aiohttp

    session = await _http()
    for attempt in (0, 1):
        token = await _token(force=attempt == 1)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.request(
                method, f"{API}{path}", headers=headers, **kwargs
            ) as response:
                if response.status == 401 and attempt == 0:
                    continue  # token rejected -- refresh and try once more
                if response.status == 429:
                    delay = float(response.headers.get("Retry-After", "0") or 0)
                    if attempt == 0 and delay <= 2:
                        await asyncio.sleep(delay)
                        continue
                    raise SpotifyError(
                        "Spotify is rate-limiting me, try again in a moment.",
                        f"429 retry-after={delay}",
                    )
                if response.status == 403:
                    raise SpotifyError(
                        "Spotify says that needs a Premium account.",
                        f"403 {await response.text()}",
                    )
                if response.status == 404:
                    raise SpotifyError(
                        "This speaker isn't showing up in Spotify right now.",
                        f"404 {await response.text()}",
                    )
                if response.status >= 400:
                    raise SpotifyError(
                        "I couldn't reach Spotify just now.",
                        f"{response.status} {await response.text()}",
                    )
                if response.status == 204 or not (await response.read()):
                    return None
                return await response.json(content_type=None)
        except asyncio.TimeoutError:
            raise SpotifyError(
                "I couldn't reach Spotify just now.", f"{method} {path} timed out"
            ) from None
        except aiohttp.ClientError as exc:
            raise SpotifyError("I couldn't reach Spotify just now.", str(exc)) from None
    raise SpotifyError("I couldn't reach Spotify just now.", "retries exhausted")


# --- API surface -------------------------------------------------------------


async def search(query: str, kind: str = "track", limit: int = 5) -> dict | None:
    """Return the best-matching catalog item, or None if nothing matched."""
    if kind not in KINDS:
        raise SpotifyError(f"I can't play a {kind}.", f"unsupported kind {kind!r}")
    params = {"q": query, "type": kind, "limit": str(limit)}
    if MARKET:
        params["market"] = MARKET
    body = await _api("GET", "/search", params=params)
    items = ((body or {}).get(f"{kind}s") or {}).get("items") or []
    # Spotify occasionally returns null placeholders in items.
    for item in items:
        if item:
            return item
    return None


async def devices() -> list[dict]:
    body = await _api("GET", "/me/player/devices")
    return (body or {}).get("devices") or []


async def resolve_device(force: bool = False) -> str:
    """Find this speaker's Connect device id by name."""
    global _device_id
    if _device_id and not force:
        return _device_id
    for device in await devices():
        if (device.get("name") or "").casefold() == DEVICE_NAME.casefold():
            _device_id = device["id"]
            return _device_id
    raise SpotifyError(
        "This speaker isn't showing up in Spotify right now.",
        f"no Connect device named {DEVICE_NAME!r} -- is spotifyd running and logged in?",
    )


async def play_uri(uri: str, device_id: str, context_uri: str = "") -> None:
    """Start playback of one URI on a specific device.

    device_id is always passed: without it Spotify plays on whatever device
    was last active, which could be the user's phone in another room.

    context_uri, when given, is played with uri as the starting offset
    instead of queuing uri alone. A bare single-track queue has nothing
    before or after it, so MPRIS next/previous have nothing to do --
    previous just restarts the same track, and next stops playback
    entirely (which drops spotifyd's MPRIS session, so the transport
    buttons then look stuck). An album/playlist context gives Spotify an
    actual queue to move through.
    """
    if context_uri:
        payload = {"context_uri": context_uri, "offset": {"uri": uri}}
    elif uri.startswith("spotify:track:"):
        payload = {"uris": [uri]}
    else:
        payload = {"context_uri": uri}
    await _api("PUT", "/me/player/play", params={"device_id": device_id}, json=payload)


def describe(item: dict, kind: str) -> dict[str, str]:
    """Pull the few fields worth reading back to the user."""
    described = {"name": item.get("name", "")}
    if kind in ("track", "album"):
        artists = ", ".join(a.get("name", "") for a in item.get("artists") or [])
        if artists:
            described["artist"] = artists
    elif kind == "playlist":
        described["owner"] = (item.get("owner") or {}).get("display_name", "")
    return {key: value for key, value in described.items() if value}


async def prepare_query(query: str, kind: str = "track"):
    """Search now, play later: returns (description, start), where awaiting
    start() begins playback. Everything that can fail in a way worth telling
    the user about (nothing found, speaker not visible to Spotify) fails here.
    Raises SpotifyError with a spoken message."""
    # Concurrent: the device lookup doesn't depend on what we found, so a
    # cold "play X" costs two round trips rather than three.
    found, device_id = await asyncio.gather(search(query, kind), resolve_device())
    if not found:
        raise SpotifyError(
            f"I couldn't find anything called {query}.", f"no {kind} results for {query!r}"
        )

    # A lone track has no context of its own -- play it inside its album so
    # next/previous have a real queue to move through (see play_uri).
    context_uri = ((found.get("album") or {}).get("uri") or "") if kind == "track" else ""

    async def start() -> None:
        try:
            await play_uri(found["uri"], device_id, context_uri)
        except SpotifyError as exc:
            # A device id from a previous spotifyd session 404s -- re-resolve
            # once rather than caching a stale id for the life of the process.
            if "404" not in exc.detail:
                raise
            await play_uri(found["uri"], await resolve_device(force=True), context_uri)

    return describe(found, kind), start


async def play_query(query: str, kind: str = "track") -> dict[str, str]:
    """Search and play in one shot. Raises SpotifyError with a spoken message."""
    described, start = await prepare_query(query, kind)
    await start()
    return described


# --- one-time authorisation --------------------------------------------------


def _auth(port: int) -> int:
    """Authorization Code flow. Stdlib only, so it also runs under system python."""
    import base64
    import secrets
    import select
    import sys
    import threading
    import urllib.error
    import urllib.parse
    import urllib.request
    from http.server import BaseHTTPRequestHandler, HTTPServer

    config = _load_config()
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode(
        {
            "client_id": config["client_id"],
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
        }
    )
    print(f"\nOpen this URL in a browser signed in to the Spotify account\n"
          f"this speaker streams from:\n\n{AUTH_URL}?{query}\n")
    print(f"Either forward the port (ssh -L {port}:127.0.0.1:{port} ...) and let the\n"
          f"redirect land here, or paste the full 127.0.0.1 callback URL below.\n")

    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in params.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done -- you can close this tab and go back to the terminal.\n")
            done.set()

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # Wait for either the browser redirect or a pasted URL, whichever lands
    # first -- a headless Pi may have neither a browser nor a forwarded port.
    while not done.is_set():
        ready, _, _ = select.select([sys.stdin], [], [], 0.5)
        if not ready:
            continue
        line = sys.stdin.readline().strip()
        if not line:
            continue
        params = urllib.parse.parse_qs(urllib.parse.urlparse(line).query)
        result.update({k: v[0] for k, v in params.items()})
        break
    server.shutdown()

    if result.get("error"):
        print(f"Spotify refused the request: {result['error']}")
        return 1
    if result.get("state") != state:
        print("State mismatch -- start over rather than trusting this response.")
        return 1
    code = result.get("code")
    if not code:
        print("No authorisation code came back.")
        return 1

    data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
    ).encode()
    basic = base64.b64encode(
        f"{config['client_id']}:{config['client_secret']}".encode()
    ).decode()
    request = urllib.request.Request(
        TOKEN_URL, data=data, headers={"Authorization": f"Basic {basic}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        print(f"Token exchange failed: {exc.code} {exc.read().decode()}")
        return 1

    if not body.get("refresh_token"):
        print(f"No refresh token in the response: {body}")
        return 1
    _save_refresh_token(body["refresh_token"])
    print(f"Linked. Refresh token saved to {CONFIG_PATH}.")
    return 0


# --- CLI ---------------------------------------------------------------------


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    command = argv[1] if len(argv) > 1 else "help"

    if command == "auth":
        port = int(argv[2]) if len(argv) > 2 else urlparse_port(REDIRECT_URI)
        return _auth(port)

    async def run() -> int:
        try:
            if command == "devices":
                for device in await devices():
                    mark = "*" if device.get("is_active") else " "
                    print(f"{mark} {device['name']:<24} {device['type']:<10} {device['id']}")
            elif command == "search":
                kind = argv[3] if len(argv) > 3 else "track"
                found = await search(argv[2], kind)
                print(json.dumps(describe(found, kind) if found else {}, indent=2))
            elif command == "play":
                kind = argv[3] if len(argv) > 3 else "track"
                print(json.dumps(await play_query(argv[2], kind), indent=2))
            else:
                print(__doc__)
                return 1
        except SpotifyError as exc:
            print(f"{exc.message}\n  ({exc.detail})")
            return 1
        finally:
            await close()
        return 0

    return asyncio.run(run())


def urlparse_port(uri: str) -> int:
    import urllib.parse

    return urllib.parse.urlparse(uri).port or 8888


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
