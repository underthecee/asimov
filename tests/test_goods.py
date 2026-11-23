"""goods-1a (plan/goods-1-design.md; Steering's rulings R1-R6, 2026-11-23): a trade's input is a good.

Seed (the farmers') and tools (the crafters') are counts in inv. A finished unit of a supplying trade's work makes
one while it holds fewer than `hold`; a working day's first tick of work uses one up; a supply run buys up to `ahead`
days' worth from what the supplier holds, in one `deal` the buyer's receipt cites; a supplier with none sells none."""
import json

import pytest

from asiimov.events import Event
from asiimov.invariants import Checker, deal_violations, goods_totals, goods_violations
from asiimov.minds import StubMind
from asiimov.params import generate_population
from asiimov.replay import replay
from asiimov.sim import DEFAULT_STAKES, TICKS_PER_DAY, TRADE_GOOD, WORKPLACE, Sim

TR = DEFAULT_STAKES["trade"]


def _world(days, seed=3, n=100):
    sim = Sim(seed, n, StubMind())
    check, bad = Checker(sim), []
    for _ in range(days * TICKS_PER_DAY):
        bad += check.step()
    return sim, bad


@pytest.fixture(scope="module")
def week():
    return _world(7)


def test_genesis_gives_each_working_adult_its_input_and_each_supplier_its_opening_stock():
    sim = Sim(1, 100, StubMind())
    for a in sim.state["agents"]:
        want = {g: 0 for g in TRADE_GOOD.values()}
        for need in sim._trade_needs(a):
            want[TRADE_GOOD[need]] += TR["first"]
        if a["role"] in TRADE_GOOD:
            want[TRADE_GOOD[a["role"]]] += TR["start"]
        assert {g: a["inv"][g] for g in want} == want, a["role"]
    assert goods_violations(sim) == [] and sum(goods_totals(sim).values()) > 0
    assert all(a["inv"]["seed"] == a["inv"]["tools"] == 0 for a in sim.state["agents"] if a["role"] in ("child", "elder"))


def test_a_world_without_the_trade_stake_holds_no_goods():
    pop = generate_population(1, 30)
    sim = Sim(1, 30, StubMind(), genesis={**pop, "stakes": {k: v for k, v in DEFAULT_STAKES.items()
                                                           if k not in ("trade", "neighbor")}})
    sim.run(2)
    assert all(set(a["inv"]) == {"food", "coin"} for a in sim.state["agents"])
    assert not [e for e in sim.events if e.kind in ("made", "used", "deal", "trade_due")]


def test_a_week_of_goods_keeps_every_ledger_and_replays(week):
    sim, bad = week
    assert bad == []
    kinds = [e.kind for e in sim.events]
    assert min(kinds.count(k) for k in ("made", "used", "deal")) > 100  # not vacuous
    assert any(e.kind == "receipt" and e.data["why"] == "none to sell" for e in sim.events)
    assert replay(sim.events + [Event(len(sim.events), sim.state["tick"], "end", "rule", None,
                                      {"hash": sim.state_hash()}, [])]).state_hash() == sim.state_hash()


def test_goods_are_made_only_at_work_by_the_trade_that_makes_them_and_used_once_a_day(week):
    sim, _ = week
    role = {a["id"]: a["role"] for a in sim.state["agents"]}
    needs = {a["id"]: {TRADE_GOOD[n] for n in sim._trade_needs(a)} for a in sim.state["agents"]}
    used = set()
    for e in sim.events:
        if e.kind == "made":
            assert e.origin == "rule" and e.data == {TRADE_GOOD[role[e.actor]]: TR["make"]}
        elif e.kind == "used":
            (g, n), = e.data.items()
            key = (e.actor, g, e.tick // TICKS_PER_DAY)
            assert n == 1 and g in needs[e.actor] and key not in used, key
            used.add(key)
    # it follows the work, not the yield: farmers make seed on a winter day, when a unit of farm work yields no food
    winter = [d for d in range(7) if sim._winter(d)]
    assert winter and any(e.kind == "made" and "seed" in e.data and e.tick // TICKS_PER_DAY in winter for e in sim.events)
    assert not any(e.kind == "produce" and "food" in e.data and e.tick // TICKS_PER_DAY in winter for e in sim.events)


def _at_work(sim, role):
    sim.run_until(TICKS_PER_DAY + 66)
    return next(a for a in sim.state["agents"] if a["role"] == role and a["activity"] == "work"
                and a["place"] == WORKPLACE[role] and a["override"] is None and a["intent"] is None
                and str(a["id"]) not in sim.state["sick"]["ill"])


def test_a_supplier_makes_nothing_more_once_it_holds_the_cap():
    sim = Sim(1, 100, StubMind())
    a = _at_work(sim, "crafter")
    a["inv"]["tools"], a["work_progress"] = TR["hold"], 2999
    n0 = len(sim.events)
    sim._act(a)
    assert a["inv"]["tools"] == TR["hold"] and not [e for e in sim.events[n0:] if e.kind == "made"]
    assert [e for e in sim.events[n0:] if e.kind == "produce" and e.actor == a["id"]]  # the wage is still earned
    a["inv"]["tools"], a["work_progress"] = TR["hold"] - 1, 2999
    n0 = len(sim.events)
    sim._act(a)
    assert a["inv"]["tools"] == TR["hold"] and [e.data for e in sim.events[n0:] if e.kind == "made"] == [{"tools": 1}]


def test_the_days_first_tick_of_work_uses_one_unit_and_a_sick_villager_keeps_its_own():
    sim = Sim(1, 100, StubMind())
    a = _at_work(sim, "farmer")
    day = sim.state["tick"] // TICKS_PER_DAY
    a["inv"]["tools"], a["used"] = 2, {}
    n0 = len(sim.events)
    sim._act(a)
    sim._act(a)  # a second tick of work the same day uses nothing more
    assert a["inv"]["tools"] == 1 and a["used"] == {"crafter": day}
    assert [(e.actor, e.data) for e in sim.events[n0:] if e.kind == "used"] == [(a["id"], {"tools": 1})]
    a["inv"]["tools"], a["used"] = 1, {}
    well = sim._work_rate(a)
    sim._make_sick(a, [])
    n0 = len(sim.events)
    sim._act(a)
    assert a["inv"]["tools"] == 1 and a["used"] == {} and not [e for e in sim.events[n0:] if e.kind == "used"]
    assert sim._supplied(a) and sim._work_rate(a) == well // 2  # sick at half pace, but not short of its input


@pytest.mark.parametrize("held,in_use,coin,stock,n", [
    (0, 0, 50, 5, 2),   # nothing for today: today's and tomorrow's
    (1, 0, 50, 5, 1),   # today's in hand: tomorrow's
    (0, 1, 50, 5, 1),   # today's in use: tomorrow's
    (0, 0, 1, 5, 1),    # as far as its coin goes
    (0, 0, 50, 1, 1),   # as far as the supplier's stock goes
])
def test_a_run_buys_up_to_two_days_ahead_within_its_coin_and_the_suppliers_stock(held, in_use, coin, stock, n):
    sim = Sim(1, 100, StubMind())
    sim.run_until(TICKS_PER_DAY + 60)
    day = sim.state["tick"] // TICKS_PER_DAY
    a = next(a for a in sim.state["agents"] if a["role"] == "farmer" and not a["asleep"] and a["override"] is None)
    b = next(b for b in sim.state["agents"] if b["role"] == "crafter" and b["family"] != a["family"])
    a["inv"]["tools"], a["used"], a["inv"]["coin"], b["inv"]["tools"] = held, {"crafter": day} if in_use else {}, coin, stock
    a.setdefault("supply_failed", {})["crafter"] = 0
    due = sim._emit("trade_due", "rule", a["id"], {"to": b["id"], "need": "crafter"}, [])
    coin_b = b["inv"]["coin"]
    r = sim.events[sim._trade(a, b, due)]
    assert r.data["ok"] and r.data["coin"] == n * TR["price"]
    assert (a["inv"]["tools"], b["inv"]["tools"]) == (held + n, stock - n)
    assert (a["inv"]["coin"], b["inv"]["coin"]) == (coin - n * TR["price"], coin_b + n * TR["price"])
    assert deal_violations(sim.events) == [] and goods_violations(sim) == []


def test_a_supplier_with_none_sells_none_and_the_buyer_turns_to_one_that_holds_a_unit():
    sim = Sim(1, 100, StubMind())
    sim.run_until(TICKS_PER_DAY + 60)
    ag = sim.state["agents"]
    a = next(a for a in ag if a["role"] == "farmer" and not a["asleep"] and a["override"] is None
             and a.get("suppliers", {}).get("crafter") is not None)
    b = ag[a["suppliers"]["crafter"]]
    a["inv"]["tools"], a["used"], a["inv"]["coin"], b["inv"]["tools"] = 0, {}, 50, 0
    due = sim._emit("trade_due", "rule", a["id"], {"to": b["id"], "need": "crafter"}, [])
    before = (a["inv"]["coin"], b["inv"]["coin"], len(sim.events))
    a["occasion"] = None
    r = sim.events[sim._trade(a, b, due)]
    assert r.data == {"action": "trade", "ok": False, "why": "none to sell", "to": b["id"]} and r.parents == [due]
    assert a["occasion"] is None  # the rule re-chooses; no mind is asked about it, so no prompt gains the words
    assert (a["inv"]["coin"], b["inv"]["coin"]) == before[:2] and a["inv"]["tools"] == 0
    assert not [e for e in sim.events[before[2]:] if e.kind == "deal"]
    sim._trade_ended(a, "crafter", False, "none to sell")  # it counts against the supplier, as "no coin" does
    assert a["supply_failed"]["crafter"] == 1
    # the next choice: a crafter that holds a unit comes before one with fewer customers that holds none
    others = [o for o in ag if o["role"] == "crafter" and o["family"] != a["family"] and o["id"] != b["id"]]
    stocked = max(others, key=lambda o: (sim.state["trade_load"].get(str(o["id"]), 0), o["id"]))
    for o in others:
        o["inv"]["tools"] = 1 if o is stocked else 0
    assert sim._supplier(a, "crafter")["id"] == stocked["id"]
    # while the supplier is sick, the failed run cites its sickness: a sickness can be traced into a shortage
    sick = sim._make_sick(stocked, [])
    stocked["inv"]["tools"] = 0
    due2 = sim._emit("trade_due", "rule", a["id"], {"to": stocked["id"], "need": "crafter"}, [])
    assert sim.events[sim._trade(a, stocked, due2)].parents == [due2, sick]


def test_the_players_body_with_none_to_sell_is_not_asked_and_with_stock_its_sale_is_a_deal():
    for stock in (0, 3):
        sim = Sim(1, 100, StubMind())
        sim.run_until(TICKS_PER_DAY + 60)
        ag = sim.state["agents"]
        b = next(b for b in ag if b["role"] == "crafter" and not b["asleep"] and b["place"] not in (None, b["home"]))
        sim.step([{"kind": "possess", "agent": b["id"]}])
        a = next(a for a in ag if a["role"] == "farmer" and a["family"] != b["family"] and not a["asleep"])
        a["place"], a["route"] = b["place"], None
        a["inv"]["coin"], a["override"], a["intent"] = 50, None, None
        a["inv"]["tools"], a["used"], a["trade_try"], b["inv"]["tools"] = 0, {}, {}, stock
        a.setdefault("suppliers", {})["crafter"] = b["id"]
        a.setdefault("supply_failed", {})["crafter"] = 0
        sim._trade_due(a)
        n0 = len(sim.events)
        sim.step([{"kind": "act", "action": "wait"}])
        asks = [e for e in sim.events[n0:] if e.kind == "ask" and e.actor == a["id"]]
        if not stock:
            r = next(e for e in sim.events[n0:] if e.kind == "receipt" and e.actor == a["id"] and e.data["action"] == "trade")
            assert not asks and r.data["why"] == "none to sell" and a["override"] is None
            continue
        assert len(asks) == 1
        sim.step([{"kind": "act", "action": "agree", "target": a["id"]}])
        d = next(e for e in reversed(sim.events) if e.kind == "deal")
        assert d.data["from"] == b["id"] and d.data["to"] == a["id"] and d.data["tools"] == TR["ahead"]
        assert asks[0].id in d.parents and b["inv"]["tools"] == stock - TR["ahead"]
        assert deal_violations(sim.events, 0, b["id"]) == []


def test_the_supply_deal_check_catches_a_tampered_deal(week):
    sim, _ = week
    evs = sim.events
    d = next(e for e in evs if e.kind == "deal")
    assert deal_violations(evs) == []

    def tampered(i, **change):
        e = evs[i]
        fields = {"data": e.data, "actor": e.actor, "parents": e.parents, **change}
        out = list(evs)
        out[i] = Event(e.id, e.tick, e.kind, e.origin, fields["actor"], fields["data"], fields["parents"])
        return deal_violations(out)

    good = next(g for g in TRADE_GOOD.values() if g in d.data)
    other = next(g for g in TRADE_GOOD.values() if g != good)
    assert tampered(d.id, data={**d.data, "coin": d.data["coin"] + 1})  # coin that isn't the price a unit
    assert tampered(d.id, data={**{k: v for k, v in d.data.items() if k != good}, other: d.data[good]})  # not its good
    assert tampered(d.id, data={**d.data, good: 0})  # nothing moved
    assert tampered(d.id, actor=d.data["from"])  # not the buyer's act
    assert tampered(d.id, parents=[])  # no run behind it
    assert tampered(d.id + 1, data={**evs[d.id + 1].data, "coin": d.data["coin"] + 1})  # the receipt says another sum
    assert tampered(d.id + 1, parents=[p for p in evs[d.id + 1].parents if p != d.id])  # the receipt doesn't cite it


@pytest.mark.parametrize("bad", [{"hold": 0}, {"ahead": 0}, {"start": TR["hold"] + 1}, {"first": TR["ahead"] + 1},
                                 {"make": -1}, {"needs": {"farmer": ["guard"]}}, {"hold": None}])
def test_trade_stakes_that_cannot_hold_are_refused(bad):
    stakes = json.loads(json.dumps(DEFAULT_STAKES))
    stakes["trade"].update(bad)
    if bad.get("hold", 0) is None:
        del stakes["trade"]["hold"]
    with pytest.raises(ValueError, match="trade:"):
        Sim(1, 20, StubMind(), genesis={**generate_population(1, 20), "stakes": stakes})
