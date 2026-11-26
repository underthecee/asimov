"""The village map (plan/t6-map-spec.md in the shared folder): layout, walking, who is together, and the
rules that follow from travel. Bars 3, 4, 5 and 9 of the spec's acceptance live here."""
import json
import os
import statistics
import subprocess
import sys

import pytest

from asiimov.events import Event
from asiimov.invariants import Checker, presence_violations
from asiimov.memory import assemble_prompt
from asiimov.minds import MindReply, ReplayDivergence, StubMind
from asiimov.params import generate_population
from asiimov.replay import replay
from asiimov.sim import DEFAULT_STAKES, TICKS_PER_DAY, WORKPLACE, Sim
from asiimov.worldmap import WALK, build_map, position, travel_ticks

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Scripted(StubMind):
    """Deliberates with `answer(req)`; plans and reflects like the stub."""

    def __init__(self, answer):
        self.answer = answer

    def decide(self, req):
        if req.kind == "deliberate":
            return MindReply(json.dumps({"reason": "x", "goal": "", "then": [], **self.answer(req)}), "scripted", {})
        return super().decide(req)


def awake(sim, place_of: dict):
    """Only the given agents awake, at the given places (a home name, or a public place)."""
    for a in sim.state["agents"]:
        a.update(asleep=True, wake_tick=10**6, sleep_tick=None)
    out = []
    for i, place in place_of.items():
        a = sim.state["agents"][i]
        a.update(asleep=False, wake_tick=0, sleep_tick=10**6, plan=[], habit="home", activity="home",
                 place=place, route=None)
        out.append(a)
    return out


def walk(sim, a, place, activity="explore"):
    sim._set_activity(a, activity, place, [])
    assert a["place"] is None and a["route"]["to"] == place


# ------------------------------------------------------------------ layout (bar 5)
@pytest.mark.parametrize("agents,families,table", [
    (100, 32, {"plaza": (1, 2), "market": (1, 2), "field": (2, 3), "woods": (2, 3)}),
    (300, 103, {"plaza": (2, 3), "market": (2, 3), "field": (3, 4), "woods": (3, 4)}),
    (1000, 335, {"plaza": (2, 4), "market": (2, 5), "field": (4, 7), "woods": (4, 6)}),
])
def test_travel_times_are_the_specs_table(agents, families, table):
    pop = generate_population(1, agents)
    assert len(pop["families"]) == families
    places = build_map([f["home"] for f in pop["families"]])["places"]
    homes = [places[f["home"]]["anchor"] for f in pop["families"]]
    for p, (median, longest) in table.items():
        trips = [travel_ticks(h, places[p]["anchor"]) for h in homes]
        assert (statistics.median(trips), max(trips)) == (median, longest), p
    assert travel_ticks(places["plaza"]["anchor"], places["market"]["anchor"]) == 1


def test_no_two_lots_share_a_tile_and_the_layout_is_in_genesis():
    sim = Sim(1, 300, StubMind())
    anchors = [tuple(p["anchor"]) for p in sim.map["places"].values()]
    assert len(anchors) == len(set(anchors))
    assert sim.events[0].data["map"] == build_map([f["home"] for f in sim.state["families"]])


def test_a_log_with_another_map_is_refused():
    sim = Sim(1, 20, StubMind())
    sim.run(1)
    sim.finish()
    events = [Event(**json.loads(e.to_json())) for e in sim.events]
    events[0].data["map"]["places"]["market"]["anchor"] = [30, 0]
    with pytest.raises(ReplayDivergence):
        replay(events)


# ------------------------------------------------------------------ walking
def test_a_walk_takes_its_travel_time_and_ends_with_an_arrive_event():
    sim = Sim(1, 100, StubMind())
    (a,) = awake(sim, {0: "plaza"})
    walk(sim, a, "woods")
    start = sim.events[-1]
    ticks = travel_ticks(sim.map["places"]["plaza"]["anchor"], sim.map["places"]["woods"]["anchor"])
    assert ticks > 1 and start.data == {"activity": "explore", "place": "woods", "arrive": start.tick + ticks,
                          "from": sim.map["places"]["plaza"]["anchor"]}
    a["plan"] = [{"activity": "explore", "start_h": 0, "end_h": 24}]
    while sim.state["tick"] < start.data["arrive"]:
        sim.step()
        assert a["place"] is None
    sim.step()
    arrive = next(e for e in sim.events if e.kind == "arrive" and e.actor == 0)
    assert arrive.tick == start.data["arrive"] and arrive.parents == [start.id] and arrive.data == {"place": "woods"}


def test_turning_mid_walk_starts_from_where_the_walker_is():
    sim = Sim(1, 100, StubMind())
    (a,) = awake(sim, {0: "woods"})
    walk(sim, a, "field")  # 4 ticks south along x = 0
    places = sim.map["places"]
    sim.state["tick"] += 2
    here = position(a["route"], places["field"]["anchor"], sim.state["tick"])
    assert here == [0, places["woods"]["anchor"][1] + 2 * WALK]
    sim._set_activity(a, "work", "market", [])
    assert a["route"]["from"] == here and a["route"]["arrive"] == sim.state["tick"] + travel_ticks(here, places["market"]["anchor"])


def test_work_counts_only_at_the_workplace():
    sim = Sim(1, 20, StubMind())
    worker = next(a for a in sim.state["agents"] if a["role"] in WORKPLACE and a["role"] not in ("child", "elder"))
    awake(sim, {worker["id"]: worker["home"]})
    worker["plan"] = [{"activity": "work", "start_h": 0, "end_h": 24}]
    sim.step()
    assert worker["place"] is None and worker["work_progress"] == 0
    while worker["place"] is None:
        sim.step()
    progress = worker["work_progress"]
    sim.step()
    assert worker["work_progress"] > progress or any(e.kind == "produce" for e in sim.events)


def test_bedtime_away_from_home_walks_home_then_sleeps_there():
    sim = Sim(1, 20, StubMind())
    (a,) = awake(sim, {0: "woods"})
    a["sleep_tick"] = sim.state["tick"]
    sim.step()
    assert a["route"]["to"] == a["home"] and not a["asleep"]
    assert not any(e.kind == "mind_reflect" for e in sim.events)
    while not a["asleep"]:
        sim.step()
    reflect = next(e for e in sim.events if e.kind == "mind_reflect")
    assert a["place"] == a["home"]
    assert reflect.tick == next(e for e in sim.events if e.kind == "arrive").tick  # reflects on arrival


# ------------------------------------------------------------------ together (bars 4 and 9)
PERSON_ACTIONS = ["steal", "ask_food", "buy_from", "ask_kin"]


@pytest.mark.parametrize("action", PERSON_ACTIONS)
@pytest.mark.parametrize("who_walks", ["actor", "target", "both"])
def test_a_person_action_needs_both_arrived_at_one_place(action, who_walks):
    sim = Sim(3, 30, StubMind())
    fam = next(f for f in sim.state["families"] if len(f["members"]) >= 2)
    i, j = fam["members"][:2]
    a, t = awake(sim, {i: "plaza", j: "plaza"})
    a["inv"]["coin"], t["inv"]["food"] = 30, 5
    for x in {"actor": [a], "target": [t], "both": [a, t]}[who_walks]:
        walk(sim, x, "market")  # both walkers head the same way: still not together
    sim._resolve(a, {"action": action, "target": t["id"]}, 0)
    assert sim.events[-1].data["why"] == "target not here" and t["inv"]["food"] == 5


@pytest.mark.parametrize("action", PERSON_ACTIONS + ["give_food"])
def test_presence_is_checked_when_the_reply_applies_not_when_it_was_asked(action):
    """Steering's apply-time bar: the target, later in id order, starts walking in the rules phase of the
    tick the actor asked in. The reply commits after that and must fail through the together helper."""
    sim = Sim(3, 30, Scripted(lambda req: {"action": action, "target": req.view["kin"][0]["id"]}))
    fam = next(f for f in sim.state["families"] if len(f["members"]) >= 2)
    i, j = sorted(fam["members"][:2])
    a, t = awake(sim, {i: "plaza", j: "plaza"})
    a["inv"].update(food=0, coin=30)
    a["needs"]["hunger"], a["cooldown_until"] = 1000, 0
    t["inv"]["food"], t["needs"]["hunger"] = 5, 0
    hour = sim._hour()
    t["plan"] = [{"activity": "work" if t["role"] not in ("child", "elder") else "explore",
                  "start_h": hour, "end_h": hour + 1}]
    if action == "give_food":  # a reach step: it waits for the target instead of failing at once
        a["inv"]["food"] = 2
        a["occasion"] = {"why": "gift", "salience": 1000, "who": j, "event": None, "detail": "", "tick": 0}
    sim.step()
    asked = next(e for e in sim.events if e.kind == "mind_deliberate")
    assert t["place"] is None  # it set out after the request was built, before the commit
    receipt = next(e for e in sim.events if e.kind == "receipt" and e.parents == [asked.id])
    if action == "give_food":
        assert receipt.data["why"] == "started" and not any(
            e.kind == "receipt" and e.data["action"] == "give_food" and e.data["why"] != "started" for e in sim.events)
    else:
        assert receipt.data["why"] == "target not here"
    assert t["inv"]["food"] == 5


def test_walkers_dont_talk_notice_or_show_up_here():
    sim = Sim(3, 30, StubMind())
    agents = awake(sim, {i: "plaza" for i in range(6)})
    for x in agents[:4]:
        walk(sim, x, "woods", "socialize")
    for x in agents:
        x["traits"]["extraversion"] = 1000
    sim._talk()
    assert not any(e.kind == "talk" and (e.actor < 4 or e.data["with"] < 4) for e in sim.events)
    occupancy = {"plaza": [4, 5]}
    view = sim._view(agents[0], occupancy)
    assert view["present"] == [] and sim._notice(agents[0], occupancy) is None


def test_the_presence_check_catches_a_planted_talk_between_walkers():
    """Test the test (S6): the log-only check flags what the engine must never do."""
    events = [Event(0, 0, "activity", "rule", 0, {"activity": "socialize", "place": "woods", "arrive": 3}, []),
              Event(1, 0, "activity", "rule", 1, {"activity": "socialize", "place": "woods", "arrive": 3}, []),
              Event(2, 0, "talk", "chance", 0, {"with": 1, "place": "woods"}, []),
              Event(3, 0, "receipt", "rule", 0, {"action": "buy_food", "ok": True, "why": "bought"}, [])]
    out = presence_violations(events, 0, {0: "plaza", 1: "plaza"})
    assert len(out) == 2 and "talk" in out[0] and "market" in out[1]


def test_a_stub_world_never_meets_away_from_a_shared_place():
    # pay off: compressed pay (pay-1) lifts the slowest earners, so nobody asks kin in 3 days on seeds 1 to 6,
    # and the check must still see an ask_kin receipt
    sim = Sim(1, 60, StubMind(), genesis={**generate_population(1, 60), "stakes": {"tithe": DEFAULT_STAKES["tithe"]}})
    check = Checker(sim)
    bad = []
    for tick in range(3 * TICKS_PER_DAY):
        bad += check.step([{"kind": "fire", "family": 3}] if tick == TICKS_PER_DAY + 60 else [])
    assert bad == []
    kinds = {e.kind for e in sim.events}
    assert {"arrive", "talk", "meet"} <= kinds
    assert any(e.kind == "receipt" and e.data["action"] == "ask_kin" for e in sim.events)


# ------------------------------------------------------------------ prompts (bar 8)
def test_a_walkers_deliberate_prompt_says_where_it_is_going():
    sim = Sim(3, 30, StubMind())
    (a,) = awake(sim, {0: "plaza"})
    walk(sim, a, "market", "shop")
    view = sim._view(a, {"plaza": [0]})
    sim._request(a, "deliberate", view, [], lambda eid, out: None)
    prompt = sim.pending[0][0].prompt
    assert "You are on your way to the market. " in prompt and "Here: nobody." in prompt
    assert "ask_kin" not in sim.pending[0][0].view["offer"]


def test_ask_kin_is_offered_only_with_family_here_and_says_so():
    sim = Sim(3, 30, StubMind())
    fam = next(f for f in sim.state["families"] if len(f["members"]) >= 2)
    i, j = fam["members"][:2]
    a, t = awake(sim, {i: "plaza", j: "plaza"})
    occupancy = {"plaza": [i, j]}
    sim._request(a, "deliberate", sim._view(a, occupancy), [], lambda eid, out: None)
    req = sim.pending.pop(i)[0]
    assert "ask_kin" in req.view["offer"] and "ask_kin asks a family member here for 1 food" in req.prompt
    occupancy = {"plaza": [i]}
    walk(sim, t, "field")
    sim._request(a, "deliberate", sim._view(a, occupancy), [], lambda eid, out: None)
    assert "ask_kin" not in sim.pending.pop(i)[0].view["offer"]


def test_plan_and_reflect_prompts_at_a_place_have_no_map_text():
    sim = Sim(3, 30, StubMind())
    (a,) = awake(sim, {0: "plaza"})
    a["sleep_tick"] = 100
    for kind in ("plan", "reflect"):
        prompt, _ = assemble_prompt(a, sim.state, kind, sim._situation(a, sim._view(a, {"plaza": [0]}), kind))
        assert "You are at plaza. " in prompt and "on your way" not in prompt and "tile" not in prompt


# ------------------------------------------------------------------ determinism (bar 3)
def test_a_map_world_is_the_same_under_every_hash_seed():
    code = ("from asiimov.sim import Sim; from asiimov.minds import StubMind; import hashlib; "
            "s = Sim(4, 60, StubMind()); s.run(2, {200: [{'kind': 'fire', 'family': 1}]}); s.finish(); "
            "print(hashlib.sha256(''.join(e.to_json() for e in s.events).encode()).hexdigest())")
    out = set()
    for h in range(1, 9):
        env = dict(os.environ, PYTHONHASHSEED=str(h))
        out.add(subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                               text=True, check=True).stdout.strip())
    assert len(out) == 1, out


def test_two_walkers_on_one_route_stay_apart_through_step():
    """The step loop's occupancy holds arrivals only (review nit on PR #30): two villagers walking the same
    way are listed nowhere and don't talk until they arrive."""
    sim = Sim(1, 100, StubMind())
    a, b = awake(sim, {0: "woods", 1: "woods"})
    for x in (a, b):
        x["traits"]["extraversion"] = 1000
        x["plan"] = [{"activity": "socialize", "start_h": 0, "end_h": 24}]  # the plaza, 2+ ticks away
    seen = []  # the occupancy the step hands to every rule (views, notice, loneliness)
    body = sim._body
    sim._body = lambda x, occupancy: (seen.append((x["id"], occupancy)), body(x, occupancy))
    sim.step()
    arrive = a["route"]["arrive"]
    assert b["route"]["arrive"] == arrive > sim.state["tick"]
    while sim.state["tick"] < arrive:
        n0, seen[:] = len(sim.events), []
        sim.step()
        assert not any(e.kind in ("talk", "meet", "snub") for e in sim.events[n0:])
        for aid, occupancy in seen:
            if aid in (0, 1):
                assert None not in occupancy and sim._view(sim.state["agents"][aid], occupancy)["present"] == []
    sim.step()
    assert a["place"] == b["place"] == "plaza"


def test_a_shop_step_waits_until_it_reaches_the_market():
    sim = Sim(1, 100, StubMind())
    (a,) = awake(sim, {0: "woods"})
    a["inv"].update(food=0, coin=30)
    sim.state["market_food"] = 50
    eid = sim._emit("mind_deliberate", "mind", 0, {"kind": "deliberate"}, [], mind={})
    sim._resolve(a, {"action": "go_market", "target": None, "goal": "eat", "then": [{"do": "rest", "target": None}]}, eid)
    sim.step()
    arrive = a["route"]["arrive"]
    while sim.state["tick"] < arrive:
        sim.step()
        assert a["intent"]["i"] == 0 and not any(e.kind == "receipt" and e.data["action"] == "buy_food" for e in sim.events)
    sim.step()
    buy = next(e for e in sim.events if e.kind == "receipt" and e.data["action"] == "buy_food")
    assert buy.data["ok"] and buy.tick == arrive and a["intent"]["i"] == 1


@pytest.mark.parametrize("families", [1, 7, 16, 25, 40, 60, 100])
def test_no_drawn_road_tile_lies_inside_a_place_it_doesnt_lead_to(families):
    """The client draws a dirt path from the plaza's anchor straight to the anchor of each place that isn't a
    home, about a tile wide (client/godot/scripts/main.gd, _draw_ground). The engine has no roads, so this pins
    the layout the drawing leans on: every tile such a path covers is open ground or one of its two ends, never
    a home's lot, at any size of village. (The dry run of 2026-11-26 reported a road inside two homes; the
    tester had walked on the lots beside it.)"""
    from asiimov.worldmap import place_at
    m = build_map([f"home_{i}" for i in range(families)])
    hub = m["places"]["plaza"]["anchor"]
    roads = [n for n in m["places"] if n != "plaza" and not n.startswith("home")]
    assert sorted(roads) == ["field", "market", "woods", "workshop"]
    for name in roads:
        (x0, y0), (x1, y1) = hub, m["places"][name]["anchor"]
        assert x0 == x1 or y0 == y1, "the path is drawn straight: its two anchors share a row or a column"
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                # the line runs along the tiles' shared edge, so it covers the tile on each side of it
                for tile in ([x, y], [x - 1, y] if x0 == x1 else [x, y - 1]):
                    assert place_at(m, tile) in (None, "plaza", name), (name, tile, place_at(m, tile))
