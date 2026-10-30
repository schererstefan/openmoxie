"""Tests for the Mycroft bridge -> Puppet control wiring layer.

Mocked/offline only: no MQTT, no robot, no network, no disk storage.
The wiring layer is a pure translation from BridgeController turns to
control-action dicts in the existing Puppet control shapes
(hive.mqtt.control_commands). It publishes NOTHING: every test here
asserts the bridge MockTransport stays empty on the wired path.

Run from site/: ../.venv-stageb/bin/python -m unittest hive.test_bridge_wiring
"""
import os
import tempfile
import unittest

from hive.mqtt import control_commands
from hive.mqtt.bridge_control_wiring import (
    CONTROL_ACTION_KINDS,
    WiredBridge,
    gesture_action_for_cue,
    turn_to_control_actions,
    validate_turn_actions,
)
from hive.mqtt.mycroft_bridge import (
    MODE_BRIDGE,
    MODE_STOCK,
    BridgeController,
    FakeMycroftAgent,
)
from hive.mqtt.show_me import ShowMeSession


def _wired(**kw):
    kw.setdefault("agent", FakeMycroftAgent())
    kw.setdefault("tools", {
        "calendar": lambda arg: "Tomorrow: no meetings, Stefan.",
        "weather": lambda arg: "Sunny in Studio City, Stefan.",
    })
    w = WiredBridge(BridgeController(**kw))
    w.enable()
    w.set_robot_state("awake")
    return w


def _actions(turn):
    validate_turn_actions(turn)
    return turn["control_actions"]


class OneActionPerTurnTests(unittest.TestCase):
    """Rule 1: exactly one control action per action-selecting turn."""

    def test_gesture_turn_one_action(self):
        w = _wired()
        turn = w.handle_turn("arms out")
        actions = _actions(turn)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "gesture")
        self.assertEqual(actions[0]["behaviour"], "bht_arms_out")
        self.assertEqual(actions[0]["markup"].count("<mark"), 1)

    def test_look_turn_one_action(self):
        w = _wired()
        turn = w.handle_turn("look")
        actions = _actions(turn)
        self.assertEqual(len(actions), 1)
        self.assertIn(actions[0]["kind"], ("look", "gesture"))

    def test_dance_opted_in_one_action(self):
        w = _wired()
        turn = w.handle_turn("dance", allow_dance=True)
        actions = _actions(turn)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "dance")
        self.assertEqual(actions[0]["behaviour"],
                         "bht_dance_cruncher_short_120bpm")

    def test_show_me_start_and_ack_one_action_each(self):
        w = _wired()
        start = w.handle_turn("show me")
        self.assertEqual(len(_actions(start)), 1)
        self.assertEqual(start["control_actions"][0]["kind"], "show_me")
        self.assertEqual(start["control_actions"][0]["phase"], "awaiting")
        ack = w.handle_turn("a red book")
        self.assertEqual(len(_actions(ack)), 1)
        self.assertEqual(ack["control_actions"][0]["kind"], "show_me")
        self.assertEqual(ack["control_actions"][0]["phase"], "described")

    def test_celebrate_turn_one_action(self):
        w = _wired()
        turn = w.handle_turn("celebrate")
        actions = _actions(turn)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "celebrate")

    def test_story_turn_one_action(self):
        w = _wired()
        turn = w.handle_turn("tell me a story")
        actions = _actions(turn)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "story")

    def test_sleep_turn_one_action(self):
        w = _wired()
        turn = w.handle_turn("goodnight")
        actions = _actions(turn)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "sleep")
        self.assertEqual(actions[0]["payload"]["command"], "remote_chat")
        self.assertEqual(
            actions[0]["payload"]["response_action"]["action"], "sleep")

    def test_interrupt_turn_one_action(self):
        w = _wired()
        turn = w.handle_turn("stop")
        actions = _actions(turn)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "interrupt")

    def test_wake_one_action(self):
        w = _wired()
        w.set_robot_state("sleeping")
        turn = w.wake()
        actions = _actions(turn)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "wake")
        self.assertIn("my croft", turn["speech"])

    def test_chat_with_nested_gesture_collapses_to_one_action(self):
        # FakeMycroftAgent's default reply carries gesture_cue="look":
        # speech + attentive look must be ONE action, never two.
        w = _wired()
        turn = w.handle_turn("how are you")
        actions = _actions(turn)
        self.assertEqual(len(actions), 1)
        self.assertIn(actions[0]["kind"], ("look", "gesture"))
        self.assertIn("my croft", actions[0]["speech"])

    def test_two_action_turn_fails_validation(self):
        forged = {"control_actions": [
            {"kind": "gesture"}, {"kind": "dance"}]}
        with self.assertRaises(ValueError):
            validate_turn_actions(forged)

    def test_action_kinds_are_closed_set(self):
        self.assertEqual(set(CONTROL_ACTION_KINDS), {
            "gesture", "look", "dance", "show_me", "celebrate",
            "story", "sleep", "wake", "interrupt"})


class GestureAllowlistTests(unittest.TestCase):
    """Rule 2: allowlisted trees only, fail-closed, never guessed."""

    def test_unknown_behaviour_forged_turn_no_action_with_error(self):
        turn = {"command": "gesture", "behaviour": "bht_not_a_real_tree"}
        actions, error = turn_to_control_actions(turn)
        self.assertEqual(actions, [])
        self.assertIn("not allowlisted", error)

    def test_unknown_cue_no_action_with_error(self):
        action, error = gesture_action_for_cue("teleport")
        self.assertIsNone(action)
        self.assertIn("unknown gesture cue", error)

    def test_known_cue_one_validated_action(self):
        action, error = gesture_action_for_cue("arms out")
        self.assertIsNone(error)
        self.assertEqual(action["behaviour"], "bht_arms_out")
        self.assertIn("+behaviour+:+bht_arms_out+", action["markup"])

    def test_forged_markup_is_rebuilt_not_trusted(self):
        turn = {"command": "gesture", "behaviour": "bht_arms_out",
                "markup": "FORGED-MARKUP"}
        actions, error = turn_to_control_actions(turn)
        self.assertIsNone(error)
        self.assertNotIn("FORGED-MARKUP", actions[0]["markup"])
        self.assertIn("+behaviour+:+bht_arms_out+", actions[0]["markup"])


class DanceGateTests(unittest.TestCase):
    """Rule 3: flag-gated OFF by default, awake-only, clamped, single."""

    def test_stageb_flag_still_off_and_untouched_by_wiring(self):
        self.assertFalse(control_commands.ALLOW_DANCE_MODULE)
        w = _wired()
        w.handle_turn("dance")
        self.assertFalse(control_commands.ALLOW_DANCE_MODULE)

    def test_dance_refused_by_default_no_action(self):
        w = _wired()
        turn = w.handle_turn("dance")
        self.assertEqual(_actions(turn), [])
        self.assertIn("error", turn)
        self.assertIn("dance", turn["error"].lower())
        # The dance lock must not be held by a refused turn.
        self.assertIsNone(w.controller.dance.active_tree)

    def test_dance_opted_in_requires_awake(self):
        w = _wired()
        w.set_robot_state("sleeping")
        with self.assertRaises(ValueError):
            w.handle_turn("dance", allow_dance=True)
        self.assertIsNone(w.controller.dance.active_tree)

    def test_second_concurrent_dance_rejected_never_queued(self):
        w = _wired()
        w.handle_turn("dance", allow_dance=True)
        with self.assertRaises(ValueError):
            w.handle_turn("dance", allow_dance=True)

    def test_interrupt_releases_dance(self):
        w = _wired()
        w.handle_turn("dance", allow_dance=True)
        w.interrupt()
        self.assertIsNone(w.controller.dance.active_tree)
        turn = w.handle_turn("dance", allow_dance=True)
        self.assertEqual(_actions(turn)[0]["kind"], "dance")

    def test_duration_clamped_10_to_60(self):
        base = {"command": "dance",
                "dance_tree": "bht_dance_cruncher_short_120bpm"}
        big, err = turn_to_control_actions(
            dict(base, duration=999), allow_dance=True)
        self.assertIsNone(err)
        self.assertEqual(big[0]["duration"], 60.0)
        small, err = turn_to_control_actions(
            dict(base, duration=1), allow_dance=True)
        self.assertIsNone(err)
        self.assertEqual(small[0]["duration"], 10.0)

    def test_non_dance_tree_refused_even_opted_in(self):
        turn = {"command": "dance", "dance_tree": "bht_arms_out",
                "duration": 30}
        actions, error = turn_to_control_actions(turn, allow_dance=True)
        self.assertEqual(actions, [])
        self.assertIn("not allowlisted", error)

    def test_dance_translation_gated_without_opt_in(self):
        turn = {"command": "dance",
                "dance_tree": "bht_dance_cruncher_short_120bpm",
                "duration": 30}
        actions, error = turn_to_control_actions(turn)
        self.assertEqual(actions, [])
        self.assertIn("gated", error.lower())


class ShowMeNoStorageTests(unittest.TestCase):
    """Rule 4: idle->awaiting->received->described, NO storage anywhere."""

    def test_show_me_creates_no_files(self):
        w = _wired()
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                w.handle_turn("show me")
                w.handle_turn("a red book")
            finally:
                os.chdir(cwd)
            self.assertEqual(os.listdir(tmp), [])

    def test_session_holds_only_state_and_description_token(self):
        session = ShowMeSession()
        session.start()
        session.receive("a red book")
        self.assertEqual(set(session.__dict__), {"state", "description"})
        for value in session.__dict__.values():
            self.assertNotIsInstance(value, (bytes, bytearray))
        session.acknowledge()
        self.assertEqual(session.state, "idle")
        self.assertIsNone(session.description)

    def test_forged_media_payload_stripped_from_action(self):
        turn = {"command": "look",
                "behaviour": "bht_sensoryidlefacetracker_anim",
                "image_bytes": b"\x89PNG-fake", "video": b"fake",
                "audio": b"fake", "stored_on_disk": True}
        actions, error = turn_to_control_actions(turn)
        self.assertIsNone(error)
        action = actions[0]
        for key in ("image_bytes", "image", "video", "audio", "data"):
            self.assertNotIn(key, action)
        for value in action.values():
            self.assertNotIsInstance(value, (bytes, bytearray))
        self.assertFalse(action["stored_on_disk"])

    def test_show_me_wired_flow_end_state_idle_nothing_retained(self):
        w = _wired()
        w.handle_turn("show me")
        ack = w.handle_turn("a small plant")
        self.assertFalse(ack["stored_on_disk"])
        self.assertEqual(w.controller.show_me.state, "idle")
        self.assertIsNone(w.controller.show_me.description)


class InterruptWinsTests(unittest.TestCase):
    """Rule 5: interrupt clears all sessions, wins within its turn."""

    def test_interrupt_beats_dance_same_turn(self):
        w = _wired()
        w.handle_turn("dance", allow_dance=True)
        turn = w.handle_turn("stop dancing and dance again")
        self.assertEqual([a["kind"] for a in _actions(turn)], ["interrupt"])
        self.assertIsNone(w.controller.dance.active_tree)

    def test_interrupt_beats_gesture_same_turn(self):
        w = _wired()
        turn = w.handle_turn("wave then stop")
        self.assertEqual([a["kind"] for a in _actions(turn)], ["interrupt"])

    def test_interrupt_beats_sleep_same_turn(self):
        w = _wired()
        turn = w.handle_turn("stop, then go to sleep")
        self.assertEqual([a["kind"] for a in _actions(turn)], ["interrupt"])
        self.assertNotEqual(w.controller.robot_state, "sleeping")

    def test_interrupt_beats_show_me_same_turn(self):
        w = _wired()
        w.handle_turn("show me")
        turn = w.handle_turn("stop showing me")
        self.assertEqual([a["kind"] for a in _actions(turn)], ["interrupt"])
        self.assertEqual(w.controller.show_me.state, "idle")

    def test_interrupt_method_clears_everything(self):
        w = _wired()
        w.handle_turn("dance", allow_dance=True)
        w.handle_turn("celebrate")
        out = w.interrupt()
        self.assertEqual([a["kind"] for a in _actions(out)], ["interrupt"])
        self.assertIsNone(w.controller.dance.active_tree)
        self.assertEqual(w.controller.celebration_index, 0)


class ModeGateTests(unittest.TestCase):
    """Rule 7: STOCK default, enable=takeover, disable=stock+clear."""

    def test_default_stock_no_actions(self):
        w = WiredBridge(BridgeController(agent=FakeMycroftAgent()))
        self.assertEqual(w.mode, MODE_STOCK)
        turn = w.handle_turn("wave")
        self.assertEqual(turn["command"], "stock")
        self.assertEqual(_actions(turn), [])

    def test_enable_then_disable_restores_stock_and_clears(self):
        w = _wired()
        self.assertEqual(w.mode, MODE_BRIDGE)
        w.handle_turn("dance", allow_dance=True)
        w.handle_turn("show me")
        out = w.disable()
        self.assertEqual(out["mode"], MODE_STOCK)
        self.assertTrue(out["stock_restored"])
        self.assertIsNone(w.controller.dance.active_tree)
        self.assertEqual(w.controller.show_me.state, "idle")
        turn = w.handle_turn("wave")
        self.assertEqual(turn["command"], "stock")


class WakeGatingTests(unittest.TestCase):
    """Rule 6: sleeping->wake action, standby/unknown->error, no publish."""

    def test_wake_from_sleeping_returns_action_no_publish(self):
        w = _wired()
        w.set_robot_state("sleeping")
        turn = w.wake()
        self.assertEqual(_actions(turn)[0]["kind"], "wake")
        self.assertEqual(w.controller.robot_state, "awake")
        self.assertEqual(w.controller.transport.calls, [])

    def test_wake_from_standby_refused_no_publish(self):
        w = _wired()
        w.set_robot_state("standby")
        with self.assertRaises(ValueError):
            w.wake()
        self.assertEqual(w.controller.transport.calls, [])

    def test_wake_from_unknown_refused_no_publish(self):
        w = _wired()
        w.set_robot_state("unknown")
        with self.assertRaises(ValueError):
            w.wake()
        self.assertEqual(w.controller.transport.calls, [])

    def test_wake_with_bridge_off_refused(self):
        w = WiredBridge(BridgeController(agent=FakeMycroftAgent()))
        with self.assertRaises(ValueError):
            w.wake()
        self.assertEqual(w.controller.transport.calls, [])


class NoFallbackAndRefusalTests(unittest.TestCase):
    """Rules 8+9: no silent stock fallback; action intents refused only."""

    def test_no_agent_explicit_not_stock_no_actions(self):
        w = WiredBridge(BridgeController())
        w.enable()
        w.set_robot_state("awake")
        turn = w.handle_turn("tell me something new")
        self.assertIn("agent is connected", turn["speech"])
        self.assertNotEqual(turn.get("command"), "stock_fallback")
        self.assertEqual(_actions(turn), [])

    def test_explicit_fallback_only_when_asked(self):
        w = WiredBridge(BridgeController())
        w.enable()
        w.set_robot_state("awake")
        turn = w.handle_turn("tell me something new",
                             allow_stock_fallback=True)
        self.assertEqual(turn["command"], "stock_fallback")
        self.assertEqual(_actions(turn), [])

    def test_action_intents_spoken_refusal_zero_actions(self):
        w = _wired()
        for phrase in ("send an email to Bob", "create an event",
                       "turn on the lights", "drive across the room",
                       "record video now"):
            turn = w.handle_turn(phrase)
            self.assertIn("will not do that", turn["speech"], phrase)
            self.assertEqual(_actions(turn), [], phrase)


class CreativeSteppingTests(unittest.TestCase):
    """Rule 10: one step/beat per turn, never auto-chained."""

    def test_celebrate_advances_exactly_one_step_per_turn(self):
        w = _wired()
        first = w.handle_turn("celebrate")
        self.assertEqual(w.controller.celebration_index, 1)
        second = w.handle_turn("celebrate")
        self.assertEqual(w.controller.celebration_index, 2)
        self.assertNotEqual(first["speech"], second["speech"])

    def test_story_repeated_calls_each_single_beat(self):
        w = _wired()
        for _ in range(2):
            turn = w.handle_turn("tell me a story")
            self.assertEqual(len(_actions(turn)), 1)
            self.assertEqual(turn["control_actions"][0]["kind"], "story")


class NoPublishTests(unittest.TestCase):
    """Whole wired sequence: the transport is never touched."""

    def test_full_wired_sequence_publishes_nothing(self):
        w = _wired()
        w.set_robot_state("sleeping")
        w.wake()
        w.handle_turn("wave")
        w.handle_turn("dance", allow_dance=True)
        w.handle_turn("stop")
        w.handle_turn("show me")
        w.handle_turn("a red book")
        w.handle_turn("celebrate")
        w.handle_turn("tell me a story")
        w.handle_turn("goodnight")
        self.assertEqual(w.controller.transport.calls, [])


if __name__ == "__main__":
    unittest.main()
