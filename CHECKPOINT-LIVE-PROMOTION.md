# Checkpoint — Live promotion: test-server code becomes the real server

Date: Thu Oct 29, 2026, ~21:33 PDT. Robot: firmware v24.10.803, paired, sleep mode.
No robot commands, no QR, no re-pair, no wake (robot was MQTT-disconnected, so server wake was not applicable).

## What was promoted, and what was NOT
- PROMOTED: the test-server code/image only — openmoxie-test-server:latest
  (branch mycroft-test-server, commit e80bfe4, from mycroft-bridge-wiring 631bb01),
  full branch tree including Stage B models.py, so the earlier file-copy ImportError
  (clone views.py needing Spark constants absent in stock live) cannot recur.
- NOT copied: test DB (backup copy from 20:40) — live DB
  ~/OpenMoxie/local/work/db.sqlite3 stays authoritative.
- NOT copied: test broker stock self-signed certs — live broker keeps live
  Lets Encrypt certs in ~/OpenMoxie/mosquitto-keys/ for moxie.ozoneretire.com
  (CN verified, issuer LE YE2, notAfter Jan 27, 2027).

## Backup (Step 1, verified)
- ~/OpenMoxie-backup-pre-promotion-20261029-2130.tgz (14K), contains:
  local/work/db.sqlite3, mosquitto-keys/, docker-compose.yml,
  docker-compose.override.yml, mosquitto-debug.conf. Tar listing verified.
- Pre-promotion live state recorded: containers openmoxie-server-1 /
  openmoxie-mqtt-1 (stock images openmoxie/openmoxie-server:latest,
  openmoxie/openmoxie-mqtt:latest), ports 8001/8883, live DB migrations
  up to 0017, firmware 803, pairing_status paired, mode sleep, battery 0.88.

## Promotion (Steps 2-3)
- Old live containers openmoxie-server-1 / openmoxie-mqtt-1 STOPPED, not deleted,
  kept for rollback. Test containers openmoxie-test-server-1 /
  openmoxie-test-mqtt-1 also stopped (ports freed, no parallel broker).
- New live containers, compose at ~/OpenMoxie-live-promotion/docker-compose.yml,
  project openmoxie-promoted:
  - openmoxie-server-promoted, image openmoxie-test-server:latest, 8001:8000,
    volume /Users/stefan/OpenMoxie/local/work:/app/site/work
  - openmoxie-mqtt-promoted, image openmoxie-test-mqtt:latest, 8883:8883,
    volumes live work:/mosquitto/log, live mosquitto-keys:/mosquitto/config/keys:ro,
    live mosquitto-debug.conf:/mosquitto/config/mosquitto.conf:ro
- Exact compose change vs live compose: image names only. Ports, volumes,
  config files are the live ones. Original live compose untouched in ~/OpenMoxie/.

## Migration (Step 4)
- Live DB migration 0018_muse_spark_chat_provider applied on first startup:
  startup log "Applying hive.0018_muse_spark_chat_provider... OK".
  Verified afterwards with showmigrations: 0018 [X]. No hand DB edits.

## Verification (Step 5, primary outputs)
- Live containers up, single clean start 21:33:13 PDT, no ImportError, no traceback.
- Live dashboard http://192.168.4.31:8001/hive/dashboard -> 200.
- Live /hive/logs/ anonymous -> 302 to /admin/login/?next=/hive/logs/;
  /hive/logs_api/?lines=10 anonymous -> 302. Staff rendering was verified on the
  test server, not re-probed live (no staff password used here).
- Broker TLS: openssl s_client to 192.168.4.31:8883, servername
  moxie.ozoneretire.com -> subject CN=moxie.ozoneretire.com, issuer LE YE2,
  TLSv1.3, AEAD-AES256-GCM-SHA384, Verify return code 0 (ok).
- Robot state observed after promotion: firmware
  v3.6.4-24_12_28-18_42-master-a90cfdee72-v24.10.803-rls-robot, pairing paired,
  mode sleep, battery 0.88, last_connect 04:18:28Z / last_disconnect 04:19:34Z
  (21:18/21:19 PDT, pre-promotion). Robot did NOT reconnect during promotion
  checks — it was already disconnected/sleep before promotion, and no wake
  was attempted. Reconnect is expected on Stefan's local wake, in daylight, in view.

## Rollback (Step 6, not needed, documented)
- If dashboard/logs/broker/server fail: stop the promoted containers with
  docker compose -p openmoxie-promoted down in ~/OpenMoxie-live-promotion,
  then docker start openmoxie-server-1 and openmoxie-mqtt-1, verify dashboard
  200 on 8001. Live DB backup above restores the pre-promotion DB if migration
  0018 ever needs reverting (do not restore over a newer DB without Stefan).
  Do not leave live broken while trying another fix.

## What remains
- Bridge OFF by default, stock GPT chat unchanged, no Puppet API wiring
  (auth decision still pending per local_moxie skill), no live robot behaviour claimed.
- Daylight live-test protocol still pending, Stefan in view, one step at a time:
  wake greeting, one chat turn, one gesture, interrupt, sleep, bridge disable;
  dance last, short tree only.
- Auth decisions for any bridge serving path still pending.
- Test server can be restarted later from its compose if needed; it is stopped now.
