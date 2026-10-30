# Checkpoint — Spark global-switch promoted to LIVE

Date: 2026-10-29 evening PDT. Branch mycroft-spark-switch-live from mycroft-spark-global-switch (0a01a2f).
Source worktree: ~/dev/openmoxie-spark-switch. Fork only, no upstream, no merge.

## What was promoted
- Full branch code only: ai_factory resolve_chat_provider_detailed (global-default precedence),
  SinglePromptChat.vendor_explicit, migration 0019, conversations.py vendor-only-when-explicit,
  plus the prior bridge-wiring + logs code already live.
- NOT copied: test DB, test self-signed certs. Live DB and live LE certs kept.

## Steps
1. Backup BEFORE change: ~/OpenMoxie-backup-pre-spark-switch-20261029-2145.tgz (14K, DB +
   mosquitto-keys/ + docker-compose.yml + mosquitto-debug.conf, listing verified).
   Promoted compose copy saved alongside as OpenMoxie-backup-pre-spark-switch-promoted-compose.yml.
   Pre-state: promoted containers up, migrations to 0018, firmware 803, paired, sleep,
   global openai, spark model muse-spark-1.3-contributor, spark key present (presence only).
2. Stopped (NOT deleted) previous promoted containers openmoxie-server-promoted /
   openmoxie-mqtt-promoted. Old stock containers already stopped, kept for rollback.
3. Built images openmoxie-spark-switch-server:latest / openmoxie-spark-switch-mqtt:latest
   from the full spark-switch worktree (Dockerfile / mqtt.Dockerfile, full tree, never a
   partial file copy). New compose: ~/OpenMoxie-live-spark-switch/docker-compose.yml,
   project openmoxie-spark-switch, containers openmoxie-server-spark-switch (8001) /
   openmoxie-mqtt-spark-switch (8883), mounting ONLY live work dir and live mosquitto-keys/,
   live external_host 192.168.4.31. Previous promoted compose untouched in ~/OpenMoxie-live-promotion/.
4. Migration 0019 applied on startup: "Applying hive.0019_singlepromptchat_vendor_explicit... OK",
   showmigrations 0018 [X], 0019 [X]. No hand DB edits.

## Verification (primary outputs)
- New live containers up, no ImportError/traceback in startup log.
- Live dashboard http://192.168.4.31:8001/hive/dashboard -> 200.
- Live /hive/logs/ anonymous -> 302 to /admin/login/?next=/hive/logs/; logs_api anonymous -> 302.
- Broker TLS to 192.168.4.31:8883, servername moxie.ozoneretire.com: CN correct, issuer
  Lets Encrypt YE2, TLSv1.3, Verify return code 0 (ok).
- Robot state: firmware v24.10.803 (full v3.6.4-24_12_28-18_42-master-a90cfdee72-v24.10.803-rls-robot),
  pairing paired, mode sleep, battery 0.88.
- Config (presence only, no key values): global provider STILL openai, spark model STILL
  muse-spark-1.3-contributor, spark key present True. Existing conversations vendor_explicit=False.
- Robot reconnect NOT observed after this promotion: last_connect / last_disconnect in DB are
  pre-promotion (21:37 PDT, asleep/disconnected). No wake attempted (wake only when sleep-idle
  AND MQTT-connected). Reconnect expected on Stefans local wake, daylight, in view.
- Behaviour unchanged on promotion alone: global remains openai, so chat stays on GPT until
  Stefan flips global to Spark himself. After 0019, defaulted conversations will then follow
  global; explicit overrides stay put.

## Rollback (not needed)
- Stop new containers: cd ~/OpenMoxie-live-spark-switch && docker compose -p openmoxie-spark-switch down
- Restart previous promoted containers: cd ~/OpenMoxie-live-promotion && docker compose -p openmoxie-promoted up -d
- Verify dashboard 200. DB restore from the backup tarball only if migration 0019 ever needs
  reverting, and only on Stefans direction.

## Remains
- Global flip to Spark is Stefans step, NOT done here. Contributor vs Standard model choice is
  his: muse-spark-1.3-contributor is the testing variant; Standard is muse-spark-1.3.
- Bridge OFF by default, stock GPT unchanged until global flip, no Puppet API wiring,
  auth decisions for any bridge serving path still pending (local_moxie skill).
- Daylight live-test protocol still pending (Stefan in view, one step at a time: wake greeting,
  one chat turn, one gesture, interrupt, sleep, bridge disable; dance last, short tree only).
- No live robot behaviour is claimed beyond the state above.
