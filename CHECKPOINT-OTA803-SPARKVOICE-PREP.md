# Prep checkpoint — OTA 803 + Muse Spark voice — PREP ONLY, NOT EXECUTED

- Clone: /Users/stefan/dev/openmoxie-contrib, branch mycroft-ota803-prep, base mycroft-bridge-full 52a6cfc.
- Fences kept: running ~/OpenMoxie never touched. No container, DB, broker, cert, or port change. No command sent to the real robot. No new QR printed or shown to the robot. No OTA flashed. No deployment. No key, password, or token recorded. No PR opened.
- Status: PREP ONLY. OTA is NOT approved by this document. Stefan must give an explicit flash go-ahead at kickoff, after the go/no-go list below.

## Part 1 — OTA to 803, Justin's duranaki path

### What Justin said (screenshot, Oct 29, 2026, LinkedIn)
- Edit the migration QR to include the web root, put https://duranaki.com.
- The api path to get the full URL is the URL he sent before.
- Monitor the docker logs for the server to see the OTA complete, as there is no parent app to notify.
- Then you do not have to spin up Apache.

### Exact code edits to apply AT KICKOFF ONLY (not applied in this prep branch)
In site/hive/mqtt/moxie_server.py:
1. Line 27: _PROVIDE_HTTP_TOKENS = False  ->  True
   Effect, lines 248-252: on client-service-http-token the server replies with the dummy token 'notoken'. Comment in code: OpenMoxie does not support any, but providing a dummy token can unblock some Moxie actions like OTA download.
2. get_endpoint_qr_data(), lines 500-511: add, before serialising:
   scfg.webservice_root = 'https://duranaki.com/'
   and change scfg.gcp_project from self._mqtt_project_id to 'o'
   Reason, doc/RemoteModuleAPI.md OTA section (lines 131-162): minimise QR density, Moxie can struggle with dense QR codes. The stock function currently sets gcp_project, mqtt_host, override_port, disable_verify, and does NOT set webservice_root — that missing field is the edit Justin describes.
3. No other server change is required for this path. Do NOT change mqtt_host, port, or disable_verify at kickoff unless the kickoff review says so; the working migration config is mqtt_host 192.168.4.31, port 8883, disable_verify 1.

### Exact config record to add AT KICKOFF ONLY
In the robot config (admin view, per doc/RemoteModuleAPI.md):
  'ota_update': {'id': 'rls', 'version': 'v3.6.4-24_12_28-18_42-master-a90cfdee72-v24.10.803-rls-robot'}
Current robot firmware (confirmed Oct 28, 2026): v24.10.801. The version mismatch is what triggers the robot to request a token and then fetch the URL.

### Exact robot request and expected response
- Robot request: https://duranaki.com/api/ota_updates/rls/url?access_token=notoken
  (token value is the dummy 'notoken' from step 1; Justin's server ignores the parameter, as his own Apache setup did.)
- Expected response, JSON, exactly:
  {'url': 'https://duranaki.com/moxie/v3.6.4-24_12_28-18_42-master-a90cfdee72-v24.10.803-rls-robot'}
- Robot then downloads the signed image from that URL.

### Duranaki endpoint check — DONE, Oct 29, 2026, public GET, no robot, no download
- GET https://duranaki.com/api/ota_updates/rls/url (no token): HTTP 200, body exactly the JSON above.
- GET https://duranaki.com/api/ota_updates/rls/url?access_token=test-not-real (dummy): HTTP 200, same JSON body.
- HEAD https://duranaki.com/moxie/v3.6.4-24_12_28-18_42-master-a90cfdee72-v24.10.803-rls-robot: HTTP 200, Apache/2.4.52 (Ubuntu), Content-Length 1799076684 (about 1.80 GB), Last-Modified Oct 29 05:16 UTC.
- Conclusion: Justin's endpoint IS live today and serves the exact path the robot will request. No Apache spin-up is needed on our side, as Justin said. The image itself was NOT downloaded and is NOT hash-verified — see go/no-go.

### QR generation code — generated and decoded in the clone, NOT printed, NOT shown to the robot
- New module: site/hive/mqtt/ota_qr_prep.py
  - build_ota_endpoint_qr_data(mqtt_host='192.168.4.31', override_port=8883, disable_verify=True) builds ServiceConfiguration2 (proto field webservice_root is field 2, site/hive/mqtt/protos/embodied/logging/Cloud2_pb2.py) with webservice_root https://duranaki.com/, gcp_project 'o', and returns {'debug': {'command': 'om', 'param': base64}}.
  - decode_ota_endpoint_qr_data() parses it back and raises ValueError on bad input.
  - Constants: OTA_ID 'rls', OTA_VERSION above, OTA_URL_ENDPOINT https://duranaki.com/api/ota_updates/rls/url, OTA_IMAGE_URL above.
- Test: site/hive/test_ota_qr_prep.py — roundtrip asserts webservice_root, project 'o', host, port, disable_verify; rejects bad payloads; asserts config record and URLs. The QR payload string itself is deliberately NOT printed in test output or in this checkpoint.
- What is NOT done: no PDF, no print job, no screen display to Moxie. A printed QR is a kickoff step only, after the go/no-go list passes, with Stefan in view.

### Docker-log monitoring — script and checklist
- New module: site/hive/mqtt/ota_monitor.py — classify_log_line() maps log lines to stages (token_request, ota_url_fetch, download, install, reboot, reconnect) and flags failure markers (tlsv1 alert, unknown ca, certificate, connection refused, timeout, 404, checksum, signature, corrupt, failed). post_ota_checks() lists the post-reconnect verifications.
- Containers to tail at kickoff (running install only, at kickoff, not now):
  - openmoxie-server-1 (Django server log) — watch for the HTTP TOKEN send and any OTA-related log lines.
  - openmoxie-mqtt-1 (broker log /mosquitto/log/mosquitto.log) — watch for device disconnect/reconnect and CONNACK.
- Completion is NOT a single log line. Confirm all of: Puppet API online true; DB hive_moxiedevice row still exists with the same embodied_robot_id; firmware string contains v24.10.803; broker shows the device client connected over TLS, subscribing, publishing zmq events; robot in view answers one Puppet speak line and one gesture.
- Failure criteria — STOP, do not re-flash blindly: power loss during download/install, robot disconnects and does not reconnect, TLS/cert errors, 404/checksum/signature errors, version still 801 after reconnect, device row gone, or robot unresponsive. Report the log tail to Justin before any second attempt.

### Pairing-fix point — clarified
- Justin, Oct 28: 803 has a pairing fix — 801, if you were not paired, it would not advance; 803 just accepts pairing status from the server as truth.
- Our robot IS paired on 801 (device row exists, talked, puppeted Oct 28). OTA is therefore OPTIONAL, not needed for current pairing. It only becomes needed if 801-specific failures appear (re-pair stall, cert/pairing/Wi-Fi-transition failure, or a feature that requires 803). Staying on 801 is the default unless a failure forces the decision.

### Go/no-go gate list — ALL must pass before any kickoff
1. Stefan explicit flash go-ahead, in chat, at kickoff time. This prep is not that go-ahead.
2. Hash verification if obtainable from Justin (SHA256 of the 1.80 GB image, or Justin's confirmation of the ETag/last-modified). Currently NOT obtained — do not flash on the link alone if a hash can be got first.
3. Duranaki endpoint live check re-run at kickoff (it passed Oct 29, re-check, it is Justin's server and can change).
4. QR decode verified from the exact QR that will be shown (roundtrip test above, plus a decode of the printed QR file before showing it).
5. Backup of running server code, DB (~/OpenMoxie/local/work/db.sqlite3), and certs (mosquitto-keys), with a written rollback path.
6. Robot charged AND plugged in, Stefan in view for the whole OTA, robot not left unattended. Brick risk on power loss is the main risk; the robot rebooted once on Oct 28.
7. Server edits (webservice_root, project 'o', _PROVIDE_HTTP_TOKENS=True) applied to a reviewed copy first, then to the running server, with the previous code saved.
8. Monitoring in place: two log tails running, plus the post-OTA check list above, before the QR is shown.

## Part 2 — Muse Spark voice prep

### Verdict first — STT, not TTS
- Muse Voice Transcribe (muse-voice-transcribe-1.0, Meta Model API, launched Sep 1, 2026) is SPEECH-TO-TEXT ONLY. It does NOT synthesise speech and does NOT provide a speech-to-speech API, per Meta's own docs as summarised in secondary coverage (developer.meta.com Model API page, orcarouter.ai model page, Sep 29, 2026 read of Meta docs).
- Do NOT present it as a TTS replacement. A separate 'Muse voice model' for TTS / custom voices is, as of Oct 26, 2026 reporting, internal testing only, disabled for public users, no confirmed release date. Muse Realtime Voice / Avatar (Sep 23, 2026) are consumer-app only, not on the Meta Model API.
- What this means for Moxie: Spark chat + Muse Voice STT + Moxie robot TTS is a coherent full voice loop. Spark chat alone does not give Moxie a new voice.

### What Muse Voice Transcribe actually is
- Model: muse-voice-transcribe-1.0, Meta Model API.
- Endpoints: WebSocket wss://api.meta.ai/v1/asr/realtime (streaming), POST /v1/asr/transcribe (one-shot recordings).
- Price: USD 0.18 per hour of audio (USD 3.00 per 1,000 minutes), same for streaming and batch.
- Streaming: 80 ms audio chunks, live partial transcripts, finalised transcript about 0.16 s after speech ends in third-party reporting.
- Diarization: 20+ speakers in one stream, no separate batch pass.
- Endpointing / voice activity detection: built into the model, native speech boundaries, so turn ends are detected by the model, not by a separate timer.
- Languages: 70+ trained, 25 extensively validated at launch, code-switching within and between sentences, language and keyword biasing.
- Protocol quirks (from third-party provider implementations, to verify against Meta docs at implementation): auth is in the first frame, not the Authorization header; turns can overlap, state keyed by turnId; idle audio closes the stream and a WebSocket ping does not reset that timer; streaming needs continuous real-time PCM, including silence; realtime sessions have a reported 60-minute maximum in one plugin implementation.
- Cost note: at Moxie conversation volumes (minutes per day, not hours), the per-hour price is negligible in absolute terms, but it is still a metered cloud call — no live endpoint call was made in this prep, and none should be made without Stefan approval.

### (i) STT swap option — what changes in OpenMoxie
- Today: site/hive/mqtt/zmq_stt_handler.py, STTHandler.get_stt_client() (lines 101-105) calls create_openai() from ai_factory.py, and the handler doc (lines 23-26) says it is tuned to OpenAI Whisper, which does not support streaming, so it buffers a whole utterance then transcribes it. STT was deliberately NOT touched in Stage B, and still calls create_openai() explicitly.
- Swap design (prep, not implemented):
  - Add a new STT provider module, e.g. site/hive/mqtt/muse_voice_stt.py, with the same session interface as STTSession/STTHandler (on_request, transcript out, session keyed by device+uuid), but backed by a WebSocket client to wss://api.meta.ai/v1/asr/realtime, with first-frame auth, turnId-keyed state, and endpointing-driven turn completion.
  - Add a config switch (HiveConfiguration, e.g. stt_provider openai/muse_voice, default openai) so the swap is opt-in and reversible, mirroring the Spark chat_provider pattern. Do NOT change the default.
  - Keep Whisper as fallback: if the Muse Voice stream fails or times out, fall back to the existing Whisper path for that utterance, and log the fallback, rather than dropping the turn.
  - Key handling: Muse Voice needs its own Meta Model API key, separate from the Spark chat key in concept (one Meta key may serve both, verify at implementation), stored the same plaintext way as other keys in this codebase today — flag that under the overnight vulnerability audit (plaintext keys in SQLite), do not claim it is encrypted.
  - Audio format: Moxie ZMQ STT requests carry audio in the robot's format; the swap must include a format/PCM conversion step and a test for it. That conversion is unverified today and is the main implementation risk, after the WebSocket protocol itself.
- Benefits if it works: lower turn latency (streaming partials + 0.16 s finalisation vs whole-utterance Whisper), native endpointing (cleaner turn boundaries, less cut-off or hang), diarization (Stefan vs other speakers in the room, useful for the bridge's identity work), multilingual/code-switching (German/English, relevant for Stefan).
- What stays OpenAI: Whisper fallback, and (until a separate decision) all chat if Spark is not selected, plus any OpenAI TTS if that is chosen later. STT swap does not change the chat provider.

### (ii) TTS reality — Moxie robot TTS remains the output voice for now
- Output today: Moxie robot's own TTS, via telehealth PLAY_OUTPUT and remote_chat speech, with the phonetic rule 'my croft' (two words, croft not craft, confirmed Oct 28, 2026). That rule stays at the speech output boundary in control_commands.py / bridge, regardless of STT provider.
- Muse Voice Transcribe gives no voice. If Stefan wants a different output voice later, the candidates are a separate decision, not this prep: OpenAI TTS (already a keyholder in this stack), ElevenLabs, or a future public Muse voice/TTS product. None is selected here, none is costed here beyond noting that OpenAI TTS would reuse the existing OpenAI key relationship.
- Do not promise a Muse voice, a cloned voice, or a voice that sounds like Muse/Mycroft, unless Meta ships a public TTS product and Stefan chooses it.

### (iii) Voice interaction design — full loop
- Loop: Moxie mic -> ZMQ STT request -> (Whisper today, Muse Voice Transcribe if swapped) -> transcript -> bridge/chat (Spark or GPT, persona, short/low-reasoning to preserve volley timing) -> speech text + mood + gesture -> Moxie TTS + behaviour tree.
- Volley timing: stock OpenMoxie volleys expect a prompt response in tens of words and a timely reply; Spark low reasoning and a 20 s chat timeout are already in Stage B. Streaming STT helps the input side, but does not change the chat-side latency budget.
- Barge-in limits: today, Whisper is whole-utterance, so there is no true barge-in — the user cannot interrupt mid-robot-speech by voice and have the robot stop on the word. Muse Voice endpointing changes turn-boundary detection, but it does NOT by itself give barge-in: the robot side still needs an interrupt path (Puppet interrupt exists, and the bridge interrupt clears gesture/dance/show-me/celebration). A true barge-in design would need: streaming STT partials -> a wake/interrupt keyword or voice-activity gate during robot speech -> Puppet interrupt -> new turn. That is a separate, daylight-tested feature, not a consequence of swapping STT.
- Diarization use: label turns by speaker (Stefan vs others) in the bridge session, but do NOT use diarization as identity authentication. It is a hint, not a login.
- Privacy: STT audio goes to the chosen cloud provider (OpenAI today, Meta if swapped). Transcripts should stay off by default in the bridge (per the bridge checkpoint open question), and no audio should be stored by the server beyond the utterance buffer needed for transcription.

### (iv) Mocked/offline test plan for the STT swap (no live robot, no paid call)
1. Unit: WebSocket frame builder (first-frame auth shape, turnId state, endpointing event -> turn complete), PCM conversion (robot format -> PCM16 24 kHz or whatever the endpoint requires, verified against Meta docs at implementation), fallback to Whisper on stream error/timeout.
2. Mocked transport: a fake WebSocket server that replays partial transcripts, a speechEnd boundary, and a diarization label, and asserts the handler emits exactly one final transcript per turn, in order, with no overlap leak between turns.
3. Replay: recorded ZMQ STT request sequences from the clone's existing STT tests, fed through the new handler, asserting the same final transcripts as Whisper would give on the same audio (where audio is mocked), plus the new endpointing/diarization metadata.
4. Integration (mocked only): bridge turn with the new STT handler, fake agent, fake transport — wake -> transcript -> chat -> speech+gesture, interrupt mid-turn.
5. Live gate (daylight, Stefan in view, separate approval): one real utterance on a test server copy, then one on the running server, comparing Whisper vs Muse Voice transcripts, latency, and endpointing, before any default change. No default change without that comparison.

## Tests — this prep branch
- Command, from site/ with the clone venv:
  ../.venv-stageb/bin/python -m unittest hive.test_chat_models hive.test_stt hive.test_muse_spark hive.test_control_commands hive.test_overnight hive.test_mycroft_bridge hive.test_ota_qr_prep
- Result: Ran 115 tests in 0.160s — OK. 115 pass / 0 fail / 0 errors.
  - 8 chat models + 6 STT (existing, unmodified) + 22 Spark + 25 control (Stage B) + 23 overnight + 26 bridge + 5 OTA QR/monitor (3 QR roundtrip/config/rejection + 2 monitor classify/post-checks).
- git diff --check: clean (verify at commit time).
- New files: site/hive/mqtt/ota_qr_prep.py, site/hive/mqtt/ota_monitor.py, site/hive/test_ota_qr_prep.py, this checkpoint. No file in ~/OpenMoxie touched. No QR printed. No image downloaded.

## One-page kickoff checklist for Stefan to approve later
- [ ] OTA: decide if OTA is needed at all (default: stay on 801, paired and working) or if an 801-specific failure forces it.
- [ ] OTA: get hash confirmation from Justin, or record that you accept flashing on the link + ETag alone.
- [ ] OTA: re-check duranaki endpoint at kickoff, back up server/DB/certs, apply the three server edits to a reviewed copy, add the ota_update config record, print the QR, verify its decode, robot charged/plugged, you in view, two log tails running, then show the QR.
- [ ] OTA: after reconnect, run the post-OTA checks (version 803, same device row, Puppet speak + gesture). If any fails, stop and report logs to Justin.
- [ ] Voice: decide STT swap (Whisper stays default vs Muse Voice Transcribe opt-in) and TTS (Moxie TTS stays, vs a separate provider later).
- [ ] Voice: approve a small paid test (minutes of audio at USD 0.18/hr, plus any Meta key setup you do yourself) before any live STT comparison.
- [ ] Voice: approve bridge wiring + auth decisions from the overnight audit before any bridge endpoint goes live, regardless of STT choice.
- [ ] Neither OTA nor STT swap is approved by this prep. Both need a fresh go-ahead at kickoff.

