# CHECKPOINT — Spark Global Switch

Branch: `mycroft-spark-global-switch`
Base commit: `631bb01` (Add read-only MQTT/server log monitor page (clone only))
Worktree: `~/dev/openmoxie-spark-switch` (this file lives in the worktree root)
Date context: fix for `SinglePromptDBChatSession` always passing `source.vendor` to `resolve_chat_provider()`.

## Old behaviour

- [site/hive/models.py](site/hive/models.py): `SinglePromptChat.vendor` defaults to `AIVendor.OPEN_AI` (`1`, GPT/OpenAI). Every stock / merely-created conversation row therefore carries `vendor=1` even when no admin ever chose a vendor for that conversation.
- [site/hive/mqtt/conversations.py](site/hive/mqtt/conversations.py) `SinglePromptDBChatSession.__init__` unconditionally called `resolve_chat_provider(vendor=source.vendor)`.
- [site/hive/mqtt/ai_factory.py](site/hive/mqtt/ai_factory.py) `resolve_chat_provider(vendor=None, global_provider=None)` treated any non-`None` `vendor` as the winning candidate (when its key was configured). Because the defaulted `vendor=1` is always non-`None` and the OpenAI key is normally configured, a stock conversation always resolved to `openai`.
- Consequence: flipping the global `HiveConfiguration.chat_provider` to Muse Spark (`muse_spark`) did nothing for stock conversations. The global switch was effectively dead for the common case; only a conversation whose stored `vendor` happened to be `2` would ever reach Spark, and there was no way to distinguish "admin explicitly chose OpenAI" from "field merely defaulted to OpenAI".

## New precedence rule

Documented in the docstring of `resolve_chat_provider_detailed()` in [site/hive/mqtt/ai_factory.py](site/hive/mqtt/ai_factory.py):

1. **EXPLICIT per-conversation override** — used when `vendor_explicit is True`, or when `vendor_explicit is None` and `vendor is not None`. The latter keeps `resolve_chat_provider(vendor=...)` backward compatible: a `vendor` argument passed to the legacy entry point counts as explicit and preserves existing tests/callers.
2. **GLOBAL default** — the `global_provider` argument when supplied, otherwise the stored `_GLOBAL_CHAT_PROVIDER` (loaded from `HiveConfiguration.chat_provider`).
3. **GPT fallback** — `openai`.

`resolve_chat_provider_detailed(vendor=None, global_provider=None, vendor_explicit=None)` returns `(provider, fell_back: bool, source: str)` where `source` is one of:

| source | meaning |
| --- | --- |
| `explicit` | Explicit per-conversation vendor chosen and its key is configured. `fell_back=False`. |
| `global` | Global provider (non-default, i.e. Muse Spark) chosen and its key is configured. `fell_back=False`. |
| `gpt_default` | Global is unset/default `openai`. Returns `openai`, `fell_back=False`. |
| `gpt_fallback` | Chosen explicit or global provider has no key configured. Returns `openai`, `fell_back=True`. Never raises for a missing key, never drops the conversation. |

`resolve_chat_provider(vendor=None, global_provider=None, vendor_explicit=None)` delegates to the detailed resolver and returns only the provider string.

A merely-defaulted conversation now has `vendor_explicit=False`, so `SinglePromptDBChatSession` passes `vendor=None, vendor_explicit=False` and the global provider governs it. An admin-explicit conversation (`vendor_explicit=True`) still wins over the global in either direction (explicit OpenAI beats global Spark, and explicit Spark beats global GPT).

GPT (`openai`) remains the default provider.

## Files changed

- [site/hive/mqtt/ai_factory.py](site/hive/mqtt/ai_factory.py) — added `resolve_chat_provider_detailed(vendor=None, global_provider=None, vendor_explicit=None)` with the precedence above; `resolve_chat_provider()` gained the `vendor_explicit=None` parameter and now delegates to the detailed resolver, returning only the provider string. No change to the default provider.
- [site/hive/models.py](site/hive/models.py) — added `SinglePromptChat.vendor_explicit = BooleanField(default=False)` with the comment: "True only when an admin explicitly chose a vendor for that conversation; defaulted rows are False so the global provider governs them." Existing rows default to `False` (global governs them — the intended new behaviour).
- [site/hive/migrations/0019_singlepromptchat_vendor_explicit.py](site/hive/migrations/0019_singlepromptchat_vendor_explicit.py) — new migration depending on `0018_muse_spark_chat_provider`, adding `vendor_explicit`. Follows the style of `0018`. Verified with `makemigrations --check --dry-run`: no missing migrations.
- [site/hive/mqtt/conversations.py](site/hive/mqtt/conversations.py) — `SinglePromptDBChatSession` now reads `getattr(source, "vendor_explicit", False)` (robust for older/mock objects without the attribute) and calls `resolve_chat_provider(vendor=source.vendor if explicit else None, vendor_explicit=explicit)`. No other behaviour changed there.
- [site/hive/test_muse_spark.py](site/hive/test_muse_spark.py) — extended with `MuseSparkGlobalSwitchTests` (10 tests, see below). One existing mock updated: `test_db_session_passes_vendor_through` now sets `vendor_explicit=True` on its `SimpleNamespace` source so its original intent (an explicitly-chosen Spark vendor passes through to the session) is preserved under the new field; no existing assertion was weakened or removed.
- `CHECKPOINT-SPARK-GLOBAL-SWITCH.md` (this file, worktree root).

## Tests

Run from `site/` with `~/dev/openmoxie-contrib/.venv-stageb/bin/python` (Python 3.9.6, Django 4.2.30; the worktree has no venv of its own, and no venv was created inside `~/dev/openmoxie-contrib`). The only key value used in tests is the fake `"test-key-not-real"`, as in existing tests. No network, broker, robot, database or real key is used; all clients are mocked.

New class `MuseSparkGlobalSwitchTests` covers, via both `resolve_chat_provider_detailed()` and `resolve_chat_provider()`, with keys set explicitly in each test (`set_openai_key` / `set_muse_spark_key` / `set_global_chat_provider`) and restored via `addCleanup` exactly as the existing class does:

- global Spark + no explicit override -> `muse_spark` (`source="global"`, including the merely-defaulted `vendor=1, vendor_explicit=False` case)
- global Spark + explicit OpenAI override -> `openai` (`source="explicit"`, plus the backward-compatible `vendor` without `vendor_explicit` path)
- global GPT + explicit Spark override -> `muse_spark` (`source="explicit"`)
- global Spark + no Spark key -> `openai` with `fell_back=True`, `source="gpt_fallback"`
- global unset/default -> `openai` with `fell_back=False`, `source="gpt_default"`
- explicit vendor with no key -> GPT fallback `openai` with `fell_back=True`, `source="gpt_fallback"`

Plus DB-session integration tests (defaulted vendor follows global Spark; explicit OpenAI beats global Spark; missing `vendor_explicit` attribute follows global) and a model-default/migration test for `vendor_explicit`.

Exact results:

```text
python -m unittest hive.test_muse_spark -v
Ran 32 tests in 0.475s — OK   (22 pre-existing + 10 new)

python -m unittest hive.test_mycroft_bridge hive.test_overnight hive.test_control_commands hive.test_bridge_wiring hive.test_logs_view
Ran 135 tests — OK

Per-module:
  hive.test_muse_spark        Ran 32 tests — OK
  hive.test_mycroft_bridge    Ran 26 tests — OK
  hive.test_overnight         Ran 23 tests — OK
  hive.test_control_commands  Ran 25 tests — OK
  hive.test_bridge_wiring     Ran 45 tests — OK
  hive.test_logs_view         Ran 16 tests — OK
```

All modules above exist in this worktree and were run. Total across the six modules: 167 tests, all passing.

## Deliberately NOT changed

- Default provider: GPT/`openai` stays the default everywhere. Spark remains opt-in via the global `chat_provider` or an explicit per-conversation override.
- STT: [site/hive/mqtt/zmq_stt_handler.py](site/hive/mqtt/zmq_stt_handler.py) untouched; it still calls `create_openai()` explicitly so OpenAI Whisper stays on OpenAI even when chat is Spark. Covered by the pre-existing `test_stt_client_stays_openai_when_chat_is_spark`.
- No HTTP endpoint added. No setup-page/view changes.
- Puppet API untouched. No bridge/control-command behaviour changed.
- `META_API_KEY` was never set. No keys or passwords were written to any file or log; tests use only the fake `"test-key-not-real"`.
- Nothing in `~/OpenMoxie`, `~/OpenMoxie-test` (containers/DBs/certs) or `~/dev/openmoxie-contrib` was touched; no branch was checked out in `~/dev/openmoxie-contrib`. Its venv was only executed read-only to run the tests.
- Existing rows are not migrated to `vendor_explicit=True`; they default to `False`, which is the intended new behaviour (global governs them).
- No admin UI was added to set `vendor_explicit`; there is currently no shipped way for an admin to mark a conversation explicit from the UI. That is a follow-up decision, not part of this fix.

## Still needs Stefan decisions before any live use

1. **Live Spark key entry by Stefan himself in the setup page.** No real Muse Spark (or OpenAI) key has been entered or tested by this change. Stefan must enter the live Spark key himself on the setup page of the target deployment; do not copy keys through files, logs or chat.
2. **Live deploy via the full-branch promotion path, never file-by-file copy.** This branch must reach a live system only through the established full-branch promotion path. Individual files from this worktree must never be copied file-by-file onto a live box, because the model field, its migration `0019`, the resolver and the conversation wiring only work as a set (a live DB without migration `0019` applied would not have the `vendor_explicit` column).
3. **Before flipping a live global to Spark:** confirm the Spark key is present on that deployment (otherwise every Spark-governed conversation silently falls back to GPT with `fell_back=True` / `gpt_fallback`), confirm the intended `muse_spark_model` (standard vs contributor — different data-use terms), and decide whether any existing conversations should be marked `vendor_explicit=True` (e.g. pinned to GPT) before the flip, since after this change all defaulted rows follow the global.
4. **Migration on the live DB:** `0019_singlepromptchat_vendor_explicit` must be applied (`migrate`) as part of that promotion; existing conversations will read as `vendor_explicit=False` and will follow the global from that point.

## Commit / push

Committed on `mycroft-spark-global-switch` and pushed to remote `fork` only (`git push fork mycroft-spark-global-switch`); never upstream, never `main`. Verified with `git ls-remote fork mycroft-spark-global-switch` (see the session report for the resulting hash).
