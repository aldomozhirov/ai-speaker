#!/usr/bin/env bash
# AEC reference feed: program audio (pre-CamillaDSP, tapped from the
# camilladsp-loopback sink's monitor) -> XVF3800 USB playback input.
#
# The XVF3800's echo canceller needs the far-end signal on the left channel
# of its USB playback input (it discards the right channel). This is a
# reference path only: the XVF3800's own analog output must stay
# unconnected, and CamillaDSP remains the sole owner of hw:sndrpihifiberry.
#
# History of failed attempts, for the next person who touches this:
#
# 1. Original: a static pipewire.conf.d module-loopback. Loaded inline at
#    PipeWire daemon startup, before WirePlumber had created the XVF3800 USB
#    node, so target.object never resolved -- fell back to the default sink
#    (this same aloop sink), broken on every reboot.
#
# 2. Replaced with a pw-loopback *client* stream (like pw-cat), assuming
#    client streams get WirePlumber's late-binding/retry for named targets.
#    Wrong -- same fallback-to-default-sink failure on reboot.
#
# 3. Added a wait loop that polls `pw-cli ls Node` for the XVF3800 node
#    before ever starting pw-loopback. Still failed on reboot: the node
#    object appears in the graph before WirePlumber has actually finished
#    probing/negotiating the USB ALSA device, so a stream created right
#    after the node "exists" can still get resolved against it too early
#    and fall through to the same default-sink fallback -- and once
#    WirePlumber commits a link (right or wrong), it never revisits it.
#
# Current approach: stop trying to predict exactly when WirePlumber is
# ready and instead supervise the actual link state after the fact. The
# observed fallback target is always this same aloop sink (it's the
# system's configured default sink), so explicitly disconnect that specific
# wrong link and (re)connect the correct one, repeatedly, until it sticks.
# Cheap and idempotent -- reconnecting an already-correct link, or
# disconnecting a link that doesn't exist, are both harmless no-ops.
#
# node.target names resolved via `wpctl status` -- do not hardcode IDs, they
# are not stable across reboots.
#
# Separately: AUDIO_MGR_FAR_END_DSP_ENABLE (the chip register that actually
# turns echo cancellation on) lives in chip RAM, not flash. It was found off
# and fixed once by hand, then found off again after later reboots -- routing
# audio here correctly does nothing for echo cancellation if this register
# has reset. Reapplied unconditionally on every start below.
set -uo pipefail

NODE_NAME="xvf-aec-ref"
# Delay applied to the AEC reference feed, in seconds. Overridable so the
# alignment can be swept empirically (see AEC-REFERENCE-DELAY-SWEEP.md); the
# default preserves the long-standing value.
REF_DELAY="${XVF_AEC_REF_DELAY:-0.341}"
CAPTURE_TARGET="alsa_output.platform-snd_aloop.0.analog-stereo"
PLAYBACK_TARGET="alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993700254500350-00.analog-stereo"

XVF_REGISTER=(/home/aldom/.venvs/xvf3800/bin/python /home/aldom/.local/bin/xvf_register.py)

# AUDIO_MGR_FAR_END_DSP_ENABLE lives in chip RAM, not flash: it resets to 0
# (echo cancellation structurally off) on every power cycle / USB re-enum,
# so it has to be reapplied on every boot rather than being a one-time fix.
#
# Retried rather than attempted once: at boot this service can easily win the
# race against USB enumeration, and a single failed write here is silent and
# total -- the audio routing below would still come up looking healthy with
# echo cancellation structurally off underneath it.
enable_far_end_dsp() {
  "${XVF_REGISTER[@]}" ensure AUDIO_MGR_FAR_END_DSP_ENABLE 1 >/dev/null 2>&1
}

# The XVF3800's *playback* volume attenuates the AEC reference before the chip's
# far-end path ever sees it, and PipeWire's sink volume for this device IS that
# hardware mixer (verified: `wpctl set-volume <xvf> 1.0` moves amixer PCM,0 to
# 0.00dB). It was found at 0.40 == -23dB, which buried the reference ~40dB down,
# at the far-end taps' noise floor -- the AEC had a reference too quiet to use.
#
# This is invisible by ear: the XVF3800's analog output is deliberately left
# unconnected (CamillaDSP owns the real output path), so a low playback volume
# on this device silences nothing you can hear. It only starves the echo
# canceller. Asserted here on every start, same convention as the register above.
assert_reference_gains() {
  local id
  id=$(pw-dump 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
print(next((o['id'] for o in d
            if o.get('info',{}).get('props',{}).get('node.name')=='$PLAYBACK_TARGET'), ''))
" 2>/dev/null)
  [ -n "$id" ] && wpctl set-volume "$id" 1.0 >/dev/null 2>&1
  # PCM,1 is a second, independent playback control on this card (mono, not
  # touched by the PipeWire sink volume) -- max it too so nothing on the
  # reference path is attenuating.
  amixer -c Array sset PCM,1 100% >/dev/null 2>&1
}

for attempt in $(seq 30); do
  if enable_far_end_dsp; then
    break
  fi
  if (( attempt == 30 )); then
    echo "warning: could not enable AUDIO_MGR_FAR_END_DSP_ENABLE after 30 tries" >&2
  fi
  sleep 2
done

assert_reference_gains

/usr/bin/pw-loopback \
  --name="$NODE_NAME" \
  --channels=1 \
  --channel-map=MONO \
  --delay="$REF_DELAY" \
  --capture="$CAPTURE_TARGET" \
  --capture-props='{ stream.capture.sink = true node.passive = true node.description = "XVF3800 AEC reference (capture)" }' \
  --playback="$PLAYBACK_TARGET" \
  --playback-props='{ audio.position = [ FL ] node.passive = true node.description = "XVF3800 AEC reference (playback)" }' &
LOOPBACK_PID=$!

reconcile() {
  # Undo the known-bad fallback (WirePlumber looping our own output back
  # into the aloop sink it captures from) and (re)establish the real link.
  pw-link -d "output.$NODE_NAME:output_FL" "$CAPTURE_TARGET:playback_FL" >/dev/null 2>&1
  pw-link -d "output.$NODE_NAME:output_FR" "$CAPTURE_TARGET:playback_FR" >/dev/null 2>&1
  pw-link "output.$NODE_NAME:output_FL" "$PLAYBACK_TARGET:playback_FL" >/dev/null 2>&1
}

# Reconcile aggressively while the graph is settling right after boot, then
# back off to a slow defensive poll for the life of the service (covers a
# USB replug event racing the same way).
i=0
slow_ticks=0
while kill -0 "$LOOPBACK_PID" 2>/dev/null; do
  reconcile
  if (( i < 60 )); then
    sleep 1
    (( i++ ))
  else
    # A USB replug resets AUDIO_MGR_FAR_END_DSP_ENABLE without necessarily
    # killing pw-loopback, so the startup write above wouldn't run again.
    # Re-check every ~5 min; it's a no-op read when the register is already 1.
    (( slow_ticks++ ))
    if (( slow_ticks % 10 == 0 )); then
      enable_far_end_dsp || true
      assert_reference_gains || true
    fi
    sleep 30
  fi
done

wait "$LOOPBACK_PID"
