"""Receiving taps: a WebSocket subscription to ``mobile_app_notification_action``.

This is the only receive path. FWHAI's earlier webhook (an HA ``rest_command`` calling
back into the add-on) failed silently: pushes delivered and not one tap was ever
recorded. The WebSocket needs no YAML, no URL reachable from HA and no open route on
the bridge.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from websockets.asyncio.client import connect

from .actions import PREFIX, parse
from .ha import HAInstance

log = logging.getLogger(__name__)

EVENT = "mobile_app_notification_action"
BACKOFF_S = (2, 5, 10, 30, 60)


@dataclass(frozen=True)
class Tap:
    instance_id: str
    verb: str
    request_id: str
    reply: str | None
    raw: dict


def tap_from_event(instance_id: str, data: dict) -> Tap | None:
    action = data.get("action")
    if not isinstance(action, str) or not action.startswith(PREFIX):
        return None                     # a phone-side button (Snooze...), not ours
    parsed = parse(action)
    if parsed is None:
        return None
    verb, rid = parsed
    return Tap(instance_id, verb, rid, data.get("reply_text") or None, data)


class AuthError(Exception):
    pass


class SubscribeError(Exception):
    pass


class Listener:
    """One subscription per HA instance, reconnecting with back-off until stopped.

    Each tap's handler runs in its own task, so a handler that asks and waits for another
    answer doesn't hold up the taps behind it. A handler bug is logged, never drops the
    socket."""

    def __init__(self, ha: HAInstance, on_tap: Callable[[Tap], Awaitable[None]],
                 backoff_s: tuple[float, ...] = BACKOFF_S):
        self.ha = ha
        self.on_tap = on_tap
        self.backoff_s = backoff_s
        self.connected = asyncio.Event()
        self.last_error: str | None = None
        #: The token of the last sign-in attempt; None before the first.
        self.token_used: str | None = None
        self._handling: set[asyncio.Task] = set()

    async def run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("HA %s tap listener: %s", self.ha.id, self.last_error)
                if self.connected.is_set():
                    attempt = 0         # it was up: start the back-off again
            self.connected.clear()
            await asyncio.sleep(self.backoff_s[min(attempt, len(self.backoff_s) - 1)])
            attempt += 1

    async def _session(self) -> None:
        async with connect(self.ha.ws_url, open_timeout=15, ping_interval=30) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                raise AuthError(f"unexpected greeting {hello.get('type')!r}")
            self.token_used = self.ha.bearer()
            await ws.send(json.dumps({"type": "auth", "access_token": self.token_used}))
            ok = json.loads(await ws.recv())
            if ok.get("type") != "auth_ok":
                raise AuthError(ok.get("message") or ok.get("type") or "auth failed")
            await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": EVENT}))
            res = json.loads(await ws.recv())
            if res.get("id") != 1 or res.get("type") != "result" or not res.get("success"):
                err = res.get("error") or {}
                raise SubscribeError(err.get("message") or err.get("code") or "subscribe failed")
            self.last_error = None
            self.connected.set()
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "event":
                    continue
                try:
                    tap = tap_from_event(self.ha.id, msg["event"]["data"])
                except (KeyError, TypeError, AttributeError):
                    log.warning("HA %s: malformed %s event ignored", self.ha.id, EVENT)
                    continue
                if tap is not None:
                    self._handle(tap)

    def _handle(self, tap: Tap) -> None:
        task = asyncio.create_task(self.on_tap(tap), name=f"ha-tap-{tap.request_id}")
        self._handling.add(task)        # held so it isn't garbage collected mid-run
        task.add_done_callback(self._handled)

    def _handled(self, task: asyncio.Task) -> None:
        self._handling.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("tap handler failed", exc_info=task.exception())
