# franklinwh-ha-notify

Home Assistant notifications with buttons, shared by the FranklinWH bridges (the
[Direct Connect bridge](https://github.com/david2069/franklinwh-direct-connect-bridge) and
the Modbus bridge). It finds the targets that can receive notifications, sends them with
buttons, receives the taps, and keeps a record of both.

It holds no FranklinWH device code. What a button *does* is up to the bridge.

The design, decisions and plan are in [docs/DESIGN.md](docs/DESIGN.md).

## What it does

| Piece | Module |
|---|---|
| Find each HA instance's `notify.*` targets; mark which can show buttons (`notify.mobile_app_*`) | `targets` |
| Send one notification to many targets, with a delivery count | `send` |
| Receive taps over HA's WebSocket (`mobile_app_notification_action`), reconnecting | `listener` |
| Single-use, expiring requests; wait for an answer with a timeout. In memory, or kept across restarts with `Requests.persistent(conn)` | `actions` |
| Title and message templates per event, with variables and reset | `templates` |
| A record of every send and tap, kept for a number of days. `create=False` leaves the table to the bridge's own migration (apply `SCHEMA`) | `ledger` |
| All of the above behind one object | `Notifier` |

## The button contract

A button's action id is `FWH_<VERB>_<request id>`, for example `FWH_YES_3f9c0a1b2c3d4e5f`.
The request id is inside the action id because every Companion app echoes `action` back,
while other data fields come back differently on iOS and Android. Buttons without the
`FWH_` prefix (Snooze and the like, added by the phone) are ignored.

A tap resolves as one of:
- **accepted:** the bridge's handler runs;
- **unknown:** never issued, or lost in a restart (in-memory requests);
- **expired**;
- **used:** already answered;
- **not-allowed:** a button the request didn't offer.

Every outcome is recorded.

Taps arrive only over the bridge's own authenticated WebSocket to Home Assistant. There is
no webhook and nothing on the bridge accepts an unauthenticated call.

## Use

```python
import sqlite3
from franklinwh_ha_notify import (YES, NO, Button, Event, HAInstance, Ledger, Notifier,
                                  Templates, discover)

ha = HAInstance("home", "http://supervisor/core", token=lambda: os.environ["SUPERVISOR_TOKEN"])
templates = Templates()
templates.register(Event("force_left_running", "Force still running on {gateway}",
                         "A force from before the restart is still running. Release it?"))
n = Notifier({"home": ha}, Ledger(sqlite3.connect("data/bridge.db")), templates,
             enabled=lambda: settings.notifications_on)

async def release(request, answer):
    if answer.verb == YES:
        await bridge.release_force(request.gateway)     # the bridge's own gated command
        return "released"
    return "kept"

n.on("force_left_running", release)
n.listen()                                   # one tap listener per HA instance

targets = [t for t in await discover(ha) if t.buttons]
await n.ask("force_left_running", targets, [Button(YES, "Release"), Button(NO, "Keep")],
            ttl_s=900, gateway="Home")

# Or wait for the answer inline, as a scheduler step does:
answer = await n.ask_and_wait("ok_to_charge", targets, [Button(YES, "Yes"), Button(NO, "No")],
                              timeout_s=300, gateway="Home")
if answer.timed_out:
    ...                                      # the step's "if no reply" rule
```

### Adding or removing an HA instance while running

When the user adds, edits or removes an HA instance in the bridge's settings, tell the
running notifier; no restart is needed for taps:

```python
await n.add_instance(HAInstance("cabin", "https://ha.example.com", token))  # listener starts
await n.add_instance(HAInstance("cabin", "https://ha.example.com", new_token))  # signs in again
await n.remove_instance("cabin")                                             # listener closes
await n.set_instances(instances_from_settings)                               # or sync them all
```

A new instance gets a listener only while the notifier is listening (after `listen()`,
before `close()`). Call these on the notifier's event loop.

### Requests that survive a restart

In memory is the default. To keep open requests across a restart, keep them in the
bridge's database (table `notify_requests`; `create=False` leaves it to your migration,
which applies `SCHEMA`, now version 2):

```python
db = sqlite3.connect("data/bridge.db", check_same_thread=False)
n = Notifier({"home": ha}, Ledger(db), templates, requests=Requests.persistent(db))

req = await n.ask("ok_to_charge", targets, [Button(YES, "Yes"), Button(NO, "No")],
                  ttl_s=1800, gateway="Home")
save_step_state(request_id=req.id)           # the bridge's own record

# After a restart: a tap inside the TTL resolves to the original request and runs the
# on() handler. A waiting step finds its request again and waits for the time left:
req = next((r for r in n.pending("ok_to_charge") if r.id == saved_id), None)
answer = await n.wait(req, timeout_s=1800) if req else None
```

`wait` returns an answer already given at once, and times out at once (recorded as
"no reply") when the request expired while the bridge was down.

## Development

```
python3.12 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest -q
```

The tests run against a fake Home Assistant: REST through an `httpx` mock transport, and
a real local WebSocket server that speaks HA's auth and `subscribe_events` handshake.

Releases: bump `version` in `pyproject.toml`, add the CHANGELOG section, tag `v<version>`.
The publish workflow tests, checks the tag matches, and publishes to PyPI.

## Licence

MIT
