"""The player walks its body tile by tile (walk-1, plan/player-body-spec.md "Walking").

`walk` with a tile `to` is a player-only act. The engine steps the body toward the tile, x first and then y,
at most WALK tiles a tick, holds the tile it stands on (a["at"]) and says which place that is: the one whose
rectangle holds the tile, or none in the open, where it is with nobody. A walk isn't the tick's one act, so a
step and an act can share a tick. The outcome is a `moved` event citing the act. A mind is never offered it,
and a world with no walk is engine 38's but for the engine field."""
import hashlib
import json
import random

import pytest

from asiimov import inhabitant
from asiimov.events import Event
from asiimov.invariants import Checker, state_violations
from asiimov.minds import StubMind
from asiimov.replay import replay
from asiimov.sim import DEFAULT_STEP_TICKS, DELIBERATE_ACTIONS, IDLE_TICKS, PLAYER_ACTIONS, TICKS_PER_DAY, Sim
from asiimov.worldmap import WALK, bounds, build_map, distance, on_map, place_at

GOLDEN_38 = "f0221edafa0e75fc34bf9ff766c09b507c6d2d1a09ea794227b152ca0bdae583"  # tests/test_world.py, engine 38


def awake_body(seed=4, n=14):
    """A world at midday with an awake, unwalking body possessed. Returns (sim, body id)."""
    sim = Sim(seed, n, StubMind())
    sim.run_until(TICKS_PER_DAY // 2)
    b = next(a for a in sim.state["agents"] if not a["asleep"] and a["place"] is not None
             and a["sleep_tick"] > sim.state["tick"] + 30)
    sim.step([{"kind": "possess", "agent": b["id"]}])
    return sim, b["id"]


def walk(x, y):
    return {"kind": "act", "action": "walk", "to": [x, y]}


def moved(sim, aid):
    return [e for e in sim.events if e.kind == "moved" and e.actor == aid]


def open_tile(sim):
    """A tile no place's rectangle holds, beside the plaza."""
    t = [0, 8]
    assert place_at(sim.map, t) is None
    return t


def walk_to(sim, tile, acts=()):
    """Walk the body to `tile`, a tick at a time, then run `acts` on the tick it gets there."""
    b = sim.state["agents"][sim.state["player"]["agent"]]
    while sim._pos(b) != list(tile):
        last = distance(sim._pos(b), tile) <= WALK
        sim.step([walk(*tile)] + (list(acts) if last else []))
        assert not state_violations(sim), sim.state["tick"]


def test_walk_is_the_players_alone():
    assert "walk" in PLAYER_ACTIONS and "walk" not in DELIBERATE_ACTIONS


def test_a_world_with_no_walk_is_engine_38_but_for_the_version_field(monkeypatch):
    from test_goods import on_38
    from test_world import FIRE
    on_38(monkeypatch)  # engine 38's trade stake: goods-1 came after and changes the log
    s = Sim(3, 20, StubMind())
    s.run(2, FIRE)
    s.finish()
    lines = [e.to_json() for e in s.events]
    g = json.loads(lines[0])
    g["data"]["engine"] = "38"
    lines[0] = json.dumps(g, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256("".join(l + "\n" for l in lines).encode()).hexdigest() == GOLDEN_38


def test_every_anchor_is_in_its_own_place_and_a_shared_tile_has_one_owner():
    for n in (1, 5, 28, 60):
        m = build_map([f"home_{i}" for i in range(n)])
        for name, p in m["places"].items():
            assert place_at(m, p["anchor"]) == name
        x0, y0, x1, y1 = bounds(m)
        shuffled = {"places": dict(reversed(list(m["places"].items())))}
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                assert place_at(m, [x, y]) == place_at(shuffled, [x, y])  # never the listing order
    assert not on_map(m, [x1 + 1, 0]) and not on_map(m, [0.0, 0]) and not on_map(m, [True, 0]) and not on_map(m, [0])


def test_a_step_lands_on_the_tile_and_the_engine_says_which_place_it_is():
    sim, b = awake_body()
    a = sim.state["agents"][b]
    here, frm = a["place"], sim._pos(a)
    rx, ry, w, h = sim.map["places"][here]["rect"]
    corner = [rx + w - 1, ry + h - 1]  # another tile of the place it is in
    sim.step([walk(*corner)])
    m = moved(sim, b)[-1]
    assert m.data == {"from": frm, "pos": corner, "place": here} and m.origin == "rule"
    act = sim.events[m.parents[0]]
    assert act.kind == "act" and act.origin == "player" and act.data == {"action": "walk", "to": corner}
    assert a["place"] == here and a["at"] == corner and a["route"] is None and sim._pos(a) == corner
    assert inhabitant.view(sim, b)["pos"] == corner and inhabitant.view(sim, b)["place"] == here
    assert not state_violations(sim)


def test_in_the_open_it_is_at_no_place_and_with_nobody():
    sim, b = awake_body()
    a = sim.state["agents"][b]
    tile = open_tile(sim)
    walk_to(sim, tile)
    assert a["place"] is None and a["route"] is None and a["at"] == tile
    assert all(not sim._together(a, o) for o in sim.state["agents"] if o is not a)
    v = inhabitant.view(sim, b)
    assert v["place"] is None and v["route"] is None and v["pos"] == tile and v["present"] == []
    n = len(sim.events)
    sim.step([{"kind": "act", "action": "ask_food", "target": (b + 1) % len(sim.state["agents"])}])
    r = next(e for e in sim.events[n:] if e.kind == "receipt" and e.actor == b)
    assert not r.data["ok"]  # nobody is here to ask


def test_it_walks_at_most_walk_tiles_a_tick_x_first_over_all_its_walks_that_tick():
    sim, b = awake_body(n=120)  # a big map: the far corner is more than a tick away
    a = sim.state["agents"][b]
    x0, y0, x1, y1 = bounds(sim.map)
    frm = sim._pos(a)
    far = max(([x0, y0], [x0, y1], [x1, y0], [x1, y1]), key=lambda c: distance(frm, c))
    assert abs(far[0] - frm[0]) > WALK // 2 and distance(frm, far) > 2 * WALK
    near = [frm[0] + (WALK // 2 if far[0] > frm[0] else -WALK // 2), frm[1]]
    sim.step([walk(*near), walk(*far), walk(*far)])  # half a tick's walk, then the rest of it, then nothing left
    first, second, third = moved(sim, b)[-3:]
    assert first.data["pos"] == near
    assert distance(near, second.data["pos"]) == WALK - WALK // 2 and second.data["from"] == near
    assert third.data["pos"] == third.data["from"] == second.data["pos"]  # the tick's tiles are spent
    assert distance(frm, sim._pos(a)) == WALK
    sim.step([walk(*far)])
    assert distance(frm, sim._pos(a)) == 2 * WALK  # a new tick, a new WALK tiles
    assert sim._pos(a)[1] == frm[1] or sim._pos(a)[0] == far[0]  # x first, then y
    assert not state_violations(sim)


def test_a_walk_and_an_act_share_a_tick_and_the_act_is_judged_where_it_now_stands():
    sim, b = awake_body()
    a = sim.state["agents"][b]
    a["inv"]["coin"], a["inv"]["food"] = 50, 0
    sim.state["market_food"] = max(sim.state["market_food"], 5)
    stall = sim.map["places"]["market"]["anchor"]
    stall = [stall[0] + 1, stall[1] + 1]
    food = a["inv"]["food"]
    n = len(sim.events)
    walk_to(sim, stall, acts=[{"kind": "act", "action": "buy_food"}])
    assert a["place"] == "market" and a["at"] == stall
    r = [e for e in sim.events[n:] if e.kind == "receipt" and e.actor == b and e.data["action"] == "buy_food"]
    assert len(r) == 1 and r[0].data["ok"] and a["inv"]["food"] > food
    assert not any(e.kind == "receipt" and e.data["why"] == "one act per tick" for e in sim.events[n:])


def test_villagers_see_it_where_it_stands():
    sim, b = awake_body()
    a = sim.state["agents"][b]
    other = next(o for o in sim.state["agents"] if o is not a and not o["asleep"] and o["place"] not in (None, a["place"]))
    rx, ry, w, h = sim.map["places"][other["place"]]["rect"]
    walk_to(sim, [rx, ry])
    if other["place"] == a["place"]:  # it may have left while the body walked over
        assert sim._together(a, other)
        assert b in [p["id"] for p in sim._view(other, {other["place"]: [other["id"], b]})["present"]]
    assert a["place"] == place_at(sim.map, [rx, ry])


def test_it_stands_there_for_a_steps_time_then_the_day_takes_over():
    sim, b = awake_body()
    a = sim.state["agents"][b]
    tile = open_tile(sim)
    walk_to(sim, tile)
    assert a["override"]["do"] == "roam" and a["activity"] == "visit"
    for _ in range(IDLE_TICKS - 2):  # no order yet for an hour: nothing moves it
        sim.step([])
        assert a["at"] == tile and a["place"] is None
        assert not state_violations(sim)
    for _ in range(DEFAULT_STEP_TICKS + IDLE_TICKS):
        sim.step([])
        assert not state_violations(sim), sim.state["tick"]
    assert "at" not in a and (a["route"] is not None or a["place"] is not None)  # its plan or its mind walked it on


def test_other_acts_work_from_the_open_and_replace_the_stand():
    sim, b = awake_body()
    a = sim.state["agents"][b]
    walk_to(sim, open_tile(sim))
    sim.step([{"kind": "act", "action": "go_to", "place": "woods"}])
    r = next(e for e in reversed(sim.events) if e.kind == "receipt" and e.actor == b)
    assert r.data["why"] == "heading there" and a["override"]["do"] == "go"
    sim.step([])
    assert a["route"] is not None and a["route"]["to"] == "woods" and "at" not in a
    assert a["route"]["from"] == open_tile(sim)  # it sets out from the tile it stood on
    while a["route"]:
        sim.step([])
    assert a["place"] == "woods"
    sim.step([walk(0, 8)])  # a walk takes it off a route it is on, from where it is
    sim.step([{"kind": "act", "action": "home"}])
    sim.step([])
    assert a["override"]["do"] == "home" and not state_violations(sim)


def test_a_walk_stops_a_route_where_the_body_is():
    sim, b = awake_body()
    a = sim.state["agents"][b]
    sim.step([{"kind": "act", "action": "go_to", "place": "woods" if a["place"] != "woods" else "field"}])
    sim.step([])
    assert a["route"] is not None
    sim.step([])
    at = sim._pos(a)
    sim.step([walk(*at)])  # stand here
    assert a["route"] is None and a["at"] == at and a["place"] == place_at(sim.map, at)
    assert moved(sim, b)[-1].data["from"] == at and not state_violations(sim)


def test_a_seeker_waits_while_the_body_is_in_the_open():
    sim, b = awake_body()
    a = sim.state["agents"][b]
    walk_to(sim, open_tile(sim))
    seeker = next(o for o in sim.state["agents"] if o is not a and not o["asleep"] and o["place"] is not None)
    seeker["override"] = {"do": "seek", "target": b, "event": seeker["plan_event"], "left": 6}
    was = seeker["place"]
    sim.step([walk(*open_tile(sim))])
    assert seeker["place"] == was and seeker["route"] is None  # nowhere to go to: it waits where it is
    assert not state_violations(sim)


def test_a_sleeper_and_a_body_at_bedtime_dont_walk():
    sim, b = awake_body()
    a = sim.state["agents"][b]
    while not a["asleep"]:
        late = sim.state["tick"] >= a["sleep_tick"] or a["needs"]["fatigue"] >= 1000
        n = len(sim.events)
        sim.step([walk(0, 8)])
        if late:
            r = next(e for e in sim.events[n:] if e.kind == "receipt" and e.actor == b and e.data["action"] == "walk")
            assert not r.data["ok"] and r.data["why"] == "bedtime"
            assert not any(e.kind == "moved" for e in sim.events[n:])
        assert not state_violations(sim), sim.state["tick"]
    assert a["place"] == a["home"]  # the world walked it home
    n = len(sim.events)
    sim.step([walk(0, 8)])
    r = next(e for e in sim.events[n:] if e.kind == "receipt" and e.actor == b)
    assert r.data == {"action": "walk", "ok": False, "why": "asleep"}


def test_release_leaves_it_standing_and_drops_the_stand():
    sim, b = awake_body()
    a = sim.state["agents"][b]
    tile = open_tile(sim)
    walk_to(sim, tile)
    sim.step([{"kind": "release"}])
    assert a["override"] is None and not state_violations(sim)
    for _ in range(3):
        sim.step([])
        assert not state_violations(sim)
    assert "at" not in a  # its own day walked it on, from the tile


def test_walking_away_from_an_asker_declines():
    from test_player_ask import scene
    sim, x, b, _ = scene("ask_food")
    p = sim.state["player"]
    for _ in range(4):
        if p.get("asks"):
            break
        sim.step([])
    assert p.get("asks"), "the scene's ask is waiting on the player"
    ask = p["asks"][0]["event"]
    n = len(sim.events)
    sim.step([walk(*open_tile(sim))])
    assert "asks" not in p
    r = next(e for e in sim.events[n:] if e.kind == "receipt" and e.actor == x and ask in e.parents)
    assert not r.data["ok"]


@pytest.mark.parametrize("act", [
    {"kind": "act", "action": "walk"},                                   # no tile
    {"kind": "act", "action": "walk", "to": [0]},                        # half a tile
    {"kind": "act", "action": "walk", "to": [0.5, 1]},                   # not whole
    {"kind": "act", "action": "walk", "to": [True, 1]},
    {"kind": "act", "action": "walk", "to": "plaza"},
    {"kind": "act", "action": "walk", "to": [10 ** 6, 0]},               # off the map
    {"kind": "act", "action": "walk", "to": [0, 0], "target": 1},        # a tile and a person
    {"kind": "act", "action": "work", "to": [0, 0]},                     # a tile on another act
    {"kind": "act", "action": "go_to", "place": "woods", "to": [0, 0]},
])
def test_a_bad_walk_is_refused_before_it_is_logged(act):
    sim, _ = awake_body()
    n = len(sim.events)
    with pytest.raises(ValueError):
        sim.step([act])
    assert all(e.kind != "act" for e in sim.events[n:])


def session(seed, agents=20, days=2):
    """A session of random walks and acts, some several to a tick, checked every tick."""
    rng = random.Random(seed)
    sim = Sim(seed, agents, StubMind())
    sim.run_until(TICKS_PER_DAY // 2)
    b = next(a["id"] for a in sim.state["agents"] if not a["asleep"])
    sim.step([{"kind": "possess", "agent": b}])
    x0, y0, x1, y1 = bounds(sim.map)
    check = Checker(sim)
    anchors = [p["anchor"] for p in sim.map["places"].values()]
    for _ in range(days * TICKS_PER_DAY):
        acts = []
        for _ in range(rng.choice((0, 0, 1, 1, 2))):
            ax, ay = rng.choice(anchors)
            to = rng.choice(([rng.randint(x0, x1), rng.randint(y0, y1)], [ax + rng.randint(-3, 3), ay + rng.randint(-3, 3)]))
            acts.append(walk(*to) if on_map(sim.map, to) else walk(ax, ay))
        if rng.random() < 0.3:
            action = rng.choice(DELIBERATE_ACTIONS + ("go_to",))
            act = {"kind": "act", "action": action}
            if action == "go_to":
                act["place"] = rng.choice(sorted(sim.map["places"]))
            elif action in ("seek", "avoid", "give_food", "give_coin", "ask_kin", "ask_food", "buy_from", "steal"):
                act["target"] = rng.randrange(agents)
            acts.insert(rng.randrange(len(acts) + 1), act)
        check.before(acts)
        sim.step(acts)
        bad = check.after()
        assert not bad, bad
    return sim, b


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_session_of_walks_and_acts_keeps_every_invariant_and_replays_exactly(seed):
    sim, b = session(seed)
    sim.finish()
    places = {m.data["place"] for m in moved(sim, b)}
    assert len(moved(sim, b)) > 50 and None in places and len(places) > 3
    events = [Event(**json.loads(e.to_json())) for e in sim.events]
    assert replay(events).state_hash() == sim.state_hash()


def test_the_viewer_places_a_walked_body_from_the_log_alone():
    from asiimov.viewer import moves
    sim, b = awake_body()
    rng, truth = random.Random(5), []
    x0, y0, x1, y1 = bounds(sim.map)
    start = sim.state["tick"]
    for i in range(TICKS_PER_DAY):
        acts = [walk(rng.randint(x0, x1), rng.randint(y0, y1))] if rng.random() < 0.5 else []
        if i % 17 == 16:
            acts.append({"kind": "act", "action": "go_to", "place": "market"})
        sim.step(acts)
        a = sim.state["agents"][b]
        truth.append((a["place"], a["activity"], a.get("at")))
    mv = moves([json.loads(e.to_json()) for e in sim.events])[b]
    for i, (place, activity, at) in enumerate(truth):
        m = next(x for x in reversed(mv) if x[0] <= start + i)
        assert m[1:3] == [place, activity], (i, m)
        if place is None and at is not None:  # standing in the open: the page draws it on its tile
            assert m[3:5] == [at, None], (i, m)
    assert any(p is None and at is not None for p, _, at in truth) \
        and any(p is not None and at is not None for p, _, at in truth) and any(p is None and at is None for p, _, at in truth)


def test_a_snapshot_keeps_the_tile():
    sim, b = awake_body()
    tile = open_tile(sim)
    walk_to(sim, tile)
    twin = Sim.restore(json.loads(json.dumps(sim.snapshot())), sim.events, StubMind())
    for s in (sim, twin):
        for t in range(30):
            s.step([walk(tile[0] + t % 3, tile[1])] if t % 4 == 0 else [])
    assert twin.state_hash() == sim.state_hash() and [e.to_json() for e in twin.events] == [e.to_json() for e in sim.events]


# ---------------------------------------------------------------- late walks (E4) and the live runner
def test_a_late_walk_joins_the_tick_and_a_late_act_after_it_is_judged_there():
    sim, b = awake_body()
    a = sim.state["agents"][b]
    a["inv"]["coin"], a["inv"]["food"] = 50, 0
    sim.state["market_food"] = max(sim.state["market_food"], 5)
    stall = sim.map["places"]["market"]["anchor"]
    walk_to(sim, [stall[0] - 1, stall[1] + 3])  # just outside the market, in the open
    assert a["place"] is None
    got = []

    def during(s, done):
        got.append(s.late_act({"kind": "act", "action": "walk", "to": stall}))
        got.append(s.late_act({"kind": "act", "action": "buy_food"}))
    n = len(sim.events)
    sim.step([], during=during)
    tick_events = sim.events[n:]
    assert None not in got and sim.events[got[0]].data == {"action": "walk", "to": stall, "late": True}
    m = next(e for e in tick_events if e.kind == "moved")
    assert m.parents == [got[0]] and m.data["place"] == "market" and a["place"] == "market"
    r = next(e for e in tick_events if e.kind == "receipt" and e.parents[0] == got[1])
    assert r.data["ok"], r.data
    minds = [e.id for e in tick_events if e.origin == "mind"]
    assert not minds or r.id < min(minds)
    assert not state_violations(sim)


def test_late_walks_replay_where_they_joined():
    sim, b = awake_body()  # nothing set by hand here: a replay has only the log
    stall = sim.map["places"]["market"]["anchor"]
    for t in range(40):
        sim.step([], during=(lambda s, d: [s.late_act({"kind": "act", "action": "walk", "to": [stall[0] + t % 7 - 3, stall[1] + t % 9]}),
                                           s.late_act({"kind": "act", "action": "buy_food"})])
                 if t % 3 == 0 else None)
        assert not state_violations(sim)
    assert sum(e.kind == "act" and e.data.get("late") and e.data["action"] == "walk" for e in sim.events) == 14
    sim.finish()
    events = [Event(**json.loads(e.to_json())) for e in sim.events]
    assert replay(events).state_hash() == sim.state_hash()


def test_the_live_runner_takes_a_walk_with_a_tile_and_refuses_it_without(tmp_path):
    from asiimov import live
    from test_player_body import intent
    r = live.LiveRunner(str(tmp_path / "w"), StubMind(), 4, 10)
    assert r.verbs()["act"]["to"] == bounds(r.sim.map) and "walk" in r.verbs()["act"]["action"]
    assert r.submit(intent("p", "possess", {"agent": 1}, 0)) is None
    r.step()
    no = lambda i, a: r.submit(intent(i, "act", a, 1))["reason"]
    assert no("x1", {"action": "walk"}) == "bad_args"
    assert no("x2", {"action": "walk", "to": [0.5, 0]}) == "bad_args"
    assert no("x3", {"action": "walk", "to": [10 ** 6, 0]}) == "bad_args"
    assert no("x4", {"action": "walk", "to": [0, 0], "target": 2}) == "bad_args"
    assert no("x5", {"action": "work", "to": [0, 0]}) == "bad_args"
    assert no("x6", {"action": "walk", "to": [0, 0], "place": "woods"}) == "bad_args"
    home = r.sim.map["places"][r.sim.state["agents"][1]["home"]]
    inside, outside = [home["rect"][0], home["rect"][1]], [0, 8]
    within, leaving = intent("w1", "act", {"action": "walk", "to": inside}, 1), intent("w2", "act", {"action": "walk", "to": outside}, 1)
    assert not r.pulls(within) and r.pulls(leaving) and r.pulls(intent("a", "act", {"action": "work"}, 1))
    assert r.submit(within) is None
    _, events, acks = r.step()
    assert [a["status"] for a in acks] == ["applied"]
    act = next(e for e in events if e.kind == "act")
    assert act.data == {"action": "walk", "to": inside}
    # asleep at this hour, so the engine's answer is a receipt, not a move
    assert next(e for e in events if e.parents and e.parents[0] == act.id).data["why"] == "asleep"
    assert r.view()["agents"][1]["pos"] == r.sim._pos(r.sim.state["agents"][1])


def test_a_live_session_of_walks_equals_batch_and_replays(tmp_path):
    from asiimov import live
    from asiimov.events import read_jsonl
    from test_inhabitant import batch_of
    from test_player_body import intent
    out = str(tmp_path / "w")
    r = live.LiveRunner(out, StubMind(), 6, 12)
    while r.sim.state["tick"] < TICKS_PER_DAY // 2:
        r.step()
    b = next(a["id"] for a in r.sim.state["agents"] if not a["asleep"])
    assert r.submit(intent("p", "possess", {"agent": b}, r.sim.state["tick"])) is None
    r.step()
    rng = random.Random(5)
    x0, y0, x1, y1 = bounds(r.sim.map)
    for i in range(80):
        t = r.sim.state["tick"]
        assert r.submit(intent(f"w{i}", "act", {"action": "walk", "to": [rng.randint(x0, x1), rng.randint(y0, y1)]}, t)) is None
        if i % 7 == 0:
            assert r.submit(intent(f"a{i}", "act", {"action": "rest"}, t)) is None
        _, _, acks = r.step()
        assert all(a["status"] == "applied" for a in acks)
    v = inhabitant.view(r.sim, b)
    assert v["pos"] == r.sim._pos(r.sim.state["agents"][b])
    r.finish()
    events = read_jsonl(out + "/events.jsonl")
    assert sum(e.kind == "moved" for e in events) > 20
    assert replay(events).state_hash() == r.sim.state_hash()
    assert batch_of(out) == [e.to_json() for e in events]
