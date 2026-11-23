"""space-1 (plan/v0-contracts-spec.md, B1): every body stands on a tile the engine derives.

A villager at a place stands on its slot there, keyed by its id; a walker is where its route puts it, at whole
or fractional sim-times; only a body the player walked holds a tile. Reach and sight are functions of those
tiles. Nothing is stored for a villager, so a world with no player writes the log engine 38 wrote."""
import hashlib
import json
import random
from fractions import Fraction

import pytest

from asiimov import inhabitant, live
from asiimov.events import Event
from asiimov.invariants import state_violations
from asiimov.minds import StubMind
from asiimov.replay import replay
from asiimov.sim import TICKS_PER_DAY, Sim
from asiimov.worldmap import REACH, SEE, WALK, near, on_map, own_tiles, place_at, position, slot
from test_inhabitant import intent
from test_walk import awake_body, moved, open_tile, session, walk, walk_to

# The no-player bar (contracts, "How this is used"): seeds 1 to 3, 100 villagers, 3 days. Each row is the
# sha256 of the log with the genesis engine field set to "X", then the sha256 over every tick's state hash,
# both as engine 38 wrote them (main 2a4aec8). A later Build 1 step must leave them as they are.
NO_PLAYER_38 = {
    1: ("4f848edcb9b15bfba1cc43479893a5e60fbe2b9a3d316c8f6f189de014e0dc59",
        "6bd01a261286cc1d060e7af86a88b41782b5cd1da336281c815bacdbc59d87c5"),
    2: ("8d3a70a9f4d562fa823a5542b947cf3338aa4d9427de55bbc6593c4b11bef45c",
        "66044254f056bc17acdb4be8d287de9ba4c33295dfc4ddcd0edf6ad1e4fc7f73"),
    3: ("01b910ccecbbf620c7834871043fef1b0d72ecd544c8b9fbd1b1838f015470bc",
        "4fa9f897e6456ea896488ef3bc747e0d5f945d9470ac578b6cd1fa867c4ad456"),
}


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_no_player_bar_a_world_with_no_player_is_engine_38_tick_for_tick(seed):
    sim, ticks = Sim(seed, 100, StubMind()), hashlib.sha256()
    for _ in range(3 * TICKS_PER_DAY):
        sim.step()
        for a in sim.state["agents"][::7]:  # reading positions, reach and sight writes nothing
            sim.pos(a, sim.state["tick"] + Fraction(1, 3)), sim.sees(a, sim.state["agents"][0])
        ticks.update(sim.state_hash().encode())
    sim.finish()
    lines = [e.to_json() for e in sim.events]
    g = json.loads(lines[0])
    g["data"]["engine"] = "X"
    lines[0] = json.dumps(g, sort_keys=True, separators=(",", ":"))
    log = hashlib.sha256("".join(l + "\n" for l in lines).encode()).hexdigest()
    assert (log, ticks.hexdigest()) == NO_PLAYER_38[seed]
    assert all("at" not in a for a in sim.state["agents"]), "a villager holds a tile"


def test_a_slot_is_a_tile_of_its_place_keyed_by_the_id_alone():
    sim = Sim(3, 100, StubMind())
    for place in sim.map["places"]:
        tiles = own_tiles(sim.map, place)
        assert tiles and all(place_at(sim.map, t) == place for t in tiles)
        spots = [slot(tiles, i) for i in range(len(tiles))]
        assert all(s in tiles for s in spots)
        assert len({tuple(s) for s in spots}) == len(tiles), "as many ids as tiles share no tile"
        assert slot(tiles, 5) == slot(tiles, 5 + len(tiles)), "only ids a whole count apart share one"
    home = own_tiles(sim.map, "home_0")
    family = [slot(home, i) for i in range(4)]  # a household's consecutive ids stand apart, not in a row
    assert all(not near(a, b, 0) for i, a in enumerate(family) for b in family[i + 1:])


def test_every_villager_stands_on_a_tile_of_the_place_the_engine_says_it_is_at():
    sim = Sim(3, 60, StubMind())
    at_place = walking = 0
    for _ in range(2 * TICKS_PER_DAY):
        sim.step()
        assert not state_violations(sim)  # the map invariants now hold every body's tile to its place
        before = {a["id"]: (a["place"], sim.tile(a)) for a in sim.state["agents"] if a["place"] is not None}
        for a in sim.state["agents"]:
            t = sim.tile(a)
            assert on_map(sim.map, t)
            if a["place"] is None:
                walking += 1
            else:
                at_place += 1
                assert place_at(sim.map, t) == a["place"] and t == slot(own_tiles(sim.map, a["place"]), a["id"])
        # who else is there never moves it: the same villager at the same place stands on the same tile
        assert all(sim.tile(sim.state["agents"][i]) == t for i, (_, t) in before.items())
    assert at_place and walking


def _walker(sim, long=False):
    """Step until someone is on a route (one of two ticks or more, if `long`), and return that villager."""
    for _ in range(4 * TICKS_PER_DAY):
        for a in sim.state["agents"]:
            r = a["route"]
            if r and (not long or r["arrive"] - r["depart"] >= 2) and r["arrive"] > sim.state["tick"]:
                return a
        sim.step()
    raise AssertionError("no walker found")


def test_a_walkers_position_is_its_route_at_any_time_and_its_tile_is_the_floor():
    sim = Sim(3, 100, StubMind())
    a = _walker(sim, long=True)
    r, tick = a["route"], sim.state["tick"]
    to = sim.map["places"][r["to"]]["anchor"]
    # reach-1: a body is seen one tick behind its route, so a walk shows from its start (Sim.pos)
    assert sim.pos(a) == sim.pos(a, tick) == position(r, to, tick - 1) and sim._pos(a) == position(r, to, tick)
    last = None
    for k in range(0, 601, 7):  # sim-seconds into the tick (contracts B2.1: steps of 1/600)
        t = tick + Fraction(k, 600)
        p = sim.pos(a, t)
        assert p == position(r, to, t - 1)
        gone = abs(p[0] - r["from"][0]) + abs(p[1] - r["from"][1])
        full = abs(to[0] - r["from"][0]) + abs(to[1] - r["from"][1])
        assert gone == min(full, WALK * (t - 1 - r["depart"])), "WALK tiles a tick along the L path"
        assert p[1] == r["from"][1] or p[0] == to[0], "x first, then y"
        assert sim.tile(a, t) == [int(p[0] // 1), int(p[1] // 1)] and all(type(v) is int for v in sim.tile(a, t))
        assert last is None or gone >= last
        last = gone
    assert any(sim.pos(a, tick + Fraction(k, 600)) != sim.tile(a, tick + Fraction(k, 600)) for k in range(600))
    # floor, not truncation: just west of x = 0 is tile -1
    assert [int(v // 1) for v in (Fraction(-1, 2), Fraction(1, 2), Fraction(-3, 1))] == [-1, 0, -3]


def test_reading_positions_changes_nothing():
    sim, b = awake_body()
    before = sim.state_hash()
    for a in sim.state["agents"]:
        sim.pos(a), sim.tile(a, sim.state["tick"] + Fraction(599, 600))
        sim.reach(a, sim.state["agents"][b]), sim.sees(sim.state["agents"][b], a)
    inhabitant.view(sim, b)
    assert sim.state_hash() == before


def test_one_place_is_in_reach_and_sight_and_two_places_hide_each_other():
    sim, b = awake_body(seed=3, n=100)
    agents = sim.state["agents"]
    me = next(a for a in agents if a["place"] == "plaza")
    here = [a for a in agents if a["place"] == "plaza" and a is not me]
    assert here and all(sim.reach(me, a) and sim.sees(me, a) and sim.sees(a, me) for a in here)
    # the market and the home lot beside it share an edge: bodies a tile apart there neither see nor reach
    pairs = [(p, q) for p in sim.map["places"] for q in sim.map["places"] if p < q
             and any(near(s, t, 1) for s in own_tiles(sim.map, p) for t in own_tiles(sim.map, q))]
    assert pairs, "the map has two places that touch"
    x, y = dict(me, place=pairs[0][0], route=None), dict(here[0], place=pairs[0][1], route=None)
    x.pop("at", None), y.pop("at", None)
    assert not sim.sees(x, y) and not sim.reach(x, y)


def test_in_the_open_sight_is_six_tiles_and_reach_is_the_next_tile():
    sim, b = awake_body(seed=3, n=100)
    me = sim.state["agents"][b]
    walk_to(sim, open_tile(sim))
    assert me["place"] is None and me["at"] == open_tile(sim)
    v = inhabitant.view(sim, b)
    assert v["present"] == [] and v["pos"] == open_tile(sim)
    truth = {a["id"] for a in sim.state["agents"] if a["id"] != b and near(sim.tile(a), me["at"], SEE)}
    now = [s for s in v["seen"] if s.get("in", [0])[0] == 0]  # reach-1: the rest walk into sight later this tick
    assert truth and {s["id"] for s in now} == truth == set(inhabitant.sight_ids(sim, b))
    for s in v["seen"]:
        if s not in now:  # only the tiles it will be seen on, from the first: no route, no place
            assert set(s) == {"id", "name", "role", "place", "pos", "route", "in", "track"} and s["route"] is None
            assert s["pos"] == s["track"][0][1:] and all(near(k[1:], me["at"], SEE) for k in s["track"])
    for s in now:  # what draws each one: its tile, its place, and its route if it walks
        a = sim.state["agents"][s["id"]]
        # reach-1: one a tile away is in reach and shows whether it sleeps; the rest only where they stand
        assert set(s) == {"id", "name", "role", "place", "pos", "route"} | ({"reach", "asleep"} if sim.reach(me, a) else set()) \
            | ({"in", "track"} if a["route"] else set())
        assert s.get("reach", False) is sim.reach(me, a)
        assert s["pos"] == sim.tile(a) and s["place"] == a["place"] and (s["route"] is None) == (a["route"] is None)
    # reach: the body's own tile or the next one, diagonals too; two tiles off is out of reach, but in sight
    plaza = [a for a in sim.state["agents"] if a["place"] == "plaza"]
    assert plaza
    o = plaza[0]
    x, y = sim.tile(o)
    for dx, dy, reach in ((0, 0, True), (1, 1, True), (-1, 0, True), (2, 0, False), (0, -2, False), (SEE, SEE, False)):
        ghost = dict(me, at=[x + dx, y + dy], place=None)
        assert sim.reach(ghost, o) is reach and sim.reach(o, ghost) is reach
        assert sim.sees(ghost, o) and sim.sees(o, ghost)
    far = dict(me, at=[x + SEE + 1, y], place=None)
    assert not sim.sees(far, o) and not sim.reach(far, o)
    assert REACH == 1 and SEE == 6


def test_from_inside_a_place_it_sees_a_walker_passing_within_six_tiles():
    sim = Sim(3, 100, StubMind())
    sim.run_until(TICKS_PER_DAY // 2)
    seen = 0
    for _ in range(TICKS_PER_DAY // 2):
        sim.step()
        for a in sim.state["agents"]:
            if a["place"] is None or a["asleep"]:
                continue
            v = inhabitant.view(sim, a["id"])
            ids = {p["id"] for p in v["present"]}
            assert not ids & {s["id"] for s in v["seen"]}, "seen holds only those who aren't here with it"
            for s in v["seen"]:  # walkers: in sight now with their route, or (reach-1) walking into sight later this tick
                assert s["place"] is None and (s["route"] is not None) == (s["in"][0] == 0) and near(s["pos"], v["pos"], SEE)
                seen += 1
    assert seen


def test_the_first_step_sets_out_from_where_the_body_stands_in_the_place():
    sim, b = awake_body()
    a = sim.state["agents"][b]
    stood = sim.tile(a)
    assert stood == slot(own_tiles(sim.map, a["place"]), b) and inhabitant.view(sim, b)["pos"] == stood
    sim.step([walk(stood[0] + 1, stood[1])])
    m = moved(sim, b)[-1]
    assert m.data["from"] == stood and m.data["pos"] == [stood[0] + 1, stood[1]] == sim.tile(a)


def test_an_inhabitant_witnesses_what_someone_in_sight_does_and_nothing_beyond(tmp_path):
    r = live.LiveRunner(str(tmp_path / "w"), StubMind(), 3, 100)
    while r.sim.state["tick"] < TICKS_PER_DAY // 2:
        r.step()
    b = next(a["id"] for a in r.sim.state["agents"] if not a["asleep"] and a["place"] == "plaza"
             and a["sleep_tick"] > r.sim.state["tick"] + 40)
    assert r.inhabit(b, "c1") is None
    r.step()
    tile, seen_others, k = open_tile(r.sim), 0, 0
    for _ in range(30):  # stand in the open beside the plaza: villagers at the plaza's near edge and walkers are in sight
        k += 1
        assert r.submit(intent(f"w{k}", "act", {"action": "walk", "to": tile}, r.sim.state["tick"]),
                        client="c1", scope="inhabitant") is None
        n = len(r.sim.events)
        r.step()
        tick = r.sim.state["tick"] - 1
        events = [json.loads(e.to_json()) for e in r.sim.events[n:]]
        m = r.inhabitant_tick(tick, events, b)
        sight = set(inhabitant.sight_ids(r.sim, b))
        assert {p["id"] for p in m["view"]["present"] + m["view"]["seen"] if p.get("in", [0])[0] == 0} == sight
        for e in m["events"]:
            mine = e["actor"] == b or e["kind"] == "fire" or b in [e["data"].get(f) for f in inhabitant.NAMES]
            assert mine or e["actor"] in sight, ("an event from beyond its sight", e)
            seen_others += not mine
    assert r.sim.state["agents"][b]["place"] is None and seen_others, "it saw someone act from the open"
    r.close()


@pytest.mark.parametrize("seed", [1, 2])
def test_a_session_of_walks_keeps_reach_and_sight_sound_at_sampled_times_and_replays(seed):
    sim, b = session(seed, agents=40, days=1)
    rng, agents = random.Random(seed), sim.state["agents"]
    me = agents[b]
    checked = 0
    for _ in range(TICKS_PER_DAY // 2):
        sim.step([walk(*rng.choice([p["anchor"] for p in sim.map["places"].values()]))] if rng.random() < 0.5 else [])
        assert not state_violations(sim)
        for k in (0, 1, 299, 599):
            t = sim.state["tick"] + Fraction(k, 600)
            for o in agents:
                if o is me:
                    continue
                reach, sees = sim.reach(me, o, t), sim.sees(me, o, t)
                assert reach == sim.reach(o, me, t) and sees == sim.sees(o, me, t), "both ways"
                assert sees or not reach, "what is in reach is in sight"
                together = me["place"] is not None and me["place"] == o["place"]
                apart = near(sim.tile(me, t), sim.tile(o, t), SEE)
                assert sees == (together or ((me["place"] is None or o["place"] is None) and apart))
                checked += sees
    assert checked
    sim.finish()
    events = [Event(**json.loads(e.to_json())) for e in sim.events]
    assert replay(events).state_hash() == sim.state_hash()
