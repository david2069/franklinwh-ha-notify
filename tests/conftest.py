"""A fake Home Assistant: REST through an httpx MockTransport, and a real local WebSocket
server speaking the auth + subscribe_events handshake."""
import asyncio
import json
import sqlite3

import httpx
import pytest
from websockets.asyncio.server import serve

from franklinwh_ha_notify import HAInstance, Ledger

TOKEN = "test-token"


class FakeHA:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []      # (service, body)
        self.fail: set[str] = set()                 # services that answer 500
        self.services = {"mobile_app_pixel": {"name": "Pixel"}, "mobile_app_iphone": {},
                         "email_me": {}, "persistent_notification": {}}
        self.clients: set = set()
        self.subscribed = asyncio.Event()
        self.auth_ok = True
        self.port = None

    def transport(self) -> httpx.MockTransport:
        def handle(req: httpx.Request) -> httpx.Response:
            if req.headers.get("authorization") != f"Bearer {TOKEN}":
                return httpx.Response(401)
            if req.url.path == "/api/services":
                return httpx.Response(200, json=[{"domain": "notify", "services": self.services}])
            if req.url.path.startswith("/api/services/notify/"):
                svc = req.url.path.rsplit("/", 1)[1]
                if svc in self.fail:
                    return httpx.Response(500)
                self.sent.append((svc, json.loads(req.content)))
                return httpx.Response(200, json=[])
            return httpx.Response(404)
        return httpx.MockTransport(handle)

    async def ws_handler(self, ws):
        await ws.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(await ws.recv())
        if not self.auth_ok or auth.get("access_token") != TOKEN:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
            return
        await ws.send(json.dumps({"type": "auth_ok"}))
        sub = json.loads(await ws.recv())
        assert sub["type"] == "subscribe_events"
        assert sub["event_type"] == "mobile_app_notification_action"
        await ws.send(json.dumps({"id": sub["id"], "type": "result", "success": True}))
        self.clients.add(ws)
        self.subscribed.set()
        try:
            await ws.wait_closed()
        finally:
            self.clients.discard(ws)

    async def tap(self, action: str, **extra):
        msg = json.dumps({"id": 1, "type": "event", "event": {
            "event_type": "mobile_app_notification_action", "data": {"action": action, **extra}}})
        for ws in list(self.clients):
            await ws.send(msg)

    async def drop_clients(self):
        self.subscribed.clear()
        for ws in list(self.clients):
            await ws.close()


@pytest.fixture
async def fake_ha():
    ha = FakeHA()
    async with serve(ha.ws_handler, "127.0.0.1", 0) as server:
        ha.port = server.sockets[0].getsockname()[1]
        yield ha


@pytest.fixture
def instance(fake_ha):
    return HAInstance("home", f"http://127.0.0.1:{fake_ha.port}", TOKEN,
                      transport=fake_ha.transport())


@pytest.fixture
def ledger():
    return Ledger(sqlite3.connect(":memory:", check_same_thread=False))
