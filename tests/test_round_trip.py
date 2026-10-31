"""End to end against the fake HA: send with buttons, tap over the WebSocket, handler
runs, everything recorded; reconnects; a wrong token is reported."""
import asyncio

from franklinwh_ha_notify import NO, YES, Button, Event, HAInstance, Notifier, Target, Templates
from franklinwh_ha_notify.listener import Listener

PHONE = Target("home", "mobile_app_pixel", "Pixel")
EMAIL = Target("home", "email_me", "Email")


def _notifier(instance, ledger, enabled=True):
    t = Templates()
    t.register(Event("release", "Force still running on {gateway}", "Release it?"),
               Event("info", "Hello {gateway}", "{text}", ("text",)))
    return Notifier({"home": instance}, ledger, t, enabled=lambda: enabled)


async def _listening(n, fake_ha):
    n.listen()
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)


async def test_a_tap_reaches_the_handler_once(instance, fake_ha, ledger):
    n = _notifier(instance, ledger)
    calls = []

    async def release(req, ans):
        calls.append((req.gateway, ans.verb))
        return "released"
    n.on("release", release)
    await _listening(n, fake_ha)
    req = await n.ask("release", [PHONE, EMAIL], [Button(YES, "Release"), Button(NO, "Keep")],
                      ttl_s=60, gateway="Home")
    sent = dict(fake_ha.sent)
    assert sent["mobile_app_pixel"]["data"]["actions"][0]["action"] == f"FWH_YES_{req.id}"
    assert "data" not in sent["email_me"]

    await fake_ha.tap(f"FWH_YES_{req.id}")
    await fake_ha.tap(f"FWH_YES_{req.id}")          # replayed
    await fake_ha.tap("SNOOZE_15")                  # phone-side, ignored
    for _ in range(50):
        if len([r for r in ledger.recent() if r["direction"] == "RECEIVED"]) == 2:
            break
        await asyncio.sleep(0.02)
    assert calls == [("Home", YES)]
    got = [r["status"] for r in ledger.recent() if r["direction"] == "RECEIVED"]
    assert got == ["used", "accepted: released"]
    assert [r for r in ledger.recent() if r["direction"] == "SENT"][0]["status"] == "2/2 delivered"
    await n.close()


async def test_ask_and_wait_gets_a_no(instance, fake_ha, ledger):
    n = _notifier(instance, ledger)
    await _listening(n, fake_ha)

    async def tap_no():
        while not fake_ha.sent:
            await asyncio.sleep(0.01)
        rid = fake_ha.sent[-1][1]["data"]["actions"][1]["action"].rsplit("_", 1)[1]
        await fake_ha.tap(f"FWH_NO_{rid}")
    asyncio.create_task(tap_no())
    ans = await n.ask_and_wait("release", [PHONE], [Button(YES, "Yes"), Button(NO, "No")],
                               timeout_s=5, gateway="Home")
    assert ans.verb == NO and not ans.timed_out
    await n.close()


async def test_no_reply_is_recorded(instance, fake_ha, ledger):
    n = _notifier(instance, ledger)
    ans = await n.ask_and_wait("release", [PHONE], [Button(YES, "Yes")], timeout_s=0.05)
    assert ans.timed_out
    assert ledger.recent()[0]["status"] == "no reply"


async def test_switched_off_sends_nothing_and_says_so(instance, fake_ha, ledger):
    n = _notifier(instance, ledger, enabled=False)
    res = await n.notify("info", [PHONE], gateway="Home", text="hi")
    assert res.total == 0 and fake_ha.sent == []
    assert ledger.recent()[0]["direction"] == "SKIPPED"


async def test_asking_with_no_phone_is_skipped(instance, fake_ha, ledger):
    n = _notifier(instance, ledger)
    assert await n.ask("release", [EMAIL], [Button(YES, "Yes")], ttl_s=60) is None
    assert fake_ha.sent == [] and ledger.recent()[0]["status"] == "no target can show buttons"


async def test_the_listener_reconnects(instance, fake_ha):
    seen = []

    async def on_tap(tap):
        seen.append(tap.verb)
    ln = Listener(instance, on_tap, backoff_s=(0.05,))
    task = asyncio.create_task(ln.run())
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    await fake_ha.drop_clients()
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    await fake_ha.tap("FWH_YES_0123456789abcdef")
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.02)
    assert seen == ["YES"]
    task.cancel()


async def test_a_wrong_token_is_reported(fake_ha):
    bad = HAInstance("home", f"http://127.0.0.1:{fake_ha.port}", "nope")
    ln = Listener(bad, lambda t: None, backoff_s=(10,))
    task = asyncio.create_task(ln.run())
    for _ in range(100):
        if ln.last_error:
            break
        await asyncio.sleep(0.02)
    assert "Invalid access token" in ln.last_error and not ln.connected.is_set()
    task.cancel()
