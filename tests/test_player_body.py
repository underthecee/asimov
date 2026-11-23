"""The player's body (plan/player-body-spec.md, engine 16): possess, act, release.

A player act is a deliberation whose output comes from a person: recorded as a player event at the start
of the tick, resolved in _commit in _order with the minds' replies, held to the offer a mind's reply
schema would have held it to, and never sent to the mind."""
import hashlib
import json
import os
import random

import pytest

from asiimov import live
from asiimov.events import Event, read_jsonl
from asiimov.invariants import Checker
from asiimov.minds import MindReply, NoisyMind, StubMind
from asiimov.replay import replay
from asiimov.sim import DELIBERATE_ACTIONS, IDLE_TICKS, INSTANT_STEPS, PERSON_STEPS, RATED_ACTS, TICKS_PER_DAY, Sim

GOLDEN_15 = "1a1677ccd7eec0952ccfa9e52dca9ad9d8e1511391a8eddf7525a48158636f9a"  # tests/test_world.py, engine 15


def log_lines(sim):
    return [e.to_json() for e in sim.events]


def test_a_world_with_no_player_is_engine_15_but_for_the_version_field():
    """Bar 1: the same fixed world as test_world's golden, with genesis' engine field set back to 15. From
    tithe-1 (B5) the world's stakes are on by default, so this world turns them off and drops their record."""
    from asiimov.params import generate_population
    from test_world import FIRE
    sim = Sim(3, 20, StubMind(), genesis={**generate_population(3, 20), "stakes": {}})
    sim.run(2, FIRE)
    sim.finish()
    lines = log_lines(sim)
    g = json.loads(lines[0])
    g["data"]["engine"] = "15"
    del g["data"]["population"]["stakes"]
    lines[0] = json.dumps(g, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256("".join(l + "\n" for l in lines).encode()).hexdigest() == GOLDEN_15


def body_script(aid, seed=0, start=TICKS_PER_DAY // 2, end=2 * TICKS_PER_DAY, n=12):
    """Possess `aid` at `start`, a spread of acts (some targeted, one pair in one tick), release at the end."""
    rng = random.Random(seed)
    player = {start: [{"kind": "possess", "agent": aid}]}
    for t in sorted(rng.sample(range(start + 1, end - 1), n)):
        action = rng.choice(DELIBERATE_ACTIONS)
        act = {"kind": "act", "action": action}
        if action in PERSON_STEPS:
            act["target"] = rng.randrange(12)
        player.setdefault(t, []).append(act)
    player.setdefault(start + 3, []).extend([{"kind": "act", "action": "work"}, {"kind": "act", "action": "rest"}])
    player.setdefault(end - 1, []).append({"kind": "release"})
    return player


def test_possess_act_release_replays_exactly_and_the_mind_deliberates_only_when_idle():
    aid = 3
    player = body_script(aid)
    sim = Sim(5, 12, NoisyMind())
    sim.run_until(2 * TICKS_PER_DAY + 20, player)
    sim.finish()
    events = [Event(**json.loads(l)) for l in log_lines(sim)]
    assert replay(events).state_hash() == sim.state_hash()
    pos = next(e.id for e in events if e.kind == "possess")
    rel = next(e.id for e in events if e.kind == "release")
    during = [e for e in events[pos:rel] if e.actor == aid]
    orders = sorted({e.tick for e in during if e.kind in ("possess", "act")} | {events[pos].tick})
    for e in during:  # bar 4, as joon's idle-body choice amends it: only after IDLE_TICKS without an order
        if e.kind == "mind_deliberate":
            assert e.tick - max(t for t in orders if t <= e.tick) >= IDLE_TICKS, e
    assert {"mind_plan", "mind_reflect"} <= {e.kind for e in during}                  # its own mind lives on
    acts = [e for e in during if e.kind == "act"]
    receipts = {e.parents[0]: e for e in events if e.kind == "receipt" and e.parents and e.parents[0] in
                {a.id for a in acts}}
    assert len(receipts) == len(acts)  # every act ends its tick with a receipt (S8), or started a step
    assert any(r.data["why"] == "busy" for r in receipts.values())  # time-1b: the pair in one tick; was "one act per tick"
    assert not any(e.kind == "mind_deliberate" and e.actor == aid for e in events[rel:rel + 1])
    assert sim.state.get("player") is None and "player" not in sim.state


def test_every_unoffered_act_is_refused_and_rolled_acts_keep_a_minds_cooldown():
    """Bar 3: a hostile player sends a random act every tick for 3 days. Invariants and ledgers hold every
    tick, nothing outside the offer goes through, and rolled acts per day never exceed a mind's 12."""
    rng = random.Random(11)
    sim = Sim(2, 16, StubMind())
    checker = Checker(sim)
    aid = 5
    for _ in range(3 * TICKS_PER_DAY):
        t = sim.state["tick"]
        acts = [{"kind": "possess", "agent": aid}] if t == 1 else []
        if t > 1:
            for _ in range(rng.choice([1, 1, 1, 2])):
                action = rng.choice(DELIBERATE_ACTIONS)
                act = {"kind": "act", "action": action}
                me = sim.state["agents"][aid]
                here = [o["id"] for o in sim.state["agents"] if o["place"] == me["place"] and o["id"] != aid]
                if here and rng.random() < 0.6:
                    act["target"] = rng.choice(here)  # someone it can reach, so rolls happen
                elif rng.random() < 0.8:
                    act["target"] = rng.randrange(-3, 22)
                acts.append(act)
        bad = checker.step(acts)
        assert not bad, bad[:5]
    sim.finish()
    ev = sim.events
    act_ids = {e.id: e for e in ev if e.kind == "act"}
    went, rolled = 0, {}
    refusals = {"cooldown", "not offered", "unknown target", "one act per tick", "busy", "asleep", "bedtime", "falling asleep"}
    for e in ev:
        if e.kind != "receipt" or not e.parents or e.parents[0] not in act_ids:
            continue
        a = act_ids[e.parents[0]]
        if e.data["why"] == "not offered":
            assert a.data["action"] not in e.data["offer"]
        if e.data["ok"]:
            went += 1
        if a.data["action"] in RATED_ACTS and e.data["why"] not in refusals:  # reached the rules
            rolled[e.tick // TICKS_PER_DAY] = rolled.get(e.tick // TICKS_PER_DAY, 0) + 1
    whys = {e.data["why"] for e in ev if e.kind == "receipt" and e.parents and e.parents[0] in act_ids}
    assert {"not offered", "busy", "asleep", "cooldown"} <= whys, whys  # time-1b: "busy" is what "one act per tick" was
    assert went > 20 and max(rolled.values()) <= TICKS_PER_DAY // 12
    assert replay([Event(**json.loads(e.to_json())) for e in ev]).state_hash() == sim.state_hash()


class Forced(StubMind):
    """Stub everywhere, except one deliberation at (agent, tick), which answers `out`."""

    def __init__(self, aid, tick, out):
        self.aid, self.tick, self.out = aid, tick, out

    def decide(self, req):
        if (req.agent_id, req.tick, req.kind) == (self.aid, self.tick, "deliberate"):
            return MindReply(json.dumps(self.out), self.model, {"temperature": 0}, "stop")
        return super().decide(req)


def test_an_offered_act_gets_the_receipts_a_minds_identical_deliberation_gets():
    """Twin A: the player possesses the body at tick T and acts. Twin B: no player; the body's mind is
    made to deliberate at T and gives the same answer. Same receipts, rolls and all. The act joins the tick
    at its fixed point (`late`), which is where a mind's answer applies; since time-1b an act that arrives
    between ticks lands at its own time instead, by the same rules on who is there then (tests/test_time.py)."""
    rng, compared, rolls = random.Random(3), 0, 0
    for trial in range(150):
        seed, aid = rng.randrange(50), rng.randrange(14)
        T = rng.randrange(TICKS_PER_DAY // 2, TICKS_PER_DAY + 60)
        probe = Sim(seed, 14, StubMind())
        probe.run_until(T)
        me = probe.state["agents"][aid]
        here = [o["id"] for o in probe.state["agents"] if o["place"] == me["place"] and o["id"] != aid]
        if here and rng.random() < 0.6:  # a rolled act on someone present, so contested rolls get compared
            action, target = rng.choice(INSTANT_STEPS), rng.choice(here)
        else:
            action = rng.choice([x for x in DELIBERATE_ACTIONS if x != "wait"])
            target = rng.randrange(14) if action in PERSON_STEPS else None
        out = {"action": action, "target": target} if target is not None else {"action": action}
        twins = []
        joined = []
        for mind, acts in ((StubMind(), [{"kind": "possess", "agent": aid}]), (Forced(aid, T, out), [])):
            w = Sim(seed, 14, mind)
            w.run_until(T)
            a = w.state["agents"][aid]  # the same occasion and no cooldown in both, so both may act now
            a["cooldown_until"], a["occasion"] = 0, {"why": "hungry", "who": None, "event": None, "tick": T, "salience": 900}
            a["inv"]["food"] = max(1, a["inv"]["food"])  # so the mind's occasion is this one, not its hunger
            w.step(acts, during=(lambda s, done: joined.append(s.late_act({"kind": "act", **out}))) if acts else None)
            twins.append(w)
        A, B = twins
        if joined == [None]:  # its own mind was being asked this tick (a plan on waking): the act waits a tick
            continue
        if any(e.kind in ("sleep", "wake") and e.actor == aid and e.tick == T for e in A.events) or \
                A.state["agents"][aid]["asleep"]:
            continue
        ra = [e.data for e in A.events if e.kind == "receipt" and e.actor == aid and e.tick == T]
        rb = [e.data for e in B.events if e.kind == "receipt" and e.actor == aid and e.tick == T]
        if not rb:  # the mind's deliberation didn't fire (daily call budget): nothing to compare
            continue
        withheld = next(e.data["withheld"] for e in B.events if e.kind == "mind_deliberate" and e.tick == T and e.actor == aid)
        if action in withheld:
            assert ra[0]["why"] == "not offered" and ra[0]["offer"] == [x for x in DELIBERATE_ACTIONS if x not in withheld]
            continue
        if action in PERSON_STEPS and any(r["action"] == action and r["why"] == "unknown target" for r in ra):
            continue  # the mind's schema would not have let it name this person; nothing to compare
        assert ra == rb, (seed, aid, T, out)
        compared += 1
        rolls += any("roll" in r for r in ra)
    assert compared >= 30 and rolls >= 5, (compared, rolls)


# ---------------------------------------------------------------------- through the live runner
def intent(iid, verb, args, seen):
    return {"type": "intent", "id": iid, "verb": verb, "args": args, "seen_tick": seen}


SCRIPT = [(20, "p", "possess", {"agent": 2}), (21, "a1", "act", {"action": "work"}),
          (40, "a2", "act", {"action": "ask_food", "target": 4}), (40, "a3", "act", {"action": "rest"}),
          (75, "a4", "act", {"action": "go_market"}), (90, "r", "release", {})]
END = 120


def batch(script=SCRIPT, end=END):
    player = {}
    for t, _, verb, args in script:
        player.setdefault(t, []).append({"kind": verb, **args})
    sim = Sim(4, 10, StubMind())
    sim.run_until(end, player)
    sim.finish()
    return log_lines(sim)


def drive(r, sent):
    while r.sim.state["tick"] < END:
        now = r.sim.state["tick"]
        for t, iid, verb, args in SCRIPT:
            if t == now and iid not in sent:
                sent.add(iid)
                assert r.submit(intent(iid, verb, args, now)) is None, iid
        r.step()


def test_live_body_equals_batch_and_survives_a_kill_anywhere(tmp_path):
    out = str(tmp_path / "w")
    r = live.LiveRunner(out, StubMind(), 4, 10)
    drive(r, set())
    r.finish()
    want = batch()
    assert [l.rstrip("\n") for l in open(os.path.join(out, "events.jsonl"))] == want
    assert replay(read_jsonl(os.path.join(out, "events.jsonl")))
    for trial, kill in enumerate([20, 21, 40, 41, 75, 90, 91]):  # kill right after each scheduling tick
        d = str(tmp_path / f"k{trial}")
        r = live.LiveRunner(d, StubMind(), 4, 10)
        sent = set()
        while r.sim.state["tick"] < kill:
            now = r.sim.state["tick"]
            for t, iid, verb, args in SCRIPT:
                if t == now:
                    sent.add(iid)
                    r.submit(intent(iid, verb, args, now))
            if now + 1 == kill and trial % 2:
                break  # killed with the tick's intents scheduled but not run
            r.step()
        r = live.LiveRunner(d, StubMind())  # no close(): a crash
        drive(r, sent)
        r.finish()
        assert [l.rstrip("\n") for l in open(os.path.join(d, "events.jsonl"))] == want, kill


def test_the_runner_refuses_body_verbs_that_would_break_the_tick(tmp_path):
    r = live.LiveRunner(str(tmp_path / "w"), StubMind(), 4, 10)
    no = lambda i, v, a: r.submit(intent(i, v, a, 0))["reason"]
    assert no("x1", "act", {"action": "work"}) == "no_body"
    assert no("x2", "release", {}) == "no_body"
    assert no("x3", "possess", {"agent": 10}) == "bad_args"
    assert no("x4", "possess", {"agent": True}) == "bad_args"
    assert no("x5", "act", {"action": "fly"}) == "bad_args"
    assert r.submit(intent("ok", "possess", {"agent": 1}, 0)) is None
    assert no("x6", "possess", {"agent": 2}) == "body_taken"
    assert no("x7", "act", {"action": "steal", "target": -1}) == "bad_args"
    assert r.submit(intent("ok2", "act", {"action": "steal", "target": 3}, 0)) is None
    assert r.submit(intent("ok3", "release", {}, 0)) is None
    assert no("x8", "act", {"action": "work"}) == "no_body"
    _, events, acks = r.step()
    assert [a["status"] for a in acks] == ["applied"] * 3
    assert [e.kind for e in events if e.origin == "player"] == ["possess", "act", "release"]


def test_scorecard_and_diagnostics_leave_out_a_possessed_villager():
    from asiimov.diagnostics import diagnose
    from asiimov.scorecard import extract
    sim = Sim(5, 12, StubMind())
    sim.run_until(2 * TICKS_PER_DAY, body_script(3))
    events = [Event(**json.loads(l)) for l in log_lines(sim)]
    assert diagnose(events)["possessed_dropped"] == [3]
    assert 3 not in extract(events)["periods"] and 4 in extract(events)["periods"]
    assert diagnose([Event(**json.loads(l)) for l in log_lines(Sim(5, 12, StubMind()))])["possessed_dropped"] == []


def test_an_act_before_a_release_in_its_tick_lands_first_and_never_reaches_the_freed_mind():
    """Review of #39: [act, release] in one tick resolved the act on a body the player no longer held, and
    its failed receipt filed an occasion the freed mind was then asked about. Possessing another body
    releases the first the same way. Since time-1b the act lands as it is recorded, while the player still
    holds the body, so it has its own receipt before the release; the freed mind is still never asked about
    it. A line said still waits for the tick, so one queued before a release is refused as "released"."""
    cases = 0
    agents = Sim(1, 25, StubMind()).state["agents"]
    # bodies a failed receipt's occasion would reach (salience at least the threshold), plus two it wouldn't
    reach = [a["id"] for a in agents
             if 300 + a["traits"]["conscientiousness"] // 3 >= a["cognition"]["deliberation_threshold"]]
    bodies = reach[:4] + [a["id"] for a in agents if a["id"] not in reach][:2]
    assert len(bodies) == 6
    for aid in bodies:
        for action in ("buy_food", "steal", "work", "say"):
            more = {"text": "Good day."} if action == "say" else {}
            player = {65: [{"kind": "possess", "agent": aid}], 66: [{"kind": "act", "action": "buy_food"}],
                      70: [{"kind": "act", "action": action, "target": (aid + 1) % 25, **more}, {"kind": "release"}],
                      80: [{"kind": "possess", "agent": aid}],
                      90: [{"kind": "act", "action": action, "target": (aid + 2) % 25, **more},
                           {"kind": "possess", "agent": (aid + 1) % 25}],
                      100: [{"kind": "release"}]}
            sim = Sim(1, 25, StubMind())
            for end, freed in ((71, aid), (91, aid), (120, None)):
                sim.run_until(end, player)
                if freed is not None:  # what the freed mind would be asked about next
                    occ = sim.state["agents"][freed]["occasion"]
                    assert occ is None or sim.events[occ["event"]].tick < end - 1, (aid, action, occ)
            sim.finish()
            acts = [e for e in sim.events if e.kind == "act" and e.tick in (70, 90)]
            gone = {e.id for e in acts}
            freed = {e.tick: e.id for e in sim.events if e.kind == "release" and e.tick in (70, 90)}
            for e in sim.events:
                if e.kind == "receipt" and e.parents and e.parents[0] in gone:
                    assert e.tick in freed and e.id < freed[e.tick], e  # settled before the body is let go
                    if action == "say":
                        assert e.data["why"] == "released" and e.id == e.parents[0] + 1, e
                    else:
                        assert e.data["why"] != "released", e
                    gone.add(e.id)
                    cases += 1
            assert cases and (action != "say" or all(
                not set(e.parents) & gone for e in sim.events
                if e.kind not in ("receipt", "release", "possess") and e.actor == aid)), (aid, action)
            assert len(freed) == 2 and replay(sim.events).state_hash() == sim.state_hash()
    assert cases == 2 * 6 * 4


def test_a_possessed_villagers_talks_still_count_for_its_partner():
    from asiimov.events import drop_possessed
    sim = Sim(5, 12, StubMind())
    sim.run_until(2 * TICKS_PER_DAY, body_script(3))
    kept, bodies = drop_possessed(sim.events)
    talks = [e for e in sim.events if e.kind == "talk" and 3 in (e.actor, e.data["with"])]
    assert bodies == [3] and talks and all(e in kept for e in talks)
    assert not [e for e in kept if e.actor == 3 and e.kind != "talk"]


def test_an_idle_body_hands_back_to_its_own_mind_until_the_next_order():
    """joon's idle-body choice: with no order (possess or act) for IDLE_TICKS, the possessed villager's own
    mind deliberates again; an act takes it back at once. Replays exactly."""
    aid, start = 3, TICKS_PER_DAY // 2
    sim = Sim(5, 12, NoisyMind())
    fam = sim.state["agents"][aid]["family"]  # a fire is an occasion (salience 900), and not an order
    player = {start: [{"kind": "possess", "agent": aid}], start + 2: [{"kind": "fire", "family": fam}],
              start + 40: [{"kind": "act", "action": "rest"}], start + 41: [{"kind": "act", "action": "socialize"}],
              start + 42: [{"kind": "fire", "family": fam}], start + 120: [{"kind": "release"}]}
    sim.run_until(start + 130, player)
    sim.finish()
    delib = [e.tick for e in sim.events if e.kind == "mind_deliberate" and e.actor == aid and start <= e.tick < start + 120]
    assert delib, "the idle body's mind never deliberated"
    # The fire's occasion waits for the hour to pass, then the mind is asked at once; after the two orders
    # the second fire's occasion waits again, the hour counted from the last order.
    assert delib[:2] == [start + IDLE_TICKS, start + 41 + IDLE_TICKS], delib
    assert replay(sim.events).state_hash() == sim.state_hash()


def test_the_hand_back_never_asks_the_mind_about_the_players_own_act():
    """Review of #42: a refused or failed player act files a "failed" occasion that outlives the idle hour,
    so the first idle deliberation was about the player's act. Like release, the hand-back drops it."""
    cases = 0
    for aid in range(25):
        sim = Sim(1, 25, StubMind())
        sim.run_until(120, {65: [{"kind": "possess", "agent": aid}], 66: [{"kind": "act", "action": "buy_food"}],
                            70: [{"kind": "act", "action": "buy_food"}, {"kind": "act", "action": "steal", "target": (aid + 1) % 25}]})
        mine = {e.id for e in sim.events if e.kind == "act"}
        mine |= {e.id for e in sim.events if e.kind == "receipt" and e.parents and e.parents[0] in mine}
        for e in sim.events:
            if e.kind == "mind_deliberate" and e.actor == aid and e.tick >= 70:
                assert not set(e.parents) & mine, (aid, e)
                cases += 1
    assert cases >= 5  # idle deliberations that could have cited the acts


def test_a_player_steps_late_failure_is_never_the_idle_minds_occasion_across_a_kill(tmp_path):
    """Review of #42 (Steering, and k42b): an act that starts a step (seek, give_food) keeps the act as the
    step's holder, and the step can fail ticks later, after the hand-back. Its receipt cites the act, so
    the occasion is the player's by ancestry. A kill and resume between the order and the failure keeps it."""
    from asiimov import persist
    late = cases = 0
    for aid in range(25):
        player = {65: [{"kind": "possess", "agent": aid}],
                  66: [{"kind": "act", "action": "seek", "target": (aid + 7) % 25}],
                  70: [{"kind": "act", "action": "give_food", "target": (aid + 1) % 25}]}
        out = str(tmp_path / f"w{aid}")
        run = persist.start(out, 1, 25, StubMind(), "id", player, snapshot_every=24)
        run.advance(71)  # after the orders, before the steps' late receipts and the hand-back
        run = persist.resume(out, StubMind())  # a crash: no finish
        run.advance(140)
        run.finish()
        ev = run.sim.events
        acts = {e.id for e in ev if e.kind == "act"}
        theirs = {e.id for e in ev if e.kind == "receipt" and e.parents and e.parents[0] in acts}
        late += sum(1 for e in ev if e.id in theirs and not e.data["ok"] and e.tick > ev[e.parents[0]].tick)
        for e in ev:
            if e.kind == "mind_deliberate" and e.actor == aid and e.tick >= 70:
                assert not set(e.parents) & (acts | theirs), (aid, e)
                cases += 1
        assert replay(ev).state_hash() == run.sim.state_hash()
    assert late and cases >= 5, (late, cases)  # late failures happened, and idle minds were asked
