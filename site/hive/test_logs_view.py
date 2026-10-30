"""Offline read-only logs monitor tests: run from site with the venv python:
    python -m unittest hive.test_logs_view -v

No broker, robot, network, database or live log file is touched: log files
are throwaway fixtures in a temp dir and the views are driven with
RequestFactory.

There is no view-test harness in this project: existing hive view tests
(test_control_commands, test_muse_spark) configure minimal Django settings
without template directories and call view functions directly. Following
that pattern, logs_view is tested with render mocked to capture template +
context, while logs_api is exercised unmocked for real JSON responses.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


STAFF = SimpleNamespace(is_active=True, is_staff=True, is_authenticated=True)
NONSTAFF = SimpleNamespace(is_active=True, is_staff=False, is_authenticated=True)
ANON = SimpleNamespace(is_active=False, is_staff=False, is_authenticated=False)

SAMPLE_DEBUG = [
    "2026-10-29 INFO [moxie_server] connected d_abc123",
    "2026-10-29 DEBUG [device_logs] d_abc123 BoUpdater checking for update",
    "2026-10-29 ERROR [device_logs] d_abc123 BoUpdater download failed: 404 timeout",
    "2026-10-29 INFO [moxie_server] heartbeat d_xyz789",
]
SAMPLE_MOSQUITTO = [
    "1700000000: Received PUBLISH from d_abc123 (d0, q0, r0, m0, '/devices/d_abc123/state')",
    "1700000001: Received PINGREQ from d_xyz789",
]


class TailReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import django
        from django.conf import settings

        if not settings.configured:
            settings.configure(
                INSTALLED_APPS=["hive"],
                TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates"}],
                SECRET_KEY="offline-logs-view-tests",
                ALLOWED_HOSTS=["testserver"],
            )
        django.setup()
        if "testserver" not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["testserver"]
        from hive import logtail

        cls.logtail = logtail

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, lines):
        (self.work / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_fixed_filenames_only(self):
        for bad in ("../debug.log", "/etc/passwd", "..\\..\\debug.log",
                    "other.log", "debug.log/extra", "subdir/debug.log",
                    "", "DEBUG.LOG", " debug.log", "debug.log ", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.logtail.resolve_log_path(bad, work_dir=self.work)
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.logtail.get_log_tail(bad, work_dir=self.work)
        for good in ("debug.log", "mosquitto.log"):
            path = self.logtail.resolve_log_path(good, work_dir=self.work)
            self.assertEqual(path, self.work / good)

    def test_missing_file_handled_gracefully(self):
        result = self.logtail.get_log_tail("debug.log", work_dir=self.work)
        self.assertFalse(result["exists"])
        self.assertEqual(result["lines"], [])
        both = self.logtail.collect_logs(work_dir=self.work)
        self.assertEqual(both["debug.log"]["lines"], [])
        self.assertEqual(both["mosquitto.log"]["lines"], [])

    def test_tail_cap_and_order(self):
        self._write("debug.log", [f"line-{i}" for i in range(500)])
        tail = self.logtail.read_last_lines(self.work / "debug.log", 200)
        self.assertEqual(len(tail), 200)
        self.assertEqual(tail[0], "line-300")
        self.assertEqual(tail[-1], "line-499")
        result = self.logtail.get_log_tail("debug.log", count=5, work_dir=self.work)
        self.assertEqual([ln["text"] for ln in result["lines"]],
                         [f"line-{i}" for i in range(495, 500)])

    def test_absolute_cap_respected_for_huge_request(self):
        self._write("debug.log", [f"line-{i}" for i in range(3000)])
        result = self.logtail.get_log_tail("debug.log", count=999999, work_dir=self.work)
        self.assertLessEqual(len(result["lines"]), self.logtail.MAX_TAIL_LINES)
        self.assertEqual(len(result["lines"]), self.logtail.MAX_TAIL_LINES)
        self.assertEqual(result["lines"][-1]["text"], "line-2999")

    def test_filters_device_tag_level_and_text(self):
        self._write("debug.log", SAMPLE_DEBUG)
        self._write("mosquitto.log", SAMPLE_MOSQUITTO)
        by_tag = self.logtail.get_log_tail("debug.log", tag="boupdater", work_dir=self.work)
        self.assertEqual(len(by_tag["lines"]), 2)
        by_device = self.logtail.get_log_tail("debug.log", device="d_xyz789", work_dir=self.work)
        self.assertEqual([ln["text"] for ln in by_device["lines"]], [SAMPLE_DEBUG[3]])
        by_level = self.logtail.get_log_tail("debug.log", level="error", work_dir=self.work)
        self.assertEqual([ln["text"] for ln in by_level["lines"]], [SAMPLE_DEBUG[2]])
        combined = self.logtail.get_log_tail(
            "debug.log", device="d_abc123", tag="boupdater", q="checking", work_dir=self.work)
        self.assertEqual([ln["text"] for ln in combined["lines"]], [SAMPLE_DEBUG[1]])
        mqtt = self.logtail.get_log_tail("mosquitto.log", q="pingreq", work_dir=self.work)
        self.assertEqual([ln["text"] for ln in mqtt["lines"]], [SAMPLE_MOSQUITTO[1]])

    def test_classify_ota_and_failure_markers(self):
        cls = self.logtail.classify_line
        self.assertTrue(cls("BoUpdater starting")["ota"])
        self.assertTrue(cls("OTA update available")["ota"])
        self.assertFalse(cls("total bytes rotated")["ota"])  # no substring accidents
        for marker in ("timeout", "404", "checksum mismatch", "bad signature",
                       "corrupt image", "download failed", "unknown ca"):
            self.assertTrue(cls(f"BoUpdater {marker}")["failure"], marker)
        plain = cls("heartbeat ok")
        self.assertFalse(plain["ota"])
        self.assertFalse(plain["failure"])

    def test_parse_line_count(self):
        parse = self.logtail.parse_line_count
        self.assertEqual(parse(None), self.logtail.DEFAULT_TAIL_LINES)
        self.assertEqual(parse(""), self.logtail.DEFAULT_TAIL_LINES)
        self.assertEqual(parse("not-a-number"), self.logtail.DEFAULT_TAIL_LINES)
        self.assertEqual(parse("50"), 50)
        self.assertEqual(parse(50), 50)
        self.assertEqual(parse("0"), 1)
        self.assertEqual(parse("-3"), 1)
        self.assertEqual(parse("999999"), self.logtail.MAX_TAIL_LINES)


class LogsViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import django
        from django.conf import settings

        if not settings.configured:
            settings.configure(
                INSTALLED_APPS=["hive"],
                TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates"}],
                SECRET_KEY="offline-logs-view-tests",
                ALLOWED_HOSTS=["testserver"],
                ROOT_URLCONF="hive.urls",
            )
        django.setup()
        if "testserver" not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["testserver"]
        if not getattr(settings, "ROOT_URLCONF", None):
            # The auth decorator resolves its login URL through the URL
            # resolver; other hive tests configure no ROOT_URLCONF.
            settings.ROOT_URLCONF = "hive.urls"
            from django.urls import clear_url_caches

            clear_url_caches()
        from hive import logtail, views

        cls.logtail = logtail
        cls.views = views

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)
        (self.work / "debug.log").write_text("\n".join(SAMPLE_DEBUG) + "\n", encoding="utf-8")
        (self.work / "mosquitto.log").write_text("\n".join(SAMPLE_MOSQUITTO) + "\n", encoding="utf-8")
        patcher = patch.object(self.logtail, "default_work_dir", return_value=self.work)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _request(self, user, path="/logs/", params=None):
        from django.test import RequestFactory

        request = RequestFactory().get(path, data=params or {})
        request.user = user
        return request

    def test_logs_view_200_with_logs_context(self):
        from django.http import HttpResponse

        captured = {}

        def fake_render(request, template, context):
            captured["template"] = template
            captured["context"] = context
            return HttpResponse("logs page")

        with patch.object(self.views, "render", fake_render):
            response = self.views.logs_view(self._request(STAFF))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["template"], "hive/logs.html")
        context = captured["context"]
        self.assertEqual(context["lines_requested"], 200)
        debug_lines = [ln["text"] for ln in context["files"]["debug.log"]["lines"]]
        self.assertEqual(debug_lines, SAMPLE_DEBUG)
        self.assertEqual([ln["text"] for ln in context["files"]["mosquitto.log"]["lines"]],
                         SAMPLE_MOSQUITTO)

    def test_views_require_staff_login(self):
        from django.test import override_settings

        # The staff gate's redirect imports django.contrib.auth.views,
        # whose models need the auth/contenttypes apps. Production
        # settings install them; the minimal offline hive test settings
        # do not, so install them for this check only.
        apps = ["django.contrib.contenttypes", "django.contrib.auth", "hive"]
        with override_settings(INSTALLED_APPS=apps):
            for user in (ANON, NONSTAFF):
                page = self.views.logs_view(self._request(user))
                self.assertIn(page.status_code, (302, 403))
                if page.status_code == 302:
                    self.assertIn("/admin/login/", page["Location"])
                api = self.views.logs_api(self._request(user, path="/logs_api/"))
                self.assertIn(api.status_code, (302, 403))

    def test_logs_api_json_and_highlight_flags(self):
        response = self.views.logs_api(self._request(STAFF, path="/logs_api/"))
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["lines_requested"], 200)
        debug = payload["files"]["debug.log"]
        self.assertTrue(debug["exists"])
        self.assertTrue(debug["lines"][1]["ota"])
        self.assertTrue(debug["lines"][2]["ota"])
        self.assertTrue(debug["lines"][2]["failure"])
        self.assertFalse(debug["lines"][0]["failure"])

    def test_logs_api_missing_files_still_200(self):
        (self.work / "debug.log").unlink()
        (self.work / "mosquitto.log").unlink()
        response = self.views.logs_api(self._request(STAFF, path="/logs_api/"))
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertFalse(payload["files"]["debug.log"]["exists"])
        self.assertEqual(payload["files"]["debug.log"]["lines"], [])

    def test_logs_api_rejects_non_fixed_filename(self):
        for bad in ("../../etc/passwd", "/etc/passwd", "other.log", "../debug.log"):
            response = self.views.logs_api(
                self._request(STAFF, path="/logs_api/", params={"file": bad}))
            self.assertEqual(response.status_code, 400, bad)
        ok = self.views.logs_api(
            self._request(STAFF, path="/logs_api/", params={"file": "debug.log"}))
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(list(json.loads(ok.content)["files"]), ["debug.log"])

    def test_logs_api_line_cap_and_filters(self):
        (self.work / "debug.log").write_text(
            "\n".join(f"entry-{i} d_abc123" for i in range(300)) + "\n", encoding="utf-8")
        response = self.views.logs_api(self._request(
            STAFF, path="/logs_api/", params={"file": "debug.log", "lines": "3"}))
        payload = json.loads(response.content)
        self.assertLessEqual(len(payload["files"]["debug.log"]["lines"]), 3)
        self.assertEqual(payload["files"]["debug.log"]["lines"][-1]["text"], "entry-299 d_abc123")
        filtered = self.views.logs_api(self._request(
            STAFF, path="/logs_api/", params={"file": "debug.log", "tag": "BoUpdater"}))
        self.assertEqual(json.loads(filtered.content)["files"]["debug.log"]["lines"], [])

    def test_views_are_not_csrf_exempt(self):
        self.assertFalse(getattr(self.views.logs_view, "csrf_exempt", False))
        self.assertFalse(getattr(self.views.logs_api, "csrf_exempt", False))

    def test_routes_resolve(self):
        from django.urls import resolve

        # No root URLconf in the minimal offline settings, so resolve
        # against hive.urls directly (production mounts it at /hive/).
        logs_match = resolve("/logs/", urlconf="hive.urls")
        self.assertIs(logs_match.func, self.views.logs_view)
        self.assertEqual(logs_match.url_name, "logs")
        api_match = resolve("/logs_api/", urlconf="hive.urls")
        self.assertIs(api_match.func, self.views.logs_api)
        self.assertEqual(api_match.url_name, "logs_api")

    def test_template_and_dashboard_link(self):
        hive_dir = Path(self.logtail.__file__).parent
        template = hive_dir / "templates" / "hive" / "logs.html"
        self.assertTrue(template.exists())
        self.assertIn("hive:logs_api", template.read_text(encoding="utf-8"))
        dashboard = (hive_dir / "templates" / "hive" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("hive:logs", dashboard)


if __name__ == "__main__":
    unittest.main()
