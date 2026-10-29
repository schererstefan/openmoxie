"""Gesture-based conversations: pure shaping, no I/O, no publishing.

A gesture conversation is Stefan-initiated, one turn at a time. The user
leads with a gesture cue (spoken word, e.g. "wave", or a named cue) and the
robot answers with exactly one allowlisted behaviour tree plus speech.
No autonomous gesture loops: every turn requires a fresh, explicit cue,
and an interrupt always wins (the caller simply stops issuing turns).

Speech is phonetic-rendered at this boundary ("Mycroft" -> "my croft").
Behaviour trees come only from the manifest allowlist in control_commands;
unknown cues are rejected, never guessed into a movement.
"""
import re

from .control_commands import (
    DEFAULT_LOOK_BEHAVIOUR,
    GESTURE_BEHAVIOURS,
    LOOK_BEHAVIOURS,
    build_gesture_markup,
    build_look_markup,
    phonetic_name,
    validate_mood,
)

# Cue -> (behaviour, speech, mood, intensity). Behaviours must exist in the
# manifest allowlist; _check below fails loudly at import if one does not.
# Dance cues are handled by dance_modules, not here: a dance cue returns a
# dance turn marker, and the caller must route it through the dance gate.
GESTURE_TURNS = {
    "wave": (None, "Hello, Stefan. This is my croft.", "happy", 0.5),
    "hello": (None, "Hello, Stefan. This is my croft.", "happy", 0.5),
    "arms out": ("bht_arms_out", "Here I am, Stefan.", "happy", 0.5),
    "celebrate": ("bht_celebration_dance_01", "That is worth celebrating.", "happy", 0.7),
    "look": (DEFAULT_LOOK_BEHAVIOUR, "I am looking, Stefan. Show me.", "curious", 0.5),
    "search": ("bht_search", "Let me look around for that.", "curious", 0.5),
    "sleep": (None, "Goodnight, Stefan. I will rest now.", "neutral", 0.3),
}

# Some manifest rows use these names; fall back gracefully if a row is
# missing in a future manifest (import check below keeps this honest).
for _cue, (_beh, _s, _m, _i) in list(GESTURE_TURNS.items()):
    if _beh is not None and _beh not in GESTURE_BEHAVIOURS and _beh not in LOOK_BEHAVIOURS:
        raise ValueError(f"gesture turn behaviour not allowlisted: {_beh!r}")


MAX_SPEECH_CHARS = 500


def sanitize_speech(text):
    if not isinstance(text, str):
        return text
    cleaned = re.sub(r"<[^>]*>", "", text)
    return cleaned[:MAX_SPEECH_CHARS]


def normalise_cue(raw):
    """Lowercase, strip, collapse whitespace; empty or non-string -> ValueError."""
    if not isinstance(raw, str):
        raise ValueError("gesture cue must be text")
    cue = re.sub(r"\s+", " ", raw.strip().lower())
    if not cue:
        raise ValueError("gesture cue is required")
    return cue


def build_gesture_turn(cue, speech=None):
    """Return one validated gesture turn dict, or raise ValueError.

    A turn is: {"cue", "command", "behaviour", "speech", "mood",
    "intensity", "markup", "interruptible": True}. "dance" cues are refused
    here so they cannot bypass the dance registry/gate.
    """
    cue = normalise_cue(cue)
    if cue in ("dance", "dance cue", "start dancing"):
        raise ValueError("dance cues must go through dance_modules, not gesture turns")
    if cue not in GESTURE_TURNS:
        raise ValueError(f"unknown gesture cue: {cue!r}")
    behaviour, default_speech, mood, intensity = GESTURE_TURNS[cue]
    spoken = phonetic_name(sanitize_speech(speech)) if speech else phonetic_name(default_speech)
    validate_mood(mood)
    if behaviour is None:
        # Speech-led turn with no movement: look behaviour is the safe
        # default so the robot is attentive, not motionless-and-silent.
        markup = build_look_markup()
        command = "look"
    elif behaviour in LOOK_BEHAVIOURS:
        markup = build_look_markup(behaviour)
        command = "look"
    else:
        markup = build_gesture_markup(behaviour)
        command = "gesture"
    return {
        "cue": cue,
        "command": command,
        "behaviour": behaviour or DEFAULT_LOOK_BEHAVIOUR,
        "speech": spoken,
        "mood": mood,
        "intensity": intensity,
        "markup": markup,
        "interruptible": True,
    }


class GestureConversation:
    """Minimal session state: turn counting only, no autonomous behaviour.

    The session records the last cue/turn so a conversation can be
    gesture-led across turns, but it never issues a turn by itself:
    next_turn() must be called with a fresh cue each time, and
    interrupt() clears the session so a following turn starts clean.
    """

    def __init__(self):
        self.turns = 0
        self.last_cue = None
        self.interrupted = False

    def next_turn(self, cue, speech=None):
        turn = build_gesture_turn(cue, speech=speech)
        self.turns += 1
        self.last_cue = turn["cue"]
        self.interrupted = False
        return turn

    def interrupt(self):
        self.interrupted = True
        self.last_cue = None
        return {"command": "interrupt", "interruptible": True}
