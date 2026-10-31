"""One record of every notification sent and every tap received, in the bridge's own
SQLite database. Kept for a number of days rather than a number of rows, so a busy day
can't push out last week.

Directions: SENT, ERROR (nothing delivered), SKIPPED (switched off, no targets),
RECEIVED (a tap; ``status`` says how it resolved).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

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
    def __init__(self, conn: sqlite3.Connection, keep_days: int = 30):
        self._db = conn
        self._lock = threading.Lock()
        self.keep_days = keep_days
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
            self._db.execute("DELETE FROM notify_ledger WHERE ts < ?",
                             (time.time() - self.keep_days * 86400,))
            self._db.commit()

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
