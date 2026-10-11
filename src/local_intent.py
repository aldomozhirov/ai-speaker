"""Offline grammar-constrained command matching for trivial voice commands.

Every wake-word trigger today opens a full Gemini Live cloud session, even
for "pause" or "volume up" -- a real round trip and free-tier session quota
spent on a one-word command. This module runs a small offline Vosk model,
constrained to a fixed grammar of the handful of commands
spotify_tools.py/volume_tools.py already implement, against the audio
captured in the couple of seconds right after a wake-word trigger. A match
lets wake_word.py handle the command locally without ever opening a Gemini
session; no match falls through to the normal cloud session exactly as
before.

Grammar-constrained (not open-vocabulary) recognition on purpose: Vosk's
`SetGrammar` restricts the decoder to a fixed phrase list, which is both far
more accurate than open recognition for a tiny fixed set of commands (no
free-text misrecognition to filter) and cheap enough to run inline in
wake_word.py's main loop on a Pi 5 without a separate worker.

**Off by default** (LOCAL_INTENT_ENABLED=0). Two real trade-offs a user
should confirm live before relying on this, matching this project's existing
convention (see quick-action session close, half-duplex) of shipping
opt-in behind an env var rather than assuming a design works:

1. Latency: matching adds up to LOCAL_INTENT_WINDOW_SECONDS of buffering
   before either handling locally or falling through to Gemini (shorter in
   practice -- buffering stops early on trailing silence).
2. Audio loss on the fall-through path: if the model doesn't match, whatever
   was buffered during the matching window is NOT replayed into the Gemini
   session that opens afterward -- the user's command audio during that
   window is lost, same as it would be lost during any other silent gap
   before the transport starts capturing. Only worth enabling once real
   commands are confirmed to match reliably enough that this fallback case
   is rare.
"""

from __future__ import annotations

import json
import logging
import os

LOG = logging.getLogger("wake-word.local-intent")

# One entry per recognizable phrase -> (module, action, kwargs). Multiple
# phrasings can map to the same action. Kept intentionally small: this is a
# fixed grammar, not open recognition, so every phrase added here is a
# phrase Vosk is told to listen for specifically.
_GRAMMAR: dict[str, tuple[str, dict]] = {
    "play": ("spotify_play", {}),
    "resume": ("spotify_play", {}),
    "pause": ("spotify_pause", {}),
    "stop": ("spotify_pause", {}),
    "next": ("spotify_next", {}),
    "next track": ("spotify_next", {}),
    "skip": ("spotify_next", {}),
    "previous": ("spotify_previous", {}),
    "previous track": ("spotify_previous", {}),
    "back": ("spotify_previous", {}),
    "volume up": ("adjust_volume", {"direction": "up"}),
    "turn it up": ("adjust_volume", {"direction": "up"}),
    "louder": ("adjust_volume", {"direction": "up"}),
    "volume down": ("adjust_volume", {"direction": "down"}),
    "turn it down": ("adjust_volume", {"direction": "down"}),
    "quieter": ("adjust_volume", {"direction": "down"}),
    # Cancel a *scheduled* (not yet ringing) timer/alarm. Silencing one that
    # is already ringing is a different path entirely -- wake_word.py's
    # WakeTrigger.fire() intercepts before local_intent ever runs, and
    # timer_alarm.py's own open-mic stop-listener handles bare "stop".
    "cancel the timer": ("cancel_timer", {}),
    "cancel timer": ("cancel_timer", {}),
    "stop the timer": ("cancel_timer", {}),
    "cancel the alarm": ("cancel_alarm", {}),
    "cancel alarm": ("cancel_alarm", {}),
    "stop the alarm": ("cancel_alarm", {}),
}

LOCAL_INTENT_ENABLED = os.environ.get("LOCAL_INTENT_ENABLED", "0").lower() in {"1", "true", "yes"}
LOCAL_INTENT_MODEL_DIR = os.environ.get(
    "LOCAL_INTENT_MODEL_DIR",
    os.path.expanduser("~/.local/share/ai-speaker/vosk-model-small-en-us-0.15"),
)
# Raised from the original 2.2s to fit longer timer/alarm phrases ("set a
# timer for twenty five minutes"). This is a cap, not a fixed cost --
# _buffer_command_audio in wake_word.py still stops early on trailing
# silence, so short commands aren't slowed down by the higher ceiling.
LOCAL_INTENT_WINDOW_SECONDS = float(os.environ.get("LOCAL_INTENT_WINDOW_SECONDS", "5.0"))
# Stop buffering early once trailing near-silence this long follows any
# signal -- avoids paying the full window on every short command.
LOCAL_INTENT_SILENCE_CUTOFF_SECONDS = float(
    os.environ.get("LOCAL_INTENT_SILENCE_CUTOFF_SECONDS", "0.6")
)
LOCAL_INTENT_SILENCE_PEAK = int(os.environ.get("LOCAL_INTENT_SILENCE_PEAK", "300"))

# --- Timer/alarm: number-word parsing on top of the fixed grammar above ---
#
# "set a timer for twenty five minutes" / "set an alarm for seven fifteen"
# have too many valid phrasings to enumerate as _GRAMMAR entries. Instead,
# _TIMER_ALARM_VOCAB below adds the individual words needed (numbers, units,
# control words) to the grammar passed to Vosk's SetGrammar -- which
# recognizes free *sequences* composed from a word list just as well as it
# matches whole fixed phrases (the existing multi-word entries like "next
# track" and single-word entries like "next" already coexist in one list
# today, confirming both forms compose in the same grammar). match() then
# parses the recognized text with the small deterministic parsers below
# instead of a dict lookup.
_ONES = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_DURATION_UNITS = {
    "second": 1, "seconds": 1,
    "minute": 60, "minutes": 60,
    "hour": 3600, "hours": 3600,
}
# Singular forms only -- used to disambiguate the "for"/"four" homophone in
# _parse_duration (see there).
_SINGULAR_UNITS = {"second", "minute", "hour"}
_CONTROL_WORDS = {
    "set", "a", "an", "the", "timer", "alarm", "for", "to", "at", "and",
    "half", "o'clock", "am", "pm", "please", "cancel",
}
_TIMER_ALARM_VOCAB = sorted(
    set(_ONES) | set(_TENS) | set(_DURATION_UNITS) | _CONTROL_WORDS
)


def _consume_number(tokens: list[str], i: int) -> tuple[int | None, int]:
    """Parses one integer starting at tokens[i] (e.g. ["twenty", "five"] ->
    25). Returns (value, next_index), or (None, i) if tokens[i] isn't a
    number word."""
    if i >= len(tokens):
        return None, i
    tok = tokens[i]
    if tok in _TENS:
        value = _TENS[tok]
        j = i + 1
        if j < len(tokens) and _ONES.get(tokens[j], 10) < 10:
            value += _ONES[tokens[j]]
            j += 1
        return value, j
    if tok in _ONES:
        return _ONES[tok], i + 1
    return None, i


def _parse_duration(tokens: list[str]) -> dict | None:
    """Finds "<number> <unit>" runs anywhere in tokens (e.g. "...for twenty
    five minutes" -> 1500 seconds), summing multiple runs so "an hour and
    thirty minutes" works too. Returns {"seconds": N} or None."""
    total_seconds = 0
    found = False
    i, n = 0, len(tokens)
    while i < n:
        # "for" and "four" are homophones and both are in the grammar, so
        # Vosk regularly returns "four" where the user said "for" (observed
        # live 2026-08-30: "set a timer for a minute" -> "set the timer four
        # ... minute"). A count of four against a *singular* unit ("four
        # minute") is not something anyone says, so read it as the "for a
        # <unit>" it almost certainly was. Plural ("four minutes") is a
        # genuine quantity and is left alone.
        if tokens[i] == "four" and i + 1 < n and tokens[i + 1] in _SINGULAR_UNITS:
            total_seconds += _DURATION_UNITS[tokens[i + 1]]
            found = True
            i += 2
            continue
        if tokens[i] in ("a", "an") and i + 1 < n and tokens[i + 1] in _DURATION_UNITS:
            total_seconds += _DURATION_UNITS[tokens[i + 1]]
            found = True
            i += 2
            continue
        value, j = _consume_number(tokens, i)
        if value is not None and j < n and tokens[j] in _DURATION_UNITS:
            total_seconds += value * _DURATION_UNITS[tokens[j]]
            found = True
            i = j + 1
            continue
        i += 1
    if not found or total_seconds <= 0:
        return None
    return {"seconds": total_seconds}


def _parse_clock_time(tokens: list[str]) -> dict | None:
    """Finds the first "<hour> [<minute>] [am|pm]" or "<hour> o'clock" run in
    tokens (e.g. "...for seven fifteen pm" -> hour=19, minute=15). Returns
    {"hour": H, "minute": M} or None."""
    n = len(tokens)
    for i in range(n):
        hour, j = _consume_number(tokens, i)
        if hour is None or not (0 <= hour <= 23):
            continue
        minute, k = 0, j
        if k < n and tokens[k] == "o'clock":
            k += 1
        else:
            m, k2 = _consume_number(tokens, k)
            if m is not None and 0 <= m <= 59 and k2 > k:
                minute, k = m, k2
        pm = None
        if k < n and tokens[k] in ("am", "pm"):
            pm = tokens[k] == "pm"
        if pm is True and hour < 12:
            hour += 12
        elif pm is False and hour == 12:
            hour = 0
        return {"hour": hour, "minute": minute}
    return None


_model = None


def available() -> bool:
    return LOCAL_INTENT_ENABLED


def _load_model():
    global _model
    if _model is not None:
        return _model
    import vosk

    vosk.SetLogLevel(-1)  # Vosk logs its own C++-level lines to stderr by default.
    if not os.path.isdir(LOCAL_INTENT_MODEL_DIR):
        raise FileNotFoundError(f"Vosk model dir not found: {LOCAL_INTENT_MODEL_DIR}")
    _model = vosk.Model(LOCAL_INTENT_MODEL_DIR)
    return _model


_LAST_TEXT = ""


def last_text() -> str:
    """Whatever the last match() call actually recognized (""" '"""' + """ if it
    heard nothing). Lets wake_word.py tell "heard nothing" apart from "heard
    half a timer command"."""
    return _LAST_TEXT


def looks_truncated(text: str) -> bool:
    """True if `text` names a timer/alarm but carries no duration or clock
    time -- the signature of a buffer that ended on a mid-phrase pause
    ("set a timer for..." <pause> "...ten minutes"), which the 0.6s silence
    cutoff in wake_word.py's _buffer_command_audio does hit in practice.
    Tells the caller to keep listening and re-match rather than fall through
    to a cloud session that can't set timers anyway.
    """
    tokens = text.split()
    if "cancel" in tokens:  # a complete command on its own
        return False
    # Gated exactly as match() gates them. Running both parsers on every
    # utterance instead would misjudge "set the timer four" as complete,
    # because _parse_clock_time happily reads the stray "four" as 4 o'clock
    # even though match() would never consult it without an "alarm" token.
    if "timer" in tokens:
        return _parse_duration(tokens) is None
    if "alarm" in tokens:
        return _parse_clock_time(tokens) is None
    return False


def match(pcm16_mono_16k: bytes, sample_rate: int = 16000) -> tuple[str, dict] | None:
    """Run the grammar-constrained recognizer over one buffered utterance.

    Returns (action_name, kwargs) from _GRAMMAR on a match, else None. Never
    raises for a normal "didn't understand" outcome -- only for a genuine
    setup problem (missing model), which the caller should let disable this
    feature for the session rather than crash wake_word.py over.
    """
    import vosk

    model = _load_model()
    grammar = json.dumps(list(_GRAMMAR.keys()) + _TIMER_ALARM_VOCAB + ["[unk]"])
    recognizer = vosk.KaldiRecognizer(model, sample_rate, grammar)
    recognizer.AcceptWaveform(pcm16_mono_16k)
    result = json.loads(recognizer.FinalResult())
    text = (result.get("text") or "").strip()
    global _LAST_TEXT
    _LAST_TEXT = text
    if not text or text == "[unk]":
        LOG.info("local_intent: no grammar match (heard: %r)", text)
        return None

    hit = _GRAMMAR.get(text)
    if hit is not None:
        LOG.info("local_intent: matched %r -> %s%s", text, hit[0], hit[1] or "")
        return hit

    # Not one of the fixed short phrases -- try the timer/alarm word-sequence
    # parsers before giving up (see _parse_duration/_parse_clock_time above).
    tokens = text.split()
    if "cancel" in tokens:
        for kind in ("timer", "alarm"):
            if kind in tokens:
                LOG.info("local_intent: matched %r -> cancel_%s", text, kind)
                return f"cancel_{kind}", {}
    if "timer" in tokens:
        duration = _parse_duration(tokens)
        if duration is not None:
            LOG.info("local_intent: matched %r -> set_timer%s", text, duration)
            return "set_timer", duration
    if "alarm" in tokens:
        clock = _parse_clock_time(tokens)
        if clock is not None:
            LOG.info("local_intent: matched %r -> set_alarm%s", text, clock)
            return "set_alarm", clock

    LOG.info("local_intent: recognized %r but it's not in the grammar map", text)
    return None
