"""things-1 (plan/v0-contracts-spec.md B3; Steering 2026-11-23 22:03Z).

Unique things are records in one ledger, `{id, kind, at, holder}`, of kinds one table lists (asiimov/things.py).
They appear at the first possess in a world whose stakes hold `things`, grow again at dawn, and go only by
being used. A player takes, puts down, uses and hands them over, by tile; a fixture's use is the verb done
there. A world without the stake, or with no player, has none, so every earlier world's log stands."""
import collections
import json
import os
import random

import pytest

from asiimov import inhabitant, live, persist, things, verbs
from asiimov import sim as S
from asiimov.events import Event, read_jsonl
from asiimov.invariants import Checker, state_violations
from asiimov.minds import StubMind
from asiimov.params import generate_population
from asiimov.replay import replay
from asiimov.sim import DAWN, SUB, TICKS_PER_DAY, WORKPLACE, Sim
from asiimov.things import CARRY, DEFAULTS, FIXTURES, GROWN, THINGS
from asiimov.worldmap import REACH, SEE, distance, near, own_tiles, place_at
from test_inhabitant import intent
from test_time import at
from test_walk import walk, walk_to

COST_GAP = S.act_cost("take")


def stakes(**over):
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg.update(over)
    return {**json.loads(json.dumps(S.DEFAULT_STAKES)), "things": cfg}


def world(seed=5, n=30, mind=None, possess=True, role=None, **over):
    """A world with things at midday, an awake adult body possessed. Returns (sim, body id)."""
    pop = generate_population(seed, n)
    pop["stakes"] = stakes(**over)
    sim = Sim(seed, n, mind or StubMind(), genesis=pop)
    sim.run_until(TICKS_PER_DAY // 2)
    b = next(a["id"] for a in sim.state["agents"] if not a["asleep"] and a["place"] is not None
             and a["sleep_tick"] > sim.state["tick"] + 40 and a["role"] in WORKPLACE and role in (None, a["role"]))
    if possess:
        sim.step([{"kind": "possess", "agent": b}])
    return sim, b


def act(action, thing=None, target=None, **more):
    return {"kind": "act", "action": action, **({} if thing is None else {"thing": thing}),
            **({} if target is None else {"target": target}), **more}


def do(sim, *acts):
    """Step once with these acts. Returns the receipt of the last one."""
    n = len(sim.events)
    sim.step(list(acts))
    assert not state_violations(sim)
    eid = [e.id for e in sim.events[n:] if e.kind == "act"][-1]
    return next(e for e in sim.events[eid:] if e.kind == "receipt" and e.parents[0] == eid)


def walk_checked(sim, check, tile):
    """walk_to under the invariant checker, which follows every body and thing from the log."""
    me = sim.state["agents"][sim.state["player"]["agent"]]
    while sim.tile(me) != list(tile):
        bad = check.step([walk(*tile)])
        assert not bad, bad


def lying(sim, kind):
    return [r for r in sim.state["things"]["list"] if r["kind"] == kind and r["at"] is not None]


def nearest(sim, b, kind):
    me = sim.tile(sim.state["agents"][b])
    return min(lying(sim, kind), key=lambda r: (distance(me, r["at"]), r["id"]))


def replays(sim):
    sim.finish()
    events = [Event(**json.loads(e.to_json())) for e in sim.events]
    assert replay(events).state_hash() == sim.state_hash()


# ------------------------------------------------------------------ the kinds table
def test_the_kinds_table_is_whole():
    assert 1 <= len(THINGS) <= 6 and set(FIXTURES) | set(GROWN) == set(THINGS)  # B3.2
    for kind, row in THINGS.items():
        assert set(row) == {"carry", "use", "own", "cost", "stands", "grows", "words"}, kind
        assert type(row["carry"]) is bool and type(row["cost"]) is int and row["cost"] >= 0, kind
        use = row["use"]
        assert use is None or set(use) in ({"food"}, {"verb"}), kind
        if use and "verb" in use:  # a fixture's use is a verb a player already has, with no target
            v = verbs.VERBS[use["verb"]]
            assert kind in FIXTURES and "player" in v["who"] and v["takes"] is None and v["cost"] == row["cost"], kind
        assert row["own"] in (None, "work", "home") and (row["own"] is None or kind in FIXTURES), kind
        w = row["words"]
        assert w["name"] and ("use" in w) == (use is not None), kind
        assert all((k in w) == row["carry"] for k in ("take", "drop", "hand", "gift")), kind
        assert not row["carry"] or "%s" in w["hand"], kind
    assert S.act_cost("use") == S.act_cost("take") == S.act_cost("drop") == S.act_cost("hand") == COST_GAP
    assert all(row["cost"] * verbs.MINUTE == COST_GAP for row in THINGS.values())
    assert verbs.THING_ACTS == ("take", "drop", "use", "hand") and verbs.HAND_ACTS == ("hand",)
    assert set(verbs.THING_ACTS) <= set(S.PLAYER_ACTIONS) and not set(verbs.THING_ACTS) & set(S.DELIBERATE_ACTIONS)
    cw = things.client_words()
    assert cw["name"].keys() == THINGS.keys() and cw["take"] == {"flower": "Pick up the flower"}


# ------------------------------------------------------------------ who has things
def test_a_world_without_the_stake_has_no_things_and_says_so():
    sim = Sim(5, 30, StubMind())
    sim.run_until(TICKS_PER_DAY // 2)
    b = next(a["id"] for a in sim.state["agents"] if not a["asleep"] and a["place"] is not None)
    sim.step([{"kind": "possess", "agent": b}])
    assert "things" not in sim.state and not any(e.kind == "things" for e in sim.events)
    for a in (act("take", 0), act("use", 0), act("drop", 0), act("hand", 0, 1)):
        r = do(sim, a)
        assert (r.data["ok"], r.data["why"], r.data["thing"]) == (False, "nothing there", 0)
    v = inhabitant.view(sim, b)
    assert "things" not in v and "carried" not in v and not set(verbs.THING_ACTS) & set(v["offer"])
    assert not Checker(sim).step([])
    replays(sim)


def test_with_no_player_a_world_has_no_things_and_writes_the_log_it_wrote_without_the_stake():
    pop = generate_population(2, 40)
    pop["stakes"] = stakes()
    plain, staked = Sim(2, 40, StubMind()), Sim(2, 40, StubMind(), genesis=pop)
    check = Checker(staked)
    for _ in range(2 * TICKS_PER_DAY):
        plain.step([])
        assert not check.step([])
    assert "things" not in staked.state
    assert staked.events[0].data["population"]["stakes"]["things"] == DEFAULTS

    def lines(sim):  # every event after genesis; a checkpoint's hash is of the state, which holds the stakes
        return [e.to_json() if e.kind != "checkpoint" else (e.id, e.tick, e.data["day"]) for e in sim.events[1:]]
    assert lines(plain) == lines(staked) and sum(e.kind == "checkpoint" for e in plain.events) == 2


def test_things_appear_at_the_first_possess_each_on_its_tile():
    sim, b = world()
    made = [e for e in sim.events if e.kind == "things"]
    possess = next(e for e in sim.events if e.kind == "possess")
    assert len(made) == 1 and made[0].parents == [possess.id] and made[0].origin == "rule" and made[0].actor is None
    have = sim.state["things"]["list"]
    assert [{k: r[k] for k in ("id", "kind", "at")} for r in have] == made[0].data["things"]
    assert [r["id"] for r in have] == list(range(len(have))) and sim.state["things"]["next"] == len(have)
    assert all(r["holder"] is None for r in have)
    places = sim.map["places"]
    homes = sorted({a["home"] for a in sim.state["agents"]})
    by = collections.defaultdict(list)
    for r in have:
        by[r["kind"]].append(r["at"])
    assert by["stall"] == [places["market"]["anchor"]]
    assert sorted(place_at(sim.map, t) for t in by["bench"]) == sorted(set(WORKPLACE.values()))
    assert sorted(by["bed"]) == sorted(places[h]["anchor"] for h in homes)
    for kind in GROWN:
        place, n = THINGS[kind]["grows"]
        assert len(by[kind]) == n == DEFAULTS["grows"][kind] and all(place_at(sim.map, t) == place for t in by[kind])
        assert made[0].data["draws"][kind] == {"stream": f"things:{kind}:0"}
    tiles = [tuple(r["at"]) for r in have]
    assert len(set(tiles)) == len(tiles)  # no two on one tile
    # the same world makes the same things; another seed puts what grows elsewhere
    again, _ = world()
    other, _ = world(seed=6)
    assert again.state["things"] == sim.state["things"]
    assert sorted(by["flower"]) != sorted(r["at"] for r in lying(other, "flower"))
    # possessing again, the same body or another, makes nothing
    sim.step([{"kind": "release"}])
    sim.step([{"kind": "possess", "agent": b}])
    sim.step([{"kind": "possess", "agent": next(a["id"] for a in sim.state["agents"] if a["id"] != b)}])
    assert sum(e.kind == "things" for e in sim.events) == 1 and len(sim.state["things"]["list"]) == len(have)
    replays(sim)


def test_what_grows_lands_on_the_free_tiles_with_the_lowest_keyed_scores():
    sim, b = world()
    tiles = own_tiles(sim.map, "woods")
    fixtures = {tuple(r["at"]) for r in sim.state["things"]["list"] if r["kind"] in FIXTURES}
    taken = set(fixtures)
    for kind in GROWN:  # in the table's order, each passing over the tiles taken before it
        free = [k for k, t in enumerate(tiles) if tuple(t) not in taken]
        want = sorted(free, key=lambda k: (sim.rng.at(f"things:{kind}:0", k, 0, 999999), k))[:DEFAULTS["grows"][kind]]
        assert [r["at"] for r in lying(sim, kind)] == [tiles[k] for k in want]
        taken |= {tuple(tiles[k]) for k in want}


# ------------------------------------------------------------------ take, carry, put down
def test_a_flower_is_taken_from_its_tile_or_the_next_carried_and_put_down():
    sim, b = world()
    me = sim.state["agents"][b]
    fl = nearest(sim, b, "flower")
    spot = list(fl["at"])
    r = do(sim, act("take", fl["id"]))
    assert (r.data["ok"], r.data["why"], r.data["kind"]) == (False, "not in reach", "flower")
    walk_to(sim, [spot[0] + 2, spot[1]])
    assert do(sim, act("take", fl["id"])).data["why"] == "not in reach"  # two tiles off, inside the same place
    walk_to(sim, [spot[0] + 1, spot[1] + 1])
    n = len(sim.events)
    r = do(sim, act("take", fl["id"]))
    took = next(e for e in sim.events[n:] if e.kind == "took")
    assert r.data == {"action": "take", "ok": True, "why": "took", "thing": fl["id"], "kind": "flower"}
    assert took.data == {"thing": fl["id"], "kind": "flower", "from": spot} and took.actor == b and took.origin == "rule"
    assert took.parents == [r.parents[0]] and r.parents == [took.parents[0], took.id]
    assert (fl["at"], fl["holder"]) == (None, b) and sim.carried(b) == [fl]
    assert do(sim, act("take", fl["id"])).data["why"] == "already carried"
    walk_to(sim, [spot[0] + 5, spot[1]])
    n = len(sim.events)
    r = do(sim, act("drop", fl["id"]))
    dropped = next(e for e in sim.events[n:] if e.kind == "dropped")
    assert r.data["why"] == "dropped" and dropped.data == {"thing": fl["id"], "kind": "flower", "at": sim.tile(me)}
    assert (fl["at"], fl["holder"]) == (sim.tile(me), None) and not sim.carried(b)
    assert do(sim, act("drop", fl["id"])).data["why"] == "not carried"
    assert do(sim, act("take", 10 ** 6)).data["why"] == "nothing there"
    replays(sim)


def test_what_cant_be_carried_stays_and_full_hands_take_no_more():
    sim, b = world(carry=1)
    bush = nearest(sim, b, "berries")
    walk_to(sim, bush["at"])
    assert do(sim, act("take", bush["id"])).data["why"] == "can't be carried"
    first, second = sorted(lying(sim, "flower"), key=lambda r: distance(bush["at"], r["at"]))[:2]
    walk_to(sim, first["at"])
    assert do(sim, act("take", first["id"])).data["why"] == "took"
    walk_to(sim, second["at"])
    assert do(sim, act("take", second["id"])).data["why"] == "hands full"
    assert do(sim, act("drop", first["id"])).data["why"] == "dropped"
    assert do(sim, act("take", second["id"])).data["why"] == "took"
    assert CARRY == 4 and DEFAULTS["carry"] == CARRY


def test_a_thing_act_holds_the_body_for_its_cost_and_a_sleeper_takes_nothing():
    sim, b = world()
    fl = nearest(sim, b, "flower")
    walk_to(sim, fl["at"])
    n = len(sim.events)
    sim.step([act("take", fl["id"]), at(act("drop", fl["id"]), COST_GAP - 1), at(act("drop", fl["id"]), COST_GAP)])
    whys = [e.data["why"] for e in sim.events[n:] if e.kind == "receipt" and e.actor == b]
    assert whys == ["took", "busy", "dropped"]
    sim.state["agents"][b]["asleep"] = True
    assert do(sim, act("take", fl["id"])).data["why"] == "asleep"


# ------------------------------------------------------------------ use
def test_berries_are_picked_into_food_and_go():
    sim, b = world()
    me, check = sim.state["agents"][b], Checker(sim)
    bush = nearest(sim, b, "berries")
    walk_checked(sim, check, [bush["at"][0], bush["at"][1] + 1])
    food, n, total = me["inv"]["food"], len(sim.events), len(sim.state["things"]["list"])
    assert not check.step([act("use", bush["id"])])  # the food ledger counts what the ground gave
    used = next(e for e in sim.events[n:] if e.kind == "used")
    r = next(e for e in sim.events[n:] if e.kind == "receipt" and e.data["action"] == "use")
    assert used.data == {"thing": bush["id"], "kind": "berries", "food": 1} and used.actor == b
    assert r.data == {"action": "use", "ok": True, "why": "picked", "thing": bush["id"], "kind": "berries", "food": 1}
    assert r.parents[1] == used.id
    assert me["inv"]["food"] == food + 1 and sim.thing(bush["id"]) is None
    assert len(sim.state["things"]["list"]) == total - 1 and sim.state["things"]["next"] == total
    assert do(sim, act("use", bush["id"])).data["why"] == "nothing there"
    fl = nearest(sim, b, "flower")
    assert do(sim, act("use", fl["id"])).data["why"] == "not in reach"
    walk_to(sim, fl["at"])
    assert do(sim, act("use", fl["id"])).data["why"] == "no use"
    replays(sim)


def test_a_fixtures_use_is_the_verb_done_there_by_the_verbs_own_rule():
    sim, b = world(role="farmer")
    me = sim.state["agents"][b]
    stall = lying(sim, "stall")[0]
    assert do(sim, act("use", stall["id"])).data["why"] == "not in reach"
    walk_to(sim, stall["at"])
    assert me["place"] == "market"
    n, food = len(sim.events), me["inv"]["food"]
    r = do(sim, act("use", stall["id"]))
    assert not any(e.kind == "used" for e in sim.events[n:])  # nothing of the stall is used up
    assert r.data["action"] == "buy_food" and r.data["why"] in verbs.reasons("buy_food", "player")
    assert r.data["ok"] and r.data["why"] in ("bought", "had enough") and me["inv"]["food"] == food + r.data["food"]
    benches = {place_at(sim.map, r["at"]): r for r in lying(sim, "bench")}
    theirs, mine = benches["market"], benches[WORKPLACE[me["role"]]]
    assert not sim.thing_owner(me, theirs) and sim.thing_owner(me, mine)
    walk_to(sim, theirs["at"])
    r = do(sim, act("use", theirs["id"]))
    assert (r.data["action"], r.data["why"]) == ("use", "not yours")
    walk_to(sim, mine["at"])
    r = do(sim, act("use", mine["id"]))
    assert (r.data["action"], r.data["ok"], r.data["why"]) == ("work", True, "started") and me["activity"] == "work"
    bed = next(r for r in lying(sim, "bed") if place_at(sim.map, r["at"]) == me["home"])
    other = next(r for r in lying(sim, "bed") if place_at(sim.map, r["at"]) != me["home"])
    walk_to(sim, other["at"])
    assert do(sim, act("use", other["id"])).data["why"] == "not yours"
    walk_to(sim, bed["at"])
    r = do(sim, act("use", bed["id"]))
    assert (r.data["action"], r.data["ok"], r.data["why"]) == ("rest", True, "started") and me["activity"] == "rest"
    assert do(sim, act("take", bed["id"])).data["why"] == "can't be carried"
    replays(sim)


# ------------------------------------------------------------------ growth at dawn
def test_at_dawn_the_ground_makes_up_what_was_picked_and_taken_and_nothing_else():
    sim, b = world()
    check = Checker(sim)
    bushes = sorted(lying(sim, "berries"), key=lambda r: r["id"])[:2]
    for bush in bushes:
        walk_checked(sim, check, bush["at"])
        assert not check.step([act("use", bush["id"])])
    fl = nearest(sim, b, "flower")
    walk_checked(sim, check, fl["at"])
    assert not check.step([act("take", fl["id"])])
    assert (len(lying(sim, "berries")), len(lying(sim, "flower"))) == (4, 3)
    before = {r["id"] for r in sim.state["things"]["list"]}
    while sim.state["tick"] % TICKS_PER_DAY != DAWN:
        assert not check.step([])
    assert not any(e.kind == "grew" for e in sim.events)
    assert not check.step([])
    grew = [e for e in sim.events if e.kind == "grew"]
    day = grew[0].tick // TICKS_PER_DAY
    assert len(grew) == 1 and grew[0].tick % TICKS_PER_DAY == DAWN and grew[0].origin == "rule"
    assert grew[0].parents == [next(e.id for e in sim.events if e.kind == "things")]
    assert collections.Counter(t["kind"] for t in grew[0].data["things"]) == {"berries": 2, "flower": 1}
    assert grew[0].data["draws"] == {k: {"stream": f"things:{k}:{day}"} for k in ("berries", "flower")}
    assert (len(lying(sim, "berries")), len(lying(sim, "flower"))) == (6, 4) and sim.carried(b) == [fl]
    new = [r for r in sim.state["things"]["list"] if r["id"] not in before]
    assert sorted(r["id"] for r in new) == [t["id"] for t in grew[0].data["things"]] and min(r["id"] for r in new) > max(before)
    tiles = [tuple(r["at"]) for r in sim.state["things"]["list"] if r["at"] is not None]
    assert len(set(tiles)) == len(tiles)
    for _ in range(TICKS_PER_DAY):  # a day with nothing picked: the next dawn makes nothing and logs nothing
        assert not check.step([])
    assert sum(e.kind == "grew" for e in sim.events) == 1
    replays(sim)


# ------------------------------------------------------------------ handing a thing over
def carrying(sim, b):
    """The body walks to the nearest flower and takes it."""
    fl = nearest(sim, b, "flower")
    walk_to(sim, fl["at"])
    assert do(sim, act("take", fl["id"])).data["why"] == "took"
    return fl


def someone_awake(sim, b):
    """Walk the body to the plaza or the market until someone awake is there with it. Returns that one."""
    me = sim.state["agents"][b]
    for place in ("plaza", "market", "field", "workshop"):
        walk_to(sim, sim.map["places"][place]["anchor"])
        there = [o for o in sim.state["agents"] if o["id"] != b and o["place"] == place and not o["asleep"]]
        if there and me["place"] == place:
            return there[0]
    raise AssertionError("nobody awake anywhere")


def test_handing_a_flower_over_is_a_gift_by_give_foods_rule():
    sim, b = world()
    me = sim.state["agents"][b]
    fl = carrying(sim, b)
    far = next(o for o in sim.state["agents"] if o["id"] != b and not sim.reach(me, o))
    assert do(sim, act("hand", fl["id"], far["id"])).data["why"] == "target not here"
    assert do(sim, act("hand", fl["id"], b)).data["why"] == "unknown target"
    assert do(sim, act("hand", 10 ** 6, far["id"])).data["why"] == "nothing there"
    other = nearest(sim, b, "flower")
    assert do(sim, act("hand", other["id"], far["id"])).data["why"] == "not carried"
    v = someone_awake(sim, b)
    warmth = (v["relationships"].get(str(b)) or {"affinity": 0})["affinity"]
    n = len(sim.events)
    r = do(sim, act("hand", fl["id"], v["id"]))
    handed = next(e for e in sim.events[n:] if e.kind == "handed")
    assert r.data == {"action": "hand", "ok": True, "why": "handed", "thing": fl["id"], "kind": "flower", "to": v["id"]}
    assert handed.data == {"thing": fl["id"], "kind": "flower", "to": v["id"]} and r.parents[1] == handed.id
    assert (fl["at"], fl["holder"]) == (None, v["id"]) and not sim.carried(b) and sim.carried(v["id"]) == [fl]
    assert v["relationships"][str(b)]["affinity"] == min(1000, warmth + 100) and v["relationships"][str(b)]["cause"] == r.id
    assert any("gave a flower to " + v["name"] in str(m) for m in me["recent"])
    assert any(me["name"] + " gave me a flower" in str(m) for m in v["recent"])
    # the receiver may stop to think about it, as after a gift of food (the row check below reads its prompt)
    occ = {"why": "gift", "who": b, "detail": THINGS["flower"]["words"]["gift"]}
    assert sim._occasion_text(v, occ) == f" Why you stop to think: {sim._who(b)} gave you a flower."
    assert do(sim, act("take", fl["id"])).data["why"] == "not in reach"  # it is theirs now
    assert not state_violations(sim)
    replays(sim)


def test_a_sleeper_and_full_hands_take_no_gift():
    sim, b = world(carry=1)
    first = carrying(sim, b)
    v = someone_awake(sim, b)
    assert do(sim, act("hand", first["id"], v["id"])).data["why"] == "handed"
    second = carrying(sim, b)
    walk_to(sim, sim.map["places"][v["place"] or "plaza"]["anchor"])
    if sim.reach(sim.state["agents"][b], v) and not v["asleep"]:
        r = do(sim, act("hand", second["id"], v["id"]))
        assert (r.data["ok"], r.data["why"], r.data["to"]) == (False, "target hands full", v["id"])
    w = someone_awake(sim, b)
    w["asleep"] = True
    assert do(sim, act("hand", second["id"], w["id"])).data["why"] == "target asleep"
    assert sim.carried(b) == [second]


class Capture(StubMind):
    """A stub that keeps every prompt the engine hands it."""

    def __init__(self):
        self.prompts = []

    def decide(self, req):
        self.prompts.append((req.tick, req.agent_id, req.kind, req.prompt))
        return super().decide(req)


@pytest.mark.parametrize("kind", [k for k, row in THINGS.items() if row["carry"]])
def test_row_check_a_gift_of_a_thing_reads_as_a_food_gift_but_for_the_giver_and_the_thing(kind):
    """Steering 2026-11-23 22:03Z, in L39's style, at no cost: per kind, the receiver's prompt after the gift
    differs from its prompt after a gift of food only in the occasion's sentence and the memory's note, and
    those name exactly the giver and the thing. Twin worlds, one giving 1 food and one handing the thing (with
    the same food moved by hand, so what each carries is the same)."""
    gift, found = THINGS[kind]["words"]["gift"], 0
    for pick in range(6):
        twins = []
        for arm in ("food", "thing"):
            mind = Capture()
            sim, b = world(seed=5, n=30, mind=mind)
            me = sim.state["agents"][b]
            me["inv"]["food"] = 3
            offered = inhabitant.view(sim, b)["offer"].get("give_food", [])  # those it may name, here and awake
            here = [sim.state["agents"][o] for o in offered
                    if sim.state["agents"][o]["place"] == me["place"] and not sim.state["agents"][o]["asleep"]]
            if pick >= len(here):
                break
            v = here[pick]
            if arm == "food":
                r = do(sim, act("give_food", target=v["id"]))
                assert r.data["why"] in ("started", "gave")
            else:
                thing = sim._new_thing(kind, [0, 0])
                thing["at"], thing["holder"] = None, b
                me["inv"]["food"] -= 1
                v["inv"]["food"] += 1
                assert do(sim, act("hand", thing["id"], v["id"])).data["why"] == "handed"
            start = sim.state["tick"]
            for _ in range(6):
                sim.step([])
            twins.append((me["name"], sim._who(b), [p for p in mind.prompts if p[1] == v["id"] and p[0] >= start - 1 and p[2] == "deliberate"]))
        if len(twins) < 2:
            break
        (giver, who, food), (_, _, thing) = twins
        food = [p for p in food if "gave you 1 food" in p[3]]
        thing = [p for p in thing if f"gave you {gift}" in p[3]]
        if not food or not thing:
            continue  # this one didn't stop to think about it
        (t1, _, _, a), (t2, _, _, c) = food[0], thing[0]
        assert t1 == t2
        la, lc = a.split("\n"), c.split("\n")
        assert len(la) == len(lc)
        diff = [(x, y) for x, y in zip(la, lc) if x != y]
        assert diff
        for x, y in diff:
            assert y == x.replace(f"{who} gave you 1 food", f"{who} gave you {gift}").replace(
                f"{giver} gave me 1 food", f"{giver} gave me {gift}"), (x, y)
        assert f"Why you stop to think: {who} gave you {gift}." in c and f"{giver} gave me {gift}" in c
        assert c.count(gift) == a.count(f"{who} gave you 1 food") + a.count(f"{giver} gave me 1 food") == 2
        found += 1
    assert found >= 1


# ------------------------------------------------------------------ the view
def test_the_view_shows_things_in_sight_what_is_carried_and_what_can_be_done():
    sim, b = world(role="farmer")
    me = sim.state["agents"][b]
    v = inhabitant.view(sim, b)
    ids = {t["id"]: t for t in v["things"]}
    fixtures = [r for r in sim.state["things"]["list"] if r["kind"] in FIXTURES]
    assert me["place"] != "woods"
    # fixtures are map features, always listed; what lies in the woods isn't seen from elsewhere
    assert set(ids) == {r["id"] for r in fixtures} and v["carried"] == []
    yours = sorted(t["kind"] for t in v["things"] if t.get("yours"))
    assert yours == ["bed", "bench"]
    assert all(set(t) <= {"id", "kind", "pos", "reach", "yours"} for t in v["things"])
    fl = nearest(sim, b, "flower")
    walk_to(sim, [fl["at"][0] + 3, fl["at"][1]])
    v = inhabitant.view(sim, b)
    seen = {t["id"]: t for t in v["things"]}
    in_woods = {r["id"] for r in sim.state["things"]["list"] if r["kind"] in GROWN}
    assert me["place"] == "woods" and in_woods <= set(seen)  # in the place with it: everything lying there
    assert "reach" not in seen[fl["id"]] and "take" not in v["offer"]
    walk_to(sim, [fl["at"][0] + 1, fl["at"][1]])
    v = inhabitant.view(sim, b)
    seen = {t["id"]: t for t in v["things"]}
    assert seen[fl["id"]] == {"id": fl["id"], "kind": "flower", "pos": fl["at"], "reach": True}
    assert v["offer"]["take"] == [fl["id"]] and "drop" not in v["offer"] and "hand" not in v["offer"]
    do(sim, act("take", fl["id"]))
    v = inhabitant.view(sim, b)
    assert v["carried"] == [{"id": fl["id"], "kind": "flower"}] and fl["id"] not in {t["id"] for t in v["things"]}
    assert v["offer"]["drop"] == [fl["id"]] and "take" not in v["offer"]
    bush = nearest(sim, b, "berries")
    walk_to(sim, bush["at"])
    v = inhabitant.view(sim, b)
    assert bush["id"] in v["offer"]["use"] and bush["id"] not in v["offer"].get("take", [])
    # out of the woods, in the open: a thing in the open shows within SEE tiles, one inside a place doesn't
    edge = min(own_tiles(sim.map, "woods"), key=lambda t: (-t[1], abs(t[0])))
    walk_to(sim, [edge[0], edge[1] + 2])
    assert me["place"] is None and do(sim, act("drop", fl["id"])).data["why"] == "dropped"
    walk_to(sim, [edge[0], edge[1] + 2 + SEE])
    seen = {t["id"] for t in inhabitant.view(sim, b)["things"]}
    assert fl["id"] in seen and not (in_woods - {fl["id"]}) & seen
    walk_to(sim, [edge[0], edge[1] + 3 + SEE])
    assert fl["id"] not in {t["id"] for t in inhabitant.view(sim, b)["things"]}
    # with someone awake in reach, what is carried can be handed to them; a fixture's use is offered beside it
    walk_to(sim, [edge[0], edge[1] + 2])
    do(sim, act("take", fl["id"]))
    w = someone_awake(sim, b)
    v = inhabitant.view(sim, b)
    assert w["id"] in v["offer"]["hand"] and all(not sim.state["agents"][o]["asleep"] for o in v["offer"]["hand"])
    bench = next(r for r in lying(sim, "bench") if sim.thing_owner(me, r))
    walk_to(sim, bench["at"])
    v = inhabitant.view(sim, b)
    assert ("work" in v["offer"]) == (bench["id"] in v["offer"].get("use", []))
    theirs = next(r for r in lying(sim, "bench") if not sim.thing_owner(me, r))
    walk_to(sim, theirs["at"])
    assert theirs["id"] not in inhabitant.view(sim, b)["offer"].get("use", [])
    me["asleep"] = True
    v = inhabitant.view(sim, b)
    assert not set(verbs.THING_ACTS) & set(v["offer"]) and v["carried"]


# ------------------------------------------------------------------ the checker
def test_the_checker_follows_things_from_the_log_alone_and_catches_what_is_planted():
    sim, b = world()
    check = Checker(sim)
    fl = nearest(sim, b, "flower")
    walk_checked(sim, check, fl["at"])
    assert not check.step([act("take", fl["id"])])
    assert check.things.all[fl["id"]] == fl and len(check.things.all) == len(sim.state["things"]["list"])
    fl["at"] = [0, 0]  # both a tile and a holder
    bad = check.step([])
    assert any("thing" in v for v in bad) and any("differ" in v for v in bad)
    fl["at"] = None
    assert not check.step([])
    bush = lying(sim, "berries")[0]
    sim.state["things"]["list"].remove(bush)  # gone with no `used` event
    assert any("differ" in v for v in check.step([]))
    sim.state["things"]["list"].append(bush)
    sim.state["things"]["list"].sort(key=lambda r: r["id"])
    assert not check.step([])
    # a take from afar, planted in the log
    far = max(lying(sim, "flower"), key=lambda r: distance(sim.tile(sim.state["agents"][b]), r["at"]))
    check.before([])
    eid = sim._emit("took", "rule", b, {"thing": far["id"], "kind": "flower", "from": far["at"]}, [])
    far["at"], far["holder"] = None, b
    sim.step([])
    bad = check.after()
    assert any(f"event {eid}: took" in v for v in bad), bad
    for bed in lying(sim, "bed")[:1]:
        bed["at"], bed["holder"] = None, b  # a fixture in someone's hands
        assert any("thing" in v for v in check.step([]))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_session_with_things_equals_batch_holds_every_invariant_and_replays(seed):
    """Two days of a body among things: it walks to what lies about, takes, puts down, uses and hands over,
    and tries what can't work. Taken as they arrive, the log is a batch run's, the checker (with the log-only
    ledger) finds nothing, and it replays."""
    rng = random.Random(seed)
    pops = [generate_population(seed, 40) for _ in range(2)]
    for pop in pops:
        pop["stakes"] = stakes()
    early, batch = (Sim(seed, 40, StubMind(), genesis=pop) for pop in pops)
    for sim in (early, batch):
        sim.run_until(TICKS_PER_DAY // 3)
    b = next(a["id"] for a in early.state["agents"] if not a["asleep"] and a["place"] is not None and a["role"] in WORKPLACE)
    me, check, goal = early.state["agents"][b], Checker(early), None
    for i in range(2 * TICKS_PER_DAY):
        acts = [{"kind": "possess", "agent": b}] if i == 0 else []
        check.before(acts)
        for a in acts:
            early.take(a)

        def send(a, f):
            acts.append(at(a, f))
            return early.take(acts[-1])

        if i and not me["asleep"] and early.state["tick"] < me["sleep_tick"] and me["needs"]["fatigue"] < 1000:
            have = early.state["things"]["list"]
            if goal is None or early.thing(goal) is None or early.thing(goal)["at"] is None:
                grown = [r["id"] for r in have if r["at"] is not None and r["kind"] in GROWN]
                goal = rng.choice(grown if grown and rng.random() < 0.8 else [r["id"] for r in have if r["at"] is not None])
            f = rng.randrange(0, 30)
            send(walk(*early.thing(goal)["at"]), f)
            for _ in range(rng.randrange(1, 4)):
                f += COST_GAP + rng.randrange(0, 20)
                if f >= SUB:
                    break
                mine, roll = early.carried(b), rng.random()
                beside = [r for r in have if early.beside(me, r)]
                if roll < 0.45 and beside:
                    r = early.thing(goal) if early.thing(goal) in beside else rng.choice(beside)
                    send(act(rng.choice(("take", "use")), r["id"]), f)
                    goal = None if r["id"] == goal else goal
                    if goal is None:
                        break
                elif roll < 0.6 and mine:
                    send(act("drop", rng.choice(mine)["id"]), f)
                elif roll < 0.8 and mine:
                    near_by = [o["id"] for o in early.state["agents"] if o["id"] != b and early.reach(me, o)]
                    send(act("hand", rng.choice(mine)["id"], rng.choice(near_by or [rng.randrange(40)])), f)
                else:  # what can't work: a thing far off, gone, or never made
                    verb = rng.choice(verbs.THING_ACTS)
                    send(act(verb, rng.choice([r["id"] for r in have] + [10 ** 5]),
                             rng.randrange(40) if verb == "hand" else None), f)
        early.step(acts)
        batch.step(acts)
        bad = check.after()
        assert not bad, bad
        assert early.state_hash() == batch.state_hash(), early.state["tick"]
    early.finish(), batch.finish()
    log = early.events
    assert [e.to_json() for e in log] == [e.to_json() for e in batch.events]
    kinds = collections.Counter(e.kind for e in log)
    whys = collections.Counter(e.data["why"] for e in log if e.kind == "receipt" and e.actor == b)
    assert kinds["took"] > 3 and kinds["dropped"] > 1 and kinds["used"] > 3 and kinds["grew"] >= 1, kinds
    assert whys["not in reach"] and whys["nothing there"] and whys["can't be carried"], whys
    assert all(e.data["why"] in verbs.reasons(e.data["action"], "player") for e in log if e.kind == "receipt" and e.actor == b)
    events = [Event(**json.loads(e.to_json())) for e in log]
    assert replay(events).state_hash() == early.state_hash()


# ------------------------------------------------------------------ over the runner
def runner(out, seed=6, agents=20, things=True):
    r = live.LiveRunner(out, StubMind(), seed, agents, things=json.loads(json.dumps(DEFAULTS)) if things else None)
    while r.sim.state["tick"] < TICKS_PER_DAY // 2:
        r.step()
    b = next(a["id"] for a in r.sim.state["agents"] if not a["asleep"] and a["place"] is not None
             and a["sleep_tick"] > r.sim.state["tick"] + 40)
    assert r.inhabit(b, "c1") is None
    r.step()
    return r, b


def submit(r, iid, args, f=0):
    return r.submit(intent(iid, "act", args, r.sim.state["tick"]), client="c1", scope="inhabitant", f=f)


def test_over_the_runner_a_thing_act_names_its_thing_and_is_answered_at_once(tmp_path):
    out = str(tmp_path / "w")
    r, b = runner(out)
    assert persist.config(out)["things"] == DEFAULTS and "things" in r.sim.state
    for iid, args in (("x1", {"action": "take"}), ("x2", {"action": "take", "thing": -1}),
                      ("x3", {"action": "take", "thing": 1, "target": 2}), ("x4", {"action": "hand", "thing": 1}),
                      ("x5", {"action": "rest", "thing": 1}), ("x6", {"action": "use", "thing": "1"})):
        assert submit(r, iid, args)["reason"] == "bad_args", iid
    fl = nearest(r.sim, b, "flower")
    assert submit(r, "t0", {"action": "walk", "to": fl["at"]}) is None
    for _ in range(4):
        r.step()
        if r.sim.tile(r.sim.state["agents"][b]) == fl["at"]:
            break
        assert submit(r, f"w{r.sim.state['tick']}", {"action": "walk", "to": fl["at"]}) is None
    n = len(r.sim.events)
    assert submit(r, "t1", {"action": "take", "thing": fl["id"]}) is None
    (iid, ack, seen, view), = r.take_due()  # answered before the tick's step, with the body's new view
    assert iid == "t1" and ack["status"] == "applied" and [e["kind"] for e in seen] == ["act", "took", "receipt"]
    assert view["carried"] == [{"id": fl["id"], "kind": "flower"}]
    assert [e.kind for e in r.sim.events[n:]] == ["act", "took", "receipt"]
    assert r.sim.carried(b) == [fl]
    r.step()
    v = inhabitant.view(r.sim, b)
    assert v["carried"] == [{"id": fl["id"], "kind": "flower"}] and v["offer"]["drop"] == [fl["id"]]
    r.finish()
    cfg, log = persist.config(out), read_jsonl(os.path.join(out, "events.jsonl"))
    pop = generate_population(cfg["seed"], cfg["agents"])
    pop["stakes"] = stakes()
    sim = Sim(cfg["seed"], cfg["agents"], StubMind(), genesis=pop)  # the same world in one go, by run.json's schedule
    sim.run_until(log[-1].tick, {t: [a for tt, a in cfg["player"] if tt == t] for t, _ in cfg["player"]})
    sim.finish()
    assert [e.to_json() for e in sim.events] == [e.to_json() for e in log]
    assert any(a.get("thing") == fl["id"] for _, a in cfg["player"])


def test_a_runner_world_without_the_flag_has_no_things(tmp_path):
    out = str(tmp_path / "w")
    r, b = runner(out, things=False)
    assert "things" not in persist.config(out) and "things" not in r.sim.state
    assert "things" not in r.sim.events[0].data["population"]["stakes"]
