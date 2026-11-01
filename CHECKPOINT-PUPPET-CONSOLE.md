# CHECKPOINT — Puppet Console for OpenMoxie (clone + test server only)

Branch: `mycroft-puppet-console` (from `mycroft-stt-streaming` commit `1f0d63a`)
Worktree: `~/dev/openmoxie-puppet`
Status: **NOT live. NOT merged.** No live container, DB, cert, config or
robot was touched. No live robot command was sent, and the robot was
never pointed at a test server. Nothing here flips live, and no live
puppeteering behaviour is claimed. Live still runs 8001/8883
(`openmoxie-server-spark-switch` / `openmoxie-mqtt-spark-switch`,
DB migrations through 0021), verified still up after the test server
was stopped.

This checkpoint covers BOTH parts: the main puppet console and the
Vision Events panel (Stefan's "can we get the camera?" question), in
one commit series on the same branch.

## What was built

New URLs (staff only, LAN only, same hive UI mount):

- `hive/puppet_console/<pk>/` — the operator page (form POSTs, plus
  live status via the API).
- `hive/puppet_console_api/<pk>/` — JSON API for the page (GET status,
  POST one action).
- Dashboard gains a "Puppet Console" link next to "Puppet".

New files:

- `site/hive/puppet_inventory.py` — verified inventory (below).
- `site/hive/puppet_console.py` — page + API views, one-at-a-time
  enforcement, sequence state, session transcript, auto-exit.
- `site/hive/vision_events.py` — vision event inventory, detector
  payload builders, in-memory event store.
- `site/hive/templates/hive/puppet_console.html` — the console page,
  extending the existing stock `base.html` so it renders standalone now.
- `site/hive/test_puppet_console.py` — 32 offline tests.

Changed files: `site/hive/views.py` (import only), `site/hive/urls.py`
(two routes), `site/hive/templates/hive/dashboard.html` (one link).
No model was changed, so no migration is owed. (`manage.py
makemigrations --check` could not run in the bare worktree because the
project logging config requires a file-handler path that is only
present inside a container; that failure is environmental, not a
model change, and `git status` confirms `models.py` is untouched.)

## Verified inventory summary (from code, not guessed)

Sources read first: `site/hive/views.py` puppet_api,
`site/hive/mqtt/moxie_server.py`, `site/hive/mqtt/control_commands.py`
(manifest loading), `site/hive/mqtt/dance_modules.py`,
`gesture_conversation.py`, `creative_modes.py`, `show_me.py`,
`mycroft_bridge.py`, `volley.py`, and `doc/RemoteModuleAPI.md`.

Manifest total: **574** BehaviourTree rows with Default=True.
Offered as one-click console controls (each maps to an inventory entry):

| Category | Count | Transport / rule |
|---|---|---|
| Controls (speak, interrupt, wake, sleep, puppet enable/disable, wake greeting) | 7 | Telehealth speech/interrupt/behaviour, wake, remote_chat sleep payload, `moxie_mode` config |
| Gestures (operator) | 499 | Telehealth behaviour-tree mark, duration 0.1-30.0s, default 1.0s |
| Looks | 5 | `bht_360_look_around`, `bht_search`, `bht_search_still`, `bht_searching`, `bht_sensoryidlefacetracker_anim` (default) |
| Dances | 23 | `bht_dance_*` + `bht_celebration_dance_01-04`, behaviour-tree dance path |
| Gesture cues | 7 | wave, hello, arms out, celebrate, look, search, sleep (sleep cue is speech only, NOT the sleep command) |
| Celebration steps | 3 | celebrate, arms out, look — stepped manually, one per click |
| Storytelling | 1 | One beat per click (operator text + optional gesture cue, single telehealth call) |
| Show-me (Phase A) | 3 | start / describe (text only) / acknowledge; no storage |
| Creative record | 1 | Reminder record only: no timer, no delivery, nothing sent |

Manifest-only, NOT offered (47): `AUTO_GESTURE_*` (41, speech-system
auto gestures, not operator gestures) and `Bht_Motor_Test_*` (6,
diagnostic trees). They stay in the inventory so the record is
complete, but no console button maps to them — this narrows, never
widens, the stock puppet_api, which would accept any manifest tree.
The console is therefore stricter than stock by design.

Plain-language names are derived mechanically from tree ids
(`bht_arms_out` -> "arms out"); no meaning was invented for any tree.

Constraints enforced in code (not just in copy):

- Exactly one command per POST; a bundled `actions` field is rejected.
- One behaviour at a time, server-side (`ConsoleActivity` lock): a
  second trigger while one is active is rejected (400), never queued.
- The server receives no behaviour-completion event on the telehealth
  path, so completion is operator-confirmed (Mark complete) or cleared
  by Interrupt. This is stated on the page; completion is never faked.
- Interrupt always wins: it sends the robot interrupt, clears the
  active behaviour, clears any sequence, and cancels any show-me session.
- Dance: refused by default. Per-turn opt-in only, online/awake only,
  one at a time, duration clamped 10-60s (default 30s), interrupt
  releases it. The console uses the behaviour-tree dance path, NOT the
  remote_chat `DANCE` module launch, which stays disabled
  (`ALLOW_DANCE_MODULE=False` in control_commands).
- Known pre-existing inconsistency, flagged not hidden: dance duration
  is clamped to 10-60s in dance_modules, but the telehealth
  behaviour-tree mark builder clamps its duration parameter to 30s
  (`parse_duration` in control_commands). A 60s dance turn therefore
  carries a 60s turn duration in the console response while its mark
  duration is capped at 30s. How long a dance actually plays on the
  robot is a live-verification item, not something this branch asserts.
- Celebration stepped manually, one step per click; storytelling one
  beat per click; sequences never auto-advance, never loop, and stop
  clears the sequence.
- Speak applies `phonetic_name` at the output boundary only, so
  "Mycroft" is spoken as "my croft".

## Vision Events panel (the "can we get the camera?" answer)

Honest answer, on the page: **the hardware camera exists (5MP forehead
camera for gaze/face tracking), but OpenMoxie exposes detection events
only. There is no image/video/snapshot transport in the server code
(only ZMQ audio for STT), so a live video feed is not in this build
and is not faked.** A real feed would need a new robot-side image
transport. The page contains no video element, no camera image, and no
placeholder presented as live (tested, below).

Vision event inventory (from `doc/RemoteModuleAPI.md` only):

| Event | Value (input_vars) | Meaning |
|---|---|---|
| `eb-found-face` | none | A face matching the search parameters was found |
| `eb-lost-face` | none | Target face lost from view (Events section name) |
| `eb-lost-target` | none | Face lost for too long (Face Search section name) |
| `eb-br-event` | `$eb_br_value` | Moxie book detected; value is the book name |
| `eb-dr-event` | `$eb_dr_value` | Aruco code detected; value is the code |
| `eb-qr-event` | `$eb_qr_value` | QR code read; value is the QR string |

Doc discrepancy, stated not smoothed over: the Face Search section
says `eb-lost-target` is generated when a face is lost, while the
Events section lists `eb-lost-face`. Both names are inventoried and
accepted; neither is collapsed into the other.

Detector controls (only where the RemoteModule API provides them):

| Detector | Function | Subscribes to |
|---|---|---|
| Face search | `eb_start_binned_face_search` | `eb-found-face`, `eb-lost-face`, `eb-lost-target` |
| Book detector | `eb_enable_book(bool)` | `eb-br-event` |
| Aruco detector | `eb_enable_draw(bool)` | `eb-dr-event` |
| QR detector | `eb_enable_qr(bool)` | `eb-qr-event` |

Detectors are RemoteModule `execute` actions with an
`event_subscription` record (built with the real `Volley`
`add_execution_action` + `update_subscriptions` shapes), NOT telehealth
commands — there is no telehealth detector function in
`moxie_server.py`. Enabling a detector subscribes to its events so
detections surface as events, never as speech input confusion
(`parse_vision_input` accepts only exact eb-* names; anything else is
not a vision event). Each detector stays active only until one item of
its type is detected, per the doc; re-enable to keep detecting.
Experimental `eb_custom_face_search` is inventoried but NOT offered as
a control, because the doc says only two of its five parameters
actually work.

**Live-forwarding gap (verified by grep): no `site/*.py` code handles
any eb-* event today.** The panel therefore shows only events ingested
through its own plumbing (`VisionEventStore.record`, in-memory, capped
at 100, labelled "event, not an image", with timestamp, event name,
value, and source). Live robot events will NOT appear until a server
handler forwards robot remote_chat requests into that function — that
forwarding is still to be built and verified, and mocked events are
never presented as live.

**Detector live-delivery gap:** the console builds the correct
execute+subscription payload and records the requested detector state
in the operator session, but it does NOT send it to the robot and does
not claim the robot enabled it. Whether an unsolicited remote_chat
payload reaches a live robot module is unverified; the API response
carries `"live_delivery": "unverified"` and the page says so. Detector
delivery is a separate daylight verification step before any live use.

## Absent capabilities (no fake buttons for any of these)

Camera feed / live video; camera snapshot / photo capture; face
identity / face recognition; joint / head-angle control; wheel driving
/ locomotion; object description / object recognition (show-me
descriptions are operator-supplied text, not a robot vision result);
audio / video storage; autonomous running; live vision-event
forwarding (see above). Each appears on the page only as a plainly
worded "not available" note.

## Page tour

Banner (operator-controlled, not autonomous) -> live status header
(connection, mode, puppet state, battery, firmware — "unavailable"
when the robot has not reported them, never guessed; last
connect/disconnect) -> always-visible Interrupt / Stop + Mark complete
-> Speak (phonetic note) + wake greeting -> Wake / Sleep / Mode
(stock / puppet / bridge, with the leaving-puppet warning; bridge is a
console-session flag only and does not change live bridge routing) ->
session transcript (operator notes + spoken output, clear button,
never written to disk) -> Behaviour browser (searchable; gesture cues,
looks, dances with per-turn opt-in checkbox, 499 gestures, celebration
steps, story beat form, show-me flow) -> Sequence builder (checkboxes
for cues/looks/dances/celebrations + a text field for any step id such
as `gesture:bht_bow`; Run step runs exactly one step; Stop clears) ->
Favourites / Recent (session only, never robot state) -> Vision Events
panel + detector controls -> "Camera feed: not available" section ->
honest absent list -> state timeline note -> robot-filtered log tail
(reusing the existing `logtail` allowlist, staff-gated, read-only) ->
reminder record form.

## Safety + access

- Staff-gated only (`staff_member_required`, admin login): staff 200,
  anonymous 302, non-staff 302 (tested on the test server and offline).
- LAN only: no new public exposure; this is a hive LAN page like the
  rest of the UI.
- CSRF: the new views carry NO csrf-exempt decorator (tested by
  source inspection). Deployment `CsrfViewMiddleware` enforces CSRF on
  every console POST. The page's forms include the CSRF token.
- Interrupt always visible, always winning, including over sequences.
- Puppet auto-exit on disconnect: the page session records `saw_online`
  once the robot is seen online; a later status check that finds the
  robot offline while puppet mode is still set clears `moxie_mode`
  from the device config and reports the auto-exit. Enabling puppet
  while offline is not immediately undone (the stock flow legitimately
  saves the config for pickup on reconnect); the auto-exit fires on an
  observed online -> offline transition.
- No audio/video storage, no transcript logging to disk by default.
  Transcripts, sequences, favourites, recent items, detector requests
  and the bridge flag live in the operator session only. Vision events
  live in the in-memory store only.

## Pre-existing issue flagged separately (NOT fixed or hidden here)

The stock `puppet_api` view in `site/hive/views.py` is `@csrf_exempt`
(pre-existing, and `interact_update` is too). This branch does NOT
reuse that exemption and does NOT expand it — the new console is a
separate staff-gated, CSRF-protected view — but the stock exemption
itself remains, and removing or gating it is a separate follow-up
decision for Stefan (it would break the stock puppet page's current
AJAX flow, which posts without a CSRF token). Do not present the stock
puppet page as CSRF-protected.

## Test results (primary output, worktree venv)

Full suite, run from `site/` with
`~/dev/openmoxie-contrib/.venv-stageb/bin/python` (console module
listed first, because its test settings carry the template/static
config the page-render test needs; later modules reuse the configured
settings, as their own setUpClass guards allow):

```text
python -m unittest hive.test_puppet_console hive.test_muse_transcribe_streaming \
  hive.test_muse_transcribe hive.test_stt_latency hive.test_stt hive.test_muse_spark \
  hive.test_mycroft_bridge hive.test_overnight hive.test_control_commands \
  hive.test_bridge_wiring hive.test_logs_view hive.test_chat_models
Ran 311 tests — OK (skipped=1)
```

The 1 skip is the pre-existing live-timing STT skip (no key). New
module `test_puppet_console`: **32 tests, all passing**, covering
inventory allowlisting (unknown behaviour rejected fail-closed;
motor-test and AUTO_* excluded from the browser), one-at-a-time
enforcement, interrupt preemption, sequence set/step/complete/stop,
staff gating (page + API), mode transitions + disconnect auto-exit,
story/show-me/transcript/favourite rules, vision event ingest and
detector payload shape (including: detector control sends NOTHING to
the robot), bundled-action rejection, no csrf-exempt decorator in the
new module, wired URLs, and a real template render asserting the page
contains the console + vision panel + real inventory items while
containing NO `<video`, `<canvas`, `<img`, `id="camera…`,
`name="drive"`, `name="wheel"`, `name="joint"`, `name="camera"`, or
`camera-feed` element.

## Test-server verification (separate ports/DB/broker, then stopped)

- Copy of this branch built as test-only images
  `openmoxie-puppet-test-server` / `openmoxie-puppet-test-mqtt`,
  containers `openmoxie-puppet-test-server-1` /
  `openmoxie-puppet-test-mqtt-1`, ports **8002/8884** (live 8001/8883
  untouched throughout, verified still up afterwards).
- Fresh test DB (migrate + init_data), one test device row, one test
  superuser (test-only credentials, never recorded here).
- Dashboard on 8002: **200**, and it contains the "Puppet Console" link.
- Console page anonymous: **302** to `/admin/login/`. Staff login:
  console page **200** (123,425 bytes) containing every checked
  marker (operator-controlled banner, Camera feed: not available,
  Vision Events, Behaviour browser, `bht_bow`,
  `bht_dance_cruncher_short_120bpm`, `bht_search`, Interrupt / Stop,
  Sequence builder, Detector controls, Face search, Book / Aruco / QR
  detectors, live-forwarding-not-built note, wheel-driving absent note)
  and containing NO video tag, NO image tag, and NO drive input.
- Staff console API GET: **200** with the status fields. Anonymous API
  GET: **302**.
- Behaviour browser renders with the real inventory (499 gesture
  buttons on the page, not a stub list).
- No live robot was pointed at the test broker (nothing was ever
  configured to connect to 8884 except the test server itself).
- Afterwards: `docker compose down` in the test copy, verified no
  puppet-test containers running, live spark-switch containers still
  up, and the test copy directory removed. The test server no longer
  exists; re-running it means rebuilding from this branch.

## Merge point with mycroft-modern-ui

**Do NOT merge this branch yet.** The modern-UI redesign is a separate
branch, `mycroft-modern-ui` (tip at the time of this work: `39a9b99`
"Modernise OpenMoxie web UI and hosting"; its worktree
`~/dev/openmoxie-modern` was not touched — only read-only `git log`
and `git status` were run against it). This puppet branch is based on
`1f0d63a`, before the modern-UI work. Merge sequence:

1. Let `mycroft-modern-ui` land first (or rebase this branch onto its
   tip once it is settled).
2. Rebase `mycroft-puppet-console` onto `mycroft-modern-ui`, resolving
   template conflicts in `dashboard.html` (the Puppet Console link)
   and restyling `puppet_console.html` to the modern design language.
   The page currently extends the stock `base.html` deliberately so it
   renders standalone now; restyling is a presentation pass only —
   the inventory, one-at-a-time, interrupt, sequence, vision-plumbing
   and safety logic must not change in that pass.
3. Re-run the full suite + test-server verification after the rebase
   before any promotion discussion.

## What is needed before a live flip (all still open)

1. **Full-branch promotion only** — never a file copy into live (the
   Oct 29 file-copy deploy broke the live server; that failure mode is
   not retried). Promotion means building the full branch image and
   deploying it as the live server container, keeping the live DB,
   live Let's Encrypt certs, and live broker untouched, with the old
   live containers kept stopped-but-present for rollback.
2. **Rebase onto mycroft-modern-ui + restyle** (above) and re-verify.
3. **Separate decision on the stock puppet_api csrf exemption**
   (above) — the new console does not depend on it, but the stock
   page's exemption should not be silently inherited as "fine".
4. **Stefan's live daylight test with the robot in view, in this
   order:** speak, gesture, look, interrupt, sleep, dance last (short
   tree only, e.g. `bht_dance_cruncher_short_120bpm`, per-turn opt-in,
   awake only). Dance duration on the robot is unverified (see the
   30s-mark vs 60s-turn note above) — confirm what actually plays
   before any longer dance.
5. **Vision follow-ups before any live vision claim:** (a) build and
   verify server forwarding of eb-* robot events into
   `VisionEventStore.record`; (b) verify detector delivery via the
   remote_chat response path in daylight with the robot in view
   (book/aruco/QR detection + face search, one detector at a time);
   (c) only then may the panel describe events/detectors as live.
   Until then, the panel's "not yet built / unverified" labels stay.
6. **Stefan's explicit go-ahead** for the flip itself. This branch
   alone authorises nothing live.
