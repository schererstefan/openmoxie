"""Muse Voice Transcribe streaming (realtime) STT provider -- opt-in, UNVERIFIED live.

Endpoint: ``wss://api.meta.ai/v1/asr/realtime``, model
``muse-voice-transcribe-1.0``, 80 ms audio chunks. Audio is the same
mono 16 kHz int16 PCM the robot already streams, so one chunk is
``16000 * 0.080 * 2 = 2560`` bytes with no resampling or re-encoding.

UNVERIFIED, stated plainly: no live realtime call was made in this
branch, so the exact handshake shape, config-message field names and
provider message field names below are *plausible guesses*, not
confirmed wire behaviour. The one-shot endpoint in
``muse_transcribe.py`` had the same caveat for its multipart/response
shape; here it covers the whole handshake. That is why this module is
transport-injectable and why streaming stays OFF by default behind
both ``stt_provider='muse_streaming'`` and ``stt_streaming_enabled``:
a wrong guess surfaces as a per-utterance failure and the handler
falls back to one-shot Transcribe, then Whisper-1. Do not claim live
streaming behaviour, live latency, or accuracy from this module --
mocked timings prove plumbing only.

TRANSPORT CONTRACT: the caller injects ``websocket_factory``, a
callable taking the API key (``websocket_factory(api_key)``) and
returning a transport object with:

- ``send_bytes(payload: bytes)`` -- send one payload (the JSON config
  message first, then raw 2560-byte audio chunks);
- ``recv_message()`` -- return the next provider message as a ``dict``,
  or ``None`` when no message is available right now; raising means
  the transport is closed/broken;
- ``close()`` -- close the connection.

Authentication is the factory's job: the key is handed to the factory
once and this module never logs it, never puts it in the config
message, a URL, or an error. ``StreamingError`` / ``StreamingUnavailable``
carry a short ``kind`` string only -- never transcript content, never
a key, never speaker/diarization labels.

No websocket library is a hard dependency: :func:`default_websocket_factory`
imports ``websocket-client`` lazily and raises ``StreamingUnavailable``
when it (or a key) is absent, which is the expected offline behaviour
and simply engages the handler fallback chain. Tests always inject a
fake factory and never touch the network.

PRIVACY: partial transcripts are held in memory for endpointing only.
They are never logged with content, never written to disk, and never
forwarded to the robot (the handler replies only on a final). Final
text is returned to the caller, not logged here. Diarization speaker
labels, if a provider ever sent them, are ignored by the parser and
never stored or logged.
"""
import json
import logging
import re
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MUSE_STREAMING_WS_URL = "wss://api.meta.ai/v1/asr/realtime"
MUSE_STREAMING_MODEL = "muse-voice-transcribe-1.0"
SAMPLE_RATE_HZ = 16000
CHUNK_MS = 80
BYTES_PER_SAMPLE = 2  # int16 mono
CHUNK_BYTES = SAMPLE_RATE_HZ * CHUNK_MS // 1000 * BYTES_PER_SAMPLE  # 2560

PARTIAL_TYPES = ("partial", "interim")
FINAL_TYPES = ("final",)
ENDPOINT_TYPES = ("endpoint", "turn_complete")

_KIND_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class StreamingError(Exception):
    """Streaming failure. ``kind`` only -- never a key or transcript."""

    def __init__(self, kind):
        self.kind = str(kind) if kind else "streaming_error"
        super().__init__(self.kind)


class StreamingUnavailable(StreamingError):
    """No usable streaming transport (library/key absent). Kind only."""


@dataclass
class StreamingConfig:
    """Realtime session knobs (built from validated dashboard values).

    ``language_hint`` is ``None`` for auto-detect, else a language
    code from the server-side allowlist in ``stt_knobs``.
    ``endpointing_mode`` is ``'auto'`` / ``'server_timeout'`` /
    ``'disabled'``; ``endpointing_timeout_ms`` is the server-side
    silence backstop used by the first two modes.
    """

    diarization: bool = False
    language_hint: object = None
    keyword_bias: tuple = ()
    code_switching: bool = False
    endpointing_mode: str = "auto"
    endpointing_timeout_ms: int = 1000


@dataclass
class StreamingEvent:
    """One parsed provider event.

    ``kind`` is ``'partial'`` or ``'final'`` (endpoint promotions are
    finals with ``endpoint_by`` set). ``endpoint_by`` is one of
    ``None``, ``'provider'``, ``'server_timeout'`` or ``'finish'``.
    The repr deliberately omits ``text`` so an event can be logged or
    shown in a traceback without leaking transcript content.
    """

    kind: str
    text: str = ""
    endpoint_by: object = None

    @property
    def is_final(self):
        return self.kind == "final"

    def __repr__(self):
        return "StreamingEvent(kind=%r, chars=%d, endpoint_by=%r)" % (
            self.kind, len(self.text or ""), self.endpoint_by)


def build_config_message(config):
    """Session config dict for the (UNVERIFIED) realtime handshake.

    Contains model, audio format and the knob fields only: no API key
    (auth belongs to the transport factory) and no transcript. Field
    names are unverified guesses -- see the module docstring; a live
    rejection is fixed here, in the clone, never by guessing live.
    """
    return {
        "type": "session.configure",
        "model": MUSE_STREAMING_MODEL,
        "audio": {
            "sample_rate": SAMPLE_RATE_HZ,
            "encoding": "pcm_s16le",
            "channels": 1,
            "chunk_ms": CHUNK_MS,
        },
        "diarization": bool(config.diarization),
        # None means provider auto-detect; never the string "auto" here
        # so the wire shape stays distinct from the stored knob value.
        "language": config.language_hint,
        "keyword_bias": list(config.keyword_bias or ()),
        "code_switching": bool(config.code_switching),
        "endpointing": {
            "mode": config.endpointing_mode,
            "timeout_ms": int(config.endpointing_timeout_ms),
        },
    }


def _message_text(message):
    for key in ("text", "transcript", "transcription"):
        value = message.get(key)
        if isinstance(value, str):
            return value
    return ""


def _error_kind(message):
    """Short provider error kind, sanitised; never message content."""
    for key in ("kind", "error_kind", "code"):
        value = message.get(key)
        if isinstance(value, str) and _KIND_RE.match(value.strip()):
            return value.strip()
    error = message.get("error")
    if isinstance(error, dict):
        for key in ("kind", "error_kind", "code"):
            value = error.get(key)
            if isinstance(value, str) and _KIND_RE.match(value.strip()):
                return value.strip()
    elif isinstance(error, str) and _KIND_RE.match(error.strip()):
        return error.strip()
    return "provider_error"


def _transport_kind(exc):
    if isinstance(exc, (ConnectionError, EOFError, OSError)):
        return "transport_closed"
    return "transport_error"


class _WebSocketClientTransport:
    """Real-transport adapter over websocket-client (UNVERIFIED live).

    Only constructed by :func:`default_websocket_factory` after a
    lazy import succeeds. Config JSON is sent as a text frame and
    audio as binary frames (a guess -- see module docstring);
    ``recv_message`` uses a short timeout so ``pump()`` can poll
    without blocking the handler indefinitely.
    """

    def __init__(self, ws):
        self._ws = ws

    def send_bytes(self, payload):
        if payload[:1] == b"{":
            self._ws.send(payload.decode("utf-8"))
        else:
            self._ws.send_binary(payload)

    def recv_message(self):
        import websocket  # local: only reachable via the lazy factory

        try:
            raw = self._ws.recv()
        except websocket.WebSocketTimeoutException:
            return None
        if raw is None or raw == "":
            raise ConnectionError("closed")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="strict")
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def default_websocket_factory(api_key):
    """Lazy real transport. Raises ``StreamingUnavailable`` (kind only).

    No key -> ``no_key``; no websocket-client library ->
    ``websocket_unavailable``; a failed connect -> ``transport_closed``
    / ``transport_error`` as :class:`StreamingError`. The key is sent
    only as a Bearer [REDACTED] on the handshake and never logged.
    """
    if not api_key or not str(api_key).strip():
        raise StreamingUnavailable("no_key")
    try:
        import websocket
    except ImportError as exc:
        raise StreamingUnavailable("websocket_unavailable") from exc
    try:
        ws = websocket.create_connection(
            MUSE_STREAMING_WS_URL,
            header=["Authorization: Bearer " + str(api_key)],
            timeout=10,
        )
        ws.settimeout(0.05)
    except Exception as exc:
        raise StreamingError(_transport_kind(exc)) from exc
    return _WebSocketClientTransport(ws)


class StreamingSession:
    """One realtime utterance session over an injected transport.

    Lifecycle: :meth:`connect` (sends the config message),
    :meth:`feed_audio` (exact 80 ms chunks; a trailing partial chunk
    stays buffered and is never padded with invented silence),
    :meth:`pump` (drains provider messages into events, applying
    server-side endpointing), :meth:`finish` (drains once more,
    closes, returns the final text or raises ``StreamingError``).

    ``first_partial_ms`` / ``final_ms`` are monotonic durations from
    session start (numbers only, for the mocked timing table).
    ``monotonic`` is injectable so endpointing tests use a fake clock
    instead of real sleeps.
    """

    def __init__(self, config, websocket_factory=None, api_key=None,
                 monotonic=None):
        self.config = config
        self._factory = websocket_factory
        self._api_key = api_key
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._transport = None
        self._buffer = bytearray()
        self._started_at = None
        self._last_activity = None
        self.last_partial = None
        self.partial_at = None
        self.final_text = None
        self.endpoint_by = None
        self.first_partial_ms = None
        self.final_ms = None
        self._closed = False

    @property
    def buffered_bytes(self):
        """Bytes held back as an unsent trailing partial chunk."""
        return len(self._buffer)

    def _now(self):
        return self._monotonic()

    def _elapsed_ms(self, now):
        if self._started_at is None:
            return 0.0
        return max(0.0, (now - self._started_at) * 1000.0)

    def connect(self):
        """Open the transport and send the config message. Idempotent."""
        if self._transport is not None:
            return self
        factory = self._factory if self._factory is not None else default_websocket_factory
        try:
            transport = factory(self._api_key)
        except StreamingError:
            raise
        except Exception as exc:
            raise StreamingError(_transport_kind(exc)) from exc
        if transport is None:
            raise StreamingUnavailable("no_transport")
        self._transport = transport
        self._closed = False
        self._started_at = self._now()
        self._last_activity = self._started_at
        payload = json.dumps(build_config_message(self.config)).encode("utf-8")
        try:
            self._transport.send_bytes(payload)
        except StreamingError:
            raise
        except Exception as exc:
            raise StreamingError(_transport_kind(exc)) from exc
        logger.info(
            "Muse streaming session connected (diarization=%s endpointing=%s; content not logged)",
            bool(self.config.diarization), self.config.endpointing_mode)
        return self

    def feed_audio(self, pcm_bytes):
        """Buffer PCM and send each full 80 ms chunk. Returns chunks sent.

        A trailing partial chunk (<2560 bytes) stays buffered; it is
        never padded with silence and never sent by :meth:`finish`.
        """
        if self._transport is None:
            self.connect()
        if not pcm_bytes:
            return 0
        self._buffer += bytes(pcm_bytes)
        self._last_activity = self._now()
        sent = 0
        while len(self._buffer) >= CHUNK_BYTES:
            chunk = bytes(self._buffer[:CHUNK_BYTES])
            try:
                self._transport.send_bytes(chunk)
            except StreamingError:
                raise
            except Exception as exc:
                raise StreamingError(_transport_kind(exc)) from exc
            del self._buffer[:CHUNK_BYTES]
            sent += 1
        return sent

    def _promote_partial(self, endpoint_by, now):
        self.final_text = self.last_partial
        self.endpoint_by = endpoint_by
        if self.final_ms is None:
            self.final_ms = self._elapsed_ms(now)
        return StreamingEvent(
            kind="final", text=self.final_text or "", endpoint_by=endpoint_by)

    def _apply_message(self, message, now, events):
        if not isinstance(message, dict):
            return
        mtype = message.get("type")
        if not isinstance(mtype, str):
            return
        mtype = mtype.strip().lower()
        self._last_activity = now
        if mtype in PARTIAL_TYPES:
            text = _message_text(message)
            self.last_partial = text
            self.partial_at = now
            if self.first_partial_ms is None:
                self.first_partial_ms = self._elapsed_ms(now)
            events.append(StreamingEvent(kind="partial", text=text))
        elif mtype in FINAL_TYPES:
            text = _message_text(message)
            self.final_text = text
            self.endpoint_by = "provider"
            if self.final_ms is None:
                self.final_ms = self._elapsed_ms(now)
            events.append(StreamingEvent(
                kind="final", text=text, endpoint_by="provider"))
        elif mtype in ENDPOINT_TYPES:
            text = _message_text(message)
            if text:
                self.last_partial = text
                self.partial_at = now
                if self.first_partial_ms is None:
                    self.first_partial_ms = self._elapsed_ms(now)
            if self.last_partial:
                events.append(self._promote_partial("provider", now))
        elif mtype == "error":
            raise StreamingError(_error_kind(message))
        else:
            # Unknown provider chatter: ignored, content never logged.
            logger.debug("Muse streaming ignored message type=%s", mtype)

    def pump(self):
        """Drain available messages; return the new events (possibly empty).

        Server-side endpointing: in ``'server_timeout'`` mode -- and
        in ``'auto'`` mode as a backstop behind provider endpoint
        events -- a partial with no new audio/message for longer
        than ``endpointing_timeout_ms`` is promoted to a final with
        ``endpoint_by='server_timeout'``. ``'disabled'`` never
        promotes here. A closed/broken transport raises
        ``StreamingError`` (kind only).
        """
        if self._transport is None:
            self.connect()
        events = []
        while True:
            try:
                message = self._transport.recv_message()
            except StreamingError:
                raise
            except Exception as exc:
                raise StreamingError(_transport_kind(exc)) from exc
            if message is None:
                break
            self._apply_message(message, self._now(), events)
            if self.final_text is not None:
                break
        if (
            self.final_text is None
            and self.last_partial
            and self.config.endpointing_mode in ("server_timeout", "auto")
            and self._last_activity is not None
        ):
            now = self._now()
            idle_ms = (now - self._last_activity) * 1000.0
            if idle_ms > float(self.config.endpointing_timeout_ms):
                events.append(self._promote_partial("server_timeout", now))
                logger.info(
                    "Muse streaming server-timeout endpoint (content not logged)")
        return events

    def close(self):
        if self._transport is not None and not self._closed:
            self._closed = True
            try:
                self._transport.close()
            except Exception:
                pass

    def finish(self):
        """End the turn: drain, close, return final text or raise.

        Flushes nothing invented (the buffered trailing partial chunk
        is discarded, never padded/sent). If no provider final arrived
        but a partial exists, the partial is promoted with
        ``endpoint_by='finish'`` -- this is how ``'disabled'``
        endpointing still ends a turn at the robot's END_OF_SPEECH.
        With neither final nor partial, raises
        ``StreamingError('no_final')`` so the handler falls back.
        """
        try:
            if self.final_text is None:
                self.pump()
            if self.final_text is not None:
                return self.final_text
            if self.last_partial:
                now = self._now()
                self.final_text = self.last_partial
                self.endpoint_by = "finish"
                if self.final_ms is None:
                    self.final_ms = self._elapsed_ms(now)
                return self.final_text
            raise StreamingError("no_final")
        finally:
            self.close()
