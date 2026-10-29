# Overnight Checkpoint — Moxie Upgrades (Stefan asleep, no live contact)

- Repository: `/Users/stefan/dev/openmoxie-contrib`, branch `mycroft-overnight`,
  base `mycroft-spark-control` commit `7f2b3a5` (Stage B, 61 tests).
- State: work below is on the overnight branch only. Nothing was pushed or
  merged to main, nothing was pushed to `jbeghtol/openmoxie` (pull only).
  Running `~/OpenMoxie` was never touched: no containers, DB, broker,
  certs, or ports 8001/8883. NO live command was sent to the real robot
  overnight: no wake, speak, gesture, dance, or camera activation.
- No API key, password, Wi-Fi password, or token value appears in any file.
  Tests use fake strings only (existing Stage B fakes; no new secrets).
- All validation is mocked/offline. Everything new is LIVE-UNPROVEN.

## 1. What was built

### Gesture-based conversations — site/hive/mqtt/gesture_conversation.py
- One turn at a time, Stefan-initiated, no autonomous loops.
- Cues: wave, hello, arms out, celebrate, look, search, sleep.
  Each maps to exactly one allowlisted behaviour tree (or look default)
  plus speech, mood, intensity. Unknown cues rejected; dance cues are
  refused here and must go through dance_modules.
- Speech is phonetic-rendered ("my croft") and sanitised (see §5):
  markup/action tags stripped, capped at 500 chars.
- `GestureConversation` session: turn counting only, interrupt clears it.
- Tests: `site/hive/test_overnight.py`, GestureConversationTests + SanitiseTests.

### Dance modules — site/hive/mqtt/dance_modules.py
- Dance is now enabled in code under a registry, not by blindly flipping
  Stage B's `ALLOW_DANCE_MODULE=False` (that constant in
  control_commands.py is unchanged; the overnight path is separate).
- Registry: 23 trees from the manifest (`bht_dance_*` +
  `bht_celebration_dance_01-04`), default `bht_dance_cruncher_short_120bpm`.
- Duration clamped 10-60 s, default 30 s. `DanceSession`: one dance at a
  time, non-blocking acquire, second dance rejected (never queued),
  interrupt releases the lock. Dance requires `robot_state="awake"` —
  no dance while asleep/standby.
- The existing remote_chat DANCE module launch (Stage B) is unchanged.
  Behaviour-tree dance is the more precisely allowlisted path.
- NO autonomous dancing. Daytime-only live-test gate is in §6, not a
  clock check in code (server time alone cannot prove Stefan is present).

### Camera / observe the world — VERDICT: NO capture path exists
- Read-only audit in the clone, verified from primary code (see §4).
- What WAS built: `site/hive/mqtt/show_me.py`, a session protocol with
  NO image, NO video, NO audio, NO disk storage. States: idle ->
  awaiting_show -> received -> described. Stefan initiates, robot does a
  look behaviour and asks to be shown something, the user supplies a
  description (by voice/STT in a future live path), robot acknowledges.
  Cancel/interrupt resets the session.
- What was NOT built: any camera feed, snapshot, face identity, or
  object description — none exists in code to wire up, and inventing one
  would be a fake. See §4 for the real, token-based show-me design.

### Creative features — site/hive/mqtt/creative_modes.py
- Wake greeting: `build_wake_greeting()` returns a my croft greeting for
  Stefan (not the config child nickname, not the stock welcome
  "Wow, it feels good to be out of that box"). The caller must choose
  to use it; nothing here fires on wake by itself.
- Storytelling: `build_story_beat()` — one beat per call, speech + mood
  + optional gesture turn, no auto-advance.
- Celebration routine: `build_celebration_routine()` returns 3 gesture
  turns (celebrate, arms out, look) as a list the caller steps through
  one at a time; it never plays anything itself.
- Reminder: `build_reminder()` is a record only — no timer, no publish,
  no autonomous delivery. Delivery is a later, approved step.
- No wheel driving was added (no API exists, none invented). No
  autonomous house control. No continuous recording.

### API wiring — DELIBERATELY NOT DONE overnight
- The new modules are shaping-only and are NOT wired into `puppet_api`
  in views.py. Expanding an unauthenticated web API surface while Stefan
  is asleep, and before the vulnerability audit below is reviewed, would
  have been the wrong call. API wiring is a daylight, Stefan-reviewed
  step (§6).

## 2. Tests — verified by the coordinator, re-run, green

Command, from `site/` with the clone venv:
`../.venv-stageb/bin/python -m unittest hive.test_chat_models hive.test_stt hive.test_muse_spark hive.test_control_commands hive.test_overnight`

Result: Ran 84 tests in 0.164s — OK. 84 pass / 0 fail / 0 errors.
- hive.test_chat_models: 8 pass (existing, unmodified)
- hive.test_stt: 6 pass (existing, unmodified)
- hive.test_muse_spark: 22 pass (Stage B)
- hive.test_control_commands: 25 pass (Stage B)
- hive.test_overnight: 23 pass (21 new overnight + 2 sanitise tests)
No red gate. Existing suites were not edited.

## 3. Files changed (overnight branch, vs mycroft-spark-control)
- New: site/hive/mqtt/gesture_conversation.py
- New: site/hive/mqtt/dance_modules.py
- New: site/hive/mqtt/creative_modes.py
- New: site/hive/mqtt/show_me.py
- New: site/hive/test_overnight.py
- New: CHECKPOINT-OVERNIGHT.md (this file)
- Modified (safe fix only, §5): gesture_conversation.py sanitise helper.

## 4. Camera verdict, with code evidence
- NO video stream, NO snapshot/image endpoint, NO image payload,
  NO face-identity or object-description API in server code.
  control_commands.py:7 and :45 state this explicitly ("no camera, or
  face-recognition command here"; "Look = face-search behaviour trees
  only").
- The only `image` hits in server code are the reverse direction: the
  server renders QR PNGs for Moxie to scan (views.py endpoint_qr,
  wifi_qr). No robot-to-server image ever arrives.
- URLs (site/hive/urls.py): setup, dashboard, QR, face customisation,
  puppet, import/export only. "Face" in the UI is avatar customisation
  (child_pii.face_options), not recognition.
- Inbound channels that DO exist (moxie_server.py:137-141, 198-285):
  remote-chat, client-service-activity-log, zmq bridge (only STT handler
  registered, moxie_server.py:287-293, 544), device-logs, state.
  STT is the only perception stream (zmq_stt_handler.py, audio only).
- Documented but NOT shipped as a content module (doc/RemoteModuleAPI.md):
  on-robot visual detectors via generic execute + event_subscription:
  eb_start_binned_face_search / eb_custom_face_search -> eb-found-face;
  eb_enable_book -> eb-br-event + $eb_br_value (trained book covers);
  eb_enable_draw -> eb-dr-event + $eb_dr_value (ArUco);
  eb_enable_qr -> eb-qr-event + $eb_qr_value. Working precedent is QR
  only (content_modules/MoxieGo.json). No book/ArUco/face module exists
  in content_modules/.
- Privacy-safe show-me design (existing channels only, future live path):
  Stefan-initiated SHOWME module only; look behaviour, then arm ONE
  detector at a time; one-shot + 60 s server timeout + exit_module on
  expiry/lost-face/other speech; on detection, speak back only the
  token/label or "I see someone there"; Stefan may add a spoken
  description via STT; store nothing by default, only with explicit
  per-session approval; interrupt/disable/sleep ends the session and
  clears subscriptions. No continuous recording, no camera-on state
  persists after exit.
- FUTURE camera-capture path (does NOT exist, do not build on it as if
  it did): would need a new robot-side perception proto, a new ZMQ
  subscription + handler alongside STT, a new topic, and a server
  snapshot endpoint, plus a fresh consent gate (Stefan-initiated,
  single frame, in-memory only, explicit keep-or-discard, no retention
  by default). 801 firmware camera stack is outside this clone, so
  feasibility cannot be verified from here.

## 5. Vulnerability audit — code only, NOT a live-exploit claim
The live setup itself warns it has "minimal security" and must stay
LAN/firewall-protected. Core conclusion: the design is LAN-trust, not
Internet-trust. Never expose 8001/8883 beyond the LAN; keep Moxie on a
segregated/IoT SSID if possible; any future remote access is a new
authenticated design, not a port-forward.

Findings (severity, file:line, fix):
1. Critical — Unauthenticated web control. No login_required anywhere;
   urls.py routes setup/dashboard/puppet/wake/mission/export/import/QR
   with no auth. views.py:66 hive_configure, :276 puppet_api, :397
   moxie_wake, :420 export_data, :448/:468 import. Fix: login_required
   (or LAN + token gate) on all hive views; Stefan must provision admin
   credentials before switch-on.
2. Critical — Unauthenticated superuser creation. views.py:96-103:
   hive_configure POST creates a superuser if none exists. Fix: remove
   auto-creation; createsuperuser on first local setup only. Stefan
   decision on setup flow.
3. Critical — CSRF-exempt robot control. views.py:137 interact_update,
   :275 puppet_api are @csrf_exempt, no auth. Fix: remove csrf_exempt,
   require auth + CSRF + origin/referer check. Safe to code.
4. Critical — MQTT anonymous, no ACLs, spoofable devices/commands.
   site/data/openmoxie.conf: listener 8883, allow_anonymous true, no
   acl_file. Server subscribes moxie_server.py:137-141, publishes
   :409,413,418 with no per-device auth; MoxieDevice.permit
   (models.py:55,60) never checked. Fix: Mosquitto user/password +
   ACLs, enforce permit, rotate credentials. Migration: new Migration
   QRs + re-enrolment. Stefan decision.
5. Critical — Plaintext keys in SQLite + key sharing to devices.
   models.py:85-98 openai/muse_spark/google keys plaintext; settings.py
   MQTT_ENDPOINT cert_required False; moxie_server.py:230-238 sends
   Google service-account key to any device asking license when
   _SHARE_GOOGLE_KEY=True. Fix short term: file permissions, no DB
   backups to shared storage, least-privilege/scoped keys. Long term:
   encrypt keys at rest with migration + re-entry. Stefan decision.
6. High — Django dev settings shipped as-is. settings.py: hardcoded
   django-insecure SECRET_KEY (value NOT reproduced here), DEBUG=True,
   ALLOWED_HOSTS=['*'], INTERNAL_IPS includes 0.0.0.0, debug toolbar
   served. Fix: SECRET_KEY from environment, DEBUG=False, restrict
   ALLOWED_HOSTS, remove toolbar from non-dev runs, secure cookies.
7. High — Server-side code execution via imported chat code.
   conversations.py:369 exec(source.code, ...); data_import.py applies
   unfiltered __dict__.update to conversations, reachable via
   unauthenticated import. Fix: remove/sandbox exec, strip code from
   importable fields, admin-only import. Check first whether any live
   conversation uses code.
8. High — Speech/markup injection (unproven on robot, present in code).
   Puppet speak has no length cap; gesture speech concatenated raw after
   the behaviour mark (moxie_server.py:436-445); volley.py:86-106 turns
   <launch>/<sleep>/<exit> tags in response text into robot actions.
   No shell injection found on the speech path. Fix: escape/strip
   markup, cap speech length, honour action tags only from trusted
   authored content. SAFE FIX IMPLEMENTED OVERNIGHT, new modules only:
   sanitize_speech() in gesture_conversation.py strips angle-bracket
   tags and caps speech at 500 chars, tested in test_overnight.py.
   Core views.py paths were NOT changed overnight.
9. Medium — Dance/gesture abuse by repetition. control_commands.py
   duration clamped 0.1-30 s, single mark repeat:1, one movement per
   POST, but no rate limit/cooldown; interrupt covers telehealth only.
   Fix: per-device rate limiting, a "stop all" cancelling remote_chat
   actions, dance/session timeout.
10. Medium — Unauthenticated QR disclosure. views.py:162 endpoint_qr
    (Migration QR), :175 wifi_qr (caller-supplied SSID/password), no
    auth. Fix: gate both behind auth; never render Wi-Fi QR to an
    unauthenticated session.
11. Medium — Transcript / child-PII logging and exposure.
    zmq_stt_handler.py:72 logs STT-FINAL transcripts; moxie_remote_chat.py
    logs speech; hive logger DEBUG to 30-day rotating file; child
    nickname/face options/full robot_config shown in UI. Fix: redact
    transcripts by default (opt-in debug only), minimise logs, avoid
    echoing full robot_config to UI.
12. Low — Weak input handling / dependency drift. OpenAI/Google key
    inputs type="text" in setup.html vs Muse key type="password";
    requirements.txt mostly unpinned; export_data puts caller
    content_name in Content-Disposition; face-edit asset_* values not
    allowlisted. Fix: password inputs, pin + audit dependencies,
    sanitise export filename, allowlist face assets.

Positive findings preserved: gesture allowlist fails closed; mood/
intensity validated; one command per Puppet POST; dance disabled by
default in Stage B; MQTT publishes use no retain; no camera/photo
storage found; LOG_WAV=False; keys not logged in ai_factory.py; setup
template shows key-presence booleans, not values.

Safe now vs Stefan decision: safe to code — Finding 3 (auth+CSRF),
8 (speech escaping/length cap, partially done overnight), 11 (log
redaction), 12 (input types, pinning, filename sanitisation), rate
limits. Needs migration/decision — Findings 1 (admin provisioning),
2, 4 (re-enrolment), 5 (key encryption + re-entry), 7 (exec removal),
9 (dance semantics), any remote access.

## 6. What is LIVE-UNPROVEN — do not claim otherwise
Gesture execution, look trees, dance trees, dance module launch,
show-me sessions (including book/ArUco/face detectors — doc-only,
unvalidated on this robot), wake greeting replacing the stock welcome,
storytelling beats, celebration routine, reminders, Spark turns from
Stage B, and the new puppet_api wiring (not built). Mocked tests prove
only shaping, validation, registry, session state, and sanitisation.

## 7. Daylight test order (Stefan in view, one command at a time)
1. Review this checkpoint + vulnerability findings; decide Findings
   1, 2, 4, 5, 7 (admin, MQTT auth, key encryption, exec) before any
   API wiring or deployment.
2. Wire gesture_turn / dance_tree into puppet_api (daylight, reviewed),
   deploy to a test server copy first — NOT the running ~/OpenMoxie
   until that copy passes.
3. Live, robot in view: status -> wake greeting -> look -> one harmless
   gesture (bht_arms_out) -> celebration routine step by step ->
   dance tree short (bht_dance_cruncher_short_120bpm) last, interrupt
   ready between every step.
4. Show-me Phase A only: QR card token, look + eb-qr-event, voice
   confirmation, no storage. Phase B (book/ArUco/face) only after
   Phase A passes, one detector at a time.
5. Spark live turn (Stage B) separately, with Stefan entering the key
   in the running setup page himself.
6. Only then: consider deploying the overnight branch to ~/OpenMoxie,
   with a backup + rollback, and only on Stefan's explicit go-ahead.

## 8. What was NOT done, and why
- No puppet_api wiring (see §1): unauthenticated surface + asleep Stefan.
- No core views/settings/MQTT/auth changes (Findings 1-7): those need
  Stefan decisions / admin provisioning / re-enrolment, not silent
  overnight edits to a working robot.
- No live robot test, no camera activation, no deployment to
  ~/OpenMoxie, no upstream PR, no fork PR (Stefan did not ask for one).
- No wheel driving, no autonomous gestures/dances, no continuous
  recording, no image storage, no house control.

End of overnight checkpoint. Next step is Stefan's daylight review.
