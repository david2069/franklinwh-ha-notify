"""The button contract both bridges share, and the requests buttons answer.

A button's action id is ``FWH_<VERB>_<request id>``. Putting the request id in the
action id itself is what makes this platform agnostic: the Companion app on every
platform echoes ``action`` back in ``mobile_app_notification_action``, while extra
data fields come back differently on iOS and Android. Actions without the ``FWH_``
prefix (Snooze and the like, added by the phone) are not ours and are ignored.

A request is single use and expires. A tap is resolved against it:
unknown (never issued, or lost in a restart), expired, used, not-allowed (a verb the
request didn't offer) or accepted. Only an accepted tap reaches the bridge.

Requests live in memory by default. ``Requests.persistent(conn)`` also keeps them in the
``notify_requests`` table, so a tap after a restart still resolves.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
import threading
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


class _Store:
    """The ``notify_requests`` table: every change a :class:`Requests` makes, written
    through. Its own lock, like the ledger's, so a worker thread can write."""

    def __init__(self, conn: sqlite3.Connection, create: bool):
        from .ledger import SCHEMA
        self._db = conn
        self._lock = threading.Lock()
        if create:
            with self._lock:
                self._db.executescript(SCHEMA)
                self._db.commit()

    def load(self) -> list[Request]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, event, gateway, buttons, expires_at, meta, used, answer_verb,"
                " answer_reply FROM notify_requests").fetchall()
        out = []
        for rid, event, gateway, buttons, expires_at, meta, used, verb, reply in rows:
            out.append(Request(rid, event, gateway,
                               tuple(Button(**b) for b in json.loads(buttons)), expires_at,
                               json.loads(meta) if meta else {}, bool(used),
                               Answer(verb, reply) if used else None))
        return out

    def opened(self, req: Request, dropped: list[str]) -> None:
        with self._lock:
            self._delete(dropped)
            self._db.execute(
                "INSERT OR REPLACE INTO notify_requests (id, event, gateway, buttons,"
                " expires_at, meta, used, answer_verb, answer_reply)"
                " VALUES (?,?,?,?,?,?,0,NULL,NULL)",
                (req.id, req.event, req.gateway,
                 json.dumps([b.__dict__ for b in req.buttons]), req.expires_at,
                 json.dumps(req.meta, default=str)))
            self._db.commit()

    def answered(self, req: Request) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE notify_requests SET used = 1, answer_verb = ?, answer_reply = ?"
                " WHERE id = ?", (req.answer.verb, req.answer.reply, req.id))
            self._db.commit()

    def purged(self, dropped: list[str]) -> None:
        with self._lock:
            self._delete(dropped)
            self._db.commit()

    def _delete(self, ids: list[str]) -> None:
        if ids:
            self._db.executemany("DELETE FROM notify_requests WHERE id = ?", [(i,) for i in ids])


class Requests:
    """Open requests. In memory by default: expiries are minutes, so a restart losing
    them is acceptable for most uses, and a tap after a restart resolves as ``unknown``.
    :meth:`persistent` keeps them in the bridge's database as well.

    From async code use :meth:`aopen` and :meth:`aresolve`, which write the database on a
    worker thread (the :class:`Notifier` does)."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._open: dict[str, Request] = {}
        self._waiters: dict[str, asyncio.Future[Answer]] = {}
        self._claimed: set[str] = set()
        self._store: _Store | None = None

    @classmethod
    def persistent(cls, conn: sqlite3.Connection, *, create: bool = True,
                   clock: Callable[[], float] = time.time) -> "Requests":
        """Requests kept in ``notify_requests`` as well as in memory, so a tap after a
        restart still resolves. Loads the rows still held (unexpired, or expired less
        than an hour ago so a late tap reads ``expired``). ``create=False`` leaves the
        table to the bridge's own migration (apply ``SCHEMA``). For use from a worker
        thread, open ``conn`` with ``check_same_thread=False``."""
        r = cls(clock)
        r._store = _Store(conn, create)
        for req in r._store.load():
            r._open[req.id] = req
        r.purge()
        return r

    def _new(self, event: str, buttons: list[Button], ttl_s: float, gateway: str | None,
             meta: dict | None) -> tuple[Request, list[str]]:
        if not buttons:
            raise ValueError("a request needs at least one button")
        dropped = self._drop_old()
        req = Request(new_id(), event, gateway, tuple(buttons), self._clock() + ttl_s, meta or {})
        self._open[req.id] = req
        return req, dropped

    def open(self, event: str, buttons: list[Button], ttl_s: float,
             gateway: str | None = None, meta: dict | None = None) -> Request:
        req, dropped = self._new(event, buttons, ttl_s, gateway, meta)
        if self._store:
            self._store.opened(req, dropped)
        return req

    async def aopen(self, event: str, buttons: list[Button], ttl_s: float,
                    gateway: str | None = None, meta: dict | None = None) -> Request:
        """:meth:`open`, writing the database off the event loop."""
        req, dropped = self._new(event, buttons, ttl_s, gateway, meta)
        if self._store:
            await asyncio.to_thread(self._store.opened, req, dropped)
        return req

    def get(self, request_id: str) -> Request | None:
        return self._open.get(request_id)

    def pending(self, event: str | None = None) -> list[Request]:
        """Requests still waiting for an answer, and answered requests nobody has
        collected with :meth:`wait` yet (after a restart, every answered one held)."""
        now = self._clock()
        return sorted((r for r in self._open.values()
                       if (event is None or r.event == event)
                       and (r.id not in self._claimed if r.answer is not None
                            else now < r.expires_at)),
                      key=lambda r: r.expires_at)

    def _accept(self, verb: str, request_id: str, reply: str | None) -> Outcome:
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

    def resolve(self, verb: str, request_id: str, reply: str | None = None) -> Outcome:
        out = self._accept(verb, request_id, reply)
        if self._store and out.status == "accepted":
            self._store.answered(out.request)
        return out

    async def aresolve(self, verb: str, request_id: str, reply: str | None = None) -> Outcome:
        """:meth:`resolve`, writing the database off the event loop."""
        out = self._accept(verb, request_id, reply)
        if self._store and out.status == "accepted":
            await asyncio.to_thread(self._store.answered, out.request)
        return out

    async def wait(self, request_id: str, timeout_s: float) -> Answer:
        """The answer to one request, or ``Answer(None, timed_out=True)``."""
        req = self._open.get(request_id)
        if req is not None and req.answer is not None:
            self._claimed.add(request_id)
            return req.answer           # answered before anyone waited
        fut = asyncio.get_running_loop().create_future()
        self._waiters[request_id] = fut
        try:
            ans = await asyncio.wait_for(fut, timeout_s)
            self._claimed.add(request_id)
            return ans
        except asyncio.TimeoutError:
            return Answer(None, timed_out=True)
        finally:
            self._waiters.pop(request_id, None)

    def _drop_old(self) -> list[str]:
        cutoff = self._clock() - 3600
        dropped = [r.id for r in self._open.values() if r.expires_at < cutoff]
        for rid in dropped:
            del self._open[rid]
            self._claimed.discard(rid)
        return dropped

    def purge(self) -> None:
        """Drop requests expired more than an hour ago (kept that long so a late tap
        still reads ``expired``, not ``unknown``)."""
        dropped = self._drop_old()
        if self._store and dropped:
            self._store.purged(dropped)
