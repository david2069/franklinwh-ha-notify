"""The button contract both bridges share, and the requests buttons answer.

A button's action id is ``FWH_<VERB>_<request id>``. Putting the request id in the
action id itself is what makes this platform agnostic: the Companion app on every
platform echoes ``action`` back in ``mobile_app_notification_action``, while extra
data fields come back differently on iOS and Android. Actions without the ``FWH_``
prefix (Snooze and the like, added by the phone) are not ours and are ignored.

A request is single use and expires. A tap is resolved against it:
unknown (never issued, or lost in a restart), expired, used, not-allowed (a verb the
request didn't offer) or accepted. Only an accepted tap reaches the bridge.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

PREFIX = "FWH_"
YES, NO, REPLY = "YES", "NO", "REPLY"
_ACTION = re.compile(r"^FWH_([A-Z][A-Z0-9_]*?)_([0-9a-f]{16})$")


def new_id() -> str:
    return secrets.token_hex(8)


def action_id(verb: str, request_id: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", verb):
        raise ValueError(f"verb must be upper case: {verb!r}")
    return f"{PREFIX}{verb}_{request_id}"


def parse(action: str) -> tuple[str, str] | None:
    """(verb, request id) for one of our buttons, else None."""
    m = _ACTION.match(action or "")
    return (m.group(1), m.group(2)) if m else None


@dataclass(frozen=True)
class Button:
    verb: str
    title: str
    reply: bool = False     # a text-reply button (behavior: textInput)


@dataclass
class Answer:
    verb: str | None        # None when nobody answered in time
    reply: str | None = None
    timed_out: bool = False

    @property
    def yes(self) -> bool:
        return self.verb == YES


@dataclass
class Request:
    id: str
    event: str
    gateway: str | None
    buttons: tuple[Button, ...]
    expires_at: float
    meta: dict[str, Any] = field(default_factory=dict)
    used: bool = False
    answer: Answer | None = None

    @property
    def verbs(self) -> set[str]:
        return {b.verb for b in self.buttons}


@dataclass(frozen=True)
class Outcome:
    status: str             # accepted | unknown | expired | used | not-allowed
    request: Request | None = None
    answer: Answer | None = None


class Requests:
    """Open requests, in memory. Expiries are minutes, so a restart losing them is
    acceptable: a tap after a restart resolves as ``unknown`` and says so."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._open: dict[str, Request] = {}
        self._waiters: dict[str, asyncio.Future[Answer]] = {}

    def open(self, event: str, buttons: list[Button], ttl_s: float,
             gateway: str | None = None, meta: dict | None = None) -> Request:
        if not buttons:
            raise ValueError("a request needs at least one button")
        self.purge()
        req = Request(new_id(), event, gateway, tuple(buttons), self._clock() + ttl_s, meta or {})
        self._open[req.id] = req
        return req

    def get(self, request_id: str) -> Request | None:
        return self._open.get(request_id)

    def resolve(self, verb: str, request_id: str, reply: str | None = None) -> Outcome:
        req = self._open.get(request_id)
        if req is None:
            return Outcome("unknown")
        if req.used:
            return Outcome("used", req)
        if self._clock() >= req.expires_at:
            return Outcome("expired", req)
        if verb not in req.verbs:
            return Outcome("not-allowed", req)
        req.used = True
        ans = req.answer = Answer(verb, reply)
        fut = self._waiters.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result(ans)
        return Outcome("accepted", req, ans)

    async def wait(self, request_id: str, timeout_s: float) -> Answer:
        """The answer to one request, or ``Answer(None, timed_out=True)``."""
        req = self._open.get(request_id)
        if req is not None and req.answer is not None:
            return req.answer           # answered before anyone waited
        fut = asyncio.get_running_loop().create_future()
        self._waiters[request_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout_s)
        except asyncio.TimeoutError:
            return Answer(None, timed_out=True)
        finally:
            self._waiters.pop(request_id, None)

    def purge(self) -> None:
        """Drop requests expired more than an hour ago (kept that long so a late tap
        still reads ``expired``, not ``unknown``)."""
        cutoff = self._clock() - 3600
        for rid in [r.id for r in self._open.values() if r.expires_at < cutoff]:
            del self._open[rid]
