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
