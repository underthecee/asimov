"""Protocol 2's inhabitant scope (plan/player-body-spec.md): an inhabitant's body, what it is sent (bar 5),
and the release of a body left held when the runner stopped (bar 2's kill case)."""
import asyncio
import json
import os
import random

import pytest

from asiimov import inhabitant, live, persist
from asiimov.events import read_jsonl
from asiimov.minds import NoisyMind, StubMind
from asiimov.replay import replay
from asiimov.sim import TICKS_PER_DAY, Sim
from asiimov.worldmap import SEE, near, place_at
from test_live import WS

SEED, AGENTS = 4, 10
FORBIDDEN = {"soul", "traits", "params", "raw_output", "prompt_hash", "mind", "values", "need_weights",
             "cognition", "population"}


def intent(iid, verb, args, seen):
    return {"type": "intent", "id": iid, "verb": verb, "args": args, "seen_tick": seen}


def log(out):
    return [l.rstrip("\n") for l in open(os.path.join(out, "events.jsonl"))]


def batch_of(out):
    """The same world run in one go with run.json's schedule: what a live world must equal."""
    cfg = persist.config(out)
    sim = Sim(cfg["seed"], cfg["agents"], StubMind())
    log = read_jsonl(os.path.join(out, "events.jsonl"))
    end = log[-1].tick
    sim.run_until(end, {t: [a for tt, a in cfg["player"] if tt == t] for t, _ in cfg["player"]})
    # time-1a: the log may end on actions taken ahead of a tick that never ran; as many as it holds, as replay does
    taken = sum(e.origin == "player" and e.tick == end for e in log)
    for a in [a for t, a in cfg["player"] if t == end][:taken]:
        sim.take(a)
    sim.finish()
    return [e.to_json() for e in sim.events]


def test_an_inhabitant_possesses_acts_and_its_leaving_releases_the_body(tmp_path):
    out = str(tmp_path / "w")
    r = live.LiveRunner(out, StubMind(), SEED, AGENTS)
    for _ in range(20):
        r.step()
    assert r.inhabit(10, "c1") == "bad_agent" and r.inhabit(True, "c1") == "bad_agent"
    assert r.inhabit(3, "c1") is None
    assert r.inhabit(3, "c2") == "body_taken" and r.inhabit(4, "c2") == "body_taken"
    no = lambda iid, verb, args, scope: r.submit(intent(iid, verb, args, 20), scope=scope)["reason"]
    assert no("g1", "possess", {"agent": 5}, "god") == "inhabited"
    assert no("g2", "release", {}, "god") == "inhabited"
    assert no("g3", "act", {"action": "work"}, "god") == "inhabited"
    assert no("i1", "fire", {"family": 0}, "inhabitant") == "scope"
    assert no("i2", "release", {}, "inhabitant") == "scope"
    assert no("~x", "act", {"action": "work"}, "inhabitant") == "bad_id"
    assert r.submit(intent("g4", "fire", {"family": 1}, 20), scope="god") is None  # god keeps fire
    assert r.submit(intent("i3", "act", {"action": "rest"}, 20), scope="inhabitant") is None
    _, events, acks = r.step()
    assert [e.kind for e in events if e.origin == "player"] == ["possess", "fire", "act"]
    assert sorted(a["id"] for a in acks) == ["g4", "i3", "~possess-20-0"]
    r.step()
    r.leave("c2")  # not the inhabitant: nothing
    r.leave("c1")
    r.step()
    assert "player" not in r.sim.state and r.inhabitant is None
    assert r.inhabit(5, "c3") is None  # free again
    r.leave("c3")
    for _ in range(10):
        r.step()
    r.finish()
    assert log(out) == batch_of(out)
    assert replay(read_jsonl(os.path.join(out, "events.jsonl")))


@pytest.mark.parametrize("trial", range(12))
def test_a_body_left_held_by_a_crash_is_released_36_ticks_after_reopening(tmp_path, trial):
    """Kill while an inhabitant holds the body, at a random tick and phase; reopen with nobody coming back.
    The release is recorded ORPHAN_TICKS after the reopened view tick, and the log equals batch with that
    release scripted. Odd trials kill a second time, after reopening, before or after the release."""
    rng = random.Random(trial)
    out = str(tmp_path / "w")
    r = live.LiveRunner(out, StubMind(), SEED, AGENTS, snapshot_every=24)
    kill = rng.randrange(25, 70)
    while r.sim.state["tick"] < kill:
        if r.sim.state["tick"] == 20:
            assert r.inhabit(2, "c1") is None
        if r.sim.state["tick"] > 20 and rng.random() < 0.3:
            r.submit(intent(f"a{r.sim.state['tick']}", "act", {"action": rng.choice(["work", "rest", "socialize"])},
                            r.sim.state["tick"]), scope="inhabitant")
        r.step()
    if trial % 3 == 0:
        r.close()  # a clean stop with the inhabitant still connected counts the same
    r = live.LiveRunner(out, StubMind())
    reopened = r.sim.state["tick"]
    assert r.orphan_at == reopened + live.ORPHAN_TICKS and r.sim.state["player"]["agent"] == 2
    if trial % 2:
        gap = rng.choice([5, 40])  # killed again before the release, or after it
        while r.sim.state["tick"] < reopened + gap:
            r.step()
        r = live.LiveRunner(out, StubMind())
        if gap == 5:  # still held: the count starts again from this reopen
            reopened = r.sim.state["tick"]
            assert r.orphan_at == reopened + live.ORPHAN_TICKS
        else:
            # released: either already in the view, or scheduled in run.json past a view tick that a reopen
            # rolled back to the log's last flushed tick (where the buffer cut it moves with the log's size)
            assert r.orphan_at is None and r._held_agent() is None
    while r.sim.state["tick"] < 160:
        r.step()
    r.finish()
    events = read_jsonl(os.path.join(out, "events.jsonl"))
    releases = [e for e in events if e.kind == "release"]
    assert len(releases) == 1 and "player" not in r.sim.state
    assert releases[0].tick == reopened + live.ORPHAN_TICKS
    assert log(out) == batch_of(out)


def test_a_crash_that_rolls_back_past_the_possess_still_releases_the_body(tmp_path):
    """Review of #40: a reopen re-runs ticks whose possess run.json already holds, so the view can sit
    before the possess. The body still counts as held, from the tick the possess applies."""
    out = str(tmp_path / "w")
    r = live.LiveRunner(out, StubMind(), 3, 8, snapshot_every=10 ** 6)
    for t in range(7):
        if t == 5:
            assert r.inhabit(2, "c1") is None
        r.step()
    assert r.sim.state["player"]["agent"] == 2
    r = live.LiveRunner(out, StubMind())  # crash: resume re-runs from genesis and drops the last tick
    assert r.sim.state["tick"] < 6 and "player" not in r.sim.state
    assert r.orphan_at == 6 + live.ORPHAN_TICKS and r.inhabit(3, "c2") == "body_taken"
    while r.sim.state["tick"] < 60:
        r.step()
    r.finish()
    assert [e.tick for e in read_jsonl(os.path.join(out, "events.jsonl")) if e.kind == "release"] == [6 + live.ORPHAN_TICKS]
    assert log(out) == batch_of(out)


def test_an_inhabitant_that_comes_back_claims_its_body_and_another_cannot(tmp_path):
    out = str(tmp_path / "w")
    r = live.LiveRunner(out, StubMind(), SEED, AGENTS)
    for t in range(30):
        if t == 10:
            assert r.inhabit(6, "c1") is None
        r.step()
    r = live.LiveRunner(out, StubMind())  # crash
    assert r.inhabit(7, "c2") == "body_taken"
    for _ in range(5):
        r.step()
    assert r.inhabit(6, "c3") is None and r.orphan_at is None
    for _ in range(60):
        r.step()
    assert r.sim.state["player"]["agent"] == 6  # not released at reopen + 36
    r.leave("c3")
    r.step()
    assert "player" not in r.sim.state
    # A god-scope possession is left alone on reopen, as on a god client's disconnect.
    assert r.submit(intent("gp", "possess", {"agent": 1}, r.sim.state["tick"])) is None
    r.step()
    r = live.LiveRunner(out, StubMind())
    assert r.orphan_at is None and r.inhabit(1, "c4") == "body_taken"
    r.finish()
    assert log(out) == batch_of(out)


def _keys(x, path=""):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k, path
            yield from _keys(v, f"{path}.{k}")
    elif isinstance(x, list):
        for v in x:
            yield from _keys(v, path)


def test_an_inhabitant_is_sent_nothing_it_could_not_perceive(tmp_path):
    """Bar 5, over a 3-day world with noisy minds and an inhabitant acting from its offer every few ticks:
    every message is scanned for souls, traits, params and mind records, and for positions of anyone not
    at its place. The hello is fuzzed. The log replays to the same end hash, so building views changed nothing."""
    out = str(tmp_path / "w")
    runner = live.LiveRunner(out, NoisyMind(), 2, 25, mind_kind="noisy")
    end = 3 * TICKS_PER_DAY
    for _ in range(200):  # its own mind lives first, so the backlog holds its pre-possession intents
        runner.step()
    aid = min(e.actor for e in runner.sim.events if e.kind == "intent_set")
    rng = random.Random(0)
    stats = {"acts": 0, "receipts": 0, "present": 0, "offers": 0, "others": 0, "own_intents": 0, "seen": 0}

    def check(m, me_pos):
        bad = {k for k, _ in _keys(m)} & FORBIDDEN
        assert not bad, (bad, m["type"])
        if m["type"] in ("tick", "welcome"):
            v = m["view"]
            assert set(v) <= set(inhabitant.VIEW_KEYS)
            for p in v["present"]:  # space-1: each stands on a tile of the place it shares with the body
                assert place_at(runner.sim.map, p["pos"]) == v["place"], "a position of someone not at its place"
                stats["present"] += 1
            for p in v["seen"]:  # and the only other positions are of those in sight across open ground
                assert (v["place"] is None or p["place"] is None) and near(v["pos"], p["pos"], SEE), \
                    "a position of someone out of sight"
                stats["seen"] += 1
            assert "agents" not in v
        here = {p["id"] for p in m["view"]["present"] + m["view"]["seen"]} if m["type"] == "tick" else set()
        for e in m.get("events", ()):
            assert e["kind"] not in ("genesis", "checkpoint") and e["origin"] != "mind"
            assert (e["actor"] == aid or e["kind"] == "fire" or aid in [e["data"].get(k) for k in inhabitant.NAMES]
                    or e["actor"] in here), ("an event it could not witness", e)
            assert not (e["kind"].startswith("intent_") and e["actor"] != aid), e  # a mind's words are its own body's only
            stats["own_intents"] += e["kind"] == "intent_set" and e["actor"] == aid
            if e["actor"] not in (aid, None):  # another villager's: only what it does in plain sight
                assert e["kind"] in inhabitant.SEEN_NAMING and set(e["data"]) - {"says"} <= set(inhabitant.SEEN_NAMING[e["kind"]]), e
                if "says" in e["data"]:  # another's own words, only for its act aimed at this villager
                    assert aid in (e["data"].get("to"), e["data"].get("with")), e
                    assert e["kind"] in ("ask", "meet") or e["data"]["action"] in inhabitant.SAYS_ACTS, e
                assert e["kind"] != "receipt" or e["data"].get("to") == aid, e
                stats["others"] += 1

    async def main():
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.ensure_future(live.serve(runner, port=0, tps=1000, paused=True,
                                                started=lambda port, hub: ready.set_result(port)))
        port = await ready
        for agent in (25, -1, "4", None, [4], 4.0):
            c = await WS.connect(port)
            c.send({"type": "hello", "protocol": 3, "scope": "inhabitant", "agent": agent})
            assert (await c.recv())["reason"] == "bad_agent" and await c.recv() is None
        c = await WS.connect(port)
        c.send({"type": "hello", "protocol": 3, "scope": "inhabitant", "agent": aid})
        w = await c.recv()
        assert w["type"] == "welcome" and w["world"]["me"]["id"] == aid and set(w["verbs"]) == {"act"}
        check(w, None)
        taken = await WS.connect(port)
        taken.send({"type": "hello", "protocol": 3, "scope": "inhabitant", "agent": 5})
        assert (await taken.recv())["reason"] == "body_taken"
        god = await WS.connect(port)
        god.send({"type": "hello", "protocol": 3})
        await god.until("welcome")
        god.send({"type": "control", "op": "run"})
        acts, receipts = {}, {}
        while True:
            m = await c.recv()
            check(m, None)
            if m["type"] == "ack" and m["status"] == "applied":
                acts[m["event"]] = m["id"]
            if m["type"] not in ("tick", "early"):
                continue
            for e in m["events"]:  # its ack follows the tick, or (E4) the early message of a mid-tick act
                if e["kind"] == "receipt" and e["actor"] == aid and e["parents"]:
                    receipts[e["parents"][0]] = e["data"]["why"]
            if m["type"] == "early":
                continue
            v = m["view"]
            stats["offers"] += bool(v["offer"])
            if v["offer"] and rng.random() < 0.2:
                action = rng.choice(sorted(v["offer"]))
                args = {"action": action}
                if v["offer"][action]:
                    args["target"] = rng.choice(v["offer"][action])
                c.send(intent(f"i{v['tick']}", "act", args, v["tick"]))
                stats["acts"] += 1
            if v["tick"] >= end:
                break
        god.send({"type": "control", "op": "pause"})
        await god.until("status")
        c.w.close()
        god.send({"type": "control", "op": "step"})
        await god.until("tick")
        while "player" in runner.sim.state:  # the release lands on the next tick the hub runs
            god.send({"type": "control", "op": "step"})
            await god.until("tick")
        stats["receipts"] = len(set(acts) & set(receipts))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    runner.finish()
    assert stats["acts"] > 20 and stats["receipts"] >= stats["acts"] - 2 and stats["present"] and stats["seen"] and stats["offers"] and stats["others"] and stats["own_intents"]
    events = read_jsonl(os.path.join(out, "events.jsonl"))  # noisy minds: replay the recorded replies
    assert replay(events).state_hash() == events[-1].data["hash"]


def test_an_act_that_never_reached_the_villager_is_not_witnessed_and_the_village_s_names_are_known():
    """Client pass before 16b's rated session: a receipt aimed at the villager that failed because it was away
    or asleep is something it never saw; and a villager knows everyone's name and role, nothing more."""
    e = {"kind": "receipt", "origin": "rule", "actor": 2, "tick": 5,
         "data": {"action": "ask_food", "ok": False, "to": 7}, "parents": []}
    for why, seen in (("target not here", False), ("target asleep", False), ("no coin", False), ("refused", True), ("gave", True)):
        assert inhabitant.witnessed({**e, "data": {**e["data"], "why": why}}, 7, set(), -1) is seen, why
        held = {**e, "data": {**e["data"], "why": why}, "parents": [1, 2]}  # settles an ask it saw: always closed
        assert inhabitant.witnessed(held, 7, set(), -1), why
    sim = Sim(3, 12, StubMind())
    people = inhabitant.welcome_world(sim, 4)["people"]
    assert [p["id"] for p in people] == list(range(12))
    assert all(set(p) == {"id", "name", "role"} for p in people)
