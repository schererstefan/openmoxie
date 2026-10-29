"""Creative modes: wake greeting, storytelling, celebration, reminders.

All pure shaping, no I/O, no publishing, no autonomous triggers. Every
mode is Stefan-initiated, one step at a time, interruptible.

1. Wake greeting: replaces the stock Moxie welcome ("Wow, it feels good
   to be out of that box") with a my croft greeting for Stefan, not the
   config child nickname. The caller (bridge/wake path) must choose to
   use it; nothing here fires on wake by itself.
2. Storytelling: a story is a list of beats; one beat per call, each a
   speech + mood + optional gesture. No beat auto-advances.
3. Celebration routine: a named sequence of gesture turns, returned as
   a list the caller steps through one at a time (no loop in this module).
4. Reminder: a reminder record (text, due label) with no timer, no
   publish, no autonomous delivery — delivery is a later, approved step.
"""
from .control_commands import phonetic_name, validate_mood
from .gesture_conversation import build_gesture_turn


def build_wake_greeting(name="Stefan"):
    """First line after wake, in my croft identity, not stock Moxie."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("wake greeting requires a name")
    return {
        "command": "speak",
        "speech": phonetic_name(f"Hello {name.strip()}, this is my croft. I am awake and ready."),
        "mood": "happy",
        "intensity": 0.5,
        "replaces_stock_welcome": True,
    }


def build_story_beat(text, mood="happy", intensity=0.5, gesture_cue=None):
    """One storytelling beat: speech + mood + optional gesture turn."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("story beat requires text")
    validate_mood(mood)
    if not 0.0 <= float(intensity) <= 1.0:
        raise ValueError("intensity must be between 0.0 and 1.0")
    beat = {
        "command": "speak",
        "speech": phonetic_name(text),
        "mood": mood,
        "intensity": float(intensity),
        "interruptible": True,
    }
    if gesture_cue:
        beat["gesture_turn"] = build_gesture_turn(gesture_cue)
    return beat


CELEBRATION_ROUTINE = ("celebrate", "arms out", "look")


def build_celebration_routine():
    """Return the celebration routine as a list of gesture turns.

    The caller steps through it one turn at a time, with interrupt
    available between turns. This function never plays anything.
    """
    return [build_gesture_turn(cue) for cue in CELEBRATION_ROUTINE]


def build_reminder(text, due_label=""):
    """A reminder record only. No scheduling, no publish, no delivery."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("reminder requires text")
    return {
        "command": "reminder",
        "text": text.strip(),
        "due_label": due_label.strip() if isinstance(due_label, str) else "",
        "delivered": False,
        "autonomous_delivery": False,
    }
