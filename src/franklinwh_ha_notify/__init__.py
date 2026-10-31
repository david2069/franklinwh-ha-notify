"""Home Assistant notifications with buttons, shared by the FranklinWH bridges."""
from importlib.metadata import PackageNotFoundError, version as _v

from .actions import NO, REPLY, YES, Answer, Button, Outcome, Request, Requests, action_id, parse
from .ha import SUPERVISOR_CORE, HAInstance
from .ledger import SCHEMA, SCHEMA_VERSION, Ledger
from .listener import Listener, Tap
from .notifier import Notifier
from .send import SendResult, payload, send
from .targets import Target, discover, friendly
from .templates import Event, Templates

try:
    __version__ = _v("franklinwh-ha-notify")
except PackageNotFoundError:  # running from a checkout without install
    __version__ = "0.0.0+unknown"

__all__ = [
    "YES", "NO", "REPLY", "Answer", "Button", "Outcome", "Request", "Requests", "action_id",
    "parse", "SUPERVISOR_CORE", "HAInstance", "Ledger", "SCHEMA", "SCHEMA_VERSION", "Listener", "Tap", "Notifier",
    "SendResult", "payload", "send", "Target", "discover", "friendly", "Event", "Templates",
]
