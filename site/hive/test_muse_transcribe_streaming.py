"""Offline Muse Transcribe *streaming* tests: run from site with venv python:
    python -m unittest hive.test_muse_transcribe_streaming -v

Every websocket is a fake; no network, broker, robot, database or real
key is used (the only key string is the fake "test-key-not-real").
These tests prove plumbing -- config/chunking/event parsing, knob
validation, mode resolution, the streaming -> one-shot -> Whisper
fallback chain, and that mocked timings are recorded. They prove
nothing about live realtime behaviour, live latency or accuracy: the
handshake/config/message field names in muse_transcribe_streaming are
unverified (see CHECKPOINT-STT-STREAMING.md).
"""
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


FAKE_KEY = "test-key-not-real"
CHUNK = 2560  # 80 ms of 16 kHz mono int16 PCM


class FakeTransport:
    """Injectable websocket stand-in (send_bytes/recv_message/close).

    ``messages`` are returned in order (an Exception entry is raised);
    when exhausted, ``recv_message`` returns None (nothing available).
    """

    def __init__(self, messages=()):
        self._messages = list(messages)
        self.sent = []
        self.closed = False

    def send_bytes(self, payload):
        self.sent.append(bytes(payload))

    def recv_message(self):
        if self._messages:
            item = self._messages.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return None

    def close(self):
        self.closed = True

    @property
    def audio_chunks(self):
        return self.sent[1:]  # sent[0] is the JSON config message


def factory_for(*transports):
    """websocket_factory(api_key) handing out transports in order."""
    queue = list(transports)
    calls = []

    def factory(api_key):
        calls.append(api_key)
        return queue.pop(0) if queue else FakeTransport()

    factory.calls = calls
    return factory


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
                SECRET_KEY="offline-muse-streaming-tests",
                ALLOWED_HOSTS=["testserver"],
                ROOT_URLCONF="hive.urls",
                STATIC_URL="/static/",
            )
        django.setup()
        # Mount hive.urls under its namespace (production does this in
        # openmoxie.urls) so {% url 'hive:...' %} resolves when the
        # setup template is rendered against hive.urls directly.
        import types
        from django.urls import include, path

        urlconf = types.ModuleType("offline_streaming_urlconf")
        urlconf.urlpatterns = [path("hive/", include("hive.urls"))]
        sys.modules["offline_streaming_urlconf"] = urlconf
        from hive import models, views
        from hive.mqtt import ai_factory, muse_transcribe, stt_knobs
        from hive.mqtt import muse_transcribe_streaming, zmq_stt_handler

        cls.models = models
        cls.views = views
        cls.factory = ai_factory
        cls.knobs = stt_knobs
        cls.transcribe_mod = muse_transcribe
        cls.streaming = muse_transcribe_streaming
        cls.stt_mod = zmq_stt_handler

    def setUp(self):
        f = self.factory
        self._saved = (
            f.get_stt_provider(), f._MUSE_SPARK_KEY,
            f._MUSE_TRANSCRIBE_KEY_OVERRIDE, f._OPENAPI_KEY,
            f.get_stt_streaming_enabled(), f.get_stt_diarization_enabled(),
            f.get_stt_endpointing_mode(), f.get_stt_endpointing_timeout_ms(),
            f.get_stt_language_hint(), f.get_stt_keyword_bias(),
            f.get_stt_code_switching_enabled(),
        )
        f.set_openai_key(FAKE_KEY)
        f.set_muse_spark_key(None)
        f.set_muse_transcribe_key(None)
        f.set_stt_provider("openai")
        f.set_stt_streaming_enabled(False)
        f.set_stt_diarization_enabled(False)
        f.set_stt_endpointing_mode("auto")
        f.set_stt_endpointing_timeout_ms(1000)
        f.set_stt_language_hint("auto")
        f.set_stt_keyword_bias("")
        f.set_stt_code_switching_enabled(False)
        self.addCleanup(self._restore_factory)

    def _restore_factory(self):
        f = self.factory
        (provider, spark, override, openai, streaming, diar, mode,
         timeout, lang, keywords, switching) = self._saved
        f.set_openai_key(openai)
        f.set_muse_spark_key(spark)
        f.set_muse_transcribe_key(override)
        f.set_stt_provider(provider)
        f.set_stt_streaming_enabled(streaming)
        f.set_stt_diarization_enabled(diar)
        f.set_stt_endpointing_mode(mode)
        f.set_stt_endpointing_timeout_ms(timeout)
        f.set_stt_language_hint(lang)
        f.set_stt_keyword_bias(keywords)
        f.set_stt_code_switching_enabled(switching)

    def _stream(self, transport, config=None, clock=None):
        kwargs = {}
        if clock is not None:
            kwargs["monotonic"] = lambda: clock[0]
        return self.streaming.StreamingSession(
            config or self.streaming.StreamingConfig(),
            websocket_factory=factory_for(transport),
            api_key=FAKE_KEY, **kwargs)


# ---------------------------------------------------------------------------
# Streaming lifecycle (Task 1)
# ---------------------------------------------------------------------------

class StreamingLifecycleTests(_BaseCase):
    def test_chunk_constant_is_80ms(self):
        self.assertEqual(self.streaming.CHUNK_BYTES, CHUNK)
        self.assertEqual(self.streaming.MUSE_STREAMING_WS_URL,
                         "wss://api.meta.ai/v1/asr/realtime")

    def test_connect_sends_config_without_key_or_transcript(self):
        transport = FakeTransport()
        config = self.streaming.StreamingConfig(
            diarization=True, language_hint="fr",
            keyword_bias=("Moxie", "bonjour"), code_switching=True,
            endpointing_mode="server_timeout", endpointing_timeout_ms=1500)
        with self.assertLogs("hive.mqtt.muse_transcribe_streaming", level="INFO") as logs:
            self._stream(transport, config).connect()
        sent = json.loads(transport.sent[0].decode("utf-8"))
        self.assertEqual(sent["model"], "muse-voice-transcribe-1.0")
        self.assertTrue(sent["diarization"])
        self.assertEqual(sent["language"], "fr")
        self.assertEqual(sent["keyword_bias"], ["Moxie", "bonjour"])
        self.assertTrue(sent["code_switching"])
        self.assertEqual(sent["endpointing"],
                         {"mode": "server_timeout", "timeout_ms": 1500})
        blob = transport.sent[0] + "\n".join(logs.output).encode()
        self.assertNotIn(FAKE_KEY.encode(), blob)

    def test_config_builder_auto_language_is_none(self):
        message = self.streaming.build_config_message(
            self.streaming.StreamingConfig())
        self.assertIsNone(message["language"])
        self.assertFalse(message["diarization"])
        self.assertEqual(message["keyword_bias"], [])

    def test_feed_audio_exact_chunks_and_trailing_partial_buffered(self):
        transport = FakeTransport()
        stream = self._stream(transport)
        stream.connect()
        sent = stream.feed_audio(b"\x01\x00" * 3840)  # 7680 bytes
        self.assertEqual(sent, 3)
        self.assertEqual([len(c) for c in transport.audio_chunks],
                         [CHUNK, CHUNK, CHUNK])
        sent = stream.feed_audio(b"\x02\x00" * 100)  # 200 bytes: no chunk
        self.assertEqual(sent, 0)
        self.assertEqual(stream.buffered_bytes, 200)
        self.assertEqual(len(transport.audio_chunks), 3)

    def test_finish_never_sends_trailing_partial(self):
        transport = FakeTransport([{"type": "final", "text": "done"}])
        stream = self._stream(transport)
        stream.feed_audio(b"\x00\x00" * 1400)  # 2800 bytes: 1 chunk + 240 held
        self.assertEqual(stream.finish(), "done")
        self.assertEqual(len(transport.audio_chunks), 1)
        self.assertTrue(transport.closed)

    def test_partial_updates_state_but_is_not_final(self):
        transport = FakeTransport([{"type": "partial", "text": "hel"}])
        stream = self._stream(transport)
        events = stream.pump()
        self.assertEqual([(e.kind, e.text) for e in events],
                         [("partial", "hel")])
        self.assertEqual(stream.last_partial, "hel")
        self.assertIsNotNone(stream.partial_at)
        self.assertIsNone(stream.final_text)
        self.assertIsNotNone(stream.first_partial_ms)

    def test_interim_and_transcript_field_accepted(self):
        transport = FakeTransport([{"type": "interim", "transcript": "hi"}])
        stream = self._stream(transport)
        events = stream.pump()
        self.assertEqual(events[0].text, "hi")

    def test_final_event_returns_final_text(self):
        transport = FakeTransport([
            {"type": "partial", "text": "hello"},
            {"type": "final", "text": "hello there"},
        ])
        stream = self._stream(transport)
        stream.pump()
        self.assertEqual(stream.final_text, "hello there")
        self.assertEqual(stream.endpoint_by, "provider")
        self.assertEqual(stream.finish(), "hello there")

    def test_endpoint_event_promotes_last_partial(self):
        transport = FakeTransport([
            {"type": "partial", "text": "hello there"},
            {"type": "endpoint"},
        ])
        stream = self._stream(transport)
        events = stream.pump()
        finals = [e for e in events if e.is_final]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].text, "hello there")
        self.assertEqual(finals[0].endpoint_by, "provider")

    def test_turn_complete_promotes_last_partial(self):
        transport = FakeTransport([
            {"type": "partial", "text": "bye"},
            {"type": "turn_complete"},
        ])
        stream = self._stream(transport)
        stream.pump()
        self.assertEqual(stream.final_text, "bye")

    def test_disconnect_before_final_raises_kind_only(self):
        transport = FakeTransport([ConnectionError("gone")])
        stream = self._stream(transport)
        with self.assertRaises(self.streaming.StreamingError) as ctx:
            stream.pump()
        self.assertEqual(ctx.exception.kind, "transport_closed")
        self.assertNotIn(FAKE_KEY, str(ctx.exception))

    def test_error_event_raises_kind_only(self):
        transport = FakeTransport([{"type": "error", "text": "secret words"}])
        stream = self._stream(transport)
        with self.assertRaises(self.streaming.StreamingError) as ctx:
            stream.pump()
        self.assertEqual(ctx.exception.kind, "provider_error")
        self.assertNotIn("secret words", str(ctx.exception))

    def test_finish_without_any_text_raises(self):
        stream = self._stream(FakeTransport())
        with self.assertRaises(self.streaming.StreamingError) as ctx:
            stream.finish()
        self.assertEqual(ctx.exception.kind, "no_final")

    def test_finish_promotes_partial_in_disabled_mode(self):
        config = self.streaming.StreamingConfig(endpointing_mode="disabled")
        transport = FakeTransport([{"type": "partial", "text": "only partial"}])
        stream = self._stream(transport, config)
        self.assertEqual(stream.finish(), "only partial")
        self.assertEqual(stream.endpoint_by, "finish")

    def test_event_repr_hides_transcript(self):
        event = self.streaming.StreamingEvent(kind="partial", text="secret words")
        self.assertNotIn("secret words", repr(event))

    def test_default_factory_no_key_is_unavailable(self):
        with self.assertRaises(self.streaming.StreamingUnavailable) as ctx:
            self.streaming.default_websocket_factory(None)
        self.assertEqual(ctx.exception.kind, "no_key")

    def test_default_factory_missing_library_is_unavailable(self):
        # Force `import websocket` to fail deterministically (no network).
        with patch.dict(sys.modules, {"websocket": None}):
            with self.assertRaises(self.streaming.StreamingUnavailable) as ctx:
                self.streaming.default_websocket_factory(FAKE_KEY)
        self.assertEqual(ctx.exception.kind, "websocket_unavailable")


class EndpointingTests(_BaseCase):
    def _clocked(self, mode):
        clock = [1000.0]
        config = self.streaming.StreamingConfig(
            endpointing_mode=mode, endpointing_timeout_ms=1000)
        transport = FakeTransport([{"type": "partial", "text": "waiting"}])
        stream = self._stream(transport, config, clock=clock)
        stream.connect()
        stream.feed_audio(b"\x00\x00" * 1280)
        stream.pump()
        return stream, clock

    def test_server_timeout_promotes_partial(self):
        stream, clock = self._clocked("server_timeout")
        self.assertIsNone(stream.final_text)
        clock[0] += 1.5  # 1500 ms idle > 1000 ms timeout
        events = stream.pump()
        self.assertEqual(stream.final_text, "waiting")
        self.assertEqual(stream.endpoint_by, "server_timeout")
        self.assertTrue(events[-1].is_final)

    def test_auto_timeout_is_backstop_promotion(self):
        stream, clock = self._clocked("auto")
        clock[0] += 1.5
        stream.pump()
        self.assertEqual(stream.final_text, "waiting")
        self.assertEqual(stream.endpoint_by, "server_timeout")

    def test_disabled_never_promotes_on_timeout(self):
        stream, clock = self._clocked("disabled")
        clock[0] += 10.0
        events = stream.pump()
        self.assertIsNone(stream.final_text)
        self.assertEqual([e for e in events if e.is_final], [])

    def test_no_promotion_before_timeout(self):
        stream, clock = self._clocked("server_timeout")
        clock[0] += 0.5
        stream.pump()
        self.assertIsNone(stream.final_text)


class TimedMockedStreamingTests(_BaseCase):
    def test_mocked_streaming_timings_recorded(self):
        transport = FakeTransport([
            {"type": "partial", "text": "hello"},
            {"type": "final", "text": "hello there"},
        ])
        stream = self._stream(transport)
        start = time.perf_counter()
        stream.connect()
        stream.feed_audio(b"\x00\x00" * 3840)
        stream.pump()
        text = stream.finish()
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self.assertEqual(text, "hello there")
        # MOCKED plumbing only: finite, non-negative, generous ceiling.
        for value in (stream.first_partial_ms, stream.final_ms, elapsed_ms):
            self.assertIsNotNone(value)
            self.assertGreaterEqual(value, 0.0)
            self.assertLess(value, 5000.0)


# ---------------------------------------------------------------------------
# Knobs + mode resolution (Task 2)
# ---------------------------------------------------------------------------

class KnobValidationTests(_BaseCase):
    def test_empty_input_gives_defaults(self):
        clean, rejected = self.knobs.validate_stt_knobs({})
        self.assertEqual(clean, {
            "stt_provider": "openai",
            "stt_streaming_enabled": False,
            "stt_diarization_enabled": False,
            "stt_endpointing_mode": "auto",
            "stt_endpointing_timeout_ms": 1000,
            "stt_language_hint": "auto",
            "stt_keyword_bias": "",
            "stt_code_switching_enabled": False,
        })
        self.assertEqual(rejected, [])

    def test_language_allowlist_has_25_codes_plus_auto(self):
        self.assertEqual(len(self.knobs.VALIDATED_LANGUAGE_CODES), 25)
        self.assertEqual(self.knobs.VALID_LANGUAGE_HINTS[0], "auto")
        for code in ("en", "no", "zh", "uk"):
            self.assertIn(code, self.knobs.VALIDATED_LANGUAGE_CODES)

    def test_bad_provider_endpointing_language_rejected(self):
        clean, rejected = self.knobs.validate_stt_knobs({
            "stt_provider": "bogus",
            "stt_endpointing_mode": "whenever",
            "stt_language_hint": "xx",
        })
        self.assertEqual(clean["stt_provider"], "openai")
        self.assertEqual(clean["stt_endpointing_mode"], "auto")
        self.assertEqual(clean["stt_language_hint"], "auto")
        self.assertEqual(set(rejected),
                         {"stt_provider", "stt_endpointing_mode", "stt_language_hint"})

    def test_good_values_accepted_and_bools_parsed(self):
        clean, rejected = self.knobs.validate_stt_knobs({
            "stt_provider": "muse_streaming",
            "stt_streaming_enabled": "on",
            "stt_diarization_enabled": "true",
            "stt_code_switching_enabled": "1",
            "stt_endpointing_mode": "disabled",
            "stt_language_hint": "fr",
        })
        self.assertEqual(rejected, [])
        self.assertEqual(clean["stt_provider"], "muse_streaming")
        self.assertTrue(clean["stt_streaming_enabled"])
        self.assertTrue(clean["stt_diarization_enabled"])
        self.assertTrue(clean["stt_code_switching_enabled"])
        clean2, _ = self.knobs.validate_stt_knobs(
            {"stt_streaming_enabled": True, "stt_diarization_enabled": "off"})
        self.assertTrue(clean2["stt_streaming_enabled"])
        self.assertFalse(clean2["stt_diarization_enabled"])

    def test_blank_language_is_auto_not_rejected(self):
        clean, rejected = self.knobs.validate_stt_knobs({"stt_language_hint": ""})
        self.assertEqual(clean["stt_language_hint"], "auto")
        self.assertEqual(rejected, [])

    def test_timeout_clamping(self):
        clean, _ = self.knobs.validate_stt_knobs({"stt_endpointing_timeout_ms": "100"})
        self.assertEqual(clean["stt_endpointing_timeout_ms"], 200)
        clean, _ = self.knobs.validate_stt_knobs({"stt_endpointing_timeout_ms": "99999"})
        self.assertEqual(clean["stt_endpointing_timeout_ms"], 5000)
        clean, rejected = self.knobs.validate_stt_knobs(
            {"stt_endpointing_timeout_ms": "garbage"})
        self.assertEqual(clean["stt_endpointing_timeout_ms"], 1000)
        self.assertIn("stt_endpointing_timeout_ms", rejected)
        clean, rejected = self.knobs.validate_stt_knobs(
            {"stt_endpointing_timeout_ms": "2500"})
        self.assertEqual(clean["stt_endpointing_timeout_ms"], 2500)
        self.assertEqual(rejected, [])

    def test_keyword_item_cap(self):
        raw = ", ".join("word%d" % i for i in range(21))
        clean, rejected = self.knobs.validate_stt_knobs({"stt_keyword_bias": raw})
        self.assertEqual(len(clean["stt_keyword_bias"].split(", ")), 20)
        self.assertIn("stt_keyword_bias", rejected)

    def test_keyword_overlong_and_illegal_dropped(self):
        clean, _ = self.knobs.validate_stt_knobs({
            "stt_keyword_bias": "Moxie, " + "x" * 41 + ", bad!word, O'Brien-Smith, two words"})
        self.assertEqual(clean["stt_keyword_bias"], "Moxie, O'Brien-Smith, two words")

    def test_keyword_total_cap_drops_whole_items(self):
        items = ["w%02d-%s" % (i, "y" * 20) for i in range(20)]  # joined > 500
        clean, _ = self.knobs.validate_stt_knobs(
            {"stt_keyword_bias": ", ".join(items)})
        stored = clean["stt_keyword_bias"]
        self.assertLessEqual(len(stored), 500)
        for kept in stored.split(", "):
            self.assertIn(kept, items)  # never cut mid-word

    def test_keyword_empties_dropped_without_rejection(self):
        clean, rejected = self.knobs.validate_stt_knobs(
            {"stt_keyword_bias": "Moxie,, robot,"})
        self.assertEqual(clean["stt_keyword_bias"], "Moxie, robot")
        self.assertEqual(rejected, [])


class ModeResolutionTests(_BaseCase):
    def test_all_branches(self):
        r = self.knobs.resolve_stt_mode
        self.assertEqual(r("openai", False, False),
                         ("whisper", False, "openai_default"))
        self.assertEqual(r("openai", True, True),
                         ("whisper", False, "openai_default"))
        self.assertEqual(r("muse_transcribe", False, True),
                         ("muse_oneshot", False, "muse_transcribe"))
        self.assertEqual(r("muse_transcribe", True, False),
                         ("whisper", True, "whisper_fallback_no_key"))
        self.assertEqual(r("muse_streaming", True, True),
                         ("muse_streaming", False, "muse_streaming"))
        self.assertEqual(r("muse_streaming", True, False),
                         ("whisper", True, "whisper_fallback_no_key"))
        self.assertEqual(r("muse_streaming", False, True),
                         ("muse_oneshot", False, "muse_oneshot_streaming_disabled"))
        self.assertEqual(r("muse_streaming", False, False),
                         ("whisper", True, "whisper_fallback_no_key"))


class FactoryKnobTests(_BaseCase):
    def test_streaming_config_from_globals(self):
        f = self.factory
        f.set_stt_diarization_enabled(True)
        f.set_stt_language_hint("es")
        f.set_stt_keyword_bias("Moxie, hola")
        f.set_stt_code_switching_enabled(True)
        f.set_stt_endpointing_mode("server_timeout")
        f.set_stt_endpointing_timeout_ms(2000)
        config = f.get_stt_streaming_config()
        self.assertTrue(config.diarization)
        self.assertEqual(config.language_hint, "es")
        self.assertEqual(config.keyword_bias, ("Moxie", "hola"))
        self.assertTrue(config.code_switching)
        self.assertEqual(config.endpointing_mode, "server_timeout")
        self.assertEqual(config.endpointing_timeout_ms, 2000)
        self.assertEqual(f.get_stt_keyword_list(), ["Moxie", "hola"])

    def test_auto_language_maps_to_none(self):
        self.assertIsNone(self.factory.get_stt_streaming_config().language_hint)

    def test_unknown_setter_values_keep_current(self):
        f = self.factory
        f.set_stt_language_hint("de")
        with self.assertLogs("hive.mqtt.ai_factory", level="WARNING"):
            f.set_stt_language_hint("klingon")
        self.assertEqual(f.get_stt_language_hint(), "de")
        with self.assertLogs("hive.mqtt.ai_factory", level="WARNING"):
            f.set_stt_endpointing_mode("whenever")
        self.assertEqual(f.get_stt_endpointing_mode(), "auto")
        with self.assertLogs("hive.mqtt.ai_factory", level="WARNING"):
            f.set_stt_endpointing_timeout_ms("garbage")
        self.assertEqual(f.get_stt_endpointing_timeout_ms(), 1000)

    def test_resolve_detailed_unchanged_for_existing_providers(self):
        f = self.factory
        self.assertEqual(f.resolve_stt_provider_detailed(),
                         ("openai", False, "openai_default"))
        f.set_muse_spark_key(FAKE_KEY)
        f.set_stt_provider("muse_transcribe")
        self.assertEqual(f.resolve_stt_provider_detailed(),
                         ("muse_transcribe", False, "muse_transcribe"))
        f.set_muse_spark_key(None)
        self.assertEqual(f.resolve_stt_provider_detailed(),
                         ("openai", True, "whisper_fallback_no_key"))

    def test_resolve_detailed_streaming_provider(self):
        f = self.factory
        f.set_muse_spark_key(FAKE_KEY)
        f.set_stt_provider("muse_streaming")
        self.assertEqual(f.get_stt_provider(), "muse_streaming")
        self.assertEqual(f.resolve_stt_provider_detailed(),
                         ("muse_streaming", False, "muse_streaming"))


class ModelDefaultsTests(_BaseCase):
    def test_fresh_config_streaming_defaults(self):
        cfg = self.models.HiveConfiguration()
        self.assertEqual(cfg.stt_provider, "openai")
        self.assertFalse(cfg.stt_streaming_enabled)
        self.assertFalse(cfg.stt_diarization_enabled)
        self.assertEqual(cfg.stt_endpointing_mode, "auto")
        self.assertEqual(cfg.stt_endpointing_timeout_ms, 1000)
        self.assertEqual(cfg.stt_language_hint, "auto")
        self.assertEqual(cfg.stt_keyword_bias, "")
        self.assertFalse(cfg.stt_code_switching_enabled)
        self.assertIn(("muse_streaming", "Muse Transcribe streaming (Meta) — opt-in"),
                      self.models.STT_PROVIDER_CHOICES)

    def test_migration_0021_shape(self):
        import importlib
        migration = importlib.import_module(
            "hive.migrations.0021_hiveconfiguration_stt_streaming_knobs").Migration
        self.assertEqual(migration.dependencies,
                         [("hive", "0020_hiveconfiguration_stt_provider")])
        added = {op.name for op in migration.operations
                 if op.__class__.__name__ == "AddField"}
        self.assertEqual(added, {
            "stt_streaming_enabled", "stt_diarization_enabled",
            "stt_endpointing_mode", "stt_endpointing_timeout_ms",
            "stt_language_hint", "stt_keyword_bias",
            "stt_code_switching_enabled",
        })


# ---------------------------------------------------------------------------
# Handler fallback chain (Task 4)
# ---------------------------------------------------------------------------

class HandlerStreamingTests(_BaseCase):
    def setUp(self):
        super().setUp()
        self.server = Mock()
        self.handler = self.stt_mod.STTHandler(self.server)
        self.addCleanup(self.handler._worker_queue.shutdown)
        factory_patch = patch("hive.mqtt.zmq_stt_handler.create_openai")
        self.openai_factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)
        self.whisper_api = self.openai_factory.return_value.audio.transcriptions.create
        self.whisper_api.return_value = SimpleNamespace(
            text="whisper text", segments=[])

    def _enable_streaming(self, factory):
        self.factory.set_muse_spark_key(FAKE_KEY)
        self.factory.set_stt_provider("muse_streaming")
        self.factory.set_stt_streaming_enabled(True)
        self.handler.streaming_websocket_factory = factory

    def _perform(self):
        from hive.mqtt.protos.embodied.perception.audio.zmqSTT_pb2 import (
            zmqSTTRequest, zmqSTTResponse)
        session = self.stt_mod.STTSession(self.handler, "robot", "utterance")
        session.on_request(zmqSTTRequest(
            timestamp=10000, audio_content=b"\x00\x00" * 16000, uuid="utterance"))
        session.perform()
        self.server.send_zmq_to_bot.assert_called_once()
        device, response = self.server.send_zmq_to_bot.call_args.args
        self.assertEqual(device, "robot")
        return session, zmqSTTResponse.FromString(response.SerializeToString())

    def test_streaming_success_skips_oneshot_and_whisper(self):
        transport = FakeTransport([
            {"type": "partial", "text": "hello"},
            {"type": "final", "text": "hello streaming"},
        ])
        self._enable_streaming(factory_for(transport))
        with patch("hive.mqtt.zmq_stt_handler.transcribe_wav") as mock_oneshot:
            session, response = self._perform()
        mock_oneshot.assert_not_called()
        self.whisper_api.assert_not_called()
        self.assertEqual(response.speech, "hello streaming")
        self.assertEqual(response.error_code, 0)
        self.assertEqual(session.stt_provider, "muse_streaming")
        self.assertEqual(session.stt_source, "muse_streaming")
        self.assertFalse(session.fell_back)
        self.assertNotIn(FAKE_KEY.encode(), b"".join(transport.sent))

    def test_streaming_failure_falls_back_to_oneshot(self):
        transport = FakeTransport([ConnectionError("died mid-utterance")])
        self._enable_streaming(factory_for(transport))
        result = self.transcribe_mod.TranscribeResult(
            text="oneshot text", start_ms=0, end_ms=500)
        with patch("hive.mqtt.zmq_stt_handler.transcribe_wav",
                   return_value=result):
            with self.assertLogs("hive.mqtt.zmq_stt_handler", level="WARNING") as logs:
                session, response = self._perform()
        self.assertEqual(response.speech, "oneshot text")
        self.assertEqual(session.stt_provider, "muse_transcribe")
        self.assertEqual(session.stt_source, "muse_transcribe_fallback")
        self.assertTrue(session.fell_back)
        combined = "\n".join(logs.output)
        self.assertIn("falling back to Muse Transcribe one-shot", combined)
        self.assertNotIn(FAKE_KEY, combined)
        self.assertNotIn("oneshot text", combined)

    def test_streaming_and_oneshot_failure_fall_back_to_whisper(self):
        transport = FakeTransport([{"type": "error"}])
        self._enable_streaming(factory_for(transport))
        with patch("hive.mqtt.zmq_stt_handler.transcribe_wav",
                   side_effect=self.transcribe_mod.MuseTranscribeError(
                       "HTTP 500", status=500)):
            with self.assertLogs("hive.mqtt.zmq_stt_handler", level="WARNING") as logs:
                session, response = self._perform()
        self.assertEqual(response.speech, "whisper text")
        self.assertEqual(session.stt_provider, "openai")
        self.assertEqual(session.stt_source, "whisper_fallback")
        self.assertTrue(session.fell_back)
        combined = "\n".join(logs.output)
        self.assertNotIn(FAKE_KEY, combined)
        self.assertNotIn("whisper text", combined)

    def test_no_transport_falls_back_to_oneshot(self):
        # Handler factory left as None: lazy adapter, library forced absent.
        self.factory.set_muse_spark_key(FAKE_KEY)
        self.factory.set_stt_provider("muse_streaming")
        self.factory.set_stt_streaming_enabled(True)
        result = self.transcribe_mod.TranscribeResult(
            text="oneshot text", start_ms=0, end_ms=0)
        with patch.dict(sys.modules, {"websocket": None}):
            with patch("hive.mqtt.zmq_stt_handler.transcribe_wav",
                       return_value=result):
                session, response = self._perform()
        self.assertEqual(response.speech, "oneshot text")
        self.assertEqual(session.stt_source, "muse_transcribe_fallback")

    def test_streaming_empty_final_falls_back(self):
        transport = FakeTransport([{"type": "final", "text": "  "}])
        self._enable_streaming(factory_for(transport))
        result = self.transcribe_mod.TranscribeResult(
            text="oneshot text", start_ms=0, end_ms=0)
        with patch("hive.mqtt.zmq_stt_handler.transcribe_wav",
                   return_value=result):
            session, response = self._perform()
        self.assertEqual(response.speech, "oneshot text")
        self.assertTrue(session.fell_back)

    def test_streaming_provider_with_switch_off_uses_oneshot(self):
        self.factory.set_muse_spark_key(FAKE_KEY)
        self.factory.set_stt_provider("muse_streaming")
        self.factory.set_stt_streaming_enabled(False)
        live_factory = Mock(side_effect=AssertionError("must not stream"))
        self.handler.streaming_websocket_factory = live_factory
        result = self.transcribe_mod.TranscribeResult(
            text="oneshot text", start_ms=0, end_ms=0)
        with patch("hive.mqtt.zmq_stt_handler.transcribe_wav",
                   return_value=result):
            session, response = self._perform()
        live_factory.assert_not_called()
        self.assertEqual(response.speech, "oneshot text")
        self.assertEqual(session.stt_source, "muse_transcribe")
        self.assertFalse(session.fell_back)

    def test_total_failure_still_sends_error_66(self):
        transport = FakeTransport([ConnectionError("down")])
        self._enable_streaming(factory_for(transport))
        self.whisper_api.side_effect = RuntimeError("synthetic whisper failure")
        with patch("hive.mqtt.zmq_stt_handler.transcribe_wav",
                   side_effect=self.transcribe_mod.MuseTranscribeError(
                       "HTTP 500", status=500)):
            with self.assertLogs("hive.mqtt.zmq_stt_handler", level="WARNING"):
                _session, response = self._perform()
        self.assertEqual(response.error_code, 66)


class HandlerLiveFrameTests(_BaseCase):
    def setUp(self):
        super().setUp()
        self.server = Mock()
        self.handler = self.stt_mod.STTHandler(self.server)
        self.addCleanup(self.handler._worker_queue.shutdown)
        factory_patch = patch("hive.mqtt.zmq_stt_handler.create_openai")
        self.openai_factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)
        self.whisper_api = self.openai_factory.return_value.audio.transcriptions.create
        self.whisper_api.return_value = SimpleNamespace(
            text="whisper text", segments=[])

    def _frame(self, vad, samples=1280):
        from hive.mqtt.protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTRequest
        req = zmqSTTRequest(timestamp=10000, uuid="u1", vad=vad,
                            audio_content=b"\x00\x00" * samples)
        return req.SerializeToString()

    def _enable(self, factory):
        self.factory.set_muse_spark_key(FAKE_KEY)
        self.factory.set_stt_provider("muse_streaming")
        self.factory.set_stt_streaming_enabled(True)
        self.handler.streaming_websocket_factory = factory

    def test_early_final_replies_immediately(self):
        from hive.mqtt.protos.embodied.perception.audio.zmqSTT_pb2 import (
            zmqSTTRequest, zmqSTTResponse)
        transport = FakeTransport([{"type": "final", "text": "early words"}])
        self._enable(factory_for(transport))
        self.handler.handle_zmq(
            "robot", "p", self._frame(zmqSTTRequest.VADState.SPEECH))
        self.server.send_zmq_to_bot.assert_called_once()
        _device, response = self.server.send_zmq_to_bot.call_args.args
        parsed = zmqSTTResponse.FromString(response.SerializeToString())
        self.assertEqual(parsed.speech, "early words")
        self.assertEqual(parsed.type, zmqSTTResponse.FINAL)
        self.assertEqual(self.handler._sessions, {})
        self.whisper_api.assert_not_called()

    def test_partial_live_then_end_of_speech_buffered_path(self):
        from hive.mqtt.protos.embodied.perception.audio.zmqSTT_pb2 import (
            zmqSTTRequest, zmqSTTResponse)
        live = FakeTransport([{"type": "partial", "text": "buf"}])
        buffered = FakeTransport([{"type": "final", "text": "buffered final"}])
        self._enable(factory_for(live, buffered))
        self.handler.handle_zmq(
            "robot", "p", self._frame(zmqSTTRequest.VADState.SPEECH))
        self.server.send_zmq_to_bot.assert_not_called()  # partial never sent
        session = self.handler._sessions[("robot", "u1")]
        session.perform()  # what END_OF_SPEECH queues; run inline here
        self.server.send_zmq_to_bot.assert_called_once()
        _device, response = self.server.send_zmq_to_bot.call_args.args
        parsed = zmqSTTResponse.FromString(response.SerializeToString())
        self.assertEqual(parsed.speech, "buffered final")
        self.assertEqual(session.stt_provider, "muse_streaming")

    def test_no_factory_means_no_live_attempt(self):
        from hive.mqtt.protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTRequest
        self.factory.set_muse_spark_key(FAKE_KEY)
        self.factory.set_stt_provider("muse_streaming")
        self.factory.set_stt_streaming_enabled(True)
        self.assertIsNone(self.handler.streaming_websocket_factory)
        self.handler.handle_zmq(
            "robot", "p", self._frame(zmqSTTRequest.VADState.SPEECH))
        self.server.send_zmq_to_bot.assert_not_called()
        session = self.handler._sessions[("robot", "u1")]
        self.assertFalse(session._streaming_attempted)

    def test_whisper_mode_ignores_configured_factory(self):
        from hive.mqtt.protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTRequest
        factory = Mock(return_value=FakeTransport())
        self.handler.streaming_websocket_factory = factory  # provider: openai
        self.handler.handle_zmq(
            "robot", "p", self._frame(zmqSTTRequest.VADState.SPEECH))
        factory.assert_not_called()
        self.server.send_zmq_to_bot.assert_not_called()


# ---------------------------------------------------------------------------
# Views / templates (Task 5)
# ---------------------------------------------------------------------------

KNOB_FIELD_NAMES = [
    "stt_provider", "stt_streaming_enabled", "stt_diarization_enabled",
    "stt_endpointing_mode", "stt_endpointing_timeout_ms",
    "stt_language_hint", "stt_keyword_bias", "stt_code_switching_enabled",
]

STAFF = SimpleNamespace(is_active=True, is_staff=True, is_authenticated=True)
ANON = SimpleNamespace(is_active=False, is_staff=False, is_authenticated=False)


class ViewRenderingTests(_BaseCase):
    def test_setup_render_contains_every_knob(self):
        from django.template.loader import render_to_string
        from django.test import RequestFactory, override_settings

        cfg = self.models.HiveConfiguration()
        cfg.stt_provider = "muse_streaming"
        cfg.stt_streaming_enabled = True
        cfg.stt_language_hint = "fr"
        cfg.stt_keyword_bias = "Moxie"
        cfg.stt_endpointing_timeout_ms = 2500
        request = RequestFactory().get("/setup")
        request.user = ANON
        with override_settings(ROOT_URLCONF="offline_streaming_urlconf"):
            html = render_to_string(
                "hive/setup.html",
                {"object": cfg, "needs_admin": False, "moxie_version": "test"},
                request=request)
        for name in KNOB_FIELD_NAMES:
            self.assertIn('name="%s"' % name, html, name)
        self.assertIn('value="muse_streaming" selected', html)
        self.assertIn('value="2500"', html)
        self.assertIn("Moxie", html)

    def test_setup_and_dashboard_template_sources(self):
        hive_dir = Path(self.models.__file__).parent
        setup = (hive_dir / "templates" / "hive" / "setup.html").read_text(
            encoding="utf-8")
        for name in KNOB_FIELD_NAMES:
            self.assertIn(name, setup, name)
        self.assertIn("muse_streaming", setup)
        dashboard = (hive_dir / "templates" / "hive" / "dashboard.html").read_text(
            encoding="utf-8")
        self.assertIn("stt_status", dashboard)

    def test_hive_configure_saves_validated_knobs(self):
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
            "stt_provider": "muse_streaming",
            "stt_streaming_enabled": "on",
            "stt_endpointing_mode": "server_timeout",
            "stt_endpointing_timeout_ms": "99999",
            "stt_language_hint": "klingon",
            "stt_keyword_bias": "Moxie, bad!word",
        })
        with patch.object(self.views, "HiveConfiguration") as mock_model, \
             patch.object(self.views, "get_instance"), \
             patch.object(self.views, "get_user_model", return_value=user_model), \
             patch.object(self.views, "reverse", return_value="/dashboard/"):
            mock_model.objects.get_or_create.return_value = (cfg, False)
            response = self.views.hive_configure(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(cfg.stt_provider, "muse_streaming")
        self.assertTrue(cfg.stt_streaming_enabled)
        self.assertEqual(cfg.stt_endpointing_mode, "server_timeout")
        self.assertEqual(cfg.stt_endpointing_timeout_ms, 5000)  # clamped
        self.assertEqual(cfg.stt_language_hint, "auto")  # rejected -> kept
        self.assertEqual(cfg.stt_keyword_bias, "Moxie")  # cleaned

    def test_hive_configure_is_not_csrf_exempt(self):
        self.assertFalse(getattr(self.views.hive_configure, "csrf_exempt", False))

    def test_logs_staff_gate_unchanged(self):
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
        # Setup/Dashboard are LAN bootstrap pages in stock code and are
        # NOT staff-gated; no 302 is claimed for them (see checkpoint).
        for view in (self.views.SetupView, self.views.DashboardView):
            self.assertFalse(
                any("staff" in getattr(b, "__name__", "").lower()
                    for b in view.__mro__))


if __name__ == "__main__":
    unittest.main()
