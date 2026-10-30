# CHECKPOINT — Muse Transcribe STT streaming (opt-in, clone + tests only)

Branch: `mycroft-stt-streaming` (from `mycroft-stt-swap` commit `2eaf32e`)
Worktree: `~/dev/openmoxie-stt-streaming`
Status: **NOT live.** No live container, DB, cert, config or robot was
touched. Live STT is unchanged (Whisper-1 default; one-shot Muse
Transcribe still opt-in from the previous branch). Nothing here flips
live, and streaming is **off by default** behind two switches.

Prior-branch facts still in force (see `CHECKPOINT-STT-SWAP.md`):
one-shot `POST https://api.meta.ai/v1/asr/transcribe`, model
`muse-voice-transcribe-1.0` at $0.18/hr, Whisper-1 deprecated with
removal Feb 26, 2027. No published WER benchmark vs Whisper exists;
**no accuracy superiority is claimed anywhere in this branch.**

## Streaming flow (in words)

1. Robot mic + VAD produce `zmqSTTRequest` frames (mono 16 kHz int16
   PCM). `STTHandler.handle_zmq` buffers every frame in the
   `STTSession` exactly as before — buffering is unconditional, so the
   proven whole-utterance paths always have the full audio.
2. **Per-frame live path (only when effective):** if the effective mode
   is `muse_streaming` (provider `muse_streaming` AND streaming switch
   on AND Muse key present) *and* a transport factory is configured
   on the handler (`STTHandler.streaming_websocket_factory`), each
   incoming frame is also fed to a per-session `StreamingSession`,
   which re-chunks the PCM into exact **80 ms / 2560-byte** chunks
   (16,000 × 0.080 × 2; no resampling, no silence padding — a trailing
   partial chunk stays buffered and is never sent).
3. Provider messages are drained by `pump()`: `partial`/`interim`
   events update in-memory state only — they are **never forwarded to
   the robot and never logged with content**. A `final` event, or an
   `endpoint`/`turn_complete` event promoting the last partial, is the
   early final.
4. **Early final:** the handler sends the `FINAL` `zmqSTTResponse`
   immediately via `zmq_reply`, removes the session, and does not run
   `perform()` at END_OF_SPEECH for that utterance.
5. **No early final:** END_OF_SPEECH pops the session and queues the
   existing `perform()` on the worker pool (unchanged). `perform()`
   builds the WAV as before, then `_transcribe()` tries, in order:
   streaming over the buffered PCM → one-shot Transcribe → Whisper-1.
6. **Endpointing (server-side):** `auto` = provider endpoint events
   win, server timeout is a backstop; `server_timeout` = server
   promotes the last partial after `endpointing_timeout_ms` with no
   new audio/message; `disabled` = only a provider final or
   `finish()` at end-of-speech ends the turn. Promotions record
   `endpoint_by` (`provider` / `server_timeout` / `finish`).
7. If streaming is not effective, or no factory is configured for the
   per-frame path, handler behaviour is identical to before this
   branch. `perform()`'s buffered streaming attempt with the default
   factory uses the lazy real-transport adapter, which raises
   `StreamingUnavailable` in this offline branch (no websocket library
   installed; handshake unverified) — documented, logged by kind, and
   absorbed by the fallback chain. It is not hidden.

## Knobs: defaults, validation, API mapping

Saved **only** via the existing `hive_configure` POST, validated in
exactly one place (`site/hive/mqtt/stt_knobs.py:validate_stt_knobs`);
raw POST values never reach storage or any provider API. No new URL
endpoint, no CSRF exemption added.

| Knob (model field / POST name) | Default | Validation | Where it goes |
|---|---|---|---|
| `stt_provider` | `openai` | allowlist `openai` / `muse_transcribe` / `muse_streaming`; invalid keeps current | Mode resolution (server-side only) |
| `stt_streaming_enabled` | `False` | checkbox (`on`/`true`/`1`/True) | Mode resolution (server-side only; separate switch) |
| `stt_diarization_enabled` | `False` | checkbox | Realtime config message `diarization` |
| `stt_endpointing_mode` | `auto` | allowlist `auto` / `server_timeout` / `disabled` | Config `endpointing.mode` **and** server-side promotion logic |
| `stt_endpointing_timeout_ms` | `1000` | int clamped 200–5000; garbage → 1000 | Config `endpointing.timeout_ms` **and** server-side backstop |
| `stt_language_hint` | `auto` | `auto` + 25-code server-side allowlist (below); blank → `auto` | Config `language` (`auto` sent as `None`) |
| `stt_keyword_bias` | `''` | comma-separated, ≤20 items, ≤40 chars each, letters/digits/space/hyphen/apostrophe only, stored string ≤500 chars (whole items dropped, never cut mid-word) | Config `keyword_bias` list (biasing hints only) |
| `stt_code_switching_enabled` | `False` | checkbox | Config `code_switching` |

Language allowlist (server-side only — **not** a claim about Meta's
official validated list, which is unverified here): `auto` plus
`en es fr de it pt nl pl cs sk hu ro bg uk ru ar hi ja ko zh tr sv da
fi no` (25). Anything else is rejected to `auto`/kept-current.

Mode resolution (`resolve_stt_mode`): `openai` → whisper;
`muse_transcribe` → one-shot (whisper fallback without key);
`muse_streaming` + switch on + key → streaming; + switch on + no key
→ whisper fallback; + switch off → one-shot (whisper fallback without
key). So with defaults, every existing install stays on Whisper.

**Unverified API behaviours (no live realtime call was made):** the
exact `wss://api.meta.ai/v1/asr/realtime` handshake (auth placement),
config-message field names (`type: session.configure`, `audio.*`,
`language`, `keyword_bias`, `code_switching`, `endpointing.*`), and
provider message shapes (`type` + `text`/`transcript`) are plausible
guesses chosen to be fixable in one file (`muse_transcribe_streaming.py`).
The transport is injectable (`websocket_factory(api_key)` →
`send_bytes` / `recv_message` / `close`) precisely so tests need no
network and the live-shape fix touches only the adapter/config
builder. `websocket-client` is **not** in `requirements.txt`; the
real adapter imports it lazily and raises `StreamingUnavailable`
(kind only) when absent.

## Fallback chain (never silent)

`streaming → one-shot Transcribe → Whisper-1 → error_code 66 reply`

- Streaming failure of any kind (no transport, disconnect, provider
  error, empty final, any exception) → warning log (kind/status/
  provider only) → one-shot `transcribe_wav`.
- One-shot failure/empty → existing Whisper-1 path.
- Whisper failure → pre-existing `error_code 66` proto reply.
- Provenance on the session: `stt_provider` / `stt_source` ∈
  `muse_streaming`, `muse_transcribe` (direct one-shot),
  `muse_transcribe_fallback` (one-shot after streaming failed),
  `whisper_fallback` (Whisper after Muse failure/no key),
  `openai_default`. The proto has no fell-back field; the robot reply
  shape is unchanged.
- The one-shot path and `muse_transcribe.py` public behaviour are
  untouched; Whisper-1 remains the ultimate fallback while it exists.

## Tests (all mocked/offline; fake key `test-key-not-real` only)

New `site/hive/test_muse_transcribe_streaming.py` — **56 tests**:
streaming lifecycle (config without key/transcript, exact 2560-byte
chunking + trailing-partial buffering, partial≠final, final,
endpoint/turn_complete promotion, disconnect/error kind-only,
`finish()` rules, event repr hides text, lazy-adapter unavailability);
endpointing with a fake clock (`server_timeout` promotes, `auto`
backstop promotes, `disabled` never, no early promotion); knob
validation (allowlists, timeout 100→200 / 99999→5000 / garbage→1000,
keyword 21→20, overlong/illegal dropped, 500-char whole-item cap);
all `resolve_stt_mode` branches; factory setters/getters +
`StreamingConfig` mapping; model defaults + migration-0021 shape;
handler fallbacks (streaming success skips one-shot/Whisper; failure
→ one-shot (`muse_transcribe_fallback`); both fail → Whisper
(`whisper_fallback`); no-transport → one-shot; switch-off →
one-shot without touching the factory; total failure → error 66;
fallback logs assert no fake key and no transcript text); live frames
(early final replies immediately, partial-only falls through to
buffered `perform()`, no factory / whisper mode = no live attempt);
views (setup template really rendered via Django templates asserting
every knob field + selected/checked states; `hive_configure`
validated save incl. clamp/clean/keep-current; not CSRF-exempt;
staff-gated `logs_view` unchanged: staff 200, anonymous 302).

Full relevant suite, run from `site/` with
`~/dev/openmoxie-contrib/.venv-stageb/bin/python`:

```text
python -m unittest hive.test_muse_transcribe_streaming hive.test_muse_transcribe \
  hive.test_stt_latency hive.test_stt hive.test_muse_spark hive.test_mycroft_bridge \
  hive.test_overnight hive.test_control_commands hive.test_bridge_wiring hive.test_logs_view
Ran 271 tests — OK (skipped=1)
```

Per module: `test_muse_transcribe_streaming` **56** ·
`test_muse_transcribe` 28 · `test_stt_latency` 14 (13 run + 1
pre-existing live-timing skip, no key) · `test_stt` 6 ·
`test_muse_spark` 32 · `test_mycroft_bridge` 26 · `test_overnight` 23 ·
`test_control_commands` 25 · `test_bridge_wiring` 45 ·
`test_logs_view` 16. `makemigrations --check --dry-run`:
**“No changes detected.”**

Grep checks: the only `logger.*` calls in the new modules log
kind/type/flags (`diarization=`, `endpointing=`, message `type=`) —
none can carry transcript text or a key; no `open(`/file-write of
audio or transcripts exists in the new modules; no new
`csrf_exempt`; `hive/urls.py` untouched.

### Mocked timing table — MOCKED, plumbing only

Single local probe (fake websocket emitting partial→final, 1 s of
synthetic silence PCM = 12 full 80 ms chunks sent, remainder held):

| Measure (MOCKED) | Value | Live |
|---|---|---|
| First partial (`first_partial_ms`) | **0.029 ms** | not measured |
| Final (`final_ms`, `endpoint_by=provider`) | **0.032 ms** | not measured |
| Whole mocked session wall time | **0.036 ms** | not measured |

These numbers prove that timing hooks record finite durations, not
real latency. Real figures need network + model time at the daylight
live test; **do not quote them as streaming latency.**

## Privacy stance

- No audio stored to disk by streaming code; streaming turns ignore
  `LOG_WAV` entirely (`perform()` skips the WAV write when streaming
  was attempted; the early-final path never writes).
- No transcripts logged by streaming code: partials live in memory
  for endpointing only; streaming-turn final logs carry provider
  only; `StreamingEvent.__repr__` omits text; errors carry kind only.
  (Disclosed exception, pre-existing stock behaviour untouched by
  this branch: the legacy non-streaming `STT-FINAL: {text}` debug
  line on the Whisper/one-shot path still logs the transcript in
  `perform()` when streaming was not attempted. Removing it is a
  separate behaviour change, recommended as a follow-up, not smuggled
  into this branch.)
- Diarization off by default; speaker labels are never logged or
  stored (the parser ignores any such fields).
- Keys: presence (bool) only outside the existing factory/header
  paths; the streaming module never logs, returns, or config-sends a
  key. Tests use `test-key-not-real` exclusively.

## Auth note (honest)

`SetupView`/`DashboardView`/`hive_configure` are LAN bootstrap pages
in stock code with **no staff/login gate** — this branch does not
claim otherwise and does not change their posture. Knobs save only
through the existing CSRF-protected `hive_configure` POST (not
CSRF-exempt; verified by test). The only staff-gated pages remain
`logs_view`/`logs_api` (`staff_member_required`; staff 200 /
anonymous 302 re-asserted in the new tests). No new endpoint of any
kind was added. The dashboard STT block is read-only knob values —
no keys, no transcripts.

## Live-vs-clone split (honest)

- **Proven (clone, mocked):** knob validation/migration/UI wiring,
  chunking, event parsing, endpointing rules, per-frame early-final
  plumbing, and the full fallback chain, all offline.
- **Not proven:** real transport (library absent by design), live
  handshake/config/message field names, real partial/final latency,
  endpointing feel with a real child speaker, and accuracy vs
  Whisper (no benchmark exists; none claimed). Per-frame live
  forwarding is wired in the handler but only exercised with a fake
  websocket; with no factory configured in production wiring, real
  utterances take buffered `perform()` → (streaming attempt fails
  unavailable) → one-shot → Whisper until a verified transport is
  deliberately configured.

## Files changed (branch `mycroft-stt-streaming`)

- `site/hive/mqtt/muse_transcribe_streaming.py` (new) — transport-
  injectable realtime session, config builder, endpointing, timings.
- `site/hive/mqtt/stt_knobs.py` (new) — pure-Python validation +
  mode resolution (single save-time validation path).
- `site/hive/models.py` + `site/hive/migrations/0021_hiveconfiguration_stt_streaming_knobs.py`
  (new; depends on 0020) — `muse_streaming` choice + 7 knob fields
  (migration also carries the required `stt_provider` choices
  AlterField so `--check` is clean).
- `site/hive/mqtt/ai_factory.py` — knob globals/setters/getters,
  `get_stt_streaming_config()`, `muse_streaming` provider support;
  openai/muse_transcribe resolution unchanged.
- `site/hive/mqtt/zmq_stt_handler.py` — streaming-first fallback
  chain, per-frame early final, `LOG_WAV`/transcript-log suppression
  for streaming turns; worker pool + Whisper/error-66 paths kept.
- `site/hive/mqtt/moxie_server.py` — `update_from_database()` loads
  knobs via `getattr` defaults (pre-0021 row cannot crash startup).
- `site/hive/views.py`, `templates/hive/setup.html`,
  `templates/hive/dashboard.html` — validated save, full knob UI +
  confirm dialog, read-only dashboard STT summary.
- `site/hive/test_muse_transcribe_streaming.py` (new, 56 tests).
- `CHECKPOINT-STT-STREAMING.md` (this file).

## What is needed before any live flip (all still open)

1. **Full-branch promotion only** (built image + migration
   `hive.0021` applied to the live DB as part of promotion; never
   file-copy), exactly as prior promotions.
2. **Stefan's live knob choices, made by him on the live setup
   page:** provider, streaming switch, diarization (default off),
   endpointing, language (default auto), keywords. Streaming stays
   off until he turns both switches on.
3. **Websocket transport decision:** add/pin a websocket library and
   configure a real factory deliberately (or accept buffered-only
   attempts failing unavailable into one-shot). Until then there is
   no real streaming to flip.
4. **Daylight live voice test with Stefan in view:** first a live
   handshake/config-shape pass (fix field names in the clone if the
   realtime endpoint rejects the guessed config message — never fix
   live), then measured real partial/final latency with these hooks,
   accuracy scoring on real phrases (no superiority claim without
   it), and a forced-fallback test (streaming broken → one-shot →
   Whisper each proven live).
5. **Whisper-1 sunset watch** (removal Feb 26, 2027) still applies
   to the fallback itself; see `CHECKPOINT-STT-SWAP.md`.
