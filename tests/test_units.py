import asyncio
import sqlite3
import time

import pytest

from franklinwh_ha_notify import (NO, YES, Button, Event, Ledger, Requests, Target, Templates,
                                  action_id, discover, friendly, parse, payload, send)
from franklinwh_ha_notify.listener import tap_from_event


# --- the button contract ---------------------------------------------------------

def test_action_ids_round_trip_with_the_request_id_inside():
    rid = "0123456789abcdef"
    assert action_id("YES", rid) == "FWH_YES_0123456789abcdef"
    assert parse(action_id("CHOICE_2", rid)) == ("CHOICE_2", rid)


@pytest.mark.parametrize("action", ["SNOOZE_15", "FWH_YES", "FWH_yes_0123456789abcdef",
                                    "FWH_YES_0123", "", "URI"])
def test_phone_side_and_malformed_actions_are_not_ours(action):
    assert parse(action) is None
    assert tap_from_event("home", {"action": action}) is None


def test_verbs_must_be_upper_case():
    with pytest.raises(ValueError):
        action_id("yes", "0123456789abcdef")


# --- requests -----------------------------------------------------------------------

def _two():
    return [Button(YES, "Yes"), Button(NO, "No")]


def test_a_request_is_accepted_once():
    r = Requests()
    req = r.open("release", _two(), ttl_s=60, gateway="gw1")
    out = r.resolve(YES, req.id)
    assert out.status == "accepted" and out.answer.yes and out.request.gateway == "gw1"
    assert r.resolve(YES, req.id).status == "used"


def test_no_is_an_answer_not_a_timeout():
    r = Requests()
    req = r.open("release", _two(), ttl_s=60)
    out = r.resolve(NO, req.id)
    assert out.status == "accepted" and out.answer.verb == NO and not out.answer.timed_out


def test_unknown_expired_and_not_offered():
    now = [1000.0]
    r = Requests(clock=lambda: now[0])
    req = r.open("release", [Button(YES, "Yes")], ttl_s=60)
    assert r.resolve(YES, "ffffffffffffffff").status == "unknown"
    assert r.resolve("RELEASE", req.id).status == "not-allowed"
    now[0] += 61
    assert r.resolve(YES, req.id).status == "expired"


async def test_wait_returns_the_answer_or_times_out():
    r = Requests()
    req = r.open("ask", _two(), ttl_s=60)
    asyncio.get_running_loop().call_later(0.05, r.resolve, YES, req.id)
    assert (await r.wait(req.id, 2)).yes
    req2 = r.open("ask", _two(), ttl_s=60)
    ans = await r.wait(req2.id, 0.05)
    assert ans.timed_out and ans.verb is None


async def test_an_answer_before_anyone_waits_is_kept():
    r = Requests()
    req = r.open("ask", _two(), ttl_s=60)
    r.resolve(NO, req.id)
    assert (await r.wait(req.id, 0.05)).verb == NO


# --- templates ------------------------------------------------------------------------

def test_templates_render_override_and_reset():
    t = Templates()
    t.register(Event("soc_low", "Battery low on {gateway}", "SoC {soc}%", ("soc",)))
    assert t.render("soc_low", gateway="Home", soc=19.5) == ("Battery low on Home", "SoC 19.5%")
    t.set("soc_low", message="Only {soc}% left ({missing})")
    assert t.render("soc_low", gateway="Home", soc=19.0)[1] == "Only 19% left ({missing})"
    t.reset("soc_low")
    assert t.render("soc_low", soc=7)[1] == "SoC 7%"


def test_a_stray_brace_sends_the_text_as_written():
    t = Templates()
    t.register(Event("x", "T", "M"))
    t.set("x", message="50% {off")
    assert t.render("x")[1] == "50% {off"


# --- targets and sending ---------------------------------------------------------------

async def test_discovery_lists_phones_first_and_skips_the_ha_list(instance):
    targets = await discover(instance)
    assert [t.service for t in targets] == ["mobile_app_iphone", "mobile_app_pixel", "email_me"]
    assert [t.buttons for t in targets] == [True, True, False]
    assert friendly("mobile_app_davids_iphone") == "Davids Iphone"


def test_buttons_go_only_to_targets_that_can_show_them():
    b = [Button(YES, "Yes"), Button("REPLY", "Reply", reply=True)]
    p = payload("T", "M", b, "0123456789abcdef")
    assert p["data"]["actions"][0]["action"] == "FWH_YES_0123456789abcdef"
    assert p["data"]["actions"][1]["behavior"] == "textInput"
    assert "push" not in p["data"]
    assert "data" not in payload("T", "M", b, "0123456789abcdef", with_buttons=False)


async def test_one_failing_target_does_not_stop_the_others(instance, fake_ha):
    fake_ha.fail.add("mobile_app_pixel")
    ts = [Target("home", "mobile_app_pixel", "P"), Target("home", "email_me", "E"),
          Target("gone", "mobile_app_x", "X")]
    res = await send({"home": instance}, ts, "T", "M")
    assert (res.delivered, res.total, res.summary) == (1, 3, "1/3 delivered")
    assert [s for s, _ in fake_ha.sent] == ["email_me"]


# --- ledger -----------------------------------------------------------------------------

def test_ledger_keeps_days_not_rows():
    led = Ledger(sqlite3.connect(":memory:"), keep_days=30)
    led.record("SENT", "old", ts=time.time() - 31 * 86400)
    for i in range(150):
        led.record("SENT", f"e{i}", gateway="gw1", detail={"i": i})
    rows = led.recent(limit=1000)
    assert len(rows) == 150 and "old" not in {r["event"] for r in rows}
    assert rows[0]["detail"] == {"i": 149}
