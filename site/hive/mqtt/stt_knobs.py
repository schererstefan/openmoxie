"""Validated STT knobs: provider mode resolution + raw-input validation.

Pure Python (no Django import) so the MQTT handler, the setup view and
offline tests can share exactly one validation path. Raw dashboard
input must go through :func:`validate_stt_knobs` before it is stored
or sent anywhere; nothing in this module touches the network, logs
transcripts, or handles API keys (``has_key`` is a boolean only).

LANGUAGE ALLOWLIST: ``'auto'`` plus the 25 codes in
:data:`VALIDATED_LANGUAGE_CODES`. This is a *server-side* allowlist
enforced here so a bad value can never reach a provider config. It is
NOT a claim about Meta's official validated/supported language list
for Muse Voice Transcribe -- that list has not been verified against
a live endpoint in this branch, and the codes here are simply the
choices the dashboard offers.

MODE RESOLUTION (:func:`resolve_stt_mode`): given the configured
``stt_provider``, the separate ``stt_streaming_enabled`` switch and
whether an effective Muse key is present:

============================================  ==========================
Configuration                                 Effective mode
============================================  ==========================
``openai``                                    ``whisper``
``muse_transcribe`` + key                     ``muse_oneshot``
``muse_transcribe`` + no key                  ``whisper`` (fell_back)
``muse_streaming`` + enabled + key            ``muse_streaming``
``muse_streaming`` + enabled + no key         ``whisper`` (fell_back)
``muse_streaming`` + disabled + key           ``muse_oneshot``
``muse_streaming`` + disabled + no key        ``whisper`` (fell_back)
============================================  ==========================

Streaming therefore needs BOTH the provider choice and its own switch;
with the switch off, ``muse_streaming`` behaves as the proven one-shot
path. Whisper-1 stays the ultimate fallback in the handler chain
(streaming -> one-shot -> Whisper -> error reply); this module only
picks the starting point.
"""

STT_PROVIDER_OPENAI = "openai"
STT_PROVIDER_MUSE_TRANSCRIBE = "muse_transcribe"
STT_PROVIDER_MUSE_STREAMING = "muse_streaming"
VALID_STT_PROVIDERS = (
    STT_PROVIDER_OPENAI,
    STT_PROVIDER_MUSE_TRANSCRIBE,
    STT_PROVIDER_MUSE_STREAMING,
)

ENDPOINTING_AUTO = "auto"
ENDPOINTING_SERVER_TIMEOUT = "server_timeout"
ENDPOINTING_DISABLED = "disabled"
VALID_ENDPOINTING_MODES = (
    ENDPOINTING_AUTO,
    ENDPOINTING_SERVER_TIMEOUT,
    ENDPOINTING_DISABLED,
)

LANGUAGE_AUTO = "auto"
# Server-side allowlist only -- see module docstring. Exactly 25 codes.
VALIDATED_LANGUAGE_CODES = (
    "en", "es", "fr", "de", "it", "pt", "nl", "pl", "cs", "sk",
    "hu", "ro", "bg", "uk", "ru", "ar", "hi", "ja", "ko", "zh",
    "tr", "sv", "da", "fi", "no",
)
VALID_LANGUAGE_HINTS = (LANGUAGE_AUTO,) + VALIDATED_LANGUAGE_CODES

ENDPOINTING_TIMEOUT_MIN_MS = 200
ENDPOINTING_TIMEOUT_MAX_MS = 5000
ENDPOINTING_TIMEOUT_DEFAULT_MS = 1000

KEYWORD_MAX_ITEMS = 20
KEYWORD_MAX_ITEM_CHARS = 40
KEYWORD_MAX_TOTAL_CHARS = 500
# Letters/digits/space/hyphen/apostrophe only (checked per character so
# accented letters in names are allowed without an ASCII-only regex).
_KEYWORD_EXTRA_CHARS = frozenset(" -'")

MODE_WHISPER = "whisper"
MODE_MUSE_ONESHOT = "muse_oneshot"
MODE_MUSE_STREAMING = "muse_streaming"

DEFAULT_KNOBS = {
    "stt_provider": STT_PROVIDER_OPENAI,
    "stt_streaming_enabled": False,
    "stt_diarization_enabled": False,
    "stt_endpointing_mode": ENDPOINTING_AUTO,
    "stt_endpointing_timeout_ms": ENDPOINTING_TIMEOUT_DEFAULT_MS,
    "stt_language_hint": LANGUAGE_AUTO,
    "stt_keyword_bias": "",
    "stt_code_switching_enabled": False,
}

_TRUTHY_STRINGS = frozenset({"on", "true", "1"})


def parse_bool(value):
    """Dashboard boolean: True only for ``True``/``1``/'on'/'true'/'1'."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_STRINGS
    return False


def _keyword_item_ok(item):
    if not item or len(item) > KEYWORD_MAX_ITEM_CHARS:
        return False
    return all(ch.isalnum() or ch in _KEYWORD_EXTRA_CHARS for ch in item)


def clean_keyword_items(raw_value):
    """Clean a comma-separated keyword value into a capped item list.

    Empty items are dropped; items over 40 chars or containing any
    character other than letters/digits/space/hyphen/apostrophe are
    dropped whole (never sanitised in place); at most 20 items are
    kept; the joined stored string is capped at 500 chars by dropping
    trailing items whole, never by cutting an item mid-way.
    Returns ``(items, changed)`` where ``changed`` is True when any
    item was dropped or truncated away.
    """
    if raw_value is None:
        return [], False
    if isinstance(raw_value, (list, tuple)):
        pieces = [str(v) for v in raw_value]
    else:
        pieces = str(raw_value).split(",")
    changed = False
    items = []
    for piece in pieces:
        item = piece.strip()
        if not item:
            # Empty items (",," / trailing comma) are dropped silently;
            # they do not count as a rejection on their own.
            continue
        if not _keyword_item_ok(item):
            changed = True
            continue
        items.append(item)
    if len(items) > KEYWORD_MAX_ITEMS:
        items = items[:KEYWORD_MAX_ITEMS]
        changed = True
    while items and len(", ".join(items)) > KEYWORD_MAX_TOTAL_CHARS:
        items.pop()
        changed = True
    return items, changed


def join_keyword_items(items):
    return ", ".join(items)


def keyword_list_from_stored(stored):
    """Stored keyword string -> tuple of items (re-validated, capped)."""
    items, _changed = clean_keyword_items(stored)
    return tuple(items)


def clamp_endpointing_timeout_ms(value):
    """Parse/clamp a timeout. Returns ``(ms, ok)``.

    Parseable ints are clamped to 200..5000 (``ok`` False when the raw
    value was out of range); unparseable values return the 1000 ms
    default with ``ok`` False.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return ENDPOINTING_TIMEOUT_DEFAULT_MS, False
    clamped = max(ENDPOINTING_TIMEOUT_MIN_MS,
                  min(ENDPOINTING_TIMEOUT_MAX_MS, parsed))
    return clamped, clamped == parsed


def validate_stt_knobs(raw):
    """Validate raw knob input -> ``(clean, rejected)``.

    ``clean`` always carries every knob with a safe value (validated
    input, clamped/cleaned input, or the default). ``rejected`` lists
    the field names whose raw value was present but invalid or had to
    be adjusted (allowlist miss, unparseable/clamped timeout, dropped
    keyword items). Fields absent from ``raw`` take defaults and are
    not rejected. Never raises on bad input.
    """
    raw = raw or {}
    clean = dict(DEFAULT_KNOBS)
    rejected = []

    if "stt_provider" in raw and raw["stt_provider"] is not None:
        provider = str(raw["stt_provider"]).strip().lower()
        if provider in VALID_STT_PROVIDERS:
            clean["stt_provider"] = provider
        elif provider:
            rejected.append("stt_provider")

    for field in ("stt_streaming_enabled", "stt_diarization_enabled",
                  "stt_code_switching_enabled"):
        if field in raw:
            clean[field] = parse_bool(raw[field])

    if "stt_endpointing_mode" in raw and raw["stt_endpointing_mode"] is not None:
        mode = str(raw["stt_endpointing_mode"]).strip().lower()
        if mode in VALID_ENDPOINTING_MODES:
            clean["stt_endpointing_mode"] = mode
        elif mode:
            rejected.append("stt_endpointing_mode")

    if "stt_endpointing_timeout_ms" in raw and raw["stt_endpointing_timeout_ms"] is not None:
        raw_timeout = raw["stt_endpointing_timeout_ms"]
        if isinstance(raw_timeout, str) and not raw_timeout.strip():
            pass  # blank = default, not a rejection
        else:
            timeout_ms, ok = clamp_endpointing_timeout_ms(raw_timeout)
            clean["stt_endpointing_timeout_ms"] = timeout_ms
            if not ok:
                rejected.append("stt_endpointing_timeout_ms")

    if "stt_language_hint" in raw and raw["stt_language_hint"] is not None:
        hint = str(raw["stt_language_hint"]).strip().lower()
        if not hint:
            clean["stt_language_hint"] = LANGUAGE_AUTO
        elif hint in VALID_LANGUAGE_HINTS:
            clean["stt_language_hint"] = hint
        else:
            clean["stt_language_hint"] = LANGUAGE_AUTO
            rejected.append("stt_language_hint")

    if "stt_keyword_bias" in raw and raw["stt_keyword_bias"] is not None:
        items, changed = clean_keyword_items(raw["stt_keyword_bias"])
        clean["stt_keyword_bias"] = join_keyword_items(items)
        if changed:
            rejected.append("stt_keyword_bias")

    return clean, rejected


def resolve_stt_mode(provider, streaming_enabled, has_key):
    """Resolve the effective STT mode -> ``(mode, fell_back, source)``.

    See the module docstring table. ``fell_back`` is True only when a
    configured Muse mode cannot be used because no effective key is
    present (an unknown provider also falls back to Whisper rather
    than raising, so a corrupt config row can never drop a voice
    turn). Streaming-disabled ``muse_streaming`` resolving to the
    one-shot path is by design, not a key fallback.
    """
    candidate = str(provider or "").strip().lower()
    if candidate == STT_PROVIDER_OPENAI:
        return MODE_WHISPER, False, "openai_default"
    if candidate == STT_PROVIDER_MUSE_TRANSCRIBE:
        if has_key:
            return MODE_MUSE_ONESHOT, False, "muse_transcribe"
        return MODE_WHISPER, True, "whisper_fallback_no_key"
    if candidate == STT_PROVIDER_MUSE_STREAMING:
        if streaming_enabled:
            if has_key:
                return MODE_MUSE_STREAMING, False, "muse_streaming"
            return MODE_WHISPER, True, "whisper_fallback_no_key"
        if has_key:
            return MODE_MUSE_ONESHOT, False, "muse_oneshot_streaming_disabled"
        return MODE_WHISPER, True, "whisper_fallback_no_key"
    return MODE_WHISPER, True, "whisper_fallback_unknown"
