#!/bin/bash
# spotifyd on_song_change_hook (aka --onevent). Invoked by spotifyd for every
# PLAYER_EVENT, not just song changes despite the option's name -- including
# sessionconnected, fired when a client (phone/desktop) takes over this
# device via Spotify Connect. That's the one event we react to here.
#
# Confirmed live 2026-08-22 via `journalctl -t spotifyd-hook`: the real value
# is "sessionconnected" (no underscore) -- despite librespot's own PlayerEvent
# enum being SessionConnected, spotifyd's event-to-string conversion strips
# underscores from the variant name. The initial guess ("session_connected",
# from binary strings alone) was wrong and never fired.
logger -t spotifyd-hook "PLAYER_EVENT=${PLAYER_EVENT:-<unset>}"

if [ "$PLAYER_EVENT" = "sessionconnected" ]; then
    # Backgrounded and detached: spotifyd should not be kept waiting on chime
    # playback, and this script's own exit must not kill sound_fx.py's
    # ffmpeg/pw-cat children.
    nohup /usr/bin/python3 "$HOME/.local/bin/sound_fx.py" spotify-connect \
        >/dev/null 2>&1 &
    disown
fi
