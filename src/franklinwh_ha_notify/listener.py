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
    action = data.get("action") or ""
    if not action.startswith(PREFIX):
        return None                     # a phone-side button (Snooze...), not ours
    parsed = parse(action)
    if parsed is None:
        return None
    verb, rid = parsed
    return Tap(instance_id, verb, rid, data.get("reply_text") or None, data)


class AuthError(Exception):
    pass


class Listener:
    """One subscription per HA instance, reconnecting with back-off until stopped."""

    def __init__(self, ha: HAInstance, on_tap: Callable[[Tap], Awaitable[None]],
                 backoff_s: tuple[float, ...] = BACKOFF_S):
        self.ha = ha
        self.on_tap = on_tap
        self.backoff_s = backoff_s
        self.connected = asyncio.Event()
        self.last_error: str | None = None

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
            self.connected.clear()
            await asyncio.sleep(self.backoff_s[min(attempt, len(self.backoff_s) - 1)])
            attempt += 1

    async def _session(self) -> None:
        async with connect(self.ha.ws_url, open_timeout=15, ping_interval=30) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                raise AuthError(f"unexpected greeting {hello.get('type')!r}")
            await ws.send(json.dumps({"type": "auth", "access_token": self.ha.bearer()}))
            ok = json.loads(await ws.recv())
            if ok.get("type") != "auth_ok":
                raise AuthError(ok.get("message") or ok.get("type") or "auth failed")
            await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": EVENT}))
            self.last_error = None
            self.connected.set()
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "event":
                    continue
                tap = tap_from_event(self.ha.id, (msg.get("event") or {}).get("data") or {})
                if tap is not None:
                    try:
                        await self.on_tap(tap)
                    except Exception:  # noqa: BLE001 — a handler bug never drops the socket
                        log.exception("tap handler failed for %s", tap.request_id)
