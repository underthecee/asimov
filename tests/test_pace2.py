"""pace-2 (Steering 2026-11-20 09:06Z; results/s16b/aipt3/results.md): in a held world with a player in it, plan and
reflect calls stop blocking ticks. One filed at tick t is asked at once and lands (its event, then its effect) at t+2's
commit, which alone waits for it. Replay is exact (the event records `filed`; ReplayMind keys on it), a world with no
player or not held is untouched, and a villager with a call in flight still answers the player (Steering 09:06Z)."""
import json
import random

import pytest

from asiimov.events import Event
from asiimov.invariants import Checker
from asiimov.minds import StubMind
from asiimov.params import generate_population
from asiimov.replay import replay
from asiimov.sim import DEFAULT_STAKES, TICKS_PER_DAY, Sim

PRIVATE = {"consent": "private", "typed": 1}


def held_world(seed, n=40, talk=PRIVATE, mind=None, workers=1):
    pop = generate_population(seed, n)
    pop["stakes"] = {**json.loads(json.dumps(DEFAULT_STAKES)), "talk": dict(talk)}
    return Sim(seed, n, mind or StubMind(), genesis=pop, workers=workers)


def talker(seed, ticks, every=1.0, talk=PRIVATE, mind=None, workers=1, start=None, snap_at=()):
    """A player who says a line to someone here at most ticks, from the hour before dawn on. `snap_at`: ticks where
    the world is snapshotted and continued from the restored copy (a reopen with calls in flight)."""
    sim = held_world(seed, talk=talk, mind=mind, workers=workers)
    sim.run_until(start if start is not None else TICKS_PER_DAY - 6)
    me = next(a["id"] for a in sim.state["agents"] if a["role"] == "guard")
    sim.step([{"kind": "possess", "agent": me}])
    r = random.Random(seed)
    check = Checker(sim)
    for _ in range(ticks):
        ag = sim.state["agents"]
        here = [o["id"] for o in ag if o["id"] != me and o["place"] is not None and o["place"] == ag[me]["place"]
                and not o["asleep"]]
        acts = []
        if here and r.random() < every:
            acts.append({"kind": "act", "action": "say", "target": r.choice(here), "text": "Could you spare some food?"})
        assert check.step(acts) == []
        if sim.state["tick"] in snap_at:
            assert sim.state.get("inflight"), "not vacuous: the snapshot holds calls in flight"
            snap = json.loads(json.dumps(sim.snapshot()))
            sim = Sim.restore(snap, sim.events, sim.mind, workers)
            check = Checker(sim)
    sim.finish()
    return sim, me


def landed(events):
    """(kind, agent, filed, landed) for every plan and reflect call."""
    return [(e.data["kind"], e.actor, e.data.get("filed", e.tick), e.tick) for e in events
            if e.kind in ("mind_plan", "mind_reflect") and not e.data.get("attempt")]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_plan_or_reflection_lands_two_ticks_after_it_was_filed_and_replays_exactly(seed):
    sim, me = talker(seed, TICKS_PER_DAY + 30)  # through a dawn and a dusk
    held = next(e.tick for e in sim.events if e.kind == "possess")
    calls = [c for c in landed(sim.events) if c[1] != me and c[2] > held]  # before the possess, nothing flies
    assert all(t == f for _, a, f, t in landed(sim.events) if f < held)
    kinds = {c[0] for c in calls if c[3] == c[2] + 2}
    assert kinds == {"plan", "reflect"}, calls  # not vacuous: both kinds flew
    assert all(t == f + 2 for _, _, f, t in calls), [c for c in calls if c[3] != c[2] + 2]
    # filed at the tick its wake or bedtime was (the request's prompt and the event's `filed` agree)
    assert not any(e.kind == "deferred" for e in sim.events), "pace-1 is off where pace-2 applies"
    assert "inflight" not in sim.state or all(f["due"] > sim.state["tick"] - 1 for f in sim.state["inflight"].values())
    replay(sim.events)


def test_the_players_own_body_never_flies():
    sim, me = talker(4, TICKS_PER_DAY + 30)
    mine = [c for c in landed(sim.events) if c[1] == me]
    assert all(f == t for _, _, f, t in mine)


@pytest.mark.parametrize("talk", [None, {"consent": "mind"}], ids=["no-talk", "not-held"])
def test_a_world_that_is_not_held_never_flies(talk):
    sim = held_world(5, talk=talk or {}) if talk else Sim(5, 40, StubMind())
    sim.run_until(TICKS_PER_DAY - 6)
    me = next(a["id"] for a in sim.state["agents"] if a["role"] == "guard")
    sim.step([{"kind": "possess", "agent": me}])
    sim.run_until(2 * TICKS_PER_DAY)
    assert not any("filed" in e.data for e in sim.events) and "inflight" not in sim.state


def test_a_held_world_with_no_player_never_flies():
    sim = held_world(6)
    sim.run(2)
    assert not any("filed" in e.data for e in sim.events) and "inflight" not in sim.state


def test_threads_change_nothing_in_the_log():
    """The live path asks a flying call on a worker thread; the log is the one the in-order path writes."""
    a, _ = talker(7, 60, every=0.5)
    b, _ = talker(7, 60, every=0.5, workers=8)
    assert [e.to_json() for e in a.events] == [e.to_json() for e in b.events]


def test_a_reopen_with_calls_in_flight_continues_the_same_world():
    """A snapshot taken while calls are out (live close(), or the daily one) keeps them in state; the restored world
    asks them again at their due tick and writes the log the uninterrupted world wrote."""
    plain, _ = talker(8, 50, every=0.5, start=TICKS_PER_DAY - 6)
    dawn = sorted({f for _, _, f, t in landed(plain.events) if t == f + 2})
    snap_at = {dawn[0] + 1}  # between the filing tick and the landing tick
    again, _ = talker(8, 50, every=0.5, start=TICKS_PER_DAY - 6, snap_at=snap_at)
    assert [e.to_json() for e in again.events] == [e.to_json() for e in plain.events]
    replay(again.events)


class Keeps(StubMind):
    def __init__(self):
        super().__init__()
        self.reqs = []

    def decide(self, req):
        self.reqs.append(req)
        return super().decide(req)


def test_a_villager_with_a_plan_in_flight_answers_the_player_and_its_prompt_shows_no_plan_yet():
    """Steering 09:06Z: an answer filed while that villager's plan is in flight replays exactly, and its prompt doesn't
    show the pending plan as already adopted. The player is a villager who wakes first in a home; a housemate who
    wakes after is spoken to while its morning plan is out."""
    first = held_world(9)
    first.run_until(TICKS_PER_DAY + 40)  # nobody is held yet, so this is the same world up to the possess
    wakes = [e for e in first.events if e.kind == "wake" and e.tick >= TICKS_PER_DAY - 6]
    pairs = [(a.actor, a.tick, b.actor) for a in wakes for b in wakes
             if a.data["place"] == b.data["place"] and a.tick + 2 <= b.tick <= a.tick + 8 and a.actor != b.actor]
    hit = None
    for me, woke, to in pairs:
        mind = Keeps()
        sim = held_world(9, mind=mind)
        sim.run_until(woke + 1)
        ag = sim.state["agents"]
        sim.step([{"kind": "possess", "agent": me}])
        for _ in range(10):
            tick = sim.state["tick"]
            flight = sim.state.get("inflight", {}).get(str(to))
            if flight and flight["kind"] == "plan" and ag[to]["place"] == ag[me]["place"] and not ag[me]["asleep"]:
                sim.step([{"kind": "act", "action": "say", "target": to, "text": "Good morning, what's your plan today?"}])
                if any(e.kind == "said" and e.data["with"] == to for e in sim.events if e.tick == tick):
                    hit = (to, tick)
                break  # landed, or the player's timetable took it away first: the next pair
            sim.step([])
        if hit:
            break
    assert hit, "not vacuous: a line landed on a villager whose plan was in flight"
    to, tick = hit
    sim.run_until(sim.state["tick"] + 3)
    answer = next(e for e in sim.events if e.kind == "mind_answer" and e.actor == to and e.tick >= tick)
    plan = next(e for e in sim.events if e.kind == "mind_plan" and e.actor == to and e.data.get("filed", -1) < tick + 1
                and e.tick >= tick)
    assert answer.tick <= plan.tick, (answer.tick, plan.tick)  # answered while (or as) the plan landed
    req = next(r for r in mind.reqs if r.kind == "answer" and r.agent_id == to and r.tick == answer.data.get("filed", answer.tick))
    blocks = [b for b in json.loads(plan.mind["raw_output"]).get("blocks", [])] if plan.mind["raw_output"].startswith("{") else []
    plan_head = req.prompt.split("# Today's plan", 1)[1].split("#", 1)[0] if "# Today's plan" in req.prompt else ""
    assert not plan_head.strip(), plan_head  # no plan shown as adopted
    assert blocks or plan.mind["raw_output"]  # the plan had content to leak, had it been adopted
    sim.finish()
    replay(sim.events)


# The whole log of the path joon plays: a held, typed world with a player saying lines across a dawn and a dusk, with
# pace-2 on and text-1's hostile mind. Pinned under the branch label, so the rename at merge doesn't move it
# (Steering 2026-11-20 09:36Z: the held path with pace-2 on, pinned byte for byte).
HELD = {1: "c1e3551940a0218af7d08b2e864d5da2cfc9a77fd52e1707222d9e5a31d6c7a3",
        2: "593b6b631294b23682da32020e2d44a3c92f2375e8f299857fd6e2f87abbda0d"}


def held_log(seed):
    import hashlib
    from test_text1 import Hostile
    sim, _ = talker(seed, TICKS_PER_DAY + 30, every=0.6, mind=Hostile(seed))
    flown = sum(1 for e in sim.events if "filed" in e.data)
    return hashlib.sha256("\n".join(e.to_json() for e in sim.events).encode()).hexdigest(), flown, sim


@pytest.mark.parametrize("seed", sorted(HELD))
def test_the_held_typed_path_with_pace_2_on_is_pinned(monkeypatch, seed):
    from asiimov import replay as R
    from asiimov import sim as S
    monkeypatch.setattr(S, "ENGINE_VERSION", "pace-2")
    monkeypatch.setattr(R, "ENGINE_VERSION", "pace-2")
    from test_goods import on_38
    on_38(monkeypatch)  # the pins are engine 38's worlds: goods-1 came after and changes every log
    log, flown, sim = held_log(seed)
    assert flown > 20, "not vacuous: calls flew"
    assert any(e.kind == "mind_answer" for e in sim.events) and any(e.kind == "mind_decide" for e in sim.events)
    assert log == HELD[seed]
    replay(sim.events)


def test_a_body_possessed_while_its_call_is_in_flight_is_the_players_at_once():
    """Review of #104: the player's own body never waits. A villager whose plan is out, taken by the player, acts on
    the player's word at once; its plan still lands at its due tick, and the world replays."""
    sim = held_world(1)
    sim.run_until(TICKS_PER_DAY - 6)
    first = next(a["id"] for a in sim.state["agents"] if a["role"] == "guard")
    sim.step([{"kind": "possess", "agent": first}])
    for _ in range(60):
        out = {int(k): f for k, f in sim.state.get("inflight", {}).items()
               if f["kind"] == "plan" and f["due"] > sim.state["tick"] and int(k) != first}
        if out:
            break
        sim.step([])
    assert out, "not vacuous: a plan was in flight"
    to, flight = min(out.items())
    tick = sim.state["tick"]
    sim.step([{"kind": "possess", "agent": to}, {"kind": "act", "action": "rest"}])
    assert [e.data["ok"] for e in sim.events if e.kind == "receipt" and e.tick == tick and e.actor == to] == [True]
    sim.run_until(flight["due"] + 1)
    plan = next(e for e in sim.events if e.kind == "mind_plan" and e.actor == to and e.tick >= tick)
    assert plan.tick == flight["due"] and plan.data["filed"] == flight["due"] - 2
    sim.finish()
    replay(sim.events)
