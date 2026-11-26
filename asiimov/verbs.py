"""verbs-1 (plan/v0-contracts-spec.md B4): one table of verbs.

Every thing a body can do is one row of VERBS: who may use it (a mind, the player, a rule), what it names,
what it needs, what it costs the body, which engine method carries it out, and its words: the line a mind's
menu says for it, the player's label for it, and the reasons its receipt can carry. The engine's lists of
actions, the minds' menu and reply schema, the runner's checks, the player's cost and the client's words are
all read from here, so a new verb is a new row.

verbs-1 is a pure move: every list below is, value for value and in the same order, the literal it replaced
(tests/test_verbs.py pins each one), so no log and no prompt changes.

A row:
  who      who may choose it. "mind": a mind may (its deliberation, where it has a `menu` line; a plan block
           or an intent step, where it has a `step` rank). "player": a player's act may name it. "rule": only
           a rule of the world starts it.
  takes    what it names: None, "person" (a villager's id), "place" (a place on the map), "tile" ([x, y]),
           "line" (a person and the words said to them), "thing" (a thing's id; things-1), "gift" (a thing
           and the person it is handed to) or "offer" (a person and the two sides of a trade; deal-1).
  among    for a mind's person verb, the ids its reply may name: "kin_here", "present" or "people".
  needs    what must hold for it to work. "awake" and "free" (the body is not held by an act before it) are
           the player's body's; "cooldown" holds a player's rolled act to the deliberation cooldown; the rest
           say what the handler checks: "reach" (the target at one place with the body, or within a tile),
           "sight", "kin", "market", "coin", "food", "ask" (an ask aimed at the body), "walker", "beside" (the
           thing on the body's tile or the next one), "carried" (the thing in its hands), "hands" (a hand free).
  cost     sim-minutes the act holds the player's body (B2.3). 0: words, answers, a walk, a call.
  kind     how it runs. "now": it settles where the body is when it applies. "ask": the same, and its
           target may say no. "reach": the body goes to its
           target and acts when both are there. "hold": it lasts a while (avoid). "activity": the body goes
           to the activity's place and stays some hours. "go": the body walks to a named place.
  step     its rank among what a plan's step may name (STEP_DOS, in the order the reply schema lists them);
           None if it can't be a step.
  starts   the step it starts when chosen as an action, if not itself (go_market starts a shop step).
  handler  the Sim method that carries it out, called as handler(body, act event id, action, target).
  words    menu: the mind's menu clause ({price}: the food price). label: the player's E-list label (%s:
           the target's name). short: the label on a person's row of buttons. doing: the act in a sentence
           ("Didn't work: ask for food, because ..."). asking, asked: "You asked %s for food", "asked you
           for food". plan, now: the activity in a plan and under way. seen: what the narrator says the body
           is visibly doing. told: the narrator's line for a receipt, by its why ({a}: who acted, {who}: the
           one it names, {goods} and {role}: what a supplier sells and its trade).
  reasons  the `why` its receipt can carry, beyond those any act of its user can (ANY_ACT, PLAYER_ONLY,
           OFFERED). Each is a key of REASONS, which holds its words.
"""

from . import things

MINUTE = 60  # a sim-minute in the tick's 600ths (sim.SUB // 10): the unit of a verb's cost


def _row(who, handler, *, takes=None, among=None, needs=(), cost=1, kind="now", step=None, starts=None,
         reasons=(), **words) -> dict:
    return {"who": who, "takes": takes, "among": among, "needs": needs, "cost": cost, "kind": kind, "step": step,
            "starts": starts, "handler": handler, "words": words, "reasons": reasons}


BOTH = ("mind", "player")
BODY = ("awake", "free")  # what every act with a cost needs of the player's body
ASKED_NOW = ("target not here", "target asleep", "already asked")  # an ask, where the target stands
# The narrator's lines for what happened (words.told): how much it stands out, the line, and for a line that
# names {who}, the line when the receipt names nobody (without one, such a receipt gets no line).
KIN_NONE = ("standout", "{a} asked {who} for food and got none", "{a} asked their family for food and got none")
GAVE_ASKED = ("standout", "{who} gave {a} food when asked")
BOUGHT_FROM = ("buy", "{a} bought food from {who}")
TURNED_DOWN = ("standout", "{a} asked {who} for food and was turned down")

# In the order a deliberation's prompt lists them, then the player's own, then what only a plan or a rule
# starts. PLAYER_ACTIONS, the menu and the reply schema read this order.
VERBS = {
    "buy_food": _row(BOTH, "_do_buy", needs=BODY + ("cooldown", "market", "coin"),
                     reasons=("bought", "had enough", "not at market", "no coin", "no stock"),
                     menu="buy_food buys food if you are at the market, carry enough coin and it has some",
                     label="Buy food at the market", doing="buy food",
                     told={"bought": ("buy", "{a} bought food at the market"),
                           "no coin": ("standout", "{a} hadn't the coin to buy food")}),
    "go_market": _row(BOTH, "_do_step", needs=BODY + ("coin",), kind="activity", starts="shop",
                      reasons=("heading to market",),
                      menu="go_market takes you to the market, where you can buy food if you carry enough coin and it has some",
                      label="Go to the market", doing="go to the market"),
    "ask_kin": _row(BOTH, "_do_person", takes="person", among="kin_here", kind="ask", needs=BODY + ("cooldown", "reach", "kin"),
                    step=11, reasons=ASKED_NOW + ("not kin", "kin has nothing to spare", "kin gave food", "kin refused"),
                    menu="ask_kin asks a family member here for 1 food",  # "here" since the map's place check
                    label="Ask %s (family) for food", short="ask family for food", doing="ask family for food",
                    asking="You asked %s for food", asked="asked you for food",
                    told={"kin gave food": ("standout", "{who} in {a}'s family gave them food", "{a}'s family gave them food"),
                          "kin refused": KIN_NONE, "kin has nothing to spare": KIN_NONE}),
    "ask_food": _row(BOTH, "_do_person", takes="person", among="present", kind="ask", needs=BODY + ("cooldown", "reach"),
                     step=12, reasons=ASKED_NOW + ("nothing to spare", "gave", "refused"),
                     menu="ask_food asks a person here for 1 food; they may say no",
                     label="Ask %s for food", short="ask for food", doing="ask for food",
                     asking="You asked %s for food", asked="asked you for food",
                     told={"gave": GAVE_ASKED, "refused": TURNED_DOWN, "nothing to spare": TURNED_DOWN}),
    "buy_from": _row(BOTH, "_do_person", takes="person", among="present", kind="ask", needs=BODY + ("cooldown", "reach", "coin"),
                     step=13, reasons=ASKED_NOW + ("nothing to spare", "no coin", "sold", "refused"),
                     menu="buy_from offers a person here {price} coin for 1 food; they may say no",
                     label="Buy food from %s", short="buy food", doing="buy food",
                     asking="You tried to buy food from %s", asked="wanted to buy food from you",
                     told={"sold": BOUGHT_FROM, "refused": TURNED_DOWN, "nothing to spare": TURNED_DOWN}),
    "steal": _row(BOTH, "_do_person", takes="person", among="present", needs=BODY + ("cooldown", "reach"),
                  step=14, reasons=("target not here", "target has no food", "stolen", "caught", "not in talk"),
                  menu="steal tries to take 1 food from a person here, and you may be caught",
                  label="Steal food from %s", short="steal food", doing="steal",
                  asking="You tried to steal food from %s", asked="tried to steal from you"),
    "give_food": _row(BOTH, "_do_step", takes="person", among="people", needs=BODY + ("reach", "food"), kind="reach",
                      step=9, reasons=("started", "gave", "no food to give", "time ran out", "not in talk"),
                      menu="give_food hands them 1 food",
                      label="Give food to %s", short="give food", doing="give food",
                      asking="You gave food to %s", asked="gave you food",
                      told={"gave": ("standout", "{a} gave food to {who}")}),
    "give_coin": _row(BOTH, "_do_step", takes="person", among="people", needs=BODY + ("reach", "coin"), kind="reach",
                      step=10, reasons=("started", "gave", "no coin to give", "time ran out", "not in talk"),
                      menu="give_coin hands them {price} coin",
                      label="Give a coin to %s", short="give a coin", doing="give a coin",
                      asking="You gave a coin to %s", asked="gave you a coin",
                      told={"gave": ("standout", "{a} gave coin to {who}")}),
    "seek": _row(BOTH, "_do_step", takes="person", among="people", needs=BODY + ("reach",), kind="reach", step=7,
                 reasons=("started", "time ran out"),
                 menu="seek finds a person and talks with them",
                 label="Go and find %s", short="go to them", doing="find someone", asked="came to see you"),
    "avoid": _row(BOTH, "_do_step", takes="person", among="people", needs=BODY, kind="hold", step=8,
                  reasons=("started",),
                  menu="avoid keeps you away from them",
                  label="Keep away from %s", short="keep away", doing="keep away"),
    "work": _row(BOTH, "_do_step", needs=BODY, kind="activity", step=1, reasons=("started", "works sick"),
                 menu="work goes to your work for a few hours",
                 label="Go to work", doing="go to work", plan="work", now="Working", seen="at work"),
    "socialize": _row(BOTH, "_do_step", needs=BODY, kind="activity", step=2, reasons=("started",),
                      menu="socialize spends a few hours with others",
                      label="Go and see people", doing="see people", plan="see people", now="Seeing people",
                      seen="passing the time"),
    "home": _row(BOTH, "_do_step", needs=BODY, kind="activity", step=3, reasons=("started", "stays home sick"),
                 menu="home takes you home for a few hours",
                 label="Go home", doing="go home", plan="go home", now="Staying", seen="at home"),
    "rest": _row(BOTH, "_do_step", needs=BODY, kind="activity", step=4, reasons=("started",),
                 menu="rest lasts a few hours",
                 label="Rest", doing="rest", plan="rest", now="Resting", seen="resting"),
    "explore": _row(BOTH, "_do_step", needs=BODY, kind="activity", step=5, reasons=("started",),
                    menu="explore lasts a few hours",
                    label="Explore", doing="explore", plan="explore", now="Exploring", seen="wandering"),
    "wait": _row(BOTH, "_do_wait", needs=BODY, reasons=("waited",),
                 menu="wait does nothing for now",
                 label="Wait", doing="wait"),
    # The player's alone: held to no offer, and never in a mind's menu.
    "agree": _row(("player",), "_do_answer", takes="person", needs=("awake", "ask"), cost=0,
                  reasons=("said yes", "no ask", "not together"),
                  label="Say yes to %s", short="give", doing="say yes"),
    "decline": _row(("player",), "_do_answer", takes="person", needs=("awake", "ask"), cost=0,
                    reasons=("said no", "no ask", "not together"),
                    label="Say no to %s", short="not now", doing="say no"),
    "go_to": _row(("player",), "_do_go_to", takes="place", needs=BODY, kind="go",
                  reasons=("heading there", "already here", "arrived", "time ran out")),
    "say": _row(("player",), "_do_say", takes="line", needs=("awake", "reach"), cost=0,
                reasons=("said", "unknown target", "target not here", "target asleep", "price changed",
                         "one act per tick"),
                short="say something", doing="say something"),
    "walk": _row(("player",), "_walk", takes="tile", needs=("awake",), cost=0, reasons=()),
    "hail": _row(("player",), "_do_hail", takes="person", needs=("awake", "sight", "walker"), cost=0,
                 reasons=("hailed", "already stopped", "unknown target", "target not in sight", "not walking"),
                 label="Call out to %s", short="call out", doing="call out to"),
    # things-1 (B3): what a player does with a thing. The thing's kind (things.THINGS) says whether it can
    # be carried and what using it does, and words the label; a fixture's use is the verb done there, whose
    # own row gives that receipt's reasons.
    "take": _row(("player",), "_do_thing", takes="thing", needs=BODY + ("beside", "hands"),
                 reasons=("took", "nothing there", "not in reach", "can't be carried", "hands full", "already carried"),
                 doing="pick it up"),
    "drop": _row(("player",), "_do_thing", takes="thing", needs=BODY + ("carried",),
                 reasons=("dropped", "nothing there", "not carried"), doing="put it down"),
    "use": _row(("player",), "_do_thing", takes="thing", needs=BODY + ("beside",),
                reasons=("picked", "nothing there", "not in reach", "no use", "not yours"), doing="use it"),
    "hand": _row(("player",), "_do_thing", takes="gift", needs=BODY + ("carried", "reach"),
                 reasons=("handed", "nothing there", "not carried", "unknown target", "target not here", "target asleep",
                          "target hands full"),
                 doing="hand it over"),
    # deal-1 (B5): the player offers a villager in reach a trade, `give` for `want`, each side counts of food
    # and coin and the ids of things. A limit refuses it at once, in words; nothing wanted is a gift and
    # moves at once; anything else is settled by the villager's nature (a roll, held for the day per exact
    # terms), which the villager then answers aloud, and a yes moves both sides in one `deal`.
    "offer": _row(("player",), "_do_offer", takes="offer", needs=BODY + ("reach",),
                  reasons=("offered", "gave", "unknown target", "target not here", "target asleep", "not yours to offer",
                           "not theirs to give", "nothing to spare", "bad terms", "hands full", "target hands full",
                           "too many offers", "already traded", "gone"),
                  label="Offer %s a trade", short="offer a trade", doing="offer a trade"),
    # A plan's block or an intent's step, never an action on the menu (go_market is).
    "shop": _row(("mind",), "_do_step", needs=("coin",), kind="activity", step=6,
                 reasons=("heading to market", "bought", "had enough", "no coin", "no stock", "time ran out"),
                 plan="shop", now="Shopping", seen="shopping for food"),
    # Only a rule starts these.
    "trade": _row(("rule",), "_trade", takes="person", needs=("reach", "coin"), kind="reach",
                  reasons=("supplied", "no coin", "refused", "time ran out"),
                  told={"supplied": ("routine", "{a} got {goods} from {who}, the {role}")}),
    "visit": _row(("rule",), "_neighbor_resolve", takes="person", reasons=("going to see them",)),
    "help": _row(("rule",), "_neighbor_resolve", takes="person", reasons=("taking them something",)),
    "keep_away": _row(("rule",), "_neighbor_resolve", takes="person", reasons=("keeping away",)),
    "carry_on": _row(("rule",), "_neighbor_resolve", takes="person", reasons=("carrying on",)),
}

# What a body is doing that is no verb: an activity only the world sets.
STATES = {"visit": {"plan": "visit someone", "now": "Visiting"},
          "sleep": {"plan": "sleep", "seen": "getting ready for bed"}}

# The reasons any act can be refused for, by who made it.
ANY_ACT = ("unparseable or unknown action", "unknown target")       # a mind's reply or a player's act
PLAYER_ONLY = ("asleep", "bedtime", "falling asleep", "busy", "released")  # the player's body can't act now
OFFERED = ("not offered", "cooldown")                              # a player's act held to a mind's offer

# Every `why` a receipt can carry, with its words for the one who acted ("you"), for the one it was aimed at
# ("to_you") and, by the target's name, for an act of yours a villager settled ("by_them", %s: their name).
REASONS = {
    "heading to market": {"you": "you head to the market"},
    "started": {"you": "you start"},
    "waited": {"you": "you wait"},
    "said": {"you": "they heard you"},
    "not in talk": {"you": "goods don't change hands in talk"},
    "bought": {"you": "you buy %d food"},
    "had enough": {"you": "you have enough food, so you buy none"},
    "not at market": {"you": "you aren't at the market"},
    "not kin": {"you": "they aren't family"},
    "one act per tick": {"you": "one line at a time; your first one was said"},
    "busy": {"you": "you're still busy with the last thing; try again in a moment"},
    "going to see them": {"you": "you go to see them"},
    "taking them something": {"you": "you take them something"},
    "keeping away": {"you": "you keep away"},
    "carrying on": {"you": "you carry on"},
    "supplied": {"you": "you buy what your work needs"},
    "not offered": {"you": "it wasn't possible any more by the time it came up"},
    "cooldown": {"you": "too soon after your last ask; wait a little"},
    "unknown target": {"you": "they aren't here any more", "by_them": "%s isn't here any more."},
    "target not here": {"you": "they aren't here any more", "to_you": "you weren't there any more",
                        "by_them": "%s isn't here any more."},
    "target asleep": {"you": "they're asleep", "to_you": "you were asleep", "by_them": "%s is asleep."},
    "target has no food": {"you": "they have no food", "to_you": "you had no food"},
    "kin has nothing to spare": {"you": "they have nothing to spare", "to_you": "you had nothing to spare",
                                 "by_them": "%s had nothing to spare."},
    "kin gave food": {"you": "they gave you food", "to_you": "you gave them food", "by_them": "%s gave you food."},
    "kin refused": {"you": "they refused", "to_you": "you said no", "by_them": "%s said no."},
    "no coin": {"you": "you have no coin", "to_you": "they couldn't pay"},
    "nothing to spare": {"you": "they have nothing to spare", "to_you": "you had nothing to spare",
                         "by_them": "%s had nothing to spare."},
    "refused": {"you": "they refused", "to_you": "you said no", "by_them": "%s said no."},
    "sold": {"you": "they sold it to you", "to_you": "you sold them 1 food"},
    "gave": {"you": "done", "to_you": "you gave them 1 food"},
    "stolen": {"you": "you got away with it", "to_you": "they stole food from you",
               "by_them": "You stole food from %s and got away with it."},
    "caught": {"you": "you were caught", "to_you": "you caught them stealing", "by_them": "%s caught you stealing."},
    "released": {"you": "you let go of the villager first"},
    "stays home sick": {"you": "you stay home sick"},
    "works sick": {"you": "you work while sick"},
    "said yes": {"you": "they get what they asked for"},
    "said no": {"you": "they're told no"},
    "no ask": {"you": "they have no ask of you now"},
    "not together": {"you": "you're no longer together"},
    "already asked": {"you": "they're still waiting for an answer"},
    "asleep": {"you": "you were asleep"},
    "bedtime": {"you": "it's bedtime, so you head home"},
    "falling asleep": {"you": "you're falling asleep"},
    "no food to give": {"you": "you have no food to give"},
    "no coin to give": {"you": "you have no coin to give"},
    "no stock": {"you": "the market has no food left"},
    "unparseable or unknown action": {"you": "that isn't something you can do"},
    "heading there": {"you": "you set off"},
    "already here": {"you": "you're already there"},
    "arrived": {"you": "you arrive"},
    "time ran out": {"you": "it took too long"},
    "price changed": {"you": "the price changed; check it and offer again"},
    "hailed": {"you": "they stop and wait for you"},
    "already stopped": {"you": "they're already waiting for you"},
    "not walking": {"you": "they aren't walking by"},
    "target not in sight": {"you": "they're too far to hear you"},
    "took": {"you": "you have it now"},
    "dropped": {"you": "it lies where you stood"},
    "handed": {"you": "they take it"},
    "picked": {"you": "you have %d more food"},
    "nothing there": {"you": "it isn't there any more"},
    "not in reach": {"you": "it's too far to reach; stand on it or beside it"},
    "can't be carried": {"you": "it can't be carried"},
    "hands full": {"you": "your hands are full; put something down first"},
    "already carried": {"you": "you're already carrying it"},
    "not carried": {"you": "you aren't carrying it"},
    "no use": {"you": "there's nothing to do with it"},
    "not yours": {"you": "it isn't yours to use"},
    "target hands full": {"you": "their hands are full"},
    "offered": {"you": "they heard your offer"},
    "not yours to offer": {"you": "you don't have what you offered"},
    "not theirs to give": {"you": "they don't have what you asked for"},
    "bad terms": {"you": "they want no dealings with you"},
    "too many offers": {"you": "they've heard enough offers from you today"},
    "already traded": {"you": "you've already made that trade with them today"},
    "gone": {"you": "you had walked away before they could answer"},  # an offer's deal (deal-1), never a receipt
}


def _names(test) -> tuple:
    return tuple(name for name, row in VERBS.items() if test(row))


def _steps(test=lambda row: True) -> tuple:
    return tuple(sorted((name for name, row in VERBS.items() if row["step"] is not None and test(row)),
                        key=lambda name: VERBS[name]["step"]))


# The engine's lists, read from the table (sim.py and llm.py import them under these names).
DELIBERATE_ACTIONS = _names(lambda r: "mind" in r["who"] and "menu" in r["words"])
PLAYER_ACTIONS = _names(lambda r: "player" in r["who"])
ACTIVITIES = _steps(lambda r: r["kind"] == "activity")                 # what a plan block can say
PERSON_STEPS = _steps(lambda r: r["takes"] == "person")                # intent steps that name a target
STEP_DOS = ACTIVITIES + PERSON_STEPS                                   # what an intent step can do
INSTANT_STEPS = _names(lambda r: r["step"] is not None and r["kind"] in ("ask", "now"))  # act on the tick the step starts
ASK_STEPS = _names(lambda r: r["kind"] == "ask")                       # a no to one doesn't end an intent
RATED_ACTS = INSTANT_STEPS + _names(lambda r: "cooldown" in r["needs"] and r["step"] is None)
RULE_STEPS = _names(lambda r: r["who"] == ("rule",) and r["kind"] == "reach")
REACH_STEPS = _steps(lambda r: r["kind"] == "reach") + RULE_STEPS    # go where the target is, then act there
GIFTS = _names(lambda r: r["kind"] == "reach" and "mind" in r["who"] and {"food", "coin"} & set(r["needs"]))
ANSWER_ACTS = _names(lambda r: "ask" in r["needs"])
GO_ACTS = _names(lambda r: r["takes"] == "place")
SAY_ACTS = _names(lambda r: r["takes"] == "line")
WALK_ACTS = _names(lambda r: r["takes"] == "tile")
HAIL_ACTS = _names(lambda r: "walker" in r["needs"])
THING_ACTS = _names(lambda r: r["takes"] in ("thing", "gift"))            # things-1: an act that names a thing
HAND_ACTS = _names(lambda r: r["takes"] == "gift")                        # and the person it goes to
OFFER_ACTS = _names(lambda r: r["takes"] == "offer")                      # deal-1: an act that names a trade's two sides
NO_TARGET = [name for name in DELIBERATE_ACTIONS if VERBS[name]["takes"] is None]   # the reply schema's lists
PERSON_ACTIONS = tuple((name, VERBS[name]["among"]) for name in DELIBERATE_ACTIONS if VERBS[name]["takes"] == "person")
MENU = {name: VERBS[name]["words"]["menu"] for name in DELIBERATE_ACTIONS}


def cost(action: str) -> int:
    """How long a player's act holds the body, in 600ths of a tick (B2.3)."""
    return VERBS[action]["cost"] * MINUTE


def menu(price: int) -> dict:
    """One factual clause per action a deliberation may be offered, at a food price: the clauses that name
    coin say the live price."""
    return {name: line.format(price=price) for name, line in MENU.items()}


SEEN = {**{name: row["words"]["seen"] for name, row in VERBS.items() if "seen" in row["words"]},
        **{name: s["seen"] for name, s in STATES.items() if "seen" in s}}  # the narrator: what a body is visibly doing


def told(action, why):
    """The narrator's line for a receipt: (weight, line, the line when it names nobody or None), or None."""
    row = VERBS.get(action) if isinstance(action, str) else None
    line = row["words"].get("told", {}).get(why) if row and isinstance(why, str) else None
    return None if line is None else (line[0], line[1], line[2] if len(line) > 2 else None)


def reasons(action: str, who: str) -> frozenset:
    """Every `why` a receipt for `action` can carry when `who` ("mind", "player" or "rule") made it."""
    row = VERBS[action]
    out = set(row["reasons"]) | set(ANY_ACT)
    if who == "player":
        out |= set(PLAYER_ONLY) | (set(OFFERED) if "mind" in row["who"] else set())
    return frozenset(out)


def client_words() -> dict:
    """The client's word tables, as inhabit_ui.gd names them."""
    def col(key):
        return {name: row["words"][key] for name, row in VERBS.items() if key in row["words"]}

    def state(key):
        return {**col(key), **{name: s[key] for name, s in STATES.items() if key in s}}

    def why(key):
        return {name: words[key] for name, words in REASONS.items() if key in words}
    return {"ACTION": col("label"), "SHORT": col("short"), "DOING": col("doing"), "ASKING": col("asking"),
            "ASKED": col("asked"), "VERB": state("plan"), "NOW": state("now"),
            "WHY": why("you"), "TO_YOU": why("to_you"), "BY_THEM": why("by_them"),
            "HERE_ONLY": list(INSTANT_STEPS), "RATED": list(RATED_ACTS),
            "PLAYER_ACTIONS": list(PLAYER_ACTIONS), "THING_ACTS": list(THING_ACTS), "OFFER_ACTS": list(OFFER_ACTS),
            # things-1: a thing's name and the label of each verb its kind allows (things.THINGS)
            **{"THING_" + key.upper(): words for key, words in things.client_words().items()}}


GD_HEAD = """# Written by `python -m asiimov.verbs` from the verb table (asiimov/verbs.py, verbs-1). Don't edit it here:
# change the table and run that again. tests/test_verbs.py fails when the two differ.
extends RefCounted
"""


def client_gd() -> str:
    """client/godot/scripts/verb_words.gd: the client's words, one constant a table."""
    import json
    out = [GD_HEAD]
    for name, value in client_words().items():
        if isinstance(value, dict):
            rows = ",\n".join(f"\t\t{json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}" for k, v in value.items())
            out.append(f"const {name} := {{\n{rows}}}")
        else:
            out.append(f"const {name} := {json.dumps(value)}")
    return "\n".join(out) + "\n"


# The player's verbs the client words in its own panels (the map, the Talk box, the keys), so they carry no label.
_UNLABELLED = {"go_to": "Walk to a place on the map", "say": "Say a line to someone", "walk": "Step to the next tile",
               "take": "Pick up a thing beside you", "drop": "Put down a thing you carry",
               "use": "Use a thing beside you or in your hands", "hand": "Hand a thing you carry to someone"}


def player_list() -> str:
    """What a player can do, as plan/ai-playtest.md lists it for the AI playtester: one line a verb."""
    lines = []
    for name in PLAYER_ACTIONS:
        row = VERBS[name]
        w = row["words"]
        what = (w.get("label") or _UNLABELLED[name]).replace("%s", "someone")
        names = {None: "", "person": ", naming a villager", "place": ", naming a place", "tile": ", naming a tile",
                 "line": ", naming a villager, with the words", "thing": ", naming a thing",
                 "gift": ", naming a thing and a villager",
                 "offer": ", naming a villager, what you give and what you want"}[row["takes"]]
        held = f"holds the body {row['cost']} sim-minute" if row["cost"] else "costs the body no time"
        lines.append(f"- `{name}`: {what}{names}; {held}.")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "client", "godot", "scripts", "verb_words.gd")
    if "--check" in sys.argv:
        sys.exit(0 if open(path, encoding="utf-8").read() == client_gd() else f"{path} is not what the table writes")
    with open(path, "w", encoding="utf-8") as f:
        f.write(client_gd())
    print(f"wrote {path}")
