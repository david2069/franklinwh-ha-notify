"""0.1.3 (#5): add, change or remove an HA instance on a running Notifier, with its tap
listener."""
import asyncio

from franklinwh_ha_notify import YES, Button, Event, HAInstance, Notifier, Target, Templates

from conftest import TOKEN

PHONE = Target("home", "mobile_app_pixel", "Pixel")


def _notifier(instances, ledger):
    t = Templates()
    t.register(Event("release", "Force still running on {gateway}", "Release it?"))
    return Notifier(instances, ledger, t)


def _inst(fake_ha, iid="home", token=TOKEN):
    return HAInstance(iid, f"http://127.0.0.1:{fake_ha.port}", token, transport=fake_ha.transport())


async def _until(cond, n=250):
    for _ in range(n):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_an_instance_added_while_listening_gets_a_listener_and_its_taps(fake_ha, ledger):
    n = _notifier({}, ledger)
    n.listen()
    calls = []

    async def release(req, ans):
        calls.append(ans.verb)
        return "released"
    n.on("release", release)
    await n.add_instance(_inst(fake_ha))
    assert "home" in n.instances
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    assert n.listener_status()["home"]["connected"]

    req = await n.ask("release", [PHONE], [Button(YES, "Release")], ttl_s=60)
    await fake_ha.tap(f"FWH_YES_{req.id}")
    assert await _until(lambda: calls)
    assert calls == [YES]
    await n.close()


async def test_adding_before_listen_starts_no_listener(fake_ha, ledger):
    n = _notifier({}, ledger)
    await n.add_instance(_inst(fake_ha))
    assert "home" in n.instances and n.listener_status() == {}
    n.listen()
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    assert list(n.listener_status()) == ["home"]
    await n.close()


async def test_removing_an_instance_closes_its_listener(fake_ha, ledger):
    n = _notifier({"home": _inst(fake_ha)}, ledger)
    n.listen()
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    await n.remove_instance("home")
    assert "home" not in n.instances and n.listener_status() == {}
    assert await _until(lambda: not fake_ha.clients)
    await n.remove_instance("home")                 # already gone: no error
    await n.close()


async def test_a_new_token_restarts_the_listener(fake_ha, ledger):
    n = _notifier({"home": _inst(fake_ha, token="nope")}, ledger)
    n.listen()
    assert await _until(lambda: n.listener_status()["home"]["error"])
    assert not n.listener_status()["home"]["connected"]

    await n.add_instance(_inst(fake_ha))            # same id, corrected token
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    st = n.listener_status()["home"]
    assert st["connected"] and st["error"] is None
    await n.close()


async def test_an_unchanged_instance_keeps_its_connection(fake_ha, ledger):
    n = _notifier({"home": _inst(fake_ha)}, ledger)
    n.listen()
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    before = set(fake_ha.clients)
    renamed = _inst(fake_ha)
    renamed.name = "Home"
    await n.add_instance(renamed)
    await asyncio.sleep(0.1)
    assert set(fake_ha.clients) == before and n.listener_status()["home"]["connected"]
    assert n.instances["home"].name == "Home"
    await n.close()


async def test_set_instances_adds_and_removes(fake_ha, ledger):
    n = _notifier({"home": _inst(fake_ha)}, ledger)
    n.listen()
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    await n.set_instances({"cabin": _inst(fake_ha, "cabin")})
    assert list(n.instances) == ["cabin"]
    assert list(n.listener_status()) == ["cabin"]
    assert await _until(lambda: n.listener_status()["cabin"]["connected"])
    await n.close()


async def test_after_close_adding_starts_no_listener(fake_ha, ledger):
    n = _notifier({}, ledger)
    n.listen()
    await n.close()
    await n.add_instance(_inst(fake_ha))
    assert n.listener_status() == {}


async def test_a_token_read_through_a_callable_restarts_only_when_it_changes(fake_ha, ledger):
    # A bridge passes a callable that reads its store, so the new token shows only
    # through bearer(), and every call builds a new callable.
    store = {"token": "nope"}

    def inst():
        return _inst(fake_ha, token=lambda: store["token"])
    n = _notifier({"home": inst()}, ledger)
    n.listen()
    assert await _until(lambda: n.listener_status()["home"]["error"])
    store["token"] = TOKEN
    await n.add_instance(inst())
    await asyncio.wait_for(fake_ha.subscribed.wait(), 5)
    before = set(fake_ha.clients)
    await n.add_instance(inst())                    # same token, new callable: kept
    await asyncio.sleep(0.1)
    assert set(fake_ha.clients) == before and n.listener_status()["home"]["connected"]
    await n.close()
