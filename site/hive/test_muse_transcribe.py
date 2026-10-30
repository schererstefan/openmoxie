"""Offline Muse Transcribe STT tests: run from site with the venv python:
    python -m unittest hive.test_muse_transcribe -v

All endpoint calls are mocked; no network, broker, robot, database or
real key is used. The only key value in this file is the fake
"test-key-not-real", as in the existing Muse Spark tests. A real
Transcribe endpoint call is therefore still pending (see
CHECKPOINT-STT-SWAP.md); these tests prove selection, audio format,
fallback and key hygiene, not live endpoint behaviour.
"""
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import soundfile as sf


def _wav(samplerate=16000, channels=1, subtype="PCM_16", frames=16000):
    import numpy as np

    data = np.zeros((frames, channels), dtype=np.int16) if channels > 1 else np.zeros(frames, dtype=np.int16)
    buffer = io.BytesIO()
    sf.write(buffer, data, samplerate, format="WAV", subtype=subtype)
    return buffer.getvalue()


class _FactoryCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import django
        from django.conf import settings

        if not settings.configured:
            settings.configure(
                INSTALLED_APPS=["hive"],
                TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates"}],
                SECRET_KEY="offline-muse-transcribe-tests",
            )
        django.setup()
        from hive.mqtt import ai_factory
        from hive.mqtt import muse_transcribe
        from hive.mqtt import zmq_stt_handler

        cls.factory = ai_factory
        cls.transcribe_mod = muse_transcribe
        cls.stt_mod = zmq_stt_handler

    def setUp(self):
        self._saved = (
            self.factory.get_stt_provider(),
            self.factory._MUSE_SPARK_KEY,
            self.factory._MUSE_TRANSCRIBE_KEY_OVERRIDE,
            self.factory._OPENAPI_KEY,
        )
        self.factory.set_openai_key("test-key-not-real")
        self.factory.set_muse_spark_key(None)
        self.factory.set_muse_transcribe_key(None)
        self.factory.set_stt_provider("openai")
        self.addCleanup(self._restore_factory)

    def _restore_factory(self):
        provider, spark_key, override_key, openai_key = self._saved
        self.factory.set_openai_key(openai_key)
        self.factory.set_muse_spark_key(spark_key)
        self.factory.set_muse_transcribe_key(override_key)
        self.factory.set_stt_provider(provider)


class ProviderSelectionTests(_FactoryCase):
    def test_default_is_whisper(self):
        provider, fell_back, source = self.factory.resolve_stt_provider_detailed()
        self.assertEqual((provider, fell_back, source), ("openai", False, "openai_default"))

    def test_opt_in_muse_with_spark_key(self):
        self.factory.set_muse_spark_key("test-key-not-real")
        self.factory.set_stt_provider("muse_transcribe")
        provider, fell_back, source = self.factory.resolve_stt_provider_detailed()
        self.assertEqual((provider, fell_back, source), ("muse_transcribe", False, "muse_transcribe"))
        self.assertEqual(self.factory.get_muse_transcribe_key(), "test-key-not-real")

    def test_opt_in_muse_with_override_key_and_no_spark_key(self):
        self.factory.set_muse_transcribe_key("test-key-not-real")
        self.factory.set_stt_provider("muse_transcribe")
        provider, fell_back, source = self.factory.resolve_stt_provider_detailed()
        self.assertEqual((provider, fell_back, source), ("muse_transcribe", False, "muse_transcribe"))

    def test_override_beats_spark_key(self):
        self.factory.set_muse_spark_key("spark-key-not-real")
        self.factory.set_muse_transcribe_key("test-key-not-real")
        self.assertEqual(self.factory.get_muse_transcribe_key(), "test-key-not-real")

    def test_blank_override_falls_back_to_spark_key(self):
        self.factory.set_muse_spark_key("spark-key-not-real")
        self.factory.set_muse_transcribe_key("")
        self.assertEqual(self.factory.get_muse_transcribe_key(), "spark-key-not-real")

    def test_opt_in_muse_without_any_key_falls_back_to_whisper(self):
        self.factory.set_stt_provider("muse_transcribe")
        provider, fell_back, source = self.factory.resolve_stt_provider_detailed()
        self.assertEqual((provider, fell_back, source), ("openai", True, "whisper_fallback_no_key"))

    def test_unknown_provider_raises_and_setter_keeps_current(self):
        with self.assertRaises(ValueError):
            self.factory.resolve_stt_provider_detailed(provider="bogus")
        self.factory.set_stt_provider("bogus")
        self.assertEqual(self.factory.get_stt_provider(), "openai")

    def test_chat_provider_does_not_change_stt(self):
        self.factory.set_muse_spark_key("test-key-not-real")
        self.factory.set_global_chat_provider("muse_spark")
        provider, _, _ = self.factory.resolve_stt_provider_detailed()
        self.assertEqual(provider, "openai")


class AudioConversionTests(_FactoryCase):
    def test_existing_handler_wav_format_is_endpoint_valid(self):
        samplerate, channels, frames = self.transcribe_mod.validate_wav_bytes(_wav())
        self.assertEqual((samplerate, channels, frames), (16000, 1, 16000))

    def test_24khz_accepted(self):
        samplerate, _, _ = self.transcribe_mod.validate_wav_bytes(_wav(samplerate=24000, frames=24000))
        self.assertEqual(samplerate, 24000)

    def test_stereo_rejected(self):
        with self.assertRaises(ValueError):
            self.transcribe_mod.validate_wav_bytes(_wav(channels=2))

    def test_wrong_samplerate_rejected(self):
        with self.assertRaises(ValueError):
            self.transcribe_mod.validate_wav_bytes(_wav(samplerate=8000, frames=8000))

    def test_non_pcm16_rejected(self):
        with self.assertRaises(ValueError):
            self.transcribe_mod.validate_wav_bytes(_wav(subtype="FLOAT"))

    def test_empty_payload_rejected(self):
        with self.assertRaises(ValueError):
            self.transcribe_mod.validate_wav_bytes(b"")


class TranscribeEndpointTests(_FactoryCase):
    def _fake_response(self, status=200, payload=None):
        response = Mock()
        response.status_code = status
        response.json.return_value = payload if payload is not None else {"text": "hello world"}
        return response

    def test_one_shot_request_shape_and_bearer_auth(self):
        wav = _wav()
        calls = {}

        def fake_post(url, headers, files, data, timeout):
            calls.update(url=url, headers=headers, files=files, data=data, timeout=timeout)
            return self._fake_response(payload={"text": "hello world", "start_ms": 100, "end_ms": 900})

        result = self.transcribe_mod.transcribe_wav(wav, "test-key-not-real", http_post=fake_post)
        self.assertEqual((result.text, result.start_ms, result.end_ms), ("hello world", 100, 900))
        self.assertEqual(calls["url"], "https://api.meta.ai/v1/asr/transcribe")
        self.assertEqual(calls["headers"]["Authorization"], "Bearer test-key-not-real")
        self.assertEqual(calls["data"], {"model": "muse-voice-transcribe-1.0"})
        name, body, content_type = calls["files"]["file"]
        self.assertEqual((name, body, content_type), ("utterance.wav", wav, "audio/wav"))

    def test_turns_shape_parsed(self):
        result = self.transcribe_mod.transcribe_wav(
            _wav(),
            "test-key-not-real",
            http_post=lambda url, **kw: self._fake_response(payload={
                "turns": [
                    {"text": "hello", "start_ms": 0, "end_ms": 500},
                    {"text": "there", "start_ms": 500, "end_ms": 900},
                ]
            }),
        )
        self.assertEqual((result.text, result.start_ms, result.end_ms), ("hello there", 0, 900))

    def test_segments_seconds_shape_parsed(self):
        result = self.transcribe_mod.transcribe_wav(
            _wav(),
            "test-key-not-real",
            http_post=lambda url, **kw: self._fake_response(payload={
                "segments": [{"text": "hello", "start": 0.2, "end": 0.6}]
            }),
        )
        self.assertEqual((result.text, result.start_ms, result.end_ms), ("hello", 200, 600))

    def test_http_error_raises_with_status(self):
        with self.assertRaises(self.transcribe_mod.MuseTranscribeError) as ctx:
            self.transcribe_mod.transcribe_wav(
                _wav(), "test-key-not-real",
                http_post=lambda url, **kw: self._fake_response(status=401, payload={}),
            )
        self.assertEqual(ctx.exception.status, 401)

    def test_missing_key_raises_without_network_call(self):
        called = []

        def fake_post(**kw):
            called.append(True)
            return self._fake_response()

        with self.assertRaises(self.transcribe_mod.MuseTranscribeError):
            self.transcribe_mod.transcribe_wav(_wav(), None, http_post=fake_post)
        self.assertEqual(called, [])

    def test_bad_wav_raises_without_network_call(self):
        called = []

        def fake_post(**kw):
            called.append(True)
            return self._fake_response()

        with self.assertRaises(ValueError):
            self.transcribe_mod.transcribe_wav(b"not-a-wav", "test-key-not-real", http_post=fake_post)
        self.assertEqual(called, [])

    def test_no_key_string_in_transcribe_logs(self):
        with self.assertLogs("hive.mqtt.muse_transcribe", level="INFO") as logs:
            self.transcribe_mod.transcribe_wav(
                _wav(), "test-key-not-real",
                http_post=lambda url, **kw: self._fake_response(payload={"text": "hello"}),
            )
        self.assertNotIn("test-key-not-real", "\n".join(logs.output))


class HandlerFallbackTests(_FactoryCase):
    def setUp(self):
        super().setUp()
        self.server = Mock()
        self.handler = self.stt_mod.STTHandler(self.server)
        self.addCleanup(self.handler._worker_queue.shutdown)
        factory_patch = patch("hive.mqtt.zmq_stt_handler.create_openai")
        self.openai_factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)
        self.whisper_api = self.openai_factory.return_value.audio.transcriptions.create
        self.whisper_api.return_value = SimpleNamespace(text="whisper text", segments=[])

    def _perform(self, transcribe_patch=None):
        from hive.mqtt.protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTRequest, zmqSTTResponse

        session = self.stt_mod.STTSession(self.handler, "robot", "utterance")
        session.on_request(zmqSTTRequest(
            timestamp=10000, audio_content=b"\x00\x00" * 16000, uuid="utterance",
        ))
        session.perform()
        self.server.send_zmq_to_bot.assert_called_once()
        device, response = self.server.send_zmq_to_bot.call_args.args
        self.assertEqual(device, "robot")
        parsed = zmqSTTResponse.FromString(response.SerializeToString())
        return session, parsed

    def test_default_whisper_path_never_calls_transcribe(self):
        with patch("hive.mqtt.zmq_stt_handler.transcribe_wav") as mock_transcribe:
            session, response = self._perform()
        mock_transcribe.assert_not_called()
        self.assertEqual(response.speech, "whisper text")
        self.assertFalse(session.fell_back)
        self.assertEqual(session.stt_provider, "openai")

    def test_muse_success_skips_whisper(self):
        self.factory.set_muse_spark_key("test-key-not-real")
        self.factory.set_stt_provider("muse_transcribe")
        result = self.transcribe_mod.TranscribeResult(text="hello muse", start_ms=100, end_ms=900)
        with patch("hive.mqtt.zmq_stt_handler.transcribe_wav", return_value=result) as mock_transcribe:
            session, response = self._perform()
        mock_transcribe.assert_called_once()
        self.whisper_api.assert_not_called()
        self.assertEqual(response.speech, "hello muse")
        self.assertEqual((response.start_timestamp, response.end_timestamp), (10100, 10900))
        self.assertFalse(session.fell_back)
        self.assertEqual(session.stt_provider, "muse_transcribe")

    def test_muse_error_falls_back_to_whisper(self):
        self.factory.set_muse_spark_key("test-key-not-real")
        self.factory.set_stt_provider("muse_transcribe")
        with patch(
            "hive.mqtt.zmq_stt_handler.transcribe_wav",
            side_effect=self.transcribe_mod.MuseTranscribeError("HTTP 401", status=401),
        ):
            with self.assertLogs("hive.mqtt.zmq_stt_handler", level="WARNING") as logs:
                session, response = self._perform()
        self.assertEqual(response.speech, "whisper text")
        self.assertEqual(response.error_code, 0)
        self.assertTrue(session.fell_back)
        self.assertEqual(session.stt_provider, "openai")
        combined = "\n".join(logs.output)
        self.assertIn("falling back", combined)
        self.assertNotIn("test-key-not-real", combined)

    def test_muse_empty_transcript_falls_back_to_whisper(self):
        self.factory.set_muse_spark_key("test-key-not-real")
        self.factory.set_stt_provider("muse_transcribe")
        result = self.transcribe_mod.TranscribeResult(text="", start_ms=0, end_ms=0)
        with patch("hive.mqtt.zmq_stt_handler.transcribe_wav", return_value=result):
            session, response = self._perform()
        self.assertEqual(response.speech, "whisper text")
        self.assertTrue(session.fell_back)

    def test_muse_selected_but_no_key_falls_back_without_transcribe_call(self):
        self.factory.set_stt_provider("muse_transcribe")
        with patch("hive.mqtt.zmq_stt_handler.transcribe_wav") as mock_transcribe:
            session, response = self._perform()
        mock_transcribe.assert_not_called()
        self.assertEqual(response.speech, "whisper text")
        self.assertTrue(session.fell_back)

    def test_both_providers_failing_still_sends_error_response(self):
        self.factory.set_muse_spark_key("test-key-not-real")
        self.factory.set_stt_provider("muse_transcribe")
        self.whisper_api.side_effect = RuntimeError("synthetic whisper failure")
        with patch(
            "hive.mqtt.zmq_stt_handler.transcribe_wav",
            side_effect=self.transcribe_mod.MuseTranscribeError("HTTP 500", status=500),
        ):
            with self.assertLogs("hive.mqtt.zmq_stt_handler", level="WARNING"):
                _session, response = self._perform()
        self.assertEqual(response.error_code, 66)
        self.assertEqual(response.error_message, "synthetic whisper failure")


class ModelDefaultsTests(_FactoryCase):
    def test_hive_configuration_stt_defaults(self):
        from hive import models

        cfg = models.HiveConfiguration()
        self.assertEqual(cfg.stt_provider, "openai")
        self.assertEqual(cfg.muse_transcribe_api_key, "")


if __name__ == "__main__":
    unittest.main()
