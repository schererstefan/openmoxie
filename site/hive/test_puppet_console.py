"""Offline puppet-console tests: run from site with the venv python:
    python -m unittest hive.test_puppet_console -v

No broker, robot, network, database or live log file is touched:
publishes are recorded by a mocked MQTT client behind the real
MoxieServer send methods, the console views are driven with
RequestFactory, and the console page template is really rendered
(APP_DIRS + django_bootstrap5 + staticfiles, no {% url %} tags in the
new template so it renders standalone).

CSRF note: RequestFactory calls bypass middleware, so CSRF enforcement
itself is not exercised here; what IS tested is that the new module
carries no @csrf_exempt decorator (deployment middleware then
enforces CSRF). The pre-existing stock puppet_api exemption is out of
scope for this module and is flagged in the checkpoint.
"""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

DEVICE_ID = "d_testdevice"
TELEHEALTH = "/devices/%s/commands/telehealth" % DEVICE_ID
WAKEUP = "/devices/%s/commands/wakeup" % DEVICE_ID
REMOTE_CHAT = "/devices/%s/commands/remote_chat" % DEVICE_ID

STAFF = SimpleNamespace(is_active=True, is_staff=True, is_authenticated=True)
NONSTAFF = SimpleNamespace(is_active=True, is_staff=False, is_authenticated=True)
ANON = SimpleNamespace(is_active=False, is_staff=False, is_authenticated=False)


class _StubRobotData:
    def __init__(self, online=True):
        self.online = online

    def device_online(self, device_id):
        return self.online

    def get_puppet_state(self, device_id):
        return "READY"

    def config_update_live(self, device):
        return False

    def get_config(self, device_id):
        return {}


def _make_server(online=True):
    from hive.mqtt.moxie_server import MoxieServer

    server = MoxieServer.__new__(MoxieServer)
    server._client = Mock()
    server._remote_chat = SimpleNamespace(
        make_markup=lambda text, mi=None: "MARKUP[%s]" % text)
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


class PuppetConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import django
        from django.conf import settings

        if not settings.configured:
            settings.configure(
                INSTALLED_APPS=[
                    "django.contrib.contenttypes",
                    "django.contrib.auth",
                    "django.contrib.staticfiles",
                    "django_bootstrap5",
                    "hive",
                ],
                TEMPLATES=[{
                    "BACKEND": "django.template.backends.django.DjangoTemplates",
                    "APP_DIRS": True,
                    "OPTIONS": {},
                }],
                STATIC_URL="/static/",
                SECRET_KEY="offline-puppet-console-tests",
                ALLOWED_HOSTS=["testserver"],
                ROOT_URLCONF="hive.urls",
            )
        django.setup()
        if not getattr(settings, "ROOT_URLCONF", None):
            # The auth decorator resolves its login URL through the URL
            # resolver; other hive tests configure no ROOT_URLCONF.
            settings.ROOT_URLCONF = "hive.urls"
            from django.urls import clear_url_caches

            clear_url_caches()
        from hive import puppet_console, puppet_inventory, vision_events

        cls.pc = puppet_console
        cls.inv = puppet_inventory
        cls.ve = vision_events

    def setUp(self):
        self.pc.reset_console_state()
        self.session = {}
        self.server = _make_server(online=True)

    # -- helpers -------------------------------------------------------------
    def _device(self, puppet=False):
        config = {"moxie_mode": "TELEHEALTH"} if puppet else {}
        return SimpleNamespace(
            pk=1, name="Test Moxie", device_id=DEVICE_ID,
            robot_config=config, state={"battery_level": 88},
            last_connect=None, last_disconnect=None, save=Mock())

    def _model(self, device):
        return type("DM", (), {"DoesNotExist": _DoesNotExist,
                               "objects": _FakeDeviceModel._Manager(device)})

    def _request(self, method, data=None, user=STAFF, server=None, device=None):
        from django.test import RequestFactory

        factory = RequestFactory()
        request = getattr(factory, method)("/console", data=data or {})
        request.user = user
        request.session = self.session
        model = self._model(device if device is not None else self._device())
        srv = server if server is not None else self.server
        ctx = patch.object(self.pc, "MoxieDevice", model)
        ctx2 = patch.object(self.pc, "get_instance", return_value=srv)
        return request, ctx, ctx2

    def _api(self, method, data=None, user=STAFF, server=None, device=None):
        request, ctx, ctx2 = self._request(method, data, user, server, device)
        with ctx, ctx2:
            response = self.pc.puppet_console_api(request, pk=1)
        body = json.loads(response.content) if response.content else {}
        return response.status_code, body

    def _page(self, method, data=None, user=STAFF, server=None, device=None):
        request, ctx, ctx2 = self._request(method, data, user, server, device)
        with ctx, ctx2:
            response = self.pc.puppet_console_page(request, pk=1)
        return response

    # -- inventory -----------------------------------------------------------
    def test_inventory_counts(self):
        counts = self.inv.INVENTORY["counts"]
        self.assertEqual(counts["gestures"], 499)
        self.assertEqual(counts["looks"], 5)
        self.assertEqual(counts["dances"], 23)
        self.assertEqual(counts["gesture_cues"], 7)
        self.assertEqual(counts["manifest_total"], 574)
        self.assertEqual(counts["manifest_only_not_offered"], 47)
        self.assertEqual(counts["gestures"] + counts["looks"] + counts["dances"]
                         + counts["manifest_only_not_offered"], 574)

    def test_inventory_fail_closed_and_motor_auto_excluded(self):
        self.assertFalse(self.inv.is_operator_gesture("not_a_tree"))
        self.assertFalse(self.inv.is_dance_tree("not_a_tree"))
        self.assertFalse(self.inv.is_look("not_a_tree"))
        self.assertNotIn("gesture:not_a_tree", self.inv.sequence_step_ids())
        self.assertIn("Bht_Motor_Test_Head_Rotation", self.inv.MANIFEST_ONLY_NOT_OFFERED)
        self.assertNotIn("Bht_Motor_Test_Head_Rotation", self.inv.OPERATOR_GESTURES)
        self.assertTrue(any(b.startswith("AUTO_GESTURE_")
                            for b in self.inv.MANIFEST_ONLY_NOT_OFFERED))
        self.assertFalse(any(b.startswith("AUTO_GESTURE_")
                             for b in self.inv.OPERATOR_GESTURES))

    def test_plain_names(self):
        self.assertEqual(self.inv.plain_name("bht_arms_out"), "arms out")
        self.assertEqual(self.inv.plain_name("Gesture_Nod"), "Nod")
        self.assertEqual(self.inv.plain_name("bht_dance_cruncher_short_120bpm"),
                         "dance cruncher short 120bpm")

    def test_absent_list_covers_camera_driving_joints(self):
        titles = [t for t, _ in self.inv.ABSENT]
        for wanted in ("Camera feed / live video", "Joint / head-angle control",
                       "Wheel driving / locomotion", "Face identity / face recognition"):
            self.assertIn(wanted, titles)

    # -- vision --------------------------------------------------------------
    def test_vision_event_inventory(self):
        self.assertEqual(set(self.ve.VISION_EVENTS),
                         {"eb-found-face", "eb-lost-face", "eb-lost-target",
                          "eb-br-event", "eb-dr-event", "eb-qr-event"})
        self.assertEqual(set(self.ve.DETECTORS),
                         {"face_search", "book", "aruco", "qr"})

    def test_parse_vision_input_never_confuses_speech(self):
        parsed = self.ve.parse_vision_input(
            "eb-qr-event", {"$eb_qr_value": "QR123"})
        self.assertEqual(parsed["value"], "QR123")
        self.assertEqual(parsed["value_field"], "$eb_qr_value")
        self.assertIsNone(self.ve.parse_vision_input("hello Stefan"))
        self.assertIsNone(self.ve.parse_vision_input("eb-found-face-extra"))
        book = self.ve.parse_vision_input("eb-br-event", {"$eb_br_value": "Dune"})
        self.assertEqual(book["value"], "Dune")

    def test_detector_payload_shape(self):
        payload = self.ve.build_detector_payload("qr", True)
        action = payload["response_actions"][0]
        self.assertEqual(action["action"], "execute")
        self.assertEqual(action["function_id"], "eb_enable_qr")
        self.assertEqual(action["function_args"], ["true"])
        self.assertEqual(action["event_subscription"]["active"], ["eb-qr-event"])
        off = self.ve.build_detector_payload("book", False)
        off_action = off["response_actions"][0]
        self.assertEqual(off_action["function_id"], "eb_enable_book")
        self.assertEqual(off_action["function_args"], ["false"])
        self.assertTrue(off_action["event_subscription"]["clear"])
        with self.assertRaises(ValueError):
            self.ve.build_detector_payload("camera", True)

    # -- gating --------------------------------------------------------------
    def test_staff_gating_page(self):
        self.assertEqual(self._page("get", user=STAFF).status_code, 200)
        self.assertEqual(self._page("get", user=ANON).status_code, 302)
        self.assertEqual(self._page("get", user=NONSTAFF).status_code, 302)

    def test_staff_gating_api(self):
        self.assertEqual(self._api("get", user=STAFF)[0], 200)
        self.assertEqual(self._api("get", user=ANON)[0], 302)

    def test_no_csrf_exemption_in_new_module(self):
        path = Path(__file__).resolve().parent / "puppet_console.py"
        source = path.read_text()
        self.assertNotIn("@csrf_exempt", source)

    def test_urls_wired(self):
        from hive import urls

        names = [p.name for p in urls.urlpatterns]
        self.assertIn("puppet_console", names)
        self.assertIn("puppet_console_api", names)

    # -- speak / interrupt / one-at-a-time ------------------------------------
    def test_speak_applies_phonetic_name(self):
        code, body = self._api("post", {"action": "speak", "speech": "Hello Mycroft",
                                        "mood": "happy", "intensity": "0.5"})
        self.assertEqual(code, 200)
        self.assertEqual(body["spoken"], "Hello my croft")
        pubs = _publishes(self.server)
        self.assertEqual(len(pubs), 1)
        self.assertEqual(pubs[0][0], TELEHEALTH)
        self.assertEqual(pubs[0][1]["message"]["output"]["text"], "Hello my croft")
        self.assertIn("Hello my croft",
                      [e["text"] for e in body["transcript"]])

    def test_unknown_gesture_rejected_fail_closed(self):
        code, body = self._api("post", {"action": "gesture", "behaviour": "not_a_tree"})
        self.assertEqual(code, 400)
        self.assertEqual(self.server._client.publish.call_count, 0)

    def test_one_at_a_time_and_complete(self):
        code, _ = self._api("post", {"action": "speak", "speech": "one",
                                     "mood": "happy", "intensity": "0.5"})
        self.assertEqual(code, 200)
        code, _ = self._api("post", {"action": "gesture", "behaviour": "bht_bow"})
        self.assertEqual(code, 400)
        self.assertEqual(self.server._client.publish.call_count, 1)
        code, body = self._api("post", {"action": "complete"})
        self.assertEqual(code, 200)
        self.assertIsNone(body["activity"])
        code, _ = self._api("post", {"action": "gesture", "behaviour": "bht_bow"})
        self.assertEqual(code, 200)
        self.assertEqual(self.server._client.publish.call_count, 2)

    def test_interrupt_preempts_and_clears(self):
        self._api("post", {"action": "speak", "speech": "talking",
                           "mood": "happy", "intensity": "0.5"})
        code, body = self._api("post", {"action": "interrupt"})
        self.assertEqual(code, 200)
        self.assertIsNone(body["activity"])
        pubs = _publishes(self.server)
        self.assertEqual(pubs[-1][1]["message"], {"action": "INTERRUPT"})
        code, _ = self._api("post", {"action": "gesture", "behaviour": "bht_bow"})
        self.assertEqual(code, 200)

    def test_bundled_actions_rejected(self):
        code, _ = self._api("post", {"action": "speak", "actions": "speak,gesture",
                                     "speech": "hi", "mood": "happy", "intensity": "0.5"})
        self.assertEqual(code, 400)
        self.assertEqual(self.server._client.publish.call_count, 0)

    def test_wake_and_sleep(self):
        offline = _make_server(online=False)
        code, _ = self._api("post", {"action": "wake"}, server=offline)
        self.assertEqual(code, 400)
        code, _ = self._api("post", {"action": "wake"})
        self.assertEqual(code, 200)
        self.assertEqual(_publishes(self.server)[-1][0], WAKEUP)
        self._api("post", {"action": "interrupt"})
        code, _ = self._api("post", {"action": "sleep"})
        self.assertEqual(code, 200)
        topic, payload = _publishes(self.server)[-1]
        self.assertEqual(topic, REMOTE_CHAT)
        self.assertEqual(payload["response_actions"][0]["action"], "sleep")

    # -- dance -----------------------------------------------------------------
    def test_dance_requires_opt_in_and_online(self):
        code, _ = self._api("post", {"action": "dance", "tree": "bht_dance_cruncher_short_120bpm"})
        self.assertEqual(code, 400)
        offline = _make_server(online=False)
        code, _ = self._api("post", {"action": "dance", "tree": "bht_dance_cruncher_short_120bpm",
                                     "dance_opt_in": "yes"}, server=offline)
        self.assertEqual(code, 400)
        code, body = self._api("post", {"action": "dance", "tree": "bht_dance_cruncher_short_120bpm",
                                        "dance_opt_in": "yes", "duration": "999"})
        self.assertEqual(code, 200)
        self.assertEqual(body["duration"], 60.0)
        code, _ = self._api("post", {"action": "dance", "tree": "not_a_dance",
                                     "dance_opt_in": "yes"})
        self.assertEqual(code, 400)

    # -- sequence ---------------------------------------------------------------
    def test_sequence_step_complete_advance_and_stop(self):
        code, body = self._api("post", {"action": "sequence_set",
                                        "steps_text": "gesture:bht_bow, look:bht_search"})
        self.assertEqual(code, 200)
        self.assertEqual(self.server._client.publish.call_count, 0)
        code, body = self._api("post", {"action": "sequence_step"})
        self.assertEqual(code, 200)
        self.assertEqual(body["step"], "gesture:bht_bow")
        code, _ = self._api("post", {"action": "sequence_step"})
        self.assertEqual(code, 400)
        code, body = self._api("post", {"action": "complete"})
        self.assertEqual(code, 200)
        self.assertEqual(body["sequence"]["index"], 1)
        code, body = self._api("post", {"action": "sequence_step"})
        self.assertEqual(code, 200)
        self.assertEqual(body["step"], "look:bht_search")
        code, body = self._api("post", {"action": "complete"})
        self.assertEqual(body["sequence"]["status"], "done")
        code, body = self._api("post", {"action": "sequence_stop"})
        self.assertEqual(code, 200)
        self.assertEqual(body["sequence"]["status"], "none")

    def test_sequence_rejects_unknown_and_unopted_dance(self):
        code, _ = self._api("post", {"action": "sequence_set",
                                     "steps_text": "gesture:not_a_tree"})
        self.assertEqual(code, 400)
        code, _ = self._api("post", {"action": "sequence_set",
                                     "steps_text": "dance:bht_dance_cruncher_short_120bpm"})
        self.assertEqual(code, 400)
        code, _ = self._api("post", {"action": "sequence_set",
                                     "steps_text": "dance:bht_dance_cruncher_short_120bpm",
                                     "dance_opt_in": "yes"})
        self.assertEqual(code, 200)

    def test_sequence_stop_clears_and_interrupts(self):
        self._api("post", {"action": "sequence_set", "steps_text": "gesture:bht_bow"})
        self._api("post", {"action": "sequence_step"})
        code, body = self._api("post", {"action": "sequence_stop"})
        self.assertEqual(code, 200)
        self.assertEqual(body["sequence"]["status"], "none")
        self.assertIsNone(body["activity"])
        self.assertEqual(_publishes(self.server)[-1][1]["message"],
                         {"action": "INTERRUPT"})

    # -- mode / auto-exit ---------------------------------------------------------
    def test_mode_puppet_stock_bridge(self):
        device = self._device()
        code, body = self._api("post", {"action": "mode", "mode": "puppet"}, device=device)
        self.assertEqual(code, 200)
        self.assertEqual(device.robot_config.get("moxie_mode"), "TELEHEALTH")
        code, body = self._api("post", {"action": "mode", "mode": "stock"}, device=device)
        self.assertEqual(code, 200)
        self.assertIn("Leaving puppet mode", body["warning"])
        self.assertNotIn("moxie_mode", device.robot_config)
        code, body = self._api("post", {"action": "mode", "mode": "bridge"}, device=device)
        self.assertEqual(code, 200)
        self.assertIn("session only", body["warning"])
        self.assertEqual(body["mode"], "bridge")

    def test_puppet_auto_exits_on_observed_disconnect(self):
        device = self._device(puppet=True)
        code, body = self._api("get", device=device)
        self.assertEqual(code, 200)
        self.assertTrue(body["status"]["puppet_enabled"])
        self.server._robot_data.online = False
        code, body = self._api("get", device=device)
        self.assertEqual(code, 200)
        self.assertFalse(body["status"]["puppet_enabled"])
        self.assertTrue(body["status"]["auto_exited_on_disconnect"])
        self.assertNotIn("moxie_mode", device.robot_config)

    # -- creative / show-me / transcript -------------------------------------------
    def test_story_beat_one_per_click(self):
        code, body = self._api("post", {"action": "story_beat", "text": "Once upon a time.",
                                        "mood": "happy", "intensity": "0.5"})
        self.assertEqual(code, 200)
        self.assertIn("Once upon a time.", [e["text"] for e in body["transcript"]])
        code, _ = self._api("post", {"action": "story_beat", "text": "Second beat.",
                                     "mood": "happy", "intensity": "0.5"})
        self.assertEqual(code, 400)

    def test_show_me_phase_a_no_disk(self):
        # Flow: start (behaviour occupies activity) -> operator marks it
        # complete -> describe (text only, no robot send) -> acknowledge.
        code, body = self._api("post", {"action": "show_me_start"})
        self.assertEqual(code, 200)
        self.assertEqual(body["show_me_state"], "awaiting_show")
        code, _ = self._api("post", {"action": "complete"})
        self.assertEqual(code, 200)
        code, body = self._api("post", {"action": "show_me_describe",
                                        "description": "a red book"})
        self.assertEqual(code, 200)
        self.assertEqual(body["show_me_state"], "received")
        self.assertFalse(body["stored_on_disk"])
        code, body = self._api("post", {"action": "show_me_ack"})
        self.assertEqual(code, 200)
        self.assertEqual(body["show_me_state"], "idle")

    def test_transcript_add_and_clear_session_only(self):
        code, body = self._api("post", {"action": "transcript_add", "text": "hello robot"})
        self.assertEqual(code, 200)
        self.assertEqual(len(body["transcript"]), 1)
        code, body = self._api("post", {"action": "clear_transcript"})
        self.assertEqual(body["transcript"], [])

    def test_favourite_validation(self):
        code, _ = self._api("post", {"action": "favourite", "item": "gesture:not_a_tree"})
        self.assertEqual(code, 400)
        code, body = self._api("post", {"action": "favourite", "item": "gesture:bht_bow"})
        self.assertEqual(code, 200)
        self.assertIn("gesture:bht_bow", body["favourites"])

    # -- vision via console ---------------------------------------------------------
    def test_vision_event_ingest_and_unknown_rejected(self):
        code, body = self._api("post", {"action": "vision_event",
                                        "event": "eb-qr-event", "value": "QR123"})
        self.assertEqual(code, 200)
        self.assertEqual(body["vision_event"]["event"], "eb-qr-event")
        self.assertEqual(body["vision_event"]["value"], "QR123")
        self.assertFalse(body["vision_event"]["is_image"])
        code, _ = self._api("post", {"action": "vision_event",
                                     "event": "eb-not-real", "value": "x"})
        self.assertEqual(code, 400)

    def test_detector_control_builds_payload_without_robot_send(self):
        before = self.server._client.publish.call_count
        code, body = self._api("post", {"action": "detector", "detector": "qr",
                                        "enabled": "true"})
        self.assertEqual(code, 200)
        self.assertEqual(body["function_id"], "eb_enable_qr")
        self.assertEqual(body["subscribed_events"], ["eb-qr-event"])
        self.assertEqual(body["live_delivery"], "unverified")
        self.assertEqual(self.server._client.publish.call_count, before)
        code, _ = self._api("post", {"action": "detector", "detector": "camera",
                                     "enabled": "true"})
        self.assertEqual(code, 400)

    # -- rendered page ---------------------------------------------------------------
    def test_rendered_page_has_console_and_no_fake_controls(self):
        response = self._page("get")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn("Operator-controlled, not autonomous", html)
        self.assertIn("Camera feed: not available", html)
        self.assertIn("Vision Events", html)
        self.assertIn("bht_bow", html)
        self.assertIn("bht_dance_cruncher_short_120bpm", html)
        self.assertIn("Interrupt / Stop", html)
        # No fake camera/driving/joint control elements, ever.
        self.assertNotIn("<video", html)
        self.assertNotIn("<canvas", html)
        self.assertNotIn("<img", html)
        self.assertNotIn('id="camera', html)
        self.assertNotIn('name="drive"', html)
        self.assertNotIn('name="wheel"', html)
        self.assertNotIn('name="joint"', html)
        self.assertNotIn('name="camera"', html)
        self.assertNotIn("camera-feed", html)

    def test_page_post_redirects_and_sends(self):
        response = self._page("post", {"action": "speak", "speech": "hi",
                                       "mood": "happy", "intensity": "0.5"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/hive/puppet_console/1", response["Location"])
        self.assertEqual(self.server._client.publish.call_count, 1)

    def test_status_header_reports_battery_and_unavailable_firmware(self):
        code, body = self._api("get")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"]["battery"], 88)
        self.assertEqual(body["status"]["firmware"], "unavailable")
        self.assertTrue(body["status"]["online"])
