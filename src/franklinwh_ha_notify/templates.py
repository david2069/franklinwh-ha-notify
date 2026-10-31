"""Title and message templates, one per event a bridge sends.

A bridge registers each event with its default text and the variables it supplies; a
user override replaces the default until it is reset. ``{gateway}`` is always available.
A variable a template names but the event didn't supply stays as written, so a typo
shows in the notification instead of failing the send.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Event:
    key: str
    title: str
    message: str
    variables: tuple[str, ...] = ()
    description: str = ""


class _Keep(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return "" if v is None else str(v)


@dataclass
class Templates:
    events: dict[str, Event] = field(default_factory=dict)
    overrides: dict[str, dict[str, str]] = field(default_factory=dict)  # key -> {title?, message?}

    def register(self, *events: Event) -> None:
        for e in events:
            self.events[e.key] = e

    def set(self, key: str, title: str | None = None, message: str | None = None) -> None:
        if key not in self.events:
            raise KeyError(f"unknown event {key!r}")
        o = {k: v for k, v in (("title", title), ("message", message)) if v}
        if o:
            self.overrides[key] = o
        else:
            self.overrides.pop(key, None)

    def reset(self, key: str) -> None:
        self.overrides.pop(key, None)

    def render(self, key: str, **values) -> tuple[str, str]:
        e = self.events[key]
        o = self.overrides.get(key, {})
        vals = _Keep({k: _fmt(v) for k, v in values.items()})
        vals.setdefault("gateway", "")
        try:
            return (o.get("title", e.title).format_map(vals),
                    o.get("message", e.message).format_map(vals))
        except (ValueError, IndexError):        # a stray "{" in a user's text
            return o.get("title", e.title), o.get("message", e.message)
