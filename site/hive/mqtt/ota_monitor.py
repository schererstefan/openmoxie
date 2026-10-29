"""OTA log monitor — checklist + log-line classifier, no I/O, no robot.

Kickoff monitoring per Justin: there is no parent-app notification for
OTA completion, so watch the server docker logs. Containers in the
running install (do NOT touch them during prep):
  - openmoxie-server-1  (Django server log)
  - openmoxie-mqtt-1    (broker log /mosquitto/log/mosquitto.log)

Stages are inferred from log substrings only. A stage is NOT proven by
a single line; confirm version + pairing after reboot (see checkpoint).
"""

STAGES = {
    "token_request": ["http_token", "client-service-http-token"],
    "ota_url_fetch": ["ota_updates", "/api/ota_updates/rls/url"],
    "download": ["ota", "download"],
    "install": ["install", "update", "flash"],
    "reboot": ["reboot", "restart", "disconnect"],
    "reconnect": ["connected", "CONNACK"],
}

FAILURE_MARKERS = [
    "tlsv1 alert", "unknown ca", "certificate", "connection refused",
    "timeout", "404", "checksum", "signature", "corrupt", "failed",
]


def classify_log_line(line):
    """Return (stages, failure_flags) for one log line. Pure, testable."""
    text = (line or "").lower().replace("_", " ")
    stages = [name for name, keys in STAGES.items()
              if any(k.lower().replace("_", " ") in text for k in keys)]
    failures = [m for m in FAILURE_MARKERS if m in text]
    return stages, failures


def post_ota_checks():
    """Commands to run AFTER the robot reconnects, in order."""
    return [
        "Puppet API: GET /hive/puppet_api/1 -> online true",
        "DB: hive_moxiedevice row still exists, same embodied_robot_id",
        "DB/server log: firmware string contains v24.10.803 (was v24.10.801)",
        "Broker: device client d_<id> connected, TLS, subscribing, zmq events",
        "Robot in view: wake greeting, one Puppet speak line, one gesture",
        "If any check fails: STOP, do not re-flash blindly, report log tail",
    ]
