"""Words for what a player can see (readable-world E2, plan/readable-world-spec.md, Steering 2026-11-14).

Talk and snubs at the player's place, refusals of the player's asks and visits to the player get a line or
two of speech after the tick. They go to narration.jsonl and to the inhabitant as `heard`, and never into
the world: the log is the same as a world run with no narrator. The player's own villager never speaks."""
import asyncio
import json
import os

from asiimov import live, narrate
from asiimov.events import read_jsonl
from asiimov.minds import StubMind
from asiimov.sim import TICKS_PER_DAY, Sim
from test_inhabitant import batch_of, intent
from test_live import WS


class Ev:
    def __init__(self, id, kind, actor, data):
        self.id, self.kind, self.actor, self.data = id, kind, actor, data


def world_with_body(seed=4, n=20):
    sim = Sim(seed, n, StubMind())
    sim.run_until(TICKS_PER_DAY // 2)
    ag = sim.state["agents"]
    for b in ag:
        if b["asleep"] or b["place"] is None:
            continue
        others = [o["id"] for o in ag if o is not b and o["place"] == b["place"] and not o["asleep"]]
        if len(others) >= 2:
            return sim, b["id"], others
    raise AssertionError("no crowd")


def test_talk_here_gets_words_and_the_players_villager_never_speaks():
    sim, me, others = world_with_body()
    x, y = others[:2]
    evs = [Ev(1, "talk", x, {"with": y, "gossip": []}), Ev(2, "talk", x, {"with": me, "gossip": []}),
           Ev(3, "talk", me, {"with": y, "gossip": []}), Ev(4, "snub", x, {"with": me}),
           Ev(5, "snub", me, {"with": y})]
    its = narrate.items(sim, evs, me)
    assert [(i["kind"], i["speakers"], i["to_player"]) for i in its] == [
        ("talk", [x, y], False), ("talk", [x], True), ("talk", [y], True), ("snub", [x], True), ("snub", [y], True)]
    for it in its:
        assert me not in it["speakers"]
        for line in narrate.TemplateNarrator().lines(it):
            assert line["who"] in it["speakers"] and 0 < len(line["text"]) <= narrate.MAX_CHARS
        p = narrate.prompt(it)
        assert all(sim.state["agents"][s]["name"] in p for s in it["speakers"])
        assert "soul" not in p.lower() and "trait" not in p.lower() and "Lately" not in p


def test_talk_elsewhere_or_while_asleep_gets_no_words():
    sim, me, others = world_with_body()
    far = next(a["id"] for a in sim.state["agents"] if a["place"] not in (None, sim.state["agents"][me]["place"]))
    assert narrate.items(sim, [Ev(1, "talk", far, {"with": others[0], "gossip": []})], me) == []
    sim.state["agents"][me]["asleep"] = True
    assert narrate.items(sim, [Ev(1, "talk", others[0], {"with": others[1], "gossip": []})], me) == []


def test_a_refusal_of_the_players_ask_and_a_visit_get_the_other_villagers_line():
    sim, me, (x, *_) = world_with_body()
    evs = [Ev(1, "receipt", me, {"action": "ask_food", "ok": False, "why": "refused", "to": x}),
           Ev(2, "receipt", me, {"action": "ask_kin", "ok": False, "why": "kin has nothing to spare", "to": x}),
           Ev(3, "receipt", me, {"action": "ask_food", "ok": False, "why": "target asleep", "to": x}),
           Ev(4, "receipt", me, {"action": "ask_food", "ok": True, "why": "gave", "to": x}),
           Ev(5, "meet", x, {"with": me}), Ev(6, "meet", me, {"with": x})]
    its = narrate.items(sim, evs, me)
    assert [(i["event"], i["kind"], i["speakers"]) for i in its] == [(1, "refusal", [x]), (2, "refusal", [x]), (5, "visit", [x])]
    assert "nothing to spare" in narrate.prompt(its[1])


def test_the_model_narrator_keeps_good_lines_and_falls_back_on_anything_else():
    sim, me, others = world_with_body()
    x, y = others[:2]
    it = narrate.items(sim, [Ev(1, "talk", x, {"with": y, "gossip": []})], me)[0]
    nx, ny = sim.state["agents"][x]["name"], sim.state["agents"][y]["name"]
    n = narrate.LLMNarrator("http://127.0.0.1:9", "m")
    replies = [json.dumps({"lines": [{"who": nx, "text": "Morning."}, {"who": ny, "text": "Busy one, isn't it?"}]}),
               json.dumps({"lines": [{"who": "Somebody Else", "text": "Hi"}]}),  # not a speaker
               "not json", json.dumps({"lines": []})]
    for i, text in enumerate(replies):
        n.client.post = lambda path, body, t=text: {"choices": [{"message": {"content": t}}]}
        n.said = {}  # each reply as the day's first words; repeats have their own test
        got = n.lines(dict(it))
        if i == 0:
            assert got == [{"who": x, "text": "Morning."}, {"who": y, "text": "Busy one, isn't it?"}]
        else:
            assert got == narrate.TemplateNarrator().lines(it)

    def down(path, body):
        raise RuntimeError("LLM server unreachable")
    n.client.post = down
    n.said = {}
    got = dict(it)
    assert n.lines(got) == narrate.TemplateNarrator().lines(it) and got["fallback"] is True


def test_an_inhabitant_hears_words_and_the_world_log_is_unchanged(tmp_path):
    out = str(tmp_path / "w")
    runner = live.LiveRunner(out, StubMind(), 4, 20)
    for _ in range(TICKS_PER_DAY // 2 - 10):
        runner.step()
    aid = 3
    heard = []

    async def main():
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.ensure_future(live.serve(runner, port=0, tps=1000, paused=True, narrator=narrate.TemplateNarrator(),
                                                started=lambda port, hub: ready.set_result(port)))
        port = await ready
        c = await WS.connect(port)
        c.send({"type": "hello", "protocol": 3, "scope": "inhabitant", "agent": aid})
        await c.until("welcome")
        god = await WS.connect(port)
        god.send({"type": "hello", "protocol": 3})
        await god.until("welcome")
        god.send({"type": "control", "op": "run"})
        k = 0
        while True:
            m = await c.recv()
            if m["type"] == "heard":
                heard.append(m)
            if m["type"] == "tick":
                v = m["view"]
                k += 1
                if v["present"] and not v["asleep"] and k % 3 == 0:  # ask someone here for food now and then
                    c.send(intent(f"a{k}", "act", {"action": "ask_food", "target": v["present"][0]["id"]}, v["tick"]))
                if v["tick"] >= TICKS_PER_DAY + 30:
                    break
        god.send({"type": "control", "op": "pause"})
        await god.until("status")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    runner.finish()
    assert len(heard) >= 2 and {h["kind"] for h in heard} <= {"talk", "snub", "refusal", "visit"}
    for h in heard:
        assert h["lines"] and all(x["who"] != aid and x["name"] and x["text"] for x in h["lines"])
    side = [json.loads(l) for l in open(os.path.join(out, "narration.jsonl"))]
    assert len(side) >= len(heard) and all(r["narrator"] == "template-v0" and r["player"] == aid for r in side)
    assert [l.rstrip("\n") for l in open(os.path.join(out, "events.jsonl"))] == batch_of(out)
    assert not any("heard" in e.kind or "narrat" in e.kind for e in read_jsonl(os.path.join(out, "events.jsonl")))


def test_the_prompt_holds_nothing_the_player_could_not_see():
    """Review of #61: a speaker's memory (its notes, say of a theft elsewhere) and needs stay out of the prompt."""
    sim, me, (x, y, *_) = world_with_body()
    sim._note(x, "stole food from Somebody Elsewhere")
    sim.state["agents"][x]["needs"]["hunger"] = 600
    sim.state["agents"][y]["needs"]["hunger"] = 900
    its = narrate.items(sim, [Ev(1, "talk", x, {"with": y, "gossip": []}), Ev(2, "meet", x, {"with": me})], me)
    for it in its:
        p = narrate.prompt(it)
        assert "stole" not in p and "Somebody Elsewhere" not in p and "hungry" not in p
    assert "looks starving" in narrate.prompt(its[0])
    for facts in its[0]["people"].values():
        assert set(facts) == {"name", "role", "looks", "doing", "mood"}


def test_the_prompt_names_the_hour_the_season_and_what_each_speaker_is_doing():
    """E2b (Steering 10:09Z): the 8B dry run's lines invented rain, corn and prices and said "Mornin'" at noon,
    because the prompt gave only "a small farming village". It now gives the hour, the part of day, the season,
    what each speaker is visibly doing, and says to speak only of these facts."""
    sim, me, (x, y, *_) = world_with_body()
    it = narrate.items(sim, [Ev(1, "talk", x, {"with": y, "gossip": []})], me)[0]
    p = narrate.prompt(it)
    h = (sim.state["tick"] - 1) % TICKS_PER_DAY * 10 // 60
    assert f"Time: {h:02d}:00, {narrate.part_of_day(h)}" in p
    assert it["season"] in ("winter", "the growing season", "harvest day, the last before winter") and it["season"] in p
    for s in (x, y):
        a = sim.state["agents"][s]
        assert f"{a['name']}, {a['role']}, {narrate.DOING[a['activity']]}" in p
    assert "Speak only of these facts and the people here" in p
    assert [narrate.part_of_day(h) for h in (3, 6, 10, 14, 19, 22)] == \
        ["night", "early morning", "morning", "afternoon", "evening", "night"]


def test_the_season_follows_the_year():
    sim, _, _ = world_with_body()
    y = sim.state["stakes"]["year"]["yields"]
    names = [narrate.season(sim, d) for d in range(len(y))]
    for d, n in enumerate(names):
        nxt = y[(d + 1) % len(y)]
        assert n == ("winter" if y[d] == 0 else "harvest day, the last before winter" if nxt == 0 else "the growing season")
    assert "winter" in names and names.count("harvest day, the last before winter") == 1


def test_the_prompt_says_what_the_place_holds_and_the_greeting_for_the_hour():
    """E2c (Steering 13:07Z): dry run 2's lines named beans, stalls and a cow, and said "Evenin'" at 14:00. The
    prompt now says outright what each place holds and gives the greeting word for the hour."""
    sim, me, (x, y, *_) = world_with_body()
    it = narrate.items(sim, [Ev(1, "talk", x, {"with": y, "gossip": []})], me)[0]
    for place, holds in list(narrate.PLACE_HOLDS.items()) + [("home_3", narrate.HOME_HOLDS)]:
        for h, word in ((7, "Good morning"), (14, "Good afternoon"), (19, "Good evening"), (2, "Good evening")):
            p = narrate.prompt(dict(it, place=place, hour=h))
            assert holds in p and narrate.VILLAGE in p and f'A greeting now is "{word}"' in p
            assert "home_3" not in p
    assert 'just "food"' in narrate.PLACE_HOLDS["market"] and "no stalls" in narrate.PLACE_HOLDS["market"]


def test_the_deny_list_catches_kinds_of_food_stalls_animals_weather_and_wrong_greetings():
    assert narrate.invented("Got any beans at the east stall?", 10) == ["beans", "stall"]
    assert narrate.invented("Saw fox tracks by your cow. Looks like rain.", 10) == ["fox", "cow", "rain"]
    assert narrate.invented("Evenin', Mara.", 14) == ["evening greeting"]
    assert narrate.invented("Mornin'. Good morning to you.", 14) == ["morning greeting"]
    assert narrate.invented("Good afternoon. Bought some food?", 14) == []
    assert narrate.invented("Good evening.", 23) == [] and narrate.invented("Mornin'!", 7) == []
    assert narrate.invented("I need tools from Oren, and the plaza's busy.", 10) == []
    # Review of #70: a plain time word is a true topic, not a greeting.
    assert narrate.invented("I got tools from Rolu this morning.", 15) == []
    assert narrate.invented("See you this evening at the plaza.", 10) == []
    assert narrate.invented("I will rest this afternoon.", 9) == []
    assert narrate.invented("Well. Evening, Rolu!", 10) == ["evening greeting"]
    assert narrate.invented("Hi. Mornin' to you.", 15) == ["morning greeting"]
    assert narrate.invented("Evening comes soon.", 14) == [] and narrate.invented("Evening.", 14) == ["evening greeting"]
    assert narrate.invented("Afternoon's been slow.", 9) == [] and narrate.invented("Good mornin'!", 15) == ["morning greeting"]


def test_a_deny_list_hit_is_asked_once_more_then_falls_back():
    sim, me, (x, y, *_) = world_with_body()
    it = narrate.items(sim, [Ev(1, "talk", x, {"with": y, "gossip": []})], me)[0]
    it["hour"] = 14
    nx, ny = sim.state["agents"][x]["name"], sim.state["agents"][y]["name"]
    bad = json.dumps({"lines": [{"who": nx, "text": "Fresh beans today."}, {"who": ny, "text": "Busy day."}]})
    good = json.dumps({"lines": [{"who": nx, "text": "Good afternoon."}, {"who": ny, "text": "Busy day, that's all."}]})
    n = narrate.LLMNarrator("http://127.0.0.1:9", "m")
    for replies, fallback in (([bad, good], False), ([bad, bad], True), ([good], False)):
        asked = []
        q = list(replies)
        n.client.post = lambda path, body: (asked.append(body["messages"][0]["content"]),
                                            {"choices": [{"message": {"content": q.pop(0)}}]})[1]
        got = dict(it)
        n.said = {}
        lines = n.lines(got)
        assert got["fallback"] is fallback and len(asked) == len(replies)
        assert lines == (narrate.TemplateNarrator().lines(it) if fallback else
                         [{"who": x, "text": "Good afternoon."}, {"who": y, "text": "Busy day, that's all."}])
        if len(asked) == 2:
            assert "Not beans" in asked[1] and asked[1].startswith(asked[0])
            assert got["denied"][0] == ["beans"]


def test_topics_come_from_the_speakers_own_events_and_favour_what_stands_out():
    """E2c: topics come from the speakers' own real events of the last day, never the player's or a bystander's.
    E2d (Steering 13:44Z): one per speaker; what touches the other speaker or the listener first, then what
    stands out (a refusal, a gift, sickness), then a purchase, then routine trade, at most one routine trade
    topic per exchange; a speaker just turned down sounds it."""
    sim, me, (x, y, *_) = world_with_body()
    ag = sim.state["agents"]
    other = next(a["id"] for a in ag if a["id"] not in (me, x, y) and a["family"] != ag[x]["family"])
    sup = next(a["id"] for a in ag if a["role"] == "crafter" and a["id"] not in (me, x, y, other))
    t = sim.state["tick"]
    nx, ny, no, ns = (ag[i]["name"] for i in (x, y, other, sup))
    talk = [Ev(9, "talk", x, {"with": y, "gossip": []})]

    class E(Ev):
        def __init__(self, id, kind, actor, data, tick=t - 1):
            super().__init__(id, kind, actor, data)
            self.tick = tick

    r = narrate.Recent()
    r.feed(sim, [E(1, "receipt", x, {"action": "buy_food", "ok": True, "why": "bought"}, t - 200),  # over a day ago
                 E(2, "receipt", x, {"action": "buy_from", "ok": True, "why": "sold", "to": other}, t - 30),
                 E(3, "receipt", x, {"action": "ask_food", "ok": False, "why": "refused", "to": other}, t - 20),
                 E(4, "receipt", y, {"action": "trade", "ok": True, "why": "supplied", "to": sup, "coin": 1}),
                 E(5, "receipt", y, {"action": "give_food", "ok": True, "why": "gave", "to": other}, t - 40),
                 E(6, "receipt", me, {"action": "buy_food", "ok": True, "why": "bought"}),
                 E(7, "receipt", other, {"action": "give_coin", "ok": True, "why": "gave", "to": x})])
    assert r.candidates(sim, x) == [(narrate.STANDOUT, f"{nx} asked {no} for food and was turned down"),
                                    (narrate.BUY, f"{nx} bought food from {no}")]
    # A newer routine trade loses to an older gift.
    assert r.topics(sim, y, k=1) == [f"{ny} gave food to {no}"]
    # pt8 (joon: "words should be directed to a person"): news that touches neither the other speaker nor the
    # listener is not offered at all, so nobody opens with it to no one in particular.
    it = narrate.items(sim, talk, me, r)[0]
    assert it["topics"] == []
    p = narrate.prompt(it)
    assert "might bring up" not in p and "bought food" not in p
    assert it["people"][str(x)]["mood"] == "" and "color how they speak" not in p  # turned down 20 ticks ago
    # What touches the listener or the other speaker is offered.
    r.feed(sim, [E(8, "receipt", y, {"action": "give_coin", "ok": True, "why": "gave", "to": me}, t - 50)])
    assert narrate.items(sim, talk, me, r)[0]["topics"] == [{"who": y, "text": f"{ny} gave coin to {ag[me]['name']}"}]
    assert f"Something true {ny} might bring up: {ny} gave coin to {ag[me]['name']}." in narrate.prompt(
        narrate.items(sim, talk, me, r)[0])
    # Sickness in the family stands out; a bystander's doesn't count.
    kin = [m for m in sim.state["families"][ag[x]["family"]]["members"] if m != x][0]
    sim.state["sick"]["ill"] = {str(kin): t + 99, str(other): t + 99}
    assert r.candidates(sim, x)[0] == (narrate.STANDOUT, f"{ag[kin]['name']} in {nx}'s family is sick")
    # Steering 11:05Z: family sickness bears on any neighbour, so it is offered though it touches neither listener.
    assert {"who": x, "text": f"{ag[kin]['name']} in {nx}'s family is sick"} in narrate.items(sim, talk, me, r)[0]["topics"]
    # Just turned down: the tone may show it.
    r.feed(sim, [E(10, "receipt", x, {"action": "ask_kin", "ok": False, "why": "kin refused", "to": kin})])
    it = narrate.items(sim, talk, me, r)[0]
    assert it["people"][str(x)]["mood"] == "just turned down for food" and "color how they speak" in narrate.prompt(it)
    assert narrate.items(sim, talk, me)[0]["topics"] == []


def test_an_exchange_gets_at_most_one_routine_trade_topic():
    sim, me, (x, y, *_) = world_with_body()
    ag = sim.state["agents"]
    sup = next(a["id"] for a in ag if a["role"] == "crafter" and a["id"] not in (me, x, y))
    t = sim.state["tick"]
    r = narrate.Recent()
    evs = [Ev(1, "receipt", x, {"action": "trade", "ok": True, "why": "supplied", "to": sup}),
           Ev(2, "receipt", y, {"action": "trade", "ok": True, "why": "supplied", "to": sup})]
    for e in evs:
        e.tick = t - 1
    r.feed(sim, evs)
    it = narrate.items(sim, [Ev(9, "talk", x, {"with": y, "gossip": []})], me, r)[0]
    assert it["topics"] == []  # pt8: a trade with a bystander bears on neither listener ("I got tools from...")
    e3 = Ev(3, "receipt", x, {"action": "trade", "ok": True, "why": "supplied", "to": y})
    e3.tick = t - 1
    r.feed(sim, [e3])
    it = narrate.items(sim, [Ev(9, "talk", x, {"with": y, "gossip": []})], me, r)[0]
    assert [tp["who"] for tp in it["topics"]] == [x]  # one with the other speaker does


def test_only_the_opener_greets():
    """E2d: the reply answers what was said and doesn't greet; a second greeting is a hit, asked once more."""
    sim, me, (x, y, *_) = world_with_body()
    it = dict(narrate.items(sim, [Ev(1, "talk", x, {"with": y, "gossip": []})], me)[0], hour=14)
    two = lambda a, b: [{"who": x, "text": a}, {"who": y, "text": b}]
    assert narrate.check(it, two("Good afternoon, Ari.", "Good afternoon to you.")) == ["second greeting"]
    assert narrate.check(it, two("Good afternoon.", "Hello there.")) == ["second greeting"]
    assert narrate.check(it, two("Good afternoon. Any luck?", "Afternoon's been slow, honestly.")) == []
    assert narrate.check(it, two("Good afternoon. Any luck?", "None. Hit a wall, I did.")) == []
    # Review of #71: a bare part-of-day word greets only before ",", "!", "." or the end.
    assert narrate.check(it, two("Good afternoon.", "Afternoon is long today.")) == []
    assert narrate.check(it, two("Good afternoon.", "Evening comes soon.")) == []
    assert narrate.check(it, two("Good afternoon.", "Afternoon!")) == ["second greeting"]
    assert narrate.check(it, two("Good afternoon.", "Afternoon, Ari. Busy?")) == ["second greeting"]
    assert narrate.check(it, two("Good afternoon.", "Afternoon to you.")) == ["second greeting"]
    assert narrate.check(it, [{"who": x, "text": "Hello!"}]) == []
    p = narrate.prompt(it)
    nx, ny = (sim.state["agents"][i]["name"] for i in (x, y))
    assert f"Only {nx} may greet. {ny} doesn't greet: {ny} answers what {nx} just said" in p
    assert 'Only the first line may greet, with "Good afternoon"' in p


def test_every_line_is_said_to_someone_or_is_thinking_aloud():
    """pt8 (joon: "words / dialogues should be directed to a person, unless it is a form of thinking out loud"):
    the model names whom each line is to, or "self"; a line naming nobody valid gets the item's own addressee."""
    it = {"speakers": [1, 2], "player": 6, "kind": "talk", "to_player": False, "place": "plaza", "hour": 9,
          "opener": 1, "people": {"1": {"name": "Ari", "role": "farmer", "looks": "", "doing": "", "mood": ""},
                                  "2": {"name": "Bo", "role": "guard", "looks": "", "doing": "", "mood": ""},
                                  "6": {"name": "Mimi", "role": "guard", "looks": "", "doing": "", "mood": ""}}}
    assert '"to": NAME or "self"' in narrate.prompt(it) and "thinking aloud" in narrate.prompt(it)
    got = narrate.clean(it, {"lines": [{"who": "Ari", "to": "Mimi", "text": "Morning, Mimi."},
                                       {"who": "Bo", "to": "self", "text": "Long day ahead."}]})
    assert [x.get("to") for x in got] == [6, "self"]
    got = narrate.clean(it, {"lines": [{"who": "Ari", "to": "Nobody", "text": "Morning."},
                                       {"who": "Bo", "to": "Bo", "text": "Morning."}]})
    assert [x["to"] for x in narrate.addressed(it, got)] == [2, 1]  # in a chat, each to the other
    one = {**it, "speakers": [1], "to_player": True}
    assert narrate.addressed(one, [{"who": 1, "text": "Hello."}]) == [{"who": 1, "text": "Hello.", "to": 6}]
    # Steering 11:49Z: a "self" line that speaks to "you" is said to the other speaker (here, the player)
    assert narrate.addressed(one, [{"who": 1, "text": "I asked for food, but you said no.", "to": "self"},
                                   {"who": 1, "text": "Your turn, I suppose.", "to": "self"},
                                   {"who": 1, "text": "Hm. Youth these days.", "to": "self"}]) == [
        {"who": 1, "text": "I asked for food, but you said no.", "to": 6},
        {"who": 1, "text": "Your turn, I suppose.", "to": 6},
        {"who": 1, "text": "Hm. Youth these days.", "to": "self"}]
    assert [x["to"] for x in narrate.addressed(it, [{"who": 1, "text": "You again?", "to": "self"},
                                                    {"who": 2, "text": "Me again.", "to": "self"}])] == [2, "self"]


def test_a_line_heard_earlier_that_day_is_asked_once_more_then_the_exchange_is_dropped():
    """Steering 2026-11-18 23:52Z (pt11: 61 of 130 lines repeated word for word, mostly a bare "Good morning")."""
    sim, me, (x, y, *_) = world_with_body()
    it = narrate.items(sim, [Ev(1, "talk", x, {"with": y, "gossip": []})], me)[0]
    it["hour"], it["tick"] = 8, 3 * TICKS_PER_DAY + 50
    nx, ny = sim.state["agents"][x]["name"], sim.state["agents"][y]["name"]
    first = json.dumps({"lines": [{"who": nx, "text": "Good morning."}, {"who": ny, "text": "Busy day."}]})
    same = json.dumps({"lines": [{"who": nx, "text": "good morning!"}, {"who": ny, "text": "Quiet here."}]})
    new = json.dumps({"lines": [{"who": nx, "text": "Good morning, the well is low."}, {"who": ny, "text": "It is."}]})
    n = narrate.LLMNarrator("http://127.0.0.1:9", "m")

    def run(replies, tick=None):
        asked, q = [], list(replies)
        n.client.post = lambda path, body: (asked.append(body["messages"][0]["content"]),
                                            {"choices": [{"message": {"content": q.pop(0)}}]})[1]
        got = dict(it, tick=tick or it["tick"])
        return n.lines(got), got, asked

    lines, got, asked = run([first])
    assert len(lines) == 2 and got["repeated"] == [] and got["dropped"] is False
    lines, got, asked = run([same, new])  # case and punctuation don't make it new
    assert [l["text"] for l in lines] == ["Good morning, the well is low.", "It is."] and len(asked) == 2
    assert '"good morning!" again' in asked[1] and asked[1].startswith(asked[0]) and got["repeated"] == [["good morning!"]]
    lines, got, asked = run([same, first])  # a second repeat: no words at all
    assert lines == [] and got["dropped"] is True and len(asked) == 2
    reply_again = json.dumps({"lines": [{"who": nx, "text": "Did the well run dry?"}, {"who": ny, "text": "It is."}]})
    lines, got, asked = run([reply_again, reply_again])  # Steering 00:06Z: the opener stands without its reply
    assert [l["text"] for l in lines] == ["Did the well run dry?"] and got["dropped"] is False
    lines, got, asked = run([json.dumps({"lines": [{"who": nx, "text": "Long day ahead."}, {"who": ny, "text": "Busy day."}]}),
                             "not json"])  # a failed re-ask: the first answer, cut the same way
    assert [l["text"] for l in lines] == ["Long day ahead."]
    assert narrate.cut([{"text": "a"}, {"text": "b"}, {"text": "c"}], {"b"}) == [{"text": "a"}]
    assert narrate.cut([{"text": "Hi."}, {"text": "New."}], {"hi"}) == []  # a reply never stands alone
    lines, got, asked = run([same], tick=4 * TICKS_PER_DAY + 50)  # a new day: heard afresh
    assert len(lines) == 2 and got["dropped"] is False and len(asked) == 1
    assert list(n.said) == [(me, 4)]  # yesterday's lines are let go
    assert narrate.repeats([{"text": "Hi."}, {"text": "hi"}], set()) == ["hi"]  # within one exchange too

    def down(path, body):
        down.calls += 1
        raise RuntimeError("LLM server unreachable")
    down.calls, n.client.post = 0, down
    got = dict(it, tick=4 * TICKS_PER_DAY + 60)
    assert n.lines(got) == narrate.TemplateNarrator().lines(it) and down.calls == 1  # the template, heard afresh
    got = dict(it, tick=4 * TICKS_PER_DAY + 70)
    assert n.lines(got) == [] and down.calls == 2 and got["dropped"] is True  # repeated: no call to a down server
