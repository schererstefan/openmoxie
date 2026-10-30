# CHECKPOINT — Test OpenMoxie server (mycroft-test-server)

Date: Oct 29, 2026, ~21:21–21:22 PDT. Branch: `mycroft-test-server`
(from `mycroft-bridge-wiring` 631bb01). Fork only; never upstream, never main.
TEST ONLY. This is not a live deploy and proves no live robot behaviour.
All robot behaviour claims from this build are mocked / routing-only.

## What was built

- Test directory (mini): `~/OpenMoxie-test/` — a full copy of the
  `mycroft-bridge-wiring` branch tree (Dockerfile, mqtt.Dockerfile,
  requirements.txt, site/, keys/, content_modules/, doc/), NOT a file-by-file
  copy into a live container. That distinction matters: the previous live
  deploy failed because clone views.py depends on Stage B models.py
  (Spark constants) absent in stock live. A full-branch build has no such
  mismatch — migration 0018 applied cleanly (see below).
- Test compose: `~/OpenMoxie-test/docker-compose.yml`
  (template committed here as `test-server/docker-compose.test.yml`).
  - mqtt: image `openmoxie-test-mqtt:latest`,
    container `openmoxie-test-mqtt-1`, host port **8884:8883**
  - server: image `openmoxie-test-server:latest`,
    container `openmoxie-test-server-1`, host port **8002:8000**
  - Compose project: `openmoxie-test` (network `openmoxie-test_default`).
  - Work dir: `~/OpenMoxie-test/local/work/` mounted to both
    `/mosquitto/log` (mqtt) and `/app/site/work` (server), mirroring live.
  - Test-only image names: a test build can never overwrite the live
    `openmoxie/openmoxie-server:latest` / `openmoxie/openmoxie-mqtt:latest`.
- Test broker certs: the clone's stock self-signed keys/ (repo keys),
  via mqtt.Dockerfile `COPY ./keys`. The live Let's Encrypt keys were
  NOT used and NOT copied into the test build.
- Test DB: `~/OpenMoxie-test/local/work/db.sqlite3`, extracted ONLY from
  `~/OpenMoxie-backup-pre-live-bridge-20261029-2040.tgz` (the pre-live
  backup). The live DB file was never read or written by this task.
  WARNING: that backup copy contains the production config rows
  (external_host, stored provider key row). Keep port 8002 LAN-only,
  never commit the test DB, never publish test URLs beyond the LAN.
  Test DB at verification: 1 device row, firmware
  `v3.6.4-24_12_28-18_42-master-a90cfdee72-v24.10.803-rls-robot`,
  pairing_status `paired` (a copy — the live robot stayed on the live broker).

## Start / stop (mini)

```
cd ~/OpenMoxie-test
docker compose -p openmoxie-test up --build -d   # first build: pip install, a few minutes
docker compose -p openmoxie-test down            # stop (keeps test work dir + DB)
docker ps --format "{{.Names}} {{.Status}}" | grep openmoxie
```

URLs (mini LAN): test dashboard `http://192.168.4.31:8002/hive/dashboard`,
test logs `http://192.168.4.31:8002/hive/logs/` (staff-gated, see below).
Live stays `http://192.168.4.31:8001/hive/dashboard` (8001/8883 untouched).

## Verified, from primary outputs (Oct 29, 2026, 21:21 PDT)

1. Build + start clean, no ImportError:
   `openmoxie-test-server:latest` and `openmoxie-test-mqtt:latest` built,
   both containers Up. Server log: `Applying hive.0018_muse_spark_chat_provider... OK`,
   then `INFO connecting to: mqtt`, `INFO Connected with result code 0`
   (host `mqtt` = the test broker inside the test network, not live).
2. Dashboard: test `GET /hive/dashboard` → **200**; live dashboard → **200**
   in the same check (live containers not restarted by this task:
   live server started 20:43 PDT at the earlier rollback, live mqtt
   since Oct 28).
3. Logs page auth gate: anonymous `GET /hive/logs/` → **302**
   (to admin login); a temporary staff user created in the TEST DB only
   then rendered `GET /hive/logs/` → **200** (8,953 bytes, mentions both
   `debug.log` and `mosquitto.log`), and `GET /hive/logs_api/?lines=50`
   → **200**, showing test startup lines (21:21 timestamps). The temporary
   staff user was deleted afterwards (delete count 1).
4. Test-vs-live logs proof: marker line `TESTSERVER-MARKER-8F3K2Q`
   appended to the TEST debug.log appeared in the test logs_api output
   (filtered query), was absent from the live debug.log (count 0), and
   was then removed from the test debug.log (count 0 after removal).
5. Bridge wiring in the test context: inside the test container,
   `WiredBridge` / `BridgeController` import OK, and the full suite
   `hive.test_mycroft_bridge hive.test_overnight hive.test_control_commands
   hive.test_bridge_wiring hive.test_logs_view` →
   **Ran 135 tests in 0.330s — OK** (same 135 as the clone venv run).
6. Test work dir after startup: `db.sqlite3` (188K, migration 0018
   applied to the test copy only), `debug.log` (1.4K),
   `mosquitto.log` (699B, test broker log).

## Deliberately NOT done

- No live touch: no `docker cp` into live containers, no live restart,
  no live DB/cert edit, no live robot command, no robot re-pointing.
  The robot stayed on the live broker (8001/8883) throughout.
- No Puppet API wiring: the bridge wiring is pure translation only
  (no new endpoint, no MQTT publishing from the bridge), per the
  local_moxie skill — bridge control stays out of Puppet until its
  authentication decision is made.
- No wheel driving, camera feed, face identity, or joint control claims.
  The 135 tests prove routing / translation only, mocked.
- No secrets committed: test compose template and this checkpoint
  contain no keys, passwords, or tokens. The temporary test staff
  password was used only for this verification and the user was deleted;
  it is intentionally not recorded here.

## Remaining decisions before ANY live deploy

- Auth for any future bridge serving path (blocked on the vuln-audit
  auth decisions); the logs page's staff gate (`/admin/login/`) is the
  only gate verified so far, and only on the test server.
- Full-branch live rebuild procedure: build the complete branch image
  (never file-by-file copy), deploy to a fresh live container from a
  backup, verify dashboard + logs + robot reconnect, with a rollback
  plan (restore stock image/container + backup DB/certs) — the rollback
  used after the failed copy deploy is the template, not the procedure.
- Daylight live-test protocol with Stefan in view, one step at a time:
  wake greeting, one chat turn, one gesture, interrupt, sleep, bridge
  disable (stock restored). Dance last, short tree only. No live tests
  while Stefan is asleep.
- Whether the test server stays running or is stopped (`down`) when
  not in use; it is currently left running for inspection.
- Test DB hygiene: the test DB is a backup copy with production config
  rows — decide whether to scrub provider keys from future test DB
  copies, and never let a test DB leak into a commit or a public URL.
