# Full Mycroft Bridge — Checkpoint (built overnight, test later)

- Repository: `/Users/stefan/dev/openmoxie-contrib`, branch `mycroft-bridge-full`,
  base `mycroft-overnight` commit `088628d` (84 tests).
- State: bridge branch only. NOTHING pushed/merged to main, NOTHING pushed
  to `jbeghtol/openmoxie`. Pushed to Stefan's fork `schererstefan/openmoxie`
  only, after green tests + secret scan (below).
- Hard fences kept: running `~/OpenMoxie` NEVER touched (no containers, DB,
  broker, certs, ports 8001/8883). NO live command sent to the real robot:
  no wake, speak, gesture, dance, camera. NO API key, password, Wi-Fi
  password, or token in any file; tests use fake strings only.
  `puppet_api` in `views.py` was NOT wired to the bridge tonight — that is
  a daylight, reviewed step (see Stefan decisions below), for the same
  reason as overnight: do not expand an unauthenticated web API surface
  while Stefan is asleep and before the vuln audit is reviewed.

## 1. Architecture (text diagram)

    Moxie STT transcript + wake/sleep state
        |
        v
    BridgeController (site/hive/mqtt/mycroft_bridge.py)
      mode gate: STOCK (default) / BRIDGE (explicit enable only)
        |
        +-- wake()      -> my croft greeting, MQTT-connected only
        +-- handle_turn -> intent router, first match wins:
        |     stop/interrupt -> interrupt (clears all)
        |     sleep/goodnight -> sleep turn, state=sleeping
        |     dance cue -> DanceSession (awake-only, one at a time)
        |     gesture cue -> GestureConversation (allowlisted tree)
        |     celebrate -> celebration routine, one step per turn
        |     show me -> ShowMeSession (no image/audio/disk storage)
        |     story -> one creative story beat (no auto-advance)
        |     calendar/weather -> ToolRouter, read-only only
        |     action intent -> REFUSED spoken turn, never executed
        |     default chat -> injected MycroftAgent answer, or an
        |       explicit "no agent connected" turn (never silent GPT)
        +-- proactive_turn -> raises unless Stefan-set schedule passed
        +-- interrupt() -> clears gesture/dance/show-me/celebration
        |
        v
    ONE robot-bound turn dict (command, speech, mood, intensity,
    behaviour/markup, transport) -> caller publishes via EXISTING
    MoxieServer methods only (telehealth PLAY_OUTPUT, remote_chat,
    send_wakeup_to_bot). This module publishes NOTHING itself;
    MockTransport in tests records calls instead.

## 2. Files

- NEW `site/hive/mqtt/mycroft_bridge.py` (~330 lines): BridgeController,
  MycroftAgent interface, FakeMycroftAgent, ToolRouter, MockTransport,
  mode/state constants, routing, phonetic + sanitise at the boundary.
- NEW `site/hive/test_mycroft_bridge.py` (26 tests).
- NEW `CHECKPOINT-BRIDGE-FULL.md` (this file).
- Reused, NOT modified: `control_commands.py` (allowlist, phonetic),
  `gesture_conversation.py` (gesture turns, sanitize_speech),
  `dance_modules.py` (DanceSession, 23 trees, 10-60s),
  `creative_modes.py` (wake greeting, story beat, celebration),
  `show_me.py` (show-me session, no storage),
  `conversations.py` / `ai_factory.py` (GPT/Spark chat paths untouched;
  bridge is a separate, flag-gated path, not a replacement of them).

## 3. Routing decisions (explicit, contestable)

1. Bridge OFF = stock, exactly. Bridge ON = full takeover on wake:
   wake greeting is my croft for Stefan, NOT the stock welcome
   ("Wow, it feels good to be out of that box") and NOT the config
   child nickname "Pat". Disable() restores stock and clears sessions.
2. Stock GPT is fallback ONLY when `allow_stock_fallback=True` is passed
   explicitly to handle_turn(). Without it, and with no agent connected,
   the robot says the bridge agent is not connected — it does not
   silently answer as stock Moxie. This is deliberate: silent fallback
   would hide a broken bridge as a working Moxie.
3. Wake is MQTT-gated, matching the live-proven Oct 28 result:
   sleeping (connected) -> wake succeeds; standby/unknown (disconnected)
   -> ValueError, no publish. Local voice/touch/power or a pre-set
   alarm timer is the only standby wake.
4. Tools are read-only only: calendar, weather, timer_record,
   show_me_status. ToolRouter rejects anything else, including
   send_email. Action intents in speech (mail, create event, lights,
   drive, record video/photo, call) return a refusal turn, never a
   queued action. No wheel driving, no house control, no camera
   recording — no such API exists in this codebase and none was invented.
5. One movement per turn, no autonomous loops, no proactive speech
   without a Stefan-set schedule record. Interrupt always wins and
   clears dance (releases the lock), gesture, show-me, celebration.
6. Speech: sanitised (angle-bracket tags stripped, 500 chars) then
   phonetic-rendered ("Mycroft" -> "my croft", never "my craft").
   Note: this means the literal string "Mycroft agent" in a bridge
   message is spoken as "my croft agent" — intended, not a bug.

## 4. What is mocked vs real

- REAL (code-verified, reused): allowlist loading from
  `doc/AssetBundleMasterManifest.csv`, gesture/dance/celebration/
  show-me shaping, mood/intensity validation, sanitise + phonetic.
- MOCKED (tests only, must be replaced by daylight wiring): the
  Mycroft agent (FakeMycroftAgent, canned answers), read-only tools
  (lambdas returning canned calendar/weather), the transport
  (MockTransport records wakeup/turn calls, sends nothing), the
  device id ("mock-device"), and STT transcripts (replayed strings).
- NOT BUILT (deliberately, see decisions): a real Mycroft agent adapter
  (needs Mycroft's own API/process contract, which is not in this
  clone), real calendar/weather tool backends, puppet_api wiring,
  deployment to ~/OpenMoxie, any robot-side change.

## 5. Tests — verified by re-running, green

Command, from `site/` with the clone venv:
`../.venv-stageb/bin/python -m unittest hive.test_chat_models hive.test_stt hive.test_muse_spark hive.test_control_commands hive.test_overnight hive.test_mycroft_bridge`

Result: Ran 110 tests in 0.161s — OK. 110 pass / 0 fail / 0 errors.
- 8 chat models + 6 STT (existing, unmodified) + 22 Spark + 25 control
  (Stage B) + 23 overnight + 26 bridge (mode, wake sleeping/standby/
  unknown/off, chat phonetic, gesture, dance awake-only + second-dance
  rejection + interrupt release, celebration steps, story beat,
  show-me no-storage flow, sleep, calendar/weather read-only, unknown
  tool rejection, action-intent refusals, no-agent explicit turn,
  explicit-fallback-only, markup stripping, proactive gate, identity
  Stefan-not-Pat, full replayed transcript sequence).
- `git diff --check` clean. Secret scan (grep for token/key patterns in
  the two new files): no secrets found.

## 6. What is LIVE-UNPROVEN — do not claim otherwise

Every bridge behaviour: wake greeting replacing stock welcome, gesture
turns, dance trees, show-me sessions, story/celebration, tool answers,
interrupt timing, and the bridge-to-MoxieServer transport wiring.
Mocked tests prove only routing, validation, session state, gating,
and shaping. The two previously live-proven facts (Puppet speak after
READY; wake from sleep-idle, Oct 28) do not transfer to the bridge.

## 7. Daylight test order (Stefan in view, one step at a time)

1. Review this checkpoint + overnight vuln audit; decide the audit
   items that gate wiring (admin auth, superuser creation, MQTT auth/
   re-enrolment, key encryption, exec removal) before any API wiring.
2. Wire BridgeController into puppet_api / a dedicated bridge endpoint
   in daylight, reviewed, with auth (see decisions) — NOT the
   unauthenticated surface as-is. Deploy to a test server copy first,
   NOT the running ~/OpenMoxie until that copy passes.
3. Live, robot in view: status -> bridge enable -> wake greeting
   (sleep-idle) -> one chat turn (fake agent first, then real agent
   adapter) -> gesture turn (bht_arms_out) -> read-only tool turn
   (calendar or weather) -> interrupt -> sleep -> bridge disable
   (stock restored, stock welcome returns).
4. Only after 3 passes: dance last (short tree, interrupt ready),
   celebration steps, show-me Phase A (QR token only, no storage).
5. Only then: consider deploying the bridge branch to ~/OpenMoxie,
   with backup + rollback, on Stefan's explicit go-ahead.

## 8. What needs Stefan decisions before wiring/deployment

1. Real Mycroft agent adapter: what process/API on the mini answers a
   turn, with what latency budget (volley timing), and what memory/
   tool scope it may use from Moxie (read-only first, per plan).
2. Auth for any bridge endpoint: puppet_api is currently unauthenticated
   and CSRF-exempt (overnight Finding 1/3). Wiring the bridge into it
   without auth would expand that surface — decide auth first.
3. Real read-only tool backends (calendar, weather): which sources,
   and whether transcripts may be logged (proposed: off by default).
4. Whether stock fallback should ever be automatic (current answer:
   no, explicit only) and whether the contributor Spark variant stays
   exposed (Stage B question, still open).
5. Deployment target and rollback: test server copy vs ~/OpenMoxie,
   and whether the bridge branch should become a PR to upstream
   (fork branch exists; no PR opened).

End of bridge checkpoint. Next step is Stefan's daylight review.
