"""Puppet console: staff-gated, CSRF-protected operator page + API.

Deliberately NOT reusing the stock puppet_api exemption: this module
carries no csrf-exempt decorator anywhere. In deployment, Django's
CsrfViewMiddleware enforces CSRF on every POST here; the stock
puppet_api csrf exemption (views.py) is a separate, pre-existing issue,
flagged in the checkpoint as a follow-up, not expanded or hidden.

Transport: every robot-bound call goes through the SAME internal
MoxieServer methods the stock puppet_api uses (send_telehealth_*,
send_wakeup_to_bot, send_command_to_bot_json with build_sleep_payload),
plus handle_config_updated for puppet mode. No new MQTT topic, no new
robot protocol, no wheel/joint/camera command is invented here.

One-at-a-time: ConsoleActivity is a process-wide lock. A trigger that
arrives while another behaviour is active is REJECTED (400), never
queued. The server receives no behaviour-completion event on the
telehealth path, so completion is operator-confirmed (Mark complete
button) or cleared by Interrupt. Interrupt always wins: it sends the
robot interrupt, clears the active behaviour, and clears any sequence
and show-me session.

Puppet auto-exit on disconnect: the page session records saw_online
once the robot is seen online. A later status check that finds the
robot offline while puppet mode is still set clears moxie_mode
(auto-exit) and reports it. Enabling puppet while offline is not
immediately undone (saw_online was never set in that session), because
the stock flow legitimately saves the config for the robot to pick up
on reconnect; the auto-exit fires on an observed online->offline
transition, which is the disconnect case the task names.

LAN only: this is a LAN server page like the rest of the hive UI; there
is no public exposure added here, and staff login is required.

Session storage only: transcripts, sequence, favourites, recent items,
detector requests and bridge-mode flag live in the operator's Django
session (or a per-request fallback dict in tests). Nothing here writes
audio, video, or transcripts to disk. Vision events live in the
in-memory VisionEventStore (vision_events.py), capped at 100.
"""
import threading
from datetime import datetime, timezone

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from . import logtail
from .models import MoxieDevice
from .mqtt.control_commands import (
    DEFAULT_LOOK_BEHAVIOUR,
    build_sleep_payload,
    parse_duration,
    phonetic_name,
    validate_intensity,
    validate_mood,
)
from .mqtt.creative_modes import build_reminder, build_story_beat, build_wake_greeting
from .mqtt.dance_modules import build_dance_tree_turn
from .mqtt.gesture_conversation import build_gesture_turn, sanitize_speech
from .mqtt.moxie_server import get_instance
from .mqtt.show_me import ShowMeSession
from .puppet_inventory import (
    ABSENT,
    INVENTORY,
    is_celebration_cue,
    is_dance_tree,
    is_gesture_cue,
    is_look,
    is_operator_gesture,
    sequence_step_ids,
)
from .vision_events import (
    DETECTORS,
    VISION_EVENTS,
    VISION_STORE,
    build_detector_payload,
    is_detector,
    parse_vision_input,
)

STAFF_LOGIN = "/admin/login/"
CONSOLE_URL = "/hive/puppet_console/%s"


class ConsoleActivity:
    """One behaviour at a time, process-wide. No queue, no loop."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active = None
        self.last_completed = None
        self.last_interrupted = None

    def start(self, label, kind, item_id, sequence_owned=False):
        with self._lock:
            if self.active is not None:
                raise ValueError(
                    "a behaviour is already active (%s); complete or interrupt it first"
                    % self.active["label"])
            self.active = {"label": label, "kind": kind, "item_id": item_id,
                           "sequence_owned": bool(sequence_owned)}
            return self.active

    def complete(self):
        with self._lock:
            if self.active is None:
                raise ValueError("no behaviour is active")
            self.last_completed = self.active
            self.active = None
            return self.last_completed

    def release(self):
        """Clear without marking completed (used when a send fails)."""
        with self._lock:
            self.active = None

    def interrupt(self):
        with self._lock:
            self.last_interrupted = self.active
            self.active = None
            return self.last_interrupted


ACTIVITY = ConsoleActivity()
_SHOW_ME_SESSIONS = {}


def reset_console_state():
    """Test helper: clear activity, show-me sessions and vision store."""
    ACTIVITY.active = None
    ACTIVITY.last_completed = None
    ACTIVITY.last_interrupted = None
    _SHOW_ME_SESSIONS.clear()
    VISION_STORE.clear()


def _session_dict(request):
    session = getattr(request, "session", None)
    if session is not None:
        return session
    fallback = getattr(request, "_puppet_session", None)
    if fallback is None:
        fallback = {}
        request._puppet_session = fallback
    return fallback


def _session_get(session, key, default):
    try:
        value = session.get(key, default)
    except Exception:
        value = default
    return default if value is None else value


def _session_set(session, key, value):
    try:
        session[key] = value
    except Exception:
        pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _transcript(session):
    return list(_session_get(session, "puppet_transcript", []))


def _transcript_add(session, role, text):
    entries = _transcript(session)
    entries.append({"role": role, "text": text, "time": _now()})
    _session_set(session, "puppet_transcript", entries[-100:])


def _recent_add(session, item_id):
    recent = [i for i in _session_get(session, "puppet_recent", []) if i != item_id]
    recent.insert(0, item_id)
    _session_set(session, "puppet_recent", recent[:10])


def _favourites(session):
    return list(_session_get(session, "puppet_favourites", []))


def _status(device, server, session):
    robot_data = server.robot_data()
    online = False
    puppet_state = None
    try:
        online = bool(robot_data.device_online(device.device_id))
    except Exception:
        online = False
    try:
        puppet_state = robot_data.get_puppet_state(device.device_id)
    except Exception:
        puppet_state = None
    config = device.robot_config or {}
    puppet_enabled = config.get("moxie_mode") == "TELEHEALTH"
    auto_exited = False
    saw_online = bool(_session_get(session, "puppet_saw_online", False))
    if online:
        _session_set(session, "puppet_saw_online", True)
    elif saw_online and puppet_enabled:
        # Observed online -> offline transition: auto-exit puppet mode.
        config.pop("moxie_mode", None)
        device.robot_config = config
        try:
            device.save()
        except Exception:
            pass
        try:
            server.handle_config_updated(device)
        except Exception:
            pass
        puppet_enabled = False
        auto_exited = True
        _session_set(session, "puppet_saw_online", False)
    raw_state = getattr(device, "state", None)
    state = raw_state if isinstance(raw_state, dict) else {}
    battery = state.get("battery_level")
    firmware = (state.get("firmware_version") or state.get("ota_version")
                or state.get("version"))
    if bool(_session_get(session, "puppet_bridge_mode", False)):
        mode = "bridge"
    elif puppet_enabled:
        mode = "puppet"
    else:
        mode = "stock"
    return {
        "online": online,
        "puppet_enabled": puppet_enabled,
        "puppet_state": puppet_state,
        "mode": mode,
        "battery": battery if battery is not None else "unavailable",
        "firmware": firmware if firmware is not None else "unavailable",
        "last_connect": str(getattr(device, "last_connect", "") or ""),
        "last_disconnect": str(getattr(device, "last_disconnect", "") or ""),
        "auto_exited_on_disconnect": auto_exited,
    }


def _log_tail(device_id):
    try:
        files = logtail.collect_logs(count=50, device=device_id)
        out = []
        for filename, payload in files.items():
            out.append({"filename": filename,
                        "lines": [line["text"] for line in payload.get("lines", [])]})
        return out
    except Exception:
        return []


def _public_state(device, server, session):
    status = _status(device, server, session)
    sequence = _session_get(session, "puppet_sequence",
                            {"steps": [], "index": 0, "status": "none"})
    detectors = _session_get(session, "puppet_detectors", {})
    return {
        "status": status,
        "activity": ACTIVITY.active,
        "last_completed": ACTIVITY.last_completed,
        "last_interrupted": ACTIVITY.last_interrupted,
        "transcript": _transcript(session),
        "sequence": sequence,
        "favourites": _favourites(session),
        "recent": _session_get(session, "puppet_recent", []),
        "vision_events": VISION_STORE.recent(device_id=device.device_id),
        "detectors": detectors,
        "logs": _log_tail(device.device_id),
    }


def _send_behaviour(server, device_id, behaviour, kind, speech="", duration=1.0):
    server.send_telehealth_behaviour(device_id, behaviour, speech=speech,
                                     duration=duration)


def _trigger_step(server, device, session, step_id, dance_opt_in=False):
    """Trigger one validated sequence/browser step. Returns (label, kind).

    Raises ValueError for anything not in the inventory. Sends exactly
    one robot call for the step (a story-style gesture+speech pair is a
    single telehealth PLAY_OUTPUT call, not two).
    """
    device_id = device.device_id
    if step_id.startswith("gesture:"):
        tree = step_id[len("gesture:"):]
        if not is_operator_gesture(tree):
            raise ValueError("gesture not allowlisted: %r" % (tree,))
        _send_behaviour(server, device_id, tree, "gesture")
        return tree, "gesture"
    if step_id.startswith("look:"):
        tree = step_id[len("look:"):]
        if not is_look(tree):
            raise ValueError("look not allowlisted: %r" % (tree,))
        _send_behaviour(server, device_id, tree, "look")
        return tree, "look"
    if step_id.startswith("dance:"):
        tree = step_id[len("dance:"):]
        if not dance_opt_in:
            raise ValueError("dance requires a per-turn opt-in for this step")
        turn = build_dance_tree_turn(tree=tree, robot_state="awake")
        _send_behaviour(server, device_id, tree, "dance",
                        speech=turn["speech"], duration=turn["duration"])
        return tree, "dance"
    if step_id.startswith("cue:"):
        cue = step_id[len("cue:"):]
        if not is_gesture_cue(cue):
            raise ValueError("unknown gesture cue: %r" % (cue,))
        if cue == "sleep":
            raise ValueError("the sleep cue is speech only and is not a sequence step")
        turn = build_gesture_turn(cue)
        _send_behaviour(server, device_id, turn["behaviour"], turn["command"],
                        speech=turn["speech"])
        return cue, "gesture_cue"
    if step_id.startswith("celebration:"):
        cue = step_id[len("celebration:"):]
        if not is_celebration_cue(cue):
            raise ValueError("unknown celebration step: %r" % (cue,))
        turn = build_gesture_turn(cue)
        _send_behaviour(server, device_id, turn["behaviour"], turn["command"],
                        speech=turn["speech"])
        return cue, "celebration"
    raise ValueError("unknown sequence step: %r" % (step_id,))


def _bool_field(post, name):
    return str(post.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def _handle_action(request, device, server, session):
    """Shared POST dispatch for the API view and the page form view.

    Returns (payload_dict, http_status). Exactly one action per POST:
    a bundled 'actions' field is rejected, and every branch sends at
    most one robot call (interrupt sends one, sequence_stop sends one).
    """
    post = request.POST
    if "actions" in post:
        return {"result": False, "error": "exactly one action per request"}, 400
    action = (post.get("action") or "").strip()
    device_id = device.device_id
    status = _status(device, server, session)

    def ok(extra=None):
        payload = {"result": True, "action": action}
        if extra:
            payload.update(extra)
        payload.update(_public_state(device, server, session))
        return payload, 200

    def fail(message, code=400):
        return {"result": False, "action": action, "error": message}, code

    if action == "interrupt":
        try:
            server.send_telehealth_interrupt(device_id)
        except Exception as exc:
            return fail("interrupt send failed: %s" % (exc,), 500)
        ACTIVITY.interrupt()
        _session_set(session, "puppet_sequence",
                     {"steps": [], "index": 0, "status": "none"})
        _SHOW_ME_SESSIONS.pop(device_id, None)
        _transcript_add(session, "system", "Interrupt: stopped current behaviour and cleared the sequence.")
        return ok()

    if action == "complete":
        try:
            done = ACTIVITY.complete()
        except ValueError as exc:
            return fail(str(exc))
        sequence = _session_get(session, "puppet_sequence",
                                {"steps": [], "index": 0, "status": "none"})
        if sequence.get("status") == "running" and done.get("sequence_owned"):
            index = sequence["index"] + 1
            sequence = {"steps": sequence["steps"], "index": index,
                        "status": "done" if index >= len(sequence["steps"]) else "running"}
            _session_set(session, "puppet_sequence", sequence)
        _transcript_add(session, "system", "Marked complete: %s" % done["label"])
        return ok()

    if action == "speak":
        speech = (post.get("speech") or "").strip()
        if not speech:
            return fail("speech is required")
        try:
            mood = validate_mood(post.get("mood"))
            intensity = validate_intensity(post.get("intensity"))
        except ValueError as exc:
            return fail(str(exc))
        spoken = phonetic_name(speech)
        try:
            ACTIVITY.start(spoken, "speak", "speak")
        except ValueError as exc:
            return fail(str(exc))
        try:
            server.send_telehealth_speech(device_id, spoken, mood, intensity)
        except Exception as exc:
            ACTIVITY.release()
            return fail("speak send failed: %s" % (exc,), 500)
        _transcript_add(session, "robot", spoken)
        _recent_add(session, "speak")
        return ok({"spoken": spoken})

    if action in ("gesture", "look"):
        behaviour = (post.get("behaviour") or "").strip()
        if action == "gesture" and not is_operator_gesture(behaviour):
            return fail("gesture not allowlisted: %r" % (behaviour,))
        if action == "look":
            behaviour = behaviour or DEFAULT_LOOK_BEHAVIOUR
            if not is_look(behaviour):
                return fail("look not allowlisted: %r" % (behaviour,))
        try:
            duration = parse_duration(post.get("duration"))
        except ValueError as exc:
            return fail(str(exc))
        spoken = phonetic_name(post.get("speech") or "")
        try:
            ACTIVITY.start(behaviour, action, "%s:%s" % (action, behaviour))
        except ValueError as exc:
            return fail(str(exc))
        try:
            _send_behaviour(server, device_id, behaviour, action,
                            speech=spoken, duration=duration)
        except Exception as exc:
            ACTIVITY.release()
            return fail("send failed: %s" % (exc,), 500)
        if spoken:
            _transcript_add(session, "robot", spoken)
        _recent_add(session, "%s:%s" % (action, behaviour))
        return ok({"behaviour": behaviour})

    if action == "dance":
        tree = (post.get("tree") or post.get("behaviour") or "").strip()
        if not is_dance_tree(tree):
            return fail("dance tree not allowlisted: %r" % (tree,))
        if not _bool_field(post, "dance_opt_in"):
            return fail("dance requires a per-turn opt-in (dance_opt_in)")
        if not status["online"]:
            return fail("dance requires the robot to be online/awake")
        try:
            turn = build_dance_tree_turn(tree=tree, duration=post.get("duration"),
                                         speech=post.get("speech") or "",
                                         robot_state="awake")
        except ValueError as exc:
            return fail(str(exc))
        try:
            ACTIVITY.start(tree, "dance", "dance:%s" % tree)
        except ValueError as exc:
            return fail(str(exc))
        try:
            _send_behaviour(server, device_id, tree, "dance",
                            speech=turn["speech"], duration=turn["duration"])
        except Exception as exc:
            ACTIVITY.release()
            return fail("send failed: %s" % (exc,), 500)
        _recent_add(session, "dance:%s" % tree)
        return ok({"dance_tree": tree, "duration": turn["duration"]})

    if action == "wake":
        try:
            sent = server.send_wakeup_to_bot(device_id)
        except Exception as exc:
            return fail("wake send failed: %s" % (exc,), 500)
        if not sent:
            return fail("robot is offline; wake was not sent", 400)
        _transcript_add(session, "system", "Wake sent.")
        return ok()

    if action == "sleep":
        try:
            ACTIVITY.start("sleep", "sleep", "sleep")
        except ValueError as exc:
            return fail(str(exc))
        try:
            server.send_command_to_bot_json(device_id, "remote_chat",
                                            build_sleep_payload())
        except Exception as exc:
            ACTIVITY.release()
            return fail("send failed: %s" % (exc,), 500)
        _transcript_add(session, "system", "Sleep sent.")
        return ok()

    if action == "wake_greeting":
        greeting = build_wake_greeting("Stefan")
        try:
            ACTIVITY.start("wake greeting", "speak", "wake_greeting")
        except ValueError as exc:
            return fail(str(exc))
        try:
            server.send_telehealth_speech(device_id, greeting["speech"],
                                          greeting["mood"], greeting["intensity"])
        except Exception as exc:
            ACTIVITY.release()
            return fail("send failed: %s" % (exc,), 500)
        _transcript_add(session, "robot", greeting["speech"])
        return ok({"spoken": greeting["speech"]})

    if action == "mode":
        value = (post.get("mode") or "").strip().lower()
        if value not in ("stock", "puppet", "bridge"):
            return fail("unknown mode: %r" % (value,))
        was_puppet = bool(status["puppet_enabled"])
        warning = ""
        if value == "puppet":
            config = device.robot_config or {}
            config["moxie_mode"] = "TELEHEALTH"
            device.robot_config = config
            try:
                device.save()
                server.handle_config_updated(device)
            except Exception as exc:
                return fail("mode save failed: %s" % (exc,), 500)
            _session_set(session, "puppet_bridge_mode", False)
            _transcript_add(session, "system", "Puppet mode entered.")
        elif value == "stock":
            if was_puppet:
                warning = ("Leaving puppet mode: Moxie returns to stock behaviour. "
                           "Operator control from this page ends until puppet mode is re-entered.")
            config = device.robot_config or {}
            config.pop("moxie_mode", None)
            device.robot_config = config
            _session_set(session, "puppet_bridge_mode", False)
            try:
                device.save()
                server.handle_config_updated(device)
            except Exception as exc:
                return fail("mode save failed: %s" % (exc,), 500)
            _transcript_add(session, "system", "Stock mode restored.")
        else:  # bridge: session flag only, by design
            warning = ("Bridge mode is recorded in this console session only. "
                       "This page does NOT change live bridge routing, and leaving puppet mode "
                       "ends operator puppet control until puppet mode is re-entered.")
            config = device.robot_config or {}
            config.pop("moxie_mode", None)
            device.robot_config = config
            _session_set(session, "puppet_bridge_mode", True)
            try:
                device.save()
                server.handle_config_updated(device)
            except Exception as exc:
                return fail("mode save failed: %s" % (exc,), 500)
            _transcript_add(session, "system", "Bridge mode flagged for this console session only.")
        return ok({"mode": value, "warning": warning})

    if action == "gesture_cue":
        cue = (post.get("cue") or "").strip().lower()
        if not is_gesture_cue(cue):
            return fail("unknown gesture cue: %r" % (cue,))
        turn = build_gesture_turn(cue)
        try:
            ACTIVITY.start(cue, turn["command"], "cue:%s" % cue)
        except ValueError as exc:
            return fail(str(exc))
        try:
            if turn["command"] in ("gesture", "look"):
                _send_behaviour(server, device_id, turn["behaviour"],
                                turn["command"], speech=turn["speech"])
            else:
                server.send_telehealth_speech(device_id, turn["speech"],
                                              turn["mood"], turn["intensity"])
        except Exception as exc:
            ACTIVITY.release()
            return fail("send failed: %s" % (exc,), 500)
        _transcript_add(session, "robot", turn["speech"])
        _recent_add(session, "cue:%s" % cue)
        return ok({"cue": cue, "behaviour": turn["behaviour"]})

    if action == "celebration_step":
        cue = (post.get("cue") or "").strip().lower()
        if not is_celebration_cue(cue):
            return fail("unknown celebration step: %r" % (cue,))
        turn = build_gesture_turn(cue)
        try:
            ACTIVITY.start(cue, "celebration", "celebration:%s" % cue)
        except ValueError as exc:
            return fail(str(exc))
        try:
            _send_behaviour(server, device_id, turn["behaviour"], turn["command"],
                            speech=turn["speech"])
        except Exception as exc:
            ACTIVITY.release()
            return fail("send failed: %s" % (exc,), 500)
        _transcript_add(session, "robot", turn["speech"])
        _recent_add(session, "celebration:%s" % cue)
        return ok({"cue": cue})

    if action == "story_beat":
        text = (post.get("text") or "").strip()
        if not text:
            return fail("story beat text is required")
        try:
            mood = validate_mood(post.get("mood"))
            intensity = validate_intensity(post.get("intensity"))
            beat = build_story_beat(text, mood=mood, intensity=intensity,
                                    gesture_cue=(post.get("gesture_cue") or None))
        except ValueError as exc:
            return fail(str(exc))
        try:
            ACTIVITY.start("story beat", "story", "story_beat")
        except ValueError as exc:
            return fail(str(exc))
        try:
            nested = beat.get("gesture_turn")
            if nested:
                _send_behaviour(server, device_id, nested["behaviour"],
                                nested["command"], speech=beat["speech"])
            else:
                server.send_telehealth_speech(device_id, beat["speech"],
                                              beat["mood"], beat["intensity"])
        except Exception as exc:
            ACTIVITY.release()
            return fail("send failed: %s" % (exc,), 500)
        _transcript_add(session, "robot", beat["speech"])
        return ok({"spoken": beat["speech"]})

    if action == "reminder":
        text = (post.get("text") or "").strip()
        if not text:
            return fail("reminder text is required")
        try:
            record = build_reminder(text, due_label=post.get("due_label") or "")
        except ValueError as exc:
            return fail(str(exc))
        reminders = list(_session_get(session, "puppet_reminders", []))
        reminders.append(record)
        _session_set(session, "puppet_reminders", reminders)
        return ok({"reminder": record,
                   "note": "Record only: no timer, no delivery, nothing sent to the robot."})

    if action == "show_me_start":
        show_me = _SHOW_ME_SESSIONS.get(device_id) or ShowMeSession()
        try:
            turn = show_me.start()
        except ValueError as exc:
            return fail(str(exc))
        _SHOW_ME_SESSIONS[device_id] = show_me
        try:
            ACTIVITY.start("show-me start", "show_me", "show_me_start")
        except ValueError as exc:
            return fail(str(exc))
        try:
            _send_behaviour(server, device_id, turn["behaviour"], turn["command"],
                            speech=turn["speech"])
        except Exception as exc:
            ACTIVITY.release()
            return fail("send failed: %s" % (exc,), 500)
        _transcript_add(session, "robot", turn["speech"])
        return ok({"show_me_state": show_me.state})

    if action == "show_me_describe":
        show_me = _SHOW_ME_SESSIONS.get(device_id)
        if show_me is None:
            return fail("no show-me session is active; start one first")
        description = sanitize_speech(post.get("description") or "")
        if not description:
            return fail("show-me description is required")
        try:
            result = show_me.receive(description)
        except ValueError as exc:
            return fail(str(exc))
        _transcript_add(session, "operator",
                        "Shown (text description, session only): %s" % description)
        return ok({"show_me_state": show_me.state, "description": result["description"],
                   "stored_on_disk": False})

    if action == "show_me_ack":
        show_me = _SHOW_ME_SESSIONS.get(device_id)
        if show_me is None:
            return fail("no show-me session is active; start one first")
        try:
            turn = show_me.acknowledge()
        except ValueError as exc:
            return fail(str(exc))
        _SHOW_ME_SESSIONS.pop(device_id, None)
        try:
            ACTIVITY.start("show-me acknowledge", "show_me", "show_me_ack")
        except ValueError as exc:
            return fail(str(exc))
        try:
            _send_behaviour(server, device_id, turn["behaviour"], turn["command"],
                            speech=turn["speech"])
        except Exception as exc:
            ACTIVITY.release()
            return fail("send failed: %s" % (exc,), 500)
        _transcript_add(session, "robot", turn["speech"])
        return ok({"show_me_state": "idle"})

    if action == "sequence_set":
        raw_steps = post.getlist("steps") if hasattr(post, "getlist") else []
        if post.get("steps_text"):
            raw_steps = list(raw_steps) + [s.strip() for s in str(post.get("steps_text")).split(",") if s.strip()]
        if not raw_steps and post.get("steps"):
            raw_steps = [s.strip() for s in str(post.get("steps")).split(",") if s.strip()]
        steps = [s.strip() for s in raw_steps if s.strip()]
        if not steps:
            return fail("sequence requires at least one step")
        allowed = sequence_step_ids()
        for step in steps:
            if step not in allowed:
                return fail("sequence step not allowlisted: %r" % (step,))
        dance_opt_in = _bool_field(post, "dance_opt_in")
        if any(s.startswith("dance:") for s in steps) and not dance_opt_in:
            return fail("sequences containing a dance step require dance_opt_in")
        _session_set(session, "puppet_sequence",
                     {"steps": steps, "index": 0, "status": "ready",
                      "dance_opt_in": dance_opt_in})
        _transcript_add(session, "system",
                        "Sequence set: %d step(s). Nothing runs until you click Run step." % len(steps))
        return ok({"steps": steps})

    if action == "sequence_step":
        sequence = _session_get(session, "puppet_sequence",
                                {"steps": [], "index": 0, "status": "none"})
        if sequence.get("status") not in ("ready", "running"):
            return fail("no runnable sequence; set one first")
        if ACTIVITY.active is not None:
            return fail("a behaviour is already active; complete or interrupt it first")
        index = sequence["index"]
        if index >= len(sequence["steps"]):
            return fail("sequence is finished")
        step_id = sequence["steps"][index]
        try:
            ACTIVITY.start(step_id, "sequence", step_id, sequence_owned=True)
        except ValueError as exc:
            return fail(str(exc))
        try:
            label, kind = _trigger_step(server, device, session, step_id,
                                        dance_opt_in=bool(sequence.get("dance_opt_in")))
        except ValueError as exc:
            ACTIVITY.release()
            return fail(str(exc))
        except Exception as exc:
            ACTIVITY.release()
            return fail("send failed: %s" % (exc,), 500)
        sequence = {"steps": sequence["steps"], "index": index, "status": "running",
                    "dance_opt_in": bool(sequence.get("dance_opt_in"))}
        _session_set(session, "puppet_sequence", sequence)
        _recent_add(session, step_id)
        return ok({"step": step_id, "index": index, "label": label, "kind": kind})

    if action == "sequence_stop":
        _session_set(session, "puppet_sequence",
                     {"steps": [], "index": 0, "status": "none"})
        if ACTIVITY.active is not None:
            try:
                server.send_telehealth_interrupt(device_id)
            except Exception:
                pass
            ACTIVITY.interrupt()
        _transcript_add(session, "system", "Sequence stopped and cleared.")
        return ok()

    if action == "favourite":
        item_id = (post.get("item") or "").strip()
        if item_id not in sequence_step_ids():
            return fail("favourite item not allowlisted: %r" % (item_id,))
        favourites = _favourites(session)
        if item_id not in favourites:
            favourites.append(item_id)
        _session_set(session, "puppet_favourites", favourites[:20])
        return ok({"favourites": favourites})

    if action == "transcript_add":
        text = sanitize_speech(post.get("text") or "")
        if not text:
            return fail("transcript text is required")
        _transcript_add(session, "stt", text)
        return ok({"note": "Session-only transcript note. Live STT is not forwarded to the web layer in stock code; this entry is operator-entered."})

    if action == "clear_transcript":
        _session_set(session, "puppet_transcript", [])
        return ok({"note": "Session transcript cleared. Nothing was written to disk."})

    if action == "vision_event":
        raw = (post.get("event") or "").strip()
        parsed = parse_vision_input(raw)
        if parsed is None:
            return fail("unknown vision event: %r" % (raw,))
        value = (post.get("value") or "").strip() or None
        entry = VISION_STORE.record(raw, value=value, source="console-ingest",
                                    device_id=device_id)
        return ok({"vision_event": entry,
                   "note": "Event, not an image. Live robot event forwarding to the web layer is not yet built; this entry was ingested through the console."})

    if action == "detector":
        detector_id = (post.get("detector") or "").strip()
        if not is_detector(detector_id):
            return fail("unknown detector: %r" % (detector_id,))
        enabled = _bool_field(post, "enabled")
        try:
            payload = build_detector_payload(detector_id, enabled)
        except ValueError as exc:
            return fail(str(exc))
        detectors = dict(_session_get(session, "puppet_detectors", {}))
        detectors[detector_id] = enabled
        _session_set(session, "puppet_detectors", detectors)
        function_id, events, plain, _ = DETECTORS[detector_id]
        return ok({"detector": detector_id, "name": plain, "enabled": enabled,
                   "function_id": function_id, "subscribed_events": list(events),
                   "payload": payload,
                   "live_delivery": "unverified",
                   "note": ("Payload built (execute action + event subscription). "
                            "No robot send is claimed: detector delivery via the remote_chat "
                            "response path is not yet verified live. Detections surface as "
                            "events in the Vision Events panel, never as speech input.")})

    return fail("unknown action: %r" % (action,))


@staff_member_required(login_url=STAFF_LOGIN)
@require_http_methods(["GET", "POST"])
def puppet_console_page(request, pk):
    try:
        device = MoxieDevice.objects.get(pk=pk)
    except Exception:
        return HttpResponseBadRequest()
    server = get_instance()
    session = _session_dict(request)
    if request.method == "POST":
        payload, _code = _handle_action(request, device, server, session)
        return HttpResponseRedirect(CONSOLE_URL % pk)
    state = _public_state(device, server, session)
    context = {
        "object": device,
        "device": device,
        "inventory": INVENTORY,
        "absent": ABSENT,
        "vision_events_info": VISION_EVENTS,
        "detectors_info": DETECTORS,
        "reminders": _session_get(session, "puppet_reminders", []),
        **state,
    }
    return render(request, "hive/puppet_console.html", context)


@staff_member_required(login_url=STAFF_LOGIN)
@require_http_methods(["GET", "POST"])
def puppet_console_api(request, pk):
    try:
        device = MoxieDevice.objects.get(pk=pk)
    except Exception:
        return HttpResponseBadRequest()
    server = get_instance()
    session = _session_dict(request)
    if request.method == "GET":
        payload = {"result": True}
        payload.update(_public_state(device, server, session))
        return JsonResponse(payload)
    payload, code = _handle_action(request, device, server, session)
    return JsonResponse(payload, status=code)
