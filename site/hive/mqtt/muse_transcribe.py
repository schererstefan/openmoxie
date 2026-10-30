"""Muse Voice Transcribe STT provider (one-shot file endpoint, clone/test only).

Model: ``muse-voice-transcribe-1.0`` on Meta Model API, STT only.
Endpoint: ``POST https://api.meta.ai/v1/asr/transcribe`` with an
``Authorization: Bearer <Meta Model API key>`` header and a multipart
upload. The streaming endpoint (``wss://api.meta.ai/v1/asr/realtime``,
80 ms chunks, diarization, endpointing) is a follow-up and is NOT
implemented here: this module deliberately matches the existing
whole-utterance flow, where the robot's VAD already delimits an utterance
and the server transcribes the finished recording in one call.

AUDIO FORMAT SENT (the main unverified risk, handled explicitly):
the existing handler already builds exactly what this endpoint accepts
-- mono, 16-bit PCM WAV at 16 kHz (``soundfile`` write: samplerate 16000,
``format='WAV'``, ``subtype='PCM_16'``, one channel from a 1-D int16
array). Per Meta's documented file-endpoint constraints, mono 16-bit
PCM WAV at 16 or 24 kHz only, max 32 MB body / 10 minutes per request.
``validate_wav_bytes`` enforces samplerate in (16000, 24000), exactly
1 channel and subtype PCM_16 before any network call, and raises
``ValueError`` otherwise so the caller falls back to Whisper instead of
sending an endpoint-rejected payload.

REQUEST SHAPE (partly unverified -- see checkpoint): multipart field
``file`` as ``utterance.wav`` with ``audio/wav`` content type, plus a
``model`` field carrying ``muse-voice-transcribe-1.0``. The endpoint URL,
Bearer-header auth, WAV constraints and price are grounded in Meta's
docs as summarised in CHECKPOINT-STT-SWAP.md; the exact multipart field
names and the exact JSON response shape were NOT published in the
sources available, so ``parse_transcribe_response`` accepts every
plausible shape (``text`` / ``transcript`` / ``transcription`` string,
or a ``turns``/``segments`` list with text plus start/end in ms or
seconds) and the live endpoint call is still pending verification.
Until that live call succeeds, Transcribe must stay opt-in and Whisper
must stay as the fallback -- which is exactly how the handler uses it.

KEYS: the key is passed in by the caller (the effective key comes from
``ai_factory.get_muse_transcribe_key()``: explicit Transcribe override,
else the Muse Spark key -- same Meta Model API key family, same base
URL, same Bearer scheme). This module never logs the key, never logs
transcript content, and never puts the key in a URL or error message;
``MuseTranscribeError`` carries only an HTTP status / short error kind.
"""

import io
import logging

import requests
import soundfile as sf

logger = logging.getLogger(__name__)

MUSE_TRANSCRIBE_BASE_URL = "https://api.meta.ai/v1"
MUSE_TRANSCRIBE_URL = MUSE_TRANSCRIBE_BASE_URL + "/asr/transcribe"
MUSE_TRANSCRIBE_MODEL = "muse-voice-transcribe-1.0"
# Same fail-fast budget as the chat factory: a spoken turn leaves the
# robot silent while it waits, so a hung provider must not stall a
# worker thread indefinitely. The handler falls back to Whisper on
# timeout, so this is a per-attempt ceiling, not a dropped turn.
TRANSCRIBE_TIMEOUT_SECONDS = 20.0
ALLOWED_SAMPLE_RATES = (16000, 24000)
MAX_BODY_BYTES = 32 * 1024 * 1024


class MuseTranscribeError(Exception):
    """Transcribe failure. Carries status/kind only -- never a key."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def validate_wav_bytes(wav_bytes):
    """Validate the exact audio format the endpoint accepts.

    Returns ``(samplerate, channels, frames)``. Raises ``ValueError``
    for anything other than mono 16-bit PCM WAV at 16 or 24 kHz, and
    ``MuseTranscribeError`` for an oversize body. No audio content or
    key is ever included in an error message.
    """
    if not wav_bytes:
        raise ValueError("Empty WAV payload for Muse Transcribe")
    if len(wav_bytes) > MAX_BODY_BYTES:
        raise MuseTranscribeError("WAV payload exceeds 32 MB endpoint cap")
    try:
        info = sf.info(io.BytesIO(wav_bytes))
    except Exception as exc:
        raise ValueError("Unparseable WAV payload for Muse Transcribe") from exc
    if info.samplerate not in ALLOWED_SAMPLE_RATES:
        raise ValueError(
            "Muse Transcribe WAV samplerate %s not in %s"
            % (info.samplerate, ALLOWED_SAMPLE_RATES)
        )
    if info.channels != 1:
        raise ValueError(
            "Muse Transcribe WAV must be mono, got %s channels" % info.channels
        )
    if info.subtype != "PCM_16":
        raise ValueError(
            "Muse Transcribe WAV must be PCM_16, got %s" % info.subtype
        )
    return info.samplerate, info.channels, info.frames


def _turn_bounds(item):
    """(start_ms, end_ms) for one turn/segment dict, or (0, 0)."""
    if not isinstance(item, dict):
        return 0, 0
    start_ms = item.get("start_ms")
    end_ms = item.get("end_ms")
    if start_ms is None and item.get("start") is not None:
        start_ms = item["start"] * 1000
    if end_ms is None and item.get("end") is not None:
        end_ms = item["end"] * 1000
    try:
        return int(start_ms or 0), int(end_ms or 0)
    except (TypeError, ValueError):
        return 0, 0


def parse_transcribe_response(payload):
    """Extract ``(text, start_ms, end_ms)`` from a Transcribe response.

    Accepts the plausible response shapes (see module docstring) because
    Meta's published docs do not pin the one-shot JSON shape down:
    a top-level text string, or a ``turns``/``segments`` list whose
    texts are joined in order with min-start / max-end bounds. Returns
    ``("", 0, 0)`` when no text is present; the caller treats an empty
    transcript as a Transcribe failure and falls back to Whisper.
    """
    if not isinstance(payload, dict):
        raise MuseTranscribeError("Transcribe response was not a JSON object")
    for key in ("text", "transcript", "transcription"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            start_ms = payload.get("start_ms")
            end_ms = payload.get("end_ms")
            try:
                return value, int(start_ms or 0), int(end_ms or 0)
            except (TypeError, ValueError):
                return value, 0, 0
    for key in ("turns", "segments"):
        items = payload.get(key)
        if isinstance(items, list) and items:
            texts = []
            starts = []
            ends = []
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    texts.append(item["text"])
                    start_ms, end_ms = _turn_bounds(item)
                    starts.append(start_ms)
                    ends.append(end_ms)
                elif isinstance(item, str):
                    texts.append(item)
            if texts:
                return (
                    " ".join(texts),
                    min(starts) if starts else 0,
                    max(ends) if ends else 0,
                )
    return "", 0, 0


class TranscribeResult:
    """One-shot Transcribe outcome. ``fell_back`` is always False here.

    The fallback flag lives on the handler's utterance result, not on
    this provider result: this object means "Transcribe itself answered".
    """

    def __init__(self, text, start_ms=0, end_ms=0):
        self.text = text
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.provider = "muse_transcribe"
        self.fell_back = False


def transcribe_wav(wav_bytes, api_key, http_post=None):
    """Transcribe one whole-utterance WAV via the one-shot endpoint.

    ``http_post`` is injectable for tests (defaults to
    ``requests.post``); tests pass a fake and never touch the network.
    Raises ``ValueError`` for a bad WAV format, ``MuseTranscribeError``
    for a missing key, HTTP error or unparseable response. The key is
    sent only as a Bearer header and is never logged or returned.
    """
    validate_wav_bytes(wav_bytes)
    if not api_key or not str(api_key).strip():
        raise MuseTranscribeError("No Muse Transcribe key configured")
    post = http_post if http_post is not None else requests.post
    response = post(
        MUSE_TRANSCRIBE_URL,
        headers={"Authorization": "Bearer " + str(api_key), "Accept": "application/json"},
        files={"file": ("utterance.wav", wav_bytes, "audio/wav")},
        data={"model": MUSE_TRANSCRIBE_MODEL},
        timeout=TRANSCRIBE_TIMEOUT_SECONDS,
    )
    status = getattr(response, "status_code", None)
    if status != 200:
        # Status only: response bodies are not echoed, so nothing
        # sensitive can leak into logs through an error path.
        raise MuseTranscribeError(
            "Muse Transcribe endpoint returned HTTP %s" % status, status=status
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise MuseTranscribeError("Unparseable Muse Transcribe response") from exc
    text, start_ms, end_ms = parse_transcribe_response(payload)
    logger.info("Muse Transcribe returned a transcript (content not logged)")
    return TranscribeResult(text=text, start_ms=start_ms, end_ms=end_ms)
