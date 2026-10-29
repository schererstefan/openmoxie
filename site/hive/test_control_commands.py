"""Offline Full Control API tests: run from site with the venv python:
    python -m unittest hive.test_control_commands -v

No broker, robot, network or database: publishes are recorded by a mocked
MQTT client behind the real MoxieServer send methods, and the view is driven
with RequestFactory plus a fake device row.
"""
import csv
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

DEVICE_ID = "d_testdevice"
TELEHEALTH = f"/devices/{DEVICE_ID}/commands/telehealth"
WAKEUP = f"/devices/{DEVICE_ID}/commands/wakeup"
REMOTE_CHAT = f"/devices/{DEVICE_ID}/commands/remote_chat"


class _StubRobotData:
    def __init__(self, online=True):
        self.online = online

    def device_online(self, device_id):
        return self.online

    def get_puppet_state(self, device_id):
        return "READY"

    def config_update_live(self, device):
        return False  # config saved, nothing published while "offline"

    def get_config(self, device_id):
        return {}


def _make_server(online=True):
    """Real MoxieServer send-methods over a recording MQTT client."""
    from hive.mqtt.moxie_server import MoxieServer

    server = MoxieServer.__new__(MoxieServer)
    server._client = Mock()
    server._remote_chat = SimpleNamespace(
        make_markup=lambda text, mi=None: f"MARKUP[{text}|{mi[0]}:{mi[1]}]")
    server._robot_data = _StubRobotData(online=online)
    return server


def _publishes(server):
    out = []
    for call in server._client.publish.call_args_list:
        out.append((call.args[0], json.loads(call.kwargs["payload"])))
    return out


class _DoesNotExist(Exception):
    pass


class _FakeDeviceModel:
    DoesNotExist = _DoesNotExist

    class _Manager:
        def __init__(self, device=None):
            self._device = device

        def get(self, pk):
            if self._device is None:
                raise _DoesNotExist("no such device")
            return self._device

    objects = _Manager(None)


class ControlCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import django
        from django.conf import settings

        if not settings.configured:
            settings.configure(
                INSTALLED_APPS=["hive"],
                TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates"}],
                SECRET_KEY="offline-control-command-tests",
            )
        django.setup()
        from hive.mqtt import control_commands
        from hive import views

        cls.cc = control_commands
        cls.views = views

    # -- helpers -------------------------------------------------------------

    def _device(self):
        return SimpleNamespace(device_id=DEVICE_ID, robot_config={}, save=Mock())

    def _call(self, method, data=None, server=None, device=None):
        from django.test import RequestFactory

        server = server if server is not None else _make_server()
        device = device if device is not None else self._device()
        request = getattr(RequestFactory(), method)("/puppet", data=data or {})
        model = type("DM", (), {"DoesNotExist": _DoesNotExist,
                                 "objects": _FakeDeviceModel._Manager(device)})
        with patch.object(self.views, "MoxieDevice", model), \
             patch.object(self.views, "get_instance", return_value=server):
            response = self.views.puppet_api(request, pk=1)
        body = json.loads(response.content) if response.content else {}
        return response.status_code, body, server, device

    # -- phonetic_name ---------------------------------------------------------

    def test_phonetic_name(self):
        fn = self.cc.phonetic_name
        self.assertEqual(fn("Mycroft"), "my croft")
        self.assertEqual(fn("MYCROFT"), "my croft")
        self.assertEqual(fn("Hello Mycroft, meet mycroft."), "Hello my croft, meet my croft.")
        self.assertEqual(fn("Mycroft's hat"), "my croft's hat")
        self.assertEqual(fn("my craft"), "my craft")  # already-spoken form left alone
        self.assertEqual(fn("Mycroftian"), "Mycroftian")  # substring not substituted
        self.assertEqual(fn(""), "")
        self.assertIsNone(fn(None))

    # -- allowlists ---------------------------------------------------------------

    def test_gesture_allowlist_matches_manifest(self):
        with self.cc.default_manifest_path().open(newline="", encoding="utf-8-sig") as fh:
            expected = {
                row["Lbl"].strip() for row in csv.DictReader(fh)
                if row["Asset Type"].strip() == "BehaviourTree"
                and row["Default"].strip().lower() == "true" and row["Lbl"].strip()
            }
        self.assertEqual(set(self.cc.GESTURE_BEHAVIOURS), expected)
        self.assertIn("bht_arms_out", self.cc.GESTURE_BEHAVIOURS)
        self.assertIn("bht_beckon_user", self.cc.GESTURE_BEHAVIOURS)
        self.assertNotIn("bht_not_a_real_tree", self.cc.GESTURE_BEHAVIOURS)

    def test_missing_manifest_fails_closed(self):
        self.assertEqual(self.cc.load_gesture_behaviours("/nonexistent/manifest.csv"), frozenset())

    def test_look_allowlist_is_exact_five(self):
        self.assertEqual(set(self.cc.LOOK_BEHAVIOURS), {
            "bht_search", "bht_searching", "bht_search_still",
            "bht_360_look_around", "bht_sensoryidlefacetracker_anim",
        })
        self.assertEqual(self.cc.DEFAULT_LOOK_BEHAVIOUR, "bht_sensoryidlefacetracker_anim")
        self.assertTrue(set(self.cc.LOOK_BEHAVIOURS) <= set(self.cc.GESTURE_BEHAVIOURS))

    def test_gesture_markup_single_mark(self):
        markup = self.cc.build_gesture_markup("bht_arms_out")
        self.assertEqual(markup.count("<mark"), 1)
        self.assertIn("cmd:behaviour-tree", markup)
        self.assertIn("+behaviour+:+bht_arms_out+", markup)
        with self.assertRaises(ValueError):
            self.cc.build_gesture_markup("bht_not_a_real_tree")

    def test_look_markup_default_and_rejections(self):
        self.assertIn("+behaviour+:+bht_sensoryidlefacetracker_anim+",
                      self.cc.build_look_markup())
        for behaviour in self.cc.LOOK_BEHAVIOURS:
            self.assertIn(f"+behaviour+:+{behaviour}+", self.cc.build_look_markup(behaviour))
        with self.assertRaises(ValueError):  # gesture-valid, look-invalid
            self.cc.build_look_markup("bht_arms_out")

    # -- dance / sleep payloads ------------------------------------------------------

    def test_dance_flag_off_by_default_and_refused(self):
        self.assertFalse(self.cc.ALLOW_DANCE_MODULE)
        self.assertFalse(self.cc.is_dance_enabled())
        with self.assertRaises(ValueError):
            self.cc.build_dance_payload()

    def test_dance_payload_when_flag_on(self):
        with patch.object(self.cc, "ALLOW_DANCE_MODULE", True):
            payload = self.cc.build_dance_payload()
            self.assertEqual(payload["command"], "remote_chat")
            action = {"action": "launch", "output_type": "GLOBAL_RESPONSE", "module_id": "DANCE"}
            self.assertEqual(payload["response_action"], action)
            self.assertEqual(payload["response_actions"], [action])
            self.assertEqual(self.cc.build_dance_payload(module_id="DANCE")["response_action"], action)
            with self.assertRaises(ValueError):
                self.cc.build_dance_payload(module_id="READ")

    def test_sleep_payload_is_response_action(self):
        payload = self.cc.build_sleep_payload()
        self.assertEqual(payload["command"], "remote_chat")
        self.assertEqual(payload["response_action"]["action"], "sleep")
        self.assertNotIn("module_id", payload["response_action"])

    # -- validators -------------------------------------------------------------------

    def test_intensity_validation(self):
        for ok in (0, 0.5, 1, "0.25"):
            self.assertEqual(self.cc.validate_intensity(ok), float(ok))
        for bad in (-0.1, 1.1, "loud", None):
            with self.assertRaises(ValueError):
                self.cc.validate_intensity(bad)

    def test_mood_and_duration_validation(self):
        self.assertEqual(self.cc.validate_mood("happy"), "happy")
        with self.assertRaises(ValueError):
            self.cc.validate_mood("ecstatic")
        self.assertEqual(self.cc.parse_duration(None), 1.0)
        self.assertEqual(self.cc.parse_duration("2"), 2.0)
        self.assertEqual(self.cc.parse_duration(100), 30.0)  # clamped
        self.assertEqual(self.cc.parse_duration(0), 0.1)  # clamped
        with self.assertRaises(ValueError):
            self.cc.parse_duration("long")

    def test_single_command_and_single_movement(self):
        self.assertEqual(self.cc.parse_single_command("speak"), "speak")
        for bundled in ("gesture,dance", "gesture+dance", "gesture dance", "", None):
            with self.assertRaises(ValueError):
                self.cc.parse_single_command(bundled)
        with self.assertRaises(ValueError):
            self.cc.parse_single_command("speak", post_keys={"commands": "x"})
        self.cc.validate_single_movement(["gesture", "speak"])  # one movement
        with self.assertRaises(ValueError):
            self.cc.validate_single_movement(["gesture", "look"])

    # -- MoxieServer shaping over a mocked MQTT client ------------------------------

    def test_server_speech_and_interrupt_topics(self):
        server = _make_server()
        server.send_telehealth_speech(DEVICE_ID, "Hello", "happy", 0.5)
        server.send_telehealth_interrupt(DEVICE_ID)
        self.assertEqual(_publishes(server), [
            (TELEHEALTH, {"command": "telehealth", "message": {
                "action": "PLAY_OUTPUT",
                "output": {"text": "Hello", "markup": "MARKUP[Hello|happy:0.5]"}}}),
            (TELEHEALTH, {"command": "telehealth", "message": {"action": "INTERRUPT"}}),
        ])

    def test_server_behaviour_silent_and_with_speech(self):
        server = _make_server()
        server.send_telehealth_behaviour(DEVICE_ID, "bht_arms_out")
        server.send_telehealth_behaviour(DEVICE_ID, "bht_search", speech="Looking now")
        pubs = _publishes(server)
        self.assertEqual([t for t, _ in pubs], [TELEHEALTH, TELEHEALTH])
        silent, spoken = pubs[0][1]["message"]["output"], pubs[1][1]["message"]["output"]
        self.assertEqual(silent["text"], "")
        self.assertEqual(silent["markup"].count("<mark"), 1)
        self.assertIn("+behaviour+:+bht_arms_out+", silent["markup"])
        self.assertEqual(spoken["text"], "Looking now")
        self.assertTrue(spoken["markup"].endswith("Looking now"))
        self.assertIn("+behaviour+:+bht_search+", spoken["markup"])

    def test_server_wake_online_and_offline(self):
        online = _make_server(online=True)
        self.assertTrue(online.send_wakeup_to_bot(DEVICE_ID))
        self.assertEqual(_publishes(online), [(WAKEUP, {"command": "wakeup"})])
        offline = _make_server(online=False)
        self.assertFalse(offline.send_wakeup_to_bot(DEVICE_ID))
        self.assertEqual(_publishes(offline), [])

    # -- puppet_api ------------------------------------------------------------------

    def test_status_enrichment_no_publish(self):
        status, body, server, _ = self._call("get")
        self.assertEqual(status, 200)
        self.assertEqual(body["online"], True)
        self.assertEqual(body["puppet_state"], "READY")
        self.assertEqual(body["puppet_enabled"], False)
        self.assertEqual(body["dance_enabled"], False)
        self.assertEqual(body["control_commands"], list(self.cc.CONTROL_COMMANDS))
        self.assertEqual(_publishes(server), [])

    def test_unknown_device_command_and_bundling_rejected(self):
        from django.test import RequestFactory
        request = RequestFactory().post("/puppet", data={"command": "speak"})
        model = type("DM", (), {"DoesNotExist": _DoesNotExist,
                                 "objects": _FakeDeviceModel._Manager(None)})
        with patch.object(self.views, "MoxieDevice", model), \
             patch.object(self.views, "get_instance", return_value=_make_server()):
            self.assertEqual(self.views.puppet_api(request, pk=99).status_code, 400)
        for data in ({"command": "fly"}, {}, {"command": "gesture,dance"},
                     {"command": "gesture+dance"}, {"commands": "speak", "command": "speak"}):
            status, _, server, _ = self._call("post", data)
            self.assertEqual(status, 400, data)
            self.assertEqual(_publishes(server), [], data)

    def test_enable_disable_existing_shape(self):
        server, device = _make_server(), self._device()
        status, body, _, _ = self._call("post", {"command": "enable"}, server, device)
        self.assertEqual((status, body), (200, {"result": True}))
        self.assertEqual(device.robot_config["moxie_mode"], "TELEHEALTH")
        status, body, _, _ = self._call("post", {"command": "disable"}, server, device)
        self.assertEqual((status, body), (200, {"result": True}))
        self.assertNotIn("moxie_mode", device.robot_config)
        self.assertEqual(device.save.call_count, 2)
        self.assertEqual(_publishes(server), [])  # config only; stub is "offline"

    def test_speak_applies_phonetic_name(self):
        status, body, server, _ = self._call("post", {
            "command": "speak", "speech": "Hi Mycroft", "mood": "happy", "intensity": "0.5"})
        self.assertEqual((status, body), (200, {"result": True}))
        topic, payload = _publishes(server)[0]
        self.assertEqual(topic, TELEHEALTH)
        self.assertEqual(payload["message"]["output"]["text"], "Hi my croft")

    def test_speak_validation_rejections(self):
        for data in (
            {"command": "speak", "speech": "Hi", "mood": "happy", "intensity": "1.5"},
            {"command": "speak", "speech": "Hi", "mood": "happy", "intensity": "-1"},
            {"command": "speak", "speech": "Hi", "mood": "bogus", "intensity": "0.5"},
            {"command": "speak", "mood": "happy", "intensity": "0.5"},
        ):
            status, _, server, _ = self._call("post", data)
            self.assertEqual(status, 400, data)
            self.assertEqual(_publishes(server), [], data)

    def test_interrupt_existing_shape(self):
        status, body, server, _ = self._call("post", {"command": "interrupt"})
        self.assertEqual((status, body), (200, {"result": True}))
        self.assertEqual(_publishes(server),
                         [(TELEHEALTH, {"command": "telehealth", "message": {"action": "INTERRUPT"}})])

    def test_gesture_and_look_api(self):
        status, body, server, _ = self._call("post", {"command": "gesture", "behaviour": "bht_arms_out"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"result": True, "command": "gesture", "behaviour": "bht_arms_out"})
        self.assertIn("+behaviour+:+bht_arms_out+", _publishes(server)[0][1]["message"]["output"]["markup"])

        status, body, server, _ = self._call("post", {
            "command": "gesture", "behaviour": "bht_beckon_user", "speech": "Come here Mycroft"})
        self.assertEqual(status, 200)
        self.assertEqual(_publishes(server)[0][1]["message"]["output"]["text"], "Come here my croft")

        status, _, server, _ = self._call("post", {"command": "gesture", "behaviour": "nope"})
        self.assertEqual(status, 400)
        self.assertEqual(_publishes(server), [])

        status, body, server, _ = self._call("post", {"command": "look"})
        self.assertEqual(body["behaviour"], "bht_sensoryidlefacetracker_anim")
        self.assertEqual(status, 200)
        status, _, server, _ = self._call("post", {"command": "look", "behaviour": "bht_arms_out"})
        self.assertEqual(status, 400)
        self.assertEqual(_publishes(server), [])

    def test_dance_api_flag_and_module(self):
        status, _, server, _ = self._call("post", {"command": "dance"})
        self.assertEqual(status, 400)  # flag off by default
        self.assertEqual(_publishes(server), [])
        with patch.object(self.cc, "ALLOW_DANCE_MODULE", True):
            status, body, server, _ = self._call("post", {"command": "dance"})
            self.assertEqual(status, 200)
            self.assertEqual(body["module_id"], "DANCE")
            topic, payload = _publishes(server)[0]
            self.assertEqual(topic, REMOTE_CHAT)
            self.assertEqual(payload["response_action"]["module_id"], "DANCE")
            self.assertEqual(payload["response_action"]["action"], "launch")
            status, _, server, _ = self._call("post", {"command": "dance", "module_id": "READ"})
            self.assertEqual(status, 400)
            self.assertEqual(_publishes(server), [])

    def test_wake_and_sleep_api(self):
        status, body, server, _ = self._call("post", {"command": "wake"}, _make_server(online=True))
        self.assertEqual((status, body), (200, {"result": True, "command": "wake"}))
        self.assertEqual(_publishes(server)[0][0], WAKEUP)
        status, body, server, _ = self._call("post", {"command": "wake"}, _make_server(online=False))
        self.assertEqual((status, body), (200, {"result": False, "command": "wake"}))
        self.assertEqual(_publishes(server), [])
        status, body, server, _ = self._call("post", {"command": "sleep"})
        self.assertEqual((status, body), (200, {"result": True, "command": "sleep"}))
        topic, payload = _publishes(server)[0]
        self.assertEqual(topic, REMOTE_CHAT)
        self.assertEqual(payload["response_action"]["action"], "sleep")

    def test_mocked_mqtt_full_sequence(self):
        server, device = _make_server(), self._device()
        steps = [
            {"command": "enable"},
            {"command": "speak", "speech": "Hello Mycroft", "mood": "neutral", "intensity": "0.5"},
            {"command": "gesture", "behaviour": "bht_arms_out"},
            {"command": "look"},
            {"command": "dance"},
            {"command": "interrupt"},
            {"command": "sleep"},
            {"command": "disable"},
        ]
        with patch.object(self.cc, "ALLOW_DANCE_MODULE", True):
            for data in steps:
                status, _, _, _ = self._call("post", data, server, device)
                self.assertEqual(status, 200, data)
        pubs = _publishes(server)
        self.assertEqual([t for t, _ in pubs],
                         [TELEHEALTH, TELEHEALTH, TELEHEALTH, REMOTE_CHAT, TELEHEALTH, REMOTE_CHAT])
        self.assertEqual(pubs[0][1]["message"]["output"]["text"], "Hello my croft")
        self.assertIn("+behaviour+:+bht_arms_out+", pubs[1][1]["message"]["output"]["markup"])
        self.assertIn("+behaviour+:+bht_sensoryidlefacetracker_anim+", pubs[2][1]["message"]["output"]["markup"])
        self.assertEqual(pubs[3][1]["response_action"]["module_id"], "DANCE")
        self.assertEqual(pubs[4][1]["message"]["action"], "INTERRUPT")
        self.assertEqual(pubs[5][1]["response_action"]["action"], "sleep")
        self.assertNotIn("moxie_mode", device.robot_config)
        self.assertEqual(device.save.call_count, 2)


if __name__ == "__main__":
    unittest.main()
