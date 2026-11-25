"""reach-1 (plan/v0-contracts-spec.md B1.3, B1.4, B2.4; Steering 2026-11-23 20:24Z to 20:39Z).

The player's acts reach across open ground: at one place, or one of the two in the open and their tiles at
most a tile apart at the act's own time (`Sim._near`). A villager walking by can be called to (`hail`, within
sight) and stops where it is to listen; a line restarts its wait, and it walks on after. A body is seen one
tick behind its route, so every walk shows from its start and can be met on its way. Words and doings reach
an inhabitant only from its place or from reach; sight alone shows who stands where and that someone spoke.
Between two villagers nothing changes, so a world with no player writes the log it wrote before."""
import json
import os
import random
from fractions import Fraction

import pytest

from asiimov import inhabitant
from asiimov.events import Event, read_jsonl
from asiimov.invariants import Bodies, Checker, presence_violations, state_violations
from asiimov.minds import StubMind
from asiimov.replay import replay
from asiimov.sim import HAIL_ACTS, LISTEN_TICKS, OCCASION_TICKS, PLAYER_ACTIONS, DELIBERATE_ACTIONS, SUB, TICKS_PER_DAY, Sim, act_cost
from asiimov.worldmap import REACH, SEE, WALK, near, place_at, position, tile_at, travel_ticks
from test_inhabitant import batch_of
from test_time import at, runner_with_body, send
from test_walk import awake_body, open_tile, walk, walk_to


def anchor(sim, place):
    return list(sim.map["places"][place]["anchor"])


def path_tile(sim, w, f):
    """Where walker `w` is seen at f 600ths into the coming tick: one tick behind its route."""
    r = w["route"]
    return tile_at(r, anchor(sim, r["to"]), (sim.state["tick"] - 1) * SUB + f, SUB)


def passer(sim, b, tiles=SEE, steps=3 * TICKS_PER_DAY, errand=False):
    """Step until someone walking by comes within `tiles` of the body during the coming tick, still on its
    way (and on no errand of its own, a one-off step or an intent, unless `errand`: those keep going, as at a
    place). Returns (that walker, the first f at which it is that near)."""
    me = sim.state["agents"][b]
    for _ in range(steps):
        for w in sim.state["agents"]:
            r = w["route"]
            if w["id"] == b or not r or w["asleep"] or sim._halted(w) \
                    or (w["override"] is not None or w["intent"] is not None) is not errand:
                continue
            for f in range(0, SUB, 5):
                p = path_tile(sim, w, f)
                if near(p, sim.tile(me), tiles) and p != anchor(sim, r["to"]):
                    return w, f
        sim.step([walk(*sim.tile(me))])  # a step in place: the body keeps standing where it is
        assert not state_violations(sim)
    raise AssertionError("nobody walked by")


def by_the_road(seed=3, n=60):
    """A body standing in the open south of the plaza, on the way to the field."""
    sim, b = awake_body(seed=seed, n=n)
    walk_to(sim, open_tile(sim))
    assert sim.state["agents"][b]["place"] is None
    return sim, b


def act(action, target=None, **more):
    return {"kind": "act", "action": action, **({} if target is None else {"target": target}), **more}


def receipt_of(sim, eid):
    return next(e for e in sim.events[eid:] if e.kind == "receipt" and e.parents[0] == eid)


def replays(sim):
    sim.finish()
    events = [Event(**json.loads(e.to_json())) for e in sim.events]
    assert replay(events).state_hash() == sim.state_hash()


# ------------------------------------------------------------------ a body is seen one tick behind its route
def test_tile_at_is_the_floor_of_position_in_whole_numbers():
    rng = random.Random(1)
    for _ in range(400):
        r = {"from": [rng.randint(-50, 50), rng.randint(-50, 50)], "depart": rng.randint(0, 5)}
        to = [rng.randint(-50, 50), rng.randint(-50, 50)]
        n = rng.randint(0, 8 * SUB)
        x, y = position(r, to, Fraction(n, SUB))
        assert tile_at(r, to, n, SUB) == [int(x // 1), int(y // 1)]


def test_a_walk_of_n_ticks_is_seen_over_n_windows_from_its_start_to_its_destination():
    """Steering 2026-11-23 20:39Z: step T decides a walk; the time until the next step shows its first tick,
    and the walker stands on its destination only from the step that arrives it."""
    sim = Sim(3, 60, StubMind())
    sim.run_until(TICKS_PER_DAY // 3)
    seen, was = {1: 0, 2: 0}, {}
    for _ in range(TICKS_PER_DAY):
        for a in sim.state["agents"]:
            was[a["id"]] = sim.tile(a)
        sim.step()
        tick = sim.state["tick"]  # the window between this step and the next shows sim-time [tick, tick + 1)
        for a in sim.state["agents"]:
            r = a["route"]
            if not r:
                continue
            to, n = anchor(sim, r["to"]), r["arrive"] - r["depart"]
            assert a["place"] is None and r["depart"] < tick <= r["arrive"]
            assert sim.pos(a, tick) == sim.tile(a) == position(r, to, tick - 1) and sim._tile_now(a) == sim.tile(a)
            if r["depart"] == tick - 1:  # decided by the step just run: the window opens with it at its start
                assert sim.tile(a) == list(r["from"])
                seen[min(n, 2)] += 1
            ends = sim.pos(a, tick + Fraction(SUB - 1, SUB))
            if r["arrive"] == tick:  # its last window: it reaches the destination in it, for the next step to arrive it
                assert near(ends, to, 1)
            else:  # and never in a window before that
                assert ends != to and sim.tile(a, tick + 1) != to
            for k in (1, 150, 599):  # the whole-number tile the engine judges reach by is the same
                t = tick + Fraction(k, SUB)
                x, y = sim.pos(a, t)
                assert tile_at(r, to, (tick - 1) * SUB + k, SUB) == [int(x // 1), int(y // 1)] == sim.tile(a, t)
    assert seen[1] > 50 and seen[2] > 10  # walks of one tick, never seen before, and longer ones


# ------------------------------------------------------------------ hail
def test_hail_is_the_players_alone_and_costs_nothing():
    assert HAIL_ACTS == ("hail",) and "hail" in PLAYER_ACTIONS and "hail" not in DELIBERATE_ACTIONS
    assert act_cost("hail") == 0
    sim, b = awake_body()
    with pytest.raises(ValueError, match="hail takes a target"):
        sim.step([act("hail")])


def test_a_walker_hailed_within_sight_stops_where_it_is_and_walks_on_after():
    sim, b = by_the_road()
    w, f = passer(sim, b)
    tick, me = sim.state["tick"], sim.state["agents"][b]
    to, was, p = w["route"]["to"], dict(w["route"]), path_tile(sim, w, f)
    check = Checker(sim)
    acts = [at(act("hail", w["id"]), f)]
    check.before(acts)
    n = len(sim.events)
    eid = sim.take(acts[0])
    assert [e.kind for e in sim.events[n:]] == ["act", "halt", "receipt"]  # at once, before the tick runs
    halt, rc = sim.events[n + 1], sim.events[n + 2]
    assert halt.actor == w["id"] and halt.parents == [eid] and halt.origin == "rule"
    assert rc.data == {"action": "hail", "ok": True, "why": "hailed", "to": w["id"]} and rc.parents == [eid, halt.id]
    r = w["route"]
    assert r["from"] == p == halt.data["pos"] and r["to"] == to == halt.data["place"] and r["event"] == halt.id
    assert r["depart"] == tick + LISTEN_TICKS - 1 == halt.data["depart"]
    assert r["arrive"] == r["depart"] + travel_ticks(p, anchor(sim, to)) == halt.data["arrive"] >= was["arrive"]
    assert near(p, sim.tile(me), SEE) and w["place"] is None and sim._halted(w)
    assert w["override"]["do"] == "stay" and w["override"]["place"] == to and w["override"]["talk"]
    sim.step(acts)
    assert not check.after()
    for i in range(1, r["arrive"] - tick + 1):  # it stands there for the hold, then walks on, and arrives
        assert w["route"] is not None and w["place"] is None
        standing = sim.state["tick"] < tick + LISTEN_TICKS
        for k in (0, 300, 599):
            assert (sim.tile(w, sim.state["tick"] + Fraction(k, SUB)) == p) is (standing or (k == 0 and sim.state["tick"] == tick + LISTEN_TICKS))
        assert not check.step([walk(*sim.tile(me))])
    assert w["route"] is None and w["place"] == to and sim.state["tick"] == r["arrive"] + 1
    assert [e.kind for e in sim.events if e.actor == w["id"] and e.tick == r["arrive"]][0] == "arrive"
    replays(sim)


def test_a_walker_on_an_errand_of_its_own_stops_too_and_keeps_its_errand():
    """As a line at a place leaves a running step alone (a sick day home, a trade), a hail gives no listening
    hold to a walker that has one: it stops where it is, and its own step decides what it does next."""
    sim, b = by_the_road()
    w, f = passer(sim, b, errand=True)
    step = json.dumps([w["override"], w["intent"]], sort_keys=True)
    eid = sim.take(at(act("hail", w["id"]), f))
    assert receipt_of(sim, eid).data["why"] == "hailed" and sim._halted(w)
    assert json.dumps([w["override"], w["intent"]], sort_keys=True) == step
    check = Checker(sim)
    check.before([at(act("hail", w["id"]), f)])
    sim.step([at(act("hail", w["id"]), f)])
    assert not check.after()
    for _ in range(2 * LISTEN_TICKS + 4):
        assert not check.step([walk(*sim.tile(sim.state["agents"][b]))])
    replays(sim)


def test_hail_says_why_it_cant_and_a_second_hail_adds_no_time():
    sim, b = by_the_road()
    me = sim.state["agents"][b]
    w, f = passer(sim, b)
    here = next(a for a in sim.state["agents"] if a["place"] == "plaza" and sim.sees(me, a))
    far = next(a for a in sim.state["agents"] if not sim.sees(me, a) and a["id"] != b)
    n = len(sim.events)
    ids = [sim.take(at(act("hail", t), f)) for t in (here["id"], far["id"], b, 10 ** 6, w["id"], w["id"])]
    whys = [(receipt_of(sim, i).data["ok"], receipt_of(sim, i).data["why"]) for i in ids]
    assert whys == [(False, "not walking"), (False, "target not in sight"), (False, "unknown target"),
                    (False, "unknown target"), (True, "hailed"), (True, "already stopped")]
    assert sum(e.kind == "halt" for e in sim.events[n:]) == 1
    depart = w["route"]["depart"]
    sim.step([at(act("hail", t), f) for t in (here["id"], far["id"], b, 10 ** 6, w["id"], w["id"])])
    for _ in range(LISTEN_TICKS - 2):  # hailing again while it stands never lengthens the wait
        eid = sim.take(act("hail", w["id"]))
        assert receipt_of(sim, eid).data["why"] == "already stopped" and w["route"]["depart"] == depart
        sim.step([act("hail", w["id"])])
    assert not state_violations(sim)
    replays(sim)


def test_a_hail_holds_no_ask_as_answered_and_is_never_busy():
    """It costs the body nothing and is no answer to anyone: like a line, it leaves a held ask waiting."""
    sim, b = by_the_road()
    w, f = passer(sim, b)
    sim.state["player"]["asks"] = [{"asker": w["id"], "action": "ask_food", "event": 0, "src": 0, "until": sim.state["tick"] + 3}]
    eid = sim.take(at(act("hail", w["id"]), f))
    assert receipt_of(sim, eid).data["why"] == "hailed" and len(sim.state["player"]["asks"]) == 1
    del sim.state["player"]["asks"]
    sim.take(at(act("wait"), f))  # the body is now held for a sim-minute
    eid = sim.take(at(act("hail", w["id"]), f))
    assert receipt_of(sim, eid).data["why"] == "already stopped"  # not "busy"


def test_the_view_lists_who_walks_past_this_tick_and_whom_it_can_hail():
    """The body stands still for a whole tick while walkers cover 40 tiles, so the view tells it who comes
    within sight before the next step, with the tiles they are seen on, and the offer names them for a hail."""
    sim, b = by_the_road()
    me = sim.state["agents"][b]
    for _ in range(TICKS_PER_DAY):  # until someone not in sight yet walks into sight later in the tick
        w, f = passer(sim, b)
        if f > 0:
            break
        sim.step([walk(*sim.tile(me))])
    v = inhabitant.view(sim, b)
    s = next(s for s in v["seen"] if s["id"] == w["id"])
    assert not sim.sees(me, w) and s["route"] is None and s["place"] is None and "reach" not in s and "asleep" not in s
    assert s["in"][0] == f and s["pos"] == s["track"][0][1:] == path_tile(sim, w, f)
    for k, x, y in s["track"]:  # every tile it is told is one the walker is seen on, in sight, at that time
        assert s["in"][0] <= k <= s["in"][1] and [x, y] == path_tile(sim, w, k) and near([x, y], sim.tile(me), SEE)
    assert not near(path_tile(sim, w, max(0, f - 5)), sim.tile(me), SEE)
    assert w["id"] in v["offer"]["hail"] and "hail" in PLAYER_ACTIONS
    assert all(sim.state["agents"][o]["route"] for o in v["offer"]["hail"])
    eid = sim.take(at(act("hail", w["id"]), f))
    assert receipt_of(sim, eid).data["why"] == "hailed"
    v = inhabitant.view(sim, b)
    s = next(s for s in v["seen"] if s["id"] == w["id"])  # stopped: it stands in sight for the whole tick
    assert w["id"] not in v["offer"].get("hail", []) and s["in"] == [0, SUB - 1] and s["route"]["from"] == s["pos"]
    assert {tuple(k[1:]) for k in s["track"]} == {tuple(s["pos"])}


# ------------------------------------------------------------------ reach: acts and lines across open ground
def stopped(seed=3, n=60):
    """A walker hailed and stopped, and the body walked to the tile beside it, both out in the open."""
    sim, b = by_the_road(seed, n)
    me = sim.state["agents"][b]
    w, f = passer(sim, b)
    acts = [at(act("hail", w["id"]), f)]
    sim.take(acts[0])
    p = w["route"]["from"]
    spot = next(t for t in ([p[0], p[1] + 1], [p[0], p[1] - 1], [p[0] + 1, p[1]], [p[0] - 1, p[1]])
                if place_at(sim.map, t) is None)
    acts.append(at(walk(*spot), f))
    sim.take(acts[1])
    assert sim.tile(me) == spot and me["place"] is None and place_at(sim.map, p) is None, (p, spot)
    assert sim.reach(me, w) and sim._near(me, w) and sim._near(w, me)
    return sim, b, w, acts, f


def test_a_line_to_a_stopped_walker_is_heard_in_the_open_and_restarts_its_wait():
    sim, b, w, acts, f = stopped()
    tick, check = sim.state["tick"], Checker(sim)
    check.before(acts)
    sim.step(acts)
    assert not check.after()
    line = act("say", w["id"], text="Good day. Where are you headed?")
    n = len(sim.events)
    assert not check.step([line])
    said = next(e for e in sim.events[n:] if e.kind == "said")
    assert said.actor == b and said.data["with"] == w["id"] and said.data["place"] is None
    assert receipt_of(sim, n).data == {"action": "say", "ok": True, "why": "said", "to": w["id"]}
    halt = next(e for e in sim.events[n:] if e.kind == "halt")  # its wait starts again from the line
    assert halt.actor == w["id"] and halt.parents == [said.id] and halt.data["depart"] == tick + 1 + LISTEN_TICKS - 1
    assert w["occasion"]["why"] == "spoken" and w["route"]["depart"] == halt.data["depart"]
    n = len(sim.events)
    assert not check.step([walk(*sim.tile(sim.state["agents"][b]))])
    reply = next(e for e in sim.events[n:] if e.kind == "reply")  # its mind answers on the next tick, on the road
    assert reply.actor == w["id"] and reply.data["with"] == b and w["place"] is None
    nxt, r = reply.data["next"], w["route"]
    if nxt == "stay":
        assert r["depart"] == tick + 2 + OCCASION_TICKS - 1
    elif nxt == "leave":
        assert r["depart"] == tick + 2 and sim.events[r["event"]].kind == "activity" and sim.events[r["event"]].data["from"] == r["from"]
    else:
        assert r["depart"] == halt.data["depart"]
    for _ in range(r["arrive"] - sim.state["tick"] + 1):
        assert not check.step([walk(*sim.tile(sim.state["agents"][b]))])
    assert w["route"] is None and w["place"] == r["to"]
    replays(sim)


def test_a_line_or_an_ask_two_tiles_off_doesnt_reach():
    sim, b, w, acts, f = stopped()
    me, p = sim.state["agents"][b], w["route"]["from"]
    off = next(t for t in ([p[0], p[1] + 2], [p[0], p[1] - 2], [p[0] + 2, p[1]], [p[0] - 2, p[1]])
               if place_at(sim.map, t) is None)
    acts.append(at(walk(*off), f))
    sim.take(acts[-1])
    assert sim.sees(me, w) and not sim.reach(me, w)
    v = inhabitant.view(sim, b)
    assert all(w["id"] not in who for who in v["offer"].values() if who is not v["offer"].get("hail"))
    assert next(s for s in v["seen"] if s["id"] == w["id"]).get("reach") is None
    acts += [at(act("ask_food", w["id"]), f + 100), at(act("say", w["id"], text="Hello there."), f + 100)]
    ask = sim.take(acts[-2])
    assert receipt_of(sim, ask).data["why"] in ("not offered", "unknown target")
    n = len(sim.events)
    sim.step(acts)
    assert next(e for e in sim.events[n:] if e.kind == "receipt" and e.data["action"] == "say").data["why"] == "target not here"
    assert not state_violations(sim)


def test_asks_gifts_and_theft_reach_a_stopped_walker_a_tile_away():
    sim, b, w, acts, f = stopped()
    me = sim.state["agents"][b]
    me["inv"]["food"], w["inv"]["food"], me["cooldown_until"] = 3, 3, 0
    v = inhabitant.view(sim, b)
    seen = next(s for s in v["seen"] if s["id"] == w["id"])
    assert seen["reach"] is True and seen["asleep"] is False and v["present"] == []
    for action in ("ask_food", "steal", "give_food", "seek"):  # the server's offer names it, so the client lists it
        assert w["id"] in v["offer"][action], action
    check = Checker(sim)
    check.before(acts)
    food = w["inv"]["food"]
    acts.append(at(act("give_food", w["id"]), f + COST_GAP))
    give = sim.take(acts[-1])
    sim.step(acts)
    assert not check.after()
    done = [e for e in sim.events[give:] if e.kind == "receipt" and e.parents[0] == give]
    assert done[-1].data["ok"] and done[-1].data.get("to") == w["id"] and w["inv"]["food"] == food + 1, [e.data for e in done]
    for action in ("ask_food", "steal"):
        me["cooldown_until"] = 0
        a2 = act(action, w["id"])
        check.before([a2])
        eid = sim.take(a2)
        d = receipt_of(sim, eid).data
        assert d.get("to", d.get("victim")) == w["id"] and d["why"] not in ("not offered", "unknown target", "target not here"), d
        sim.step([a2])
        assert not check.after()  # (no replay here: the test set food and the cooldown by hand; the session below replays)


COST_GAP = SUB // 10  # a sim-minute: what one act holds the body for (time-1b)


def test_an_act_on_a_stopped_walker_keeps_it_standing_as_a_line_does():
    sim, b, w, acts, f = stopped()
    tick, check = sim.state["tick"], Checker(sim)
    check.before(acts)
    sim.step(acts)
    assert not check.after() and w["route"]["depart"] == tick + LISTEN_TICKS - 1
    v = inhabitant.view(sim, b)
    action = next(x for x in ("ask_food", "give_coin", "give_food", "steal") if w["id"] in v["offer"].get(x, []))
    again, deed = act("hail", w["id"]), at(act(action, w["id"]), COST_GAP)
    check.before([again, deed])
    assert receipt_of(sim, sim.take(again)).data["why"] == "already stopped"
    assert w["route"]["depart"] == tick + LISTEN_TICKS - 1  # a second call adds nothing
    eid = sim.take(deed)
    halt = sim.events[w["route"]["event"]]  # the act does: its wait starts again from now
    assert halt.kind == "halt" and halt.parents == [eid] and halt.data["depart"] == tick + 1 + LISTEN_TICKS - 1
    assert halt.data["pos"] == w["route"]["from"] == sim.tile(w) and w["override"]["left"] == LISTEN_TICKS
    sim.step([again, deed])
    assert not check.after()
    other = next(o for o in sim.state["agents"] if o["route"] is not None and o["id"] != w["id"])
    n = len(sim.events)
    sim._keep(sim.state["agents"][b], other, eid)  # nobody else is stopped by an act
    assert len(sim.events) == n
    replays(sim)


def test_the_presence_check_follows_bodies_from_the_log_alone_and_catches_a_planted_reach():
    """Test the test: the log-only positions agree with the engine, and an act across more than a tile is flagged."""
    sim, b, w, acts, f = stopped()
    start, where = len(sim.events), {a["id"]: a["place"] for a in sim.state["agents"]}
    bodies = Bodies(sim)
    assert bodies.held == b and bodies.tile(w["id"], where) == sim._tile_now(w) and bodies.tile(b, where) == sim._tile_now(sim.state["agents"][b])
    sim.step(acts)
    sim.step([act("say", w["id"], text="Good day.")])
    log = list(sim.events)
    assert not presence_violations(log, start, dict(where), _bodies_at(bodies))
    said = next(e for e in log[start:] if e.kind == "receipt" and e.data["action"] == "say" and e.data["ok"])
    # the same receipt with no positions to go by: the old rule (one place) flags it
    assert any(f"event {said.id}:" in v for v in presence_violations(log, start, dict(where)))
    # and with positions, once the body's walk is taken out of the log, it stands six tiles off: flagged too
    far = _bodies_at(bodies)
    far.at[b] = [bodies.at[b][0], bodies.at[b][1] + 5]
    kept = [e for e in log[start:] if e.kind != "moved"]
    assert any(f"event {said.id}:" in v for v in presence_violations(log[:start] + kept, start, dict(where), far))


def _bodies_at(b: Bodies) -> Bodies:
    c = Bodies.__new__(Bodies)
    c.map, c.held, c.tick, c.f = b.map, b.held, b.tick, b.f
    c.routes, c.at = {k: dict(v) for k, v in b.routes.items()}, {k: list(v) for k, v in b.at.items()}
    return c


def test_between_two_villagers_nothing_changes():
    """`_near` is `_together` unless the player's body is one of the two: two walkers on one tile never meet."""
    sim = Sim(3, 60, StubMind())
    sim.run_until(TICKS_PER_DAY // 2)
    agents = sim.state["agents"]
    for a in agents:
        for o in agents:
            assert sim._near(a, o) is sim._together(a, o)
    sim.step([{"kind": "possess", "agent": 0}])
    for a in agents[1:]:
        for o in agents[1:]:
            assert sim._near(a, o) is sim._together(a, o)


# ------------------------------------------------------------------ words need the place or reach; sight shows who stands where
def test_sight_alone_shows_who_stands_where_and_that_someone_spoke_never_the_words():
    ev = lambda kind, data, actor=5: {"id": 9, "tick": 7, "kind": kind, "origin": "rule", "actor": actor, "data": data, "parents": []}
    here, nearby = {5, 6}, {6}
    said = ev("said", {"with": 6, "text": "Come by tonight.", "place": "plaza", "move": "asks_food"})
    reply = ev("reply", {"with": 6, "says": "I will.", "answer": "yes"})
    talk = ev("talk", {"with": 6, "place": "plaza", "gossip": "thief:3"})
    going = ev("activity", {"activity": "work", "place": "field", "arrive": 9, "from": [0, 0]})
    for e, shown in ((said, {"with": 6, "place": "plaza"}), (reply, {"with": 6}), (talk, {"with": 6, "place": "plaza"}),
                     (going, {"place": "field", "arrive": 9, "from": [0, 0]})):
        assert inhabitant.witnessed(e, 1, here, 7, nearby)
        assert inhabitant.scrub(e, 1, None, nearby)["data"] == shown
    for kind, data in (("eat", {"household": 2}), ("sell", {"food": 1}), ("sleep", {}), ("wake", {"place": "home_1"}),
                       ("ask", {"action": "ask_food", "to": 6}), ("meet", {"with": 6, "place": "plaza"}),
                       ("deal", {"move": "asks_food", "from": 5, "to": 6, "food": 1, "ok": True})):
        assert not inhabitant.witnessed(ev(kind, data), 1, here, 7, nearby), kind
        assert inhabitant.witnessed(ev(kind, data, actor=6), 1, here, 7, nearby), kind  # from someone in reach: as before
    full = inhabitant.scrub({**said, "actor": 6, "data": {**said["data"], "with": 5}}, 1, None, nearby)
    assert full["data"]["text"] == "Come by tonight."
    # a line said to it is its own to hear, wherever the speaker stands
    mine = {**said, "data": {**said["data"], "with": 1}}
    assert inhabitant.witnessed(mine, 1, set(), -1, set()) and inhabitant.scrub(mine, 1, None, set())["data"]["text"]


def test_an_inhabitant_in_the_open_gets_no_words_from_inside_a_place():
    sim, b = by_the_road()
    me = sim.state["agents"][b]
    inside = [a["id"] for a in sim.state["agents"] if a["place"] == "plaza" and sim.sees(me, a)]
    assert inside and not set(inside) & set(inhabitant.near_ids(sim, b))
    events = [{"id": 10 ** 6 + i, "tick": sim.state["tick"], "kind": "said", "origin": "rule", "actor": a,
               "data": {"with": inside[0], "text": "A secret.", "place": "plaza"}, "parents": []} for i, a in enumerate(inside)]
    here, nearby = set(inhabitant.sight_ids(sim, b)), set(inhabitant.near_ids(sim, b))
    got = [inhabitant.scrub(e, b, None, nearby) for e in events if inhabitant.witnessed(e, b, here, sim.state["tick"], nearby)]
    assert len(got) == len(inside) and all("text" not in e["data"] for e in got)
    v = inhabitant.view(sim, b)
    assert all("asleep" not in s for s in v["seen"] if not s.get("reach"))


# ------------------------------------------------------------------ a session
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_session_of_hails_lines_and_acts_in_the_open_equals_batch_and_replays(seed):
    """A day by the road: the body hails whoever walks by, steps up to the one that stopped, and speaks, asks,
    gives, steals or greets. Taken as they arrive, the log is a batch run's, the checker (with the log-only
    positions) finds nothing, and it replays."""
    rng = random.Random(seed)
    early, batch = (Sim(seed, 60, StubMind()) for _ in range(2))
    for sim in (early, batch):
        sim.run_until(TICKS_PER_DAY // 3)
    b = next(a["id"] for a in early.state["agents"] if not a["asleep"] and a["place"] is not None)
    me = early.state["agents"][b]
    spots = [open_tile(early), [0, -8], [8, 0]]
    assert all(place_at(early.map, t) is None for t in spots)
    check, spot = Checker(early), spots[0]
    for i in range(TICKS_PER_DAY):
        tick = early.state["tick"]
        acts = [{"kind": "possess", "agent": b}] if i == 0 else []
        check.before(acts)
        for a in acts:
            early.take(a)

        def send(a, f):  # taken as it arrives, so the next choice sees what it did
            acts.append(at(a, f))
            return early.take(acts[-1])

        if i and i % 40 == 0:
            spot = rng.choice(spots)
        if i and not me["asleep"] and tick < me["sleep_tick"] and me["needs"]["fatigue"] < 1000:
            f, lines = rng.randrange(0, 40), 0
            halted = [w for w in early.state["agents"] if early._halted(w) and near(w["route"]["from"], early.tile(me), 3 * SEE)]
            if not halted:
                send(walk(*spot), f)
            for w in early.state["agents"]:  # hail the first two that come within sight this tick
                r = w["route"]
                if w["id"] == b or not r or early._halted(w) or len([a for a in acts if a.get("action") == "hail"]) >= 2:
                    continue
                f0 = next((k for k in range(f, SUB, 15) if near(path_tile(early, w, k), early.tile(me), SEE)
                           and path_tile(early, w, k) != anchor(early, r["to"])), None)
                if f0 is not None:
                    f = f0
                    assert receipt_of(early, send(act("hail", w["id"]), f)).data["why"] == "hailed"
            halted = [w for w in early.state["agents"] if early._halted(w) and near(w["route"]["from"], early.tile(me), 3 * SEE)]
            if halted:
                w = rng.choice(halted)
                p = w["route"]["from"]
                send(walk(p[0], p[1] + rng.choice((-1, 1))), f)
                for kind in rng.sample(("say", "ask_food", "give_food", "steal", "seek", "buy_from", "give_coin"), 3):
                    if kind == "say":
                        send(act("say", w["id"], text="Good day to you."), f)
                    elif f + COST_GAP < SUB:
                        f += COST_GAP
                        send(act(kind, w["id"]), f)
        early.step(acts)
        batch.step(acts)
        bad = check.after()
        assert not bad, bad
        assert early.state_hash() == batch.state_hash(), early.state["tick"]
    early.finish(), batch.finish()
    log = early.events
    assert [e.to_json() for e in log] == [e.to_json() for e in batch.events]
    hailed = [e for e in log if e.kind == "receipt" and e.data["action"] == "hail" and e.data["why"] == "hailed"]
    in_open = [e for e in log if e.kind == "said" and e.actor == b and e.data["place"] is None]
    replies = [e for e in log if e.kind == "reply" and e.data["with"] == b]
    walkers = {e.data["to"] for e in hailed}
    reached = [e for e in log if e.kind == "receipt" and e.actor == b and (e.data["ok"] or e.data["why"] == "caught")
               and e.data.get("to", e.data.get("victim")) in walkers and e.data["action"] not in ("hail", "say")]
    met = [e for e in log if e.kind == "meet" and e.actor == b]
    assert len(hailed) > 15 and len(in_open) > 3 and replies and len(reached) > 3 and met, \
        (len(hailed), len(in_open), len(replies), len(reached), len(met))
    assert sum(e.kind == "halt" for e in log) >= len(hailed)
    events = [Event(**json.loads(e.to_json())) for e in log]
    assert replay(events).state_hash() == early.state_hash()


def test_the_viewer_stands_a_stopped_walker_on_its_tile_from_the_log_alone():
    """The page draws a walker `walk * max(0, t - depart)` tiles along its entry's [from, to, depart], where t is
    the last tick run: the engine's tile at the next view tick (a body is seen one tick behind its route)."""
    from asiimov.viewer import moves
    from asiimov.worldmap import position
    sim, b, w, acts, f = stopped()
    start, truth = sim.state["tick"], []
    sim.step(acts)
    truth.append((w["place"], sim.tile(w)))
    sim.step([act("say", w["id"], text="Wait a moment.")])
    truth.append((w["place"], sim.tile(w)))
    while w["route"] is not None:
        sim.step([])
        truth.append((w["place"], sim.tile(w)))
    mv = moves([json.loads(e.to_json()) for e in sim.events])[w["id"]]
    stood = 0
    for i, (place, tile) in enumerate(truth):
        m = next(x for x in reversed(mv) if x[0] <= start + i)
        assert m[1] == place, (i, m)
        if place is None:
            frm, to, depart = m[3:6]
            drawn = position({"from": frm, "depart": depart}, anchor(sim, to), max(start + i, depart))
            assert drawn == tile, (i, m, tile)
            stood += start + i < depart
    assert stood >= LISTEN_TICKS and truth[-1][0] == mv[-1][1] is not None


def test_over_the_runner_a_hail_is_answered_at_once_with_the_walker_standing_in_the_view(tmp_path):
    """What the client gets: the hail's ack, the halt and the receipt it witnessed, and a view that lists the
    walker standing where it stopped, no longer to be called to. The log equals a batch run of its own acts."""
    out = str(tmp_path / "w")
    r, b = runner_with_body(out, seed=3, agents=60)
    sim, me = r.sim, r.sim.state["agents"][b]
    for i in range(40):  # out to the road, a walk a tick, as the keys send them
        if sim.tile(me) == open_tile(sim):
            break
        assert send(r, f"w{i}", {"action": "walk", "to": open_tile(sim)}) is None
        r.step()
    assert sim.tile(me) == open_tile(sim) and me["place"] is None
    for i in range(3 * TICKS_PER_DAY):
        v = inhabitant.view(sim, b)
        if v["offer"].get("hail"):
            break
        assert send(r, f"s{i}", {"action": "walk", "to": open_tile(sim)}) is None  # stand still, awake
        r.step()
    w = v["offer"]["hail"][0]
    going = next(p for p in v["seen"] if p["id"] == w)
    f = going["in"][0]
    assert going["track"][0][0] == f and len(going["track"]) > 1 and going["track"][0][1:] != going["track"][-1][1:]
    assert send(r, "h1", {"action": "hail", "target": w}, f) is None
    (iid, ack, seen, view), = r.take_due()
    assert iid == "h1" and ack["status"] == "applied"
    kinds = [(e["kind"], e["actor"]) for e in seen]
    assert ("halt", w) in kinds and ("receipt", b) in kinds, kinds
    rec = next(e for e in seen if e["kind"] == "receipt")
    assert rec["data"] == {"action": "hail", "ok": True, "why": "hailed", "to": w}
    stood = next(p for p in view["seen"] if p["id"] == w)
    halt = next(e for e in seen if e["kind"] == "halt")
    assert stood["in"] == [0, SUB - 1] and {tuple(k[1:]) for k in stood["track"]} == {tuple(halt["data"]["pos"])}
    assert len(stood["track"]) == 2 and stood["route"]["from"] == halt["data"]["pos"] == going["track"][0][1:]
    assert w not in view["offer"].get("hail", [])
    r.step()
    r.finish()
    events = read_jsonl(os.path.join(out, "events.jsonl"))
    assert batch_of(out) == [e.to_json() for e in events]
    assert replay(events).state_hash() == sim.state_hash()
