"""One record of every notification sent and every tap received, in the bridge's own
SQLite database. Kept for a number of days rather than a number of rows, so a busy day
can't push out last week.

Directions: SENT, ERROR (nothing delivered), SKIPPED (switched off, no targets),
RECEIVED (a tap; ``status`` says how it resolved).

The bridge owns its schema. ``create=False`` leaves the table to the bridge's own
migration, which applies :data:`SCHEMA` (version :data:`SCHEMA_VERSION`). From async code
call :meth:`arecord`, which writes off the event loop. Old rows are pruned at most once
an hour, not on every insert.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from typing import Any

#: Bumped when SCHEMA changes, so a bridge's migration knows to apply it again.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,
    event TEXT NOT NULL,
    gateway TEXT,
    request_id TEXT,
    status TEXT,
    title TEXT,
    message TEXT,
    delivered INTEGER,
    total INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS notify_ledger_ts ON notify_ledger (ts);
"""


class Ledger:
    PRUNE_EVERY_S = 3600

    def __init__(self, conn: sqlite3.Connection, keep_days: int = 30, *, create: bool = True):
        self._db = conn
        self._lock = threading.Lock()
        self.keep_days = keep_days
        self._pruned_at = 0.0
        if create:
            with self._lock:
                self._db.executescript(SCHEMA)
                self._db.commit()

    def record(self, direction: str, event: str, *, gateway: str | None = None,
               request_id: str | None = None, status: str | None = None,
               title: str | None = None, message: str | None = None,
               delivered: int | None = None, total: int | None = None,
               detail: Any = None, ts: float | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO notify_ledger (ts, direction, event, gateway, request_id, status,"
                " title, message, delivered, total, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts or time.time(), direction, event, gateway, request_id, status, title,
                 message, delivered, total,
                 None if detail is None else json.dumps(detail, default=str)))
            now = time.time()
            if now - self._pruned_at >= self.PRUNE_EVERY_S:
                self._db.execute("DELETE FROM notify_ledger WHERE ts < ?",
                                 (now - self.keep_days * 86400,))
                self._pruned_at = now
            self._db.commit()

    async def arecord(self, direction: str, event: str, **kw) -> None:
        """:meth:`record` on a worker thread, so a send or tap never blocks the loop."""
        await asyncio.to_thread(self.record, direction, event, **kw)

    def recent(self, limit: int = 100, gateway: str | None = None) -> list[dict]:
        sql = "SELECT * FROM notify_ledger"
        args: list = []
        if gateway:
            sql += " WHERE gateway = ?"
            args.append(gateway)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            cur = self._db.execute(sql, args)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            if r["detail"]:
                r["detail"] = json.loads(r["detail"])
        return rows
