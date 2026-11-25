"""verbs-1 (plan/v0-contracts-spec.md B4): one table of verbs, and a pure move.

Every list the engine, the reply schema, the runner and the client read is the literal it replaced, value for
value and in its order. The log and every prompt are byte-identical: the engine's goldens and the pinned
player worlds hold as they did (tests/test_world.py, test_space.py, test_text1.py, test_think1.py,
test_pace2.py), and a bank of the player's own inputs from two real sessions, aipt3 and pt11, builds the
prompts it built before the move. Every verb a mind may choose has its words, and every reason a receipt can
carry has its words."""
import collections
import hashlib
import json
import os
import random

import pytest

from asiimov import llm, memory, narrate, things, verbs
from asiimov import sim as S
from asiimov.minds import StubMind
from asiimov.params import generate_population
from asiimov.sim import SUB, TICKS_PER_DAY, Sim
from asiimov.verbs import REASONS, VERBS
from test_client_words import source_whys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Each list as it stood in sim.py and llm.py before the move (reach-1), in its order.
BEFORE = {
    "DELIBERATE_ACTIONS": ("buy_food", "go_market", "ask_kin", "ask_food", "buy_from", "steal", "give_food", "give_coin",
                           "seek", "avoid", "work", "socialize", "home", "rest", "explore", "wait"),
    "PLAYER_ACTIONS": ("buy_food", "go_market", "ask_kin", "ask_food", "buy_from", "steal", "give_food", "give_coin",
                       "seek", "avoid", "work", "socialize", "home", "rest", "explore", "wait", "agree", "decline",
                       "go_to", "say", "walk", "hail"),
    "ACTIVITIES": ("work", "socialize", "home", "rest", "explore", "shop"),
    "PERSON_STEPS": ("seek", "avoid", "give_food", "give_coin", "ask_kin", "ask_food", "buy_from", "steal"),
    "STEP_DOS": ("work", "socialize", "home", "rest", "explore", "shop",
                 "seek", "avoid", "give_food", "give_coin", "ask_kin", "ask_food", "buy_from", "steal"),
    "INSTANT_STEPS": ("ask_kin", "ask_food", "buy_from", "steal"),
    "RATED_ACTS": ("ask_kin", "ask_food", "buy_from", "steal", "buy_food"),
    "RULE_STEPS": ("trade",),
    "REACH_STEPS": ("seek", "give_food", "give_coin", "trade"),
    "GIFTS": ("give_food", "give_coin"),
    "ANSWER_ACTS": ("agree", "decline"),
    "GO_ACTS": ("go_to",), "SAY_ACTS": ("say",), "WALK_ACTS": ("walk",), "HAIL_ACTS": ("hail",),
}
UNORDERED = {"ASK_STEPS": ("ask_food", "buy_from", "ask_kin")}  # only ever asked "is it in"
SCHEMA_LISTS = {
    "NO_TARGET": ["buy_food", "go_market", "work", "socialize", "home", "rest", "explore", "wait"],
    "PERSON_ACTIONS": (("ask_kin", "kin_here"), ("ask_food", "present"), ("buy_from", "present"), ("steal", "present"),
                       ("give_food", "people"), ("give_coin", "people"), ("seek", "people"), ("avoid", "people")),
}
CLAUSES = {  # ACTION_CLAUSES as it stood, at a price
    "buy_food": "buy_food buys food if you are at the market, carry enough coin and it has some",
    "go_market": "go_market takes you to the market, where you can buy food if you carry enough coin and it has some",
    "ask_kin": "ask_kin asks a family member here for 1 food",
    "ask_food": "ask_food asks a person here for 1 food; they may say no",
    "buy_from": "buy_from offers a person here {price} coin for 1 food; they may say no",
    "steal": "steal tries to take 1 food from a person here, and you may be caught",
    "give_food": "give_food hands them 1 food",
    "give_coin": "give_coin hands them {price} coin",
    "seek": "seek finds a person and talks with them",
    "avoid": "avoid keeps you away from them",
    "work": "work goes to your work for a few hours",
    "socialize": "socialize spends a few hours with others",
    "home": "home takes you home for a few hours",
    "rest": "rest lasts a few hours",
    "explore": "explore lasts a few hours",
    "wait": "wait does nothing for now",
}
NARRATOR_DOING = {"work": "at work", "socialize": "passing the time", "home": "at home", "rest": "resting",
                  "explore": "wandering", "shop": "shopping for food", "sleep": "getting ready for bed"}


# ------------------------------------------------------------------ a pure move: the lists
def test_every_list_read_from_the_table_is_the_literal_it_replaced():
    for name, was in BEFORE.items():
        if name == "PLAYER_ACTIONS":  # things-1 added its four rows after them and deal-1 its one; nothing else has changed
            assert S.PLAYER_ACTIONS == verbs.PLAYER_ACTIONS == was + verbs.THING_ACTS + verbs.OFFER_ACTS
            continue
        assert getattr(S, name) == was == getattr(verbs, name), name
    for name, was in UNORDERED.items():
        assert set(getattr(S, name)) == set(was) and len(getattr(S, name)) == len(was), name
    assert llm.NO_TARGET == SCHEMA_LISTS["NO_TARGET"] and llm.PERSON_ACTIONS == SCHEMA_LISTS["PERSON_ACTIONS"]
    assert llm.ACTIVITIES == list(BEFORE["ACTIVITIES"])
    assert narrate.DOING == NARRATOR_DOING and set(narrate.ASK_ACTS) == {"ask_kin", "ask_food", "buy_from"}


def test_the_menu_is_the_clauses_as_they_were_at_any_price():
    for price in (0, 1, S.FOOD_PRICE, 7, 12, 144):
        want = {k: v.format(price=price) for k, v in CLAUSES.items()}
        assert S.action_clauses(price) == want and list(S.action_clauses(price)) == list(CLAUSES)
    assert S.ACTION_CLAUSES == S.action_clauses(S.FOOD_PRICE) and S.GIFT_COIN == S.FOOD_PRICE
    assert S.deliberate_ask(["buy_food", "wait"], 3) == (
        " Your action happens now; target is a person's #id or null. " + CLAUSES["buy_food"] + ". " + CLAUSES["wait"] + "."
        " To pursue something over hours or days, set goal and up to 3 more steps in then (these words),"
        " done in order; for a one-off, leave goal empty and then []."
        " A new goal replaces the intention you have now; to keep it, leave goal empty.")


def test_a_verbs_cost_is_what_time_1b_charged():
    free = {"say", "agree", "decline", "walk", "hail"}
    for name in S.PLAYER_ACTIONS:
        assert S.act_cost(name) == (0 if name in free else S.COST) == verbs.cost(name), name
        assert ("free" in VERBS[name]["needs"]) == (name not in free), name  # the body must be free exactly when it costs
        assert "awake" in VERBS[name]["needs"], name
    assert verbs.MINUTE == S.COST == SUB // 10


# ------------------------------------------------------------------ the table itself
def test_every_row_is_whole():
    ranks = [row["step"] for row in VERBS.values() if row["step"] is not None]
    assert sorted(ranks) == list(range(1, len(ranks) + 1))  # a step's rank is its place in STEP_DOS
    for name, row in VERBS.items():
        assert row["who"] and set(row["who"]) <= {"mind", "player", "rule"}, name
        assert row["takes"] in (None, "person", "place", "tile", "line", "thing", "gift", "offer"), name
        assert row["kind"] in ("now", "ask", "reach", "hold", "activity", "go"), name
        assert callable(getattr(Sim, row["handler"])), name  # the engine method that carries it out
        assert (row["among"] is not None) == (name in S.DELIBERATE_ACTIONS and row["takes"] == "person"), name
        assert row["starts"] is None or row["starts"] in S.STEP_DOS, name
        assert set(row["reasons"]) <= set(REASONS), (name, set(row["reasons"]) - set(REASONS))
        assert set(row["words"].get("told", {})) <= set(row["reasons"]), name  # the narrator tells only what can happen
    for group in (verbs.ANY_ACT, verbs.PLAYER_ONLY, verbs.OFFERED):
        assert set(group) <= set(REASONS)
    used = set(verbs.ANY_ACT) | set(verbs.PLAYER_ONLY) | set(verbs.OFFERED) | {w for r in VERBS.values() for w in r["reasons"]}
    assert used == set(REASONS), set(REASONS) - used  # and no reason has words but no verb


def test_every_verb_a_mind_may_choose_has_its_words_and_every_player_verb_its_label():
    for name in S.DELIBERATE_ACTIONS:
        w = VERBS[name]["words"]
        assert w["menu"].startswith(name + " ") and w["label"] and w["doing"], name
        assert ("%s" in w["label"]) == (VERBS[name]["takes"] == "person"), name
    for name in S.STEP_DOS:  # what a plan or a step may name reads as words in the plan panel, or names a person
        assert "plan" in VERBS[name]["words"] or VERBS[name]["takes"] == "person", name
    unlabelled = {name for name in S.PLAYER_ACTIONS if "label" not in VERBS[name]["words"]}
    # a click on the map, the Talk box and the walking keys; a thing's kind words its own labels (things-1)
    assert unlabelled == {"go_to", "say", "walk"} | set(verbs.THING_ACTS)
    for name in verbs.THING_ACTS:
        assert VERBS[name]["words"]["doing"] and name in things.client_words(), name


def test_every_reason_a_receipt_can_carry_has_its_words():
    assert not source_whys() - set(REASONS), sorted(source_whys() - set(REASONS))
    for why, words in REASONS.items():
        assert words["you"] and set(words) <= {"you", "to_you", "by_them"}, why
        assert "by_them" not in words or "%s" in words["by_them"], why


def _receipts(sim) -> list:
    out = []
    for e in sim.events:
        if e.kind == "receipt":
            who = "player" if sim.events[e.parents[0]].origin == "player" else "mind"
            out.append((e.data["action"], e.data["why"], who))
    return out


def _player_days(seed: int) -> Sim:
    sim, rng = Sim(seed, 24, StubMind()), random.Random(seed)
    sim.step([{"kind": "possess", "agent": 3}])
    for _ in range(2 * TICKS_PER_DAY):
        acts = []
        if rng.random() < 0.5:
            act = {"kind": "act", "action": rng.choice(S.PLAYER_ACTIONS)}
            takes = VERBS[act["action"]]["takes"]
            if takes == "place":
                act["place"] = rng.choice(sorted(sim.map["places"]))
            elif takes == "line":
                act.update(target=rng.randrange(24), text="Good day to you.")
            elif takes == "tile":
                act["to"] = [rng.randint(-8, 8), rng.randint(-8, 8)]
            elif takes == "person" and (act["action"] in S.HAIL_ACTS or rng.random() < 0.8):
                act["target"] = rng.randrange(24)
            elif takes in ("thing", "gift"):  # things-1: this world has none, and says so
                act.update(thing=rng.randrange(40), **({"target": rng.randrange(24)} if takes == "gift" else {}))
            elif takes == "offer":  # deal-1: a trade, an ask or a gift of food and coin, to anyone
                act.update(target=rng.randrange(24), give=rng.choice([{}, {"coin": 3}, {"food": 1}, {"coin": 40}]),
                           want=rng.choice([{"food": 1}, {"coin": 2}, {"food": 9}]))
            acts = [act] * (2 if rng.random() < 0.05 else 1)
        sim.step(acts)
    return sim


def test_each_receipt_carries_a_reason_its_verbs_row_lists():
    """Random player days, a village on its own and a world of hostile minds: every receipt's `why` is one its
    verb's row lists for who made the act."""
    import test_talk1
    worlds = [_player_days(seed) for seed in range(3)]
    for mind, n in ((StubMind(), 100), (test_talk1.Hostile(2), 40)):
        sim = Sim(2, n, mind)
        sim.run_until(2 * TICKS_PER_DAY)
        worlds.append(sim)
    seen = collections.Counter(r for sim in worlds for r in _receipts(sim))
    for (action, why, who), n in seen.items():
        if action not in VERBS:  # a mind's unparseable reply: its own text is the receipt's action
            assert why == "unparseable or unknown action" and who == "mind", (action, why)
            continue
        assert why in verbs.reasons(action, who), (action, why, who, n)
    assert len({a for a, _, _ in seen} & set(VERBS)) >= 22 and len({w for _, w, _ in seen}) >= 30, len(seen)


# ------------------------------------------------------------------ what the table declares, the engine does
def _body(seed=5):
    sim = Sim(seed, 24, StubMind())
    sim.run_until(TICKS_PER_DAY // 2)
    b = next(a["id"] for a in sim.state["agents"] if not a["asleep"] and a["place"] is not None
             and a["sleep_tick"] > sim.state["tick"] + 30)
    sim.step([{"kind": "possess", "agent": b}])
    return sim, b


def _act(sim, name, b, f=0):
    takes, other = VERBS[name]["takes"], (b + 1) % 24
    act = {"kind": "act", "action": name, "f": f}
    if takes == "place":
        act["place"] = "plaza"
    elif takes == "line":
        act.update(target=other, text="Good day.")
    elif takes == "tile":
        act["to"] = sim.tile(sim.state["agents"][b])
    elif takes == "person":
        act["target"] = other
    elif takes in ("thing", "gift"):
        act.update(thing=0, **({"target": other} if takes == "gift" else {}))
    elif takes == "offer":
        act.update(target=other, give={"coin": 1}, want={"food": 1})
    return act


def test_a_verb_that_needs_the_body_free_is_refused_busy_inside_the_cost_of_the_one_before():
    for name in S.PLAYER_ACTIONS:
        sim, b = _body()
        sim.take({"kind": "act", "action": "rest", "f": 0})
        eid = sim.take(_act(sim, name, b, f=verbs.cost("rest") - 1))
        got = [e.data["why"] for e in sim.events[eid:] if e.kind == "receipt" and e.parents[0] == eid]
        assert ("busy" in got) == ("free" in VERBS[name]["needs"]), (name, got)


def test_a_sleepers_act_is_refused_whatever_the_verb():
    for name in S.PLAYER_ACTIONS:
        sim, b = _body()
        me = sim.state["agents"][b]
        while not me["asleep"]:
            sim.step([])
        act = _act(sim, name, b)
        n = len(sim.events)
        sim.step([act])
        got = [e.data["why"] for e in sim.events[n:] if e.kind == "receipt" and e.actor == b and e.data["action"] == name]
        assert got and got[0] in ("asleep", "one act per tick") and "awake" in VERBS[name]["needs"], (name, got)


def test_a_mind_cannot_use_a_verb_that_is_not_a_minds():
    sim, b = _body()
    a = sim.state["agents"][(b + 1) % 24]
    for name, row in VERBS.items():
        if "mind" in row["who"]:
            continue
        n = len(sim.events)
        sim._resolve(a, {"action": name, "target": b}, 0)
        assert [(e.data["action"], e.data["why"]) for e in sim.events[n:] if e.kind == "receipt"] \
            == [(name, "unparseable or unknown action")], name
    n = len(sim.events)
    sim._resolve(a, {"action": ["wait"]}, 0)  # not even a word
    assert sim.events[n].data["why"] == "unparseable or unknown action" and sim.events[n].data["action"] == "['wait']"


# ------------------------------------------------------------------ generated from the table
def test_the_clients_words_are_the_file_the_table_writes():
    path = os.path.join(ROOT, "client", "godot", "scripts", "verb_words.gd")
    assert open(path, encoding="utf-8").read() == verbs.client_gd(), "run `python -m asiimov.verbs`"
    ui = open(os.path.join(ROOT, "client", "godot", "scripts", "inhabit_ui.gd"), encoding="utf-8").read()
    for name, table in verbs.client_words().items():
        if name != "PLAYER_ACTIONS":
            assert f"const {name} := VerbWords.{name}" in ui, name  # the client keeps no copy of its own
    words = verbs.client_words()
    assert set(words["WHY"]) == set(REASONS) and words["HERE_ONLY"] == list(S.INSTANT_STEPS)
    assert words["PLAYER_ACTIONS"] == list(S.PLAYER_ACTIONS) and words["RATED"] == list(S.RATED_ACTS)


def test_the_playtesters_verb_list_is_the_tables():
    doc = open(os.path.join(ROOT, "plan", "ai-playtest.md"), encoding="utf-8").read()
    assert verbs.player_list() in doc, "plan/ai-playtest.md: paste verbs.player_list() under 'What a player can do'"
    assert verbs.player_list().count("\n") == len(S.PLAYER_ACTIONS)


def test_the_narrators_lines_are_the_tables():
    told = {(name, why): line for name, row in VERBS.items() for why, line in row["words"].get("told", {}).items()}
    assert told[("buy_food", "bought")] == ("buy", "{a} bought food at the market")
    assert told[("ask_kin", "kin gave food")][2] == "{a}'s family gave them food"
    assert verbs.told("trade", "supplied") == ("routine", "{a} got {goods} from {who}, the {role}", None)
    assert verbs.told("steal", "stolen") is None and verbs.told(["x"], "gave") is None and verbs.told("nope", "gave") is None
    for (name, why), line in told.items():
        assert line[0] in narrate.WEIGHT and "{a}" in line[1], (name, why)
        assert len(line) == 2 or ("{who}" in line[1] and "{who}" not in line[2]), (name, why)


def test_the_narrator_tells_each_outcome_as_it_did():
    """narrate.Facts.feed as it read before the move, line for line, against the table's, for every
    outcome a verb can have (the old chain also named two it can't: a sale to an ask, a gift to a buy)."""
    def before(act, why, a, who, role):
        t, w = None, "standout"
        if act == "buy_food" and why == "bought":
            w, t = "buy", f"{a} bought food at the market"
        elif act == "buy_food" and why == "no coin":
            t = f"{a} hadn't the coin to buy food"
        elif act == "ask_kin" and why == "kin gave food":
            t = f"{who} in {a}'s family gave them food" if who else f"{a}'s family gave them food"
        elif act == "ask_kin" and why in ("kin refused", "kin has nothing to spare"):
            t = f"{a} asked {who or 'their family'} for food and got none"
        elif act in ("ask_food", "buy_from") and who and why in ("gave", "sold"):
            t = f"{who} gave {a} food when asked" if act == "ask_food" else f"{a} bought food from {who}"
            w = "standout" if act == "ask_food" else "buy"
        elif act in ("ask_food", "buy_from") and who and why in ("refused", "nothing to spare"):
            t = f"{a} asked {who} for food and was turned down"
        elif act == "give_food" and who and why == "gave":
            t = f"{a} gave food to {who}"
        elif act == "give_coin" and who and why == "gave":
            t = f"{a} gave coin to {who}"
        elif act == "trade" and who and why == "supplied":
            t, w = f"{a} got {S.TRADE_INPUT.get(role, 'goods')} from {who}, the {role}", "routine"
        return (w, t) if t else None

    def now(act, why, a, who, role):
        line = verbs.told(act, why)
        if not (line and (who or "{who}" not in line[1] or line[2])):
            return None
        return line[0], (line[1] if who or line[2] is None else line[2]).format(
            a=a, who=who, role=role, goods=S.TRADE_INPUT.get(role, "goods"))
    n = 0
    for act in list(VERBS) + ["nope"]:
        for why in list(REASONS) + ["nope"]:
            for who, role in ((None, ""), ("Bel Anlu", "farmer"), ("Cor Deka", "elder")):
                if act in VERBS and why not in VERBS[act]["reasons"]:
                    assert now(act, why, "Ari Vone", who, role) is None, (act, why, who)
                    continue
                assert now(act, why, "Ari Vone", who, role) == before(act, why, "Ari Vone", who, role), (act, why, who)
                n += before(act, why, "Ari Vone", who, role) is not None
    assert n >= 30


# ------------------------------------------------------------------ a pure move: the prompts
# Hashes written on reach-1 (commit 6301447), before the move. A log's mind events carry each prompt's hash,
# so a log that is byte-identical built byte-identical prompts.
BANK = json.load(open(os.path.join(ROOT, "tests", "data", "verbs_bank.json"), encoding="utf-8"))
BANK_LOGS = {"aipt3": "d074af528c4b5fa20e6314554aaeecd91dab0ce1a60bbd70d1a3cf3ecafcd5cd",
             "pt11": "b4ff31e7a6279d9140f2eba64cc4a1f10bb4ec0707b92abfbfd3e124f99b9f6d"}
BANK_MENUS = "113d0e048c2121436cdc967c92770b052a0e6f9a56ec38812b6b6e47e8890c81"


def test_the_golden_world_under_reach_1s_label_is_reach_1s_log(monkeypatch):
    from test_world import FIRE, GOLDEN, run
    monkeypatch.setattr(S, "ENGINE_VERSION", "reach-1")
    s = run(StubMind(), agents=20, days=2, seed=3, player=FIRE)
    assert hashlib.sha256("".join(e.to_json() + "\n" for e in s.events).encode()).hexdigest() == GOLDEN["reach-1"]


@pytest.mark.parametrize("name", sorted(BANK_LOGS))
def test_the_prompt_bank_a_real_sessions_inputs_build_the_prompts_they_built_before(monkeypatch, name):
    """The player's own acts and lines from aipt3 and pt11, at their ticks, in a world of stub minds with the
    session's seed, size and talk settings: every plan, deliberation, answer, private decision and reflection
    it asks for is the prompt reach-1 built."""
    from asiimov import replay as R
    monkeypatch.setattr(S, "ENGINE_VERSION", "reach-1")  # the label the pins were written under
    monkeypatch.setattr(R, "ENGINE_VERSION", "reach-1")
    cfg, pop = BANK["sessions"][name], None
    if cfg["talk"]:
        pop = generate_population(cfg["seed"], cfg["agents"])
        pop["stakes"] = {**json.loads(json.dumps(S.DEFAULT_STAKES)), "talk": cfg["talk"]}
    sim = Sim(cfg["seed"], cfg["agents"], StubMind(), genesis=pop)
    by = collections.defaultdict(list)
    for tick, act in cfg["player"]:
        by[tick].append(act)
    for tick in range(max(by) + 13):
        sim.step(by.get(tick, []))
    sim.finish()
    kinds = collections.Counter(e.kind for e in sim.events)
    assert kinds["mind_deliberate"] > 100 and kinds["mind_answer"] >= 16 and kinds["mind_plan"] >= 100 \
        and kinds["mind_reflect"] > 50 and kinds["said"] >= 16, kinds
    assert name != "aipt3" or kinds["mind_decide"] >= 10  # its world asks privately before a typed ask moves food
    assert all(e.mind["prompt_hash"] for e in sim.events if e.kind.startswith("mind_"))
    assert hashlib.sha256("\n".join(e.to_json() for e in sim.events).encode()).hexdigest() == BANK_LOGS[name]


class Recorder(StubMind):
    """A stub that hashes every prompt the engine hands a mind, in call order: the text built for a real mind
    (MindRequest.prompt, and an answer's `told` prompts), which is the same whatever mind then replies."""

    def __init__(self):
        self.h, self.kinds = hashlib.sha256(), collections.Counter()

    def decide(self, req):
        self.kinds[req.kind, (req.view.get("occasion") or {}).get("why")] += 1
        for text in [req.prompt] + [p for _, p in sorted((req.told or {}).items())]:
            self.h.update(f"{req.kind}|{req.agent_id}|{req.tick}|{len(text)}\n".encode() + text.encode() + b"\n")
        return super().decide(req)


# Written on reach-1 (commit 6301447), before the move (Steering 2026-11-23 22:00Z: goods-1a's early read
# counts on the final engine only if a world with no player builds byte-identical prompts, goods-1 R6).
NO_PLAYER_PROMPTS = {"plain": "81b7bceb2e778a841701c41381c975ed27f0c08603b5b73c2999dc2af4feac55",
                     "sick": "4422b4b166d6e2c4f6bef3a8e7c2e0083ba33d0ad1d954a3e9f36cc02f3708c9"}


@pytest.mark.parametrize("name", sorted(NO_PLAYER_PROMPTS))
def test_the_prompt_bank_a_world_with_no_player_builds_every_prompt_as_before(name):
    """Stub, seed 1, 100 villagers, 3 days, no player: every plan, deliberation and reflection prompt, byte
    for byte. `sick` is the same world with one forced case (villager 7 at tick 40), so the sick ask's
    prompt and what the neighbours' rule does about it are in the bank too (seed 1 draws no case in 3 days)."""
    pop = None
    if name == "sick":
        pop = generate_population(1, 100)
        pop["stakes"] = json.loads(json.dumps(S.DEFAULT_STAKES))
        pop["stakes"]["sick"]["cases"] = [[40, 7]]
    mind = Recorder()
    sim = Sim(1, 100, mind, genesis=pop)
    for _ in range(3 * TICKS_PER_DAY):
        sim.step([])
    sim.finish()
    asked = collections.Counter(kind for kind, _ in mind.kinds.elements())
    assert asked["plan"] == 300 and asked["deliberate"] > 400 and asked["reflect"] > 250, asked
    if name == "sick":
        assert mind.kinds["deliberate", "sick"] >= 20
        assert {e.data["action"] for e in sim.events if e.kind == "receipt"} >= {"help", "carry_on"}
    assert mind.h.hexdigest() == NO_PLAYER_PROMPTS[name]


def test_the_prompt_bank_every_menu_the_sessions_minds_were_offered_reads_as_before():
    menus = []
    for withheld in BANK["withheld"]:
        offer = [x for x in S.DELIBERATE_ACTIONS if x not in withheld]
        for price in (S.FOOD_PRICE, S.FOOD_PRICE + 1, 6):
            menus.append(S.deliberate_ask(offer, price))
        menus.append(memory.schema_text(offer))
    assert len(BANK["withheld"]) >= 6 and [] in BANK["withheld"]
    assert hashlib.sha256("\n".join(menus).encode()).hexdigest() == BANK_MENUS
