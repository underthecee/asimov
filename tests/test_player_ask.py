"""An ask aimed at the player's body waits for the player's answer (player-ask-1, Steering 2026-11-10).

ask_kin, ask_food and buy_from aimed at the possessed body, while it isn't idle, are logged as an `ask` the
body witnesses. The player answers with `agree` or `decline` (target: the asker) within ASK_WAIT_TICKS. If
it doesn't, or acts otherwise first, or lets go, the body's own nature answers with the usual roll. The
asker's step waits, and its receipt cites the ask and the answer. A world with no player is unchanged."""
import hashlib
import json

from asiimov import inhabitant
from asiimov.events import Event
from asiimov.minds import MindReply, StubMind
from asiimov.replay import replay
from asiimov.sim import ASK_WAIT_TICKS, TICKS_PER_DAY, Sim

GOLDEN_22 = "f8e02d5476a6d4a53eebf1695f43dc57aee0935ac40bb99e744f8e80ee553b78"  # tests/test_world.py, engine 22


def test_a_world_with_no_player_is_engine_22_but_for_the_version_field():
    from test_world import FIRE
    from asiimov.params import generate_population
    from asiimov.sim import DEFAULT_STAKES
    # engine 22's stakes: the neighbor channel (engine 25) and trade-1 came after and change the log
    stakes = {k: v for k, v in DEFAULT_STAKES.items() if k not in ("neighbor", "trade")}
    s = Sim(3, 20, StubMind(), genesis={**generate_population(3, 20), "stakes": stakes})
    s.run(2, FIRE)
    s.finish()
    lines = [e.to_json() for e in s.events]
    g = json.loads(lines[0])
    g["data"]["engine"] = "22"
    lines[0] = json.dumps(g, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256("".join(l + "\n" for l in lines).encode()).hexdigest() == GOLDEN_22


def scene(action="ask_food"):
    """A world at a tick where villager A and the possessed body B are together, awake and fed, with A's
    intent about to ask B. Returns (sim, A, B, the player acts so far by tick)."""
    for seed in range(40):
        sim = Sim(seed, 14, StubMind())
        sim.run_until(TICKS_PER_DAY // 2)
        ag = sim.state["agents"]
        for b in ag:
            if b["asleep"] or b["place"] is None:
                continue
            fam = sim.state["families"][b["family"]]["members"]
            for a in ag:
                if a is b or a["asleep"] or a["place"] != b["place"] or (action == "ask_kin") != (a["id"] in fam):
                    continue
                player = {}
                sim.step([{"kind": "possess", "agent": b["id"]}])
                player[sim.state["tick"] - 1] = [{"kind": "possess", "agent": b["id"]}]
                if not (sim._together(a, b) and not a["asleep"] and not b["asleep"]):
                    break
                b["inv"]["food"], a["inv"]["coin"] = 5, 50
                pos = next(e.id for e in sim.events if e.kind == "possess")
                a["override"] = None
                a["trade_try"] = {n: sim.state["tick"] // TICKS_PER_DAY for n in sim._trade_needs(a)}  # no supply run
                a["intent"] = {"goal": "get food", "steps": [{"do": action, "target": b["id"]}], "i": 0,
                               "event": pos, "tick": sim.state["tick"], "left": 6}
                return sim, a["id"], b["id"], player
    raise AssertionError("no scene")


def run_with(sim, player, acts_by_offset):
    t0 = sim.state["tick"]
    for k in range(ASK_WAIT_TICKS + 3):
        acts = acts_by_offset.get(k, [])
        if acts:
            player[sim.state["tick"]] = acts
        sim.step(acts)
    return t0


def receipt_for(sim, asker, ask_id):
    return next(e for e in sim.events if e.kind == "receipt" and e.actor == asker and ask_id in e.parents)


def test_the_player_says_yes_and_the_asker_gets_food_citing_the_answer():
    sim, a, b, player = scene()
    sim.step([])  # A's step asks; the ask waits
    ask = next(e for e in sim.events if e.kind == "ask" and e.actor == a)
    assert ask.data == {"action": "ask_food", "to": b}
    assert not any(e.kind == "receipt" and e.actor == a and e.tick == ask.tick for e in sim.events)
    assert sim.state["player"]["asks"][0]["asker"] == a
    sim.step([])  # still waiting: nobody rolled for the body
    assert not any(e.kind == "receipt" and ask.id in e.parents for e in sim.events)
    act = [{"kind": "act", "action": "agree", "target": a}]
    player[sim.state["tick"]] = act
    sim.step(act)
    r = receipt_for(sim, a, ask.id)
    mine = next(e for e in sim.events if e.kind == "receipt" and e.actor == b and e.data["action"] == "agree")
    assert r.data["ok"] and r.data["why"] == "gave" and r.data["answered"] == 1 and "roll" not in r.data
    assert mine.id in r.parents and mine.data["why"] == "said yes"
    na, nb = sim.state["agents"][a]["name"], sim.state["agents"][b]["name"]
    assert any(f"gave 1 food to {na}, who asked" in n for n in sim.state["agents"][b]["recent"])
    assert any(f"{nb} gave me 1 food when I asked" in n for n in sim.state["agents"][a]["recent"])
    assert "asks" not in sim.state["player"]
    assert any(e.kind == "intent_done" and e.actor == a for e in sim.events)  # the waiting step ended


def test_the_player_says_no_and_the_asker_is_refused():
    sim, a, b, player = scene("ask_kin")
    sim.step([])
    ask = next(e for e in sim.events if e.kind == "ask" and e.actor == a)
    sim.step([{"kind": "act", "action": "decline", "target": a}])
    r = receipt_for(sim, a, ask.id)
    assert not r.data["ok"] and r.data["why"] == "kin refused" and r.origin == "rule"
    assert any(e.kind == "intent_failed" and e.actor == a for e in sim.events)  # its one-step intent failed, as ever


def test_unanswered_the_bodys_own_nature_answers_after_an_hour_and_the_asker_never_stalls():
    sim, a, b, player = scene()
    sim.step([])
    ask = next(e for e in sim.events if e.kind == "ask" and e.actor == a)
    for _ in range(ASK_WAIT_TICKS - 1):
        sim.step([])
        assert not any(e.kind == "receipt" and ask.id in e.parents for e in sim.events)
    sim.step([])
    r = receipt_for(sim, a, ask.id)
    assert r.tick == ask.tick + ASK_WAIT_TICKS and r.origin == "chance" and "roll" in r.data


def test_acting_otherwise_declines_and_letting_go_lets_the_bodys_nature_answer_at_once():
    """talk-1 (design §2.4): an active player who moves on without answering has said no; its nature doesn't
    give its goods away for it. Released, the body's own nature answers, as before."""
    for acts, origin in (([{"kind": "act", "action": "rest"}], "rule"), ([{"kind": "release"}], "chance")):
        sim, a, b, player = scene("buy_from")
        sim.step([])
        ask = next(e for e in sim.events if e.kind == "ask" and e.actor == a)
        t = sim.state["tick"]
        sim.step(acts)
        r = receipt_for(sim, a, ask.id)
        assert r.tick == t and r.origin == origin, acts
        if origin == "rule":
            assert not r.data["ok"] and r.data["why"] == "refused" and r.data["answered"] == 1


def test_an_answer_with_no_ask_is_refused():
    sim, a, b, player = scene()
    other = next(o["id"] for o in sim.state["agents"] if o["id"] not in (a, b))
    sim.step([{"kind": "act", "action": "agree", "target": other}])
    r = next(e for e in sim.events if e.kind == "receipt" and e.actor == b and e.data["action"] == "agree")
    assert not r.data["ok"] and r.data["why"] == "no ask"


class AsksTheBody(StubMind):
    """Stub everywhere, but a deliberation that may ask villager 5 for food (or as kin) does."""

    def decide(self, req):
        v = req.view
        people = [p if isinstance(p, int) else p.get("id") for p in v.get("people") or []]
        if req.kind == "deliberate" and 5 in people:
            for action in ("ask_kin", "ask_food", "buy_from"):
                if action in v.get("offer", []):
                    return MindReply(json.dumps({"action": action, "target": 5}), self.model, {"temperature": 0}, "stop")
        return super().decide(req)


def test_asks_answered_by_a_player_replay_exactly():
    """Minds ask the body during their deliberations; the player answers each on the next tick, yes then
    no. Every ask gets exactly one receipt, which cites the player's answer, and the log replays exactly."""
    sim, k = Sim(8, 14, AsksTheBody()), 0
    for t in range(3 * TICKS_PER_DAY):
        acts = [{"kind": "possess", "agent": 5}] if t == 10 else []
        asks = sim.state.get("player", {}).get("asks", [])
        if asks:
            k += 1
            v = inhabitant.view(sim, 5)  # what the player sees: the ask, its deadline, and both answers offered
            assert [{k: x[k] for k in ("from", "name", "action", "until")} for x in v["asks"]] == [{"from": q["asker"], "name": sim.state["agents"][q["asker"]]["name"],
                                  "action": q["action"], "until": q["until"]} for q in asks]
            if not v["asleep"]:
                assert v["offer"]["agree"] == v["offer"]["decline"] == [q["asker"] for q in asks]
            acts.append({"kind": "act", "action": "agree" if k % 2 else "decline", "target": asks[0]["asker"]})
        elif "player" in sim.state and t % 3 == 0:
            acts.append({"kind": "act", "action": "wait"})
        sim.step(acts)
    sim.finish()
    asks = [e for e in sim.events if e.kind == "ask"]
    answers = {e.id for e in sim.events if e.kind == "receipt" and e.actor == 5 and e.data["action"] in ("agree", "decline")}
    assert len(asks) >= 3
    for q in asks:
        rs = [e for e in sim.events if e.kind == "receipt" and q.id in e.parents]
        assert len(rs) == 1 and rs[0].actor == q.actor and set(rs[0].parents) & answers, (q, rs)
    events = [Event(**json.loads(e.to_json())) for e in sim.events]
    assert replay(events).state_hash() == sim.state_hash()


def test_an_idle_body_answers_by_its_nature_at_once():
    from asiimov.sim import IDLE_TICKS
    sim, a, b, player = scene()
    holder, sim.state["agents"][a]["intent"] = sim.state["agents"][a]["intent"], None  # ask once the body is idle
    for _ in range(IDLE_TICKS):
        sim.step([])
    A, B = sim.state["agents"][a], sim.state["agents"][b]
    B["inv"]["food"], A["place"], A["route"] = 5, B["place"], None  # together again, wherever the hour took them
    assert not A["asleep"] and not B["asleep"] and sim._idle(B)
    sim.state["agents"][a]["intent"] = {**holder, "tick": sim.state["tick"]}
    sim.step([])
    assert not any(e.kind == "ask" for e in sim.events)
    assert any(e.kind == "receipt" and e.actor == a and e.data["action"] == "ask_food" and e.origin == "chance"
               for e in sim.events)


class AsksThenBuys(StubMind):
    """A deliberation that may ask villager 5 for food asks, then means to buy from it (review on #53)."""

    def decide(self, req):
        v = req.view
        people = [p if isinstance(p, int) else p.get("id") for p in v.get("people") or []]
        if req.kind == "deliberate" and 5 in people and "ask_food" in v.get("offer", []):
            return MindReply(json.dumps({"action": "ask_food", "target": 5, "goal": "get food",
                                         "then": [{"do": "buy_from", "target": 5}]}), self.model, {"temperature": 0}, "stop")
        return super().decide(req)


def test_a_step_that_asks_the_body_again_waits_for_the_first_ask_rather_than_failing():
    """The mind's act asks the body and the intent's first step asks it again the same tick. The step waits
    for the first ask's answer and then asks; it never fails as "already asked"."""
    sim = Sim(8, 14, AsksThenBuys())
    for t in range(2 * TICKS_PER_DAY):
        acts = [{"kind": "possess", "agent": 5}] if t == 10 else []
        if "player" in sim.state and t % 3 == 0:
            acts.append({"kind": "act", "action": "wait"})
        sim.step(acts)
    asks = [e for e in sim.events if e.kind == "ask"]
    assert any(q.data["action"] == "buy_from" for q in asks), "no step ever asked after its act's ask"
    assert not any(e.kind == "receipt" and e.data.get("why") == "already asked" for e in sim.events)
    pairs = [(p, q) for p in asks for q in asks if p.data["action"] == "ask_food" and q.data["action"] == "buy_from"
             and sim.events[q.parents[0]].parents[0] == p.parents[0]]  # the same reply's act, then its step
    assert pairs
    for p, q in pairs:
        settled = next(e for e in sim.events if e.kind == "receipt" and p.id in e.parents)
        assert settled.id < q.id  # the step asked only once the act's ask was answered


def test_the_view_never_offers_an_answer_on_the_tick_the_ask_times_out():
    """At view tick == until, step() settles the ask by the body's nature before any act applies, so the view
    no longer lists it (review on #53); one tick earlier it is listed and an answer lands."""
    sim, a, b, player = scene()
    sim.step([])
    q = sim.state["player"]["asks"][0]
    while sim.state["tick"] < q["until"] - 1:
        sim.step([])
    v = inhabitant.view(sim, b)
    assert [x["from"] for x in v["asks"]] == [a] and v["offer"]["agree"] == [a]
    sim.step([{"kind": "act", "action": "agree", "target": a}])
    r = receipt_for(sim, a, q["event"])
    assert r.data["ok"] and r.data["answered"] == 1
    sim2, a2, b2, _ = scene()
    sim2.step([])
    q2 = sim2.state["player"]["asks"][0]
    while sim2.state["tick"] < q2["until"]:
        sim2.step([])
    assert sim2.state["player"]["asks"] == [q2]  # still held: this tick's step settles it before acts apply
    assert inhabitant.view(sim2, b2)["asks"] == [] and "agree" not in inhabitant.view(sim2, b2)["offer"]


REASON = "I need food and my family should support me"


class AsksWithReason(StubMind):
    """AsksTheBody, with the reply's reason in its own words (Steering's example)."""

    def decide(self, req):
        v = req.view
        people = [p if isinstance(p, int) else p.get("id") for p in v.get("people") or []]
        if req.kind == "deliberate" and 5 in people:
            for action in ("ask_kin", "ask_food", "buy_from"):
                if action in v.get("offer", []):
                    return MindReply(json.dumps({"action": action, "target": 5, "reason": REASON}),
                                     self.model, {"temperature": 0}, "stop")
        return super().decide(req)


def test_the_asker_s_reason_is_shown_on_the_ask_and_only_on_acts_aimed_at_the_body():
    """Reasons as words (Steering, 2026-11-10): the pending ask and its events carry the asker's own reason;
    no event the body is sent carries `says` unless it is another's ask, gift or visit aimed at it, and the
    words are always that actor's own mind's reason or goal."""
    sim, shown = Sim(8, 14, AsksWithReason()), 0
    for t in range(2 * TICKS_PER_DAY):
        acts = [{"kind": "possess", "agent": 5}] if t == 10 else []
        asks = sim.state.get("player", {}).get("asks", [])
        if asks:
            v = inhabitant.view(sim, 5)
            for x in v["asks"]:
                if x["action"] == "trade":  # trade-1's rule buy: no mind reply, so no words (the client words it)
                    assert x["says"] is None
                    continue
                assert x["says"] == REASON
                shown += 1
            acts.append({"kind": "act", "action": "agree", "target": asks[0]["asker"]})
        elif "player" in sim.state and t % 3 == 0:
            acts.append({"kind": "act", "action": "wait"})
        sim.step(acts)
    assert shown
    from asiimov.live import event_dicts
    sent = [inhabitant.scrub(e, 5, sim.events) for e in event_dicts(sim.events) if inhabitant.witnessed(e, 5, set(), -1)]
    said = [e for e in sent if "says" in e["data"]]
    assert any(e["kind"] == "ask" and e["data"]["says"] == REASON for e in said)
    for e in said:
        d = e["data"]
        assert e["actor"] != 5 and (d.get("to") == 5 or d.get("with") == 5), e
        assert e["kind"] in ("ask", "meet") or d["action"] in inhabitant.SAYS_ACTS, e
        minds = [x for x in sim.events if x.origin == "mind" and x.actor == e["actor"]]
        assert any(d["says"] in (json.loads(m.mind["raw_output"]).get("reason"), json.loads(m.mind["raw_output"]).get("goal"))
                   for m in minds), e
    assert not any("says" in inhabitant.scrub(e, 5)["data"] for e in event_dicts(sim.events)
                   if inhabitant.witnessed(e, 5, set(), -1))  # no log, no words


def test_no_words_for_a_theft_or_a_rule_act_or_another_s_ask():
    from asiimov.events import Event as E
    ask = {"action": "ask_food", "target": 5, "reason": "hungry", "goal": "eat today"}
    log = [E(0, 0, "mind_deliberate", "mind", 3, {}, [], {"raw_output": json.dumps(ask)}),
           E(1, 0, "receipt", "chance", 3, {"action": "steal", "to": 5}, [0]),
           E(2, 0, "ask", "rule", 3, {"action": "ask_food", "to": 6}, [0]),
           E(3, 0, "ask", "rule", 3, {"action": "ask_food", "to": 5}, [0]),
           E(4, 0, "receipt", "rule", 4, {"action": "give_food", "to": 5}, [0]),  # another's mind: never read
           E(5, 0, "meet", "rule", 3, {"with": 5}, [0]),          # the reply was an ask, not this visit: its goal
           E(6, 0, "receipt", "rule", 3, {"action": "buy_from", "to": 5}, [0])]  # nor this buy
    got = [inhabitant.says(log, json.loads(e.to_json()), 5) for e in log]
    assert got == [None, None, None, "hungry", None, "eat today", "eat today"]


class AvoidsThenGives(StubMind):
    """Review on #54: a reply whose reason is for its first action, aimed at a third villager, and whose
    intent then gives food to the player's body."""

    def decide(self, req):
        v = req.view
        people = [p if isinstance(p, int) else p.get("id") for p in v.get("people") or []]
        other = [p for p in people if p != 5]
        if req.kind == "deliberate" and 5 in people and other and "avoid" in v.get("offer", []):
            return MindReply(json.dumps({"action": "avoid", "target": other[0], "reason": "SECRET: I'll rob him tonight",
                                         "goal": "feed my friend", "then": [{"do": "give_food", "target": 5}]}),
                             self.model, {"temperature": 0}, "stop")
        return super().decide(req)


def test_a_step_aimed_at_the_body_shows_its_intent_s_goal_never_the_first_action_s_reason():
    sim = Sim(8, 14, AvoidsThenGives())
    for t in range(2 * TICKS_PER_DAY):
        acts = [{"kind": "possess", "agent": 5}] if t == 10 else []
        if "player" in sim.state and t % 3 == 0:
            acts.append({"kind": "act", "action": "wait"})
        sim.step(acts)
    from asiimov.live import event_dicts
    sent = [inhabitant.scrub(e, 5, sim.events) for e in event_dicts(sim.events) if inhabitant.witnessed(e, 5, set(), -1)]
    gifts = [e for e in sent if e["kind"] == "receipt" and e["data"].get("action") == "give_food" and e["actor"] != 5]
    assert gifts, "no gift reached the body"
    assert all(e["data"].get("says") == "feed my friend" for e in gifts), gifts
    assert not any("SECRET" in json.dumps(e) for e in sent)


def test_an_answer_after_the_asker_left_is_refused_and_says_so():
    """aipt1 t66/t120 (Steering 11:49Z): a late agree to an asker who has walked off returned ok "said yes" while
    nothing changed. Now it fails with the reason in words; the ask settles as before."""
    sim, a, b, player = scene()
    sim.step([])
    ask = next(e for e in sim.events if e.kind == "ask" and e.actor == a)
    x = sim.state["agents"][a]
    x["place"] = next(p for p in sim.map["places"] if p != x["place"])  # walked off (a harness move)
    food = sim.state["agents"][b]["inv"]["food"]
    sim.step([{"kind": "act", "action": "agree", "target": a}])
    r = next(e for e in sim.events if e.kind == "receipt" and e.actor == b and e.data["action"] == "agree")
    assert not r.data["ok"] and r.data["why"] == "not together"
    settled = receipt_for(sim, a, ask.id)
    assert not settled.data["ok"] and settled.data["why"] == "target not here" and r.id in settled.parents
    assert sim.state["agents"][b]["inv"]["food"] == food and "asks" not in sim.state["player"]
