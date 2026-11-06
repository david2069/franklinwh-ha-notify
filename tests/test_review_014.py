"""0.1.4: fixes from a peer review (2026-11-07)."""
import asyncio
import re
import pathlib
import sqlite3

from franklinwh_ha_notify import (NO, YES, Button, Event, Ledger, Notifier, Requests, Target,
                                  Templates)
from franklinwh_ha_notify.listener import Listener, tap_from_event

from conftest import TOKEN

PHONE = Target("home", "mobile_app_pixel", "Pixel")
README = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()


def _notifier(instance, ledger, requests=None):
    t = Templates()
    t.register(Event("release", "Force still running on {gateway}", "Release it?"),
               Event("confirm", "Sure?", "Really release {gateway}?"))
    return Notifier({"home": instance}, ledger, t, requests=requests)


async def _until(cond, n=250):
    for _ in range(n):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


def _rid(fake_ha):
    return fake_ha.sent[-1][1]["data"]["actions"][0]["action"].rsplit("_", 1)[1]


def _code(md):
    return re.findall(r"```python\n(.*?)```", md, re.S)


# --- README ---------------------------------------------------------------------------

def test_readme_connections_can_be_used_from_the_ledgers_worker_thread():
    calls = [c for b in _code(README) for c in re.findall(r"sqlite3\.connect\([^)]*\)", b)]
    assert calls and all("check_same_thread=False" in c for c in calls)


def test_readme_examples_import_what_they_use_and_handle_ask_returning_none():
    for block in _code(README):
        if "os." in block:
            assert "import os" in block
        if "req.id" in block:
            assert "if req is None" in block or "if req is not None" in block


# --- Requests.wait --------------------------------------------------------------------

async def test_two_waits_on_one_request_both_get_the_answer():
    r = Requests()
    req = r.open("release", [Button(YES, "Yes")], ttl_s=60)
    w1 = asyncio.create_task(r.wait(req.id, 2))
    w2 = asyncio.create_task(r.wait(req.id, 2))
    await asyncio.sleep(0.01)
    r.resolve(YES, req.id)
    a1, a2 = await asyncio.gather(w1, w2)
    assert a1.verb == YES and a2.verb == YES


async def test_a_wait_that_ends_leaves_the_other_waiting():
    r = Requests()
    req = r.open("release", [Button(YES, "Yes")], ttl_s=60)
    short = asyncio.create_task(r.wait(req.id, 0.02))
    long = asyncio.create_task(r.wait(req.id, 2))
    assert (await short).timed_out
    r.resolve(YES, req.id)
    assert (await long).verb == YES


# --- ask with nothing delivered -------------------------------------------------------

async def test_an_ask_nobody_received_is_not_left_pending(instance, fake_ha, ledger):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    n = _notifier(instance, ledger, Requests.persistent(conn))
    fake_ha.fail.add("mobile_app_pixel")
    assert await n.ask("release", [PHONE], [Button(YES, "Yes")], ttl_s=60) is None
    assert n.pending() == []
    assert conn.execute("SELECT COUNT(*) FROM notify_requests").fetchone()[0] == 0


# --- listener -------------------------------------------------------------------------

async def test_a_handler_can_ask_and_wait_for_an_answer_on_the_same_socket(instance, fake_ha,
                                                                           ledger):
    n = _notifier(instance, ledger)
    got = []

    async def release(req, ans):
        inner = await n.ask_and_wait("confirm", [PHONE], [Button(YES, "Yes"), Button(NO, "No")],
                                     timeout_s=3, gateway="Home")
        got.append(inner)
        return "confirmed" if inner.yes else "not confirmed"
    n.on("release", release)
    n.listen()
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    await n.ask("release", [PHONE], [Button(YES, "Release")], ttl_s=60)
    await fake_ha.tap(f"FWH_YES_{_rid(fake_ha)}")
    assert await _until(lambda: len(fake_ha.sent) == 2)
    await fake_ha.tap(f"FWH_YES_{_rid(fake_ha)}")
    assert await _until(lambda: got)
    assert got[0].verb == YES and not got[0].timed_out
    await n.close()


async def test_a_failing_handler_task_keeps_the_socket(instance, fake_ha):
    seen = []

    async def on_tap(tap):
        seen.append(tap.request_id)
        raise RuntimeError("bridge bug")
    ln = Listener(instance, on_tap, backoff_s=(10,))
    task = asyncio.create_task(ln.run())
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    await fake_ha.tap("FWH_YES_0123456789abcdef")
    await fake_ha.tap("FWH_YES_fedcba9876543210")
    assert await _until(lambda: len(seen) == 2)
    assert ln.connected.is_set() and ln.last_error is None
    task.cancel()


def test_a_non_string_action_is_not_ours():
    assert tap_from_event("home", {"action": 5}) is None
    assert tap_from_event("home", {"action": ["FWH_YES_0123456789abcdef"]}) is None


async def test_a_malformed_event_keeps_the_socket(instance, fake_ha):
    seen = []

    async def on_tap(tap):
        seen.append(tap.verb)
    ln = Listener(instance, on_tap, backoff_s=(10,))
    task = asyncio.create_task(ln.run())
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    await fake_ha.tap(5)
    await fake_ha.tap("FWH_YES_0123456789abcdef")
    assert await _until(lambda: seen)
    assert seen == ["YES"] and ln.last_error is None
    task.cancel()


async def test_backoff_starts_again_after_a_session_that_was_connected(instance, fake_ha):
    ln = Listener(instance, lambda t: None, backoff_s=(0.05, 10))
    task = asyncio.create_task(ln.run())
    for _ in range(2):          # connected, then an error: the next try is the first back-off
        await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
        fake_ha.subscribed.clear()
        await fake_ha.send_raw("not json")
    await asyncio.wait_for(fake_ha.subscribed.wait(), 2)
    task.cancel()


async def test_a_failed_subscription_is_reported(instance, fake_ha):
    fake_ha.subscribe_ok = False
    ln = Listener(instance, lambda t: None, backoff_s=(10,))
    task = asyncio.create_task(ln.run())
    assert await _until(lambda: ln.last_error or ln.connected.is_set())
    assert not ln.connected.is_set() and "Unauthorized" in ln.last_error
    task.cancel()


# --- notifier -------------------------------------------------------------------------

async def test_close_waits_for_an_instance_being_added(instance, fake_ha, ledger):
    n = _notifier(instance, ledger)
    n.listen()
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    cabin = type(instance)("cabin", f"http://127.0.0.1:{fake_ha.port}", TOKEN,
                           transport=fake_ha.transport())
    await asyncio.gather(n.close(), n.add_instance(cabin))
    await asyncio.sleep(0.05)
    running = [t for t in asyncio.all_tasks() if t.get_name().startswith("ha-taps-")]
    assert running == [] and n.listener_status() == {}


async def test_a_send_with_no_targets_is_skipped(instance, fake_ha, ledger):
    n = _notifier(instance, ledger)
    res = await n.notify("release", [], gateway="Home")
    assert res.total == 0
    row = ledger.recent()[0]
    assert row["direction"] == "SKIPPED" and row["status"] == "no targets"


# --- templates ------------------------------------------------------------------------

def test_a_template_typo_sends_the_text_as_written():
    t = Templates()
    t.register(Event("e", "On {gateway.name}", "Level {soc[x]} {0}"))
    assert t.render("e", gateway="Home", soc=50) == ("On {gateway.name}", "Level {soc[x]} {0}")
