# CHECKPOINT — OpenMoxie modern web UI + hosting upgrade (clone + test server ONLY)

Branch: `mycroft-modern-ui` (from `mycroft-stt-streaming` commit `1f0d63a`)
Worktree: `~/dev/openmoxie-modern`
Status: **NOT live. NOT merged.** No live container, DB, cert, config or
robot was touched. Live stays on branch `mycroft-spark-switch-live`
containers `openmoxie-server-spark-switch` / `openmoxie-mqtt-spark-switch`
on 8001/8883. Nothing in this branch flips live; live promotion is a
separate, Stefan-approved step (see "What is needed before a live flip").

## Before / after (what changed, in one paragraph)

Before: every served page repeated the same black header bar, loaded a
Bootstrap CSS/JS pair whose local files do not exist in the repo
(`site/static/bootstrap/...` is referenced by settings but absent, so
those requests 404) and a hive stylesheet containing only chat-window
and header rules — in practice the pages rendered largely unstyled,
with tables, tiny controls and no responsive layout.
`moxie_data.html` and `import.html` additionally loaded a JSON viewer
from `https://unpkg.com` (a CDN dependency that fails on a LAN without
internet and phones home to a third party). After: one shared
`base.html` (header + nav + skip link + footer, rendered once),
one self-hosted vanilla stylesheet — the served copy is
`site/static/hive/modern.css` (verified 200 on the test server), with
an identical copy at `site/hive/static/hive/modern.css` for the
staticfiles finder — and rewritten dashboard / setup / logs pages
built from status cards, sections and labelled forms. All other served pages keep their inner markup and forms and
inherit the new look from the shared base + stylesheet; their only
changes are the removal of the now-duplicated header line, the CDN
viewer swap (below), and `aria-label`s on the moxie edit controls.
No route, view, POST endpoint or form field name was changed.

## Design rules honoured

- Vanilla CSS + minimal vanilla JS only. No framework, no build step,
  no CDN asset: the only `https://` strings left in templates are the
  two informational outbound links in setup copy (OpenAI billing / API
  keys, Google Cloud) — they are links a human may click, not assets
  the page needs to render, and the pages render fully without them.
- Responsive: card grid collapses, tables scroll horizontally instead
  of squashing, forms stack under 640 px, viewport meta present.
- Dark/light: light by default; `prefers-color-scheme: dark` theme with
  the same contrast treatment. No manual theme toggle (kept out to
  avoid a cookie/JS state mechanism on a LAN bootstrap page).
- Accessibility: skip link, labelled controls (every setup control has
  a `<label for>`), fieldsets/legends on setup, table captions +
  `scope`, visible focus outlines, plain-language empty/error states,
  `aria-label`s on log boxes and moxie edit controls.
- Honest status only: every dashboard value comes from the DB
  (`MoxieDevice.state`, `robot_config`, `HiveConfiguration`) or the
  broker connected list. Missing firmware/battery render as
  "Unavailable", never as a guessed number. See "Status derivation".

## Status derivation (dashboard cards — read this before trusting a card)

- Connection: Online = device id is in the broker connected list
  (same source the old dashboard used); otherwise Offline, except:
- Sleep / Standby: only when the stored robot `state.mode` string
  contains "sleep" / "standby" (case-insensitive). A stored mode of
  anything else renders verbatim as "robot mode: …" next to the
  connection label, and an absent mode renders nothing extra.
- Firmware: `state.robot_firmware_version`, else "Unavailable".
- Battery: `state.battery_level`, else "Unavailable".
- Mode: "Puppet" when `robot_config.moxie_mode == "TELEHEALTH"`,
  else "Stock". **Bridge is deliberately not shown per device:**
  bridge mode is an in-process `BridgeController` state with no HTTP
  toggle and no persisted per-device flag, so the dashboard cannot
  observe it; it defaults to Stock and the card says so in a footnote
  rather than inventing a BRIDGE badge. If a future branch persists
  bridge state, the card should read that flag — do not guess it.
- STT card: the existing `stt_status` context (provider, effective
  mode, streaming/diarization/code-switching flags, endpointing mode +
  timeout, language hint, Whisper-fallback note), unchanged in logic.
- Chat card (new): `chat_status` from `HiveConfiguration` — provider
  label (GPT (OpenAI) / Muse Spark (Meta)) and model. For GPT the
  model is per-conversation, so the card honestly says
  "Per-conversation setting" instead of naming one model. No config
  row → "No configuration saved yet" empty state.

## Page-by-page field inventory (nothing dropped)

Verified mechanically: each name below was counted in the new template
and in the HEAD version; counts match except where noted as an
intentional duplicate (dashboard cards repeat the per-device links
that the device table also carries).

### Setup (`site/hive/templates/hive/setup.html`, POST → `hive_configure`, unchanged)

| Field (name = id) | Type | Notes |
|---|---|---|
| `apikey` | text | blank = keep stored OpenAI key; value never echoed |
| `chat_provider` | select openai/muse_spark | unchanged options |
| `muse_spark_model` | select muse-spark-1.3 / -contributor | unchanged options + terms note |
| `stt_provider` | select openai/muse_transcribe/muse_streaming | all STT knobs below |
| `stt_streaming_enabled` | checkbox | separate switch, off by default |
| `stt_diarization_enabled` | checkbox | streaming only, off by default |
| `stt_endpointing_mode` | select auto/server_timeout/disabled | unchanged |
| `stt_endpointing_timeout_ms` | number 200–5000, step 100 | server clamps on save |
| `stt_language_hint` | select auto + 25 codes | unchanged list |
| `stt_keyword_bias` | text | unchanged validation note |
| `stt_code_switching_enabled` | checkbox | off by default |
| `musetranscribeapikey` | password | blank = reuse Muse Spark key |
| `musesparkapikey` | password | blank = keep stored key |
| `googleapikey` | text | JSON validated client-side, as before |
| `hostname` | text | required, as before |
| `adminUser` / `adminPassword` | text / password | only when `needs_admin` (no superuser yet) |
| `allowall` | checkbox | **still inside the same HTML comment as stock** — not a live field, preserved verbatim, not activated |

Also preserved: `provider_status` presence-only line (key configured /
not configured, never a value), the LAN/firewall warning, the OpenAI
and Google intro copy, and the submit-validation JS — rewritten from
jQuery to vanilla JS with the same checks and the same messages
(hostname required, Google key must be valid JSON, admin required when
shown, confirm prompts when Muse chat/STT is selected without any Muse
key). `hive_configure` itself is untouched: same validation
(`validate_stt_knobs`), same clamping/cleaning, same blank-keeps-key
behaviour, still not CSRF-exempt (proven by test).

### Dashboard (`site/hive/templates/hive/dashboard.html`)

All stock routes/links present: `endpoint_qr`, `wifi_edit`, `setup`,
`logs`, `reload_database`, `export_content`, `import_review`
(import form: field `json_file`, label added; a duplicated
`method` attribute in stock was fixed to a single `method="post"`),
per-device `moxie`, `moxie_missions`, `face`, `puppet`, `moxie_data`,
`moxie_wake` (only when wake is enabled and the device is online, as
before), per-conversation `interact`. The per-device links now appear
twice (once in the new status cards, once in the retained device
table) — that is the only count change, and it is intentional.
Admin link kept, rendered only when admin URLs are mounted
(`{% url as %}` guard, so offline rendering does not break).

### Logs (`site/hive/templates/hive/logs.html`, staff-gated, unchanged gating)

Filter fields unchanged: `file`, `device`, `tag`, `level`, `q`,
`lines` (GET → `hive:logs`); `refresh_now` button, `auto_refresh`
(5 s) checkbox, `logs_api` JSON refresh, `log-missing` /
`log-box` / `log-line` / `log-failure` / `log-ota` classes. The
inline `<style>` block moved into `modern.css` with identical class
names. New: plain-language empty state when a file exists but no
lines match, `aria-label` + keyboard focus on log boxes.
**Gating unchanged:** `staff_member_required` on `logs_view` /
`logs_api`, anonymous → 302 to `/admin/login/` (proven by test).

### Other served pages (inner markup unchanged except header + notes)

| Page | Fields / functions preserved |
|---|---|
| `moxie.html` (POST `moxie_edit`) | `moxie_name`, `pairing_status`, `nickname`, `schedule`, `screen_brightness`, `audio_volume`, firmware / connect / battery / mode display; only added `aria-label`s |
| `wifi.html` (POST `wifi_qr`) | `ssid`, `password`, `frequency`, `hidden` |
| `face.html` (POST `face_edit`) | `asset_<layer>` selects, `child_recover` |
| `puppet.html` (POST `puppet_api`) | `mood`, `intensity`, `user_input`, puppet/interrupt buttons |
| `interact.html` (POST `interact_update`) | `user_input`, chat history/window ids |
| `export.html` | `conversations`, `globals`, `schedules`, `content_name`, `content_details` |
| `import.html` | `conversations`, `globals`, `schedules`, `json_data`; CDN pretty-json viewer → `<pre class="json-view">` showing the same `json_data_str` |
| `missions.html` | `mission_sets`, `mission_action` |
| `moxie_data.html` | `persist_data`, `active_config`; CDN pretty-json viewer → `<pre class="json-view">` (collapsible viewer lost; plain readable JSON gained; offline-safe) |
| `results.html` | **Not a served page**: no URL route references it (stock Django tutorial leftover). Left untouched, flagged here so it is not mistaken for a regression. |

### Gating flag (honest, as tasked)

- Logs: staff-gated before and after (see above).
- **Setup and dashboard were NOT gated in stock code** (plain
  `TemplateView`s, no login decorator) and are still not gated here.
  This branch does not weaken any gate, but it also does not newly
  gate them: setup is the LAN bootstrap page that creates the first
  admin, so gating it before an admin exists would lock out first
  setup. Any decision to add a gate belongs to a separate, explicit
  Stefan decision — it is not smuggled into a UI redesign.

## Hosting changes (`docker-compose.yml` — live compose in this branch)

- Healthchecks, server + broker, observability only:
  server = `python3` urllib GET of `/hive/` (config-row read +
  redirect only, no MQTT touch); broker = TLS `mosquitto_sub` for one
  `$SYS/broker/uptime` message (proves listener + certs + broker).
  `depends_on` deliberately has **no** `service_healthy` condition, so
  a flaky probe can never block the server from starting.
- Restart: `unless-stopped` on both services (stock had none).
- Image tags pinned to the branch: `openmoxie-server:mycroft-modern-ui`
  / `openmoxie-mqtt:mycroft-modern-ui` (stock defaulted to `:latest`),
  so building this branch cannot overwrite live images.
- Resource limits (deploy.resources): server 1 CPU / 1024 MB
  (256 MB reserved), broker 0.5 CPU / 256 MB (64 MB reserved).
  Note: under plain `docker compose up` these are best-effort hints
  in some Compose versions; they are recorded here as the intended
  limits, not as a measured enforcement claim.
- Unchanged on purpose: ports (`8001:8000`, `8883:8883`), volumes
  (`./local/work` → server work dir + broker log dir, so structured
  logging to `debug.log` / `mosquitto.log` is retained), no new
  auth, no public exposure, no cloud hosting. Port bindings were
  **not** moved to loopback: the robot connects over the LAN, so
  loopback would break it; the firewall in front of the host remains
  the LAN-only control (as the setup warning states).
- New container names (`openmoxie-server-modern-ui` /
  `openmoxie-mqtt-modern-ui`): a promotion must stop the previous
  live containers first, or the new ones will fail on the already-bound
  8001/8883 ports. See rollback below.
- Test compose is separate and lives outside the branch
  (`~/dev/openmoxie-modern-test/docker-compose.yml`): test-only image
  and container names, ports 8005/8887 bound to `127.0.0.1` only
  (mini-local, never LAN), DB a copy of the old test DB.

## Tests (primary output, worktree venv)

Runner: from `site/` with
`~/dev/openmoxie-contrib/.venv-stageb/bin/python -m unittest …`
(Python 3.9.6; the worktree has no venv of its own).

New module `site/hive/test_modern_ui.py` — **16 tests**, all offline:
base shell + no-CDN/no-Bootstrap assertions, modern.css presence +
legacy-copy equality, dashboard render (cards, all routes, file
input label) + empty states, device-card derivation (online / sleep /
standby / offline / Puppet / Stock / Unavailable fallbacks),
chat-status labels, setup render (all 18 fields, selected/checked
states, stored key never echoed, presence-only line),
setup label-for-every-control source check, `hive_configure` still
persists every knob incl. clamp (99999 → 5000) and chat model,
logs render (all filters, empty state) + staff gate unchanged +
no new CSRF exemption, no duplicated header in child templates,
secondary pages render (moxie / wifi / face / interact / puppet)
+ secondary field names in source, compose hardening assertions.

Full suite (primary output):

```text
Ran 295 tests in 0.204s — OK (skipped=1)
```

Per module: `test_modern_ui` 16 · `test_muse_transcribe_streaming`
56 · `test_muse_transcribe` 28 · `test_stt_latency` 14 (13 run + 1
pre-existing live-timing skip, no key) · `test_stt` 6 ·
`test_muse_spark` 32 · `test_mycroft_bridge` 26 ·
`test_overnight` 23 · `test_control_commands` 25 ·
`test_bridge_wiring` 45 · `test_logs_view` 16 · `test_chat_models` 8.
No existing test regressed. Baseline for comparison: the streaming
branch reported 271 for its 10-module list; this run adds the 16 new
tests and the 8 `test_chat_models` tests (not in that list) = 295.

`makemigrations --check --dry-run` from the worktree shell fails on
the file log handler (it needs `site/work`, which only exists inside
the container via the `./local/work` mount), so migration state was
verified inside the test container instead: the test server started
from the pre-STT old test DB, its startup migrate applied `0019` /
`0020` / `0021`, and a later in-container `--check --dry-run`
reports "No changes detected" (see test-server section). No new model
fields were added by this branch, so no new migration exists.

## Test-server verification (separate ports/DB/broker, robot never pointed)

Test server: `~/dev/openmoxie-modern-test/` (compose + DB copy outside
the branch), images `openmoxie-modern-ui-test-server` /
`openmoxie-modern-ui-test-mqtt`, containers
`openmoxie-modern-ui-test-server-1` / `-mqtt-1`, ports **8005**
(server) / **8887** (broker), both bound to `127.0.0.1` only.
DB is a copy of the old test DB (`~/OpenMoxie-test/local/work/`),
never the live DB. The robot was never pointed at 8005/8887, no QR
was shown, no live config changed.

- Build from this branch succeeded; container startup ran
  `makemigrations` ("No changes detected" — this branch adds no model
  fields) then `migrate`, applying `hive.0019`, `0020` and `0021` to
  the pre-STT test DB, then `init_data`. Inside the container,
  `makemigrations --check --dry-run` afterwards also reports
  **"No changes detected."**
- Static-serving discovery (fixed in-branch, not worked around):
  the first test run returned 404 for `/static/hive/modern.css`
  while `/static/hive/style.css` returned 200, because
  `openmoxie/urls.py` serves `/static/` only from `STATIC_ROOT`
  (`site/static/`), not from app static dirs. Fix: the served copy
  now lives at `site/static/hive/modern.css` (identical content to
  the app-static copy, enforced equal by test); after rebuild,
  `/static/hive/modern.css` → **200** (9893 bytes).
- Served pages on 8005: `/hive/` → 302 to `/hive/dashboard`;
  `/hive/dashboard` **200**; `/hive/setup` **200**;
  `/hive/logs/` anonymous **302** to `/admin/login/?next=/hive/logs/`;
  `/hive/logs_api/` anonymous **302**; `/hive/moxie/1`,
  `/hive/moxie_data/1`, `/hive/face/1`, `/hive/puppet/1`,
  `/hive/moxie_missions/1`, `/hive/interact/1`, `/hive/wifi_edit/`,
  `/hive/export_content/` all **200** (the slash-less wifi/export
  URLs 301 to their slash versions, stock Django behaviour).
- Rendered dashboard snippet (test DB, no personal data, no keys):
  STT card — "muse_streaming / whisper (Whisper fallback — no Muse
  key)", Streaming On, Diarization On, Code-switching On,
  "server_timeout (2500 ms) · language hint: fr"; Chat card —
  "Muse Spark (Meta)" / "muse-spark-1.3-contributor"; device card —
  "Sleep · robot mode: sleep" (sleep derivation from stored
  `state.mode`, proven on a real served page, not just a unit test).
- Setup persist on the test DB (real CSRF-valid POST, knob columns
  only — key values were left blank and never read): posted
  provider muse_streaming, streaming/diarization/code-switching on,
  endpointing server_timeout, timeout 2500, language fr, keyword
  "Moxie", chat muse_spark / contributor model, hostname
  testhost-modern → POST 302 to dashboard; re-rendered setup shows
  every value selected/checked, and the test-DB config row reads
  back exactly those knob values.
- After verification the test containers were stopped
  (`docker compose down` in `~/dev/openmoxie-modern-test/`); images
  and the test DB copy remain for re-verification. Live containers
  (`openmoxie-server-spark-switch` / `openmoxie-mqtt-spark-switch`)
  stayed up on 8001/8883 throughout, verified before, during and
  after the test run.

## Rollback path (for a future live promotion — NOT executed here)

1. Stop the new containers (`docker compose down` in the promoted
   directory, or `docker stop openmoxie-server-modern-ui
   openmoxie-mqtt-modern-ui`).
2. Restart the previous live containers
   (`openmoxie-server-spark-switch` / `openmoxie-mqtt-spark-switch`);
   they still own 8001/8883 once the new ones are stopped.
3. DB restore ONLY on Stefan's explicit direction, never
   automatically: this branch adds no model fields and no migration,
   so a rollback normally needs no DB change at all; if a migration
   ever lands in a follow-up, restore from the pre-promotion backup
   of `./local/work/db.sqlite3` taken before the flip.
4. Certs: untouched by this branch (live LE broker cert, expires
   Jan 27, 2027, is not renewed or replaced here).

## What is needed before a live flip (checklist, none of it done here)

- [ ] Stefan reviews this checkpoint + the rendered test pages and
  approves a full-branch promotion (image build from
  `mycroft-modern-ui`, never a file copy into live — the Oct 29
  file-copy deploy failure mode still applies).
- [ ] Pre-flip backup: `./local/work/db.sqlite3` + live keys/certs dir
  (`~/OpenMoxie/mosquitto-keys/` via the live override), snapshot
  name + time recorded.
- [ ] Promotion in daylight with Stefan in view: stop previous live
  containers first (new container names + same ports), start the
  branch images, confirm dashboard 200, setup renders all knobs,
  logs anon 302, healthchecks report healthy, and the robot's next
  reconnect is observed (robot is never pointed at a test server).
- [ ] Decide separately whether setup/dashboard should gain any gate
  (see "Gating flag") — not part of this branch.
- [ ] Confirm resource limits are actually enforced in the deployed
  Compose version (`docker inspect` HostConfig) rather than assumed.
- [ ] Re-run `makemigrations --check --dry-run` inside the promoted
  container before declaring the flip clean.

## Files changed (branch `mycroft-modern-ui`)

- `site/hive/templates/base.html` — rewritten shell (nav, skip link, footer, self-hosted CSS only).
- `site/hive/static/hive/modern.css` — new (finder copy);
  `site/static/hive/modern.css` — new (served copy, same content);
  `site/static/hive/style.css` — same content (legacy path kept consistent).
- `site/hive/templates/hive/dashboard.html` — rewritten (cards + retained tables/forms).
- `site/hive/templates/hive/setup.html` — rewritten (fieldsets, same fields/JS behaviour, vanilla JS).
- `site/hive/templates/hive/logs.html` — rewritten (same filters/API, styles moved to CSS, empty state).
- `site/hive/templates/hive/{export,face,import,interact,missions,moxie,moxie_data,puppet,wifi}.html` — header line removed (now in base); `moxie_data`/`import` CDN viewer → `<pre>`; `moxie` aria-labels.
- `site/hive/views.py` — DashboardView gains `device_cards` + `chat_status` (helpers `_device_status_card`, `_chat_status`); all existing context keys kept.
- `site/hive/test_modern_ui.py` — new, 16 tests.
- `docker-compose.yml` — healthchecks, restart, branch-pinned tags, resource limits, LAN-only + backup/rollback header notes.
- `CHECKPOINT-MODERN-UI.md` — this file.
- Test-only, outside the branch: `~/dev/openmoxie-modern-test/` (compose + copied test DB). No live files were edited.
