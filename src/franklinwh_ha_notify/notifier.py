"""The one object a bridge holds: send, ask, receive taps, record everything.

A bridge supplies its HA instances, a ledger on its own database, its event templates,
a master switch, and a handler per event that acts on an accepted answer. Everything
FranklinWH-specific (what Release does, which gateway, the gates) lives in those
handlers, in the bridge.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .actions import Answer, Button, Outcome, Request, Requests
from .ha import HAInstance
from .ledger import Ledger
from .listener import Listener, Tap
from .send import SendResult, send
from .targets import Target
from .templates import Templates

Handler = Callable[[Request, Answer], Awaitable[str | None]]


class Notifier:
    def __init__(self, instances: dict[str, HAInstance], ledger: Ledger,
                 templates: Templates, enabled: Callable[[], bool] = lambda: True,
                 requests: Requests | None = None):
        self.instances = instances
        self.ledger = ledger
        self.templates = templates
        self.enabled = enabled
        self.requests = requests or Requests()
        self._handlers: dict[str, Handler] = {}
        self._listeners: dict[str, tuple[Listener, asyncio.Task]] = {}
        self._listening = False
        self._lock = asyncio.Lock()

    def on(self, event: str, handler: Handler) -> None:
        """Act on an accepted answer to a request opened for ``event``."""
        self._handlers[event] = handler

    async def notify(self, event: str, targets: list[Target], *, gateway: str | None = None,
                     **values) -> SendResult:
        return await self._send(event, targets, gateway, None, values)

    async def ask(self, event: str, targets: list[Target], buttons: list[Button], *,
                  ttl_s: float, gateway: str | None = None, meta: dict | None = None,
                  **values) -> Request | None:
        """Send with buttons. None when nothing went out (switched off, no phone
        delivered), so the caller can apply its no-reply rule at once. A request no phone
        received is discarded, so :meth:`pending` doesn't list it."""
        if not any(t.buttons for t in targets):
            await self.ledger.arecord("SKIPPED", event, gateway=gateway, status="no target can show buttons")
            return None
        req = await self.requests.aopen(event, buttons, ttl_s, gateway, meta)
        res = await self._send(event, targets, gateway, req, values)
        if not res.delivered:
            await self.requests.adiscard(req.id)
            return None
        return req

    async def ask_and_wait(self, event: str, targets: list[Target], buttons: list[Button], *,
                           timeout_s: float, gateway: str | None = None, **values) -> Answer:
        req = await self.ask(event, targets, buttons, ttl_s=timeout_s, gateway=gateway, **values)
        if req is None:
            return Answer(None, timed_out=True)
        return await self.wait(req, timeout_s)

    def pending(self, event: str | None = None) -> list[Request]:
        """Requests still open, and answered ones nobody has collected with :meth:`wait`
        (after a restart with persistent requests, every answered one still held)."""
        return self.requests.pending(event)

    async def wait(self, request: Request, timeout_s: float) -> Answer:
        """The waiting half of :meth:`ask_and_wait`: the answer, or a timeout recorded as
        "no reply". Waits no longer than the request has left; one that expired
        unanswered (say while the bridge was down) times out at once, and one already
        answered returns its answer."""
        req = self.requests.get(request.id) or request
        left = min(timeout_s, req.expires_at - self.requests._clock())
        if req.answer is None and left <= 0:
            ans = Answer(None, timed_out=True)
        else:
            ans = await self.requests.wait(req.id, max(left, 0))
        if ans.timed_out:
            await self.ledger.arecord("RECEIVED", req.event, gateway=req.gateway,
                                      request_id=req.id, status="no reply")
        return ans

    async def _send(self, event, targets, gateway, req: Request | None, values) -> SendResult:
        title, message = self.templates.render(event, gateway=gateway or "", **values)
        if not self.enabled():
            await self.ledger.arecord("SKIPPED", event, gateway=gateway, title=title, message=message,
                               status="notifications are off")
            return SendResult()
        if not targets:
            await self.ledger.arecord("SKIPPED", event, gateway=gateway, title=title, message=message,
                                      status="no targets")
            return SendResult()
        res = await send(self.instances, targets, title, message,
                         list(req.buttons) if req else None, req.id if req else None)
        await self.ledger.arecord(
            "SENT" if res.delivered else "ERROR", event, gateway=gateway,
            request_id=req.id if req else None, status=res.summary, title=title,
            message=message, delivered=res.delivered, total=res.total,
            detail=[d.__dict__ for d in res.deliveries])
        return res

    async def on_tap(self, tap: Tap) -> Outcome:
        out = await self.requests.aresolve(tap.verb, tap.request_id, tap.reply)
        req = out.request
        event = req.event if req else "unknown"
        gateway = req.gateway if req else None
        result = None
        if out.status == "accepted":
            handler = self._handlers.get(event)
            if handler is not None:
                try:
                    result = await handler(req, out.answer)
                except Exception as e:  # noqa: BLE001 — recorded, never raised into the listener
                    result = f"failed: {type(e).__name__}: {e}"
        await self.ledger.arecord("RECEIVED", event, gateway=gateway, request_id=tap.request_id,
                           status=out.status if result is None else f"{out.status}: {result}",
                           detail={"verb": tap.verb, "reply": tap.reply,
                                   "instance": tap.instance_id})
        return out

    def listen(self, instance_ids: list[str] | None = None) -> None:
        """Start a tap listener on each instance (all by default). Idempotent. From now
        until :meth:`close`, an instance added with :meth:`add_instance` gets one too."""
        self._listening = True
        for iid in instance_ids or list(self.instances):
            if iid in self._listeners or iid not in self.instances:
                continue
            self._start(iid)

    def _start(self, iid: str) -> None:
        ln = Listener(self.instances[iid], self.on_tap)
        self._listeners[iid] = (ln, asyncio.create_task(ln.run(), name=f"ha-taps-{iid}"))

    async def _stop(self, iid: str) -> None:
        entry = self._listeners.pop(iid, None)
        if entry is not None:
            entry[1].cancel()
            await asyncio.gather(entry[1], return_exceptions=True)

    async def add_instance(self, instance: HAInstance) -> None:
        """Add an HA instance, or replace the one with the same id, on a running notifier.

        While listening, a new instance gets a tap listener. A changed URL or token
        restarts that instance's listener, so it signs in again; any other change (a
        name) keeps the connection. Call it on the notifier's event loop."""
        async with self._lock:
            iid = instance.id
            old = self.instances.get(iid)
            self.instances[iid] = instance
            entry = self._listeners.get(iid)
            # A token may be a callable reading the bridge's store, so compare what the
            # listener last signed in with against what the instance gives now.
            if entry is not None and old is not None and old.ws_url == instance.ws_url \
                    and entry[0].token_used in (None, instance.bearer()):
                entry[0].ha = instance
                return
            await self._stop(iid)
            if self._listening:
                self._start(iid)

    async def remove_instance(self, instance_id: str) -> None:
        """Remove an HA instance and close its tap listener. Unknown ids are ignored."""
        async with self._lock:
            self.instances.pop(instance_id, None)
            await self._stop(instance_id)

    async def set_instances(self, instances: dict[str, HAInstance]) -> None:
        """Make the notifier's instances exactly ``instances``: add, change and remove
        as :meth:`add_instance` and :meth:`remove_instance` do."""
        for iid in [i for i in self.instances if i not in instances]:
            await self.remove_instance(iid)
        for inst in instances.values():
            await self.add_instance(inst)

    def listener_status(self) -> dict[str, dict]:
        return {iid: {"connected": ln.connected.is_set(), "error": ln.last_error}
                for iid, (ln, _) in self._listeners.items()}

    async def close(self) -> None:
        async with self._lock:      # so an add_instance in progress can't start a listener after
            self._listening = False
            for _, task in self._listeners.values():
                task.cancel()
            await asyncio.gather(*(t for _, t in self._listeners.values()), return_exceptions=True)
            self._listeners.clear()
