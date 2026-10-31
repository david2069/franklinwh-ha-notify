"""Sending one notification to many targets.

The payload follows what works on both Companion apps: ``data.actions`` for buttons and
nothing nested under ``data.push`` (FWHAI found nested push options break the iOS action
category). Targets that can't show buttons get the text only.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .actions import Button, action_id
from .ha import HAInstance
from .targets import Target


def payload(title: str, message: str, buttons: list[Button] | None = None,
            request_id: str | None = None, with_buttons: bool = True) -> dict:
    p: dict = {"title": title, "message": message}
    if buttons and request_id and with_buttons:
        acts = []
        for b in buttons:
            a = {"action": action_id(b.verb, request_id), "title": b.title}
            if b.reply:
                a["behavior"] = "textInput"
            acts.append(a)
        p["data"] = {"actions": acts, "tag": f"fwh_{request_id}"}
    return p


@dataclass
class Delivery:
    target: str
    ok: bool
    error: str = ""


@dataclass
class SendResult:
    deliveries: list[Delivery] = field(default_factory=list)

    @property
    def delivered(self) -> int:
        return sum(d.ok for d in self.deliveries)

    @property
    def total(self) -> int:
        return len(self.deliveries)

    @property
    def summary(self) -> str:
        if not self.total:
            return "no targets"
        return f"{self.delivered}/{self.total} delivered"


async def _one(ha: HAInstance, t: Target, body: dict) -> Delivery:
    try:
        async with ha.client() as c:
            r = await c.post(f"/api/services/notify/{t.service}", json=body)
        if r.status_code in (200, 201):
            return Delivery(t.key, True)
        return Delivery(t.key, False, f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001 — one target's failure never stops the others
        return Delivery(t.key, False, f"{type(e).__name__}: {e}")


async def send(instances: dict[str, HAInstance], targets: list[Target], title: str,
               message: str, buttons: list[Button] | None = None,
               request_id: str | None = None) -> SendResult:
    jobs = []
    res = SendResult()
    for t in targets:
        ha = instances.get(t.instance_id)
        if ha is None:
            res.deliveries.append(Delivery(t.key, False, "unknown HA instance"))
            continue
        jobs.append(_one(ha, t, payload(title, message, buttons, request_id, t.buttons)))
    res.deliveries.extend(await asyncio.gather(*jobs))
    return res
