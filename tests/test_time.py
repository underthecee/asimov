"""time-1a (plan/v0-contracts-spec.md, B2.1 and B2.2): a player act carries its time inside the tick, and a walk
is bounded by walking speed over that time and applied the moment it arrives.

Every act may carry `f`, its time inside its tick in 600ths (sim-seconds), which only the live runner sets and
the act's event records, so batch and replay use the recorded value. A walk moves the body as far as walking
speed over the sim-time since its last walk allows, plus what it kept while standing (a tick's walking at
most), clamped toward its tile and never farther. Between ticks the runner hands each action to the engine as
it arrives (Sim.take): the action is recorded then and a walk moves the body then, and the step skips what was
taken, so a batch run of the same schedule writes the same log. A walk never pulls a tick forward.

The no-player bar is tests/test_space.py's, unchanged: no hash there moved. Walks on whole ticks move as
walk-1's did, which tests/test_walk.py holds, unchanged but for the one line on pulling.

time-1b (B2.3): every act but a line said lands the moment it is recorded, judged where the body stands and by
who is there then, with its receipt at once, and holds the body for its cost (a sim-minute until verbs-1's
table; an answer to an ask costs nothing). An act that finds the body still held is refused as "busy", which
replaces the tick's one act; the runner stamps an act that arrives while the body is held at the moment it is
free, so it lands. A line said keeps its tick's fixed point, one line a tick (the fallback this step names)."""
import asyncio
import json
import os
import random

import pytest

from asiimov import inhabitant, live
from asiimov.events import Event, read_jsonl
from asiimov.invariants import Checker, state_violations
from asiimov.minds import StubMind
from asiimov.replay import replay
from asiimov.sim import ANSWER_ACTS, COST, GO_ACTS, PLAYER_ACTIONS, SAY_ACTS, SUB, TICKS_PER_DAY, WALK_ACTS, Sim, act_cost
from asiimov.worldmap import WALK, bounds, distance, on_map, own_tiles, place_at
from test_inhabitant import batch_of, intent
from test_live import WS
from test_walk import awake_body, moved, walk

PER = SUB // WALK  # sim-seconds a tile takes at walking speed


def at(act, f):
    return {**act, "f": f} if f else act


def corners(sim):
    x0, y0, x1, y1 = bounds(sim.map)
    return [[x0, y0], [x1, y1], [x0, y1], [x1, y0]]


def far_corner(sim, frm):
    return max(corners(sim), key=lambda c: distance(frm, c))


def edge(sim, place, near):
    """A tile of `place` and a tile in the open beside it, the pair nearest `near`."""
    pairs = [(t, [t[0] + dx, t[1] + dy]) for t in own_tiles(sim.map, place) for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0))]
    return min(((t, o) for t, o in pairs if on_map(sim.map, o) and place_at(sim.map, o) is None),
               key=lambda p: (distance(near, p[0]), p))


# ------------------------------------------------------------------ the stamp (B2.1)
def test_an_act_records_its_time_inside_the_tick_and_leaves_it_out_at_zero():
    sim, b = awake_body()
    n = len(sim.events)
    sim.step([walk(0, 0), at(walk(0, 1), 150), at({"kind": "act", "action": "rest"}, 420)])
    acts = [e for e in sim.events[n:] if e.kind == "act"]
    assert [e.data for e in acts] == [{"action": "walk", "to": [0, 0]}, {"action": "walk", "to": [0, 1], "f": 150},
                                      {"action": "rest", "f": 420}]
    assert all(e.tick == acts[0].tick for e in acts)  # t = tick + f/600: the tick is the event's own


@pytest.mark.parametrize("f", [-1, SUB, 1.5, "3", True, None])
def test_f_is_a_whole_number_of_600ths_below_one_tick(f):
    sim, b = awake_body()
    n, before = len(sim.events), sim.state_hash()
    with pytest.raises(ValueError, match="f is a whole number"):
        sim.take({**walk(0, 0), "f": f})
    assert len(sim.events) == n and sim.state_hash() == before  # refused before it is logged


def test_acts_apply_in_order_of_t_so_an_earlier_f_after_a_later_one_is_refused():
    sim, b = awake_body()
    sim.take(at(walk(0, 0), 300))
    sim.take(at(walk(0, 1), 300))  # the same sim-second: in order of arrival
    n = len(sim.events)
    with pytest.raises(ValueError, match="never earlier"):
        sim.take(at(walk(0, 2), 299))
    assert len(sim.events) == n
    sim.step([at(walk(0, 0), 300), at(walk(0, 1), 300)])
    sim.take(walk(0, 2))  # a new tick starts at f = 0 again
    assert not state_violations(sim)


# ------------------------------------------------------------------ the walk's bound (B2.2)
def test_a_walk_goes_as_far_as_walking_speed_over_the_time_since_its_last_walk_allows():
    sim, b = awake_body(n=120)
    a = sim.state["agents"][b]
    frm = sim.tile(a)
    far = far_corner(sim, frm)
    assert distance(frm, far) > 2 * WALK
    gone = lambda: distance(frm, sim.tile(a))
    sim.take(walk(*far))  # it stood until now: a tick's walking kept, and no more
    assert gone() == WALK
    sim.take(walk(*far))  # the same instant
    assert gone() == WALK and moved(sim, b)[-1].data["from"] == moved(sim, b)[-1].data["pos"]
    sim.take(at(walk(*far), PER - 1))  # a sim-second short of a tile's time
    assert gone() == WALK
    sim.take(at(walk(*far), PER))
    assert gone() == WALK + 1
    sim.take(at(walk(*far), 4 * PER + 7))  # three tiles' time and a bit; the bit is kept
    assert gone() == WALK + 4
    sim.take(at(walk(*far), 5 * PER))
    assert gone() == WALK + 5
    acts = [at(walk(*far), f) for f in (0, 0, PER - 1, PER, 4 * PER + 7, 5 * PER)]
    sim.step(acts)
    sim.step([walk(*far)])  # a whole tick later, less the five tiles' time already walked in the last one
    assert gone() == 2 * WALK
    assert not state_violations(sim)


def test_standing_keeps_one_ticks_walking_and_no_more():
    sim, b = awake_body(n=120)
    a = sim.state["agents"][b]
    frm = sim.tile(a)
    far = far_corner(sim, frm)
    sim.step([walk(*far)])
    assert distance(frm, sim.tile(a)) == WALK
    for _ in range(4):  # it stands for four ticks
        sim.step([])
    here = sim.tile(a)
    back = far_corner(sim, here)
    assert distance(here, back) > WALK
    sim.step([at(walk(*back), 599)])
    assert distance(here, sim.tile(a)) == WALK


def test_a_near_tile_is_reached_and_the_rest_is_kept():
    sim, b = awake_body(n=120)
    a = sim.state["agents"][b]
    frm = sim.tile(a)
    far = far_corner(sim, frm)
    step = [frm[0] + (3 if far[0] > frm[0] else -3), frm[1]]
    sim.take(walk(*step))
    assert sim.tile(a) == step and sim.state["player"]["walk"] == [sim.state["tick"] * SUB, (WALK - 3) * SUB]
    sim.take(walk(*far))
    assert distance(frm, sim.tile(a)) == WALK


def flat_out(seed, ticks=6):
    """A body walked as hard as the engine lets it, toward one far corner and then the next, with walks at
    random sim-seconds. Returns (sim, body, [(t in 600ths, tiles this walk moved, whether it was cut short)])."""
    rng = random.Random(seed)
    sim, b = awake_body(seed=4, n=120)
    a = sim.state["agents"][b]
    out, goal = [], far_corner(sim, sim.tile(a))
    for _ in range(ticks):
        tick, acts = sim.state["tick"], []
        for f in sorted(rng.sample(range(SUB), rng.randint(1, 12))):
            if sim.tile(a) == goal:
                goal = far_corner(sim, goal)
            frm = sim.tile(a)
            acts.append(at(walk(*goal), f))
            sim.take(acts[-1])
            out.append((tick * SUB + f, distance(frm, sim.tile(a)), sim.tile(a) != goal))
        sim.step(acts)
        assert not state_violations(sim)
    return sim, b, out


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_a_walk_never_moves_the_body_faster_than_walking_speed(seed):
    sim, b, walks = flat_out(seed)
    assert sum(d for _, d, _ in walks) > 4 * WALK
    for i, (t0, _, short) in enumerate(walks):
        gone = 0
        for t1, d, _ in walks[i + 1:]:
            gone += d
            # Over any stretch: walking speed, plus at most the one tick's walking it had kept.
            assert gone * SUB <= WALK * (t1 - t0) + WALK * SUB, (seed, i, t0, t1, gone)
            if short:  # from a walk the bound cut short, nothing was kept: walking speed, to the tile
                assert gone * SUB < WALK * (t1 - t0) + SUB, (seed, i, t0, t1, gone)
    first = walks[0][1]
    assert first <= WALK  # the first walk of all: what a body that never walked has kept
    # and never slower than it asked for while it had the tiles: flat out, it keeps up with walking speed
    t0, tn = walks[0][0], walks[-1][0]
    assert sum(d for _, d, _ in walks) >= WALK * (tn - t0) // SUB


# ------------------------------------------------------------------ applied as it arrives (Sim.take)
def small_act(rng, sim, agents):
    """Any act a player may send but a walk, with what it needs."""
    action = rng.choice([a for a in PLAYER_ACTIONS if a not in WALK_ACTS])
    act = {"kind": "act", "action": action}
    if action in GO_ACTS:
        act["place"] = rng.choice(sorted(sim.map["places"]))
    elif action in SAY_ACTS:
        act.update(target=rng.randrange(agents), text="Good day to you.")
    elif action in ANSWER_ACTS or rng.random() < 0.8:
        act["target"] = rng.randrange(agents)
    return act


def random_tick(rng, sim, agents):
    """One tick's player acts at rising sim-seconds: walks, and now and then another act."""
    x0, y0, x1, y1 = bounds(sim.map)
    anchors = [p["anchor"] for p in sim.map["places"].values()]
    acts = []
    for f in sorted(rng.choice((0, rng.randrange(SUB))) for _ in range(rng.choice((0, 1, 1, 2, 3, 5)))):
        ax, ay = rng.choice(anchors)
        to = rng.choice(([rng.randint(x0, x1), rng.randint(y0, y1)], [ax + rng.randint(-3, 3), ay + rng.randint(-3, 3)]))
        act = walk(*to) if on_map(sim.map, to) else walk(ax, ay)
        if rng.random() < 0.3:
            act = small_act(rng, sim, agents)
        acts.append(at(act, f))
    return acts


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_taking_actions_as_they_arrive_writes_the_log_a_batch_run_writes_and_replays(seed):
    agents, rng = 20, random.Random(seed)
    early, batch = Sim(seed, agents, StubMind()), Sim(seed, agents, StubMind())
    for sim in (early, batch):
        sim.run_until(TICKS_PER_DAY // 2)
    b = next(a["id"] for a in early.state["agents"] if not a["asleep"])
    check = Checker(early)
    for i in range(TICKS_PER_DAY):
        acts = [{"kind": "possess", "agent": b}] if i == 0 else random_tick(rng, early, agents)
        if i == 100:
            acts.append({"kind": "release"})
        elif 100 < i < 104:
            acts = []
        elif i == 104:
            acts = [{"kind": "possess", "agent": b}, {"kind": "fire", "family": 0}]
        check.before(acts)
        for act in acts:
            n, tick = len(early.events), early.state["tick"]
            eid = early.take(act)
            assert eid >= n and early.events[eid].tick == tick and early.events[eid].origin == "player"
            if act.get("action") == "walk":  # the body has moved, or the engine has said why not, before the step
                assert early.events[eid + 1].kind in ("moved", "receipt") and early.events[eid + 1].parents == [eid]
        early.step(acts)
        batch.step(acts)
        bad = check.after()
        assert not bad, bad
        assert early.state_hash() == batch.state_hash(), early.state["tick"]
    early.finish(), batch.finish()
    assert [e.to_json() for e in early.events] == [e.to_json() for e in batch.events]
    assert sum(e.kind == "moved" for e in early.events) > 40
    assert sum(e.kind == "act" and "f" in e.data for e in early.events) > 40
    # time-1b (B2.7): small acts at random t are in it, more than one landing in a tick, and some finding the body held
    acts_of = lambda e: early.events[e.parents[0]] if e.kind == "receipt" and e.parents and early.events[e.parents[0]].kind == "act" else None
    landed = [acts_of(e).tick for e in early.events if acts_of(e) and e.data["ok"] and act_cost(e.data["action"])]
    assert len(landed) > 15 and len(landed) > len(set(landed)), (len(landed), len(set(landed)))
    assert any(acts_of(e) and e.data.get("why") == "busy" for e in early.events)
    events = [Event(**json.loads(e.to_json())) for e in early.events]
    assert replay(events).state_hash() == early.state_hash()


def test_the_step_takes_only_a_list_that_begins_with_what_was_taken():
    sim, b = awake_body()
    sim.take(walk(0, 0))
    with pytest.raises(ValueError, match="must begin with the ones already taken"):
        sim.step([walk(0, 1)])
    with pytest.raises(ValueError, match="were taken and are missing"):
        sim.step([])
    with pytest.raises(ValueError, match="a late act joins a running tick"):
        sim.take({**walk(0, 2), "late": True})
    sim.step([walk(0, 0), walk(0, 1)])  # the taken one is skipped, the other applied
    assert [m.data["pos"] for m in moved(sim, b)[-2:]] == [[0, 0], [0, 1]]
    sim.step([])  # the next tick starts with nothing taken
    assert not state_violations(sim)


def test_a_log_that_ends_on_actions_taken_before_their_tick_replays():
    sim, b = awake_body()
    sim.step([at(walk(3, 9), 100)])
    sim.take(at(walk(0, 8), 50))
    sim.take(at({"kind": "act", "action": "rest"}, 80))
    end = sim.finish()
    assert sim.events[end].tick == sim.events[end - 1].tick == sim.state["tick"]
    events = [Event(**json.loads(e.to_json())) for e in sim.events]
    assert replay(events).state_hash() == sim.state_hash()


# ------------------------------------------------------------------ the live runner
def runner_with_body(out, seed=6, agents=12):
    """A live world at midday with an inhabitant on client "c1". Returns (runner, body id)."""
    r = live.LiveRunner(out, StubMind(), seed, agents)
    while r.sim.state["tick"] < TICKS_PER_DAY // 2:
        r.step()
    b = next(a["id"] for a in r.sim.state["agents"] if not a["asleep"] and a["place"] is not None
             and a["sleep_tick"] > r.sim.state["tick"] + 40)
    assert r.inhabit(b, "c1") is None
    r.step()
    return r, b


def send(r, iid, args, f=0):
    return r.submit(intent(iid, "act", args, r.sim.state["tick"]), client="c1", scope="inhabitant", f=f)


def test_the_runner_stamps_each_act_and_never_earlier_than_the_one_before(tmp_path):
    r, b = runner_with_body(str(tmp_path / "w"))
    t = r.sim.state["tick"]
    for iid, args, f in (("w0", {"action": "walk", "to": [0, 0]}, 0), ("w1", {"action": "walk", "to": [0, 1]}, 200),
                         ("a1", {"action": "rest"}, 100),  # the pace changed between the two: it doesn't go back
                         ("w2", {"action": "walk", "to": [0, 2]}, 10 ** 6), ("w3", {"action": "walk", "to": [0, 3]}, -5)):
        assert send(r, iid, args, f) is None
    assert [a.get("f") for a in r.run.player[t]] == [None, 200, 200, SUB - 1, SUB - 1]
    assert r.submit(intent("x", "act", {"action": "walk", "to": [0, 4], "f": 3}, t), "c1", "inhabitant")["reason"] == "bad_args"
    r.step()
    assert [e.data.get("f") for e in r.sim.events if e.tick == t and e.kind == "act"] == [None, 200, 200, SUB - 1, SUB - 1]
    assert send(r, "w4", {"action": "walk", "to": [0, 0]}, 7) is None  # a new tick: f starts again
    assert r.run.player[t + 1][-1]["f"] == 7
    side = [json.loads(l) for l in open(tmp_path / "w" / "runner.jsonl")]
    assert next(x for x in side if x["kind"] == "received" and x["id"] == "w1")["act"]["f"] == 200  # durable with it


def test_a_walk_is_applied_and_answered_as_it_arrives_with_the_bodys_new_view(tmp_path):
    out = str(tmp_path / "w")
    r, b = runner_with_body(out)
    a, t = r.sim.state["agents"][b], r.sim.state["tick"]
    place = a["place"]
    frm, outside = edge(r.sim, place, r.sim.tile(a))
    assert send(r, "w0", {"action": "walk", "to": frm}) is None
    r.step()
    assert r.sim.tile(a) == frm and t + 1 == r.sim.state["tick"]
    t += 1
    other = next(o["id"] for o in r.sim.state["agents"] if o["id"] != b)
    assert send(r, "w1", {"action": "walk", "to": outside}, 120) is None
    assert send(r, "a1", {"action": "say", "target": other, "text": "Good day."}, 130) is None
    n = len(r.sim.events)
    (iid, ack, evs, view), = r.take_due()
    assert iid == "w1" and ack["status"] == "applied" and ack["tick"] == t and ack["event"] == n
    assert [e["kind"] for e in evs] == ["act", "moved"] and evs[1]["data"]["pos"] == outside
    assert r.sim.tile(a) == outside and a["place"] is None  # before the tick has run
    assert view == inhabitant.view(r.sim, b) and view["pos"] == outside and view["place"] is None and view["present"] == []
    assert [e.kind for e in r.sim.events[n:]] == ["act", "moved", "act"]  # the line is recorded, not yet resolved
    assert r.take_due() == []
    assert send(r, "w2", {"action": "walk", "to": frm}, 300) is None
    (iid2, ack2, evs2, view2), = r.take_due()
    assert iid2 == "w2" and view2["place"] == place and evs2[1]["data"]["place"] == place
    tick, events, acks = r.step()
    assert tick == t and [a["id"] for a in acks] == ["a1"]  # the walks were answered when they were taken
    assert any(e.kind == "receipt" and e.parents[0] == n + 2 for e in events)  # the line, in its tick
    assert [e.id for e in events[:3]] == [n, n + 1, n + 2] and events[-1].id == len(r.sim.events) - 1
    side = [json.loads(l) for l in open(os.path.join(out, "runner.jsonl"))]
    assert [x["id"] for x in side if x["kind"] == "applied" and x.get("early")][-2:] == ["w1", "w2"]
    r.finish()
    log = read_jsonl(os.path.join(out, "events.jsonl"))
    assert [e.id for e in log] == list(range(len(log)))  # each event on disk once, in order
    assert batch_of(out) == [e.to_json() for e in log]


def live_session(out, take: bool, seed=5):
    r, b = runner_with_body(out)
    rng = random.Random(seed)
    x0, y0, x1, y1 = bounds(r.sim.map)
    home = r.sim.map["places"][r.sim.state["agents"][b]["home"]]["anchor"]
    early = 0
    for i in range(90):
        fs = sorted(rng.randrange(SUB) for _ in range(rng.choice((1, 1, 2, 4))))
        for k, f in enumerate(fs):
            to = rng.choice(([rng.randint(x0, x1), rng.randint(y0, y1)], [home[0] + rng.randint(-4, 4), home[1] + rng.randint(-4, 4)]))
            assert send(r, f"w{i}.{k}", {"action": "walk", "to": to if on_map(r.sim.map, to) else home}, f) is None
            if i % 7 == 0 and k == 0:
                assert send(r, f"a{i}", {"action": "rest"}, f) is None
            elif i % 3 == 0:  # time-1b: any small act, at this walk's moment (the runner lands it when the body is free)
                args = {x: v for x, v in small_act(rng, r.sim, 12).items() if x != "kind"}
                ack = send(r, f"a{i}.{k}", args, f)
                assert ack is None or ack["status"] == "rejected", ack
            if take:
                took = r.take_due()
                early += len(took)
                for iid, ack, evs, view in took:
                    assert ack["status"] == "applied" and r.sim.events[ack["event"]].kind == "act"
                    assert view["pos"] == r.sim.tile(r.sim.state["agents"][b])
        _, _, acks = r.step()
        assert all(a["status"] == "applied" for a in acks) and (not take or all(a["id"].startswith("a") for a in acks))
    r.leave("c1")
    if take:
        assert r.take_due() == [] and r.sim.events[-1].kind == "release"  # released at once, not at the next tick
    r.step()
    r.finish()
    return r, early


def test_a_live_session_of_walks_taken_as_they_arrive_equals_batch_and_the_untaken_one_and_replays(tmp_path):
    r, early = live_session(str(tmp_path / "a"), True)
    events = read_jsonl(str(tmp_path / "a" / "events.jsonl"))
    assert early > 150 and sum(e.kind == "moved" for e in events) > 150
    assert replay(events).state_hash() == r.sim.state_hash()
    assert batch_of(str(tmp_path / "a")) == [e.to_json() for e in events]
    live_session(str(tmp_path / "b"), False)
    assert open(tmp_path / "a" / "events.jsonl").read() == open(tmp_path / "b" / "events.jsonl").read()


def test_walks_answered_before_a_crash_rerun_to_the_same_events(tmp_path):
    out = str(tmp_path / "w")
    r, b = runner_with_body(out)
    t = r.sim.state["tick"]
    assert send(r, "w1", {"action": "walk", "to": [0, 8]}, 100) is None
    assert send(r, "w2", {"action": "walk", "to": [3, 9]}, 400) is None
    took = r.take_due()
    acked = {iid: ack["event"] for iid, ack, _, _ in took}
    assert len(acked) == 2
    was = [e.to_json() for e in r.sim.events if e.tick == t]
    # The process dies here: the walks were answered, and the log on disk stops at the tick before.
    assert all(e.tick < t for e in read_jsonl(os.path.join(out, "events.jsonl")))
    r2 = live.LiveRunner(out, StubMind())  # a crash reopen runs the last recorded tick again, then this one
    assert r2.sim.state["tick"] == t - 1 and not [e for e in r2.sim.events if e.tick >= t - 1]
    assert r2.submit(intent("w1", "act", {"action": "walk", "to": [0, 8]}, t), "c1", "inhabitant")["event"] == acked["w1"]
    assert r2.inhabit(b, "c1") is None  # its body is still held: the inhabitant comes back for it
    assert r2.take_due() == []
    r2.step()
    assert r2.sim.state["tick"] == t and not [e for e in r2.sim.events if e.tick == t]
    assert send(r2, "w3", {"action": "walk", "to": [0, 8]}, 500) is None
    (iid, ack, _, _), = r2.take_due()  # the two before it are taken again first, and aren't answered twice
    assert iid == "w3" and [e.to_json() for e in r2.sim.events if e.tick == t][:len(was)] == was
    r2.step()
    assert {i: r2.intents[i]["event"] for i in acked} == acked
    r2.finish()
    assert batch_of(out) == [e.to_json() for e in read_jsonl(os.path.join(out, "events.jsonl"))]


def test_closing_between_ticks_with_walks_taken_reopens_to_the_same_world(tmp_path):
    out = str(tmp_path / "w")
    r, b = runner_with_body(out)
    assert send(r, "w1", {"action": "walk", "to": [0, 8]}, 100) is None
    r.take_due()
    t, n = r.sim.state["tick"], len(os.listdir(os.path.join(out, "snapshots")))
    r.close()
    assert len(os.listdir(os.path.join(out, "snapshots"))) == n  # no snapshot over what the log doesn't hold yet
    r2 = live.LiveRunner(out, StubMind())  # so it reopens as after a crash: the last recorded tick runs again
    assert r2.sim.state["tick"] == t - 1
    r2.step()
    r2.step()
    assert any(e.kind == "moved" and e.tick == t for e in r2.sim.events)
    r2.finish()
    events = read_jsonl(os.path.join(out, "events.jsonl"))
    assert batch_of(out) == [e.to_json() for e in events]
    assert replay(events).state_hash() == r2.sim.state_hash()


def test_a_finished_world_that_ended_on_taken_walks_is_extended_from_run_json(tmp_path):
    out = str(tmp_path / "w")
    r, b = runner_with_body(out)
    assert send(r, "w1", {"action": "walk", "to": [0, 8]}, 100) is None
    r.take_due()
    t = r.sim.state["tick"]
    r.finish()
    events = read_jsonl(os.path.join(out, "events.jsonl"))
    assert [e.kind for e in events[-3:]] == ["act", "moved", "end"] and events[-1].tick == t
    assert replay(events).state_hash() == r.sim.state_hash()
    r2 = live.LiveRunner(out, StubMind())  # the end and the tick it cut short come off; run.json still holds the walk
    assert r2.sim.state["tick"] == t and r2.sim.events[-1].tick == t - 1
    r2.step()
    r2.finish()
    longer = read_jsonl(os.path.join(out, "events.jsonl"))
    assert [e.to_json() for e in longer[:len(events) - 1]] == [e.to_json() for e in events[:-1]]
    assert batch_of(out) == [e.to_json() for e in longer]


# ------------------------------------------------------------------ time-1b: acts land at their own time (B2.3)
def receipt_of(sim, eid):
    return next(e for e in sim.events[eid + 1:] if e.kind == "receipt" and e.parents[0] == eid)


def test_an_act_lands_the_moment_it_is_recorded_with_its_receipt_before_the_tick_runs():
    sim, b = awake_body()
    a, t = sim.state["agents"][b], sim.state["tick"]
    eid = sim.take(at({"kind": "act", "action": "rest"}, 100))
    r = sim.events[eid + 1]
    assert (r.kind, r.parents, r.data["ok"], r.data["why"], r.tick) == ("receipt", [eid], True, "started", t)
    assert a["override"]["do"] == "rest" and a["override"]["event"] == eid  # the step is set before the tick runs
    assert sim.state["player"]["busy"] == t * SUB + 100 + COST
    sim.step([at({"kind": "act", "action": "rest"}, 100)])
    assert a["activity"] == "rest" and sim.state["tick"] == t + 1  # and it runs in that very tick
    assert not state_violations(sim)


def test_the_body_is_held_for_the_acts_cost_and_a_second_act_inside_it_is_busy():
    sim, b = awake_body()
    whys = []
    for action, f in (("rest", 100), ("work", 100), ("work", 100 + COST - 1), ("work", 100 + COST),
                      ("rest", 100 + COST + 1), ("decline", 100 + COST + 1)):
        eid = sim.take(at({"kind": "act", "action": action, **({"target": 0} if action == "decline" else {})}, f))
        whys.append(receipt_of(sim, eid).data["why"])
    # held from 100 for a sim-minute; free again on the minute; an answer is never held (there was no ask)
    assert whys == ["started", "busy", "busy", "started", "busy", "no ask"]
    assert act_cost("say") == act_cost("agree") == act_cost("walk") == 0 and act_cost("buy_food") == COST == SUB // 10
    n = len(sim.events)
    sim.take(at(walk(0, 8), 100 + COST + 2))  # a walk isn't held either: walking speed bounds it
    assert sim.events[n + 1].kind == "moved"


def test_two_small_acts_in_one_tick_both_land_when_they_are_a_sim_minute_apart():
    """B2.7: what was "one act per tick". Ten sim-minutes a tick, so up to ten acts, each in its own minute."""
    sim, b = awake_body()
    acts = [at({"kind": "act", "action": "rest" if k % 2 else "wait"}, k * COST) for k in range(SUB // COST)]
    n = len(sim.events)
    sim.step(acts)
    mine = [e for e in sim.events[n:] if e.kind == "receipt" and e.actor == b]
    assert [r.data["ok"] for r in mine] == [True] * 10 and not any(r.data["why"] == "busy" for r in mine)
    sim.step([{"kind": "act", "action": "rest"}, {"kind": "act", "action": "work"}])  # the same instant: one lands
    assert [e.data["why"] for e in sim.events if e.kind == "receipt" and e.actor == b][-2:] == ["started", "busy"]
    assert not state_violations(sim)


def test_a_sleeper_and_a_body_at_bedtime_are_told_so_at_once():
    sim = Sim(4, 14, StubMind())
    sim.step([{"kind": "possess", "agent": 3}])
    a = sim.state["agents"][3]
    assert a["asleep"]
    eid = sim.take({"kind": "act", "action": "work"})
    assert receipt_of(sim, eid).data["why"] == "asleep" and "busy" not in sim.state["player"]  # no act, no hold
    sim.step([{"kind": "act", "action": "work"}])
    while a["asleep"]:
        sim.step([])
    a["sleep_tick"] = sim.state["tick"]
    eid = sim.take({"kind": "act", "action": "work"})
    assert receipt_of(sim, eid).data["why"] == ("falling asleep" if a["place"] == a["home"] else "bedtime")


def test_a_line_said_still_waits_for_its_tick_one_line_a_tick():
    """The fallback time-1b names (B2.6): the talk path's timing is pinned from the tick a line lands in."""
    sim, b = awake_body()
    a = sim.state["agents"][b]
    other = next(o["id"] for o in sim.state["agents"] if o["id"] != b and o["place"] == a["place"] and not o["asleep"])
    say = lambda text, f: at({"kind": "act", "action": "say", "target": other, "text": text}, f)
    acts = [at({"kind": "act", "action": "wait"}, 10), say("Good day.", 20), say("And again.", 30)]
    n = len(sim.events)
    ids = [sim.take(x) for x in acts]
    assert [e.kind for e in sim.events[n:]] == ["act", "receipt", "act", "act"]  # the wait landed; the lines wait
    sim.step(acts)
    said = [e for e in sim.events[n:] if e.kind == "said"]
    assert len(said) == 1 and said[0].parents[0] == ids[1] and said[0].id > ids[2]  # at the tick's fixed point
    assert receipt_of(sim, ids[1]).data["ok"] and receipt_of(sim, ids[2]).data["why"] == "one act per tick"
    assert not state_violations(sim)


def test_an_act_is_answered_at_once_with_its_receipt_and_a_line_with_its_tick(tmp_path):
    out = str(tmp_path / "w")
    r, b = runner_with_body(out)
    t = r.sim.state["tick"]
    other = next(o["id"] for o in r.sim.state["agents"] if o["id"] != b)
    assert send(r, "a1", {"action": "rest"}, 50) is None
    assert send(r, "s1", {"action": "say", "target": other, "text": "Good day."}, 60) is None
    assert send(r, "a2", {"action": "wait"}, 70) is None  # sent while the body is held: stamped when it is free
    took = r.take_due()
    assert [x[0] for x in took] == ["a1", "a2"]
    for iid, ack, evs, view in took:
        assert ack["status"] == "applied" and [e["kind"] for e in evs] == ["act", "receipt"] and evs[1]["data"]["ok"]
        assert view["tick"] == t and view == inhabitant.view(r.sim, b) or iid == "a1"
    assert [a.get("f") for a in r.run.player[t]] == [50, 60, 50 + COST]
    tick, events, acks = r.step()
    assert [a["id"] for a in acks] == ["s1"]  # the line is acked with its tick
    r.finish()
    log = read_jsonl(os.path.join(out, "events.jsonl"))
    assert batch_of(out) == [e.to_json() for e in log] and replay(log).state_hash() == r.sim.state_hash()


def test_an_act_sent_while_a_tick_runs_and_the_body_is_held_waits_and_lands_when_it_is_free(tmp_path):
    out = str(tmp_path / "w")
    r, b = runner_with_body(out)
    t, got = r.sim.state["tick"], []
    for iid, action in (("k1", "rest"), ("k2", "wait")):
        r.queue_late(intent(iid, "act", {"action": action}, t), "c1", "inhabitant",
                     lambda ack, evs, view, iid=iid: got.append((iid, ack, evs)), 12)
    r.step()
    (i1, ack1, evs1), (i2, ack2, _) = got
    assert i1 == "k1" and ack1["tick"] == t and evs1[1]["data"]["ok"]  # joined the running tick, at its last second
    assert r.sim.events[ack1["event"]].data == {"action": "rest", "f": SUB - 1, "late": True}
    assert i2 == "k2" and ack2 is None  # the body was held: it waits for the next tick
    assert r.run.player[t + 1] == [{"kind": "act", "action": "wait", "f": COST - 1}]  # when the body is free
    (iid, ack, evs, _), = r.take_due()
    assert iid == "k2" and ack["tick"] == t + 1 and evs[1]["data"] == {"action": "wait", "ok": True, "why": "waited"}
    r.step()
    r.finish()
    assert batch_of(out) == [e.to_json() for e in read_jsonl(os.path.join(out, "events.jsonl"))]


def test_with_no_pull_at_all_an_act_is_still_answered_at_once(tmp_path):
    """Before time-1b an act waited for the next tick: 50 s at this pace with the pull off (tests/test_pull.py)."""
    out = str(tmp_path / "w")
    runner, b = runner_with_body(out)
    runner.leave("c1")
    runner.step()
    got = []

    async def main():
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.ensure_future(live.serve(runner, port=0, tps=0.02, paused=True, pull_min=None,
                                                started=lambda port, hub: ready.set_result(port)))
        port = await ready
        c = await WS.connect(port)
        c.send({"type": "hello", "protocol": 3, "scope": "inhabitant", "agent": b})
        await c.until("welcome")
        god = await WS.connect(port)
        god.send({"type": "hello", "protocol": 3})
        await god.until("welcome")
        god.send({"type": "control", "op": "run"})
        seen = (await c.until("tick"))["view"]["tick"]
        for k, action in enumerate(("rest", "wait", "work")):  # back to back: each lands when the body is free
            c.send(intent(f"a{k}", "act", {"action": action}, seen))
        for k in range(3):
            got.append((await asyncio.wait_for(c.recv(), 3.0), await asyncio.wait_for(c.recv(), 3.0)))
        assert runner.sim.state["tick"] == seen
        god.send({"type": "control", "op": "pause"})
        await god.until("status")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    for k, (early, ack) in enumerate(got):
        assert early["type"] == "early" and [e["kind"] for e in early["events"]] == ["act", "receipt"], early
        assert early["events"][1]["data"]["ok"] and "view" in early and ack["id"] == f"a{k}" and ack["status"] == "applied"
    fs = [e["events"][0]["data"].get("f", 0) for e, _ in got]
    assert fs[1] == fs[0] + COST and fs[2] == fs[1] + COST
    runner.step()
    runner.finish()
    events = read_jsonl(os.path.join(out, "events.jsonl"))
    assert batch_of(out) == [e.to_json() for e in events] and replay(events).state_hash() == runner.sim.state_hash()


# ------------------------------------------------------------------ the hub: the clock's stamp, no pull for a walk
def test_review_note_1_a_walk_is_judged_by_where_it_lands_and_pulls_no_tick_either_way(tmp_path):
    """Review on #110: pulls() judged a walk by the place of its `to`, not by where the capped step landed.
    Now no walk pulls: it is applied at once, and its answer says where it landed."""
    r, b = runner_with_body(str(tmp_path / "w"), agents=120)
    a = r.sim.state["agents"][b]
    frm = r.sim.tile(a)
    far = far_corner(r.sim, frm)
    assert place_at(r.sim.map, far) != a["place"] and distance(frm, far) > 2 * WALK
    w = lambda i, to: intent(i, "act", {"action": "walk", "to": to}, r.sim.state["tick"])
    assert not r.pulls(w("w1", far)) and not r.pulls(w("w0", frm)) and r.pulls(intent("a", "act", {"action": "rest"}, 0))
    assert r.submit(w("w1", far), "c1", "inhabitant") is None
    (_, ack, evs, view), = r.take_due()
    assert evs[1]["data"]["pos"] == view["pos"] == r.sim.tile(a) != far  # cut short of `to`: the answer says where
    assert view["place"] == a["place"] == place_at(r.sim.map, view["pos"])


def test_review_note_2_a_wiggle_across_a_places_edge_pulls_no_tick_and_is_answered_at_once(tmp_path):
    """Review on #110: each step across a place's edge pulled a tick, so a wiggle sped the world up. Now the
    steps are answered between ticks, each with its `moved` event and the view from where the body stands, and
    the world keeps its pace."""
    out = str(tmp_path / "w")
    runner, b = runner_with_body(out)
    runner.leave("c1")
    runner.step()
    a = runner.sim.state["agents"][b]
    got, spot = [], {}

    async def main():
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.ensure_future(live.serve(runner, port=0, tps=0.02, paused=True, pull_min=0.05,
                                                started=lambda port, hub: ready.set_result((port, hub))))
        port, hub = await ready
        c = await WS.connect(port)
        c.send({"type": "hello", "protocol": 3, "scope": "inhabitant", "agent": b})
        await c.until("welcome")
        god = await WS.connect(port)
        god.send({"type": "hello", "protocol": 3})
        await god.until("welcome")
        god.send({"type": "control", "op": "run"})
        v = (await c.until("tick"))["view"]  # leaving pause steps once; the pace (50 s a tick) starts here
        seen = v["tick"]
        inside, outside = edge(runner.sim, v["place"], v["pos"])
        spot.update(place=v["place"], inside=inside, outside=outside)
        c.send(intent("w", "act", {"action": "walk", "to": inside}, seen))
        assert (await c.until("early"))["view"]["pos"] == inside and (await c.recv())["id"] == "w"
        for k in range(8):
            c.send(intent(f"w{k}", "act", {"action": "walk", "to": outside if k % 2 == 0 else inside}, seen))
            early = await asyncio.wait_for(c.recv(), 3.0)
            ack = await asyncio.wait_for(c.recv(), 3.0)
            got.append((early, ack, hub.stamp()))
            await asyncio.sleep(0.12)  # past the pull floor: a pulled tick would run here
        assert runner.sim.state["tick"] == seen and not hub.pull
        god.send({"type": "control", "op": "pause"})
        await god.until("status")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    assert len(got) == 8
    for k, (early, ack, _) in enumerate(got):
        assert early["type"] == "early" and [e["kind"] for e in early["events"]] == ["act", "moved"], early
        assert early["view"]["place"] == (None if k % 2 == 0 else spot["place"])
        assert early["view"]["pos"] == early["events"][1]["data"]["pos"] == spot["outside" if k % 2 == 0 else "inside"]
        assert ack["type"] == "ack" and ack["id"] == f"w{k}" and ack["status"] == "applied"
    side = [json.loads(l) for l in open(os.path.join(out, "runner.jsonl"))]
    assert not [x for x in side if x["kind"] == "pulled"]
    fs = [x["act"].get("f", 0) for x in side if x["kind"] == "received" and x["id"][:1] == "w" and x["id"] != "w"]
    assert fs == sorted(fs) and 0 < fs[-1] < SUB and fs[-1] > fs[0]  # the clock's stamp: later walks, later f
    assert got[-1][2] >= fs[-1]
    runner.step()  # the tick they were taken ahead of
    runner.finish()
    events = read_jsonl(os.path.join(out, "events.jsonl"))
    assert [e.data.get("f", 0) for e in events if e.kind == "act" and e.data.get("action") == "walk"][-8:] == fs
    assert batch_of(out) == [e.to_json() for e in events]
    assert replay(events).state_hash() == runner.sim.state_hash()


def test_a_paused_world_stamps_zero_and_a_walk_sent_while_a_tick_runs_joins_it_at_its_last_second(tmp_path):
    out = str(tmp_path / "w")
    runner, b = runner_with_body(out)
    runner.leave("c1")
    runner.step()
    a = runner.sim.state["agents"][b]
    hub = live.Hub(runner, tps=0.02, paused=True)
    assert hub.stamp() == 0
    assert runner.inhabit(b, "c1") is None
    runner.step()
    t, got = runner.sim.state["tick"], []
    runner.queue_late(intent("w1", "act", {"action": "walk", "to": [0, 8]}, t), "c1", "inhabitant",
                      lambda ack, evs, view: got.append((ack, evs, view)), 33)
    runner.step()
    (ack, evs, view), = got
    act = runner.sim.events[ack["event"]]
    assert act.tick == t and act.data == {"action": "walk", "to": [0, 8], "f": SUB - 1, "late": True}
    assert view["pos"] == runner.sim.tile(a) == evs[1]["data"]["pos"]
    runner.finish()
    assert batch_of(out) == [e.to_json() for e in read_jsonl(os.path.join(out, "events.jsonl"))]
