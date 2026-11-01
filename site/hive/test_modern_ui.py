"""Offline modern-UI tests: run from site with the venv python:
    python -m unittest hive.test_modern_ui -v

No broker, robot, network, database or real key is used (the only key
string is the fake "test-key-not-real", and it must never appear in
rendered HTML). Templates are rendered for real through Django's
template engine with an offline urlconf that mounts hive.urls under
its "hive" namespace, exactly like the streaming tests do. View
context helpers are unit-tested directly; the DB-backed view methods
themselves are exercised on the test server (see CHECKPOINT-MODERN-UI.md).
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

FAKE_KEY = "test-key-not-real"

SETUP_FIELD_NAMES = [
    "apikey", "chat_provider", "stt_provider", "stt_streaming_enabled",
    "stt_diarization_enabled", "stt_endpointing_mode",
    "stt_endpointing_timeout_ms", "stt_language_hint", "stt_keyword_bias",
    "stt_code_switching_enabled", "musetranscribeapikey", "musesparkapikey",
    "muse_spark_model", "googleapikey", "hostname", "adminUser",
    "adminPassword",
]
STAFF = SimpleNamespace(is_active=True, is_staff=True, is_authenticated=True)
ANON = SimpleNamespace(is_active=False, is_staff=False, is_authenticated=False)


class _BaseCase(unittest.TestCase):
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
                    "OPTIONS": {"context_processors": [
                        "django.template.context_processors.request",
                        "django.template.context_processors.csrf",
                        "django.contrib.auth.context_processors.auth",
                    ]},
                }],
                SECRET_KEY="offline-modern-ui-tests",
                ALLOWED_HOSTS=["testserver"],
                ROOT_URLCONF="hive.urls",
                STATIC_URL="/static/",
            )
        django.setup()
        import types
        from django.urls import include, path

        if "offline_modern_urlconf" not in sys.modules:
            urlconf = types.ModuleType("offline_modern_urlconf")
            urlconf.urlpatterns = [path("hive/", include("hive.urls"))]
            sys.modules["offline_modern_urlconf"] = urlconf
        from hive import models, views

        cls.models = models
        cls.views = views
        cls.hive_dir = Path(models.__file__).parent

    def _render(self, template, context):
        from django.template.loader import render_to_string
        from django.test import RequestFactory, override_settings

        request = RequestFactory().get("/hive/")
        request.user = ANON
        ctx = {"moxie_version": "test"}
        ctx.update(context)
        with override_settings(ROOT_URLCONF="offline_modern_urlconf"):
            return render_to_string(template, ctx, request=request)

    def _template_source(self, name):
        return (self.hive_dir / "templates" / name).read_text(encoding="utf-8")


class BaseTemplateTests(_BaseCase):
    def test_base_has_modern_shell_and_no_cdn_or_bootstrap(self):
        html = self._render("base.html", {})
        self.assertIn('lang="en"', html)
        self.assertIn('name="viewport"', html)
        self.assertIn("hive/modern.css", html)
        self.assertIn('href="#main"', html)
        self.assertIn("Dashboard", html)
        self.assertIn("Setup", html)
        self.assertIn("Logs", html)
        self.assertNotIn("bootstrap", html.lower())
        self.assertNotIn("unpkg", html)
        self.assertNotIn("cdn.jsdelivr", html)

    def test_modern_css_exists_served_path_and_legacy_copy_match(self):
        modern = self.hive_dir / "static" / "hive" / "modern.css"
        self.assertTrue(modern.exists())
        css = modern.read_text(encoding="utf-8")
        for token in (":root", "prefers-color-scheme", "focus-visible",
                      ".card-grid", ".log-box", ".btn-primary", ".status-dot"):
            self.assertIn(token, css, token)
        self.assertNotIn("@import", css)
        self.assertNotIn("http://", css)
        self.assertNotIn("https://", css)
        # Served copy: openmoxie/urls.py serves /static/ from STATIC_ROOT
        # only, so this is the file the browser actually receives.
        served = self.hive_dir.parent / "static" / "hive" / "modern.css"
        self.assertTrue(served.exists())
        self.assertEqual(served.read_text(encoding="utf-8"), css)
        legacy = self.hive_dir.parent / "static" / "hive" / "style.css"
        self.assertTrue(legacy.exists())
        self.assertEqual(legacy.read_text(encoding="utf-8"), css)

    def test_no_template_loads_a_cdn_script(self):
        for path in sorted((self.hive_dir / "templates").rglob("*.html")):
            src = path.read_text(encoding="utf-8")
            self.assertNotIn("unpkg", src, path.name)
            self.assertNotIn("pretty-json", src, path.name)
            self.assertNotIn("cdn.jsdelivr", src, path.name)


class DashboardTests(_BaseCase):
    def _dashboard_context(self):
        device = SimpleNamespace(
            pk=1, device_id="d_online", name="Moxie",
            last_connect=None, last_disconnect=None, is_paired=True,
            schedule="Weekdays",
            robot_config=SimpleNamespace(wake_button_enabled=True),
        )
        convo = SimpleNamespace(pk=7, module_id="mod1", content_id="c1")
        convo.__str__ = lambda self: "Chat One"  # noqa: E731
        return {
            "alert": "Hello",
            "stt_status": {
                "provider": "muse_streaming", "mode": "streaming",
                "fell_back": False, "streaming_enabled": True,
                "diarization_enabled": False, "code_switching_enabled": True,
                "endpointing_mode": "auto", "endpointing_timeout_ms": 1000,
                "language_hint": "auto",
            },
            "chat_status": {
                "provider_label": "Muse Spark (Meta)",
                "model_label": "muse-spark-1.3-contributor",
            },
            "recent_devices": [device],
            "live": ["d_online"],
            "device_cards": [{
                "pk": 1, "name": "Moxie", "status_label": "Online",
                "status_class": "online", "robot_mode": None,
                "firmware": "24.10.803", "battery": 88,
                "mode_label": "Stock", "is_paired": True, "can_wake": True,
            }],
            "schedules": [SimpleNamespace(name="Weekdays")],
            "conversations": [convo],
        }

    def test_dashboard_renders_status_cards_and_all_routes(self):
        html = self._render("hive/dashboard.html", self._dashboard_context())
        for token in ("Muse Spark (Meta)", "muse-spark-1.3-contributor",
                      "muse_streaming", "streaming", "24.10.803", "Online",
                      "Migration QR Code", "Upload for Review", "Hello"):
            self.assertIn(token, html, token)
        for href in ("/hive/endpoint/", "/hive/wifi_edit/", "/hive/setup",
                     "/hive/logs/", "/hive/reload_database",
                     "/hive/export_content/", "/hive/import_review/",
                     "/hive/moxie/1", "/hive/moxie_missions/1",
                     "/hive/face/1", "/hive/puppet/1", "/hive/moxie_data/1",
                     "/hive/moxie_wake/1", "/hive/interact/7"):
            self.assertIn(href, html, href)
        self.assertIn('name="json_file"', html)
        self.assertIn('<label for="json_file"', html)

    def test_dashboard_empty_states_are_plain_language(self):
        html = self._render("hive/dashboard.html", {
            "recent_devices": [], "live": [], "device_cards": [],
            "schedules": [], "conversations": [],
        })
        self.assertIn("No Moxie devices have connected yet", html)
        self.assertIn("No schedules configured yet", html)
        self.assertIn("No conversations configured yet", html)
        self.assertIn("No configuration saved yet", html)

    def test_device_status_card_derivation_is_honest(self):
        card = self.views._device_status_card
        online = SimpleNamespace(
            pk=1, device_id="d1", name="M", is_paired=True,
            state={"mode": "awake", "robot_firmware_version": "24.10.803",
                   "battery_level": 88},
            robot_config={"moxie_mode": "TELEHEALTH", "wake_button_enabled": True})
        c = card(online, ["d1"])
        self.assertEqual((c["status_label"], c["status_class"]), ("Online", "online"))
        self.assertEqual(c["mode_label"], "Puppet")
        self.assertTrue(c["can_wake"])
        sleeping = SimpleNamespace(
            pk=2, device_id="d2", name=None, is_paired=False,
            state={"mode": "sleep"}, robot_config={})
        c = card(sleeping, [])
        self.assertEqual((c["status_label"], c["status_class"]), ("Sleep", "sleep"))
        self.assertEqual(c["firmware"], "Unavailable")
        self.assertEqual(c["battery"], "Unavailable")
        self.assertEqual(c["mode_label"], "Stock")
        self.assertFalse(c["can_wake"])
        standby = SimpleNamespace(
            pk=3, device_id="d3", name="M3", is_paired=True,
            state={"mode": "standby"}, robot_config=None)
        self.assertEqual(card(standby, [])["status_label"], "Standby")
        unknown = SimpleNamespace(
            pk=4, device_id="d4", name="M4", is_paired=True,
            state=None, robot_config=None)
        self.assertEqual(card(unknown, [])["status_label"], "Offline")

    def test_chat_status_labels(self):
        self.assertIsNone(self.views._chat_status(None))
        cfg = SimpleNamespace(chat_provider="openai", muse_spark_model="x")
        self.assertEqual(self.views._chat_status(cfg)["provider_label"], "GPT (OpenAI)")
        cfg = SimpleNamespace(chat_provider="muse_spark",
                              muse_spark_model="muse-spark-1.3")
        chat = self.views._chat_status(cfg)
        self.assertEqual(chat["provider_label"], "Muse Spark (Meta)")
        self.assertEqual(chat["model_label"], "muse-spark-1.3")


class SetupTests(_BaseCase):
    def test_setup_renders_every_field_and_no_key_value(self):
        cfg = self.models.HiveConfiguration()
        cfg.stt_provider = "muse_streaming"
        cfg.stt_streaming_enabled = True
        cfg.stt_diarization_enabled = True
        cfg.stt_code_switching_enabled = True
        cfg.stt_language_hint = "fr"
        cfg.stt_keyword_bias = "Moxie"
        cfg.stt_endpointing_timeout_ms = 2500
        cfg.stt_endpointing_mode = "server_timeout"
        cfg.chat_provider = "muse_spark"
        cfg.muse_spark_model = "muse-spark-1.3-contributor"
        cfg.openai_api_key = FAKE_KEY
        cfg.muse_spark_api_key = FAKE_KEY
        cfg.muse_transcribe_api_key = FAKE_KEY
        cfg.google_api_key = FAKE_KEY
        html = self._render("hive/setup.html",
                            {"object": cfg, "needs_admin": True})
        for name in SETUP_FIELD_NAMES:
            self.assertIn('name="%s"' % name, html, name)
        for token in ('value="muse_streaming" selected',
                      'value="2500"', "Moxie", 'value="server_timeout" selected',
                      'value="fr" selected',
                      'value="muse-spark-1.3-contributor" selected',
                      'name="adminUser"', 'name="adminPassword"',
                      'action="/hive/hive_configure/"',
                      'id="hive_edit"', "document.getElementById"):
            self.assertIn(token, html, token)
        # Presence only: a stored key must never be echoed into the page.
        self.assertNotIn(FAKE_KEY, html)
        self.assertIn("key configured", html)

    def test_setup_source_has_label_for_every_control(self):
        src = self._template_source("hive/setup.html")
        for field in ("apikey", "chat_provider", "stt_provider",
                      "stt_streaming_enabled", "stt_diarization_enabled",
                      "stt_endpointing_mode", "stt_endpointing_timeout_ms",
                      "stt_language_hint", "stt_keyword_bias",
                      "stt_code_switching_enabled", "musetranscribeapikey",
                      "musesparkapikey", "muse_spark_model", "googleapikey",
                      "hostname"):
            self.assertIn('for="%s"' % field, src, field)

    def test_hive_configure_still_persists_all_knobs(self):
        from django.test import RequestFactory

        cfg = SimpleNamespace(
            openai_api_key="", google_api_key="", external_host="",
            allow_unverified_bots=False, common_config={"x": 1},
            common_settings={"y": 2}, chat_provider="openai",
            muse_spark_api_key="", muse_spark_model="muse-spark-1.3",
            stt_provider="openai", muse_transcribe_api_key="",
            stt_streaming_enabled=False, stt_diarization_enabled=False,
            stt_endpointing_mode="auto", stt_endpointing_timeout_ms=1000,
            stt_language_hint="auto", stt_keyword_bias="",
            stt_code_switching_enabled=False, save=Mock())
        user_model = Mock()
        user_model.objects.filter.return_value.exists.return_value = True
        request = RequestFactory().post("/setup", data={
            "apikey": "", "googleapikey": "", "hostname": "testhost",
            "chat_provider": "muse_spark",
            "muse_spark_model": "muse-spark-1.3-contributor",
            "stt_provider": "muse_streaming",
            "stt_streaming_enabled": "on",
            "stt_diarization_enabled": "on",
            "stt_endpointing_mode": "server_timeout",
            "stt_endpointing_timeout_ms": "99999",
            "stt_language_hint": "fr",
            "stt_keyword_bias": "Moxie",
            "stt_code_switching_enabled": "on",
        })
        with patch.object(self.views, "HiveConfiguration") as mock_model, \
             patch.object(self.views, "get_instance"), \
             patch.object(self.views, "get_user_model", return_value=user_model), \
             patch.object(self.views, "reverse", return_value="/dashboard/"):
            mock_model.objects.get_or_create.return_value = (cfg, False)
            response = self.views.hive_configure(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(cfg.chat_provider, "muse_spark")
        self.assertEqual(cfg.muse_spark_model, "muse-spark-1.3-contributor")
        self.assertEqual(cfg.stt_provider, "muse_streaming")
        self.assertTrue(cfg.stt_streaming_enabled)
        self.assertTrue(cfg.stt_diarization_enabled)
        self.assertTrue(cfg.stt_code_switching_enabled)
        self.assertEqual(cfg.stt_endpointing_timeout_ms, 5000)  # clamped
        self.assertEqual(cfg.stt_language_hint, "fr")
        self.assertEqual(cfg.stt_keyword_bias, "Moxie")


class LogsTests(_BaseCase):
    def test_logs_renders_filters_and_empty_state(self):
        files_data = [
            {"label": "Server log", "filename": "debug.log", "exists": True,
             "lines": [{"text": "hello", "failure": False, "ota": False}]},
            {"label": "Broker log", "filename": "mosquitto.log", "exists": True,
             "lines": []},
        ]
        html = self._render("hive/logs.html", {
            "files_data": files_data,
            "allowed_files": ["debug.log", "mosquitto.log"],
            "filters": {"file": "", "device": "", "tag": "",
                        "level": "", "q": ""},
            "lines_requested": 200,
        })
        for token in ('name="file"', 'name="device"', 'name="tag"',
                      'name="level"', 'name="q"', 'name="lines"',
                      'id="log_filters"', 'id="auto_refresh"',
                      'id="refresh_now"', "hive/logs_api", "hello",
                      "No log lines matched"):
            self.assertIn(token, html, token)

    def test_logs_gate_unchanged_and_no_new_csrf_exemption(self):
        from django.http import HttpResponse
        from django.test import RequestFactory

        def request_for(user):
            request = RequestFactory().get("/logs/")
            request.user = user
            return request

        with patch.object(self.views, "render",
                          return_value=HttpResponse("logs page")):
            self.assertEqual(self.views.logs_view(request_for(STAFF)).status_code, 200)
        anon_response = self.views.logs_view(request_for(ANON))
        self.assertEqual(anon_response.status_code, 302)
        self.assertIn("/admin/login/", anon_response["Location"])
        # Stock gating, unchanged: setup/dashboard are LAN bootstrap
        # pages and were NOT staff-gated before this branch either.
        # This branch does not weaken logs and does not newly gate
        # setup/dashboard (flagged honestly in the checkpoint).
        for view in (self.views.SetupView, self.views.DashboardView):
            self.assertFalse(
                any("staff" in getattr(b, "__name__", "").lower()
                    for b in view.__mro__))
        self.assertFalse(getattr(self.views.logs_view, "csrf_exempt", False))
        self.assertFalse(getattr(self.views.logs_api, "csrf_exempt", False))
        self.assertFalse(getattr(self.views.hive_configure, "csrf_exempt", False))


class OtherPagesTests(_BaseCase):
    def test_no_child_template_repeats_the_header(self):
        for path in sorted((self.hive_dir / "templates" / "hive").glob("*.html")):
            src = path.read_text(encoding="utf-8")
            if "extends 'base.html'" in src:
                self.assertNotIn("moxheader", src, path.name)

    def test_secondary_pages_render(self):
        moxie = self._render("hive/moxie.html", {
            "object": SimpleNamespace(
                pk=1, name="Moxie", device_id="d1", is_paired=True,
                schedule_id=None, last_connect=None, last_disconnect=None,
                state={"robot_firmware_version": "24.10.803",
                       "battery_level": 88, "mode": "awake"}),
            "schedules": [],
            "active_config": {"child_pii": {"nickname": "Nick"},
                              "screen_brightness": 0.5, "audio_volume": 0.5},
        })
        for token in ('name="moxie_name"', 'name="pairing_status"',
                      'name="nickname"', 'name="schedule"',
                      'name="screen_brightness"', 'name="audio_volume"',
                      "24.10.803"):
            self.assertIn(token, moxie, token)
        wifi = self._render("hive/wifi.html", {})
        for token in ('name="ssid"', 'name="password"',
                      'name="frequency"', 'name="hidden"'):
            self.assertIn(token, wifi, token)
        face = self._render("hive/face.html", {
            "object": SimpleNamespace(pk=1, name="Moxie", device_id="d1"),
            "assets": [{"layer": "Eyes",
                        "labels": [{"label": "a", "name": "A"}]}],
            "face_options": [],
        })
        self.assertIn('name="asset_Eyes"', face)
        self.assertIn('name="child_recover"', face)
        interact = self._render("hive/interact.html", {
            "object": SimpleNamespace(module_id="mod1", content_id="c1"),
            "token": "tok",
        })
        self.assertIn('name="user_input"', interact)
        puppet = self._render("hive/puppet.html", {
            "object": SimpleNamespace(pk=1, name="Moxie", device_id="d1"),
        })
        for token in ('name="mood"', 'name="intensity"',
                      'name="user_input"', 'id="puppet_button"'):
            self.assertIn(token, puppet, token)

    def test_secondary_field_names_survive_in_source(self):
        src = lambda name: self._template_source("hive/%s" % name)
        for token in ("conversations", "globals", "schedules"):
            self.assertIn('name="%s"' % token, src("export.html"), token)
        for token in ("conversations", "globals", "schedules", "json_data"):
            self.assertIn('name="%s"' % token, src("import.html"), token)
        self.assertIn('name="mission_sets"', src("missions.html"))
        self.assertIn("json-view", src("moxie_data.html"))
        self.assertIn("json-view", src("import.html"))


class HostingTests(_BaseCase):
    def test_compose_hardening_without_behaviour_change(self):
        compose = (self.hive_dir.parent.parent / "docker-compose.yml"
                   ).read_text(encoding="utf-8")
        for token in ("healthcheck", "restart: unless-stopped",
                      "openmoxie-server:${OPENMOXIE_VERSION:-mycroft-modern-ui}",
                      "openmoxie-mqtt:${OPENMOXIE_VERSION:-mycroft-modern-ui}",
                      '"8001:8000"', '"8883:8883"',
                      "./local/work:/app/site/work",
                      "./local/work:/mosquitto/log"):
            self.assertIn(token, compose, token)
        self.assertNotIn("${OPENMOXIE_VERSION:-latest}", compose)
        self.assertNotIn("condition: service_healthy", compose)


if __name__ == "__main__":
    unittest.main()
