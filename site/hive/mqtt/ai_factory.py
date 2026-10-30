"""AI client factory: OpenAI (default) + optional Muse Spark chat provider.

GPT stays the default everywhere. Muse Spark is chat-only and opt-in via
``HiveConfiguration.chat_provider`` / ``SinglePromptChat.vendor``. Speech-to-text
is independent of the chat provider: it defaults to OpenAI Whisper-1 and
switches to Muse Voice Transcribe only when ``HiveConfiguration.stt_provider``
is ``muse_transcribe`` (see ``muse_transcribe.py``). Whisper remains the
fallback whenever Transcribe is unavailable or fails for an utterance.

Keys are held in module globals (same pattern as the original OpenAI-only
factory) and are never logged.
"""
from openai import OpenAI
import logging

from .muse_transcribe_streaming import StreamingConfig
from .stt_knobs import (
    LANGUAGE_AUTO,
    VALID_ENDPOINTING_MODES,
    VALID_LANGUAGE_HINTS,
    clamp_endpointing_timeout_ms,
    clean_keyword_items,
    join_keyword_items,
    keyword_list_from_stored,
    parse_bool,
)

logger = logging.getLogger(__name__)

PROVIDER_OPENAI = "openai"
PROVIDER_MUSE_SPARK = "muse_spark"
CHAT_PROVIDERS = (PROVIDER_OPENAI, PROVIDER_MUSE_SPARK)

MUSE_SPARK_BASE_URL = "https://api.meta.ai/v1"
# ~20s: a spoken turn leaves the robot silent while it waits, so a hung chat
# provider must fail fast instead of hanging a worker thread indefinitely.
CHAT_TIMEOUT_SECONDS = 20.0

_OPENAPI_KEY = None
_MUSE_SPARK_KEY = None
_GLOBAL_CHAT_PROVIDER = PROVIDER_OPENAI

# STT provider selection (independent of the chat provider above).
# Default is OpenAI Whisper-1; Muse Voice Transcribe is opt-in only.
# ``muse_streaming`` additionally needs the separate streaming switch
# below; with it off it resolves to the one-shot path (see stt_knobs).
STT_PROVIDER_OPENAI = "openai"
STT_PROVIDER_MUSE_TRANSCRIBE = "muse_transcribe"
STT_PROVIDER_MUSE_STREAMING = "muse_streaming"
STT_PROVIDERS = (STT_PROVIDER_OPENAI, STT_PROVIDER_MUSE_TRANSCRIBE, STT_PROVIDER_MUSE_STREAMING)
_STT_PROVIDER = STT_PROVIDER_OPENAI
# Streaming knobs (loaded from HiveConfiguration; defaults keep every
# existing install on the proven paths). Setters below never raise at
# server load: unknown values are logged and ignored (keeping the
# current value), matching set_stt_provider.
_STT_STREAMING_ENABLED = False
_STT_DIARIZATION_ENABLED = False
_STT_ENDPOINTING_MODE = "auto"
_STT_ENDPOINTING_TIMEOUT_MS = 1000
_STT_LANGUAGE_HINT = LANGUAGE_AUTO
_STT_KEYWORD_BIAS = ""
_STT_CODE_SWITCHING_ENABLED = False
# Optional per-STT override key. When unset/blank, Transcribe reuses the
# Muse Spark key: both are Meta Model API keys on the same base URL
# (https://api.meta.ai/v1) with Bearer auth, so a Spark key is expected
# to authorise Transcribe too. Live scope confirmation is still pending
# (see CHECKPOINT-STT-SWAP.md); if a live Spark key is ever rejected by
# the Transcribe endpoint, entering this override is the fix, no code
# change needed. This value, like every key here, is never logged.
_MUSE_TRANSCRIBE_KEY_OVERRIDE = None


def set_muse_transcribe_key(key):
    global _MUSE_TRANSCRIBE_KEY_OVERRIDE
    _MUSE_TRANSCRIBE_KEY_OVERRIDE = key


def get_muse_transcribe_key():
    """Effective Transcribe key: explicit override, else the Spark key."""
    if _key_present(_MUSE_TRANSCRIBE_KEY_OVERRIDE):
        return _MUSE_TRANSCRIBE_KEY_OVERRIDE
    return _MUSE_SPARK_KEY


def has_muse_transcribe_key():
    return _key_present(get_muse_transcribe_key())


def normalize_stt_provider(provider):
    """Canonical STT provider string for an STT value."""
    value = getattr(provider, "value", provider)
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if text in (STT_PROVIDER_OPENAI, "whisper", "whisper_1"):
        return STT_PROVIDER_OPENAI
    if text in (STT_PROVIDER_MUSE_STREAMING, "muse_voice_transcribe_streaming", "muse_transcribe_streaming"):
        return STT_PROVIDER_MUSE_STREAMING
    if text in (STT_PROVIDER_MUSE_TRANSCRIBE, "muse_voice_transcribe"):
        return STT_PROVIDER_MUSE_TRANSCRIBE
    raise ValueError(f"Unknown STT provider: {provider!r}")


def set_stt_provider(provider):
    """Store the STT provider (loaded from HiveConfiguration.stt_provider).

    An unknown value is logged and ignored (keeping the current/default
    provider) so a corrupt config row cannot prevent server startup.
    """
    global _STT_PROVIDER
    try:
        _STT_PROVIDER = normalize_stt_provider(provider)
    except ValueError:
        logger.error("Ignoring unknown STT provider %r; keeping %s", provider, _STT_PROVIDER)


def get_stt_provider():
    return _STT_PROVIDER


def resolve_stt_provider_detailed(provider=None):
    """Resolve the effective STT provider with provenance.

    Returns ``(provider, fell_back, source)`` where ``source`` is one of
    ``"openai_default"`` (Whisper is the configured provider),
    ``"muse_transcribe"`` (Transcribe configured and its key present) or
    ``"whisper_fallback_no_key"`` (Transcribe configured but no key, so
    this utterance must use Whisper instead). A missing Transcribe key
    never raises and never drops the utterance; the runtime fallback in
    ``zmq_stt_handler`` covers Transcribe *errors* on top of this.
    """
    if provider is not None:
        candidate = normalize_stt_provider(provider)
    else:
        candidate = _STT_PROVIDER
    if candidate == STT_PROVIDER_OPENAI:
        return STT_PROVIDER_OPENAI, False, "openai_default"
    if candidate == STT_PROVIDER_MUSE_STREAMING:
        if has_muse_transcribe_key():
            return STT_PROVIDER_MUSE_STREAMING, False, "muse_streaming"
        return STT_PROVIDER_OPENAI, True, "whisper_fallback_no_key"
    if has_muse_transcribe_key():
        return STT_PROVIDER_MUSE_TRANSCRIBE, False, "muse_transcribe"
    return STT_PROVIDER_OPENAI, True, "whisper_fallback_no_key"


def resolve_stt_provider(provider=None):
    """Backward-compatible wrapper returning only the provider string."""
    resolved, _fell_back, _source = resolve_stt_provider_detailed(provider=provider)
    return resolved


def set_stt_streaming_enabled(enabled):
    global _STT_STREAMING_ENABLED
    _STT_STREAMING_ENABLED = parse_bool(enabled)


def get_stt_streaming_enabled():
    return _STT_STREAMING_ENABLED


def set_stt_diarization_enabled(enabled):
    global _STT_DIARIZATION_ENABLED
    _STT_DIARIZATION_ENABLED = parse_bool(enabled)


def get_stt_diarization_enabled():
    return _STT_DIARIZATION_ENABLED


def set_stt_endpointing_mode(mode):
    """Store the endpointing mode; unknown values keep the current one."""
    global _STT_ENDPOINTING_MODE
    candidate = str(mode).strip().lower() if mode is not None else ""
    if candidate in VALID_ENDPOINTING_MODES:
        _STT_ENDPOINTING_MODE = candidate
    else:
        logger.warning(
            "Ignoring unknown STT endpointing mode %r; keeping %s",
            mode, _STT_ENDPOINTING_MODE)


def get_stt_endpointing_mode():
    return _STT_ENDPOINTING_MODE


def set_stt_endpointing_timeout_ms(timeout_ms):
    """Store the endpointing timeout (clamped); garbage keeps current."""
    global _STT_ENDPOINTING_TIMEOUT_MS
    if timeout_ms is None or (isinstance(timeout_ms, str) and not timeout_ms.strip()):
        return
    clamped, ok = clamp_endpointing_timeout_ms(timeout_ms)
    if not ok:
        try:
            int(timeout_ms)
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring unparseable STT endpointing timeout %r; keeping %s",
                timeout_ms, _STT_ENDPOINTING_TIMEOUT_MS)
            return
    _STT_ENDPOINTING_TIMEOUT_MS = clamped


def get_stt_endpointing_timeout_ms():
    return _STT_ENDPOINTING_TIMEOUT_MS


def set_stt_language_hint(hint):
    """Store the language hint; unknown values keep the current one."""
    global _STT_LANGUAGE_HINT
    if hint is None:
        _STT_LANGUAGE_HINT = LANGUAGE_AUTO
        return
    candidate = str(hint).strip().lower()
    if not candidate:
        _STT_LANGUAGE_HINT = LANGUAGE_AUTO
    elif candidate in VALID_LANGUAGE_HINTS:
        _STT_LANGUAGE_HINT = candidate
    else:
        logger.warning(
            "Ignoring unknown STT language hint %r; keeping %s",
            hint, _STT_LANGUAGE_HINT)


def get_stt_language_hint():
    return _STT_LANGUAGE_HINT


def set_stt_keyword_bias(value):
    """Store cleaned/capped keyword bias text; never raises."""
    global _STT_KEYWORD_BIAS
    items, _changed = clean_keyword_items(value)
    _STT_KEYWORD_BIAS = join_keyword_items(items)


def get_stt_keyword_bias():
    return _STT_KEYWORD_BIAS


def get_stt_keyword_list():
    """Keyword bias items as a list (re-validated from the stored text)."""
    return list(keyword_list_from_stored(_STT_KEYWORD_BIAS))


def set_stt_code_switching_enabled(enabled):
    global _STT_CODE_SWITCHING_ENABLED
    _STT_CODE_SWITCHING_ENABLED = parse_bool(enabled)


def get_stt_code_switching_enabled():
    return _STT_CODE_SWITCHING_ENABLED


def get_stt_streaming_config():
    """StreamingConfig from the loaded knobs (``'auto'`` -> ``None``)."""
    return StreamingConfig(
        diarization=_STT_DIARIZATION_ENABLED,
        language_hint=None if _STT_LANGUAGE_HINT == LANGUAGE_AUTO else _STT_LANGUAGE_HINT,
        keyword_bias=keyword_list_from_stored(_STT_KEYWORD_BIAS),
        code_switching=_STT_CODE_SWITCHING_ENABLED,
        endpointing_mode=_STT_ENDPOINTING_MODE,
        endpointing_timeout_ms=_STT_ENDPOINTING_TIMEOUT_MS,
    )


def set_openai_key(key):
    global _OPENAPI_KEY
    _OPENAPI_KEY = key


def set_muse_spark_key(key):
    global _MUSE_SPARK_KEY
    _MUSE_SPARK_KEY = key


def create_openai():
    global _OPENAPI_KEY
    return OpenAI(api_key=_OPENAPI_KEY, timeout=CHAT_TIMEOUT_SECONDS)


def create_muse_spark():
    global _MUSE_SPARK_KEY
    return OpenAI(
        api_key=_MUSE_SPARK_KEY,
        base_url=MUSE_SPARK_BASE_URL,
        timeout=CHAT_TIMEOUT_SECONDS,
    )


def _key_present(key):
    return bool(key and str(key).strip())


def has_openai_key():
    return _key_present(_OPENAPI_KEY)


def has_muse_spark_key():
    return _key_present(_MUSE_SPARK_KEY)


def has_provider_key(provider):
    return has_muse_spark_key() if normalize_provider(provider) == PROVIDER_MUSE_SPARK else has_openai_key()


def normalize_provider(provider):
    """Canonical ``openai``/``muse_spark`` string for a provider-ish value.

    Accepts the canonical strings, ``AIVendor``-style members/values
    (``1`` = OpenAI, ``2`` = Muse Spark) and enum members exposing ``.value``.
    Anything else raises ``ValueError`` so an unknown vendor is rejected
    loudly instead of silently routing to a default.
    """
    value = getattr(provider, "value", provider)
    if isinstance(value, int):
        if value == 1:
            return PROVIDER_OPENAI
        if value == 2:
            return PROVIDER_MUSE_SPARK
        raise ValueError(f"Unknown chat provider value: {provider!r}")
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if text in (PROVIDER_OPENAI, "open_ai"):
        return PROVIDER_OPENAI
    if text in (PROVIDER_MUSE_SPARK, "muse_spark_1_3"):
        return PROVIDER_MUSE_SPARK
    raise ValueError(f"Unknown chat provider: {provider!r}")


def set_global_chat_provider(provider):
    """Store the global default provider (loaded from HiveConfiguration).

    An unknown value is logged and ignored (keeping the current/default
    provider) so a corrupt config row cannot prevent server startup; the
    setup UI and model choices constrain this field to valid values.
    """
    global _GLOBAL_CHAT_PROVIDER
    try:
        _GLOBAL_CHAT_PROVIDER = normalize_provider(provider)
    except ValueError:
        logger.error("Ignoring unknown global chat provider %r; keeping %s", provider, _GLOBAL_CHAT_PROVIDER)


def get_global_chat_provider():
    return _GLOBAL_CHAT_PROVIDER


def resolve_chat_provider_detailed(vendor=None, global_provider=None, vendor_explicit=None):
    """Resolve the effective chat provider with provenance.

    Returns ``(provider, fell_back, source)`` where ``provider`` is the
    canonical provider string, ``fell_back`` is ``True`` only when a chosen
    provider could not be used because its key is not configured, and
    ``source`` is one of ``"explicit"``, ``"global"``, ``"gpt_default"``
    or ``"gpt_fallback"``.

    Precedence:

    1. EXPLICIT per-conversation override -- used when ``vendor_explicit``
       is ``True``, or when ``vendor_explicit`` is ``None`` and ``vendor``
       is not ``None`` (a ``vendor`` argument passed to the legacy
       :func:`resolve_chat_provider` therefore counts as explicit, which
       preserves existing callers/tests). The vendor value is ignored
       entirely when ``vendor_explicit`` is ``False``, even if a vendor
       value is supplied (e.g. a merely-defaulted ``SinglePromptChat``
       row whose ``vendor`` field holds the model default).
    2. GLOBAL default -- the ``global_provider`` argument when supplied,
       otherwise the stored ``_GLOBAL_CHAT_PROVIDER`` (loaded from
       ``HiveConfiguration.chat_provider``).
    3. GPT fallback -- ``"openai"``.

    If the chosen (explicit or global) provider has no key configured,
    fall back to ``"openai"`` with ``fell_back=True`` and
    ``source="gpt_fallback"``; this never raises for a missing key and
    never drops the conversation. If the global provider is unset or is
    the default ``"openai"``, return ``"openai"`` with
    ``fell_back=False`` and ``source="gpt_default"``. GPT (``openai``)
    remains the default provider.
    """
    if vendor_explicit is None:
        is_explicit = vendor is not None
    else:
        is_explicit = bool(vendor_explicit)

    if is_explicit and vendor is not None:
        candidate = normalize_provider(vendor)
        if has_provider_key(candidate):
            return candidate, False, "explicit"
        return PROVIDER_OPENAI, True, "gpt_fallback"

    if global_provider is not None:
        global_candidate = normalize_provider(global_provider)
    else:
        global_candidate = _GLOBAL_CHAT_PROVIDER

    if global_candidate == PROVIDER_OPENAI:
        return PROVIDER_OPENAI, False, "gpt_default"
    if has_provider_key(global_candidate):
        return global_candidate, False, "global"
    return PROVIDER_OPENAI, True, "gpt_fallback"


def resolve_chat_provider(vendor=None, global_provider=None, vendor_explicit=None):
    """Resolve the effective chat provider.

    Backward-compatible wrapper around
    :func:`resolve_chat_provider_detailed`: a ``vendor`` argument passed
    here counts as explicit when ``vendor_explicit`` is ``None``
    (preserving existing tests/callers). Pass ``vendor_explicit=False``
    to ignore a merely-defaulted ``vendor`` value so the global provider
    governs, or ``vendor_explicit=True`` for an explicit per-conversation
    override. Returns only the provider string.
    """
    provider, _fell_back, _source = resolve_chat_provider_detailed(
        vendor=vendor, global_provider=global_provider, vendor_explicit=vendor_explicit
    )
    return provider


def create_chat_client(provider):
    """Chat-only client for ``provider``. STT must not use this helper."""
    if normalize_provider(provider) == PROVIDER_MUSE_SPARK:
        return create_muse_spark()
    return create_openai()
