"""Offline Muse Spark provider tests: run from site with the venv python:
    python -m unittest hive.test_muse_spark -v

All clients are mocked; no network, broker, robot, database or real key is
used. The only key value in this file is the fake "test-key-not-real".
"""
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


def _client(content="Hello from the mock!"):
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    return client


class MuseSparkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import django
        from django.conf import settings

        if not settings.configured:
            settings.configure(
                INSTALLED_APPS=["hive"],
                TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates"}],
                SECRET_KEY="offline-muse-spark-tests",
            )
        django.setup()
        from hive.mqtt import ai_factory, conversations
        from hive import models, views

        cls.factory = ai_factory
        cls.conv = conversations
        cls.models = models
        cls.views = views

    def setUp(self):
        self._saved = (
            self.factory._OPENAPI_KEY,
            self.factory._MUSE_SPARK_KEY,
            self.factory.get_global_chat_provider(),
        )
        self.factory.set_openai_key("test-key-not-real")
        self.factory.set_muse_spark_key(None)
        self.factory.set_global_chat_provider("openai")
        self.addCleanup(self._restore_factory)

    def _restore_factory(self):
        openai_key, spark_key, provider = self._saved
        self.factory.set_openai_key(openai_key)
        self.factory.set_muse_spark_key(spark_key)
        self.factory.set_global_chat_provider(provider)

    # -- ai_factory constructors -------------------------------------------

    def test_openai_factory_no_base_url_with_timeout(self):
        with patch.object(self.factory, "OpenAI") as mock_openai:
            self.factory.create_openai()
        _, kwargs = mock_openai.call_args
        self.assertEqual(kwargs["api_key"], "test-key-not-real")
        self.assertEqual(kwargs["timeout"], 20.0)
        self.assertNotIn("base_url", kwargs)

    def test_spark_factory_base_url_timeout_and_key(self):
        self.factory.set_muse_spark_key("test-key-not-real")
        with patch.object(self.factory, "OpenAI") as mock_openai:
            self.factory.create_muse_spark()
        _, kwargs = mock_openai.call_args
        self.assertEqual(kwargs["api_key"], "test-key-not-real")
        self.assertEqual(kwargs["base_url"], "https://api.meta.ai/v1")
        self.assertEqual(kwargs["timeout"], 20.0)

    def test_factory_chat_client_dispatch(self):
        with patch.object(self.factory, "create_openai", return_value="openai-client") as mock_o, \
             patch.object(self.factory, "create_muse_spark", return_value="spark-client") as mock_s:
            self.assertEqual(self.factory.create_chat_client("openai"), "openai-client")
            self.assertEqual(self.factory.create_chat_client("muse_spark"), "spark-client")
            self.assertEqual(self.factory.create_chat_client(self.models.AIVendor.OPEN_AI), "openai-client")
            self.assertEqual(self.factory.create_chat_client(self.models.AIVendor.MUSE_SPARK), "spark-client")
            self.assertEqual(self.factory.create_chat_client(1), "openai-client")
            self.assertEqual(self.factory.create_chat_client(2), "spark-client")
        self.assertEqual(mock_o.call_count, 3)
        self.assertEqual(mock_s.call_count, 3)

    def test_unknown_provider_rejected(self):
        for bad in ("bogus", 99, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.factory.normalize_provider(bad)

    # -- provider resolution (Stage 1 B.1) ----------------------------------

    def test_resolve_gpt_default_with_no_config(self):
        self.factory.set_openai_key(None)
        self.assertEqual(self.factory.resolve_chat_provider(), "openai")
        self.assertEqual(self.factory.resolve_chat_provider(vendor=1), "openai")

    def test_resolve_global_spark_with_key(self):
        self.factory.set_muse_spark_key("test-key-not-real")
        self.factory.set_global_chat_provider("muse_spark")
        self.assertEqual(self.factory.resolve_chat_provider(), "muse_spark")

    def test_resolve_vendor_override_beats_global(self):
        self.factory.set_muse_spark_key("test-key-not-real")
        # Global OpenAI, conversation pinned to Spark.
        self.assertEqual(self.factory.resolve_chat_provider(vendor=2), "muse_spark")
        # Global Spark, conversation pinned to OpenAI (both keys present).
        self.factory.set_global_chat_provider("muse_spark")
        self.assertEqual(self.factory.resolve_chat_provider(vendor=1), "openai")

    def test_resolve_vendor_without_key_uses_keyed_global(self):
        # Selection is by configured key (B.1); this is not error-time fallback.
        self.assertEqual(self.factory.resolve_chat_provider(vendor=2), "openai")

    # -- request shaping -----------------------------------------------------

    def test_spark_completion_options(self):
        opts = self.conv.completion_options_for_provider("muse_spark", "muse-spark-1.3", 70, 0.5)
        self.assertEqual(opts, {"max_completion_tokens": 70, "reasoning_effort": "low"})
        self.assertEqual(self.conv.MUSE_SPARK_REASONING_EFFORT, "low")

    def test_openai_options_delegate_exactly(self):
        for model in ("gpt-6-luna", "gpt-3.5-turbo", "o3-mini"):
            with self.subTest(model=model):
                self.assertEqual(
                    self.conv.completion_options_for_provider("openai", model, 70, 0.5),
                    self.conv.completion_options(model, 70, 0.5),
                )

    def test_spark_next_response_uses_spark_client(self):
        self.factory.set_muse_spark_key("test-key-not-real")
        client = _client()
        with patch.object(self.conv, "create_muse_spark", return_value=client) as mock_spark, \
             patch.object(self.conv, "create_openai") as mock_openai:
            session = self.conv.SingleContextChatSession(
                model="muse-spark-1.3", max_tokens=70, temperature=0.5, provider="muse_spark")
            text, overflow = session.next_response("Hello", [])
        self.assertEqual((text, overflow), ("Hello from the mock!", False))
        mock_spark.assert_called_once_with()
        mock_openai.assert_not_called()
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "muse-spark-1.3")
        self.assertEqual(kwargs["max_completion_tokens"], 70)
        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertNotIn("max_tokens", kwargs)
        self.assertNotIn("temperature", kwargs)

    def test_default_session_uses_openai_client(self):
        client = _client()
        with patch.object(self.conv, "create_openai", return_value=client) as mock_openai, \
             patch.object(self.conv, "create_muse_spark") as mock_spark:
            session = self.conv.SingleContextChatSession(model="gpt-6-luna")
            self.assertEqual(session.provider, "openai")
            session.next_response("Hello", [])
        mock_openai.assert_called_once_with()
        mock_spark.assert_not_called()

    def test_spark_summarize_keeps_caller_max_tokens(self):
        self.factory.set_muse_spark_key("test-key-not-real")
        client = _client("a summary")
        with patch.object(self.conv, "create_muse_spark", return_value=client):
            session = self.conv.SingleContextChatSession(
                model="muse-spark-1.3", provider="muse_spark")
            text = session.summarize(max_tokens=2000, append_transcript=False)
        self.assertEqual(text, "a summary")
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["max_completion_tokens"], 2000)
        self.assertNotIn("temperature", kwargs)

    # -- fail loudly: resolved Spark with no key -----------------------------

    def test_spark_no_key_next_response_fails_loudly(self):
        # Spark key stays None while OpenAI is fully usable: still no fallback.
        with patch.object(self.conv, "create_openai") as mock_openai, \
             patch.object(self.conv, "create_muse_spark") as mock_spark:
            session = self.conv.SingleContextChatSession(
                model="muse-spark-1.3", provider="muse_spark")
            with self.assertLogs("hive.mqtt.conversations", level="ERROR") as logs:
                text, overflow = session.next_response("Hello", [])
        self.assertEqual(text, "Oh no.  I have run into a bug")
        self.assertFalse(overflow)
        self.assertIn("no Muse Spark API key", "\n".join(logs.output))
        mock_openai.assert_not_called()
        mock_spark.assert_not_called()

    def test_spark_no_key_summarize_fails_loudly(self):
        with patch.object(self.conv, "create_openai") as mock_openai, \
             patch.object(self.conv, "create_muse_spark") as mock_spark:
            session = self.conv.SingleContextChatSession(
                model="muse-spark-1.3", provider="muse_spark")
            with self.assertLogs("hive.mqtt.conversations", level="ERROR"):
                text = session.summarize(append_transcript=False)
        self.assertIn("no Muse Spark API key", text)
        mock_openai.assert_not_called()
        mock_spark.assert_not_called()

    # -- session provider plumbing --------------------------------------------

    def test_session_provider_normalization(self):
        self.assertEqual(self.conv.SingleContextChatSession(
            model="muse-spark-1.3", provider=2).provider, "muse_spark")
        self.assertEqual(
            self.conv.SingleContextChatSession(
                model="muse-spark-1.3", provider=self.models.AIVendor.MUSE_SPARK).provider,
            "muse_spark")
        with self.assertRaises(ValueError):
            self.conv.SingleContextChatSession(provider=99)
        with self.assertRaises(ValueError):
            self.conv.SingleContextChatSession(provider="bogus")

    def test_db_session_passes_vendor_through(self):
        self.factory.set_muse_spark_key("test-key-not-real")
        source = SimpleNamespace(
            max_history=20, max_volleys=10, model="muse-spark-1.3",
            prompt="Be brief.", opener="Hi!", max_tokens=70, temperature=0.5,
            vendor=self.models.AIVendor.MUSE_SPARK.value, code=None,
        )

        class _Manager:
            def get(self, pk):
                return source

        fake_chat = SimpleNamespace(objects=_Manager())
        with patch.object(self.conv, "SinglePromptChat", fake_chat):
            session = self.conv.SinglePromptDBChatSession(pk=1)
        self.assertEqual(session.provider, "muse_spark")
        self.assertEqual(session._model, "muse-spark-1.3")

    # -- STT isolation ----------------------------------------------------------

    def test_stt_client_stays_openai_when_chat_is_spark(self):
        self.factory.set_muse_spark_key("test-key-not-real")
        self.factory.set_global_chat_provider("muse_spark")
        from hive.mqtt import zmq_stt_handler

        handler = zmq_stt_handler.STTHandler(Mock())
        self.addCleanup(handler._worker_queue.shutdown)
        with patch("hive.mqtt.zmq_stt_handler.create_openai", return_value="whisper-client") as mock_factory:
            self.assertEqual(handler.get_stt_client(), "whisper-client")
        mock_factory.assert_called_once_with()
        self.assertFalse(hasattr(zmq_stt_handler, "create_chat_client"))
        self.assertFalse(hasattr(zmq_stt_handler, "create_muse_spark"))

    # -- models, migration, setup POST -------------------------------------------

    def test_model_defaults_keep_gpt(self):
        cfg = self.models.HiveConfiguration(name="default")
        self.assertEqual(cfg.chat_provider, "openai")
        self.assertEqual(cfg.muse_spark_model, "muse-spark-1.3")
        self.assertEqual(cfg.muse_spark_api_key, "")
        chat = self.models.SinglePromptChat()
        self.assertEqual(chat.vendor, self.models.AIVendor.OPEN_AI.value)
        self.assertEqual(chat.model, "gpt-6-luna")
        self.assertEqual(self.models.AIVendor.MUSE_SPARK.value, 2)

    def test_migration_adds_spark_fields(self):
        migration = importlib.import_module(
            "hive.migrations.0018_muse_spark_chat_provider").Migration
        self.assertEqual(migration.dependencies, [("hive", "0017_alter_singlepromptchat_model_default")])
        added = {op.name for op in migration.operations if op.__class__.__name__ == "AddField"}
        self.assertEqual(added, {"chat_provider", "muse_spark_api_key", "muse_spark_model"})
        altered = {op.name: op.field for op in migration.operations if op.__class__.__name__ == "AlterField"}
        self.assertIn((2, "MUSE_SPARK"), list(altered["vendor"].choices))

    def test_hive_configure_provider_and_blank_key_keeps(self):
        from django.test import RequestFactory

        cfg = SimpleNamespace(
            openai_api_key="existing-not-real", google_api_key="", external_host="",
            allow_unverified_bots=False, common_config={"x": 1}, common_settings={"y": 2},
            chat_provider="openai", muse_spark_api_key="existing-not-real",
            muse_spark_model="muse-spark-1.3", save=Mock(),
        )
        user_model = Mock()
        user_model.objects.filter.return_value.exists.return_value = True
        request = RequestFactory().post("/setup", data={
            "apikey": "", "googleapikey": "", "hostname": "testhost",
            "chat_provider": "muse_spark", "musesparkapikey": "",
            "muse_spark_model": "muse-spark-1.3-contributor",
        })
        with patch.object(self.views, "HiveConfiguration") as mock_cfg_model, \
             patch.object(self.views, "get_instance") as mock_instance, \
             patch.object(self.views, "get_user_model", return_value=user_model), \
             patch.object(self.views, "reverse", return_value="/dashboard/"):
            mock_cfg_model.objects.get_or_create.return_value = (cfg, False)
            response = self.views.hive_configure(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(cfg.chat_provider, "muse_spark")
        self.assertEqual(cfg.muse_spark_api_key, "existing-not-real")  # blank = keep
        self.assertEqual(cfg.openai_api_key, "existing-not-real")
        self.assertEqual(cfg.muse_spark_model, "muse-spark-1.3-contributor")
        cfg.save.assert_called()
        mock_instance.return_value.update_from_database.assert_called_once()

    def test_hive_configure_ignores_invalid_provider_and_model(self):
        from django.test import RequestFactory

        cfg = SimpleNamespace(
            openai_api_key="", google_api_key="", external_host="",
            allow_unverified_bots=False, common_config={"x": 1}, common_settings={"y": 2},
            chat_provider="openai", muse_spark_api_key="",
            muse_spark_model="muse-spark-1.3", save=Mock(),
        )
        user_model = Mock()
        user_model.objects.filter.return_value.exists.return_value = True
        request = RequestFactory().post("/setup", data={
            "apikey": "", "googleapikey": "", "hostname": "testhost",
            "chat_provider": "bogus", "musesparkapikey": "test-key-not-real",
            "muse_spark_model": "not-a-spark-model",
        })
        with patch.object(self.views, "HiveConfiguration") as mock_cfg_model, \
             patch.object(self.views, "get_instance"), \
             patch.object(self.views, "get_user_model", return_value=user_model), \
             patch.object(self.views, "reverse", return_value="/dashboard/"):
            mock_cfg_model.objects.get_or_create.return_value = (cfg, False)
            self.views.hive_configure(request)
        self.assertEqual(cfg.chat_provider, "openai")
        self.assertEqual(cfg.muse_spark_api_key, "test-key-not-real")
        self.assertEqual(cfg.muse_spark_model, "muse-spark-1.3")


if __name__ == "__main__":
    unittest.main()
