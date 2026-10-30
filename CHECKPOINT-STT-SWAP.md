# CHECKPOINT — Moxie STT Swap: OpenAI Whisper → Muse Voice Transcribe (clone + test server only)

Branch: `mycroft-stt-swap` (from `mycroft-spark-global-switch` commit `0a01a2f`)
Worktree: `~/dev/openmoxie-stt-swap`
Status: **NOT live.** No live container, DB, cert, config or robot was touched.
Live still runs the spark-switch code on 8001/8883 (global chat `muse_spark`,
model `muse-spark-1.3-contributor`); live STT is still Whisper-1. Nothing
in this branch flips it.

## Old flow (unchanged default)

- Robot mic + VAD delimits an utterance; audio frames stream to the server.
- `site/hive/mqtt/zmq_stt_handler.py` `STTSession.perform()` accumulates
  int16 frames, builds a WAV, and calls OpenAI Whisper-1 via
  `client.audio.transcriptions.create(model='whisper-1',
  response_format='verbose_json', timestamp_granularities=['segment'])`.
- Segment start/end times offset the robot timestamp; the proto reply is
  sent via `zmq_reply` regardless, with `error_code 66` on failure.
- This path is untouched when `stt_provider` is unset or `openai`, and
  it is independent of the chat provider: flipping chat to Spark never
  changed STT, and flipping STT never changes chat.

## New flow (opt-in only)

- New provider module `site/hive/mqtt/muse_transcribe.py`: one-shot
  `POST https://api.meta.ai/v1/asr/transcribe`, model
  `muse-voice-transcribe-1.0`, Bearer auth, multipart upload.
- `STTSession._transcribe()` resolves the STT provider first. If
  `muse_transcribe` is selected and a key is present, it calls Transcribe
  with the same WAV bytes the Whisper path builds; on success it returns
  the transcript plus turn start/end offsets. The existing Whisper code
  is the fallback path, not a deleted path.
- **Streaming realtime is NOT implemented.** The realtime endpoint
  (`wss://api.meta.ai/v1/asr/realtime`, 80 ms chunks, diarization,
  endpointing) is a follow-up. This branch matches the existing
  whole-utterance flow on purpose; do not claim streaming, partial
  transcripts, diarization or endpointing from this branch.

## Config flag

- `HiveConfiguration.stt_provider` (migration `0020`, after `0019`):
  `openai` (Whisper, default) or `muse_transcribe` (opt-in).
- `HiveConfiguration.muse_transcribe_api_key`: optional override,
  blank-means-keep on the setup page, matching the other key fields.
- Setup page (`hive/templates/hive/setup.html`): "Speech-to-Text
  Provider" select + "(OPTIONAL) Muse Transcribe API Key Override"
  input, with a confirm dialog if Transcribe is selected with no Muse
  key stored or entered. `views.hive_configure` saves both fields;
  `moxie_server.update_from_database()` loads them into `ai_factory`
  (`set_stt_provider`, `set_muse_transcribe_key`) alongside the chat
  keys. Existing behaviour is unchanged until the flag is flipped:
  default rows read `stt_provider='openai'`.

## Key question — answered: reuse the Spark key, override available

**Answer: the existing Muse Spark key is expected to be sufficient;
no separate key entry is required to try Transcribe, but the override
field exists so a scope failure needs no code change.**

Evidence (not assumption):

- Spark chat already uses `https://api.meta.ai/v1` with a Bearer key
  (`ai_factory.MUSE_SPARK_BASE_URL`); Transcribe uses the same base
  URL, and its one-shot endpoint authenticates with the
  `Authorization: Bearer` header (streaming is the exception — its
  header is ignored and auth rides the handshake frame, which is one
  more reason the one-shot path was chosen first).
- Meta Model API documentation as summarised by LiteLLM states the
  same `META_API_KEY` serves chat and Muse Voice realtime; OrcaRouter's
  Muse API write-up states the platform's models (Spark, Image, Voice
  Transcribe, SAM) are reachable "on the same credential".
- Therefore `ai_factory.get_muse_transcribe_key()` returns the explicit
  override when set, else the Spark key (`has_muse_transcribe_key()`
  checks that effective key).

**Caveat that stays attached:** key *scope* has not been proven. No
live key value was ever read, printed, or copied, and no live endpoint
call was made (see below), so a Spark key that lacks Transcribe scope
or billing would surface as a 401/403 on the first live call — which
is exactly what the per-utterance Whisper fallback absorbs. The first
live one-shot call with Stefan's live key is the scope proof; if it
fails on auth, Stefan enters the override key on the setup page.

## Audio format sent (main unverified risk, handled explicitly)

**Sent: mono, 16-bit PCM WAV at 16 kHz.** The handler already builds
exactly this (`soundfile` write: samplerate 16000, `format='WAV'`,
`subtype='PCM_16'`, one channel from a 1-D int16 array), and this
branch sends those bytes unchanged — no resampling, no re-encoding.

- `muse_transcribe.validate_wav_bytes()` enforces before any network
  call: samplerate in (16000, 24000), exactly 1 channel, subtype
  `PCM_16`, non-empty, ≤ 32 MB. Anything else raises `ValueError`
  (or `MuseTranscribeError` for oversize) and the handler falls back
  to Whisper instead of sending an endpoint-rejected payload.
- Documented endpoint constraints (Meta docs via OrcaRouter): mono
  16-bit PCM WAV at 16 or 24 kHz only, 32 MB body cap, 10 minutes
  per request. A typical Moxie utterance is seconds long, far under
  both caps.
- **Unverified, stated plainly:** the exact multipart field names
  (`file` as `utterance.wav` + `model` field) and the exact one-shot
  JSON response shape were not pinned down in the published sources
  available. `parse_transcribe_response()` therefore accepts every
  plausible shape (`text` / `transcript` / `transcription` string, or
  a `turns`/`segments` list with text plus start/end in ms or seconds).
  If the live endpoint rejects the field names or returns an
  unrecognised shape, the utterance falls back to Whisper and the
  mismatch is visible in the warning log — it is not a silent drop,
  but the live shape still needs one real call to confirm.

## Fallback behaviour (never drop a voice turn silently)

- Transcribe selected, key present, call succeeds with non-empty
  transcript → Transcribe answer, `fell_back=False`.
- Transcribe selected but **no key** → Whisper, `fell_back=True`,
  Transcribe is not even called (selection-time fallback,
  source `whisper_fallback_no_key`).
- Transcribe selected, call fails for **any** reason (auth, network,
  timeout, HTTP error, bad WAV, unparseable response, **empty
  transcript**) → logged warning (type/status only, never transcript
  content, never a key) and Whisper for that utterance,
  `fell_back=True`.
- Whisper also fails → the pre-existing error reply (`error_code 66`,
  Whisper's message), exactly as before.
- `fell_back` surfacing: the `zmqSTTResponse` proto has **no**
  `fell_back` field, so it is surfaced on the result path that exists:
  `STTSession.fell_back` / `stt_provider` / `stt_source` attributes
  and a `STT-FALLBACK: provider=... fell_back=True` warning log line.
  Do not claim the robot receives a fell_back flag — it receives the
  same transcript reply as before.

## Cost comparison

| Provider | Rate | Per hour of audio | Notes |
|---|---|---|---|
| OpenAI Whisper-1 (current) | $0.006/min | **$0.36/hr** | Deprecated Aug 26, 2026; see below |
| Muse Voice Transcribe | $3.00 / 1,000 min | **$0.18/hr** | Same rate streaming or one-shot; billed per audio second (rounded down to whole seconds per Meta docs) |

Transcribe is half the Whisper-1 price at these rates. A 10-second
Moxie turn costs ~$0.0017 on Whisper-1 vs ~$0.0005 on Transcribe at
list rates; the saving is real but small at Moxie volumes — the
deprecation deadline, not the saving, is the reason to migrate.

## Deprecation note

OpenAI deprecated `whisper-1` (with `gpt-4o-transcribe`,
`gpt-4o-mini-transcribe`, `gpt-4o-transcribe-diarize`) on **August 26,
2026**, with **removal from the API on February 26, 2027**
(OpenAI deprecations page). OpenAI's named replacements are
`gpt-transcribe` / `gpt-live-transcribe`. Whisper-1 still works today,
but any STT plan that still depends on it after Feb 26, 2027 breaks.
This branch keeps Whisper-1 as the fallback while it exists; if the
live Transcribe flip has not landed by early 2027, the fallback itself
needs a replacement (OpenAI `gpt-transcribe` or Transcribe proven live)
before the shutdown date.

## Tests — correctness / fallback / no-key-logging

New file `site/hive/test_muse_transcribe.py` (28 tests), all mocked,
no network/broker/robot/DB/real key; the only key string is the fake
`test-key-not-real`:

- Provider selection: default Whisper; opt-in Muse with Spark key;
  opt-in Muse with override key and no Spark key; override beats Spark;
  blank override falls back to Spark; opt-in Muse with no key at all
  → Whisper with `fell_back=True`; unknown provider raises and the
  setter keeps the current provider; chat provider does not change STT.
- Audio conversion: existing handler WAV validates (16000 Hz, mono,
  PCM_16); 24 kHz accepted; stereo, 8 kHz, FLOAT subtype and empty
  payload rejected; bad WAV never reaches the network.
- Endpoint (mocked `http_post`): exact URL, Bearer header, multipart
  `utterance.wav`/`audio/wav`, `model` field; `text`, `turns` and
  `segments` (seconds) response shapes parsed; HTTP 401 raises with
  status; missing key and bad WAV raise without a network call; no
  fake-key string in Transcribe logs.
- Handler fallback: default Whisper never calls Transcribe; Muse
  success skips Whisper (timestamps 10100/10900 on a 10000 base);
  Muse error and Muse empty transcript both fall back to Whisper with
  `fell_back=True` and no key in logs; Muse selected with no key
  falls back without calling Transcribe; both providers failing still
  sends `error_code 66`.
- Model defaults: fresh `HiveConfiguration()` reads
  `stt_provider='openai'`, `muse_transcribe_api_key=''`.

Pre-existing suites still pass unchanged, including
`test_stt_client_stays_openai_when_chat_is_spark` (the handler imports
no chat-client factory) and all of `test_stt.py`.

## Tests — latency (mocked) and accuracy smoke harness

New files: `site/hive/mqtt/voice_turn_latency.py` (harness) and
`site/hive/test_stt_latency.py` (14 tests, 13 run + 1 skipped).

- `measure(fn)` returns `(value, duration_ms)` wall-clock time via
  `perf_counter`.
- Timed mocked Transcribe and mocked Whisper calls: timing recorded
  (finite, non-negative float, under a generous 5 s local ceiling —
  never a brittle absolute threshold). A mocked call is sub-millisecond
  locally; that proves plumbing, not latency.
- Timed mocked Spark chat call in the same harness, so STT vs chat
  timings are comparable in shape.
- End-to-end voice-turn harness `run_mocked_voice_turn()`: stages
  `stt → chat → tts_ready`, per-stage timings + wall-clock total,
  `transport='mocked'` label. "TTS-output-ready" is the point the chat
  reply is handed to the TTS stage (a marker callable here); the real
  system synthesises on the robot, and no robot is involved. Includes
  a Whisper-fallback turn variant (`fell_back=True`, provider `openai`).
- Live-timed test: gated on BOTH `MOXIE_STT_LIVE_TIMING=1` AND
  `MUSE_TRANSCRIBE_TEST_API_KEY` being set by the operator. With
  neither set (the normal case, including this run) it **skips cleanly
  and makes no paid live call**. The key, when supplied, is used
  transiently as a Bearer header only and never logged.
- Accuracy smoke harness: `wer(reference, hypothesis)` (word-level
  Levenshtein / reference words). No recorded audio is checked in:
  the sample is a synthetic 1 s silence WAV generated in memory
  (endpoint-valid format, no speech, no personal data) and both
  providers are mocked with canned transcripts, so WER 0.0 (agreeing)
  vs 0.5 (one substitution in two words) exercises the harness only.
  **Live accuracy scoring is still pending** — it needs a real
  recorded phrase run through both live providers, daylight, Stefan
  in view.

### Timing table (mocked probe, single run, worktree venv)

| Stage | Mocked time | Live time | Notes |
|---|---|---|---|
| STT — Muse Transcribe one-shot (mocked `http_post`) | **0.076 ms** | skipped | Mock returns instantly; proves request/response plumbing only |
| Chat — Muse Spark (mocked client) | **0.014 ms** | skipped | Same harness; STT-vs-chat comparison is shape-only at this scale |
| Voice turn end-to-end (mocked lambdas: stt + chat + tts_ready) | **0.001 ms total** (stages ~0.000 ms each) | skipped | Trivial lambdas; total covers stages, as designed |
| STT — Whisper (mocked client, unit tests) | recorded, < 5 s ceiling | skipped | Timing asserted recorded, not thresholded |
| Live Transcribe timed call | — | **skipped** | No test key configured; no paid call made on assumption |

Caveat, repeated because it matters: **mocked times prove plumbing,
not real-world latency.** Real STT latency includes network round-trip
to `api.meta.ai`, model inference on seconds of audio, and the robot
round-trip; none of that is in these numbers. The first real latency
figures must come from the daylight live voice test (below), measured
with this same harness pointed at the live providers.

## Full suite results (primary output, worktree venv)

Run from `site/` with `~/dev/openmoxie-contrib/.venv-stageb/bin/python`
(Python 3.9.6; the worktree has no venv of its own):

```text
python -m unittest hive.test_muse_transcribe hive.test_stt_latency hive.test_stt \
  hive.test_muse_spark hive.test_mycroft_bridge hive.test_overnight \
  hive.test_control_commands hive.test_bridge_wiring hive.test_logs_view
Ran 215 tests in 0.215s — OK (skipped=1)
```

Per-module: `test_muse_transcribe` 28 · `test_stt_latency` 14 (13 run,
1 skipped = live-timed test, no key) · `test_stt` 6 ·
`test_muse_spark` 32 · `test_mycroft_bridge` 26 · `test_overnight` 23 ·
`test_control_commands` 25 · `test_bridge_wiring` 45 · `test_logs_view` 16.
Total 215, all passing except the 1 intentional live skip.

## Test-server verification (separate ports/DB/broker, robot never pointed)

- Test server `~/OpenMoxie-test-stt/` (compose file there, not in the
  branch): images `openmoxie-stt-test-server` / `openmoxie-stt-test-mqtt`,
  containers `openmoxie-stt-test-server-1` / `openmoxie-stt-test-mqtt-1`,
  ports **8003** (server) / **8885** (broker) — live stays 8001/8883,
  earlier test server 8002/8884. DB is a **copy** of the old test DB
  (`~/OpenMoxie-test/local/work/db.sqlite3`), never the live DB.
  Build note: non-interactive SSH lacks the Docker Desktop credential
  helper, so the build ran with a clean `DOCKER_CONFIG` (empty
  config.json + copied `docker-compose` plugin) — that is a local
  shell quirk, not a code change.
- Container startup applied migrations including
  `hive.0020_hiveconfiguration_stt_provider` (`[X]`, plus
  `makemigrations --check --dry-run`: "No changes detected").
- Inside the test container: `python -m unittest
  hive.test_muse_transcribe hive.test_stt_latency hive.test_stt` →
  **Ran 48 tests — OK (skipped=1)** (28 + 14 + 6, same live skip).
  STT provider imports, config switch and mocked one-shot all pass
  in the container image, not just the worktree venv.
- Served pages on 8003: `/hive/setup` HTTP 200 and rendering the
  `stt_provider` select + `musetranscribeapikey` input (4 occurrences
  each in the HTML); `/hive/dashboard` HTTP 200; test-DB config row
  reads `stt_provider=openai` (default preserved after migration).
- After verification the test containers were stopped
  (`docker compose down` in `~/OpenMoxie-test-stt/`); images and the
  test DB copy remain for re-verification. Live containers
  (`openmoxie-server-spark-switch` / `openmoxie-mqtt-spark-switch`)
  stayed up on 8001/8883 throughout. No QR was shown, no robot config
  was changed, and the robot was never pointed at 8003/8885.

## Files changed (branch `mycroft-stt-swap`)

- `site/hive/mqtt/muse_transcribe.py` (new) — one-shot provider,
  WAV validation, response parsing, key-safe errors.
- `site/hive/mqtt/voice_turn_latency.py` (new) — timed voice-turn
  harness + WER helper, mocked transport only.
- `site/hive/mqtt/ai_factory.py` — STT provider globals + resolver
  (`resolve_stt_provider_detailed`), Transcribe key resolution
  (override else Spark key). Chat dispatch untouched.
- `site/hive/mqtt/zmq_stt_handler.py` — `_transcribe()` with
  Transcribe-first + Whisper fallback; `fell_back`/`stt_provider`
  on the session; Whisper path and error reply unchanged.
- `site/hive/models.py` + `site/hive/migrations/0020_*` —
  `stt_provider` + `muse_transcribe_api_key` on `HiveConfiguration`.
- `site/hive/views.py`, `site/hive/mqtt/moxie_server.py`,
  `site/hive/templates/hive/setup.html` — setup save/load/render
  for the new fields.
- `site/hive/test_muse_transcribe.py` (new, 28 tests),
  `site/hive/test_stt_latency.py` (new, 14 tests).
- `CHECKPOINT-STT-SWAP.md` (this file).

## What is needed before a live flip (all still open)

1. **Stefan's live config flip, by him, in daylight, in view:**
   set `stt_provider=muse_transcribe` on the live setup page (the
   Spark key already there is the expected Transcribe key; enter the
   override only if auth fails). Do not flip from a script, and do
   not flip while the robot is asleep/away.
2. **Live promotion via the full-branch path only** (never file-copy):
   this branch must reach live as a built image + code set, with
   migration `0020` applied to the live DB as part of promotion,
   exactly as the spark-switch promotion did for `0019`.
3. **Daylight live voice test with Stefan in view, in order:**
   (a) one live one-shot Transcribe call first (scope proof for the
   Spark key + multipart/response-shape proof — if it 401s, enter
   the override key and retry; if the shape mismatches, fix parsing
   in the clone, never live); (b) wake greeting; (c) one chat turn
   on Transcribe STT; (d) one forced-fallback turn (e.g. Transcribe
   endpoint blocked) proving Whisper answers; (e) the same harness
   timings recorded live, replacing the mocked table above.
4. **Live accuracy scoring** (pending, see above): a real recorded
   phrase through both live providers, WER vs the spoken phrase,
   before any claim that Transcribe is as accurate as Whisper for
   Moxie's use.
5. **Streaming decision:** realtime WebSocket STT (partial transcripts,
   diarization, endpointing) remains unimplemented; decide later
   whether the one-shot flip is sufficient or streaming is wanted.
   Until then, do not describe live Moxie STT as streaming.
6. **Whisper-1 sunset watch:** if the live flip has not landed well
   before Feb 26, 2027, plan the fallback replacement (see Deprecation
   note) — the fallback is only as durable as Whisper-1 itself.
