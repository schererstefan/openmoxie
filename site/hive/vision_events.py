"""Vision events and detector controls for the puppet console.

Verified ONLY against doc/RemoteModuleAPI.md and hive.mqtt.volley:
- The robot hardware camera exists (5MP forehead camera for gaze/face
  tracking, per the task brief), but the OpenMoxie server code has NO
  image/video/snapshot transport (only ZMQ audio for STT). No live feed
  is built, faked, or implied anywhere here.
- Robot-side vision is EVENTS only. When a module subscribes to an event
  (Volley.update_subscriptions: event_subscription {active, clear}), the
  event arrives as a speech-like input string, e.g. 'eb-found-face',
  with parameters in input_vars where the API provides them. That is
  why detections must be handled as events, never confused with
  operator speech: this module parses eb-* inputs separately from speech.
- Detectors are RemoteModule 'execute' actions (Volley.add_execution_action),
  NOT telehealth commands: there is no telehealth detector function in
  moxie_server.py. A detector payload built here is a remote_chat
  response-action payload shape; whether an unsolicited remote_chat
  payload reaches a live robot module is NOT verified, so the console
  records the requested detector state and returns the built payload
  WITHOUT claiming the robot enabled it. Live delivery is a separate
  daylight verification step (see checkpoint).
- Server forwarding gap (verified by grep): no site/*.py code handles
  eb-found-face / eb-lost-face / eb-br-event / eb-dr-event / eb-qr-event
  today. So live robot events will NOT appear in this panel until a
  server handler forwards them into record_vision_event(). The panel
  shows only events ingested through that function, labelled as events,
  never as images. Mocked events are never presented as live.

Doc discrepancy, stated honestly: the Face Search section says
'eb-lost-target' is generated when a face is lost, while the Events
section lists 'eb-lost-face'. Both names are inventoried; neither is
collapsed into the other.
"""
import threading
import uuid
from datetime import datetime, timezone

from .mqtt.volley import Volley

# event name -> (plain name, value field in input_vars or None, description)
VISION_EVENTS = {
    "eb-found-face": ("Face found", None,
                      "A face matching the search parameters was found. Event only: no image, no identity."),
    "eb-lost-face": ("Face lost", None,
                     "Our target face was lost from view (Events section name)."),
    "eb-lost-target": ("Face target lost", None,
                       "Face lost from view for too long (Face Search section name). Doc names both eb-lost-face and eb-lost-target; both are accepted."),
    "eb-br-event": ("Book detected", "$eb_br_value",
                    "A Moxie book was visually detected; the book name arrives in input_vars."),
    "eb-dr-event": ("Aruco code detected", "$eb_dr_value",
                    "An aruco code was detected; the code arrives in input_vars."),
    "eb-qr-event": ("QR code read", "$eb_qr_value",
                    "A QR code was read; the QR string arrives in input_vars."),
}

# detector id -> (function_id, subscribed events, plain name, description)
DETECTORS = {
    "face_search": ("eb_start_binned_face_search",
                    ("eb-found-face", "eb-lost-face", "eb-lost-target"),
                    "Face search",
                    "Start searching for any face. Generates eb-found-face when a face is found."),
    "book": ("eb_enable_book", ("eb-br-event",),
             "Book detector",
             "Enable book recognition for Moxie trained book covers. Detector stays active only until one book is detected; re-enable to keep detecting."),
    "aruco": ("eb_enable_draw", ("eb-dr-event",),
              "Aruco detector",
              "Enable aruco code recognition (function eb_enable_draw). Detector stays active only until one code is detected; re-enable to keep detecting."),
    "qr": ("eb_enable_qr", ("eb-qr-event",),
           "QR detector",
           "Enable QR reading. Detector stays active only until one QR code is read; re-enable to keep detecting."),
}

# Inventory-only, NOT offered as a one-click control: experimental, and the
# doc says only min_width/min_height of its five parameters actually work.
CUSTOM_FACE_SEARCH_NOTE = (
    "eb_custom_face_search(min_width, min_height, unused_float, unused_bool, unused_bool) "
    "is EXPERIMENTAL and partially unimplemented per doc/RemoteModuleAPI.md; it is inventoried "
    "here but not offered as a console control."
)


def is_vision_event(name):
    return isinstance(name, str) and name in VISION_EVENTS


def is_detector(detector_id):
    return isinstance(detector_id, str) and detector_id in DETECTORS


def parse_vision_input(raw, input_vars=None):
    """Parse a robot input as a vision event, or return None.

    Anything that is not an exact eb-* event name is NOT a vision event:
    it must never be silently reclassified as one, and vision events must
    never be fed into the speech path as if the user said them.
    """
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if name not in VISION_EVENTS:
        return None
    _, value_field, _ = VISION_EVENTS[name]
    value = None
    if value_field and isinstance(input_vars, dict):
        value = input_vars.get(value_field)
    return {"event": name, "value": value, "value_field": value_field}


def build_detector_payload(detector_id, enabled):
    """Build the RemoteModule execute + event-subscription payload for a detector.

    enabled=True subscribes to the detector's events; enabled=False sends
    the same function with False and clears that detector's subscriptions
    (event_subscription clear=True, active=[] for the disable shape is
    expressed as a fresh subscription record with clear=True).
    Returns the Volley response dict. Raises ValueError for unknown detectors.
    This builds a payload; it does NOT publish anything.
    """
    if detector_id not in DETECTORS:
        raise ValueError("unknown detector: %r" % (detector_id,))
    function_id, events, _, _ = DETECTORS[detector_id]
    enabled = bool(enabled)
    request = {"event_id": str(uuid.uuid4()), "command": "continue", "backend": "router"}
    volley = Volley(request)
    volley.add_execution_action(function_id, fparams=["true" if enabled else "false"])
    if enabled:
        volley.update_subscriptions(list(events), clear=False)
    else:
        volley.update_subscriptions([], clear=True)
    payload = volley.response
    payload["detector"] = detector_id
    payload["detector_enabled"] = enabled
    payload["live_delivery"] = "unverified"
    return payload


class VisionEventStore:
    """In-memory, per-process vision event log. No disk, no DB, capped.

    Thread-safe append; newest first for display. A future server handler
    forwards real robot events by calling record() -- until then, only
    console-ingested events appear, and the panel says so.
    """

    MAX_EVENTS = 100

    def __init__(self):
        self._lock = threading.Lock()
        self._events = []

    def record(self, event_name, value=None, source="console-ingest", device_id=""):
        if event_name not in VISION_EVENTS:
            raise ValueError("unknown vision event: %r" % (event_name,))
        plain, value_field, description = VISION_EVENTS[event_name]
        entry = {
            "event": event_name,
            "name": plain,
            "value": value,
            "value_field": value_field,
            "description": description,
            "source": source,
            "device_id": device_id or "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "is_image": False,
        }
        with self._lock:
            self._events.insert(0, entry)
            del self._events[self.MAX_EVENTS:]
        return entry

    def recent(self, device_id="", limit=50):
        with self._lock:
            items = list(self._events)
        if device_id:
            items = [e for e in items if not e["device_id"] or e["device_id"] == device_id]
        return items[:limit]

    def clear(self):
        with self._lock:
            self._events = []


VISION_STORE = VisionEventStore()
