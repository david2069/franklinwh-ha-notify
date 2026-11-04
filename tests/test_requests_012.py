"""0.1.2 (#6): opt-in persistent requests, so a tap after a restart still resolves;
Notifier.pending() and Notifier.wait()."""
import asyncio
import sqlite3
import threading

import pytest

from franklinwh_ha_notify import (NO, SCHEMA, SCHEMA_VERSION, YES, Button, Event, Ledger,
                                  Notifier, Requests, Tap, Target, Templates)

PHONE = Target("home", "mobile_app_pixel", "Pixel")

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS notify_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, direction TEXT NOT NULL,
    event TEXT NOT NULL, gateway TEXT, request_id TEXT, status TEXT, title TEXT,
    message TEXT, delivered INTEGER, total INTEGER, detail TEXT);
CREATE INDEX IF NOT EXISTS notify_ledger_ts ON notify_ledger (ts);
"""


def _conn():
    return sqlite3.connect(":memory:", check_same_thread=False)


def _two():
    return [Button(YES, "Release"), Button(NO, "Keep")]


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _notifier(instance, ledger, requests):
    t = Templates()
    t.register(Event("release", "Force still running on {gateway}", "Release it?"))
    return Notifier({"home": instance}, ledger, t, requests=requests)


async def _received(ledger, n):
    for _ in range(100):
        rows = [r for r in ledger.recent() if r["direction"] == "RECEIVED"]
        if len(rows) >= n:
            return rows
        await asyncio.sleep(0.02)
    raise AssertionError("tap not recorded")


# --- schema ---------------------------------------------------------------------------

def test_schema_2_adds_the_requests_table_and_migrates_from_1():
    assert SCHEMA_VERSION == 2
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_V1)
    Ledger(conn, create=False).record("SENT", "kept")
    conn.executescript(SCHEMA)                 # the bridge's migration 1 -> 2
    assert "notify_requests" in _tables(conn)
    assert Ledger(conn, create=False).recent()[0]["event"] == "kept"


def test_in_memory_stays_the_default():
    conn = sqlite3.connect(":memory:")
    Ledger(conn, create=False)
    r = Requests()
    r.open("release", _two(), ttl_s=60)
    assert "notify_requests" not in _tables(conn)
    assert Requests.persistent(_conn()).pending() == []


def test_create_false_leaves_the_table_to_the_bridge():
    conn = _conn()
    with pytest.raises(sqlite3.OperationalError):
        Requests.persistent(conn, create=False)     # it did not create the table
    assert "notify_requests" not in _tables(conn)
    conn.executescript(SCHEMA)                 # the bridge's migration
    assert Requests.persistent(conn, create=False).pending() == []


# --- write-through and restart ----------------------------------------------------------

def test_open_and_resolve_survive_a_restart():
    conn = _conn()
    now = [1000.0]
    r1 = Requests.persistent(conn, clock=lambda: now[0])
    req = r1.open("release", _two() + [Button("REPLY", "Why", reply=True)], ttl_s=900,
                  gateway="Home", meta={"force": 7})
    r2 = Requests.persistent(conn, clock=lambda: now[0])        # the restart
    got = r2.get(req.id)
    assert got == req and got.meta == {"force": 7} and got.buttons[2].reply
    assert r2.resolve("REPLY", req.id, "busy").status == "accepted"
    r3 = Requests.persistent(conn, clock=lambda: now[0])
    assert r3.get(req.id).answer.reply == "busy"
    assert r3.resolve(YES, req.id).status == "used"


def test_purge_removes_rows_and_a_late_tap_still_reads_expired():
    conn = _conn()
    now = [1000.0]
    r = Requests.persistent(conn, clock=lambda: now[0])
    req = r.open("release", _two(), ttl_s=60)
    now[0] += 120
    assert Requests.persistent(conn, clock=lambda: now[0]).resolve(YES, req.id).status == "expired"
    now[0] += 3600
    r.purge()
    assert conn.execute("SELECT COUNT(*) FROM notify_requests").fetchone()[0] == 0
    assert Requests.persistent(conn, clock=lambda: now[0]).resolve(YES, req.id).status == "unknown"


async def test_async_writes_happen_off_the_event_loop():
    r = Requests.persistent(_conn())
    seen = []
    for name in ("opened", "answered"):
        real = getattr(r._store, name)
        setattr(r._store, name, lambda *a, _r=real: seen.append(threading.get_ident()) or _r(*a))
    req = await r.aopen("release", _two(), ttl_s=60)
    assert (await r.aresolve(YES, req.id)).status == "accepted"
    assert len(seen) == 2 and threading.get_ident() not in seen


# --- the acceptance, end to end ---------------------------------------------------------

async def test_a_tap_after_a_restart_resolves_to_the_original_request(instance, fake_ha, ledger):
    conn = _conn()
    now = [1000.0]
    n1 = _notifier(instance, ledger, Requests.persistent(conn, clock=lambda: now[0]))
    req = await n1.ask("release", [PHONE], _two(), ttl_s=900, gateway="Home", meta={"force": 7})
    late = await n1.ask("release", [PHONE], _two(), ttl_s=60, gateway="Home")
    await n1.close()

    n2 = _notifier(instance, ledger, Requests.persistent(conn, clock=lambda: now[0]))
    calls = []

    async def release(r, ans):
        calls.append((r.id, r.gateway, r.meta, ans.verb))
        return "released"
    n2.on("release", release)
    n2.listen()
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    now[0] += 120                              # inside the first TTL, past the second
    await fake_ha.tap(f"FWH_YES_{req.id}")
    await _received(ledger, 1)
    await fake_ha.tap(f"FWH_YES_{late.id}")
    rows = await _received(ledger, 2)
    assert calls == [(req.id, "Home", {"force": 7}, YES)]
    assert [r["status"] for r in rows] == ["expired", "accepted: released"]
    await n2.close()


# --- pending() and wait() ---------------------------------------------------------------

async def test_pending_lists_open_and_uncollected_answers(instance, fake_ha, ledger):
    conn = _conn()
    now = [1000.0]
    n = _notifier(instance, ledger, Requests.persistent(conn, clock=lambda: now[0]))
    a = await n.ask("release", [PHONE], _two(), ttl_s=900)
    b = await n.ask("release", [PHONE], _two(), ttl_s=60)
    await n.on_tap(Tap("home", NO, a.id, None, {}))
    now[0] += 120                              # b expired unanswered
    n2 = _notifier(instance, ledger, Requests.persistent(conn, clock=lambda: now[0]))
    assert [r.id for r in n2.pending()] == [a.id]
    assert n2.pending("other") == []
    assert (await n2.wait(n2.pending("release")[0], 5)).verb == NO
    assert n2.pending() == []                  # collected
    assert (await n2.wait(b, 5)).timed_out     # expired while down: at once
    assert ledger.recent()[0]["status"] == "no reply"
    assert ledger.recent()[0]["request_id"] == b.id


async def test_wait_after_a_restart_gets_the_tap(instance, fake_ha, ledger):
    conn = _conn()
    n1 = _notifier(instance, ledger, Requests.persistent(conn))
    saved = (await n1.ask("release", [PHONE], _two(), ttl_s=30)).id
    n2 = _notifier(instance, ledger, Requests.persistent(conn))
    n2.listen()
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    req = next(r for r in n2.pending() if r.id == saved)
    asyncio.get_running_loop().call_later(0.05, asyncio.ensure_future,
                                          fake_ha.tap(f"FWH_YES_{saved}"))
    assert (await n2.wait(req, 5)).yes
    await n2.close()


async def test_wait_never_outlasts_the_request(instance, fake_ha, ledger):
    n = _notifier(instance, ledger, Requests())
    req = await n.ask("release", [PHONE], _two(), ttl_s=0.05)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert (await n.wait(req, 30)).timed_out
    assert loop.time() - t0 < 5
