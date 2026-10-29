"""Show-me protocol: Stefan shows Moxie something, Moxie responds.

VERDICT PENDING camera research: this module deliberately stores NO
image, NO video, NO audio, and NO transcript beyond the session's
in-memory description text. It is a session protocol, not a camera feed:

- idle: no session, nothing observed.
- awaiting_show: Stefan initiated "show me", robot does a look behaviour
  and asks to be shown something. One session at a time.
- received: the user (or STT, in a future live path) supplies a
  description of what was shown, e.g. "a red book". That description is
  returned in the turn, not stored on disk.
- described: robot acknowledges with speech + look/gesture. Session ends.

If camera research proves a real capture path exists, that path must be
added as a separate, gated module with Stefan approval, never by
quietly extending this one. Continuous recording is out of scope.
"""
from .gesture_conversation import build_gesture_turn


class ShowMeSession:
    def __init__(self):
        self.state = "idle"
        self.description = None

    def start(self):
        if self.state != "idle":
            raise ValueError("a show-me session is already active")
        self.state = "awaiting_show"
        turn = build_gesture_turn("look")
        turn["speech"] = "Show me, Stefan. I am looking."
        turn["session_state"] = self.state
        return turn

    def receive(self, description):
        if self.state != "awaiting_show":
            raise ValueError("no show-me session is awaiting an object")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("show-me description is required")
        self.description = description.strip()
        self.state = "received"
        return {
            "command": "describe",
            "description": self.description,
            "stored_on_disk": False,
            "session_state": self.state,
        }

    def acknowledge(self):
        if self.state != "received":
            raise ValueError("nothing has been shown yet to acknowledge")
        self.state = "idle"
        desc = self.description
        self.description = None
        turn = build_gesture_turn("celebrate")
        turn["speech"] = f"Thank you, Stefan. I see {desc}."
        turn["session_state"] = "idle"
        return turn

    def cancel(self):
        self.state = "idle"
        self.description = None
        return {"command": "interrupt", "session_state": "idle"}
