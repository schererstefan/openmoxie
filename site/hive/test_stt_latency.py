"""Timed STT / chat latency tests + accuracy smoke harness (mocked only).

Run from site with the venv python:
    python -m unittest hive.test_stt_latency -v

Mocked timings prove plumbing and stage accounting, NOT real-world
latency: a mocked call returns in well under a millisecond locally, so
these tests assert only that a timing IS recorded (a finite,
non-negative float) and that it sits under a generous local ceiling
(5 s), never a brittle absolute threshold. The one live-timed test is
gated on BOTH an explicit opt-in flag AND a test key supplied by the
operator (``MOXIE_STT_LIVE_TIMING=1`` plus
``MUSE_TRANSCRIBE_TEST_API_KEY``); with neither set -- the normal case
-- it skips cleanly and no paid live call is made. No key value is
ever logged or printed by these tests.

Accuracy: no recorded audio is checked in. The "sample" is a synthetic
WAV generated in memory (silence, endpoint-valid format) and both
providers are mocked with canned transcripts, so the WER comparison
exercises the harness only. Live accuracy scoring (a real recorded
phrase through both live providers) is still pending and needs
daylight with Stefan in view.
"""
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf


def _synthetic_wav(frames=16000):
    """Synthetic, endpoint-valid sample: 1 s of silence, 16 kHz mono PCM_16.

    Contains no speech and no personal data; it is a format placeholder
    for the harness, not a phrase to score accuracy against.
    """
    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(frames, dtype=np.int16), 16000, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


class _LatencyCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import django
        from django.conf import settings

        if not settings.configured:
            settings.configure(
                INSTALLED_APPS=["hive"],
                TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates"}],
                SECRET_KEY="offline-stt-latency-tests",
            )
        django.setup()
        from hive.mqtt import ai_factory, muse_transcribe, voice_turn_latency, zmq_stt_handler

        cls.factory = ai_factory
        cls.transcribe_mod = muse_transcribe
        cls.latency = voice_turn_latency
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

    def assert_timing_recorded(self, duration_ms):
        self.assertIsInstance(duration_ms, float)
        self.assertGreaterEqual(duration_ms, 0.0)
        self.assertLess(duration_ms, 5000.0)


class TimedTranscribeTests(_LatencyCase):
    def _fake_post(self, payload=None):
        response = Mock()
        response.status_code = 200
        response.json.return_value = payload if payload is not None else {"text": "hello there"}
        return response

    def test_mocked_transcribe_call_is_timed(self):
        value, elapsed_ms = self.latency.measure(
            self.transcribe_mod.transcribe_wav,
            _synthetic_wav(), "test-key-not-real",
            http_post=lambda url, **kw: self._fake_post(),
        )
        self.assertEqual(value.text, "hello there")
        self.assert_timing_recorded(elapsed_ms)

    def test_mocked_transcribe_error_still_timed(self):
        import time

        def failing_post(url, **kw):
            raise TimeoutError("synthetic timeout")

        start = time.perf_counter()
        with self.assertRaises(TimeoutError):
            self.transcribe_mod.transcribe_wav(
                _synthetic_wav(), "test-key-not-real", http_post=failing_post,
            )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self.assert_timing_recorded(elapsed_ms)


class TimedWhisperTests(_LatencyCase):
    def test_mocked_whisper_call_is_timed(self):
        server = Mock()
        handler = self.stt_mod.STTHandler(server)
        self.addCleanup(handler._worker_queue.shutdown)
        with patch("hive.mqtt.zmq_stt_handler.create_openai") as factory:
            api = factory.return_value.audio.transcriptions.create
            api.return_value = SimpleNamespace(text="hello there", segments=[])
            session = self.stt_mod.STTSession(handler, "robot", "utterance")
            (text, _start, _end, fell_back, provider), elapsed_ms = self.latency.measure(
                session._transcribe, _synthetic_wav()
            )
        self.assertEqual(text, "hello there")
        self.assertFalse(fell_back)
        self.assertEqual(provider, "openai")
        self.assert_timing_recorded(elapsed_ms)


class TimedChatTests(_LatencyCase):
    def test_mocked_spark_chat_call_is_timed(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Hi there!"))]
        )

        def mock_chat(transcript):
            response = client.chat.completions.create(model="muse-spark-1.3", messages=[])
            return response.choices[0].message.content

        reply, elapsed_ms = self.latency.measure(mock_chat, "hello there")
        self.assertEqual(reply, "Hi there!")
        self.assert_timing_recorded(elapsed_ms)

    def test_stt_vs_chat_timings_both_recorded_for_comparison(self):
        timings = {}
        _value, timings["stt_mocked_ms"] = self.latency.measure(
            self.transcribe_mod.transcribe_wav,
            _synthetic_wav(), "test-key-not-real",
            http_post=lambda url, **kw: Mock(status_code=200, json=lambda: {"text": "hello there"}),
        )
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Hi!"))]
        )
        _reply, timings["chat_mocked_ms"] = self.latency.measure(
            lambda transcript: client.chat.completions.create().choices[0].message.content,
            "hello there",
        )
        for duration_ms in timings.values():
            self.assert_timing_recorded(duration_ms)
        # Comparison shape only: both stages present, neither is a live figure.
        self.assertEqual(set(timings), {"stt_mocked_ms", "chat_mocked_ms"})


@unittest.skipUnless(
    os.environ.get("MOXIE_STT_LIVE_TIMING") == "1" and os.environ.get("MUSE_TRANSCRIBE_TEST_API_KEY"),
    "Live timing skipped: MOXIE_STT_LIVE_TIMING=1 and MUSE_TRANSCRIBE_TEST_API_KEY are not both set; no paid live call is made.",
)
class LiveTimedTranscribeTests(_LatencyCase):
    def test_live_transcribe_call_is_timed(self):
        # Key is used transiently as a Bearer header only; never logged.
        key = os.environ["MUSE_TRANSCRIBE_TEST_API_KEY"]
        result, elapsed_ms = self.latency.measure(
            self.transcribe_mod.transcribe_wav, _synthetic_wav(), key
        )
        self.assertIsInstance(result.text, str)
        self.assert_timing_recorded(elapsed_ms)


class EndToEndVoiceTurnTests(_LatencyCase):
    def test_mocked_voice_turn_times_each_stage(self):
        result = self.latency.run_mocked_voice_turn(
            _synthetic_wav(),
            stt_fn=lambda wav: "hello there",
            chat_fn=lambda transcript: "Hi there!",
        )
        self.assertEqual(result.transport, "mocked")
        self.assertEqual((result.transcript, result.reply), ("hello there", "Hi there!"))
        self.assertEqual([stage.name for stage in result.stages], ["stt", "chat", "tts_ready"])
        for stage in result.stages:
            self.assert_timing_recorded(stage.duration_ms)
        stage_sum = sum(stage.duration_ms for stage in result.stages)
        self.assertGreaterEqual(result.total_ms, 0.0)
        self.assertLess(result.total_ms, 5000.0)
        # Total is wall-clock end-to-end, so it covers (not undercounts) stages.
        self.assertGreaterEqual(result.total_ms + 1.0, stage_sum)

    def test_mocked_voice_turn_with_whisper_fallback(self):
        server = Mock()
        handler = self.stt_mod.STTHandler(server)
        self.addCleanup(handler._worker_queue.shutdown)
        self.factory.set_muse_spark_key("test-key-not-real")
        self.factory.set_stt_provider("muse_transcribe")
        with patch("hive.mqtt.zmq_stt_handler.create_openai") as factory:
            api = factory.return_value.audio.transcriptions.create
            api.return_value = SimpleNamespace(text="hello there", segments=[])
            with patch(
                "hive.mqtt.zmq_stt_handler.transcribe_wav",
                side_effect=self.transcribe_mod.MuseTranscribeError("HTTP 401", status=401),
            ):
                session = self.stt_mod.STTSession(handler, "robot", "utterance")

                def stt_fn(wav):
                    text, _s, _e, fell_back, provider = session._transcribe(wav)
                    return text, fell_back, provider

                result = self.latency.run_mocked_voice_turn(
                    _synthetic_wav(), stt_fn=stt_fn, chat_fn=lambda transcript: "Hi!",
                )
        self.assertTrue(result.fell_back)
        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.transcript, "hello there")


class AccuracySmokeTests(_LatencyCase):
    def test_synthetic_sample_is_endpoint_valid(self):
        samplerate, channels, frames = self.transcribe_mod.validate_wav_bytes(_synthetic_wav())
        self.assertEqual((samplerate, channels, frames), (16000, 1, 16000))

    def test_wer_identical_is_zero(self):
        self.assertEqual(self.latency.wer("hello there", "hello there"), 0.0)

    def test_wer_one_substitution_in_two_words(self):
        self.assertEqual(self.latency.wer("hello there", "hello here"), 0.5)

    def test_wer_empty_reference_edge_cases(self):
        self.assertEqual(self.latency.wer("", ""), 0.0)
        self.assertEqual(self.latency.wer("", "hello"), 1.0)

    def test_mocked_providers_agreeing_scores_zero(self):
        reference = "hello there"
        muse_text = "hello there"
        whisper_text = "hello there"
        self.assertEqual(muse_text, whisper_text)
        self.assertEqual(self.latency.wer(reference, muse_text), 0.0)
        self.assertEqual(self.latency.wer(reference, whisper_text), 0.0)

    def test_mocked_providers_disagreeing_scores_difference(self):
        reference = "hello there"
        muse_text = "hello there"
        whisper_text = "hello here"
        self.assertEqual(self.latency.wer(reference, muse_text), 0.0)
        self.assertEqual(self.latency.wer(reference, whisper_text), 0.5)


if __name__ == "__main__":
    unittest.main()
