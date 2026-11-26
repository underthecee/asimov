"""The plan lane (Steering 2026-11-25 03:56Z, after aipt4's dawn tail): with --plan-lane N, at most N plan and reflect
calls are at the model server at once, while answers, decisions and hearings skip the lane. It is timing only: in a
held world every flying plan still lands at t+2, and the world log is the one written without a lane."""
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from asiimov.llm import LANE_KINDS, LLMMind
from asiimov.memory import prompt_hash
from asiimov.minds import MindRequest, StubMind
from asiimov.replay import replay
from asiimov.sim import TICKS_PER_DAY

from test_pace2 import landed, talker


class Served(LLMMind):
    """An LLMMind whose server is StubMind: each reply is the stub's for that request, after a short wait keyed on
    the prompt (so replies finish out of order), while counting how many calls of each sort are at the server."""

    def __init__(self, plan_lane=0, wait=0.002, waits=None):
        super().__init__("http://fake", "fake", plan_lane=plan_lane)
        self.stub, self.wait, self.waits = StubMind(), wait, waits or {}
        self._mu = threading.Lock()
        self.at_server = {"lane": 0, "other": 0}
        self.most = {"lane": 0, "other": 0}

    def body(self, req):
        return {**super().body(req), "_req": req}

    def post(self, path, body):
        req = body["_req"]
        side = "lane" if req.kind in LANE_KINDS else "other"
        with self._mu:
            self.at_server[side] += 1
            self.most[side] = max(self.most[side], self.at_server[side])
        try:
            time.sleep(self.waits.get(req.kind, self.wait * (int(req.prompt_hash[:2], 16) % 5)))
            out = self.stub.decide(req)
        finally:
            with self._mu:
                self.at_server[side] -= 1
        return {"choices": [{"message": {"content": out.raw_output}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "model": "fake"}


def req(kind, i):
    p = f"{kind} {i}"
    return MindRequest(kind, i, 0, p, prompt_hash(p), {})


def test_no_more_than_the_lane_of_plans_and_reflections_are_at_the_server():
    m = Served(plan_lane=3, waits={"plan": 0.02, "reflect": 0.02})
    m.stub.decide = lambda r: _raw()
    with ThreadPoolExecutor(64) as ex:
        list(ex.map(m.decide, [req(k, i) for i in range(20) for k in ("plan", "reflect")]))
    assert m.most["lane"] == 3, m.most  # not vacuous: the lane filled
    waited = [c["lane_wait_s"] for c in m.calls]
    assert max(waited) > 0.1 and all(c["latency_s"] < 0.2 for c in m.calls)  # the wait is the lane's, not the server's


def test_an_answer_does_not_wait_behind_a_full_lane():
    m = Served(plan_lane=1, waits={"plan": 0.1, "answer": 0.0})
    m.stub.decide = lambda r: _raw()
    with ThreadPoolExecutor(64) as ex:
        plans = [ex.submit(m.decide, req("plan", i)) for i in range(10)]  # a second of plans, one at a time
        time.sleep(0.05)
        t0 = time.monotonic()
        ex.submit(m.decide, req("answer", 99)).result()
        took = time.monotonic() - t0
        assert sum(p.done() for p in plans) < 3, "not vacuous: the plans were still queued"
    assert took < 0.08, took
    assert [c for c in m.calls if c["kind"] == "answer"][0].get("lane_wait_s") is None


def test_with_no_lane_nothing_waits_on_one():
    m = Served()
    m.stub.decide = lambda r: _raw()
    with ThreadPoolExecutor(16) as ex:
        list(ex.map(m.decide, [req("plan", i) for i in range(16)]))
    assert m._lane is None and not any("lane_wait_s" in c for c in m.calls)


def _raw():
    from asiimov.minds import MindReply
    return MindReply("{}", "stub", {}, "stop")


def run(seed, plan_lane, workers):
    m = Served(plan_lane=plan_lane)
    sim, me = talker(seed, TICKS_PER_DAY + 30, every=0.6, mind=m, workers=workers)
    return sim, me, m


def test_with_a_lane_every_plan_still_lands_at_t_plus_2_and_the_log_is_unchanged():
    """A dawn and a dusk of a held, typed world with a player talking: a lane of 2 on 32 worker threads writes the
    log the in-order, laneless path writes, byte for byte, and every flown call landed two ticks after it was filed."""
    a, me, _ = run(5, plan_lane=0, workers=1)
    b, _, m = run(5, plan_lane=2, workers=32)
    held = next(e.tick for e in b.events if e.kind == "possess")
    flown = [c for c in landed(b.events) if c[1] != me and c[2] > held]
    assert len(flown) > 20 and {c[0] for c in flown} == {"plan", "reflect"}, "not vacuous: both kinds flew"
    assert all(t == f + 2 for _, _, f, t in flown), [c for c in flown if c[3] != c[2] + 2]
    assert m.most["lane"] == 2, m.most  # the lane held calls back
    ha = hashlib.sha256("\n".join(e.to_json() for e in a.events).encode()).hexdigest()
    hb = hashlib.sha256("\n".join(e.to_json() for e in b.events).encode()).hexdigest()
    assert ha == hb
    replay(b.events)
