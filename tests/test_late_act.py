"""The player's act lands mid-tick (E4, plan/readable-world-spec.md, Steering 2026-11-14 10:09Z).

A tick's player acts apply at one fixed point: after the agent loop has built the minds' requests, before any
mind reply applies, so the player goes first in its tick. An act that arrives while the tick's minds are out
joins that tick there, in recorded order, marked `late`; replay applies it at the same point. The runner
acks it and sends its receipt as soon as it applies. A body whose own mind is being asked takes its act next
tick. A world with no player is unchanged."""
import asyncio
import json
import os
import time

import pytest

from asiimov import live
from asiimov.events import Event, read_jsonl
from asiimov.invariants import state_violations
from asiimov.minds import StubMind
from asiimov.replay import replay
from asiimov.sim import TICKS_PER_DAY, Sim
from test_inhabitant import batch_of, intent
from test_live import WS


def awake_body(seed=4, n=14):
    sim = Sim(seed, n, StubMind())
    sim.run_until(TICKS_PER_DAY // 2)
    b = next(a for a in sim.state["agents"] if not a["asleep"] and a["place"] is not None
             and a["sleep_tick"] > sim.state["tick"] + 30)
    sim.step([{"kind": "possess", "agent": b["id"]}])
    return sim, b["id"]


def late(action, **kw):
    return {"kind": "act", "action": action, **kw}


def far(sim, aid):
    return next(p for p in ("woods", "field", "market", "workshop", "plaza") if p != sim.state["agents"][aid]["place"])


def test_a_late_act_applies_before_any_mind_reply_and_replays_there():
    sim, b = awake_body()
    seen = []

    def during(s, done):
        seen.append(s.late_act(late("go_to", place=far(s, b))))
    n0 = len(sim.events)
    sim.step([], during=during)
    tick_events = sim.events[n0:]
    eid = seen[0]
    act = sim.events[eid]
    assert act.kind == "act" and act.origin == "player" and act.data["late"] is True
    rec = next(e for e in tick_events if e.kind == "receipt" and e.parents[0] == eid)
    assert rec.data["why"] == "heading there"
    minds = [e.id for e in tick_events if e.origin == "mind"]
    assert not minds or rec.id < min(minds), "the player's act applies before every mind reply in its tick"
    for t in range(40):
        sim.step([], during=(lambda s, d: s.late_act(late("go_to", place=far(s, b)))) if t % 5 == 0 else None)
        assert not state_violations(sim)
    sim.finish()
    events = [Event(**json.loads(e.to_json())) for e in sim.events]
    assert replay(events).state_hash() == sim.state_hash()


def test_a_late_act_after_the_bodys_one_act_this_tick_waits_for_the_next():
    """One act a tick, as ever; a plan's next step sent on the last one's receipt, mid-tick, takes the next
    tick instead of a refusal."""
    sim, b = awake_body()
    got, n = [], len(sim.events)
    sim.step([late("rest")], during=lambda s, d: got.append(s.late_act(late("work"))))
    assert got == [None] and sum(e.origin == "player" for e in sim.events[n:]) == 1
    got2 = []
    sim.step([], during=lambda s, d: got2.append(s.late_act(late("work"))))
    assert got2[0] is not None and sim.events[got2[0]].data == {"action": "work", "late": True}


def test_a_late_act_while_asleep_is_refused_as_an_early_one_would_be():
    sim, b = awake_body()
    a = sim.state["agents"][b]
    while not a["asleep"]:
        sim.step([])
    got = []
    sim.step([], during=lambda s, d: got.append(s.late_act(late("work"))))
    r = next(e for e in sim.events if e.kind == "receipt" and e.parents and e.parents[0] == got[0])
    assert r.data == {"action": "work", "ok": False, "why": "asleep"}


def test_a_body_whose_mind_is_being_asked_takes_its_act_next_tick():
    sim, b = awake_body()
    sim.step([{"kind": "act", "action": "wait"}])  # idle soon: the hand-back asks the mind
    for _ in range(200):
        n = len(sim.events)
        hit = []

        def during(s, d):
            if b in s.pending:
                hit.append(s.late_act(late("work")))
        sim.step([], during=during)
        if hit:
            assert hit == [None] and not any(e.origin == "player" for e in sim.events[n:])
            return
    pytest.skip("the hand-back never asked the mind in 200 ticks")


def test_a_late_act_for_a_body_whose_mind_is_out_cannot_be_replayed_into_its_tick():
    sim, b = awake_body()
    sim.pending[b] = ("x",)  # as if the hand-back's request were out
    assert not sim.takes_late() and sim.late_act(late("work")) is None
    sim.pending.clear()


class SlowMind(StubMind):
    """A stub that takes `delay` seconds a call, like a model on a pod."""

    def __init__(self, delay=0.15, gate=None):
        super().__init__()
        self.delay, self.gate = delay, gate

    def decide(self, req):
        if self.gate is not None:
            self.gate()
        time.sleep(self.delay)
        return super().decide(req)


def _runner_at_noon(out, mind, seed=4, n=20):
    r = live.LiveRunner(out, mind, seed, n)
    while r.sim.state["tick"] < TICKS_PER_DAY // 2:
        r.step()
    return r


def test_an_act_sent_mid_tick_is_acked_with_its_receipt_before_the_tick_ends(tmp_path):
    out = str(tmp_path / "w")
    r = _runner_at_noon(out, SlowMind(0.0))
    aid = next(a["id"] for a in r.sim.state["agents"] if not a["asleep"] and a["sleep_tick"] > r.sim.state["tick"] + 60)
    r.mind.delay = 0.15  # each tick's mind calls now take a while
    got = {"early": [], "acks": [], "ticks": []}

    async def main():
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.ensure_future(live.serve(runner=r, port=0, tps=1000, paused=True,
                                                started=lambda port, hub: ready.set_result(port)))
        port = await ready
        c = await WS.connect(port)
        c.send({"type": "hello", "protocol": 3, "scope": "inhabitant", "agent": aid})
        await c.until("welcome")
        god = await WS.connect(port)
        god.send({"type": "hello", "protocol": 3})
        await god.until("welcome")
        god.send({"type": "control", "op": "speed", "tps": 1000})  # back to back: any send lands mid-tick
        god.send({"type": "control", "op": "run"})
        k, t_sent, tick_done = 0, {}, None
        while len(got["acks"]) < 6:
            m = await c.recv()
            now = time.monotonic()
            if m["type"] == "tick":
                got["ticks"].append((m["tick"], now))
                if not m["view"]["asleep"]:
                    await asyncio.sleep(0.05)  # the next tick is running: send while its minds are out
                    k += 1
                    place = "woods" if k % 2 else "plaza"
                    t_sent[f"m{k}"] = time.monotonic()
                    c.send(intent(f"m{k}", "act", {"action": "go_to", "place": place}, m["view"]["tick"]))
            elif m["type"] == "early":
                got["early"].append((m, now))
            elif m["type"] == "ack" and m["id"] in t_sent:
                got["acks"].append((m, now - t_sent[m["id"]]))
        god.send({"type": "control", "op": "pause"})
        await god.until("status")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    r.finish()
    early_ids = {e["id"] for m, _ in got["early"] for e in m["events"]}
    mid = [(m, dt) for m, dt in got["acks"] if m["status"] == "applied" and
           any(e["parents"] and e["parents"][0] == m["event"] for em, _ in got["early"] for e in em["events"])]
    assert mid, got["acks"]
    assert len(mid) >= 4, got["acks"]  # most went in mid-tick; one whose body's mind was out waits a tick
    # Mid-tick acts came back well inside a tick's mind time (20 agents x 0.15 s, one worker).
    assert sorted(dt for _, dt in mid)[len(mid) // 2] < 1.5, [dt for _, dt in mid]
    events = list(read_jsonl(os.path.join(out, "events.jsonl")))
    lates = [e for e in events if e.kind == "act" and e.data.get("late")]
    acts = {e.id for e in events if e.kind == "act"}
    assert lates and {m["event"] for m, _ in mid} <= acts  # late ones, and ones that waited a tick, acked early
    assert [l.rstrip("\n") for l in open(os.path.join(out, "events.jsonl"))] == batch_of(out)
    assert early_ids, "receipts went out early"


def test_kill_mid_tick_after_an_early_ack_and_resume_to_the_same_event(tmp_path):
    """Steering's required test: the receipt depends only on what came before the fixed point, so a resume
    re-runs the tick to the same act event and outcome the early ack gave."""
    out = str(tmp_path / "w")
    r = _runner_at_noon(out, StubMind())
    aid = next(a["id"] for a in r.sim.state["agents"] if not a["asleep"] and a["sleep_tick"] > r.sim.state["tick"] + 60)
    r.inhabit(aid, "c1")
    r.step()
    early = []

    def send(ack, evs, view=None):  # the ack goes out, then the process dies before the tick ends
        early.append((ack, evs))
        raise RuntimeError("killed mid-tick")
    t = r.sim.state["tick"]
    r.queue_late(intent("k1", "act", {"action": "go_to", "place": far(r.sim, aid)}, t), "c1", "inhabitant", send)
    with pytest.raises(RuntimeError, match="killed mid-tick"):
        r.step()
    (ack, evs), = early
    assert ack["status"] == "applied" and ack["tick"] == t
    receipt = next(e for e in evs if e["kind"] == "receipt")
    r.side.close()
    r.run.log.close()

    r2 = live.LiveRunner(out, StubMind(), 4, 20)  # reopen: the crashed tick runs again from the record
    assert r2.intents["k1"]["status"] == "applied"  # rebuilt from the sidecar, so step() checks the re-run against it
    while True:
        tick, events, acks = r2.step()
        if tick == t:
            break
    again = {e.id: e for e in events}
    assert again[ack["event"]].kind == "act" and again[ack["event"]].data.get("late") is True
    rec = next(e for e in events if e.kind == "receipt" and e.parents and e.parents[0] == ack["event"])
    assert rec.id == receipt["id"] and rec.data == receipt["data"]
    again_ack = r2.submit(intent("k1", "act", {"action": "work"}, tick))  # a client re-sending it gets the same answer
    assert {k: again_ack[k] for k in ("id", "status", "tick", "event")} == \
        {"id": "k1", "status": "applied", "tick": t, "event": ack["event"]}
    r2.finish()
    events = [Event(**json.loads(l)) for l in open(os.path.join(out, "events.jsonl"))]
    assert replay(events).state_hash() == r2.sim.state_hash()


def test_a_rerun_that_moved_an_early_acked_act_is_caught(tmp_path):
    """Review of #69: an act acked at the fixed point is checked against the tick's events once it ends."""
    out = str(tmp_path / "w")
    r = _runner_at_noon(out, StubMind())
    aid = next(a["id"] for a in r.sim.state["agents"] if not a["asleep"] and a["sleep_tick"] > r.sim.state["tick"] + 60)
    r.inhabit(aid, "c1")
    r.step()
    t = r.sim.state["tick"]
    r.queue_late(intent("k2", "act", {"action": "work"}, t), "c1", "inhabitant", lambda ack, evs, view=None: None)

    def tamper(sim, done, run=r._during):
        run(sim, done)
        r.intents["k2"]["event"] += 1  # as if the record and the re-run disagreed
    r._during = tamper
    with pytest.raises(RuntimeError, match="was acked as event"):
        r.step()


def test_after_a_resume_an_early_ack_the_rerun_contradicts_is_caught(tmp_path):
    """Steering's condition on #69: the check runs after a reopen too, from the intents the sidecar rebuilds."""
    out = str(tmp_path / "w")
    r = _runner_at_noon(out, StubMind())
    aid = next(a["id"] for a in r.sim.state["agents"] if not a["asleep"] and a["sleep_tick"] > r.sim.state["tick"] + 60)
    r.inhabit(aid, "c1")
    r.step()
    t = r.sim.state["tick"]

    def send(ack, evs, view=None):
        raise RuntimeError("killed mid-tick")
    r.queue_late(intent("k3", "act", {"action": "work"}, t), "c1", "inhabitant", send)
    with pytest.raises(RuntimeError, match="killed mid-tick"):
        r.step()
    r.side.close()
    r.run.log.close()
    side = os.path.join(out, "runner.jsonl")
    lines = [json.loads(l) for l in open(side)]
    for x in lines:  # the sidecar says the ack named another event than the re-run will make
        if x["kind"] == "applied" and x["id"] == "k3":
            x["event"] += 1
    with open(side, "w") as f:
        f.write("".join(json.dumps(x, sort_keys=True) + "\n" for x in lines))
    r2 = live.LiveRunner(out, StubMind(), 4, 20)
    assert r2.intents["k3"]["status"] == "applied"
    with pytest.raises(RuntimeError, match="'k3' was acked as event"):
        while r2.sim.state["tick"] <= t:
            r2.step()

