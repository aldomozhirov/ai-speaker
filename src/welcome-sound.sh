#!/bin/bash
# Plays welcome.mp3 once at boot, via welcome-sound.service.
#
# Unlike every other sound_fx.py caller (session start/end, Spotify connect),
# which only ever fire well after boot from a real user trigger -- by which
# point the aloop sink has already been proven to exist because voice-agent
# and spotifyd depend on it too -- this one runs at boot itself and has no
# such guarantee.
#
# pw-cat does *not* error out when --target doesn't resolve to a real node;
# it silently falls back to the default sink (confirmed by hand, 2026-08-22,
# same failure class as the XVF3800 AEC reference routing problem documented
# in AI-SPEAKER-SETUP-NOTES.md). So rather than assume readiness from
# service ordering alone, use the shared pw_node.py wait (also used by
# wake_word.py and pw_transport.py) to poll for the target node to actually
# exist before ever invoking sound_fx.py.
TARGET="${SOUND_FX_SINK:-alsa_output.platform-snd_aloop.0.analog-stereo}"

if /usr/bin/python3 "$HOME/.local/bin/pw_node.py" "$TARGET" --timeout 15; then
    exec /usr/bin/python3 "$HOME/.local/bin/sound_fx.py" welcome
fi

logger -t welcome-sound "Target node ${TARGET} never appeared after 15s; skipping welcome chime"
exit 0
