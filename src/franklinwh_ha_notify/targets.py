"""Notify targets: the ``notify.*`` services of each HA instance.

Only the Companion app's ``notify.mobile_app_*`` services can show buttons; every other
notify service (email, Telegram, a group) gets title and message only. The list says
which is which so a settings page can show it.
"""
from __future__ import annotations

from dataclasses import dataclass

from .ha import HAInstance


@dataclass(frozen=True)
class Target:
    instance_id: str
    service: str            # "mobile_app_pixel_7", without the "notify." prefix
    label: str

    @property
    def buttons(self) -> bool:
        return self.service.startswith("mobile_app_")

    @property
    def key(self) -> str:
        return f"{self.instance_id}::{self.service}"


def friendly(service: str) -> str:
    """"mobile_app_davids_iphone" -> "Davids Iphone"."""
    return service.removeprefix("mobile_app_").replace("_", " ").strip().title() or service


async def discover(ha: HAInstance) -> list[Target]:
    """Every notify service of one instance, phones (button-capable) first."""
    async with ha.client() as c:
        r = await c.get("/api/services")
        r.raise_for_status()
        domains = r.json()
    services = next((d.get("services") or {} for d in domains if d.get("domain") == "notify"), {})
    out = []
    for name, meta in services.items():
        if name in ("send_message", "persistent_notification"):
            continue        # entity-based and HA-list services, not a device
        label = (meta or {}).get("name") or friendly(name)
        out.append(Target(ha.id, name, label))
    return sorted(out, key=lambda t: (not t.buttons, t.label.lower()))
