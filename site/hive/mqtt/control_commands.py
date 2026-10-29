"""Pure shaping/validation for the Full Control API (no I/O except allowlist load).

This module is the single place that turns validated control parameters into
robot-bound payload pieces. It never publishes MQTT itself: ``puppet_api`` in
``hive.views`` validates through here, then publishes via existing
``MoxieServer`` methods only. There is no wheel/motor/drive, head/joint-angle,
camera, or face-recognition command here -- none exists in this codebase.

Transports (all live-unproven except telehealth speech/enable):
- gesture / look: telehealth ``PLAY_OUTPUT`` markup with a single
  ``cmd:behaviour-tree`` mark (``doc/Markup.md`` format, behaviour = Lbl).
- dance / sleep: ``remote_chat`` response-action payloads built with the
  :class:`~hive.mqtt.volley.Volley` ``add_response_action`` shape.
The dance module id is fixed to ``DANCE``. One movement-class command
(gesture/dance/look/sleep) per API call; no queueing.
"""
import csv
import logging
import re
import uuid
from pathlib import Path

from .volley import Volley

logger = logging.getLogger(__name__)

# Dance is refused unless this explicit flag is True. Default False: the
# DANCE module carries a Unity-crash caution in source data, so enabling it
# is a deliberate code change here, reviewed like any other change.
ALLOW_DANCE_MODULE = False
DANCE_MODULE_ID = "DANCE"

CONTROL_COMMANDS = (
    "enable", "disable", "interrupt", "speak",
    "gesture", "dance", "look", "wake", "sleep",
)
MOVEMENT_COMMANDS = frozenset(("gesture", "dance", "look", "sleep"))

# Moods offered by the puppet page (templates/hive/puppet.html).
PUPPET_MOODS = frozenset((
    "neutral", "happy", "positive", "angry", "sad", "negative", "shy",
    "afraid", "concerned", "confused", "curious", "embarrassed",
))

# Look = face-search behaviour trees only. No head-angle/camera command exists.
LOOK_BEHAVIOURS = frozenset((
    "bht_search",
    "bht_searching",
    "bht_search_still",
    "bht_360_look_around",
    "bht_sensoryidlefacetracker_anim",
))
DEFAULT_LOOK_BEHAVIOUR = "bht_sensoryidlefacetracker_anim"

_MIN_DURATION = 0.1
_MAX_DURATION = 30.0
_DEFAULT_DURATION = 1.0

_MYCRT_RE = re.compile(r"\bMycroft\b", re.IGNORECASE)


def phonetic_name(text):
    """Render the name Mycroft as "my croft" for Moxie TTS.

    Word-boundaried and case-insensitive: "Mycroft"/"MYCROFT" become
    "my croft", "my craft" is left alone, and substring hits such as
    "Mycroftian" are not substituted. Non-string input is returned as-is.
    Applied only at the spoken control API boundary, never to chat turns.
    """
    if not isinstance(text, str):
        return text
    return _MYCRT_RE.sub("my croft", text)


def default_manifest_path():
    """Path to ``doc/AssetBundleMasterManifest.csv`` relative to this clone."""
    return Path(__file__).resolve().parents[3] / "doc" / "AssetBundleMasterManifest.csv"


def load_gesture_behaviours(csv_path=None):
    """BehaviourTree labels with Default=True from the asset manifest.

    Fails closed: a missing/unreadable manifest yields an empty frozenset so
    every gesture is then rejected rather than passed through unvalidated.
    """
    path = Path(csv_path) if csv_path else default_manifest_path()
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            rows = csv.DictReader(fh)
            return frozenset(
                (row.get("Lbl") or "").strip()
                for row in rows
                if (row.get("Asset Type") or "").strip() == "BehaviourTree"
                and (row.get("Default") or "").strip().lower() == "true"
                and (row.get("Lbl") or "").strip()
            )
    except (OSError, UnicodeDecodeError) as e:
        logger.error("Could not load behaviour allowlist from %s: %s", path, e)
        return frozenset()


GESTURE_BEHAVIOURS = load_gesture_behaviours()


def is_dance_enabled():
    return bool(ALLOW_DANCE_MODULE)


def validate_intensity(value):
    """Return intensity as float in [0.0, 1.0], else raise ValueError."""
    try:
        intensity = float(value)
    except (TypeError, ValueError):
        raise ValueError("intensity must be a number between 0.0 and 1.0") from None
    if not 0.0 <= intensity <= 1.0:
        raise ValueError("intensity must be between 0.0 and 1.0")
    return intensity


def validate_mood(value):
    mood = str(value).strip() if value is not None else ""
    if mood not in PUPPET_MOODS:
        raise ValueError(f"unknown mood: {value!r}")
    return mood


def parse_duration(value):
    """Optional behaviour duration: numeric, clamped to [0.1, 30.0] seconds."""
    if value is None or value == "":
        return _DEFAULT_DURATION
    try:
        duration = float(value)
    except (TypeError, ValueError):
        raise ValueError("duration must be a number") from None
    return max(_MIN_DURATION, min(_MAX_DURATION, duration))


def parse_single_command(raw_command, post_keys=()):
    """Return the one command in a POST, rejecting bundled commands.

    Bundling means: a ``commands`` field, or a ``command`` value listing more
    than one token (comma/plus/semicolon/whitespace separated). The contract
    is exactly one command per POST and at most one movement command, so any
    list shape is rejected instead of partially executed.
    """
    if post_keys and "commands" in post_keys:
        raise ValueError("exactly one command per request; bundled commands are not accepted")
    raw = str(raw_command).strip() if raw_command is not None else ""
    tokens = [t for t in re.split(r"[,+;\s]+", raw) if t]
    if len(tokens) != 1:
        raise ValueError("exactly one command per request; bundled commands are not accepted")
    return tokens[0]


def validate_single_movement(commands):
    """Raise if ``commands`` holds more than one movement-class command."""
    movement = [c for c in commands if c in MOVEMENT_COMMANDS]
    if len(movement) > 1:
        raise ValueError("at most one movement command per request")


def behaviour_tree_mark(behaviour, duration=_DEFAULT_DURATION):
    """Single ``cmd:behaviour-tree`` mark (doc/Markup.md format).

    ``behaviour`` must already be allowlist-validated by the caller-facing
    builders below; this function re-checks against the gesture allowlist
    (a superset of the look list) as defence in depth.
    """
    if behaviour not in GESTURE_BEHAVIOURS and behaviour not in LOOK_BEHAVIOURS:
        raise ValueError(f"behaviour not allowlisted: {behaviour!r}")
    dur = parse_duration(duration)
    return (
        '<mark name="cmd:behaviour-tree,data:{'
        f"+transition+:0.5,+duration+:{dur},+repeat+:1,"
        "+layerBlendInTime+:0.5,+layerBlendOutTime+:0.5,+blocking+:false,"
        "+action+:0,+variableName+:++,+variableValue+:++,"
        "+eventName+:+Gesture_None+,+lifetime+:0,+category+:+None+,"
        f"+behaviour+:+{behaviour}+,+Track+:++}}"
        '"/>'
    )


def build_gesture_markup(behaviour, duration=None):
    if behaviour not in GESTURE_BEHAVIOURS:
        raise ValueError(f"gesture behaviour not allowlisted: {behaviour!r}")
    return behaviour_tree_mark(behaviour, duration=parse_duration(duration))


def build_look_markup(behaviour=None, duration=None):
    chosen = behaviour or DEFAULT_LOOK_BEHAVIOUR
    if chosen not in LOOK_BEHAVIOURS:
        raise ValueError(f"look behaviour not allowlisted: {behaviour!r}")
    return behaviour_tree_mark(chosen, duration=parse_duration(duration))


def _remote_chat_action_payload(action_name, module_id=None, content_id=None):
    request = {"event_id": str(uuid.uuid4()), "backend": "router", "command": "continue"}
    volley = Volley(request)
    volley.add_response_action(action_name, module_id=module_id, content_id=content_id)
    return volley.response


def build_dance_payload(module_id=None):
    """Launch-DANCE remote_chat payload. Flagged off by default."""
    if module_id not in (None, "", DANCE_MODULE_ID):
        raise ValueError("dance module_id is fixed to DANCE")
    if not is_dance_enabled():
        raise ValueError("dance module is disabled (ALLOW_DANCE_MODULE is False)")
    return _remote_chat_action_payload("launch", module_id=DANCE_MODULE_ID)


def build_sleep_payload():
    return _remote_chat_action_payload("sleep")
