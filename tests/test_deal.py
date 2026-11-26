"""deal-1 (plan/v0-contracts-spec.md B5; Steering 2026-11-25 03:56Z, 04:00Z, 04:06Z): the player's keyed offer.

`offer {target, give, want}` puts a trade to a villager in reach. Limits refuse it at once, each in words, with no
roll and no call. Nothing wanted is a gift and moves at once. Anything else is settled by one roll on the
receiver's nature when the offer lands, held for the day per exact terms; the villager then answers aloud, its
mind wording an answer that is already made, and a yes is checked again and moves both sides at once in one
`deal`. No mind decides a trade, and nothing anyone typed is near it."""
import collections
import hashlib
import json
import os
import random

import pytest

from asiimov import inhabitant, live, persist, verbs
from asiimov import sim as S
from asiimov.events import Event, canonical, read_jsonl
from asiimov.invariants import Checker, deal_violations, state_violations
from asiimov.memory import ANSWER_LINE, MOVE_ANSWER_LINE
from asiimov.minds import ScriptMind, StubMind
from asiimov.params import generate_population
from asiimov.replay import replay
from asiimov.rng import Rng
from asiimov.sim import SUB, TICKS_PER_DAY, WORKPLACE, Sim
from asiimov.things import CARRIED, FIXTURES, THINGS
from test_inhabitant import intent
from test_talk1 import hold
from test_things import lying, replays, runner, stakes, world
from test_time import at
from test_walk import walk

ADULT = ("child", "elder")


class Rec(StubMind):
    """The stub, keeping every request the engine hands it."""

    def __init__(self):
        self.reqs = []

    def decide(self, req):
        self.reqs.append(req)
        return super().decide(req)

    def answers(self):
        return [r for r in self.reqs if r.kind == "answer"]


def offer(t, give=None, want=None, **more):
    return {"kind": "act", "action": "offer", "target": t if isinstance(t, int) else t["id"],
            "give": give or {}, "want": want or {}, **more}


def pair(seed=5, kin=None, mind=None, plain=False, **over):
    """A world with things at midday: a possessed body and an awake villager at its place, both kept there, each
    with 6 food and 40 coin (`plain`: neither, so the log still replays). Returns (sim, the body, the villager,
    the mind)."""
    mind = mind or Rec()
    sim, b = world(seed=seed, mind=mind, **over)
    ag = sim.state["agents"]
    me = ag[b]
    fam = sim.state["families"][me["family"]]["members"]
    t = next(o for o in ag if o["id"] != b and o["place"] == me["place"] and not o["asleep"]
             and o["role"] not in ADULT and (kin is None or (o["id"] in fam) == kin))
    if plain:
        return sim, me, t, mind
    hold(sim, t["id"])
    me["sleep_tick"] = sim.state["tick"] + 400
    me["override"] = {"do": "stay", "target": None, "place": me["place"], "event": 0, "left": 400}  # its own plan waits
    for x in (me, t):
        x["inv"]["food"], x["inv"]["coin"] = 6, 40
    return sim, me, t, mind


def force(sim, yes):
    """The roll's limit set so every offer is taken (or none is): the tests of what follows a yes or a no."""
    sim._offer_roll_limit = lambda t, a, give, want: (1000 if yes else 0, {"terms": 0, "in": 0, "out": 0, "loss": 0,
                                                                          "kin": False})


def put(sim, t, give=None, want=None):
    """Step once with the offer: it lands, and the villager's answer comes in the same tick. Returns the tick's events."""
    n = len(sim.events)
    sim.step([offer(t, give, want)])
    assert not state_violations(sim)
    bad = deal_violations(sim.events, n)
    assert not bad, bad
    return sim.events[n:]


def of(evs, *kinds):
    return [e for e in evs if e.kind in kinds]


def receipt(evs):
    return next(e for e in evs if e.kind == "receipt" and e.data["action"] == "offer")


def carries(sim, who, kind="flower"):
    """Put a thing of `kind` in `who`'s hands (a test's shortcut past the walk to the woods)."""
    r = lying(sim, kind)[0]
    r["at"], r["holder"] = None, who["id"]
    return r


# ------------------------------------------------------------------ the words
def test_one_side_of_an_offer_in_words():
    assert S.offer_side({"coin": 6}) == "6 coin"
    assert S.offer_side({"food": 2, "coin": 1}) == "2 food and 1 coin"
    assert S.offer_side({"food": 1}, yours=True) == "1 of your food"
    assert S.offer_side({"food": 1, "coin": 2}, yours=True) == "1 of your food and 2 of your coin"
    assert S.offer_side({"things": ["flower"]}) == "a flower" and S.offer_side({"things": ["axe"]}) == "an axe"
    assert S.offer_side({"things": ["hoe"]}, yours=True) == "your hoe"
    assert S.offer_side({"food": 2, "coin": 1, "things": ["flower", "axe", "flower"]}) == "2 food, 1 coin, an axe and 2 flowers"
    assert S.offer_side({"things": ["flower", "flower"]}, yours=True) == "your 2 flowers"
    assert S.offer_side({"food": 0, "coin": 3}) == "3 coin"  # a zero count is no part of a side
    # the client words a side the same way (client/godot/scripts/offer_box.gd; test_offer.gd pins these three lines)
    assert [S.offer_side({"coin": 6}), S.offer_side({"food": 2, "things": ["flower"]}),
            S.offer_side({"food": 2, "coin": 6, "things": ["flower", "flower"]})] == [
        "6 coin", "2 food and a flower", "2 food, 6 coin and 2 flowers"]
    with pytest.raises(AssertionError):
        S.offer_side({"food": 0})
    assert S.offer_said({"coin": 6}, {"food": 1}) == "I offer you 6 coin for 1 of your food."
    assert S.offer_said({}, {"things": ["flower"]}) == "I ask you for your flower."


@pytest.mark.parametrize("give,want", [({"coin": 6}, {"food": 1}), ({}, {"coin": 3})])
def test_the_words_after_a_keyed_offer_speak_an_answer_already_made(give, want):
    ask = not give
    said = " Bel asks you for 3 of your coin." if ask else " Bel offers you 6 coin for 1 of your food."
    tails = {k: S.offer_tail("Bel", give, want, "Ari Orra", S.offer_choice(held, why, "Bel", give, want, 1, 2))
             for k, (held, why) in {"yes": ("yes", None), "no": ("no", None), "cant": (None, S.TALK_WHY["food"]),
                                    "none": (None, None)}.items()}
    for k, t in tails.items():
        assert t.startswith(said + " Bel's family: Ari Orra. You answer them now.") and t.endswith(S.SAYS_NEXT)
        assert '"' not in t.replace(S.SAYS_NEXT, ""), "a keyed offer has no line, so nothing is quoted"
        assert t.count(S.talk_record_line("Bel", 1, 2) + S.OFFER_CLOSE + S.SAYS_NEXT) == 1
        assert "gives" not in t and "answer is your own choice" not in t  # the words choose nothing
        assert ("Tell them no." in t) == (k != "yes")
    if ask:
        assert "you hand them 3 coin now." in tails["yes"] and "you won't. Tell them no." in tails["no"]
        assert "You can't give Bel that, as you have no food to spare. Tell them no." in tails["cant"]
        assert "You can't give Bel that now. Tell them no." in tails["none"]
    else:
        assert "you take it. The trade happens now: you hand them 1 food and they hand you 6 coin." in tails["yes"]
        assert "you turn it down. Tell them no." in tails["no"]
        assert "You can't make this trade, as you have no food to spare. Tell them no." in tails["cant"]
        assert "You can't make this trade now. Tell them no." in tails["none"]


def test_every_reason_a_yes_can_be_withheld_for_has_the_receivers_words_and_the_offerers():
    row = verbs.VERBS["offer"]
    assert set(S.OFFER_WHY) <= set(row["reasons"]) and all(verbs.REASONS[k]["you"] for k in row["reasons"])
    # every limit but the ones no awake receiver can meet when it answers
    # (and "target not here": an offerer who has gone is never answered, Steering 2026-11-25 04:50Z; its deal says "gone")
    limits = set(row["reasons"]) - {"offered", "gave", "unknown target", "target asleep", "too many offers",
                                    "already traded", "target not here", "gone"}
    assert limits == set(S.OFFER_WHY)
    assert (S.OFFER_WHY["nothing to spare"], S.OFFER_WHY["bad terms"]) == (S.TALK_WHY["food"], S.TALK_WHY["terms"])


def test_every_kind_that_can_be_carried_has_a_worth_and_a_fixture_has_none():
    assert CARRIED, "nothing can be carried"
    for kind, row in THINGS.items():
        if row["carry"]:
            assert type(row.get("worth")) is int and row["worth"] >= 0, f"{kind} can be carried and has no worth"
        else:
            assert "worth" not in row, kind
    assert not set(FIXTURES) & set(CARRIED)


# ------------------------------------------------------------------ what an act may carry
def test_an_offer_names_a_target_and_two_sides_and_nothing_else_does():
    sim, me, t, _ = pair()
    for bad in ({"kind": "act", "action": "offer", "target": t["id"]},
                offer(t), offer(t, {"food": 0}, {"coin": 1}), offer(t, {"coin": -1}, {"food": 1}),
                offer(t, {"coin": 1.0}, {"food": 1}), offer(t, {"coin": True}, {"food": 1}),
                offer(t, {"coin": S.OFFER_MAX + 1}, {"food": 1}), offer(t, {"seed": 1}, {"food": 1}),
                offer(t, {"things": []}, {"food": 1}), offer(t, {"things": [1, 1]}, {"food": 1}),
                offer(t, {"things": ["flower"]}, {"food": 1}), offer(t, {"things": list(range(S.OFFER_THINGS + 1))}, {"food": 1}),
                {"kind": "act", "action": "offer", "give": {"coin": 1}, "want": {"food": 1}},
                {"kind": "act", "action": "offer", "target": t["id"], "give": {"coin": 1}},
                {"kind": "act", "action": "rest", "give": {"coin": 1}, "want": {}},
                {"kind": "act", "action": "give_food", "target": t["id"], "want": {"coin": 1}}):
        n = len(sim.events)
        with pytest.raises(ValueError):
            sim.step([bad])
        assert len(sim.events) == n, bad
    assert S.offer_side_ok({}) and S.offer_side_ok({"food": 1, "coin": S.OFFER_MAX, "things": [0, 7]})
    assert not S.offer_side_ok(None) and not S.offer_side_ok([]) and not S.offer_side_ok({"things": [[1]]})
    assert S.OFFER_ACTS == ("offer",) and "offer" in S.PLAYER_ACTIONS and "offer" not in S.DELIBERATE_ACTIONS
    assert verbs.VERBS["offer"]["who"] == ("player",) and S.act_cost("offer") == S.COST


# ------------------------------------------------------------------ limits: worded refusals, no roll, no call
def test_each_limit_refuses_in_words_with_no_roll_and_no_call():
    sim, me, t, mind = pair(carry=1)
    ag, b = sim.state["agents"], me["id"]
    far = next(o for o in ag if o["id"] != b and not sim.reach(me, o))
    mine, theirs = carries(sim, me), carries(sim, t)
    loose, stall = lying(sim, "flower")[0], lying(sim, "stall")[0]
    n0, asked = len(sim.events), len(mind.answers())

    def why(target, give=None, want=None):
        r = receipt(put(sim, target, give, want))
        assert r.data["ok"] is False and r.data["why"] in verbs.reasons("offer", "player")
        return r.data["why"]

    assert why(b, {"coin": 1}, {"food": 1}) == "unknown target"
    assert why(10 ** 6, {"coin": 1}, {"food": 1}) == "unknown target"
    assert why(far, {"coin": 1}, {"food": 1}) == "target not here"
    # the giver lacks what it offers: a count, a thing that lies on the ground, a fixture, a thing that never was
    for give in ({"coin": 900}, {"food": 90}, {"things": [loose["id"]]}, {"things": [stall["id"]]}, {"things": [10 ** 6]},
                 {"things": [theirs["id"]]}):
        assert why(t, give, {"food": 1}) == "not yours to offer", give
    for want in ({"coin": 900}, {"food": 90}, {"things": [loose["id"]]}, {"things": [mine["id"]]}, {"things": [stall["id"]]}):
        assert why(t, {"coin": 1}, want) == "not theirs to give", want
    # the receiver keeps a food after any trade that takes food from it
    assert why(t, {"coin": 30}, {"food": t["inv"]["food"]}) == "nothing to spare"
    t["inv"]["food"] = 1
    assert why(t, {"coin": 30}, {"food": 1}) == "nothing to spare"
    t["inv"]["food"] = 6
    # a grudge below -300
    t["relationships"][str(b)] = {"affinity": S.OFFER_MIN_AFFINITY - 1, "note": ""}
    assert why(t, {"coin": 30}, {"food": 1}) == "bad terms"
    del t["relationships"][str(b)]
    # hands (carry 1 here): a thing more than a hand can take, either way; a thing for a thing is no more
    assert why(t, {"things": [mine["id"]]}, {"food": 1}) == "target hands full"
    assert why(t, {"things": [mine["id"]]}) == "target hands full"  # a gift too
    assert why(t, {"coin": 5}, {"things": [theirs["id"]]}) == "hands full"
    assert not of(sim.events[n0:], "offered", "decided", "deal", "mind_answer", "reply")
    assert len(mind.answers()) == asked and "offers" not in t
    assert sim._offer_limit(me, {**t, "asleep": True}, {"coin": 1}, {"food": 1}) == "target asleep"
    force(sim, False)
    evs = put(sim, t, {"things": [mine["id"]]}, {"things": [theirs["id"]]})  # one for one: both hands stay full
    assert receipt(evs).data["why"] == "offered"
    t["relationships"][str(b)] = {"affinity": S.OFFER_MIN_AFFINITY, "note": ""}  # at -300 it is still heard
    assert receipt(put(sim, t, {"coin": 30}, {"food": 1})).data["why"] == "offered"


def test_a_fourth_distinct_offer_in_a_day_is_refused_and_terms_the_receiver_lost_on_are_done():
    sim, me, t, mind = pair()
    price = sim._price()
    assert price > 2
    force(sim, True)
    assert of(put(sim, t, {"coin": price - 1}, {"food": 1}), "deal")[0].data["ok"]  # the receiver is out 1 coin's worth
    n = len(sim.events)
    r = receipt(put(sim, t, {"coin": price - 1}, {"food": 1}))
    assert (r.data["ok"], r.data["why"]) == (False, "already traded") and not of(sim.events[n:], "offered", "decided", "deal")
    force(sim, False)
    for coin in (1, 2):
        assert receipt(put(sim, t, {"coin": coin}, {"food": 1})).data["why"] == "offered"
    assert len(of(sim.events, "decided")) == 3 == S.OFFERS_PER_DAY
    n = len(sim.events)
    r = receipt(put(sim, t, {"coin": price}, {"food": 1}))
    assert (r.data["ok"], r.data["why"]) == (False, "too many offers") and not of(sim.events[n:], "offered", "decided")
    evs = put(sim, t, {"coin": 2}, {"food": 1})  # terms it already answered are still heard, and answered the same
    assert receipt(evs).data["why"] == "offered" and not of(evs, "decided") and of(evs, "reply")[0].data["answer"] == "no"
    other = next(o for o in sim.state["agents"] if o["id"] not in (me["id"], t["id"]) and sim.reach(me, o) and not o["asleep"])
    assert receipt(put(sim, other, {"coin": 3}, {"food": 1})).data["why"] in ("offered", "nothing to spare")  # a pair's count


@pytest.mark.parametrize("extra", [0, 2])
def test_a_fair_or_better_deals_held_yes_can_be_had_again_the_same_day(extra):
    """Steering 2026-11-25 04:35Z: only terms the receiver loses on are done once dealt. 1 food for its price, or
    for more, repeats on its one held yes (no second roll), each time checked by the limits and moved in a deal."""
    sim, me, t, mind = pair()
    force(sim, True)
    give, want = {"coin": sim._price() + extra}, {"food": 1}
    had = (me["inv"]["food"], t["inv"]["food"])
    for i in (1, 2, 3):
        evs = put(sim, t, give, want)
        assert receipt(evs).data["why"] == "offered" and of(evs, "deal")[0].data["ok"], i
    assert len(of(sim.events, "decided")) == 1
    assert (me["inv"]["food"], t["inv"]["food"]) == (had[0] + 3, had[1] - 3)
    t["inv"]["food"] = 1  # the limits still hold it: its last food is not for sale
    assert (receipt(put(sim, t, give, want)).data["why"]) == "nothing to spare"


# ------------------------------------------------------------------ a gift
def test_an_offer_that_wants_nothing_is_a_gift_and_moves_at_once():
    sim, me, t, mind = pair()
    fl = carries(sim, me)
    warmth = (t["relationships"].get(str(me["id"])) or {"affinity": 0})["affinity"]
    evs = put(sim, t, {"food": 2, "coin": 3, "things": [fl["id"]]})
    act, deal, r = of(evs, "act")[0], of(evs, "deal")[0], receipt(evs)
    assert [e.kind for e in evs[:3]] == ["act", "deal", "receipt"] and not of(evs, "offered", "decided")
    assert deal.data == {"move": "offer", "from": me["id"], "to": t["id"],
                         "gives": {"food": 2, "coin": 3, "things": [fl["id"]]}, "gets": {}, "ok": True}
    assert (deal.actor, deal.origin, deal.parents) == (me["id"], "rule", [act.id])
    assert r.data == {"action": "offer", "ok": True, "why": "gave", "to": t["id"]} \
        and r.parents == [act.id, deal.id] + sim._price_cause()  # the warmth is weighed at the price in force
    assert (me["inv"], t["inv"]) == ({"food": 4, "coin": 37}, {"food": 8, "coin": 43}) and fl["holder"] == t["id"]
    rel = t["relationships"][str(me["id"])]
    assert rel["affinity"] == min(1000, warmth + 100) and rel["cause"] == r.id  # worth more than a meal: give_food's 100
    assert any(f"gave 2 food, 3 coin and a flower to {t['name']}" in m for m in me["recent"])
    assert any(f"{me['name']} gave me 2 food, 3 coin and a flower" in m for m in t["recent"])
    assert sim._occasion_text(t, {"why": "gift", "who": me["id"], "detail": "2 food, 3 coin and a flower"}) \
        == f" Why you stop to think: {sim._who(me['id'])} gave you 2 food, 3 coin and a flower."
    assert "offers" not in t and "talk_deals" not in t


def test_a_gift_warms_by_what_it_is_worth_up_to_a_meals_price():
    """Steering 2026-11-25 04:35Z: 100 x min(worth, price) // price, so 1 coin doesn't buy the warmth give_coin
    pays a meal's price for; 1 food, or a meal's price in coin, warms as give_food and give_coin do."""
    def warmed(give, things=0):
        sim, me, t, mind = pair()
        if things:
            give = {**give, "things": [carries(sim, me)["id"]]}
        before = (t["relationships"].get(str(me["id"])) or {"affinity": 0})["affinity"]
        assert receipt(put(sim, t, give)).data["why"] == "gave"
        return t["relationships"][str(me["id"])]["affinity"] - before, sim._price()

    price = warmed({"coin": 1})[1]
    assert price > 2 and THINGS["flower"]["worth"] == 1
    assert warmed({"coin": 1})[0] == 100 // price == warmed({}, things=1)[0]
    assert warmed({"coin": 2})[0] == 200 // price
    assert warmed({"coin": price})[0] == 100 == warmed({"food": 1})[0] == warmed({"food": 3, "coin": 20})[0]
    # every gift path is on the one rule (04:38Z): give_food's food and give_coin's meal's price are worth 100, and a
    # flower handed over warms as the same flower given through an offer does
    for do, want in (("give_food", 100), ("give_coin", 100), ("hand", 100 // price)):
        sim, me, t, mind = pair()
        before = (t["relationships"].get(str(me["id"])) or {"affinity": 0})["affinity"]
        more = {"thing": carries(sim, me)["id"]} if do == "hand" else {}
        sim.step([{"kind": "act", "action": do, "target": t["id"], **more}])
        assert t["relationships"][str(me["id"])]["affinity"] - before == want == sim._gift_warmth(
            {"give_food": price, "give_coin": sim._gift_coin(), "hand": 1}[do]), do


# ------------------------------------------------------------------ the roll
def clamp(v):
    return max(0, min(S.SHARE_MAX, v))


@pytest.mark.parametrize("kin", [False, True])
def test_the_roll_keeps_the_odds_of_the_acts_it_settles_like(kin):
    """1 food at the stall's price is buy_from, and a bare ask for 1 food is ask_food (ask_kin's rule for family):
    the same limit as `_share_answer` logs for the act, kin and not."""
    sim, me, t, _ = pair(kin=kin)
    price, agree = sim._price(), t["traits"]["agreeableness"]
    for aff in (-250, 0, 320, 1000):
        t["relationships"][str(me["id"])] = {"affinity": aff, "note": ""}
        buy, basis = sim._offer_roll_limit(t, me, {"coin": price}, {"food": 1})
        assert buy == sim._talk_roll_limit(t, me, "buy_from") == clamp(350 + agree // 4 + aff // 4 + (200 if kin else 0))
        assert basis == {"terms": 350, "in": price, "out": price, "loss": 0, "kin": kin}
        ask, basis = sim._offer_roll_limit(t, me, {}, {"food": 1})
        assert ask == sim._talk_roll_limit(t, me, "ask_food") == (
            400 + agree // 2 + aff // 4 if kin else clamp(100 + agree // 4 + aff // 4))
        assert basis == {"terms": 100, "in": 0, "out": price, "loss": price, "kin": kin}
    # and against the acts themselves, in a twin of this world: the limit each one's receipt logs
    for action, give in (("buy_from", {"coin": price}), ("ask_food", {})):
        a, me_a, t_a, _ = pair(kin=kin)
        o, me_o, t_o, _ = pair(kin=kin)
        me_a["cooldown_until"] = 0
        n = len(a.events)
        a.step([{"kind": "act", "action": action, "target": t_a["id"]}])
        logged = next(e for e in a.events[n:] if e.kind == "receipt" and e.data["action"] == action)
        decided = of(put(o, t_o, give, {"food": 1}), "decided")[0]
        assert logged.data["limit"] == decided.data["limit"] and logged.data["kin"] is decided.data["kin"] is kin, action


@pytest.mark.parametrize("kin", [False, True])
def test_the_loss_cap_shrinks_the_odds_as_fast_as_the_stake_grows(kin):
    sim, me, t, _ = pair(kin=kin)
    price, agree = sim._price(), t["traits"]["agreeableness"]
    plus = agree // 4 + (200 if kin else 0)
    # "1 coin for 50 coin" would stand at the bare ask's odds; the receiver is out 49 coin, so the limit falls by price/49
    limit, basis = sim._offer_roll_limit(t, me, {"coin": 1}, {"coin": 50})
    assert basis == {"terms": 105, "in": 1, "out": 50, "loss": 49, "kin": kin}
    assert limit == clamp(105 + plus) * price // 49 and limit < 60
    # a bare ask: for 50 coin, and for 2 food (of family: ask_kin's rule first, then the cap)
    base = 400 + agree // 2 if kin else clamp(100 + agree // 4)
    assert sim._offer_roll_limit(t, me, {}, {"coin": 50})[0] == base * price // 50
    assert sim._offer_roll_limit(t, me, {}, {"food": 2})[0] == base * price // (2 * price) == base // 2
    # out no more than one food's price: uncapped
    assert sim._offer_roll_limit(t, me, {"food": 1}, {"coin": 2 * price})[0] == clamp(100 + 250 * price // (2 * price) + plus)
    assert sim._offer_roll_limit(t, me, {"coin": 1}, {"coin": 1 + price})[1]["loss"] == price
    # the better for the receiver, the likelier, up to the cap; a thing weighs its kind's worth
    assert sim._offer_roll_limit(t, me, {"coin": 2 * price}, {"food": 1}) == (
        clamp(350 + 600 * price // (2 * price) + plus), {"terms": 650, "in": 2 * price, "out": price, "loss": 0, "kin": kin})
    assert sim._offer_roll_limit(t, me, {"coin": 500}, {"food": 1})[0] == S.SHARE_MAX
    worth = THINGS["flower"]["worth"]
    assert sim._offer_roll_limit(t, me, {"things": ["flower"]}, {"food": 1})[1] == {
        "terms": 100 + 250 * worth // price, "in": worth, "out": price, "loss": price - worth, "kin": kin}
    assert sim._offer_roll_limit(t, me, {"things": ["flower"]}, {"things": ["flower"]})[1]["terms"] == 350
    # clamped at 0 as at the top, as _share_answer clamps
    t["relationships"][str(me["id"])] = {"affinity": -1000, "note": ""}
    t["traits"]["agreeableness"] = 0
    if not kin:
        assert sim._offer_roll_limit(t, me, {"coin": 1}, {"food": 1})[0] == 0


SEED = 10


def test_the_roll_is_on_its_own_keyed_stream_and_the_log_recomputes_it():
    sim, me, t, _ = pair(seed=SEED, plain=True)
    price, day = sim._price(), sim.state["tick"] // TICKS_PER_DAY
    assert me["inv"]["coin"] >= price and t["inv"]["food"] >= 2
    evs = put(sim, t, {"coin": price}, {"food": 1})
    act, offered, d, r = of(evs, "act")[0], of(evs, "offered")[0], of(evs, "decided")[0], receipt(evs)
    assert [e.kind for e in evs[:4]] == ["act", "offered", "decided", "receipt"]
    assert offered.data == {"with": t["id"], "gives": {"coin": price}, "gets": {"food": 1}, "place": me["place"]}
    assert (offered.actor, offered.origin, offered.parents) == (me["id"], "rule", [act.id])
    key = canonical({"give": {"coin": price}, "want": {"food": 1}})
    assert d.data["stream"] == f"offer:{t['id']}:{me['id']}:{key}" and d.data["index"] == day
    assert d.data["roll"] == Rng(SEED).at(d.data["stream"], day, 0, 999) and 0 <= d.data["roll"] <= 999
    assert d.data["answer"] == ("yes" if d.data["roll"] < d.data["limit"] else "no")
    assert (d.actor, d.origin, d.data["with"], d.data["move"]) == (t["id"], "chance", me["id"], "offer")
    assert d.parents[0] == offered.id and d.data["gives"] == {"coin": price} and d.data["gets"] == {"food": 1}
    assert {"terms", "in", "out", "loss", "limit", "kin"} <= set(d.data)  # what the limit rests on, logged
    assert r.data == {"action": "offer", "ok": True, "why": "offered", "to": t["id"]} and r.parents == [act.id, offered.id]
    assert sim.rng.counters == Rng(SEED).counters or "offer" not in "".join(sim.rng.counters)  # no counter moved for it
    replays(sim)


def test_one_answer_a_day_per_exact_terms_with_things_by_their_kind():
    sim, me, t, mind = pair()
    force(sim, False)
    a, b2 = carries(sim, me), carries(sim, me)
    first = put(sim, t, {"things": [a["id"]]}, {"food": 1})
    d = of(first, "decided")[0]
    assert d.data["gives"] == {"things": ["flower"]} and of(first, "offered")[0].data["gives"] == {"things": [a["id"]]}
    again = put(sim, t, {"things": [b2["id"]]}, {"food": 1})  # the other flower: the same terms
    assert not of(again, "decided") and of(again, "reply")[0].data["answer"] == "no"
    assert of(again, "mind_answer")[0].data["deal"]["decided"] == d.id
    force(sim, True)  # whatever its nature would say now, today's answer is held
    assert of(put(sim, t, {"things": [a["id"]]}, {"food": 1}), "reply")[0].data["answer"] == "no"
    both = put(sim, t, {"things": [a["id"], b2["id"]]}, {"food": 1})  # two flowers are other terms
    assert of(both, "decided")[0].data["gives"] == {"things": ["flower", "flower"]} and of(both, "deal")[0].data["ok"]
    book = sim._offer_book(t, me["id"])
    assert [(v["answer"], v["done"]) for v in book.values()] == [("no", 0), ("yes", 1)]
    while sim.state["tick"] % TICKS_PER_DAY:
        sim.step([])
    assert sim._offer_book(t, me["id"]) == {}  # a new day: nothing is held


# ------------------------------------------------------------------ the answer, in words, then the deal
def test_a_yes_is_spoken_then_moves_both_sides_at_once_in_one_deal():
    sim, me, t, mind = pair()
    force(sim, True)
    fl = carries(sim, t)
    warm = (me["relationships"].get(str(t["id"])) or {"affinity": 0})["affinity"]
    theirs = (t["relationships"].get(str(me["id"])) or {"affinity": 0})["affinity"]
    give, want = {"coin": 7, "food": 1}, {"food": 2, "things": [fl["id"]]}
    evs = put(sim, t, give, want)
    assert [e.kind for e in evs if e.actor in (me["id"], t["id"]) and e.kind in (
        "act", "offered", "decided", "receipt", "mind_answer", "reply", "deal")] == [
        "act", "offered", "decided", "receipt", "mind_answer", "reply", "deal"]
    offered, d, mind_ev, reply, deal = (of(evs, k)[0] for k in ("offered", "decided", "mind_answer", "reply", "deal"))
    assert all(e.tick == offered.tick for e in (d, mind_ev, reply, deal))  # answered in the tick it landed
    assert mind_ev.data["offered"] == ["yes"] and mind_ev.data["deal"] == {
        "move": "offer", "gives": give, "gets": want, "decided": d.id} and mind_ev.parents == [offered.id]
    assert reply.data == {"with": me["id"], "says": "All right, it's a deal.", "next": "carry_on", "answer": "yes"}
    assert deal.data == {"move": "offer", "from": me["id"], "to": t["id"], "gives": give, "gets": want, "ok": True}
    assert (deal.actor, deal.origin, deal.parents) == (me["id"], "rule", [offered.id, mind_ev.id, d.id])
    assert reply.id < deal.id, "the words come first"
    assert (me["inv"], t["inv"]) == ({"food": 7, "coin": 33}, {"food": 5, "coin": 47}) and fl["holder"] == me["id"]
    assert me["relationships"][str(t["id"])]["affinity"] == warm + S.OFFER_WARMS  # a yes warms the one who offered
    assert t["relationships"][str(me["id"])]["affinity"] == theirs
    assert "talk_deals" not in t and "talk_until" not in t  # trades leave the ask record alone (B5.7)
    assert any(f"traded with {t['name']}: gave 1 food and 7 coin, got 2 food and a flower" in m for m in me["recent"])
    assert any(f"traded with {me['name']}: gave 2 food and a flower, got 1 food and 7 coin" in m for m in t["recent"])
    assert any(f"{me['name']} offered me 1 food and 7 coin for 2 food and a flower" in m for m in t["recent"])
    assert not state_violations(sim)


def test_a_no_is_spoken_and_nothing_moves_and_no_deal_is_written():
    sim, me, t, mind = pair()
    force(sim, False)
    evs = put(sim, t, {"coin": 2}, {"food": 1})
    mind_ev, reply = of(evs, "mind_answer")[0], of(evs, "reply")[0]
    assert mind_ev.data["offered"] == ["no"] and "withheld" not in mind_ev.data["deal"]
    assert reply.data["answer"] == "no" and reply.data["says"] == "No, I'll keep what I have."
    assert not of(evs, "deal") and (me["inv"], t["inv"]) == ({"food": 6, "coin": 40}, {"food": 6, "coin": 40})
    assert any(f"{t['name']} turned down my offer of 2 coin for 1 food" in m for m in me["recent"])
    assert "talk_deals" not in t
    evs = put(sim, t, {}, {"food": 1})  # a bare ask, refused
    assert of(evs, "reply")[0].data["answer"] == "no" and not of(evs, "deal")
    assert any(f"{t['name']} refused to give me 1 food" in m for m in me["recent"])
    assert any(f"{me['name']} asked me for 1 food" in m for m in t["recent"])


@pytest.mark.parametrize("held", ["yes", "no"])
def test_the_answers_prompt_states_the_offer_and_the_answer_already_made_and_asks_for_words_alone(held):
    sim, me, t, mind = pair()
    force(sim, held == "yes")
    give, want = {"coin": 6}, {"food": 1}
    evs = put(sim, t, give, want)
    req = mind.answers()[-1]
    fam = ", ".join(sim.state["agents"][m]["name"] for m in sim.state["families"][me["family"]]["members"] if m != me["id"])
    tail = S.offer_tail(me["name"], give, want, fam, S.offer_choice(held, None, me["name"], give, want, 0, 0))
    now = req.prompt.split("# Now (answer)\n")[1]
    assert now.endswith(tail) and f" {me['name']} offers you 6 coin for 1 of your food." in now
    assert '"' not in now, "a keyed offer has no line, so nothing is quoted"
    assert ANSWER_LINE in req.prompt and MOVE_ANSWER_LINE not in req.prompt  # say-3's reply: words, and what it does next
    assert req.view["trade"] == {"move": "offer", "gives": give, "gets": want, "answer": held,
                                 "decided": of(evs, "decided")[0].id}
    assert "answers" not in req.view and "gives" not in req.view
    assert req.view["occasion"]["detail"] == "I offer you 6 coin for 1 of your food."
    assert f"{me['name']} offered me 6 coin for 1 food" in req.prompt.split("# Now (answer)")[0]  # the engine's own note
    assert req.prompt_hash == of(evs, "mind_answer")[0].mind["prompt_hash"]


def test_the_answer_is_the_worlds_whatever_the_reply_holds():
    class Liar(Rec):
        def decide(self, req):
            out = super().decide(req)
            if req.kind == "answer":
                return type(out)(json.dumps({"answer": "yes", "gives": "food", "says": "Yes! Take it all.", "next": "stay"}),
                                 out.model, out.sampling, out.finish_reason)
            return out
    sim, me, t, mind = pair(mind=Liar())
    force(sim, False)
    evs = put(sim, t, {"coin": 1}, {"food": 3})
    assert of(evs, "reply")[0].data["answer"] == "no" and not of(evs, "deal") and t["inv"]["food"] == 6
    for raw in ("", "not json", json.dumps({"says": 5})):  # a failed reply loses no rolled yes
        class Broken(Rec):
            def decide(self, req, raw=raw):
                out = super().decide(req)
                return type(out)(raw, out.model, out.sampling, out.finish_reason) if req.kind == "answer" else out
        sim, me, t, mind = pair(mind=Broken())
        force(sim, True)
        evs = put(sim, t, {"coin": 5}, {"food": 1})
        assert of(evs, "reply")[0].data["answer"] == "yes" and of(evs, "deal")[0].data["ok"] and t["inv"]["food"] == 5


def test_a_yes_withheld_when_the_answer_is_asked_for_says_why_and_writes_a_deal_that_moved_nothing():
    for held in ("yes", "no"):
        sim, me, t, mind = pair()
        force(sim, held == "yes")
        o = offer(t, {"coin": 9}, {"food": 1})
        sim.take(o)  # it lands: heard, and answered by its nature
        t["inv"]["food"] = 1  # then its food goes before it is asked for its words
        n = len(sim.events)
        sim.step([o])
        evs = sim.events[n:]
        mind_ev, reply = of(evs, "mind_answer")[0], of(evs, "reply")[0]
        assert mind_ev.data["offered"] == ["no"] and mind_ev.data["deal"]["withheld"] == "nothing to spare"
        assert "You can't make this trade, as you have no food to spare. Tell them no." in mind.answers()[-1].prompt
        assert "made up your mind" not in mind.answers()[-1].prompt
        assert reply.data["answer"] == "no" and (me["inv"]["coin"], t["inv"]["coin"]) == (40, 40)
        deals = of(evs, "deal")
        if held == "yes":  # a yes never just fizzles: the player is told why
            assert deals[0].data == {"move": "offer", "from": me["id"], "to": t["id"], "gives": {"coin": 9},
                                     "gets": {"food": 1}, "ok": False, "why": "nothing to spare"}
            assert verbs.REASONS[deals[0].data["why"]]["you"]
            assert sim._offer_book(t, me["id"])[canonical({"give": {"coin": 9}, "want": {"food": 1}})]["done"] == 0
        else:  # a rolled no is its no, whatever else is true
            assert not deals
        assert not deal_violations(sim.events, n)


@pytest.mark.parametrize("held", ["yes", "no"])
def test_an_offerer_who_walks_off_before_the_answer_gets_no_call_and_a_deal_that_says_gone(held):
    """Steering 2026-11-25 04:50Z: a villager doesn't speak to someone who has left. The offer lands, the player walks
    out of reach before the villager would answer: no answer call and no reply, and a `deal` that moved nothing says
    "gone", citing the offer and the decision, whatever the roll was. The terms stay open for the day."""
    sim, me, t, mind = pair()
    force(sim, held == "yes")
    o = at(offer(t, {"coin": 5}, {"food": 1}), 0)
    sim.take(o)
    w = at(walk(*sim.map["places"]["woods"]["anchor"]), 300)
    sim.take(w)
    assert not sim._near(me, t)
    n, asked = len(sim.events), len(mind.answers())
    had = (dict(me["inv"]), dict(t["inv"]))
    sim.step([o, w])
    evs = sim.events[n:]
    off, dec = of(sim.events, "offered")[-1], of(sim.events, "decided")[-1]
    assert len(mind.answers()) == asked and not of(evs, "mind_answer", "reply")
    deal = of(evs, "deal")
    assert len(deal) == 1 and deal[0].data == {"move": "offer", "from": me["id"], "to": t["id"], "gives": {"coin": 5},
                                               "gets": {"food": 1}, "ok": False, "why": "gone"}
    assert (deal[0].actor, deal[0].parents) == (me["id"], [off.id, dec.id]) and dec.data["answer"] == held
    assert (me["inv"]["coin"], t["inv"]["food"]) == (had[0]["coin"], had[1]["food"])
    assert verbs.REASONS["gone"]["you"] and "gone" in verbs.VERBS["offer"]["reasons"]
    assert not deal_violations(sim.events, 0) and not state_violations(sim)
    assert sim._offer_book(t, me["id"])[canonical({"give": {"coin": 5}, "want": {"food": 1}})]["done"] == 0
    # the invariant: a "gone" deal must rest on the receiver's own decision about this offerer
    bad = Event(**{**json.loads(deal[0].to_json()), "parents": [off.id]})
    assert deal_violations(sim.events[:deal[0].id] + [bad], deal[0].id)


def test_the_answers_prompt_shows_what_the_hands_hold_in_the_views_words():
    """Steering 2026-11-25 04:50Z: a villager told "your flower" or "your hands are full" sees, in its own holdings
    line, what its hands hold, as the view names it. Empty hands read exactly as before."""
    sim, me, t, mind = pair()
    force(sim, True)
    put(sim, t, {"coin": 2}, {"food": 1})
    p = mind.answers()[-1].prompt
    assert f"You carry {t['inv']['food'] + 1} food and {t['inv']['coin'] - 2} coin. Here: " in p  # before the deal
    fl = carries(sim, t)
    put(sim, t, {"coin": 3}, {"things": [fl["id"]]})
    p = mind.answers()[-1].prompt
    assert "your flower" in p and " food, " in p and " coin and a flower. Here: " in p
    seen = inhabitant.view(sim, me["id"])["present"]
    assert any(x["id"] == t["id"] and x.get("carries") == [{"id": fl["id"], "kind": "flower"}] for x in seen) \
        or fl["holder"] == me["id"]  # the view shows the same kind by name (or the yes already moved it)
    assert S.carry_words(5, 30) == "5 food and 30 coin" and S.carry_words(5, 30, ["flower", "flower"]) == \
        "5 food, 30 coin and 2 flowers" and S.carry_words(0, 1, ["flower"]) == "0 food, 1 coin and a flower"
    # a full pair of hands is shown whole, so "your hands are full" rests on what the line says
    sim, me, t, mind = pair()
    force(sim, True)
    cap = sim.state["stakes"]["things"]["carry"]
    mine = carries(sim, me)
    o = offer(t, {"things": [mine["id"]]}, {"coin": 1})
    sim.take(o)  # it lands with a hand free; then its hands fill before it is asked for its words
    for _ in range(cap):
        extra = sim._new_thing("flower", [0, 0])
        extra["at"], extra["holder"] = None, t["id"]
    sim.step([o])
    assert mind.answers()[-1].view["trade"]["withheld"] == "target hands full"
    p = mind.answers()[-1].prompt
    assert f" coin and {cap} flowers. Here: " in p and "your hands are full" in p


def test_a_yes_is_checked_again_after_the_words_and_a_failed_check_says_why():
    sim, me, t, mind = pair()
    force(sim, True)
    n = len(sim.events)
    sim.step([offer(t, {"coin": 9}, {"food": 1})], during=lambda s, done: me["inv"].update(coin=3))
    evs = sim.events[n:]
    assert of(evs, "mind_answer")[0].data["offered"] == ["yes"] and of(evs, "reply")[0].data["answer"] == "yes"
    deal = of(evs, "deal")[0]
    assert (deal.data["ok"], deal.data["why"]) == (False, "not yours to offer")
    assert (me["inv"], t["inv"]) == ({"food": 6, "coin": 3}, {"food": 6, "coin": 40})
    assert not deal_violations(sim.events, n)
    r = receipt(put(sim, t, {"coin": 3}, {"food": 1}))  # other terms, which it can pay: heard again
    assert r.data["why"] == "offered"


def test_the_one_offered_to_stands_to_answer_and_the_days_long_calls_wait_for_the_reply():
    sim, me, t, mind = pair()
    force(sim, False)
    t["override"] = None
    assert not sim._talk_due([])
    sim.take(offer(t, {"coin": 900}, {"food": 1}))  # a limit: nobody is held, nothing is due
    assert t["override"] is None and not sim._talk_due([])
    o = offer(t, {"coin": 2}, {"food": 1}, f=S.act_cost("offer"))
    eid = sim.take(o)
    offered = next(e for e in sim.events[eid:] if e.kind == "offered")
    assert t["override"] == {"do": "stay", "target": None, "place": t["place"], "event": offered.id,
                             "left": S.LISTEN_TICKS, "talk": True}  # the listening hold, as for a line
    occ = t["occasion"]
    assert (occ["why"], occ["who"], occ["event"], occ["tick"]) == ("spoken", me["id"], offered.id, sim.state["tick"])
    assert occ["offer"]["answer"] == "no" and occ["offer"]["words"] == [{"coin": 2}, {"food": 1}]
    assert sim._talk_due([])  # pace-1: its answer is due this very tick, so a plan or reflect call waits one
    n = len(mind.answers())
    sim.step([offer(t, {"coin": 900}, {"food": 1}), o])
    assert len(mind.answers()) == n + 1 and t["occasion"] is None


def test_the_stub_voice_keeps_its_own_words_for_an_offer():
    class Voice:
        sampling = {}

        def reply(self, *a):
            raise AssertionError("the voice was asked to answer a keyed offer")
        answer = reply
    sim, me, t, mind = pair(mind=ScriptMind(voice=Voice()))
    force(sim, True)
    evs = put(sim, t, {"coin": 9}, {"food": 1})
    assert of(evs, "reply")[0].data["says"] == "All right, it's a deal." and of(evs, "deal")[0].data["ok"]


# ------------------------------------------------------------------ what the player is shown
def test_the_view_names_who_can_be_offered_and_what_those_in_reach_carry():
    sim, me, t, mind = pair()
    b, ag = me["id"], sim.state["agents"]
    fl = carries(sim, t)
    v = inhabitant.view(sim, b)
    here = [o["id"] for o in ag if o["id"] != b and sim.reach(me, o)]
    assert v["offer"]["offer"] == [o for o in here if not ag[o]["asleep"]] and t["id"] in v["offer"]["offer"]
    row = next(p for p in v["present"] if p["id"] == t["id"])
    assert row["carries"] == [{"id": fl["id"], "kind": "flower"}]
    assert all(p["carries"] == [] for p in v["present"] if p["id"] != t["id"])
    assert set(v) <= set(inhabitant.VIEW_KEYS)
    force(sim, True)
    evs = put(sim, t, {"coin": 9}, {"things": [fl["id"]]})
    near = set(here)
    seen = {e.kind: inhabitant.scrub(json.loads(e.to_json()), b, near=near) for e in evs
            if inhabitant.witnessed(json.loads(e.to_json()), b, near, e.tick, near)}
    assert {"act", "offered", "receipt", "reply", "deal"} <= set(seen)
    assert "decided" not in seen and "mind_answer" not in seen  # the roll is the villager's own
    assert seen["deal"]["data"] == {"move": "offer", "from": b, "to": t["id"], "gives": {"coin": 9},
                                    "gets": {"things": [fl["id"]]}, "ok": True}
    assert seen["reply"]["data"] == {"with": b, "says": "All right, it's a deal.", "answer": "yes"}
    v = inhabitant.view(sim, b)
    assert v["carried"] == [{"id": fl["id"], "kind": "flower"}] and v["coin"] == 31
    me["asleep"] = True
    assert "offer" not in inhabitant.view(sim, b)["offer"]


def test_a_world_without_things_still_trades_food_and_coin():
    mind = Rec()
    sim = Sim(5, 30, mind)
    sim.run_until(TICKS_PER_DAY // 2)
    ag = sim.state["agents"]
    me = next(a for a in ag if not a["asleep"] and a["place"] is not None and a["role"] not in ADULT
              and any(o["place"] == a["place"] and o is not a and not o["asleep"] for o in ag))
    sim.step([{"kind": "possess", "agent": me["id"]}])
    t = next(o for o in ag if o is not me and o["place"] == me["place"] and not o["asleep"])
    hold(sim, t["id"])
    me["inv"]["coin"], t["inv"]["food"] = 40, 6
    force(sim, True)
    assert receipt(put(sim, t, {"things": [0]}, {"food": 1})).data["why"] == "not yours to offer"
    assert of(put(sim, t, {"coin": 9}, {"food": 1}), "deal")[0].data["ok"]
    v = inhabitant.view(sim, me["id"])
    assert t["id"] in v["offer"]["offer"] and all("carries" not in p for p in v["present"])


# ------------------------------------------------------------------ the invariant
def test_the_deal_invariant_catches_an_offer_settled_without_its_rolled_yes():
    sim, me, t, mind = pair()
    force(sim, True)
    evs = put(sim, t, {"coin": 9}, {"food": 1})
    offered, d, mind_ev, deal = (of(evs, k)[0] for k in ("offered", "decided", "mind_answer", "deal"))
    data = dict(deal.data)

    def planted(parents, **over):
        n = len(sim.events)
        sim._emit("deal", "rule", over.pop("actor", me["id"]), {**data, **over}, parents)
        return deal_violations(sim.events, n)
    assert not planted([offered.id, mind_ev.id, d.id])
    assert any("without the offer" in v for v in planted([mind_ev.id, d.id]))
    assert any("without the receiver's answer" in v for v in planted([offered.id, d.id]))
    assert any("rolled yes" in v for v in planted([offered.id, mind_ev.id]))
    assert any("without the offer" in v for v in planted([offered.id, mind_ev.id, d.id], gives={"coin": 1}))
    assert planted([offered.id, mind_ev.id, d.id], actor=t["id"])  # an offer's deal is the offerer's
    assert planted([offered.id, mind_ev.id, d.id], gets={})  # a gift cites the act that gave it
    assert planted([offered.id, mind_ev.id, d.id], gives={"food": 0})
    assert any("no reason" in v for v in planted([offered.id, mind_ev.id, d.id], ok=False))
    # a no can settle nothing: plant a deal on a rolled no
    force(sim, False)
    evs = put(sim, t, {"coin": 1}, {"food": 1})
    o2, d2, m2 = (of(evs, k)[0] for k in ("offered", "decided", "mind_answer"))
    data = {"move": "offer", "from": me["id"], "to": t["id"], "gives": {"coin": 1}, "gets": {"food": 1}, "ok": True}
    bad = planted([o2.id, m2.id, d2.id])
    assert any("rolled yes" in v for v in bad) and any("offered yes" in v for v in bad)
    # and the things ledger follows a deal from the log alone
    sim2, me2, t2, _ = pair()
    check = Checker(sim2)
    fl = lying(sim2, "flower")[0]
    check.before([])
    eid = sim2._emit("deal", "rule", me2["id"], {"move": "offer", "from": me2["id"], "to": t2["id"],
                                                 "gives": {"things": [fl["id"]]}, "gets": {}, "ok": True}, [])
    sim2.step([])
    bad = check.after()
    assert any(f"event {eid}: deal moved thing" in v for v in bad) and any("gift's deal" in v for v in bad), bad


# ------------------------------------------------------------------ worlds that don't offer
@pytest.mark.parametrize("label", ["things-1", "45"])
def test_the_golden_world_under_things_1s_label_is_things_1s_log(monkeypatch, label):
    from test_world import FIRE, GOLDEN, run
    monkeypatch.setattr(S, "ENGINE_VERSION", label)  # the label it was built under, and the number it merged as
    s = run(StubMind(), agents=20, days=2, seed=3, player=FIRE)
    assert hashlib.sha256("".join(e.to_json() + "\n" for e in s.events).encode()).hexdigest() == GOLDEN[label]


THINGS_1_SESSION = "84ab2ef04e2e5f0eae3242232a0049c669563a50b33413e19d783882dd6b6f02"  # written by things-1's own code (d219fbf)


def test_a_player_who_makes_no_offer_lives_in_things_1s_world():
    """A played session with lines, asks and gifts of food but no offer and nothing handed over: the same log under
    either label, and the very log things-1's own code wrote for it (its hash, taken at things-1's commit). So
    nothing in deal-1 touches a world until an offer is made or a thing is handed over (a handed thing now warms
    by what it is worth, the one gift rule; tests/test_things.py)."""
    def session():
        sim, b = world(seed=3)
        me = sim.state["agents"][b]
        rng = random.Random(3)
        for i in range(TICKS_PER_DAY // 2):
            others = [o["id"] for o in sim.state["agents"] if o["id"] != b and sim.reach(me, o)]
            acts = []
            if others and i % 3 == 0:
                acts = [{"kind": "act", "action": "say", "target": rng.choice(others), "text": "Good day to you."}]
            elif others and i % 3 == 1:
                acts = [{"kind": "act", "action": rng.choice(("ask_food", "give_food", "buy_from")), "target": rng.choice(others)}]
            sim.step(acts)
        sim.finish()
        return ["".join(e.to_json() for e in sim.events[1:] if e.kind != "checkpoint"), sim.events[0].data["engine"]]
    now = session()
    assert hashlib.sha256(now[0].encode()).hexdigest() == THINGS_1_SESSION
    was_label = S.ENGINE_VERSION
    try:
        S.ENGINE_VERSION = "things-1"
        then = session()
    finally:
        S.ENGINE_VERSION = was_label
    assert now[0] == then[0] and (now[1], then[1]) == ("deal-1" if was_label == "deal-1" else was_label, "things-1")


# ------------------------------------------------------------------ sessions: early = batch, invariants, replay
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_session_of_offers_equals_batch_holds_every_invariant_and_replays(seed):
    """Two days of a body that picks flowers and puts offers to whoever is in reach: trades, asks, gifts and what
    can't work. Taken as they arrive, the log is a batch run's, the checker (ledgers, things, deals) finds nothing,
    every receipt's reason is one the verb's row lists, and it replays."""
    rng = random.Random(seed)
    pops = [generate_population(seed, 40) for _ in range(2)]
    for pop in pops:
        pop["stakes"] = stakes()
    early, batch = (Sim(seed, 40, StubMind(), genesis=pop) for pop in pops)
    for sim in (early, batch):
        sim.run_until(TICKS_PER_DAY // 3)
    b = next(a["id"] for a in early.state["agents"] if not a["asleep"] and a["place"] is not None and a["role"] in WORKPLACE)
    me, check, ag = early.state["agents"][b], Checker(early), early.state["agents"]

    def side(who, wild):
        out = {}
        if rng.random() < 0.5:
            out["food"] = rng.randint(1, 3 if not wild else 9)
        if rng.random() < 0.5:
            out["coin"] = rng.randint(1, 8 if not wild else 80)
        mine = [r["id"] for r in early.carried(who)]
        if mine and rng.random() < 0.5:
            out["things"] = rng.sample(mine, rng.randint(1, len(mine)))
        elif wild and rng.random() < 0.3:
            out["things"] = [rng.choice([r["id"] for r in early.state["things"]["list"]] + [10 ** 5])]
        return out

    for i in range(2 * TICKS_PER_DAY):
        acts = [{"kind": "possess", "agent": b}] if i == 0 else []
        check.before(acts)
        for a in acts:
            early.take(a)

        def send(a, f):
            acts.append(at(a, f))
            return early.take(acts[-1])

        if i and not me["asleep"] and early.state["tick"] < me["sleep_tick"] and me["needs"]["fatigue"] < 1000:
            f = rng.randrange(0, 30)
            flowers = [r for r in early.state["things"]["list"] if r["kind"] == "flower" and r["at"] is not None]
            if len(early.carried(b)) < 2 and flowers and rng.random() < 0.4:
                fl = min(flowers, key=lambda r: (abs(r["at"][0] - early.tile(me)[0]) + abs(r["at"][1] - early.tile(me)[1]), r["id"]))
                send(walk(*fl["at"]), f)
                if early.beside(me, fl):
                    f += S.act_cost("take")
                    send({"kind": "act", "action": "take", "thing": fl["id"]}, f)
            else:
                awake = [o for o in ag if o["id"] != b and not o["asleep"] and o["place"] is not None]
                if awake and rng.random() < 0.5:
                    send(walk(*early.tile(rng.choice(awake))), f)
            for _ in range(rng.randrange(0, 3)):
                f += S.act_cost("offer") + rng.randrange(0, 20)
                if f >= SUB:
                    break
                near_by = [o["id"] for o in ag if o["id"] != b and early.reach(me, o)]
                wild = rng.random() < 0.25
                to = rng.choice(near_by) if near_by and not wild else rng.randrange(40)
                give, want = side(b, wild), side(to, wild) if rng.random() < 0.85 else {}
                if not give and not want:
                    want = {"food": 1}
                send(offer(to, give, want), f)
        early.step(acts)
        batch.step(acts)
        bad = check.after()
        assert not bad, bad
        assert early.state_hash() == batch.state_hash(), early.state["tick"]
    early.finish(), batch.finish()
    log = early.events
    assert [e.to_json() for e in log] == [e.to_json() for e in batch.events]
    kinds = collections.Counter(e.kind for e in log)
    deals = collections.Counter((e.data["ok"], bool(e.data["gets"])) for e in log if e.kind == "deal")
    whys = collections.Counter(e.data["why"] for e in log if e.kind == "receipt" and e.actor == b and e.data["action"] == "offer")
    assert kinds["offered"] > 10 and kinds["decided"] > 10 and kinds["reply"] > 10, kinds
    assert deals[(True, True)] >= 1 and deals[(True, False)] >= 2, deals  # trades that moved, and gifts
    assert whys["offered"] and whys["gave"] and len(whys) >= 6, whys
    assert all(e.data["why"] in verbs.reasons(e.data["action"], "player") for e in log if e.kind == "receipt" and e.actor == b)
    for e in log:
        if e.kind == "decided" and e.data.get("move") == "offer":
            assert e.data["roll"] == Rng(seed).at(e.data["stream"], e.data["index"], 0, 999)
            assert (e.data["answer"] == "yes") == (e.data["roll"] < e.data["limit"])
    events = [Event(**json.loads(e.to_json())) for e in log]
    assert replay(events).state_hash() == early.state_hash()


# ------------------------------------------------------------------ over the runner
def submit(r, iid, args, f=0):
    return r.submit(intent(iid, "act", args, r.sim.state["tick"]), client="c1", scope="inhabitant", f=f)


def test_over_the_runner_an_offer_names_its_sides_and_is_answered_at_once(tmp_path):
    out = str(tmp_path / "w")
    r, b = runner(out)
    sim = r.sim
    me = sim.state["agents"][b]
    for iid, args in (("x1", {"action": "offer", "target": 1}), ("x2", {"action": "offer", "target": 1, "give": {}, "want": {}}),
                      ("x3", {"action": "offer", "give": {"coin": 1}, "want": {"food": 1}}),
                      ("x4", {"action": "offer", "target": 1, "give": {"coin": 0}, "want": {"food": 1}}),
                      ("x5", {"action": "offer", "target": 1, "give": {"coin": 1}, "want": {"things": ["flower"]}}),
                      ("x6", {"action": "rest", "give": {"coin": 1}, "want": {}}),
                      ("x7", {"action": "offer", "target": 1, "give": {"coin": 1}, "want": "food"})):
        assert submit(r, iid, args)["reason"] == "bad_args", iid
    assert r.verbs()["act"]["give"] == ["food", "coin", "things"] == r.verbs()["act"]["want"]
    to = None
    for _ in range(40):
        near_by = [o for o in sim.state["agents"] if o["id"] != b and sim.reach(me, o) and not o["asleep"]]
        if near_by:
            to = near_by[0]
            break
        r.step()
    assert to is not None, "nobody came within reach"
    n = len(sim.events)
    assert submit(r, "o1", {"action": "offer", "target": to["id"], "give": {"coin": 1}, "want": {}}) is None
    (iid, ack, seen, view), = r.take_due()  # a gift: answered before the tick's step, with the body's new view
    assert iid == "o1" and ack["status"] == "applied" and [e["kind"] for e in seen] == ["act", "deal", "receipt"]
    assert view["coin"] == me["inv"]["coin"] and to["id"] in view["offer"]["offer"]
    r.step()
    give = {"coin": min(2, me["inv"]["coin"])} if me["inv"]["coin"] else {}
    want = {"coin": 1}
    assert submit(r, "o2", {"action": "offer", "target": to["id"], "give": give, "want": want}) is None
    (iid, ack, seen, view), = r.take_due()
    assert iid == "o2" and [e["kind"] for e in seen] == ["act", "offered", "receipt"]  # the roll is not shown
    assert [e.kind for e in sim.events if e.id >= seen[0]["id"]][:4] == ["act", "offered", "decided", "receipt"]
    r.step()
    assert any(e.kind == "reply" and e.data.get("answer") in ("yes", "no") and e.actor == to["id"] for e in sim.events[n:])
    r.finish()
    cfg, log = persist.config(out), read_jsonl(os.path.join(out, "events.jsonl"))
    pop = generate_population(cfg["seed"], cfg["agents"])
    pop["stakes"] = stakes()
    twin = Sim(cfg["seed"], cfg["agents"], StubMind(), genesis=pop)  # the same world in one go, by run.json's schedule
    twin.run_until(log[-1].tick, {t: [a for tt, a in cfg["player"] if tt == t] for t, _ in cfg["player"]})
    twin.finish()
    assert [e.to_json() for e in twin.events] == [e.to_json() for e in log]
    assert sum(1 for _, a in cfg["player"] if a.get("action") == "offer") == 2
