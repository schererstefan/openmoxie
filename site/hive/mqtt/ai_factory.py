"""AI client factory: OpenAI (default) + optional Muse Spark chat provider.

GPT stays the default everywhere. Muse Spark is chat-only and opt-in via
``HiveConfiguration.chat_provider`` / ``SinglePromptChat.vendor``. Speech-to-text
never uses this module's chat dispatch: ``zmq_stt_handler`` keeps calling
``create_openai()`` explicitly so Whisper stays on OpenAI even when chat is Spark.

Keys are held in module globals (same pattern as the original OpenAI-only
factory) and are never logged.
"""
from openai import OpenAI
import logging

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


def resolve_chat_provider(vendor=None, global_provider=None):
    """Resolve the effective chat provider (Stage 1 B.1 rule, exact).

    The conversation ``vendor`` wins if it names a provider whose key is
    configured; else the global ``chat_provider`` if its key is configured;
    else ``openai``. Key *presence* drives this selection only -- once a
    provider is resolved, a missing key or a request failure must fail loudly
    in the caller, never silently fall back to the other provider.
    """
    if vendor is not None:
        candidate = normalize_provider(vendor)
        if has_provider_key(candidate):
            return candidate
    candidate = normalize_provider(global_provider) if global_provider is not None else _GLOBAL_CHAT_PROVIDER
    if has_provider_key(candidate):
        return candidate
    return PROVIDER_OPENAI


def create_chat_client(provider):
    """Chat-only client for ``provider``. STT must not use this helper."""
    if normalize_provider(provider) == PROVIDER_MUSE_SPARK:
        return create_muse_spark()
    return create_openai()
