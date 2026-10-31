"""0.1.1: ledger writes stay off the event loop, pruning is not per insert, and a bridge
that owns its schema can apply it itself."""
import sqlite3
import threading
import time

from franklinwh_ha_notify import SCHEMA, SCHEMA_VERSION, Ledger


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_create_false_leaves_the_schema_to_the_bridge():
    conn = sqlite3.connect(":memory:")
    led = Ledger(conn, create=False)
    assert "notify_ledger" not in _tables(conn)
    conn.executescript(SCHEMA)                 # what the bridge's migration does
    led.record("SENT", "e")
    assert led.recent()[0]["event"] == "e" and SCHEMA_VERSION >= 1


async def test_arecord_writes_on_another_thread():
    led = Ledger(sqlite3.connect(":memory:", check_same_thread=False))
    seen = []
    real = led.record
    led.record = lambda *a, **k: seen.append(threading.get_ident()) or real(*a, **k)
    await led.arecord("SENT", "e", gateway="gw1")
    assert seen and seen[0] != threading.get_ident()
    assert led.recent()[0]["gateway"] == "gw1"


def test_old_rows_are_pruned_at_most_once_an_hour():
    led = Ledger(sqlite3.connect(":memory:"), keep_days=1)
    led.record("SENT", "first")                # prunes, and starts the hour
    led.record("SENT", "old", ts=time.time() - 3 * 86400)
    assert "old" in {r["event"] for r in led.recent()}
    led._pruned_at -= Ledger.PRUNE_EVERY_S
    led.record("SENT", "later")
    assert "old" not in {r["event"] for r in led.recent()}
