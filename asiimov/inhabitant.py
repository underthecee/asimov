"""What an inhabitant client is sent (plan/player-body-spec.md, "Protocol 2: the inhabitant scope").

The server filters and the client is never trusted to hide anything. Everything here is built field by field
from a whitelist, never as a copy of engine state (Sim._view carries traits), and it only reads the world:
the live runner calls it between ticks, and test_inhabitant checks the log still equals batch.
"""
import json

from .sim import ANSWER_ACTS, OCCASION_TICKS, PERSON_STEPS, SUB, TALK_WHY, TICKS_PER_DAY
from .things import THINGS
from .worldmap import SEE, near

# The per-tick view's keys, as the spec lists them. A new field needs a spec line, and bar 5 checks this.
VIEW_KEYS = ("tick", "place", "route", "pos", "activity", "asleep", "hunger", "food", "coin", "plan", "recent",
             "present", "kin", "market_food", "occasion", "offer", "cooldown_until", "food_price", "intent", "asks",
             "seen", "things", "carried")
PUBLIC = frozenset({"fire"})            # events the whole village sees
NAMES = ("with", "to", "target", "household")  # data fields that name the villager an event is about
# Another villager's events it can see, and the fields of each: what it does in plain sight. Nothing that
# is that villager's mind (intent_*, plan_receipt), its body clock (day_rhythm) or its memory (sleep's counts).
SEEN = {"activity": ("activity", "place", "arrive", "from"), "arrive": ("place",), "wake": ("place",),
        "sleep": (), "talk": ("with", "place", "gossip"), "meet": ("with", "place"), "snub": ("with",),
        "eat": ("household",), "sell": ("food",), "fire": ("family",), "ask": ("action", "to"),
        "said": ("with", "text", "place", "move", "price"), "reply": ("with", "says", "answer"),  # say-1, talk-1
        "deal": ("move", "from", "to", "food", "coin", "gives", "gets", "ok", "why"),  # talk-1, deal-1: what a yes moved, in plain sight
        "offered": ("with", "gives", "gets", "place"),  # deal-1: a keyed offer, put to someone in plain sight
        "halt": ("pos", "place", "depart", "arrive")}  # reach-1: a walker the player hailed or spoke to stops
SEEN_NAMING = {**SEEN, "receipt": ("action", "ok", "why", "to", "answered")}  # also a receipt for an act aimed at it
# Another's act aimed at it that failed before reaching it: it saw nothing (and "no coin" would show another's
# purse). Kept when it settles a held ask (parents beyond the act: the ask), which the villager did see.
UNSEEN_WHY = ("target not here", "target asleep", "no coin")
# reach-1 (Steering 2026-11-23 20:26Z): what sight alone shows of someone who is neither at its place nor in
# reach: who stands where, and that someone spoke, without the words. No activity, sleep, meal, sale or deal.
SIGHT = {"activity": ("place", "arrive", "from"), "arrive": ("place",), "halt": ("pos", "place", "depart", "arrive"),
         "talk": ("with", "place"), "said": ("with", "place"), "reply": ("with",), "offered": ("with", "place")}


def _pos(sim, a) -> list[int]:
    return sim.tile(a)  # the engine's own (space-1): its slot at a place, a point on a walk, or the tile the player walked it to


def present_ids(sim, aid: int) -> list[int]:
    """Who is at the villager's place now, awake or asleep (walkers are nowhere, as in the engine)."""
    agents = sim.state["agents"]
    place = agents[aid]["place"]
    return [] if place is None else [o["id"] for o in agents if o["place"] == place and o["id"] != aid]


def sight_ids(sim, aid: int) -> list[int]:
    """Everyone the villager sees now (space-1, B1.4): those at its place, and, when it or they are out in
    the open, those within SEE tiles. Two places hide each other."""
    agents = sim.state["agents"]
    return [o["id"] for o in agents if o["id"] != aid and sim.sees(agents[aid], o)]


def near_ids(sim, aid: int) -> list[int]:
    """Those whose words and doings the villager takes in (reach-1): at its place, or within reach across open
    ground. The rest of those in sight are seen standing or walking, and no more."""
    agents = sim.state["agents"]
    return [o["id"] for o in agents if o["id"] != aid and sim.reach(agents[aid], o)]


def passing(sim, aid: int) -> dict:
    """reach-1: the walkers in sight of the villager at some time in the coming tick, each with `in`, the span
    it is in sight for ([first, last], in SUBths of the tick: sim-seconds), and `track`, the tiles it is seen
    on then, [f, x, y] a tile or so apart. Where every walker will be until the next step is known, so one
    that walks by while the body stands is seen as it passes and can be drawn, and nothing of it outside
    that span is told. Sampled every 5 sim-seconds, a third of a tile of walking."""
    agents, n0 = sim.state["agents"], sim.state["tick"] * SUB
    a, out = agents[aid], {}
    for o in agents:
        if o["id"] == aid or o["route"] is None:
            continue
        fs = [f for f in range(0, SUB, 5) if near(sim.tile_n(a, n0 + f), sim.tile_n(o, n0 + f), SEE)]
        if fs:
            run = [fs[0]]  # the first unbroken stretch: it comes into sight once as it passes
            for f in fs[1:]:
                if f != run[-1] + 5:
                    break
                run.append(f)
            ks = sorted(set(run[::3]) | {run[-1]})
            track = [[f] + sim.tile_n(o, n0 + f) for f in ks]
            last = len(track) - 1  # one that stands a while is told once at each end of its stand
            out[o["id"]] = {"in": [run[0], min(run[-1] + 4, SUB - 1)],
                            "track": [p for i, p in enumerate(track) if i in (0, last)
                                      or p[1:] != track[i - 1][1:] or p[1:] != track[i + 1][1:]]}
    return out


def _carries(sim, a, o) -> dict:
    """deal-1: the things someone in reach holds in their hands, in plain sight, so an offer can name one."""
    if "things" not in sim.state or not sim.reach(a, o):
        return {}
    return {"carries": [{"id": r["id"], "kind": r["kind"]} for r in sim.carried(o["id"])]}


def _route(r) -> dict | None:
    return None if r is None else {"from": list(r["from"]), "to": r["to"], "depart": r["depart"], "arrive": r["arrive"]}


def welcome_world(sim, aid: int) -> dict:
    """The map, its own villager and its family. No souls, traits or params, not even its own."""
    a, agents = sim.state["agents"][aid], sim.state["agents"]
    fam = sim.state["families"][a["family"]]
    return {"map": sim.map, "ticks_per_day": TICKS_PER_DAY,
            "me": {"id": aid, "name": a["name"], "role": a["role"], "family": a["family"], "home": a["home"]},
            "people": [{"id": o["id"], "name": o["name"], "role": o["role"]} for o in agents],  # a village knows its names
            "family": [{"id": m, "name": agents[m]["name"], "role": agents[m]["role"]} for m in fam["members"] if m != aid]}


def view(sim, aid: int) -> dict:
    """The villager at the current view tick, as the spec's whitelist. `offer` is what a deliberation would
    be offered from here (Sim._view, _situation and _offer, as _player_deliberate computes it); the engine
    decides again when the act applies, after this tick's arrivals and meals, so an act can still be
    refused with "not offered"."""
    st = sim.state
    tick, agents, a = st["tick"], st["agents"], st["agents"][aid]
    fam = st["families"][a["family"]]["members"]
    here = present_ids(sim, aid)
    going, sight = passing(sim, aid), sight_ids(sim, aid)
    r = a["route"]
    out = {"tick": tick, "place": a["place"],
           "route": _route(r),
           "pos": _pos(sim, a), "activity": a["activity"], "asleep": a["asleep"], "hunger": a["needs"]["hunger"],
           "food": a["inv"]["food"], "coin": a["inv"]["coin"],
           "plan": [{"activity": b["activity"], "start_h": b["start_h"], "end_h": b["end_h"]} for b in a["plan"] or []],
           "recent": list(a["recent"]),
           "present": [{"id": o, "name": agents[o]["name"], "role": agents[o]["role"], "kin": o in fam,
                        "has_food": agents[o]["inv"]["food"] > 0, "asleep": agents[o]["asleep"],  # say-1: who can hear you
                        "spare": agents[o]["inv"]["food"] > 1,  # food beyond the one they keep: what an ask can reach
                        "pos": _pos(sim, agents[o]), **_carries(sim, a, agents[o])} for o in here],
           # space-1: those in sight who aren't here with it, across open ground. Each with what draws it: its
           # tile, and its route if it is walking. reach-1: one within a tile is in reach (`reach`), may be named
           # in an act, and shows whether it sleeps; the rest are seen standing or walking, and no more.
           # A walker carries `in`, the span of this tick (in 600ths) it is in sight for, and one that comes
           # into sight later in the tick is listed too, to be drawn as it passes.
           "seen": [{"id": o, "name": agents[o]["name"], "role": agents[o]["role"],
                     "place": agents[o]["place"], "pos": _pos(sim, agents[o]), "route": _route(agents[o]["route"]),
                     **going.get(o, {}),
                     **({"reach": True, "asleep": agents[o]["asleep"], **_carries(sim, a, agents[o])}
                        if sim.reach(a, agents[o]) else {})}
                    for o in sight if o not in here]
           + [{"id": o, "name": agents[o]["name"], "role": agents[o]["role"], "place": None,
               "pos": g["track"][0][1:], "route": None, **g}  # not in sight yet: where it will first be seen
              for o, g in going.items() if o not in sight and o not in here],
           "kin": [{"id": m, "name": agents[m]["name"], "home": agents[m]["home"]} for m in fam if m != aid],
           "occasion": None, "offer": {}, "cooldown_until": a["cooldown_until"], "food_price": sim._price(),
           "intent": None,
           "asks": [{"from": q["asker"], "name": agents[q["asker"]]["name"], "action": q["action"], "until": q["until"],
                     "says": says(sim.events, _ev(sim.events[q["event"]]), aid)}
                    for q in st.get("player", {}).get("asks", [])
                    if st["player"]["agent"] == aid and q["until"] > tick]}  # one at `until` times out first
    it = a["intent"]
    if it:  # its own mind's words: what this body meant to do (Steering, 2026-11-10; never another's)
        out["intent"] = {"goal": it["goal"], "i": it["i"],
                         "steps": [{"do": st["do"], "target": st.get("target")} for st in it["steps"]]}
    if a["place"] == "market":
        out["market_food"] = st["market_food"]
    occ = a["occasion"]
    if occ and tick - occ["tick"] < OCCASION_TICKS:
        out["occasion"] = {"why": occ["why"], "who": occ["who"],
                           "text": sim._occasion_text(a, occ).removeprefix(" Why you stop to think: ").rstrip(".")}
    if not a["asleep"]:
        occupancy = {a["place"]: [aid] + here} if a["place"] is not None else {}
        v = sim._view(a, occupancy, reach=True)  # reach-1: the people it may name are those in reach
        if out["occasion"]:
            v["occasion"] = {k: occ.get(k) for k in ("why", "who", "detail")}
        v["offer"] = sim._offer(a, v)
        sim._situation(a, v, "deliberate")  # fills v["people"]; may cut a crowd, as for a mind
        out["offer"] = {x: (v["people"] if x in PERSON_STEPS else []) for x in sim._offer(a, v)}
        if out["asks"]:  # player-ask-1: answering an ask aimed at it, named by the asker
            out["offer"].update({x: [q["from"] for q in out["asks"]] for x in ANSWER_ACTS})
        # reach-1: who it can call to as they pass this tick: a walker still on its way that hasn't been stopped
        hail = [o for o, g in going.items() if not sim._halted(agents[o])
                and any(sim.can_hail(a, agents[o], tick * SUB + k[0]) is None for k in g["track"])]
        if hail:
            out["offer"]["hail"] = hail
    if "things" in st:  # things-1 (B3.5): the things in sight, what the body carries, and what it can do with them
        mine = sim.carried(aid)
        lying = [r for r in st["things"]["list"]  # a fixture is a map feature, always drawn (Steering 22:03Z)
                 if r["at"] is not None and (THINGS[r["kind"]]["stands"] or sim.sees_thing(a, r))]
        out["things"] = [{"id": r["id"], "kind": r["kind"], "pos": list(r["at"]),
                          **({"reach": True} if sim.beside(a, r, tick * SUB) else {}),
                          **({"yours": True} if THINGS[r["kind"]]["own"] and sim.thing_owner(a, r) else {})}
                         for r in lying]
        out["carried"] = [{"id": r["id"], "kind": r["kind"]} for r in mine]
        if not a["asleep"]:
            beside = [r for r in lying if sim.beside(a, r, tick * SUB)]
            use = [r["id"] for r in beside + mine
                   if THINGS[r["kind"]]["use"] and (r["holder"] == aid or sim.thing_owner(a, r))
                   and THINGS[r["kind"]]["use"].get("verb", "") in ("", *out["offer"])]
            take = [r["id"] for r in beside if THINGS[r["kind"]]["carry"]] \
                if len(mine) < st["stakes"]["things"]["carry"] else []
            hand = [o for o in near_ids(sim, aid) if not agents[o]["asleep"]] if mine else []
            for verb, ids in (("take", take), ("use", use), ("drop", [r["id"] for r in mine]), ("hand", hand)):
                if ids:
                    out["offer"][verb] = ids
    if not a["asleep"]:  # deal-1: whom it can put an offer to: anyone awake in reach (the engine's limits say the rest)
        to = [o for o in near_ids(sim, aid) if not agents[o]["asleep"]]
        if to:
            out["offer"]["offer"] = to
    assert set(out) <= set(VIEW_KEYS), "a view field outside the spec's list"
    return out


def cant_give(log, e: dict, aid: int) -> dict:
    """aipt3 (Steering 2026-11-20 09:07Z): why a villager answering `aid` could give nothing, as the engine told that
    villager's own prompt: `why`, a TALK_WHY key (and the `price` a "coin" names). Read from the answer's mind event,
    the reply's parent, and only what the engine withheld: a villager's own no has no reason here, its words are
    its reason, and "undecided" is no reason either. {} for any other event."""
    if e["kind"] != "reply" or e["data"].get("with") != aid or not e["parents"]:
        return {}
    d = log[e["parents"][0]].data
    deal = d.get("deal") or {}
    why = (d.get("typed") or {}).get("withheld") or deal.get("withheld")
    if why not in TALK_WHY:
        return {}
    return {"why": why, **({"price": deal["price"]} if why == "coin" and "price" in deal else {})}


def _ev(e) -> dict:
    return {"actor": e.actor, "kind": e.kind, "data": e.data, "parents": e.parents}


def witnessed(e: dict, aid: int, here: set, tick: int, near: set | None = None) -> bool:
    """Whether the villager would witness this event: its own, one that names it, a public one, or (on the
    tick it happens, live) one by someone in its sight (`here`: at its place, or seen across open ground, space-1).
    reach-1: of someone in sight who isn't in `near` (at its place or in reach; all of `here` when left out)
    only what SIGHT lists. Never a mind event: its own plan is in the view."""
    if e["origin"] == "mind" or e["kind"] in ("genesis", "checkpoint", "end"):
        return False
    if e["actor"] == aid or e["kind"] in PUBLIC:
        return True
    if e["kind"] in SEEN_NAMING and any(e["data"].get(k) == aid for k in NAMES):
        return not (e["kind"] == "receipt" and e["data"].get("why") in UNSEEN_WHY and len(e["parents"]) < 2)
    if e["kind"] not in SEEN or e["tick"] != tick or e["actor"] not in here:
        return False
    return near is None or e["actor"] in near or e["kind"] in SIGHT


# Acts aimed at the villager whose actor's own words it is told (Steering, 2026-11-10, joon's "show their
# reasons as words"): an ask, a gift, a sale asked for, a visit. Not a theft, an avoid or a snub: those aren't
# said to anyone's face. Refusals of the villager's own asks are the other's nature (a roll), with no words.
SAYS_ACTS = ("ask_kin", "ask_food", "buy_from", "give_food", "give_coin")
SAYS_MAX = 200     # characters of a reason shown
SAYS_DEPTH = 4     # parent links walked from the act to the actor's own mind event (act or step: 1 or 2)


def says(log, e: dict, aid: int) -> str | None:
    """What the actor of an act aimed at `aid` said of it, from its own mind reply. The reply's `reason` is
    shown only when the reply's own action and target are this act: a reason is written for the reply's
    first action, which may be aimed at someone else. An act that is a step of the reply's intent shows the
    intent's `goal` only (review on #54). Only the actor's own events are walked (parents[0]), one field of
    one reply is read, and None when there is no mind reply (a rule or rolled act)."""
    d = e["data"]
    if e["actor"] is None or e["actor"] == aid:
        return None
    aimed = (e["kind"] == "ask" and d.get("to") == aid) or (e["kind"] == "meet" and d.get("with") == aid) \
        or (e["kind"] == "receipt" and d.get("to") == aid and d.get("action") in SAYS_ACTS)
    if not aimed or (e["kind"] == "receipt" and any(log[i].kind == "ask" for i in e["parents"][1:])):
        return None  # a held ask's answer: its words were on the ask
    act = "seek" if e["kind"] == "meet" else d.get("action")
    step, i = False, (e["parents"] or [None])[0]
    for _ in range(SAYS_DEPTH):
        if i is None or not 0 <= i < len(log) or log[i].actor != e["actor"]:
            return None
        p = log[i]
        if p.origin == "mind":
            if any(0 <= j < len(log) and log[j].kind == "said" for j in p.parents):
                # say-3 (review of #78): an answer to the player's line keeps its reason and goal private, and its
                # words are already on the `reply` event, so its acts carry none of their own.
                return None
            out = _parse(p.mind or {}) or {}
            own = not step and out.get("action") == act and out.get("target") == aid
            for k in (("reason", "goal") if own else ("goal",)):
                if isinstance(out.get(k), str) and out[k].strip():
                    return " ".join(out[k].split())[:SAYS_MAX]
            return None
        step = step or p.kind.startswith("intent_")
        i = (p.parents or [None])[0]
    return None


def _parse(mind: dict) -> dict | None:
    try:
        out = json.loads(mind.get("raw_output") or "")
    except (ValueError, RecursionError):
        return None
    return out if isinstance(out, dict) else None


def scrub(e: dict, aid: int, log=None, near: set | None = None) -> dict:
    """An event as an inhabitant gets it: no mind record (prompt hash, raw output), no chance-stream draws,
    and of another villager's event only the fields SEEN lists, plus (given the log) `says`: the actor's own
    words for an act aimed at this villager. reach-1: given `near`, an event by someone outside it that doesn't
    name this villager carries only what SIGHT lists: that it moved or spoke, never the words."""
    if e["actor"] == aid:  # its own, with its own intent's goal and steps (what it meant to do)
        data = {k: v for k, v in e["data"].items() if k not in ("stream", "index", "pick_stream", "jitter_min")}
    elif near is not None and e["actor"] not in near and e["kind"] in SIGHT \
            and not any(e["data"].get(k) == aid for k in NAMES):
        data = {k: e["data"][k] for k in SIGHT[e["kind"]] if k in e["data"]}
    else:
        data = {k: e["data"][k] for k in SEEN_NAMING[e["kind"]] if k in e["data"]}
        said = says(log, e, aid) if log is not None else None
        if said:
            data["says"] = said
        if log is not None:
            data.update(cant_give(log, e, aid))
    return {"id": e["id"], "tick": e["tick"], "kind": e["kind"], "origin": e["origin"], "actor": e["actor"],
            "data": data, "parents": e["parents"]}
