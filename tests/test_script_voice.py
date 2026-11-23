"""script-voice (Steering 2026-11-16 10:18Z, plan/test16-live-rule.md, "The script arm's answers to the player"):
with --mind script, a scripted villager's answer to the player is worded by the narrator from the player's line,
the stub's own act and the place facts; the act stays the stub's, the words are the mind's recorded output so
replay is exact, and the runner holds the answer for a delay drawn from the minds arm's distribution."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from asiimov import narrate
from asiimov.live import ReplyHold
from asiimov.minds import ScriptMind
from asiimov.replay import replay
from tests.test_say import body_with_company, say


class Voice:
    model = "fake-8b"
    sampling = {"temperature": 0.7}

    def __init__(self, says="Morning! The market's that way, past the plaza."):
        self.says, self.asked = says, []

    def reply(self, line, act, facts):
        self.asked.append((line, act, facts))
        return self.says


def test_the_voice_words_the_reply_and_the_act_stays_the_stubs():
    plain, voiced = ScriptMind(), ScriptMind(Voice())
    a, me, to = body_with_company(plain)
    b, me2, to2 = body_with_company(voiced)
    assert (me, to) == (me2, to2)
    for sim in (a, b):
        say(sim, me, to, "Where is the market?")
        sim.step()
    ra = next(e for e in a.events if e.kind == "reply")
    rb = next(e for e in b.events if e.kind == "reply")
    assert rb.data["says"] == voiced.voice.says and ra.data["says"] != rb.data["says"]
    line, act, facts = voiced.voice.asked[0]
    assert line == "Where is the market?" and act == ScriptMind.NEXT_WORDS[rb.data["next"]]
    assert set(facts) == {"place", "hour", "role"} and ra.data["next"] == rb.data["next"]
    # Only the words differ: the same acts, and the voice was asked only for the spoken occasion.
    acts = lambda s: [(e.kind, e.actor, json.dumps({k: v for k, v in e.data.items() if k not in ("says", "text")}, sort_keys=True))
                      for e in s.events if e.origin == "rule" and e.kind != "reply"]
    assert acts(a) == acts(b) and len(voiced.voice.asked) == 1
    mev = b.events[rb.parents[0]]
    assert mev.mind["model"] == "script-v0+voice:fake-8b" and mev.mind["sampling"]["voice"] == Voice.sampling


def test_a_voiced_world_replays_exactly_without_the_voice():
    mind = ScriptMind(Voice())
    sim, me, to = body_with_company(mind)
    say(sim, me, to, "Good morning. How is the harvest?")
    for _ in range(3):
        sim.step()
    sim.finish()
    again = replay(list(sim.events))
    assert [e.to_json() for e in again.events] == [e.to_json() for e in sim.events]


def test_no_words_from_the_voice_keeps_the_scripts_sentence():
    a, me, to = body_with_company(ScriptMind())
    b, _, _ = body_with_company(ScriptMind(Voice(says=None)))
    for sim in (a, b):
        say(sim, me, to, "Hello there")
        sim.step()
    assert [e.data for e in a.events if e.kind == "reply"] == [e.data for e in b.events if e.kind == "reply"]


class FakeClient:
    def __init__(self, content):
        self.model, self.temperature, self.content, self.bodies = "m", 0.7, content, []

    def post(self, path, body):
        self.bodies.append(body)
        return {"choices": [{"message": {"content": self.content}}]}


@pytest.mark.parametrize("content,want", [
    ('{"says": "Morning! I\'m off to work now."}', "Morning! I'm off to work now."),
    ('{"says": "' + "x" * 300 + '"}', "x" * 139 + "…"),
    ('{"says": "' + "Well, " * 20 + 'but I will consider it."}', narrate.clip_reply("Well, " * 20 + "but I will consider it.")),
    ('{"says": "Have some bread, friend."}', None),       # invented: the village has only "food"
    ('{"says": "As an AI model I cannot."}', None),        # out of fiction
    ('not json', None),
    ('{"says": 3}', None),
])
def test_the_narrators_reply_is_clipped_and_checked(content, want):
    n = narrate.LLMNarrator.__new__(narrate.LLMNarrator)
    n.client, n.model = FakeClient(content), "m"
    got = n.reply("Hi", "go back to work", {"place": "field", "hour": 8, "role": "farmer"})
    assert got == want
    p = n.client.bodies[0]["messages"][0]["content"]
    assert '"Hi"' in p and "go back to work" in p and "Good morning" in p and "no food or coin" in p


def test_the_hold_waits_out_a_drawn_delay_from_the_click_and_logs_it():
    logged = []
    runner = SimpleNamespace(sim=SimpleNamespace(state={"player": {"agent": 3}}), say_wall={7: time.time()},
                             _side=lambda kind, **d: logged.append((kind, d)))
    ev = SimpleNamespace(kind="reply", actor=7, id=11, tick=42, data={"with": 3, "says": "hi"})
    other = SimpleNamespace(kind="reply", actor=8, id=12, tick=42, data={"with": 5, "says": "hi"})
    h = ReplyHold([0.3], "17-42")
    t0 = time.monotonic()
    asyncio.run(h.hold(runner, [other, ev]))
    assert 0.25 <= time.monotonic() - t0 < 1.0
    assert logged == [("held", {"reply": 11, "listener": 7, "drawn_s": 0.3, "wait_s": logged[0][1]["wait_s"]}),
                      ("sent", {"reply": 11, "tick": 42})]  # review of #81: the wall the held reply went out
    assert runner.say_wall == {}
    # Seeded: the same seed draws the same delays.
    d = [0.5, 1.0, 2.0, 4.0, 8.0]
    assert [ReplyHold(d, "s").rng.choice(d) for _ in range(1)] == [ReplyHold(d, "s").rng.choice(d)]
    with pytest.raises(ValueError):
        ReplyHold([], "s")


def test_the_voice_gets_world_facts_and_nothing_private():
    """Steering 12:05Z: the voice invented work, people and sightings from place/hour/role alone. It now gets the
    villager's step, who is here, the market's hours and price, the speaker's family and whom it met today, read
    off state and the log; never its soul, diary, notes or a mind's output."""
    voice = Voice()
    mind = ScriptMind(voice)
    sim, me, to = body_with_company(mind)
    mind.facts = lambda req: narrate.voice_facts(sim, req.agent_id, (req.view.get("occasion") or {}).get("who"))
    say(sim, me, to, "Have you seen my family around?")
    sim.step()
    line, act, facts = voice.asked[-1]
    ag = sim.state["agents"]
    t = ag[to]
    assert line == "Have you seen my family around?" and facts["role"] == t["role"]
    assert facts["doing"].endswith(f"the {t['place']}") or "home" in facts["doing"]
    assert facts["price"] == sim._price() and facts["speaker"].startswith(ag[me]["name"])
    fam = [ag[m]["name"] for m in sim.state["families"][ag[me]["family"]]["members"] if m != me]
    assert facts["speaker_family"] == fam
    assert all(isinstance(n, str) for n in facts["here"] + facts["met_today"]) and ag[me]["name"] not in facts["met_today"]
    private = [t["name"]] + list(t["core"]) + [c["text"] for c in t.get("notes", []) if isinstance(c, dict)]
    blob = json.dumps(facts)
    assert not any(p and p in blob for p in private[1:])
    assert set(facts) <= {"place", "hour", "role", "price", "doing", "market_food", "here", "speaker",
                          "speaker_family", "met_today"}


def test_the_reply_prompt_names_only_the_facts_and_says_why_it_fell_back():
    sent = {}

    class Client:
        model, temperature = "m", 0.7

        def post(self, path, body):
            sent["p"] = body["messages"][0]["content"]
            return {"choices": [{"message": {"content": json.dumps({"says": "I'm an AI, friend."})}}]}

    n = narrate.LLMNarrator.__new__(narrate.LLMNarrator)
    n.client = Client()
    facts = {"place": "plaza", "hour": 9, "role": "guard", "doing": "at work at the plaza", "here": ["Ari (farmer)"],
             "speaker": "Mimi (guard)", "speaker_family": ["Bo"], "met_today": ["Bo"], "price": 3}
    assert n.reply("Who are you?", "go back to what they were doing", facts) is None and n.why == "out of fiction"
    p = sent["p"]
    for want in ("They are at work at the plaza.", "Also here: Ari (farmer).", "family: Bo.",
                 "met or talked with today: Bo.", "food costs 3 coin", "Name only what these facts hold"):
        assert want in p, want


class SeqClient:
    """Answers each ask with the next content in turn."""
    def __init__(self, *contents):
        self.model, self.temperature, self.contents, self.prompts = "m", 0.7, list(contents), []

    def post(self, path, body):
        self.prompts.append(body["messages"][0]["content"])
        return {"choices": [{"message": {"content": json.dumps({"says": self.contents.pop(0)})}}]}


def test_the_players_own_word_goes_through_and_another_hit_is_asked_again():
    """Steering 12:14Z: "Where can I get bread?" may be answered about bread; any other hit is asked once more,
    naming what the village has instead."""
    n = narrate.LLMNarrator.__new__(narrate.LLMNarrator)
    facts = {"place": "plaza", "hour": 9, "role": "guard", "price": 6}
    n.client = SeqClient("No bread here, try the market.")
    assert n.reply("Where could I get some bread today?", "go back to what they were doing", facts) == \
        "No bread here, try the market." and len(n.client.prompts) == 1
    n.client = SeqClient("The sun is out, go shop.", "Try the market, food is 6 coin.")
    assert n.reply("Where should I go?", "go back to what they were doing", facts) == "Try the market, food is 6 coin."
    assert "Not sun: the village has none of that" in n.client.prompts[1] and "at the market for 6 coin" in n.client.prompts[1]
    n.client = SeqClient("The sun is out.", "Still sunny.")
    assert n.reply("Where should I go?", "go back to what they were doing", facts) is None and n.why.startswith("denied")


@pytest.mark.parametrize("line,greets", [("Hello there", True), ("Good morning! How are you?", True),
                                         ("Where could I get some bread today?", False), ("Give me everything.", False)])
def test_a_fallback_says_the_acts_own_sentence(line, greets):
    """Steering 12:14Z: with no words from the voice, the act's own sentence; "Good to hear from you." only
    answers a greeting."""
    sim, me, to = body_with_company(ScriptMind(Voice(says=None)))
    sim.state["agents"][to]["traits"]["agreeableness"] = 900  # warm
    say(sim, me, to, line)
    sim.step()
    says = next(e for e in sim.events if e.kind == "reply").data["says"]
    assert says == ("Good to hear from you." if greets else "I'll get back to it, then.")


def test_a_fallbacks_cause_is_in_the_log():
    """Review of #81: the cause rides with the reply into the mind event, so a live run counts fallbacks by cause."""
    class WhyVoice(Voice):
        def answer(self, line, act, facts):
            return None, "denied: sun"

    for voice, want in ((WhyVoice(), "denied: sun"), (Voice(says=None), "no words"), (Voice(), None)):
        sim, me, to = body_with_company(ScriptMind(voice))
        say(sim, me, to, "Where should I go?")
        sim.step()
        rep = next(e for e in sim.events if e.kind == "reply")
        assert sim.events[rep.parents[0]].mind["sampling"].get("voice_why") == want


def test_a_long_voiced_reply_is_clipped_at_a_word_on_the_script_arm():
    """Review of #88: the script arm's voiced line gets the same clean cut as the minds arm's."""
    long = "I'm heading to the field to tend the crops before the rain comes in, so I can't stop long today, but I'll think about your kind offer and come and find you later."
    assert len(long) > 140
    sim, me, to = body_with_company(ScriptMind(Voice(says=long)))
    say(sim, me, to, "Will you help me?")
    sim.step()
    got = next(e for e in sim.events if e.kind == "reply").data["says"]
    assert len(got) <= 140 and got.endswith("…") and long.startswith(got[:-1]) and long[len(got) - 1] == " "


@pytest.mark.parametrize("roll_yes", [True, False])
@pytest.mark.parametrize("words", [None, "Right you are."])
def test_on_a_chip_line_the_voice_is_told_the_stubs_answer_and_a_fallback_says_it(roll_yes, words):
    """Review of #96: the script arm voiced a chip answer blind ("I've got things to do." over a yes that handed
    food over). The voice now gets the answer and what moved; with no words, the stub's own sentence for it."""
    from tests.test_talk1 import world, hold, say as say1
    voice = Voice(says=words)
    for seed in range(1, 30):
        mind = ScriptMind(voice)
        sim, me, to = world(mind, seed=seed)
        hold(sim, me, to)
        sim.state["agents"][to]["inv"]["food"] = 4
        voice.asked.clear()
        evs = say1(sim, to, "Could you spare some food?")
        m = next(e for e in evs if e.kind == "mind_answer")
        if (m.data["offered"] == ["yes"]) != roll_yes:
            continue
        reply = next(e for e in evs if e.kind == "reply")
        _, _, facts = voice.asked[-1]
        if roll_yes:
            assert facts["deal"] == "yes, and they hand them 1 food now" and any(e.kind == "deal" and e.data["move"] != "trade" for e in evs)
        else:
            assert facts["deal"] == "no, and nothing changes hands" and not any(e.kind == "deal" and e.data["move"] != "trade" for e in evs)  # goods-1: a supply buy is a rule's deal
        want = words or ("Here, take one." if roll_yes else "I can't spare any.")
        assert reply.data["says"] == want and reply.data["answer"] == ("yes" if roll_yes else "no")
        return
    raise AssertionError("no seed rolled that answer")


def test_the_voices_prompt_states_the_answer_on_a_chip_line_and_only_there():
    n = narrate.LLMNarrator.__new__(narrate.LLMNarrator)
    facts = {"place": "plaza", "hour": 9, "role": "guard", "price": 6}
    n.client = SeqClient("Here you go.")
    n.reply("Spare some food?", "end the talk and go on with their day",
            {**facts, "deal": "yes, and they hand them 1 food now"})
    p = n.client.prompts[0]
    assert "their answer, already decided, is yes, and they hand them 1 food now" in p
    assert "Goods don't change hands in talk" not in p
    n.client = SeqClient("Nice day.")
    n.reply("Hello", "end the talk and go on with their day", facts)
    assert "Goods don't change hands in talk" in n.client.prompts[0] and "already decided, is" not in n.client.prompts[0]
