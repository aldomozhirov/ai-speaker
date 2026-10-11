#!/usr/bin/env bash
# One-shot health check for the AI speaker stack.
#
# Exists because diagnosing a regression here has historically meant manually
# grepping journalctl for each of several independent failure classes (a
# stopped unit, lost lingering, a reset AEC register, a misrouted PipeWire
# node) one at a time -- see AI-SPEAKER-SETUP-NOTES.md. This script runs all
# of those checks in one pass and prints OK/WARN/FAIL per check.
#
# Exit code is non-zero if any check is FAIL (WARN does not affect exit code
# -- it flags something worth a look but not necessarily broken, e.g.
# AEC_AECCONVERGED reading 0 while no program audio is currently playing).
set -uo pipefail

XVF_REGISTER=(/home/aldom/.venvs/xvf3800/bin/python /home/aldom/.local/bin/xvf_register.py)
PW_NODE=(/usr/bin/python3 /home/aldom/.local/bin/pw_node.py)

UNITS=(
  xvf-aec-ref.service
  volume-led.service
  volume-sync.service
  volume-buttons.service
  welcome-sound.service
  wake-word.service
  voice-agent.service
  spotifyd.service
)

VOICE_SOURCE="${VOICE_SOURCE:-alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993700254500350-00.analog-stereo}"
VOICE_SINK="${VOICE_SINK:-alsa_output.platform-snd_aloop.0.analog-stereo}"

FAIL=0
WARN=0

ok()   { printf 'OK    %s\n' "$1"; }
warn() { printf 'WARN  %s\n' "$1"; WARN=$((WARN+1)); }
fail() { printf 'FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }

# welcome-sound.service is a oneshot that runs once at boot and is expected
# to be "inactive" afterward; success there means Result=success, not
# is-active. Everything else in this stack is a long-running service that
# should stay "active".
ONESHOT_UNITS=(welcome-sound.service)

echo "== systemd units =="
for unit in "${UNITS[@]}"; do
  is_oneshot=0
  for u in "${ONESHOT_UNITS[@]}"; do
    [[ "$unit" == "$u" ]] && is_oneshot=1
  done
  if [[ $is_oneshot -eq 1 ]]; then
    result=$(systemctl --user show "$unit" -p Result --value 2>&1)
    if [[ "$result" == "success" ]]; then
      ok "$unit ran successfully (oneshot, Result=success)"
    else
      fail "$unit Result=$result (expected success)"
    fi
  elif systemctl --user is-active --quiet "$unit"; then
    ok "$unit active"
  else
    state=$(systemctl --user is-active "$unit" 2>&1)
    fail "$unit not active (state=$state)"
  fi
done

failed_units=$(systemctl --user --failed --no-legend --plain 2>&1)
if [[ -z "$failed_units" ]]; then
  ok "no failed user units"
else
  fail "systemctl --user --failed is non-empty: $failed_units"
fi

echo
echo "== boot persistence =="
linger=$(loginctl show-user "$(whoami)" -p Linger --value 2>&1)
if [[ "$linger" == "yes" ]]; then
  ok "lingering enabled (starts at boot without login)"
else
  fail "lingering is '$linger', not 'yes' -- services will NOT start at boot without an interactive login"
fi

echo
echo "== XVF3800 AEC =="
far_end=$("${XVF_REGISTER[@]}" get AUDIO_MGR_FAR_END_DSP_ENABLE 2>&1)
if [[ "$far_end" == *1* ]]; then
  ok "AUDIO_MGR_FAR_END_DSP_ENABLE=1 (echo cancellation on)"
else
  fail "AUDIO_MGR_FAR_END_DSP_ENABLE not 1 (got: $far_end) -- echo cancellation is structurally off; this register lives in chip RAM and resets on every power cycle/USB re-enum"
fi

converged=$("${XVF_REGISTER[@]}" get AEC_AECCONVERGED 2>&1)
if [[ "$converged" == *1* ]]; then
  ok "AEC_AECCONVERGED=1 (echo model has converged)"
else
  warn "AEC_AECCONVERGED not 1 (got: $converged) -- expected if no program audio is currently playing; re-check while music plays if wake-word range under music is in question"
fi

echo
echo "== PipeWire node routing =="
if "${PW_NODE[@]}" "$VOICE_SOURCE" --timeout 3 >/dev/null 2>&1; then
  ok "voice source node present ($VOICE_SOURCE)"
else
  fail "voice source node NOT found ($VOICE_SOURCE) -- wake-word/voice-agent capture will silently fall back to the default source"
fi
if "${PW_NODE[@]}" "$VOICE_SINK" --timeout 3 >/dev/null 2>&1; then
  ok "voice sink node present ($VOICE_SINK)"
else
  fail "voice sink node NOT found ($VOICE_SINK) -- chimes/voice playback will silently fall back to the default sink"
fi
# Which node clocks the program sink. PipeWire makes one node of every group
# of linked nodes its driver, by priority.driver. On 2026-10-04 this sink tied
# with the XVF3800's USB output (both 1009), a reboot handed the clock to the
# XVF3800, and PipeWire then rate-matched this sink against snd-aloop's coarse
# clock: the echo's delay wandered by 2 ms every ~25 s and the chip's echo
# canceller removed 0 dB instead of 28 dB, with every other check green. The
# sink has priority.driver = 2500 in wireplumber.conf.d/51-smart-speaker.conf
# since then.
driver=$(pw-dump 2>/dev/null | VOICE_SINK="$VOICE_SINK" /usr/bin/python3 -c '
import json, os, sys
try:
    nodes = {o["id"]: o["info"]["props"] for o in json.load(sys.stdin)
             if o.get("type") == "PipeWire:Interface:Node"}
except Exception:
    sys.exit(0)
for i, p in nodes.items():
    if p.get("node.name") == os.environ["VOICE_SINK"]:
        d = p.get("node.driver-id", i)
        print("self" if d == i else nodes.get(d, {}).get("node.name", "node %s" % d))
' 2>/dev/null)
case "$driver" in
  self) ok "program sink is the clock master of its graph" ;;
  "")   warn "could not tell which node clocks the program sink" ;;
  *)    fail "program sink is clocked by $driver -- the echo's timing wanders and the XVF3800 cannot cancel it; check priority.driver in ~/.config/wireplumber/wireplumber.conf.d/51-smart-speaker.conf and restart wireplumber" ;;
esac
# The echo canceller's reference (the XVF3800's playback device) must not be
# rate-matched to the program sink: PipeWire then chases snd-aloop's uneven
# clock and slides the reference 2-3 ms every minute or so (2026-10-04). Both
# sinks carry the same clock.name in 51-smart-speaker.conf to switch that off.
clocks=$(pw-dump 2>/dev/null | VOICE_SINK="$VOICE_SINK" /usr/bin/python3 -c '
import json, os, sys
try:
    props = [o["info"]["props"] for o in json.load(sys.stdin) if o.get("type") == "PipeWire:Interface:Node"]
except Exception:
    sys.exit(0)
sink = [p.get("clock.name") for p in props if p.get("node.name") == os.environ["VOICE_SINK"]]
xvf = [p.get("clock.name") for p in props if str(p.get("node.name", "")).startswith("alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800")]
if sink and xvf:
    print("same" if sink[0] and sink[0] == xvf[0] else "differ")
' 2>/dev/null)
case "$clocks" in
  same)   ok "echo canceller reference is not rate-matched (shares the program sink's clock name)" ;;
  differ) warn "the XVF3800 playback device does not share the program sink's clock.name -- PipeWire rate-matches the echo canceller's reference, which then slides by 2-3 ms now and then; see ~/.config/wireplumber/wireplumber.conf.d/51-smart-speaker.conf" ;;
  *)      warn "could not compare the clock names of the program sink and the XVF3800 playback device" ;;
esac

echo
echo "== microphone signal =="
# A present node says nothing about whether audio is coming out of it. On
# 2026-10-04 the XVF3800's DSP wedged (a write-only register had been read):
# the node stayed up, every unit stayed active, every check above stayed green,
# and the capture stream was exact digital zeros -- the speaker was deaf. A
# live array idles around -60..-70 dBFS, never at zero. --raw matters here:
# without it pw-cat prepends a 24-byte AU header that is not zero. (No
# node.dont-fallback: WirePlumber 0.5.8 destroys streams that carry it as soon
# as another stream starts or stops, which made this very check flaky. The
# node's presence is already checked above.)
mic=$(timeout 5 pw-cat --record --raw --target "$VOICE_SOURCE" \
        --rate 16000 --channels 2 --format s16 - 2>/dev/null | head -c 64000 | /usr/bin/python3 -c '
import array, math, sys
data = sys.stdin.buffer.read()
d = array.array("h")
d.frombytes(data[: len(data) // 4 * 4])
if len(d) < 16000:
    print("short")
    sys.exit()
levels = []
for ch in (0, 1):
    x = d[ch::2]
    rms = math.sqrt(sum(v * v for v in x) / len(x))
    levels.append("ch%d %.0f dBFS" % (ch, 20 * math.log10(max(rms, 1e-9) / 32768)))
print(("live " if any(d) else "silent ") + ", ".join(levels))
' 2>/dev/null)
case "$mic" in
  live*)
    ok "microphone is delivering signal (${mic#live })"
    ;;
  silent*)
    fail "microphone stream is digital silence on both channels -- XVF3800 DSP wedged or array muted; wake word and voice sessions hear nothing. Known recovery: stop wake-word, volume-led and xvf-aec-ref, run 'xvf_register.py set REBOOT 1', then start them again in the reverse order"
    ;;
  *)
    fail "could not capture one second of audio from $VOICE_SOURCE"
    ;;
esac

# What the wake-word service itself thinks of its microphone, if it is a
# version that says (wake_word.py's Health). A file older than the service's
# current run was left by a previous version and is ignored.
health_file="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wake-word-health.json"
if [[ -r "$health_file" ]]; then
  started=$(date -d "$(systemctl --user show wake-word.service -p ActiveEnterTimestamp --value)" +%s 2>/dev/null || echo 0)
  if (( $(stat -c %Y "$health_file") >= started )); then
    health=$(/usr/bin/python3 -c '
import json, sys, time
d = json.load(open(sys.argv[1]))
print("%s|%d min|%s" % (d.get("state", "?"), (time.time() - d.get("since", time.time())) / 60, d.get("detail", "")))
' "$health_file" 2>/dev/null)
    case "$health" in
      ok\|*)       ok "wake word reports its microphone ok" ;;
      starting\|*) warn "wake word has not seen audio yet (still starting)" ;;
      *)           fail "wake word reports a microphone problem: ${health//|/, }" ;;
    esac
  fi
fi

echo
echo "== connectivity =="
if timeout 5 curl -sI https://generativelanguage.googleapis.com >/dev/null 2>&1; then
  ok "reachable: generativelanguage.googleapis.com (Gemini Live API)"
else
  warn "could not reach generativelanguage.googleapis.com -- voice sessions will fail to connect until this recovers"
fi

echo
if [[ $FAIL -gt 0 ]]; then
  echo "RESULT: $FAIL FAIL, $WARN WARN"
  exit 1
elif [[ $WARN -gt 0 ]]; then
  echo "RESULT: OK with $WARN WARN"
  exit 0
else
  echo "RESULT: all OK"
  exit 0
fi
