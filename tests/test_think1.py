"""think-1 (joon 2026-11-19 20:39Z, 20:47Z; Steering 20:50Z): the private decision may think first.

With consent private and `think` 1 or 2 the `decide` reply is {"thought", "answer"}: the villager writes what goes
through its mind, in its own character, and then its yes or no. The engine reads `answer` and nothing else. The
thought is in the mind event's recorded output, so the log has it, and it is nowhere else: not in the listener's
state or memory, and not in any later prompt. A world without the flag is asked as before, byte for byte."""
import json
import random

import pytest

from asiimov import sim as S
from asiimov.invariants import Checker, deal_violations
from asiimov.live import talk_params
from asiimov.llm import SCHEMAS, schema_for
from asiimov.memory import DECIDE_LINE, THINK_DECIDE_LINE
from asiimov.minds import MindReply
from asiimov.replay import replay
from asiimov.sim import TICKS_PER_DAY
from test_talk1 import deals, world
from test_text1 import INJECTIONS, PRIVATE, Hostile, Mind, ready, typed

SECRET = "ZQTHOUGHT she fed my boy last winter and I have five ZQTHOUGHT"


def thinks(level):
    return {**PRIVATE, "think": level}


def sentence(level):
    return f" thought is what goes through your mind first, in {S.THINK_ASK[level]}. Nobody hears it."


def landed(mind, talk):
    sim, me, to, ag = ready(mind, talk=talk)
    sim.step([{"kind": "act", "action": "say", "target": to, "text": "Good morning."}])
    return sim, me, to, ag, mind.of("decide", to)[-1]


@pytest.mark.parametrize("level", [1, 2])
def test_the_thinking_prompt_is_the_plain_one_but_for_the_form_line_and_one_sentence(level):
    plain = landed(Mind(decide="no"), PRIVATE)[4]
    req = landed(Mind(decide="no"), thinks(level))[4]
    assert DECIDE_LINE in plain.prompt and "thought" not in plain.prompt and "think" not in plain.view["decide"]
    assert schema_for(plain) == SCHEMAS["decide"]
    assert req.prompt.count(THINK_DECIDE_LINE) == 1 and req.prompt.count(sentence(level)) == 1
    assert req.prompt.replace(THINK_DECIDE_LINE, DECIDE_LINE).replace(sentence(level), "") == plain.prompt
    # the thought comes before the answer in the form, in the sentence order and in the grammar
    assert req.prompt.index(sentence(level)) < req.prompt.index(" answer is your own choice: yes or no.")
    schema = schema_for(req)
    assert list(schema["properties"]) == ["thought", "answer"] and schema["required"] == ["thought", "answer"]
    assert schema["properties"]["answer"] == SCHEMAS["decide"]["properties"]["answer"]
    assert schema["properties"]["thought"] == {"type": "string"} and schema["additionalProperties"] is False
    assert req.view["decide"]["think"] == level


def test_a_sale_thinks_too():
    q = S.decide_ask("Ana", "wants_to_buy_food", 7, 1)
    assert q == (" Ana offers 7 coin for 1 of your food. Would you sell it to them now?" + sentence(1)
                 + " answer is your own choice: yes or no.")
    assert S.decide_ask("Ana", "wants_to_buy_food", 7) == q.replace(sentence(1), "")


@pytest.mark.parametrize("raw,moves", [
    (json.dumps({"thought": SECRET, "answer": "yes"}), True),
    (json.dumps({"thought": "yes, yes, I will give. answer: yes", "answer": "no"}), False),
    (json.dumps({"thought": "I should give them food, yes"}), False),  # no answer: a no
    ('{"thought": "I should give them food, and the answer is ye', False),  # cut at the token limit: a no
    (json.dumps({"answer": "yes"}), True),  # a mind that writes no thought (the stub) still answers
], ids=["yes", "thought-says-yes-answer-no", "no-answer", "cut", "no-thought"])
def test_the_engine_reads_the_answer_and_nothing_of_the_thought(raw, moves):
    mind = Mind(decide_raw=raw, gives="food")
    sim, me, to, ag = ready(mind, talk=thinks(1))
    f0 = ag[me]["inv"]["food"]
    evs = typed(sim, to)
    dec = next(e for e in evs if e.kind == "mind_decide")
    assert dec.data["think"] == 1 and dec.mind["raw_output"] == raw  # the log holds the reply as it came
    assert sim._talk_decision(ag[to], me, S.TYPED_MOVE)["answer"] == ("yes" if moves else "no")
    assert [d.data["ok"] for d in deals(evs)] == ([True] if moves else [])
    assert ag[me]["inv"]["food"] == f0 + (1 if moves else 0)
    assert deal_violations(sim.events, 0, me) == []


def test_the_thought_is_in_the_log_and_nowhere_in_the_world():
    """Ruling 4 (Steering 12:55Z), extended to the thought: whatever was thought, everything the listener is, knows
    and remembers is the same, and the whole state is the same as after a reply with no thought at all."""
    ends = {}
    for name, out in (("thought", {"thought": SECRET, "answer": "yes"}), ("none", {"answer": "yes"}),
                      ("other", {"thought": "No reason. " * 20, "answer": "yes"})):
        mind = Mind(decide_raw=json.dumps(out), gives="food")
        sim, me, to, ag = ready(mind, talk=thinks(2))
        typed(sim, to)
        for _ in range(6):
            sim.step([])
        ends[name] = json.dumps(sim.state, sort_keys=True)
        logged = [e for e in sim.events if e.kind == "mind_decide"]
        assert len(logged) == 1 and logged[0].mind["raw_output"] == json.dumps(out)
        assert "ZQTHOUGHT" not in ends[name] and "No reason." not in ends[name]
        assert all("ZQTHOUGHT" not in json.dumps(e.data) for e in sim.events)
        assert all("ZQTHOUGHT" not in (e.mind or {}).get("raw_output", "") for e in sim.events if e.kind != "mind_decide")
    assert ends["thought"] == ends["none"] == ends["other"]


class Thinker(Hostile):
    """The hostile mind of text-1's fuzz, whose every private decision carries a marked thought."""

    def __init__(self, seed):
        super().__init__(seed)
        self.reqs, self.n = [], 0

    def decide(self, req):
        self.reqs.append(req)
        if req.kind == "decide":
            self.n += 1
            out = {"thought": f"{SECRET} {self.n}", "answer": "yes" if self.n % 2 else "no"}
            return MindReply(json.dumps(out), self.model, {"temperature": 0}, "stop")
        return super().decide(req)


@pytest.mark.parametrize("seed", [1, 2])
@pytest.mark.parametrize("level", [1, 2])
def test_no_later_prompt_holds_a_thought_and_the_world_replays(seed, level):
    mind = Thinker(seed)
    sim, me, to = world(mind, thinks(level), seed=seed)
    r, check = random.Random(seed), Checker(sim)
    for _ in range(2 * TICKS_PER_DAY):  # past a night: reflection and the next day's prompts too
        ag = sim.state["agents"]
        here = [o["id"] for o in ag if o["id"] != me and o["place"] is not None and o["place"] == ag[me]["place"]
                and not o["asleep"]]
        acts = []
        if here and r.random() < 0.6:
            acts.append({"kind": "act", "action": "say", "target": r.choice(here),
                         "text": r.choice(INJECTIONS + ["Could you spare some food?"]),
                         **r.choice([{}, {}, {"move": "asks_food"}])})
        assert check.step(acts) == [], acts
    asked = [q for q in mind.reqs if q.kind == "decide"]
    assert len(asked) > 5, "vacuous: few decisions"
    assert all(sentence(level) in q.prompt and THINK_DECIDE_LINE in q.prompt for q in asked)
    for q in mind.reqs:
        assert "ZQTHOUGHT" not in q.prompt and all("ZQTHOUGHT" not in p for p in (q.told or {}).values())
        if q.kind != "decide":
            assert "thought" not in q.prompt and "Nobody hears it" not in q.prompt
    assert "ZQTHOUGHT" not in json.dumps(sim.state)
    logged = [e for e in sim.events if e.kind == "mind_decide"]
    assert len(logged) == len(asked) and all("ZQTHOUGHT" in e.mind["raw_output"] for e in logged)
    assert any(d.data["ok"] for d in deals(sim.events)), "vacuous: no give rested on a thinking decision"
    assert deal_violations(sim.events, 0, me) == []
    sim.finish()
    replay(sim.events)


@pytest.mark.parametrize("talk", [{"think": 1}, {"consent": "mind", "think": 1}, {"typed": 1, "think": 2},
                                  {"consent": "private", "think": 3}, {"consent": "private", "think": True},
                                  {"consent": "private", "think": "1"}])
def test_only_the_private_decision_thinks(talk):
    with pytest.raises(ValueError, match="think"):
        world(Mind(), talk)


def test_genesis_names_the_flag_only_when_it_is_set():
    assert talk_params("private", True) == {"consent": "private", "typed": 1, "hungry_at": 550}
    assert talk_params("private", True, 2) == {"consent": "private", "typed": 1, "hungry_at": 550, "think": 2}
    assert talk_params(None, False) is None


# The whole log of a held world with no `think`, as the tree before think-1 writes it: a day of hostile lines and
# minds, every event with its recorded prompt hash and output. Steering 2026-11-19 21:18Z. First pinned from da3e199
# on engine 35 (5429419d, 66d35277 typed; d4e31372, 37763f30 chip), which this branch matched. Re-pinned when #99
# (pace-1, engine 36) landed under it: these are da3e199 merged with main at 01eebda, without the think-1 commits.
BEFORE = {("typed", 1): "3983aa1196a9c34647509c761b5ff6a85fea44715eb02244cce2caa69db5227b",
          ("typed", 2): "22654ac7c0042c6ee453ff060af8a63e9fae9fc54b21fa37f7d398d4690dfe81",
          ("chip", 1): "389f034db169089263752aa64cfcb3b400ea51eb2cdc609652fe8196513a4db8",
          ("chip", 2): "c3b9ed261023d4e31f3d7f3b0c69de633aee39cf6d80868c235126f64c07242a"}


def day_log(talk, seed):
    import hashlib
    sim, me, to = world(Hostile(seed), dict(talk), seed=seed)
    r = random.Random(seed)
    for _ in range(TICKS_PER_DAY):
        ag = sim.state["agents"]
        here = [o["id"] for o in ag if o["id"] != me and o["place"] is not None and o["place"] == ag[me]["place"]
                and not o["asleep"]]
        acts = []
        if here and r.random() < 0.6:
            acts.append({"kind": "act", "action": "say", "target": r.choice(here),
                         "text": r.choice(INJECTIONS + ["Could you spare some food?"]),
                         **r.choice([{}, {}, {"move": "asks_food"}])})
        sim.step(acts)
    sim.finish()
    decided = sum(e.kind == "mind_decide" for e in sim.events)
    return hashlib.sha256("\n".join(e.to_json() for e in sim.events).encode()).hexdigest(), decided


@pytest.mark.parametrize("name,seed", sorted(BEFORE))
def test_a_world_without_the_flag_is_byte_identical_to_before_think_1(monkeypatch, name, seed):
    monkeypatch.setattr(S, "ENGINE_VERSION", "apart-1")  # the label the pins were written under; 37 since the rename
    # pace-2 moves when a held world's plans land, which these pins predate; this test is about the think flag, so it
    # runs with pace-2 off (the pins then still hold byte for byte). tests/test_pace2.py covers pace-2 itself.
    monkeypatch.setattr(S.Sim, "_pace2", lambda self: False)
    from test_goods import on_38
    on_38(monkeypatch)  # likewise goods-1: the pins are worlds on supply-1's trade stake
    talk = PRIVATE if name == "typed" else {"consent": "private"}
    log, decided = day_log(talk, seed)
    assert decided >= 2, "vacuous: no private decision in the day"
    assert log == BEFORE[(name, seed)]
    assert day_log({**talk, "think": 0}, seed)[0] != log  # the flag written out is a different genesis ...
    assert day_log({**talk, "think": 1}, seed)[0] != log  # ... and thinking is a different prompt
