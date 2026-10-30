"""Bridge -> Puppet control wiring: pure translation, publishes nothing.

This module wires BridgeController turn outputs to the existing Puppet
control path shapes (hive.mqtt.control_commands) as control-action
dicts. It is a translation layer only:

- It never publishes MQTT, never calls MoxieServer, never touches the
  bridge transport. Callers receive action dicts; publishing (if ever
  approved) stays with the existing puppet_api / MoxieServer path.
- It adds no HTTP endpoint and widens no API surface. puppet_api is
  unauthenticated (@csrf_exempt); expanding it while the vuln-audit
  auth decisions are pending would widen that surface, so the wiring
  deliberately stops at validated action dicts.

Contract (one control action per turn):
- Every wired turn carries "control_actions": a list of length 0 or 1.
  A turn selecting gesture, dance, show-me, celebrate, story, sleep,
  wake, or interrupt yields exactly one action. Speech accompanying
  an action is a field of that action, never a second action; nested
  gesture turns (chat/story) collapse into the single action. Pure
  speech turns (chat, read-only tools, refusals, agent-absent) yield
  zero actions, so refused action intents are never queued.
- Gesture/look behaviours are re-validated against the manifest
  allowlist at this boundary and markup is rebuilt, never trusted
  from the turn. Unknown trees fail closed: zero actions plus an
  explicit error in the turn, never a guessed tree.
- Dance is gated OFF by default, preserving the Stage B
  ALLOW_DANCE_MODULE=False behaviour: a dance turn yields no action
  unless the caller passes allow_dance=True for that single turn.
  Even then the robot must be awake, the tree must be an allowlisted
  dance tree, duration is clamped to 10-60s, and the bridge session
  allows one dance at a time (interrupt releases it). This layer
  never flips ALLOW_DANCE_MODULE and never builds the remote_chat
  DANCE launch payload.
- Show-me keeps its session protocol (idle -> awaiting -> received ->
  described) with no storage: no file writes, no media keys or byte
  payloads are copied into actions, and stored_on_disk is always
  False. Only the text description token crosses this boundary.
- Interrupt wins: a stop/interrupt word anywhere in the turn forces
  the interrupt action, clearing every sub-session, even when the
  same turn also mentions dance, gesture, show-me, or sleep.
- Wake returns an action dict under the bridge MQTT gate
  (sleeping -> wake allowed; standby/unknown -> ValueError) without
  touching the transport.
"""
import re

from .control_commands import (
    DEFAULT_LOOK_BEHAVIOUR,
    GESTURE_BEHAVIOURS,
    LOOK_BEHAVIOURS,
    build_gesture_markup,
    build_look_markup,
    build_sleep_payload,
    phonetic_name,
    validate_single_movement,
)
from .dance_modules import DANCE_TREES, clamp_dance_duration
from .gesture_conversation import build_gesture_turn, sanitize_speech
from .mycroft_bridge import MODE_BRIDGE, BridgeController

CONTROL_ACTION_KINDS = (
    "gesture", "look", "dance", "show_me", "celebrate",
    "story", "sleep", "wake", "interrupt",
)

DANCE_GATED_ERROR = (
    "dance gated off by default (ALLOW_DANCE_MODULE=False); "
    "no control action produced"
)

_MEDIA_KEY_TOKENS = ("image", "video", "audio", "photo", "bytes", "recording")

_INTERRUPT_RE = re.compile(r"\b(stop|interrupt)\b")


def _contains_bytes(value):
    if isinstance(value, (bytes, bytearray)):
        return True
    if isinstance(value, dict):
        return any(_contains_bytes(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_bytes(v) for v in value)
    return False


def _is_media_key(key):
    low = str(key).lower()
    return any(token in low for token in _MEDIA_KEY_TOKENS)


def _validated_behaviour_action(behaviour, speech=""):
    """Return (action, error): one allowlist-validated gesture/look action."""
    if behaviour in LOOK_BEHAVIOURS:
        markup = build_look_markup(behaviour)
        command = "look"
        kind = "look"
    elif behaviour in GESTURE_BEHAVIOURS:
        markup = build_gesture_markup(behaviour)
        command = "gesture"
        kind = "gesture"
    else:
        return None, "behaviour not allowlisted: %r" % (behaviour,)
    return {
        "kind": kind,
        "command": command,
        "transport": "telehealth",
        "behaviour": behaviour,
        "markup": markup,
        "speech": speech or "",
        "interruptible": True,
    }, None


def gesture_action_for_cue(cue):
    """Return (action, error) for one gesture cue. Fail-closed.

    Unknown cues (and dance cues, which belong to the dance gate)
    return (None, explicit error): never a guessed tree.
    """
    try:
        turn = build_gesture_turn(cue)
    except ValueError as e:
        return None, str(e)
    turn["intent"] = "gesture"
    actions, error = turn_to_control_actions(turn)
    if error:
        return None, error
    return actions[0], None


def turn_to_control_actions(turn, robot_state="awake", allow_dance=False):
    """Translate one bridge turn to (control_actions, error).

    Pure and total on policy: refusals return ([], explicit error);
    success returns ([one action], None) or ([], None) for pure
    speech. Raises ValueError only for a malformed (non-dict) turn.
    Publishes nothing; retains no media payloads.
    """
    if not isinstance(turn, dict):
        raise ValueError("turn must be a dict")
    intent = turn.get("intent")
    command = turn.get("command")
    speech = turn.get("speech") or ""

    if intent == "interrupt" or command == "interrupt":
        return [{
            "kind": "interrupt", "command": "interrupt",
            "transport": "telehealth", "interruptible": True,
        }], None

    if intent == "sleep" or command == "sleep":
        return [{
            "kind": "sleep", "command": "sleep",
            "transport": "remote_chat", "speech": speech,
            "payload": build_sleep_payload(), "interruptible": True,
        }], None

    if intent == "dance" or command == "dance":
        if not allow_dance:
            return [], DANCE_GATED_ERROR
        if robot_state != "awake":
            return [], "dance requires robot_state='awake'"
        tree = turn.get("dance_tree") or turn.get("behaviour")
        if tree not in DANCE_TREES:
            return [], "dance tree not allowlisted: %r" % (tree,)
        try:
            seconds = clamp_dance_duration(turn.get("duration"))
        except ValueError as e:
            return [], str(e)
        return [{
            "kind": "dance", "command": "dance",
            "transport": "telehealth", "behaviour": tree,
            "duration": seconds,
            "markup": build_gesture_markup(tree, duration=seconds),
            "speech": speech, "interruptible": True,
        }], None

    if intent == "show_me":
        behaviour = turn.get("behaviour") or DEFAULT_LOOK_BEHAVIOUR
        action, error = _validated_behaviour_action(behaviour, speech)
        if error:
            return [], error
        action["kind"] = "show_me"
        action["phase"] = (
            "awaiting" if turn.get("session_state") == "awaiting_show"
            else "described")
        # Only the text token crosses; no media is ever retained.
        description = turn.get("shown_description")
        if isinstance(description, str):
            action["description"] = description
        action["stored_on_disk"] = False
        return [action], None

    if intent == "celebrate":
        behaviour = turn.get("behaviour")
        if behaviour:
            action, error = _validated_behaviour_action(behaviour, speech)
            if error:
                return [], error
            action["kind"] = "celebrate"
            return [action], None
        return [{
            "kind": "celebrate", "command": "speak",
            "transport": "telehealth", "speech": speech,
            "interruptible": True,
        }], None

    if intent == "story":
        action = {
            "kind": "story", "command": "speak",
            "transport": "telehealth", "speech": speech,
            "interruptible": True,
        }
        nested = turn.get("gesture_turn")
        if isinstance(nested, dict):
            gesture, error = _validated_behaviour_action(
                nested.get("behaviour"), speech)
            if error:
                return [], error
            action["behaviour"] = gesture["behaviour"]
            action["markup"] = gesture["markup"]
        return [action], None

    if intent == "gesture" or command in ("gesture", "look"):
        behaviour = turn.get("behaviour")
        if behaviour is None and command == "look":
            behaviour = DEFAULT_LOOK_BEHAVIOUR
        if behaviour is None:
            return [], "gesture turn is missing its behaviour tree"
        action, error = _validated_behaviour_action(behaviour, speech)
        if error:
            return [], error
        if _contains_bytes(turn):
            # Media smuggled on the turn never reaches the action.
            action = {
                k: v for k, v in action.items() if not _is_media_key(k)}
        action["stored_on_disk"] = False
        return [action], None

    if intent == "wake" or turn.get("replaces_stock_welcome"):
        return [{
            "kind": "wake", "command": "wake",
            "transport": "telehealth", "speech": speech,
            "interruptible": True,
        }], None

    # Pure speech (chat, tools, refusals, stock): zero control
    # actions, unless a nested gesture turn must collapse into one.
    nested = turn.get("gesture_turn")
    if isinstance(nested, dict):
        action, error = _validated_behaviour_action(
            nested.get("behaviour"), speech)
        if error:
            return [], error
        return [action], None
    return [], None


def validate_turn_actions(turn):
    """Raise ValueError unless the turn honours one-action-per-turn."""
    if not isinstance(turn, dict):
        raise ValueError("turn must be a dict")
    actions = turn.get("control_actions")
    if actions is None:
        raise ValueError("turn has no control_actions")
    if not isinstance(actions, list):
        raise ValueError("control_actions must be a list")
    if len(actions) > 1:
        raise ValueError("exactly one control action per turn; no queue")
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("control action must be a dict")
        if action.get("kind") not in CONTROL_ACTION_KINDS:
            raise ValueError(
                "unknown control action kind: %r" % (action.get("kind"),))
        if _contains_bytes(action):
            raise ValueError("control action must not retain media payloads")
        if any(_is_media_key(k) for k in action):
            raise ValueError("control action must not carry media keys")
    validate_single_movement(
        [a.get("command") for a in actions if a.get("command")])
    return True


class WiredBridge:
    """A BridgeController with every output wired to control actions.

    Same mode gate as the bridge (STOCK by default, enable() for
    takeover, disable() restores stock and clears sessions). Adds
    per-turn control_actions, the dance opt-in gate, interrupt-wins
    preemption, and a publish-free wake. Never touches the bridge
    transport: nothing is published from here.
    """

    def __init__(self, controller=None, **bridge_kwargs):
        self.controller = controller or BridgeController(**bridge_kwargs)

    @property
    def mode(self):
        return self.controller.mode

    @property
    def robot_state(self):
        return self.controller.robot_state

    def enable(self):
        return self.controller.enable()

    def disable(self):
        return self.controller.disable()

    def set_robot_state(self, state):
        return self.controller.set_robot_state(state)

    def _attach(self, turn, allow_dance=False):
        actions, error = turn_to_control_actions(
            turn, robot_state=self.controller.robot_state,
            allow_dance=allow_dance)
        turn["control_actions"] = actions
        if error:
            turn["error"] = error
        validate_turn_actions(turn)
        return turn

    def interrupt(self):
        return self._attach(self.controller.interrupt())

    def wake(self, device_id="mock-device"):
        """Wake action dict under the bridge MQTT gate. No publish.

        device_id is accepted for signature parity with the bridge;
        it is unused because this layer publishes nothing.
        """
        return self._attach(self.controller.wake_turn())

    def handle_turn(self, transcript, device_id="mock-device",
                    allow_stock_fallback=False, allow_dance=False):
        """Route one transcript to one wired turn (see module contract)."""
        if not isinstance(transcript, str):
            raise ValueError("transcript must be text")
        low = " ".join(transcript.strip().lower().split())
        if self.controller.mode == MODE_BRIDGE:
            if _INTERRUPT_RE.search(low):
                return self.interrupt()
            if ("dance" in low and not allow_dance
                    and "sleep" not in low and "goodnight" not in low):
                self.controller.turns += 1
                turn = {
                    "command": "speak", "intent": "dance",
                    "transport": "telehealth",
                    "speech": phonetic_name(sanitize_speech(
                        "Dance is switched off by default, Stefan. "
                        "The dance module stays gated off until it "
                        "is explicitly enabled for a turn.")),
                    "mood": "concerned", "intensity": 0.5,
                    "interruptible": True,
                    "error": DANCE_GATED_ERROR, "control_actions": [],
                }
                validate_turn_actions(turn)
                return turn
        turn = self.controller.handle_turn(
            transcript, device_id=device_id,
            allow_stock_fallback=allow_stock_fallback)
        return self._attach(turn, allow_dance=allow_dance)
