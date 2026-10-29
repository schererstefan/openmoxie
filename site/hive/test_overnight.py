"""Overnight features: gesture conversations, dance, creative, show-me.

Mocked/offline only. No MQTT, no robot, no live server.
Run from site/: python -m unittest hive.test_overnight -v
"""
import unittest

from hive.mqtt.creative_modes import (
    build_celebration_routine,
    build_reminder,
    build_story_beat,
    build_wake_greeting,
)
from hive.mqtt.dance_modules import (
    DANCE_TREES,
    DanceSession,
    build_dance_tree_turn,
    clamp_dance_duration,
)
from hive.mqtt.gesture_conversation import (
    GestureConversation,
    build_gesture_turn,
    normalise_cue,
)
from hive.mqtt.show_me import ShowMeSession


class GestureConversationTests(unittest.TestCase):
    def test_arms_out_turn(self):
        turn = build_gesture_turn("arms out")
        self.assertEqual(turn["behaviour"], "bht_arms_out")
        self.assertEqual(turn["command"], "gesture")
        self.assertTrue(turn["interruptible"])

    def test_wave_is_my_croft(self):
        turn = build_gesture_turn("wave")
        self.assertIn("my croft", turn["speech"])
        self.assertNotIn("Mycroft", turn["speech"])

    def test_unknown_cue_rejected(self):
        with self.assertRaises(ValueError):
            build_gesture_turn("teleport")

    def test_dance_cue_refused_here(self):
        with self.assertRaises(ValueError):
            build_gesture_turn("dance")

    def test_empty_cue_rejected(self):
        with self.assertRaises(ValueError):
            normalise_cue("   ")

    def test_session_counts_and_interrupt(self):
        convo = GestureConversation()
        convo.next_turn("look")
        self.assertEqual(convo.turns, 1)
        convo.interrupt()
        self.assertTrue(convo.interrupted)
        self.assertIsNone(convo.last_cue)


class SanitiseTests(unittest.TestCase):
    def test_markup_stripped(self):
        turn = build_gesture_turn("wave", speech="Hi <launch:foo> there")
        self.assertNotIn("<launch", turn["speech"])
        self.assertIn("Hi", turn["speech"])

    def test_long_speech_capped(self):
        turn = build_gesture_turn("wave", speech="x" * 900)
        self.assertLessEqual(len(turn["speech"]), 500)


class DanceModuleTests(unittest.TestCase):
    def test_registry_nonempty_and_default(self):
        self.assertIn("bht_dance_cruncher_short_120bpm", DANCE_TREES)
        self.assertGreaterEqual(len(DANCE_TREES), 20)

    def test_duration_clamped(self):
        self.assertEqual(clamp_dance_duration(999), 60.0)
        self.assertEqual(clamp_dance_duration(1), 10.0)
        self.assertEqual(clamp_dance_duration(None), 30.0)

    def test_non_dance_tree_rejected(self):
        with self.assertRaises(ValueError):
            build_dance_tree_turn("bht_arms_out")

    def test_asleep_dance_rejected(self):
        with self.assertRaises(ValueError):
            build_dance_tree_turn(robot_state="sleeping")

    def test_one_dance_at_a_time(self):
        session = DanceSession()
        session.start()
        with self.assertRaises(ValueError):
            session.start()
        session.interrupt()
        # After interrupt a new dance is allowed again.
        session.start()
        session.interrupt()

    def test_bad_duration_rejected(self):
        with self.assertRaises(ValueError):
            build_dance_tree_turn(duration="fast")


class CreativeModeTests(unittest.TestCase):
    def test_wake_greeting_is_my_croft_not_stock(self):
        greeting = build_wake_greeting()
        self.assertIn("my croft", greeting["speech"])
        self.assertTrue(greeting["replaces_stock_welcome"])
        self.assertNotIn("out of that box", greeting["speech"])

    def test_story_beat_with_gesture(self):
        beat = build_story_beat("Once upon a time.", gesture_cue="celebrate")
        self.assertEqual(beat["gesture_turn"]["behaviour"], "bht_celebration_dance_01")

    def test_story_beat_bad_mood(self):
        with self.assertRaises(ValueError):
            build_story_beat("Hi.", mood="ecstatic")

    def test_celebration_routine_is_steps_not_loop(self):
        routine = build_celebration_routine()
        self.assertEqual(len(routine), 3)
        self.assertEqual(routine[0]["cue"], "celebrate")

    def test_reminder_never_autonomous(self):
        reminder = build_reminder("Water the plants.", "tomorrow morning")
        self.assertFalse(reminder["delivered"])
        self.assertFalse(reminder["autonomous_delivery"])


class ShowMeTests(unittest.TestCase):
    def test_full_session_no_storage(self):
        session = ShowMeSession()
        started = session.start()
        self.assertEqual(started["session_state"], "awaiting_show")
        received = session.receive("a red book")
        self.assertEqual(received["description"], "a red book")
        self.assertFalse(received["stored_on_disk"])
        ack = session.acknowledge()
        self.assertEqual(session.state, "idle")
        self.assertIn("red book", ack["speech"])

    def test_receive_without_start_rejected(self):
        with self.assertRaises(ValueError):
            ShowMeSession().receive("a book")

    def test_double_start_rejected(self):
        session = ShowMeSession()
        session.start()
        with self.assertRaises(ValueError):
            session.start()

    def test_cancel_resets(self):
        session = ShowMeSession()
        session.start()
        session.cancel()
        self.assertEqual(session.state, "idle")


if __name__ == "__main__":
    unittest.main()
