"""One Home Assistant instance: its REST base, its WebSocket URL and a token.

Under the HA Supervisor (an add-on) the core API is ``http://supervisor/core`` and the
token is ``SUPERVISOR_TOKEN``, which changes on every add-on restart, so the token is
read through a callable each time it is needed rather than stored.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

SUPERVISOR_CORE = "http://supervisor/core"


@dataclass
class HAInstance:
    id: str
    base_url: str
    token: Callable[[], str] | str
    name: str = ""
    timeout_s: float = 10.0
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    def bearer(self) -> str:
        return self.token() if callable(self.token) else self.token

    @property
    def ws_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base == SUPERVISOR_CORE:
            return "ws://supervisor/core/websocket"
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):] + "/api/websocket"
        return "ws://" + base.removeprefix("http://") + "/api/websocket"

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url.rstrip("/"), timeout=self.timeout_s,
            headers={"Authorization": f"Bearer {self.bearer()}"}, transport=self.transport)
