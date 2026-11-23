"""apart-1 (Steering 2026-11-19 12:19Z): no yes is offered in talk once the two have parted.

A work or avoid override outranks the listening hold, so a villager can walk off between a line landing and its
answer. Until now a yes was still offered there: the reply could word a handover ("Here, take it") and the settle then
refused it as "not together", so the words claimed food the engine never moved (pt11's headline failure; 18 of the
text-1 pilot's 188 answer and hearing rows). Now the answer's request checks that the two are at one place. If they
aren't, only a no is offered, a typed line is offered no `gives`, and the prompt says why as a fact of the world. It is
no refusal: nothing is counted against the speaker, and a decision held for the day stays.

What it does not cover: the request is built in the listener's turn of the tick, so a speaker who sets off later in
that same tick is still together when it is built. The settle's own check stays for that, and the fuzz below shows
every remaining "not together" deal is of that kind."""
import json
import random

import pytest

from asiimov import sim as S
from asiimov.invariants import Checker, deal_violations
from asiimov.memory import ANSWER_LINE, GIVES_ANSWER_LINE
from asiimov.replay import replay
from asiimov.sim import TICKS_PER_DAY
from test_talk1 import Says, deals, hold, world
from test_text1 import INJECTIONS, PRIVATE, Hostile, Mind

APART = S.TALK_WHY["apart"]


def part(sim, who):
    """Put `who` somewhere else, by hand: the two are no longer at one place."""
    a = sim.state["agents"][who]
    a["place"], a["route"] = next(p for p in sorted(sim.map["places"]) if p != a["place"]), None


def line(sim, to, move="asks_food", then=None, text="Could you spare some food?"):
    """The player's line, then the tick its answer is asked in. `then` runs between the two."""
    act = {"kind": "act", "action": "say", "target": to, "text": text, **({"move": move} if move else {})}
    first = len(sim.events)
    sim.step([act])
    if then:
        then()
    sim.step([])
    return sim.events[first:]


def ready(mind, talk, food=4):
    sim, me, to = world(mind, dict(talk) if talk else None)
    hold(sim, me, to)
    sim.state["agents"][to]["inv"]["food"] = food
    return sim, me, to, sim.state["agents"]


@pytest.mark.parametrize("who", ["listener", "speaker"])
@pytest.mark.parametrize("consent", ["mind", "nature"])
def test_a_chip_ask_is_offered_no_yes_once_the_two_have_parted(consent, who):
    mind = Says("yes")
    sim, me, to, ag = ready(mind, {"consent": "mind"} if consent == "mind" else None)
    f0, rolls = ag[me]["inv"]["food"], json.dumps(sim.rng.counters, sort_keys=True)
    evs = line(sim, to, then=lambda: part(sim, to if who == "listener" else me))
    req = next(r for r in mind.reqs if r.kind == "answer" and r.agent_id == to)
    assert not sim._together(ag[to], ag[me])
    assert req.view["answers"] == ["no"] and req.view["deal"] == {"move": "asks_food", "withheld": "apart"}
    assert f" Your answer is no: you can't say yes, as {APART}." in req.prompt
    assert "A yes hands them" not in req.prompt and "Your answer is yes" not in req.prompt
    ans = next(e for e in evs if e.kind == "mind_answer")
    assert ans.data["offered"] == ["no"] and ans.data["deal"] == {"move": "asks_food", "withheld": "apart"}
    assert deals(evs) == [] and ag[me]["inv"]["food"] <= f0
    # it is no refusal: nothing is counted, no wait starts, the speaker isn't told they were refused, and under
    # nature no roll was made for an answer that could not be had
    rec = ag[to]["talk_deals"][str(me)]
    assert (rec["yes"], rec["no"], rec["last_no"]) == (0, 0, None)
    assert not any(e.kind == "food_refused" for e in evs) and (ag[me]["occasion"] or {}).get("why") != "refused"
    assert not any("talk:" in k for k in set(json.loads(json.dumps(sim.rng.counters))) - set(json.loads(rolls)))
    assert deal_violations(sim.events, 0, me) == []


def test_a_typed_ask_is_offered_no_give_once_they_have_parted_and_the_held_yes_stays():
    mind = Mind(decide="yes", gives="food")
    sim, me, to, ag = ready(mind, PRIVATE)
    home = ag[to]["place"]
    evs = line(sim, to, move=None, then=lambda: part(sim, me))
    [dec] = [e for e in evs if e.kind == "mind_decide"]
    req = mind.of("answer", to)[-1]
    assert "gives" not in req.view and req.view["typed"] == {"withheld": "apart"}
    assert f" You can't give food now, as {APART}." in req.prompt
    assert ANSWER_LINE in req.prompt and GIVES_ANSWER_LINE not in req.prompt and "made up your mind" not in req.prompt
    ans = next(e for e in evs if e.kind == "mind_answer")
    assert ans.data["typed"] == {"offered": ["nothing"], "withheld": "apart"}
    assert deals(evs) == [] and next(e for e in evs if e.kind == "reply").data["gives"] == "nothing"
    held = sim._talk_decision(ag[to], me, S.TYPED_MOVE)
    assert held["answer"] == "yes" and held["event"] == dec.id
    # back together the same day: the same decision is the one the give rests on, and no second one is asked
    ag[me]["place"], ag[me]["route"] = home, None
    hold(sim, me, to)
    f0 = ag[me]["inv"]["food"]
    evs = line(sim, to, move=None, text="Could I have that food now?")
    req = mind.of("answer", to)[-1]
    assert req.view["gives"] == ["food", "nothing"] and APART not in req.prompt
    [d] = deals(evs)
    assert d.data["ok"] and d.data["typed"] == 1 and d.parents[2] == dec.id and ag[me]["inv"]["food"] == f0 + 1
    assert len(mind.of("decide", to)) == 1
    assert deal_violations(sim.events, 0, me) == []


def test_together_nothing_changes():
    mind = Says("yes")
    sim, me, to, ag = ready(mind, {"consent": "mind"})
    evs = line(sim, to)
    req = next(r for r in mind.reqs if r.kind == "answer" and r.agent_id == to)
    assert req.view["answers"] == ["yes", "no"] and APART not in req.prompt
    assert [d.data["ok"] for d in deals(evs)] == [True]


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("talk", [None, {"consent": "mind"}, PRIVATE, {"typed": 1}], ids=["nature", "mind", "private", "script"])
def test_the_sentence_is_in_the_prompt_exactly_when_the_two_are_apart(seed, talk):
    """Hostile minds and lines for a day and a half, in a world no test's hand touches. Each answer to a chip line,
    and each answer to a typed line in a typed world, says the two are apart exactly when they were as its request
    was built. Then nothing is offered and no deal follows. A "not together" deal can still happen, only where the
    request was built with the two together (the speaker set off later in that tick)."""
    mind = Hostile(seed) if talk and (talk.get("typed") or talk.get("consent") == "private") else __import__(
        "test_talk1").Hostile(seed)
    sim, me, to = world(mind, dict(talk) if talk else None, seed=seed)
    r, check, built = random.Random(seed), Checker(sim), {}
    request = sim._request

    def watched(a, kind, view, parents, apply):
        request(a, kind, view, parents, apply)
        occ = view.get("occasion") or {}
        if kind == "answer" and (occ.get("move") or sim._talk_params()["typed"]):
            req = sim.pending[a["id"]][0]
            # reach-1: with the player's body, "together" is one place or within a tile across open ground
            # (Sim._near, Steering 2026-11-23 20:24Z): a villager that has just set off still stands beside it
            together = sim._near(a, sim.state["agents"][occ["who"]])
            built[(sim.state["tick"], a["id"])] = together
            assert (f", as {APART}." in req.prompt) == (not together), req.prompt[-600:]
            offered = req.view.get("answers", []) + req.view.get("gives", [])
            assert together or ("yes" not in offered and "food" not in offered)
            assert together or (req.view.get("deal") or req.view["typed"])["withheld"] == "apart"
    sim._request = watched
    texts = INJECTIONS + ["Could you spare some food?", "#3 says hi", "x" * S.SAY_MAX]
    for _ in range(TICKS_PER_DAY + TICKS_PER_DAY // 2):
        ag = sim.state["agents"]
        here = [o["id"] for o in ag if o["id"] != me and o["place"] is not None and o["place"] == ag[me]["place"]
                and not o["asleep"]]
        acts = []
        if here and r.random() < 0.6:
            act = {"kind": "act", "action": "say", "target": r.choice(here), "text": r.choice(texts)}
            move = r.choice([None, None, "asks_food", "wants_to_buy_food"])
            if move:
                act["move"] = move
            if move == "wants_to_buy_food":
                act["price"] = sim._price() if r.random() < 0.8 else sim._price() + 1
            acts.append(act)
        assert check.step(acts) == [], acts
    assert built, "vacuous: no answer to a chip or a typed line"
    for d in (e for e in sim.events if e.kind == "deal"):
        ans = sim.events[d.parents[1]]
        assert built[(ans.tick, ans.actor)], "a deal after a request built apart"
        assert d.data["ok"] or d.data["why"] != "not together" or built[(ans.tick, ans.actor)]
    assert deal_violations(sim.events, 0, me) == []
    APART_SEEN[(seed, json.dumps(talk))] = sum(not b for b in built.values())
    sim.finish()
    replay(sim.events)


APART_SEEN = {}


def test_the_fuzz_met_parted_pairs():
    """Runs after the fuzz above (file order): some of its worlds must have had an answer asked for apart."""
    if len(APART_SEEN) < 12:
        pytest.skip("the fuzz didn't run in this process")
    assert sum(APART_SEEN.values()) > 0, APART_SEEN
