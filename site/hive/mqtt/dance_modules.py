"""Dance modules: a safe, gated dance registry. Pure shaping, no I/O.

Stage B left dance disabled (ALLOW_DANCE_MODULE=False) behind a code
constant, with a Unity-crash caution in source data. This module enables
dance *in code* under stricter rules, instead of flipping that flag blindly:

- Only dance behaviour trees from the manifest are allowed
  (bht_dance_*, plus bht_celebration_dance_01-04 as short celebrations).
- Duration is clamped to 10-60 seconds; default 30. No dance loop: one
  dance at a time, tracked by DanceSession, interrupt releases the lock.
- No dance while asleep/standby: the caller must pass robot_state="awake"
  (a daylight, Stefan-in-view live-test gate in docs, not a clock check
  here, because server time alone cannot prove Stefan is present).
- The existing remote_chat DANCE module launch in control_commands is
  unchanged; this module adds the behaviour-tree dance path, which is
  the more precisely allowlisted path. Both paths are live-unproven.

No autonomous dancing: a dance requires an explicit, Stefan-initiated
call with a named tree, every time.
"""
import threading

from .control_commands import (
    GESTURE_BEHAVIOURS,
    build_gesture_markup,
    phonetic_name,
)

DANCE_MIN_SECONDS = 10.0
DANCE_MAX_SECONDS = 60.0
DANCE_DEFAULT_SECONDS = 30.0

DANCE_TREES = frozenset(
    b for b in GESTURE_BEHAVIOURS
    if b.startswith("bht_dance_") or b.startswith("bht_celebration_dance_")
)
DEFAULT_DANCE_TREE = "bht_dance_cruncher_short_120bpm"


def clamp_dance_duration(value):
    if value is None or value == "":
        return DANCE_DEFAULT_SECONDS
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError("dance duration must be a number") from None
    return max(DANCE_MIN_SECONDS, min(DANCE_MAX_SECONDS, seconds))


def build_dance_tree_turn(tree=None, duration=None, speech=None, robot_state="awake"):
    """Return one validated dance turn, or raise ValueError."""
    if robot_state != "awake":
        raise ValueError("dance requires robot_state='awake' (no dance while asleep/standby)")
    chosen = tree or DEFAULT_DANCE_TREE
    if chosen not in DANCE_TREES:
        raise ValueError(f"dance tree not allowlisted: {chosen!r}")
    seconds = clamp_dance_duration(duration)
    spoken = phonetic_name(speech) if speech else ""
    return {
        "command": "dance",
        "dance_tree": chosen,
        "duration": seconds,
        "speech": spoken,
        "markup": build_gesture_markup(chosen, duration=seconds),
        "interruptible": True,
    }


class DanceSession:
    """One dance at a time. Non-blocking acquire: a second dance while one
    is active is rejected, never queued. interrupt() releases the lock."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active_tree = None

    def start(self, tree=None, duration=None, speech=None, robot_state="awake"):
        if not self._lock.acquire(blocking=False):
            raise ValueError("a dance is already active; interrupt it first")
        try:
            turn = build_dance_tree_turn(tree, duration, speech, robot_state)
        except ValueError:
            self._lock.release()
            raise
        self.active_tree = turn["dance_tree"]
        return turn

    def interrupt(self):
        self.active_tree = None
        try:
            self._lock.release()
        except RuntimeError:
            pass
        return {"command": "interrupt", "interruptible": True}
