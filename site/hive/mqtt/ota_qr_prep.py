"""OTA 803 prep ONLY — clone-only, never shown to a robot, never flashed.

Mirrors the exact server edits Justin described for the 803 OTA path
(doc/RemoteModuleAPI.md OTA section), without applying them to
moxie_server.py in this prep branch:

- scfg.webservice_root = "https://duranaki.com/"
- scfg.gcp_project = "o"  (reduces QR density; Moxie struggles with dense QRs)
- _PROVIDE_HTTP_TOKENS = True in moxie_server.py (server returns the
  dummy "notoken" token when the robot asks for an HTTP token)
- Robot config record: {"ota_update": {"id": "rls",
  "version": "v3.6.4-24_12_28-18_42-master-a90cfdee72-v24.10.803-rls-robot"}}

The robot, once it sees a newer ota_update version, requests an HTTP
token, then GETs {web_root}/api/ota_updates/{id}/url?access_token=...
Justin's duranaki server returns, ignoring the token parameter:
  {"url": "https://duranaki.com/moxie/v3.6.4-24_12_28-18_42-master-a90cfdee72-v24.10.803-rls-robot"}
Verified live Oct 29, 2026: GET https://duranaki.com/api/ota_updates/rls/url
with no token AND with a dummy token both returned HTTP 200 with exactly
that JSON body. The image itself was NOT downloaded (HEAD only, 200,
1,799,076,684 bytes, Apache/2.4.52 Ubuntu, Last-Modified Oct 29 05:16 UTC).

QR payload shape (same as stock get_endpoint_qr_data):
  {"debug": {"command": "om", "param": "<base64 of ServiceConfiguration2>"}}
"""

import base64
import json

from .protos.embodied.logging.Cloud2_pb2 import ServiceConfiguration2

OTA_ID = "rls"
OTA_VERSION = "v3.6.4-24_12_28-18_42-master-a90cfdee72-v24.10.803-rls-robot"
OTA_WEB_ROOT = "https://duranaki.com/"
OTA_IMAGE_URL = (
    "https://duranaki.com/moxie/"
    "v3.6.4-24_12_28-18_42-master-a90cfdee72-v24.10.803-rls-robot"
)
OTA_URL_ENDPOINT = "https://duranaki.com/api/ota_updates/rls/url"
OTA_CONFIG_RECORD = {"ota_update": {"id": OTA_ID, "version": OTA_VERSION}}


def build_ota_endpoint_qr_data(mqtt_host="192.168.4.31", override_port=8883,
                               disable_verify=True):
    """Build the OTA migration QR payload. Does NOT print or display it."""
    scfg = ServiceConfiguration2()
    scfg.webservice_root = OTA_WEB_ROOT
    scfg.gcp_project = "o"
    scfg.mqtt_host = mqtt_host
    scfg.override_port = override_port
    scfg.disable_verify = disable_verify
    param = base64.b64encode(scfg.SerializeToString()).decode("utf-8")
    return json.dumps({"debug": {"command": "om", "param": param}})


def decode_ota_endpoint_qr_data(qr_json):
    """Decode a QR payload back to a field dict. Raises ValueError on bad input."""
    try:
        obj = json.loads(qr_json)
        param = obj["debug"]["param"]
        if obj["debug"]["command"] != "om":
            raise ValueError("not a migration QR (command != om)")
        scfg = ServiceConfiguration2()
        scfg.ParseFromString(base64.b64decode(param))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"undecodable OTA QR payload: {exc}") from exc
    return {
        "webservice_root": scfg.webservice_root,
        "gcp_project": scfg.gcp_project,
        "mqtt_host": scfg.mqtt_host,
        "override_port": scfg.override_port,
        "disable_verify": scfg.disable_verify,
    }


def expected_robot_request_url(access_token="notoken"):
    """URL the robot will request after receiving the dummy HTTP token."""
    return f"{OTA_URL_ENDPOINT}?access_token={access_token}"
