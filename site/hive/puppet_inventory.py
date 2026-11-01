"""Verified puppet-console inventory: every UI control must map to an entry here.

Sources read before coding (no guessing):
- hive.mqtt.control_commands: CONTROL_COMMANDS, GESTURE_BEHAVIOURS
  (BehaviourTree rows with Default=True from doc/AssetBundleMasterManifest.csv),
  LOOK_BEHAVIOURS, PUPPET_MOODS, phonetic_name, parse_duration,
  build_dance_payload (remote_chat DANCE launch, DISABLED by
  ALLOW_DANCE_MODULE=False), build_sleep_payload.
- hive.mqtt.dance_modules: DANCE_TREES (bht_dance_* + bht_celebration_dance_*),
  clamp_dance_duration 10-60s, DanceSession one-at-a-time.
- hive.mqtt.gesture_conversation: GESTURE_TURNS cues.
- hive.mqtt.creative_modes: wake greeting, story beats, celebration routine.
- hive.mqtt.show_me: Phase A only, no storage.
- hive.mqtt.mycroft_bridge: mode gate STOCK/BRIDGE (in-memory controller only).

Stricter-than-stock note: the stock puppet_api accepts ANY manifest
Default=True tree, including AUTO_GESTURE_* (speech-system auto gestures)
and Bht_Motor_Test_* diagnostic trees. The console one-click browser
deliberately does NOT offer those two groups: AUTO_* are not operator
gestures, and motor-test trees read like joint/head-angle controls, which
do not exist as a console control. They remain listed in
MANIFEST_ONLY_NOT_OFFERED below so the inventory is complete and honest,
but no console button maps to them. This narrows, never widens, stock.
"""

from .mqtt.control_commands import (
    CONTROL_COMMANDS,
    DANCE_MODULE_ID,
    DEFAULT_LOOK_BEHAVIOUR,
    GESTURE_BEHAVIOURS,
    LOOK_BEHAVIOURS,
    PUPPET_MOODS,
    is_dance_enabled,
)
from .mqtt.creative_modes import CELEBRATION_ROUTINE, build_wake_greeting
from .mqtt.dance_modules import (
    DANCE_DEFAULT_SECONDS,
    DANCE_MAX_SECONDS,
    DANCE_MIN_SECONDS,
    DANCE_TREES,
    DEFAULT_DANCE_TREE,
)
from .mqtt.gesture_conversation import GESTURE_TURNS


def plain_name(tree):
    """Turn a behaviour-tree id into readable words, without inventing meaning.

    Examples: bht_arms_out -> 'arms out'; Gesture_Nod -> 'nod';
    bht_dance_cruncher_short_120bpm -> 'dance cruncher short 120bpm'.
    """
    if not isinstance(tree, str):
        return ""
    name = tree
    for prefix in ("bht_", "Bht_", "Gesture_", "AUTO_GESTURE_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.replace("_", " ").strip()


def _is_auto(tree):
    return tree.startswith("AUTO_GESTURE_")


def _is_motor_test(tree):
    return tree.lower().startswith("bht_motor_test")


# Operator-offered gestures: manifest trees minus dance, look, auto, motor-test.
OPERATOR_GESTURES = frozenset(
    b for b in GESTURE_BEHAVIOURS
    if b not in DANCE_TREES and b not in LOOK_BEHAVIOURS
    and not _is_auto(b) and not _is_motor_test(b)
)
MANIFEST_ONLY_NOT_OFFERED = frozenset(
    b for b in GESTURE_BEHAVIOURS
    if _is_auto(b) or _is_motor_test(b)
)


def _item(item_id, kind, name, description, command, params, constraints=""):
    return {
        "id": item_id,
        "kind": kind,
        "name": name,
        "description": description,
        "command": command,
        "params": params,
        "constraints": constraints,
    }


def build_inventory():
    """Return the full inventory dict, computed from the real modules."""
    controls = [
        _item("speak", "control", "Speak",
              "Moxie speaks operator text aloud. 'Mycroft' is rendered as 'my croft' at the output boundary.",
              "speak", "speech (required), mood, intensity 0.0-1.0",
              "One command per POST. Interrupt always wins."),
        _item("interrupt", "control", "Interrupt / stop",
              "Stops Moxie's current speech/behaviour immediately and clears any active behaviour and sequence.",
              "interrupt", "none", "Always visible. Always wins over any other control."),
        _item("wake", "control", "Wake",
              "Sends the wake message to an online robot. Cannot wake from full standby-disconnected state.",
              "wake", "none",
              "Refused by the server when the robot is offline; only local voice/touch/power or a pre-set alarm can wake from standby."),
        _item("sleep", "control", "Sleep",
              "Sends the remote_chat sleep payload to the robot.",
              "sleep", "none", "One command per POST."),
        _item("puppet_enable", "control", "Enter puppet mode",
              "Sets robot_config moxie_mode to TELEHEALTH so Moxie only produces outputs from the operator page.",
              "enable", "none", "Staff only. Auto-exits on disconnect (see checkpoint)."),
        _item("puppet_disable", "control", "Leave puppet mode",
              "Removes moxie_mode from robot_config, restoring stock behaviour.",
              "disable", "none", "Warning shown when leaving puppet mode."),
        _item("wake_greeting", "control", "Wake greeting",
              "First line after wake, in my croft identity for Stefan, replacing the stock Moxie welcome.",
              "speak", "speech fixed by build_wake_greeting, mood happy, intensity 0.5",
              "Operator-initiated only; nothing fires it automatically."),
    ]
    gestures = [
        _item(b, "gesture", plain_name(b) or b,
              "Behaviour-tree gesture from the asset manifest (Default=True).",
              "gesture", "behaviour (allowlisted), optional speech, duration 0.1-30.0s (default 1.0s)",
              "One behaviour at a time; operator confirms completion or interrupts.")
        for b in sorted(OPERATOR_GESTURES)
    ]
    looks = [
        _item(b, "look", plain_name(b) or b,
              "Face-search / look behaviour tree. No head-angle or camera control is involved.",
              "look", "behaviour (allowlisted), optional speech, duration 0.1-30.0s",
              "Default look: %s. One behaviour at a time." % DEFAULT_LOOK_BEHAVIOUR)
        for b in sorted(LOOK_BEHAVIOURS)
    ]
    dances = [
        _item(b, "dance", plain_name(b) or b,
              "Dance behaviour tree, played via the behaviour-tree dance path (not the disabled remote_chat DANCE launch).",
              "dance", "tree (allowlisted), duration clamped 10-60s (default 30s), optional speech, per-turn opt-in",
              "Dance refused by default: per-turn opt-in only, awake/online only, one at a time, 10-60s clamp, interrupt releases it. Live-unproven; dance last in any live test.")
        for b in sorted(DANCE_TREES)
    ]
    cues = []
    for cue, (behaviour, speech, mood, intensity) in sorted(GESTURE_TURNS.items()):
        if cue == "sleep":
            desc = "Speech-led cue only: Moxie says goodnight. This is NOT the sleep command."
        elif behaviour is None:
            desc = "Speech-led cue with the default attentive look behaviour."
        else:
            desc = "Gesture cue: one allowlisted behaviour tree plus speech."
        cues.append(_item("cue:%s" % cue, "gesture_cue", cue,
                          desc, "gesture_cue",
                          "cue (allowlisted), speech=%r, mood=%s, intensity=%s" % (speech, mood, intensity),
                          "One cue per turn. Dance cues are refused here and must go through the dance gate."))
    celebrations = [
        _item("celebration:%s" % cue, "celebration", "Celebration step: %s" % cue,
              "One step of the celebration routine, stepped manually. No auto-advance, no loop.",
              "celebration_step", "cue (one of: %s)" % ", ".join(CELEBRATION_ROUTINE),
              "Stepped manually, one step per operator click; interrupt between steps.")
        for cue in CELEBRATION_ROUTINE
    ]
    story = [
        _item("story_beat", "story", "Storytelling beat",
              "One storytelling beat: operator-supplied text spoken aloud, optionally with a gesture cue.",
              "story_beat", "text (required), mood, intensity 0.0-1.0, optional gesture_cue",
              "One beat per click. No beat auto-advances."),
    ]
    show_me = [
        _item("show_me_start", "show_me", "Show-me: start",
              "Moxie does a look behaviour and asks to be shown something. Phase A only.",
              "show_me_start", "none",
              "One session at a time. No image, video, audio or transcript is stored on disk."),
        _item("show_me_describe", "show_me", "Show-me: describe what was shown",
              "Operator (or a future STT path) supplies a text description, e.g. 'a red book'. The description is session-only text.",
              "show_me_describe", "description (required, text only)",
              "No image is captured or stored; this is a text description, not object recognition."),
        _item("show_me_ack", "show_me", "Show-me: acknowledge",
              "Moxie acknowledges the description with speech and a celebrate gesture, then the session ends.",
              "show_me_ack", "none", "Session ends after acknowledgement."),
    ]
    creative = [
        _item("reminder_record", "creative", "Reminder (record only)",
              "A reminder record (text + due label). No timer, no publish, no autonomous delivery.",
              "reminder", "text (required), due_label",
              "Delivery is a later, separately approved step; this control delivers nothing."),
    ]
    return {
        "controls": controls,
        "gestures": gestures,
        "looks": looks,
        "dances": dances,
        "gesture_cues": cues,
        "celebrations": celebrations,
        "story": story,
        "show_me": show_me,
        "creative": creative,
        "manifest_only_not_offered": [
            _item(b, "manifest_only", plain_name(b) or b,
                  "In the manifest allowlist (so stock puppet_api would accept it) but NOT offered as a one-click console control.",
                  "none", "none",
                  "AUTO_GESTURE_* are speech-system auto gestures; Bht_Motor_Test_* are diagnostic trees. Excluded so the console never implies joint/head-angle control.")
            for b in sorted(MANIFEST_ONLY_NOT_OFFERED)
        ],
        "counts": {
            "controls": len(controls),
            "gestures": len(gestures),
            "looks": len(looks),
            "dances": len(dances),
            "gesture_cues": len(cues),
            "celebrations": len(celebrations),
            "story": len(story),
            "show_me": len(show_me),
            "creative": len(creative),
            "manifest_only_not_offered": len(MANIFEST_ONLY_NOT_OFFERED),
            "manifest_total": len(GESTURE_BEHAVIOURS),
        },
        "meta": {
            "control_commands": list(CONTROL_COMMANDS),
            "moods": sorted(PUPPET_MOODS),
            "default_look": DEFAULT_LOOK_BEHAVIOUR,
            "default_dance": DEFAULT_DANCE_TREE,
            "dance_min_seconds": DANCE_MIN_SECONDS,
            "dance_max_seconds": DANCE_MAX_SECONDS,
            "dance_default_seconds": DANCE_DEFAULT_SECONDS,
            "dance_module_id": DANCE_MODULE_ID,
            "dance_module_launch_enabled": is_dance_enabled(),
            "wake_greeting": build_wake_greeting("Stefan"),
        },
    }


INVENTORY = build_inventory()


def is_operator_gesture(behaviour):
    return isinstance(behaviour, str) and behaviour in OPERATOR_GESTURES


def is_look(behaviour):
    return isinstance(behaviour, str) and behaviour in LOOK_BEHAVIOURS


def is_dance_tree(tree):
    return isinstance(tree, str) and tree in DANCE_TREES


def is_gesture_cue(cue):
    return isinstance(cue, str) and cue.strip().lower() in GESTURE_TURNS


def is_celebration_cue(cue):
    return isinstance(cue, str) and cue.strip().lower() in CELEBRATION_ROUTINE


def sequence_step_ids():
    """All ids a sequence step may reference (no story text steps, no sleep/wake)."""
    ids = ["gesture:%s" % b for b in sorted(OPERATOR_GESTURES)]
    ids += ["look:%s" % b for b in sorted(LOOK_BEHAVIOURS)]
    ids += ["dance:%s" % b for b in sorted(DANCE_TREES)]
    ids += ["cue:%s" % cue for cue in sorted(GESTURE_TURNS) if cue != "sleep"]
    ids += ["celebration:%s" % cue for cue in CELEBRATION_ROUTINE]
    return frozenset(ids)


ABSENT = [
    ("Camera feed / live video", "No image or video transport exists in the OpenMoxie server code (only ZMQ audio for STT). A live feed would need a new robot-side image transport. Nothing on this page shows a camera picture."),
    ("Camera snapshot / photo capture", "No snapshot command exists in the server code. The show-me flow records only operator-supplied text descriptions."),
    ("Face identity / face recognition", "Face-search behaviour trees can look for a face, but no identity or recognition result is exposed to the web layer."),
    ("Joint / head-angle control", "No joint or head-angle command exists. Look and gesture controls play named behaviour trees only; motor-test trees are not offered as controls."),
    ("Wheel driving / locomotion", "No driving command exists in this codebase. Moxie cannot be driven from this page."),
    ("Object description / object recognition", "No object-description result is exposed. Show-me descriptions are text the operator supplies, not a robot vision result (vision detection events are a separate panel, event-only)."),
    ("Audio / video storage", "No audio or video is stored by this console. Transcripts here are session-only text, cleared with the clear button, never written to disk by default."),
    ("Autonomous running", "Puppet mode is operator-controlled, not autonomous. Nothing runs without an operator click; sequences never auto-advance and never loop."),
    ("Live vision-event forwarding", "Robot vision events (face/book/aruco/QR) are documented in doc/RemoteModuleAPI.md, but no server code forwards them to the web layer today (verified: no eb-* handling in site/). The Vision Events panel therefore shows only events ingested through its own plumbing until that forwarding is built and verified live."),
]
