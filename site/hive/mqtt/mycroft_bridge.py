"""Full Mycroft bridge: Moxie <-> Mycroft, pure shaping + session, no I/O.

Architecture (text diagram):

    Moxie (STT transcript, wake/sleep state)
        |
        v
    BridgeController  -- mode gate: STOCK (default) / BRIDGE
        |  (bridge mode ON: every turn is routed here, never to stock
        |   GPT by default; stock GPT is fallback ONLY when the caller
        |   explicitly asks for fallback, see handle_turn fallback flag)
        +-- wake()            -> my croft greeting, MQTT-connected only
        +-- handle_turn()     -> intent router (below)
        +-- interrupt()       -> clears every sub-session, wins always
        +-- sleep()           -> sleep turn, state -> sleeping
        |
        +-- gesture_conversation (one gesture turn, allowlisted tree)
        +-- dance_modules (one dance, awake-only, 10-60s, no queue)
        +-- creative_modes (wake greeting, story beat, celebration steps)
        +-- show_me (session protocol, NO image/video/audio/disk storage)
        +-- ToolRouter (READ-ONLY tools only, see below)
        +-- MycroftAgent (injected adapter; fake in tests, real adapter
        |   is a daylight wiring step -- this module never calls the
        |   network, never reads a key, never touches ~/OpenMoxie)
        |
        v
    Robot-bound turn dict (one movement max) -> caller publishes it via
    existing MoxieServer methods (telehealth / remote_chat / wakeup).
    This module itself publishes NOTHING. Transport is injected only as
    a recording mock in tests (MockTransport below).

Intent routing, in order (first match wins, nothing falls through to a
movement by accident):
    1. interrupt / stop            -> interrupt, clears all sessions
    2. sleep / goodnight           -> sleep turn
    3. dance cue                   -> dance_modules (awake-only)
    4. gesture cue                 -> gesture_conversation
    5. show me                     -> ShowMeSession (start/receive/ack)
    6. story / tell me a story     -> one creative story beat
    7. celebrate                   -> celebration routine, one step at a time
    8. read-only tool intent       -> ToolRouter (calendar/weather/timer record)
    9. action intent (mail, create event, lights, house, drive, camera
       record)                     -> REFUSED with a spoken explanation,
                                      never executed, never queued
   10. default chat                -> injected MycroftAgent answer; if no
                                      agent is connected, an explicit
                                      "bridge agent not connected" turn --
                                      NEVER a silent stock-GPT turn.

Safety, enforced here, not left to callers:
    - Bridge mode is explicit: default STOCK, enable() required, disable()
      restores stock exactly (mode + cleared sessions).
    - Wake only when robot_state is online-awake or online-sleeping.
      OFFLINE_STANDBY (MQTT disconnected) -> wake refused; only local
      voice/touch/power or a pre-set alarm timer can wake from standby
      (proven live Oct 28, 2026). No retained-message trick is claimed.
    - One movement per turn. No autonomous loops, no proactive speech:
      proactive_turn() requires an explicit Stefan-set schedule record;
      without it, it raises. Even with it, it returns a turn dict, it
      does not publish.
    - Speech is sanitised (angle-bracket tags stripped, 500 chars) and
      phonetic-rendered ("Mycroft" -> "my croft") at this boundary.
    - Identity: user is Stefan, never the config child nickname "Pat",
      never stock Moxie welcome. Wake greeting replaces stock welcome.
    - No wheel driving, no camera recording, no house control: no such
      API exists in this codebase and none is invented here.
"""

from .control_commands import phonetic_name, validate_mood
from .creative_modes import (
    build_celebration_routine,
    build_story_beat,
    build_wake_greeting,
)
from .dance_modules import DanceSession
from .gesture_conversation import (
    GestureConversation,
    build_gesture_turn,
    sanitize_speech,
)
from .show_me import ShowMeSession

MODE_STOCK = "stock"
MODE_BRIDGE = "bridge"

STATE_AWAKE = "awake"
STATE_SLEEPING = "sleeping"
STATE_STANDBY = "standby"  # MQTT disconnected; server cannot wake it
STATE_UNKNOWN = "unknown"

# Read-only tools only. Anything that writes, moves, or controls the
# house is deliberately absent: registering it here would be the bug.
READONLY_TOOLS = ("calendar", "weather", "timer_record", "show_me_status")

# Phrases that must never be executed as actions by the bridge.
ACTION_INTENTS = (
    "send an email", "send email", "send a message", "create an event",
    "book ", "turn on the lights", "turn off the lights", "lights on",
    "lights off", "drive", "go to ", "move to ", "record video",
    "record a video", "take a photo", "take a picture", "call ",
)

GESTURE_CUES = ("wave", "hello", "arms out", "look", "search")


def _clean(text):
    if not isinstance(text, str):
        raise ValueError("transcript must be text")
    return " ".join(text.strip().lower().split())


def _speech(text, mood="happy", intensity=0.5):
    validate_mood(mood)
    if not 0.0 <= float(intensity) <= 1.0:
        raise ValueError("intensity must be between 0.0 and 1.0")
    return {
        "command": "speak",
        "speech": phonetic_name(sanitize_speech(text)),
        "mood": mood,
        "intensity": float(intensity),
        "interruptible": True,
    }


class AgentReply:
    """What a Mycroft agent returns for one turn. No I/O here."""

    def __init__(self, speech, mood="happy", intensity=0.5, gesture_cue=None):
        self.speech = speech
        self.mood = mood
        self.intensity = intensity
        self.gesture_cue = gesture_cue


class MycroftAgent:
    """Adapter interface. Real adapter = daylight wiring step."""

    def answer(self, transcript, context):
        raise NotImplementedError("no Mycroft agent connected")


class FakeMycroftAgent(MycroftAgent):
    """Deterministic stand-in for tests and offline replay only."""

    def answer(self, transcript, context):
        low = _clean(transcript)
        if "day" in low or "tomorrow" in low:
            return AgentReply("Tomorrow is a quiet day, Stefan.", "happy", 0.5)
        return AgentReply(
            "Hello, Stefan. This is my croft, listening through Moxie.",
            "happy", 0.5, gesture_cue="look",
        )


class ToolRouter:
    """Read-only tools, injected as callables. Unknown tool -> refused."""

    def __init__(self, tools=None):
        self.tools = dict(tools or {})

    def call(self, name, arg=""):
        if name not in READONLY_TOOLS:
            raise ValueError("tool not allowlisted (read-only only): %r" % (name,))
        fn = self.tools.get(name)
        if fn is None:
            return "I do not have a live %s reading connected yet, Stefan." % name
        return fn(arg)


class MockTransport:
    """Recording stand-in for MoxieServer methods. No network, no MQTT."""

    def __init__(self):
        self.calls = []

    def send_wakeup(self, device_id):
        self.calls.append(("wakeup", device_id))
        return True

    def send_interrupt(self, device_id):
        self.calls.append(("interrupt", device_id))
        return True

    def send_turn(self, device_id, turn):
        self.calls.append(("turn", device_id, turn))
        return True


class BridgeController:
    """The bridge. One instance per robot session. No I/O of its own."""

    def __init__(self, user_name="Stefan", agent=None, tools=None,
                 transport=None):
        if not isinstance(user_name, str) or not user_name.strip():
            raise ValueError("bridge requires a user name")
        self.user_name = user_name.strip()
        self.mode = MODE_STOCK
        self.robot_state = STATE_UNKNOWN
        self.agent = agent
        self.tools = ToolRouter(tools)
        self.transport = transport or MockTransport()
        self.gestures = GestureConversation()
        self.dance = DanceSession()
        self.show_me = ShowMeSession()
        self.turns = 0
        self.celebration_index = 0
        self._celebration = None

    # -- mode ---------------------------------------------------------
    def enable(self):
        """Full takeover on: wake greeting becomes my croft, chat routes here."""
        self.mode = MODE_BRIDGE
        return {"mode": MODE_BRIDGE, "stock_restored": False}

    def disable(self):
        """Restore stock Moxie exactly: mode back, all sessions cleared."""
        self.mode = MODE_STOCK
        self.interrupt()
        return {"mode": MODE_STOCK, "stock_restored": True}

    def set_robot_state(self, state):
        if state not in (STATE_AWAKE, STATE_SLEEPING, STATE_STANDBY, STATE_UNKNOWN):
            raise ValueError("unknown robot state: %r" % (state,))
        self.robot_state = state
        return self.robot_state

    # -- wake / sleep / interrupt ------------------------------------
    def _check_wake_allowed(self):
        if self.mode != MODE_BRIDGE:
            raise ValueError("bridge mode is off; wake is stock Moxie behaviour")
        if self.robot_state in (STATE_STANDBY, STATE_UNKNOWN):
            raise ValueError(
                "cannot wake from standby-disconnected; use local voice, "
                "touch, power, or a pre-set alarm timer"
            )

    def wake(self, device_id="mock-device"):
        self._check_wake_allowed()
        self.transport.send_wakeup(device_id)
        self.robot_state = STATE_AWAKE
        greeting = build_wake_greeting(self.user_name)
        greeting["transport"] = "telehealth"
        greeting["intent"] = "wake"
        return greeting

    def wake_turn(self):
        """Publish-free wake turn for wiring callers.

        Same MQTT-gated checks as wake(), but never touches the
        transport: it returns the greeting turn and the caller (the
        wiring layer) turns it into a control action dict. Nothing
        is published from here.
        """
        self._check_wake_allowed()
        self.robot_state = STATE_AWAKE
        greeting = build_wake_greeting(self.user_name)
        greeting["transport"] = "telehealth"
        greeting["intent"] = "wake"
        return greeting

    def sleep(self):
        self.robot_state = STATE_SLEEPING
        self.interrupt_sessions()
        return {"command": "sleep", "transport": "remote_chat",
                "intent": "sleep",
                "speech": "Goodnight, Stefan. I will rest now.",
                "interruptible": True}

    def interrupt(self):
        self.interrupt_sessions()
        return {"command": "interrupt", "intent": "interrupt",
                "interruptible": True}

    def control_actions_for(self, turn, allow_dance=False):
        """Translate one bridge turn via the wiring layer (no publish)."""
        from .bridge_control_wiring import turn_to_control_actions
        return turn_to_control_actions(
            turn, robot_state=self.robot_state, allow_dance=allow_dance)

    def interrupt_sessions(self):
        self.gestures.interrupt()
        self.dance.interrupt()
        self.show_me.cancel()
        self.celebration_index = 0
        self._celebration = None

    # -- proactive (gated, never autonomous) --------------------------
    def proactive_turn(self, text, scheduled=False):
        if not scheduled:
            raise ValueError(
                "proactive speech requires an explicit Stefan-set schedule; "
                "no autonomous proactive speech"
            )
        return _speech(text, "happy", 0.5)

    # -- main turn routing --------------------------------------------
    def handle_turn(self, transcript, device_id="mock-device",
                    allow_stock_fallback=False):
        """Route one STT transcript to exactly one robot-bound turn."""
        if self.mode != MODE_BRIDGE:
            return {"command": "stock", "mode": MODE_STOCK, "intent": "stock",
                    "note": "bridge off; stock Moxie handles this turn"}
        low = _clean(transcript)
        self.turns += 1

        if low in ("stop", "interrupt", "cancel") or low.startswith("stop "):
            return self.interrupt()
        if "sleep" in low or "goodnight" in low:
            return self.sleep()
        if "dance" in low:
            if self.robot_state != STATE_AWAKE:
                raise ValueError("dance requires robot_state='awake'")
            turn = self.dance.start(robot_state=self.robot_state)
            turn["transport"] = "telehealth"
            turn["intent"] = "dance"
            return turn
        for cue in GESTURE_CUES:
            if cue in low:
                turn = build_gesture_turn(cue)
                turn["transport"] = "telehealth"
                turn["intent"] = "gesture"
                return turn
        if "celebrate" in low:
            if self._celebration is None:
                self._celebration = build_celebration_routine()
                self.celebration_index = 0
            if self.celebration_index >= len(self._celebration):
                self._celebration = None
                self.celebration_index = 0
                done = _speech("Celebration complete, Stefan.", "happy", 0.5)
                done["intent"] = "celebrate"
                return done
            turn = self._celebration[self.celebration_index]
            self.celebration_index += 1
            turn["transport"] = "telehealth"
            turn["intent"] = "celebrate"
            return turn
        if low.startswith("show me"):
            turn = self.show_me.start()
            turn["transport"] = "telehealth"
            turn["intent"] = "show_me"
            return turn
        if self.show_me.state == "awaiting_show":
            received = self.show_me.receive(transcript.strip())
            ack = self.show_me.acknowledge()
            ack["transport"] = "telehealth"
            ack["intent"] = "show_me"
            ack["shown_description"] = received["description"]
            ack["stored_on_disk"] = False
            return ack
        if "tell me a story" in low or low.startswith("story"):
            beat = build_story_beat(
                "Once, a small robot woke up and listened, Stefan.", "happy", 0.5
            )
            beat["transport"] = "telehealth"
            beat["intent"] = "story"
            return beat
        if "calendar" in low or "my day" in low or "tomorrow" in low:
            answer = self.tools.call("calendar")
            turn = _speech(answer, "happy", 0.5)
            turn["transport"] = "telehealth"
            turn["intent"] = "tool"
            turn["tool"] = "calendar"
            return turn
        if "weather" in low:
            answer = self.tools.call("weather")
            turn = _speech(answer, "happy", 0.5)
            turn["transport"] = "telehealth"
            turn["intent"] = "tool"
            turn["tool"] = "weather"
            return turn
        for intent in ACTION_INTENTS:
            if intent in low:
                refused = _speech(
                    "I will not do that from Moxie without your explicit "
                    "confirmation, Stefan. That action is not in the "
                    "read-only bridge.", "concerned", 0.5,
                )
                refused["intent"] = "refusal"
                return refused
        if self.agent is None:
            if allow_stock_fallback:
                return {"command": "stock_fallback", "intent": "stock",
                        "note": "explicit fallback requested; stock GPT only"}
            absent = _speech(
                "The my croft bridge is on, but no Mycroft agent is "
                "connected yet, Stefan. I am not falling back to stock "
                "Moxie silently.", "concerned", 0.5,
            )
            absent["intent"] = "chat"
            return absent
        reply = self.agent.answer(transcript, {"user": self.user_name,
                                               "turns": self.turns})
        turn = _speech(reply.speech, reply.mood, reply.intensity)
        if reply.gesture_cue:
            turn["gesture_turn"] = build_gesture_turn(reply.gesture_cue)
        turn["transport"] = "telehealth"
        turn["intent"] = "chat"
        return turn
