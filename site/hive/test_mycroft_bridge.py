"""Tests for the full Mycroft bridge. Mocked only: no MQTT, no robot,
no network, no keys. Fake agent + MockTransport + fake read-only tools.
"""
import unittest

from hive.mqtt.mycroft_bridge import (
    MODE_BRIDGE,
    MODE_STOCK,
    BridgeController,
    FakeMycroftAgent,
    MockTransport,
)


def _bridge(**kw):
    kw.setdefault("agent", FakeMycroftAgent())
    kw.setdefault("tools", {
        "calendar": lambda arg: "Tomorrow: no meetings, Stefan.",
        "weather": lambda arg: "Sunny in Studio City, Stefan.",
    })
    b = BridgeController(**kw)
    b.enable()
    b.set_robot_state("awake")
    return b


class BridgeModeTests(unittest.TestCase):
    def test_default_is_stock(self):
        b = BridgeController()
        self.assertEqual(b.mode, MODE_STOCK)

    def test_enable_then_disable_restores_stock(self):
        b = _bridge()
        self.assertEqual(b.mode, MODE_BRIDGE)
        out = b.disable()
        self.assertEqual(out["mode"], MODE_STOCK)
        self.assertTrue(out["stock_restored"])
        self.assertEqual(b.mode, MODE_STOCK)

    def test_turn_when_bridge_off_goes_to_stock(self):
        b = BridgeController(agent=FakeMycroftAgent())
        self.assertEqual(b.handle_turn("hello")["command"], "stock")


class BridgeWakeTests(unittest.TestCase):
    def test_wake_sleeping_succeeds_and_greets_my_croft(self):
        b = _bridge()
        b.set_robot_state("sleeping")
        turn = b.wake()
        self.assertIn("my croft", turn["speech"])
        self.assertNotIn("out of that box", turn["speech"])
        self.assertTrue(turn["replaces_stock_welcome"])
        self.assertEqual(b.robot_state, "awake")
        self.assertEqual(b.transport.calls[0][0], "wakeup")

    def test_wake_from_standby_refused(self):
        b = _bridge()
        b.set_robot_state("standby")
        with self.assertRaises(ValueError):
            b.wake()
        self.assertEqual(b.transport.calls, [])

    def test_wake_from_unknown_refused(self):
        b = _bridge()
        b.set_robot_state("unknown")
        with self.assertRaises(ValueError):
            b.wake()

    def test_wake_when_bridge_off_refused(self):
        b = BridgeController()
        with self.assertRaises(ValueError):
            b.wake()


class BridgeTurnTests(unittest.TestCase):
    def test_chat_turn_phonetic_and_one_movement(self):
        b = _bridge()
        turn = b.handle_turn("Hello there")
        self.assertIn("my croft", turn["speech"])
        self.assertEqual(turn["transport"], "telehealth")

    def test_gesture_turn(self):
        b = _bridge()
        turn = b.handle_turn("wave")
        self.assertEqual(turn["command"], "look")
        self.assertIn("my croft", turn["speech"])

    def test_dance_requires_awake(self):
        b = _bridge()
        b.set_robot_state("sleeping")
        with self.assertRaises(ValueError):
            b.handle_turn("dance")
        b.set_robot_state("awake")
        turn = b.handle_turn("dance")
        self.assertEqual(turn["command"], "dance")
        self.assertEqual(turn["dance_tree"], "bht_dance_cruncher_short_120bpm")

    def test_second_dance_rejected_never_queued(self):
        b = _bridge()
        b.handle_turn("dance")
        with self.assertRaises(ValueError):
            b.handle_turn("dance")

    def test_interrupt_clears_dance_and_gesture(self):
        b = _bridge()
        b.handle_turn("dance")
        self.assertEqual(b.interrupt()["command"], "interrupt")
        turn = b.handle_turn("dance")
        self.assertEqual(turn["command"], "dance")

    def test_celebration_steps_one_at_a_time(self):
        b = _bridge()
        first = b.handle_turn("celebrate")
        second = b.handle_turn("celebrate")
        self.assertNotEqual(first["speech"], second["speech"])

    def test_story_is_one_beat(self):
        b = _bridge()
        turn = b.handle_turn("tell me a story")
        self.assertEqual(turn["command"], "speak")
        self.assertIn("Stefan", turn["speech"])

    def test_show_me_flow_no_storage(self):
        b = _bridge()
        start = b.handle_turn("show me")
        self.assertEqual(start["session_state"], "awaiting_show")
        ack = b.handle_turn("a red book")
        self.assertEqual(ack["shown_description"], "a red book")
        self.assertFalse(ack["stored_on_disk"])
        self.assertEqual(b.show_me.state, "idle")

    def test_sleep_turn_sets_sleeping(self):
        b = _bridge()
        turn = b.handle_turn("goodnight")
        self.assertEqual(turn["command"], "sleep")
        self.assertEqual(b.robot_state, "sleeping")

    def test_calendar_tool_readonly(self):
        b = _bridge()
        turn = b.handle_turn("what is my day tomorrow")
        self.assertEqual(turn["tool"], "calendar")
        self.assertIn("Tomorrow", turn["speech"])

    def test_weather_tool_readonly(self):
        b = _bridge()
        turn = b.handle_turn("what is the weather")
        self.assertEqual(turn["tool"], "weather")

    def test_unknown_tool_refused(self):
        b = _bridge()
        with self.assertRaises(ValueError):
            b.tools.call("send_email")

    def test_action_intents_refused_not_executed(self):
        b = _bridge()
        for phrase in ("send an email to Bob", "turn on the lights",
                       "drive across the room", "take a photo"):
            turn = b.handle_turn(phrase)
            self.assertIn("will not do that", turn["speech"])

    def test_no_agent_is_explicit_not_silent_gpt(self):
        b = BridgeController()
        b.enable()
        b.set_robot_state("awake")
        turn = b.handle_turn("tell me something new")
        self.assertIn("agent is connected", turn["speech"])
        self.assertIn("not falling back", turn["speech"])
        self.assertNotEqual(turn.get("command"), "stock_fallback")

    def test_explicit_fallback_only_when_asked(self):
        b = BridgeController()
        b.enable()
        b.set_robot_state("awake")
        turn = b.handle_turn("tell me something new", allow_stock_fallback=True)
        self.assertEqual(turn["command"], "stock_fallback")

    def test_speech_markup_stripped(self):
        b = _bridge()
        turn = b.proactive_turn("Hello <launch>DANCE</launch> Mycroft",
                                scheduled=True)
        self.assertNotIn("<launch>", turn["speech"])
        self.assertNotIn("<", turn["speech"])
        self.assertIn("my croft", turn["speech"])

    def test_proactive_requires_schedule(self):
        b = _bridge()
        with self.assertRaises(ValueError):
            b.proactive_turn("Reminder, Stefan.")
        turn = b.proactive_turn("Reminder, Stefan.", scheduled=True)
        self.assertIn("Reminder", turn["speech"])

    def test_identity_is_stefan_not_pat(self):
        b = _bridge()
        turn = b.wake()
        self.assertIn("Stefan", turn["speech"])
        self.assertNotIn("Pat", turn["speech"])

    def test_replayed_transcript_sequence(self):
        b = _bridge()
        b.set_robot_state("sleeping")
        self.assertEqual(b.wake()["command"], "speak")
        self.assertIn("my croft", b.handle_turn("wave")["speech"])
        self.assertEqual(b.handle_turn("what is the weather")["tool"], "weather")
        self.assertEqual(b.handle_turn("goodnight")["command"], "sleep")
        # Wake from sleeping (MQTT still connected) is allowed:
        self.assertEqual(b.wake()["command"], "speak")


if __name__ == "__main__":
    unittest.main()
