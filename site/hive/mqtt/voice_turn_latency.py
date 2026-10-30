"""End-to-end voice-turn latency harness (mocked transport only).

Pipeline measured: mic-audio-in -> STT -> chat -> TTS-output-ready.
"TTS-output-ready" here means the point at which the chat reply text
is handed to the TTS stage; in the real system the robot synthesises
speech from that text, and no robot is involved in this harness, so
the TTS stage is a marker callable, not a synthesiser. Every stage
callable is supplied by the caller -- in tests they are mocks, on a
test server they are mocked transports -- so timings from this
harness prove plumbing and stage accounting, NOT real-world latency.
No live robot timing claim may be derived from it.

Stage contract:
- ``stt_fn(wav_bytes)`` returns either a transcript string, or a
  ``(text, fell_back, provider)`` tuple (the handler's utterance result).
- ``chat_fn(transcript)`` returns a reply string.
- ``tts_fn(reply)`` returns anything (its return value is recorded as
  ``tts_ready``); the default is a no-op marker.

``wer(reference, hypothesis)`` is a word-level Levenshtein error rate
used by the accuracy smoke harness: ``(substitutions + deletions +
insertions) / words_in_reference``, or 0.0 for two empty strings and
1.0 when the reference is empty but the hypothesis is not. It is a
harness metric over mocked transcripts, not a live accuracy claim.
"""

import time


class StageTiming:
    def __init__(self, name, duration_ms, detail=""):
        self.name = name
        self.duration_ms = duration_ms
        self.detail = detail

    def as_dict(self):
        return {"stage": self.name, "duration_ms": round(self.duration_ms, 3), "detail": self.detail}


class VoiceTurnResult:
    def __init__(self, transcript, reply, stages, total_ms, fell_back=False, provider="openai"):
        self.transcript = transcript
        self.reply = reply
        self.stages = stages
        self.total_ms = total_ms
        self.fell_back = fell_back
        self.provider = provider
        # Transport label: this harness is mocked/routing-only by design.
        self.transport = "mocked"

    def as_dict(self):
        return {
            "transport": self.transport,
            "transcript": self.transcript,
            "reply": self.reply,
            "stt_provider": self.provider,
            "fell_back": self.fell_back,
            "total_ms": round(self.total_ms, 3),
            "stages": [stage.as_dict() for stage in self.stages],
        }


def measure(fn, *args, **kwargs):
    """Run ``fn`` and return ``(value, duration_ms)`` wall-clock time."""
    start = time.perf_counter()
    value = fn(*args, **kwargs)
    return value, (time.perf_counter() - start) * 1000.0


def _tts_ready_marker(reply):
    return "tts-ready"


def run_mocked_voice_turn(wav_bytes, stt_fn, chat_fn, tts_fn=None):
    """Run one mocked voice turn and time each stage.

    Returns a ``VoiceTurnResult``. Wall-clock total is measured
    end-to-end and is >= the sum of stage times (stage sum excludes
    the small glue between stages). No network, broker, robot or key
    is touched by this function itself; that depends entirely on the
    callables the caller supplies.
    """
    if tts_fn is None:
        tts_fn = _tts_ready_marker
    overall_start = time.perf_counter()
    stt_value, stt_ms = measure(stt_fn, wav_bytes)
    if isinstance(stt_value, tuple):
        transcript, fell_back, provider = stt_value
    else:
        transcript, fell_back, provider = stt_value, False, "openai"
    reply, chat_ms = measure(chat_fn, transcript)
    tts_ready, tts_ms = measure(tts_fn, reply)
    total_ms = (time.perf_counter() - overall_start) * 1000.0
    stages = [
        StageTiming("stt", stt_ms, detail="provider=%s fell_back=%s" % (provider, fell_back)),
        StageTiming("chat", chat_ms),
        StageTiming("tts_ready", tts_ms, detail=str(tts_ready)),
    ]
    return VoiceTurnResult(
        transcript=transcript,
        reply=reply,
        stages=stages,
        total_ms=total_ms,
        fell_back=fell_back,
        provider=provider,
    )


def wer(reference, hypothesis):
    """Word error rate between two transcripts (see module docstring)."""
    ref_words = reference.split()
    hyp_words = hypothesis.split()
    if not ref_words:
        return 0.0 if not hyp_words else 1.0
    # Levenshtein over words.
    previous = list(range(len(hyp_words) + 1))
    for i, ref_word in enumerate(ref_words, start=1):
        current = [i]
        for j, hyp_word in enumerate(hyp_words, start=1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (0 if ref_word == hyp_word else 1),
            ))
        previous = current
    return previous[-1] / len(ref_words)
