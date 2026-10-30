# Checkpoint: Mycroft bridge -> Puppet control wiring (clone only)

Branch: `mycroft-bridge-wiring`, from `mycroft-bridge-full` (52a6cfc).
All work is in the isolated clone `/Users/stefan/dev/openmoxie-contrib`.
Nothing was deployed, published to a broker, flashed, or sent to the
robot (mid-OTA 801->803). The running install at `~/OpenMoxie` was
never touched. All validation is mocked/offline.

## What was wired

A new pure translation layer, `site/hive/mqtt/bridge_control_wiring.py`,
turns `BridgeController` turn outputs into control-action dicts in the
existing Puppet control shapes (`hive.mqtt.control_commands`): gesture
and look become one validated `cmd:behaviour-tree` mark (telehealth),
sleep becomes the existing `build_sleep_payload()` remote_chat payload,
show-me / celebrate / story become single stepped actions, dance
becomes one allowlisted behaviour-tree dance action, wake becomes a
wake action dict, and interrupt becomes the interrupt action.

Entry points:

- `WiredBridge` wraps a `BridgeController` and exposes `handle_turn`,
  `wake`, `interrupt`, `enable`, `disable`, `set_robot_state` with the
  bridge signatures (plus per-turn `allow_dance`). Every returned turn
  carries `control_actions`.
- `turn_to_control_actions(turn, robot_state, allow_dance)` is the pure
  translator: refusals return `([], explicit error)`, success returns
  `([one action], None)` or `([], None)` for pure speech.
- `gesture_action_for_cue(cue)` and `validate_turn_actions(turn)` are
  the fail-closed helpers the tests exercise directly.

`site/hive/mqtt/mycroft_bridge.py` was extended additively only: every
turn now carries an `intent` tag (interrupt, sleep, dance, gesture,
celebrate, show_me, story, tool, refusal, chat, stock, wake) so the
wiring layer can label actions unambiguously; the wake gate checks were
extracted to `_check_wake_allowed()` (no behaviour change); a new
publish-free `wake_turn()` returns the greeting without touching the
transport; and `control_actions_for()` delegates to the wiring layer.
No routing logic, allowlist, or gate in the bridge was changed, and all
74 pre-existing tests pass unmodified.

## One-action-per-turn contract

Every wired turn carries `control_actions`, a list of length 0 or 1.
A turn selecting gesture, dance, show-me, celebrate, story, sleep,
wake, or interrupt yields exactly one action. Speech accompanying an
action is a field of that action, never a second action; a nested
gesture turn on a chat or story turn collapses into the single action.
Pure speech turns (chat, read-only tools, refusals, agent-absent) yield
zero actions. There is no queue type anywhere in this layer, and
`validate_turn_actions` rejects any turn with more than one action,
more than one movement command, an unknown action kind, or any media
key/byte payload. Action kinds are a closed set: gesture, look, dance,
show_me, celebrate, story, sleep, wake, interrupt.

## Gates preserved

- Gesture: behaviours are re-validated against the manifest allowlist
  (`GESTURE_BEHAVIOURS` / `LOOK_BEHAVIOURS`) at the wiring boundary and
  markup is rebuilt via `build_gesture_markup` / `build_look_markup`,
  never trusted from the turn. Unknown trees or cues fail closed: zero
  actions plus an explicit `error` in the turn, never a guessed tree.
- Dance: gated OFF by default. `control_commands.ALLOW_DANCE_MODULE`
  stays `False` and this layer never flips it and never builds the
  remote_chat DANCE launch payload. A dance turn produces no action
  (spoken refusal + explicit error, dance lock untouched) unless the
  caller passes `allow_dance=True` for that single turn; the opt-in
  does not stick. When opted in, the bridge still enforces awake-only
  (sleeping raises `ValueError`), the tree must be in `DANCE_TREES`,
  duration is clamped to 10-60s via `clamp_dance_duration`, one dance
  at a time, interrupt releases it.
- Show-me: session protocol idle -> awaiting -> received -> described
  (wiring phases `awaiting` / `described`). No image/video/audio/disk
  storage anywhere: actions are built only from allowlisted fields,
  only the text description token crosses the boundary,
  `stored_on_disk` is always `False`, and byte payloads or media keys
  smuggled onto a turn never reach an action. Tests assert a show-me
  flow creates no files in a temp dir and the session holds only
  `state` + `description`.
- Interrupt: a `stop`/`interrupt` word anywhere in the turn forces the
  single interrupt action and clears gesture, dance, show-me, and
  celebration sessions, winning over gesture, dance, show-me, and
  sleep in the same turn. (Safety bias: an ambiguous "do not stop"
  still interrupts; interrupting only clears sessions, it never moves.)
- Sleep/wake: wired wake uses the bridge MQTT gate — sleeping returns
  a wake action and the my-croft greeting, standby/unknown raise
  `ValueError`, bridge-off raises `ValueError` — and returns an action
  dict without touching the transport. The legacy `wake()` (with its
  injected `MockTransport` in tests) is unchanged.
- Mode gate: unchanged. Default STOCK (wired turns are the stock turn
  with zero actions), `enable()` is bridge takeover, `disable()`
  restores stock exactly and clears all sessions.
- No silent stock fallback: no agent + no explicit
  `allow_stock_fallback=True` yields the spoken "bridge agent not
  connected" turn with zero actions, never a stock chat turn.
- Action intents (send email, create event, lights, drive, record)
  remain spoken refusals with zero control actions.
- Celebrate/story: one step / one beat per turn, stepped manually by
  subsequent turns, never auto-chained.

## Deliberately NOT wired

- No new unauthenticated HTTP endpoint, and no change to the running
  server's `puppet_api` wiring in `site/hive/views.py`. `puppet_api`
  is unauthenticated (`@csrf_exempt`, no auth decorator); expanding it
  or adding a bridge endpoint while the vuln-audit auth decisions are
  pending would widen that surface. The wiring therefore stops at
  validated action dicts; a future live step would feed those dicts to
  the existing `MoxieServer` send methods behind whatever auth is
  decided, not through a new route added here.
- No MQTT publishing from the bridge or the wiring layer. The wiring
  never calls `MoxieServer`, `get_instance()`, or the bridge
  transport; tests assert `MockTransport.calls == []` across a full
  wired sequence (wake, gesture, dance, show-me, celebrate, story,
  sleep, interrupt).
- No widening of the Puppet API surface: no new commands, no new
  parameters, no changes to `control_commands.py` at all.
- No live robot command of any kind (robot mid-OTA). No deploy, no
  merge. Fake agent and fake read-only tools only, as before.

Known pre-existing quirks observed, left unchanged: behaviour-tree
markup duration is clamped to 30s by `parse_duration` while the dance
session duration allows 10-60s (same in `dance_modules` today); the
bridge routes any transcript containing "dance" before show-me receive,
so a shown object described with the word "dance" would route to the
dance gate (now a refused turn by default) instead of show-me.

## Test results

Command (from `site/`, clone venv):

    ../.venv-stageb/bin/python -m unittest hive.test_mycroft_bridge hive.test_overnight hive.test_control_commands hive.test_bridge_wiring

Result: **Ran 119 tests, OK** (74 pre-existing + 45 new in
`site/hive/test_bridge_wiring.py`). New coverage: one action per turn
for each action kind, dance gated off by default and awake-only /
clamped / single when opted in, gesture allowlist fail-closed
(including forged-markup rebuild), show-me no-storage (temp-dir empty,
session holds only state + description token, forged media stripped),
interrupt wins over dance/gesture/show-me/sleep, mode gate
enable/disable with session clearing, wake gating with no publish,
no silent fallback, refusal intents with zero actions, celebrate/story
single-stepping, and a full wired sequence publishing nothing.

## Stefan decisions still needed before any live deploy

1. Auth for bridge endpoints: how (if ever) wired action dicts may
   reach `MoxieServer` — reuse puppet_api behind new auth, a separate
   authenticated route, or in-process only. Blocked on vuln-audit
   auth decisions; nothing here assumes an answer.
2. Real agent adapter: `MycroftAgent.answer` is still an interface
   with only the fake implementation. Choosing and wiring the real
   Mycroft backend (endpoint, credentials handling) is a daylight step.
3. Transcript logging on/off: this layer logs nothing. Decide whether
   bridge turns/transcripts may be logged at the serving layer, and
   retention, before any live run.
4. Dance opt-in default: per-turn `allow_dance=True` is the only way a
   dance action is produced today. Decide whether that stays the live
   posture, becomes a config flag, or stays test-only; the Stage B
   Unity-crash caution behind `ALLOW_DANCE_MODULE=False` is unchanged.
5. Live test protocol: daylight, robot in view, post-OTA-803, one
   action at a time — to be scheduled separately. This checkpoint
   claims no live robot behaviour.
