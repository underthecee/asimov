"""World invariants and the food/coin ledger, checked tick by tick.

Shared by the property tests (tests/test_invariants.py, which assert there are none) and the long
unattended run (asiimov/soak.py, which counts them and keeps going). Each check returns a list of
violation strings instead of raising, so a long run can report every one it hits.
"""
from .events import ORIGINS, Event
from .sim import DAWN, PERSON_STEPS, RULE_STEPS, STEP_DOS, SUB, TICKS_PER_DAY, Sim, offer_empty, offer_side_ok
from .things import FIXTURES, GROWN, THINGS
from .worldmap import REACH, SEE, near, on_map, own_tiles, place_at, slot, tile_at, travel_ticks

BOOL_FIELDS = ("asleep", "has_food", "kin", "talk")  # talk: an answer's own hold (say-3)


def type_violations(x, path="state") -> list[str]:
    """Floats anywhere, and bools outside the fields that are meant to hold them."""
    if isinstance(x, bool):
        return [] if path.endswith(BOOL_FIELDS) else [f"bool at {path}"]
    if isinstance(x, float):
        return [f"float at {path}"]
    out = []
    if isinstance(x, dict):
        for k, v in x.items():
            out += type_violations(v, f"{path}.{k}")
    elif isinstance(x, list):
        for i, v in enumerate(x):
            out += type_violations(v, f"{path}[{i}]")
    return out


def longest_trip(m: dict) -> int:
    """No walk on map `m` takes longer than this many ticks: the anchors' bounding box, corner to corner."""
    xs, ys = zip(*(p["anchor"] for p in m["places"].values()))
    return travel_ticks((min(xs), min(ys)), (max(xs), max(ys)))


def map_violations(sim: Sim, a: dict, longest: int) -> list[str]:
    """An agent is at a place on the map or walking, never both; a walk's arrival is in the future and
    exactly its travel time after it set out; sleepers are at home. Checked between ticks."""
    places, aid, r = sim.map["places"], a["id"], a["route"]
    if "at" in a:  # walk-1: a body the player walked stands on a tile, and its place is the one that holds the tile
        ok = (r is None and on_map(sim.map, a["at"]) and a["place"] == place_at(sim.map, a["at"])
              and (not a["asleep"] or a["place"] == a["home"]))
        return [] if ok else [f"agent {aid} at tile {a['at']!r}, place {a['place']!r}, route {r} (asleep {a['asleep']})"]
    if r is None:
        ok = (a["place"] in places and (not a["asleep"] or a["place"] == a["home"])
              and place_at(sim.map, sim.tile(a)) == a["place"])  # space-1: its slot is a tile of its own place
        return [] if ok else [f"agent {aid} at {a['place']!r} (asleep {a['asleep']})"]
    ok = (a["place"] is None and not a["asleep"] and r["to"] in places
          and sim.state["tick"] <= r["arrive"] <= r["depart"] + longest
          and r["arrive"] == r["depart"] + travel_ticks(r["from"], places[r["to"]]["anchor"])
          and on_map(sim.map, sim.tile(a)))  # space-1: a walker's tile is on the map
    return [] if ok else [f"agent {aid} place {a['place']!r} route {r}"]


# Events that need two agents together, and the field naming the other one. A receipt counts when it
# succeeded, or for a steal that was caught (it was attempted face to face).
TOGETHER = {"talk": "with", "meet": "with", "snub": "with"}


class Bodies:
    """reach-1: where every body stands, rebuilt from the log alone, so the presence check can hold an act
    between the player's body and someone in the open to reach (B1.3) without asking the engine. It follows
    who is possessed, each walker's route (an `activity` with its `from`, a `halt`), the tile a player
    walked its body to (`moved`), and the time inside the tick of the last player act (`f`), which is the
    time the engine judges reach at."""

    def __init__(self, sim: Sim):
        self.map = sim.map
        self.held = sim.state.get("player", {}).get("agent")
        self.routes = {a["id"]: {k: a["route"][k] for k in ("from", "to", "depart")}
                       for a in sim.state["agents"] if a["route"]}
        self.at = {a["id"]: list(a["at"]) for a in sim.state["agents"] if "at" in a}
        self.tick, self.f = sim.state["tick"], sim._f if sim._window == sim.state["tick"] else 0

    def see(self, e: Event, where: dict):
        """Take one event in, before `where` is updated for it."""
        d, a = e.data, e.actor
        if e.tick != self.tick:
            self.tick, self.f = e.tick, 0
        if e.kind == "possess":
            self.held = d["agent"]
        elif e.kind == "release":
            self.held = None
        elif e.kind == "act" and e.origin == "player":
            self.f = d.get("f", 0)
        elif e.kind == "activity":
            r = self.routes.get(a)
            if d["place"] != (r["to"] if r else where.get(a)):
                self.at.pop(a, None)  # it sets off from the tile the player had put it on
            if d.get("arrive", e.tick) == e.tick:
                self.routes.pop(a, None)
            elif "from" in d:
                self.routes[a] = {"from": d["from"], "to": d["place"], "depart": e.tick}
        elif e.kind == "halt":
            self.routes[a] = {"from": d["pos"], "to": d["place"], "depart": d["depart"]}
        elif e.kind in ("arrive", "wake"):
            self.routes.pop(a, None)
        elif e.kind == "moved":
            self.routes.pop(a, None)
            self.at[a] = list(d["pos"])

    def tile(self, aid: int, where: dict) -> list[int]:
        if aid in self.routes:
            r = self.routes[aid]
            return tile_at(r, self.map["places"][r["to"]]["anchor"], (self.tick - 1) * SUB + self.f, SUB)  # Sim.pos: t - 1
        return self.at[aid] if aid in self.at else slot(own_tiles(self.map, where[aid]), aid)

    def within(self, a: int, b: int, where: dict, tiles: int) -> bool:
        return self.held in (a, b) and near(self.tile(a, where), self.tile(b, where), tiles)


def presence_violations(events: list[Event], start: int, where: dict, bodies: Bodies | None = None,
                        things: "Things | None" = None) -> list[str]:
    """From the log alone, in event order: every talk, meet, snub, gift, ask, steal and buy happens
    between agents at the same place (never two walkers), and every buy at the market. `where` maps
    agent id to place (None while walking) as of events[start]; it is updated in place.

    reach-1: with `bodies` (the positions, also from the log alone), an act between the player's body and
    someone else may also happen with one of them in the open, when their tiles are within reach at the
    act's time, or within sight for a hail; and a `halt` stops a walker on the tile its route puts it on."""
    out = []
    for e in events[start:]:
        d, a = e.data, e.actor
        if bodies is not None:
            if e.kind == "halt" and (a not in bodies.routes or where.get(a) is not None
                                     or (bodies.see(e, where), bodies.tile(a, where))[1] != d["pos"]):
                out.append(f"event {e.id}: halt of {a}, who isn't walking there")
            elif e.kind != "halt":
                bodies.see(e, where)
            if things is not None:  # things-1: a thing is taken, put down and used from its own tile or the next
                out += things.see(e, bodies, where)
        if e.kind == "activity":
            where[a] = d["place"] if d.get("arrive", e.tick) == e.tick else None
        elif e.kind in ("arrive", "wake", "moved"):  # moved (walk-1): the place holding its tile, or None in the open
            where[a] = d["place"]
        other, tiles = d.get(TOGETHER.get(e.kind, "")), REACH
        if e.kind == "receipt" and (d["ok"] or d["why"] == "caught"):
            other = d.get("to", d.get("victim"))
            tiles = SEE if d["action"] == "hail" else REACH
            if d["action"] == "buy_food" and where.get(a) != "market":
                out.append(f"event {e.id}: bought away from the market")
        if other is None:
            continue
        if where.get(a) is not None and where.get(other) is not None:
            ok = where[a] == where[other]
        else:
            ok = bodies is not None and bodies.within(a, other, where, tiles)
        if not ok:
            out.append(f"event {e.id}: {e.kind} between {a} at {where.get(a)!r} and {other} at {where.get(other)!r}")
    return out


class Things:
    """things-1 (B3.3): the ledger of things rebuilt from the log alone. A thing is made only in a `things`
    or a `grew` event, goes only in a `used` one, and changes hands or tiles only in `took`, `dropped` and
    `handed`, or (deal-1) between the two sides of an offer's `deal` that moved; so each kind's total changes
    only there, and the state's ledger must equal this one."""

    def __init__(self, sim: Sim):
        self.all = {r["id"]: dict(r) for r in sim.state.get("things", {}).get("list", [])}
        self.made = "things" in sim.state
        self.next = sim.state.get("things", {}).get("next", 0)

    def see(self, e: Event, bodies: Bodies, where: dict) -> list[str]:
        """Take one event in; what is wrong with it. `bodies` has already seen it."""
        d, out = e.data, []
        if e.kind in ("things", "grew"):
            if e.kind == "things" and (self.made or not e.parents):
                out.append(f"event {e.id}: things made twice, or with no possess to cite")
            if e.kind == "grew" and (not self.made or e.tick % TICKS_PER_DAY != DAWN):
                out.append(f"event {e.id}: grew before any thing, or away from dawn")
            self.made = True
            for t in d["things"]:
                if t["id"] != self.next or (t["kind"] not in (GROWN if e.kind == "grew" else THINGS)):
                    out.append(f"event {e.id}: made thing {t}")
                self.all[t["id"]], self.next = {"id": t["id"], "kind": t["kind"], "at": list(t["at"]), "holder": None}, t["id"] + 1
            return out
        if e.kind == "deal" and d["move"] == "offer" and d["ok"]:  # deal-1: each side's things change hands
            for side, frm, to in (("gives", d["from"], d["to"]), ("gets", d["to"], d["from"])):
                for tid in d[side].get("things", ()):
                    r = self.all.get(tid)
                    if r is None or r["holder"] != frm:
                        out.append(f"event {e.id}: deal moved thing {tid} ({r}), which {frm} doesn't carry")
                    else:
                        r["holder"] = to
            return out
        if e.kind not in ("took", "dropped", "used", "handed"):
            return out
        r, a = self.all.get(d["thing"]), e.actor
        if r is None or r["kind"] != d["kind"] or a != bodies.held:
            return [f"event {e.id}: {e.kind} of thing {d['thing']} ({r}) by {a}"]
        tile = bodies.tile(a, where)
        beside = r["at"] is not None and near(tile, r["at"], REACH)
        if e.kind == "took":
            if not (beside and THINGS[r["kind"]]["carry"] and d["from"] == r["at"]):
                out.append(f"event {e.id}: took {r} from {tile}")
            r["at"], r["holder"] = None, a
        elif e.kind == "dropped":
            if not (r["holder"] == a and d["at"] == tile):
                out.append(f"event {e.id}: dropped {r} at {d['at']} from {tile}")
            r["at"], r["holder"] = list(d["at"]), None
        elif e.kind == "handed":
            if r["holder"] != a:
                out.append(f"event {e.id}: handed {r}, which {a} doesn't carry")
            r["holder"] = d["to"]
        else:
            use = THINGS[r["kind"]]["use"] or {}
            if not ((beside or r["holder"] == a) and d.get("food") == use.get("food")):
                out.append(f"event {e.id}: used {r} from {tile}")
            del self.all[d["thing"]]
        return out


def thing_violations(sim: Sim, things: Things) -> list[str]:
    """The state's ledger of things against the log's, and each record's shape: exactly one of a tile and a
    holder, a tile on the map, a fixture where it was put and never held, nobody carrying more than the cap."""
    st, out = sim.state, []
    if "things" not in st:
        return ["things in the log and none in the state"] if things.made or things.all else []
    cfg = st.get("stakes", {}).get("things")
    if cfg is None:
        return ["things in a world whose stakes have none"]
    have = st["things"]["list"]
    if [r["id"] for r in have] != sorted(things.all) or any(r != things.all[r["id"]] for r in have) \
            or st["things"]["next"] != things.next:
        out.append("the state's things and the log's differ")
    carried = {}
    for r in have:
        ok = r["kind"] in THINGS and (r["at"] is None) != (r["holder"] is None)
        if ok and r["at"] is not None:
            ok = on_map(sim.map, r["at"])
        elif ok:
            ok = type(r["holder"]) is int and 0 <= r["holder"] < len(st["agents"]) and r["kind"] not in FIXTURES \
                and THINGS[r["kind"]]["carry"]
            carried[r["holder"]] = carried.get(r["holder"], 0) + 1
        if not ok:
            out.append(f"thing {r}")
    out += [f"agent {aid} carries {n} things" for aid, n in sorted(carried.items()) if n > cfg["carry"]]
    return out


def deal_violations(events: list[Event], start: int = 0, possessed=None) -> list[str]:
    """talk-1 (spec §3): a `deal` that moved goods cites the listener's own `mind_answer`, whose request offered
    yes and whose recorded reply says yes, and the player's `said` line that carried the move; it moves exactly
    1 food, from a villager who is never the possessed body. (With yes the only answer offered, consent nature's
    roll or a held decision, the reply only words it.) A failed deal moves nothing, so cites only.

    text-1 (spec §4): a typed deal (`typed`) cites a line said with no move and an answer that was offered food and
    whose recorded reply holds gives: food. And wherever the answer rested on a held decision (`decided`), the deal
    cites that decision: the listener's own `mind_decide` whose recorded reply parses to yes, or its `decided` roll
    with answer yes, made about this speaker and this move. So no line moves food without a recorded yes that was
    made before, and apart from, the words."""
    import json

    def field(mind, key):
        try:
            out = json.loads(mind.mind["raw_output"])
            return out.get(key) if isinstance(out, dict) else None
        except (ValueError, AttributeError, TypeError, RecursionError):
            return None

    out = []
    for e in events[start:]:
        if e.kind != "deal":
            continue
        d = e.data
        if d["move"] == "offer":
            out += _offer_deal_violations(events, e)
            continue
        typed = "typed" in d
        said = next((events[p] for p in e.parents if events[p].kind == "said"), None)
        mind = next((events[p] for p in e.parents if events[p].kind == "mind_answer"), None)
        if said is None or (("move" in said.data or d["move"] != "asks_food") if typed
                            else said.data.get("move") != d["move"]):
            out.append(f"event {e.id}: deal without a line said with its move")
        if d.get("food") != 1 or e.actor != d["from"] or d["from"] == d["to"] or d["from"] == possessed \
                or (typed and "coin" in d):
            out.append(f"event {e.id}: deal {d}")
        if mind is None or mind.actor != d["from"]:
            out.append(f"event {e.id}: deal without the listener's answer")
            continue
        basis = mind.data.get("typed" if typed else "deal", {})
        if d["ok"] and typed:
            if not ("food" in basis.get("offered", []) and field(mind, "gives") == "food"):
                out.append(f"event {e.id}: typed deal on an answer that wasn't an offered give")
        elif d["ok"]:
            offered = mind.data.get("offered", [])
            if not ("yes" in offered and (field(mind, "answer") == "yes" or offered == ["yes"])):  # one answer offered is the answer
                out.append(f"event {e.id}: deal on an answer that wasn't an offered yes")
        if "decided" in basis or typed:
            dec = next((events[p] for p in e.parents if events[p].kind in ("mind_decide", "decided")), None)
            yes = dec is not None and (field(dec, "answer") if dec.kind == "mind_decide" else dec.data.get("answer")) == "yes"
            if dec is None or dec.id != basis.get("decided") or dec.actor != d["from"] or dec.data.get("with") != d["to"] \
                    or dec.data.get("move") != d["move"] or dec.id >= mind.id or (d["ok"] and not yes):
                out.append(f"event {e.id}: deal without the listener's own earlier yes to this speaker")
    return out


def _offer_deal_violations(events: list[Event], e: Event) -> list[str]:
    """deal-1 (B5.2b): an offer's `deal` is the offerer's (`from`), with two well-formed sides. A gift (nothing
    wanted) moved, and cites the player's own act that named those sides. Any other cites the `offered` event with
    its sides, the receiver's own `decided` roll about this offerer and these terms, made before the answer, and the
    receiver's `mind_answer`, whose request rested on that roll; and one that moved goods rests on a rolled yes that
    the request offered as the only answer, with nothing withheld. So no offer moves goods on a mind's word."""
    d, out = e.data, []
    sides_ok = offer_side_ok(d.get("gives")) and offer_side_ok(d.get("gets")) \
        and not (offer_empty(d["gives"]) and offer_empty(d["gets"]))
    if not sides_ok or e.actor != d["from"] or d["from"] == d["to"]:
        return [f"event {e.id}: deal {d}"]
    cited = [events[p] for p in e.parents]
    if offer_empty(d["gets"]):
        act = next((c for c in cited if c.kind == "act" and c.origin == "player"), None)
        if not (d["ok"] and act is not None and act.actor == d["from"] and act.data.get("target") == d["to"]
                and act.data.get("give") == d["gives"] and act.data.get("want") == {}):
            out.append(f"event {e.id}: a gift's deal without the act that gave it")
        return out
    off = next((c for c in cited if c.kind == "offered"), None)
    mind = next((c for c in cited if c.kind == "mind_answer"), None)
    dec = next((c for c in cited if c.kind == "decided"), None)
    if off is None or off.actor != d["from"] or off.data["with"] != d["to"] or off.data["gives"] != d["gives"] \
            or off.data["gets"] != d["gets"]:
        out.append(f"event {e.id}: deal without the offer it settles")
    if mind is None and d.get("why") == "gone":  # the offerer left before the answer: no call, nothing moved
        if d["ok"] or dec is None or dec.actor != d["to"] or dec.data.get("with") != d["from"] \
                or dec.data.get("move") != "offer" or dec.id >= e.id:
            out.append(f"event {e.id}: a gone offer's deal without the receiver's decision on it")
        return out
    if mind is None or mind.actor != d["to"] or mind.data.get("deal", {}).get("move") != "offer":
        return out + [f"event {e.id}: deal without the receiver's answer to an offer"]
    basis = mind.data["deal"]
    if dec is None or dec.actor != d["to"] or dec.data.get("with") != d["from"] or dec.data.get("move") != "offer" \
            or dec.id != basis.get("decided") or dec.id >= mind.id or dec.data.get("answer") != "yes" \
            or dec.data["roll"] >= dec.data["limit"] or (off is not None and off.id not in mind.parents):
        out.append(f"event {e.id}: deal without the receiver's own earlier rolled yes to this offer")
    if d["ok"] and (mind.data.get("offered") != ["yes"] or "withheld" in basis):
        out.append(f"event {e.id}: deal moved goods on an answer that wasn't the offered yes")
    if not d["ok"] and not d.get("why"):
        out.append(f"event {e.id}: a deal that moved nothing says no reason")
    return out


def state_violations(sim: Sim) -> list[str]:
    st = sim.state
    n = len(st["agents"])
    out = [] if st["market_food"] >= 0 else [f"market_food {st['market_food']}"]
    out += type_violations(st)
    longest = longest_trip(sim.map)
    for a in st["agents"]:
        aid = a["id"]
        out += map_violations(sim, a, longest)
        if a["inv"]["food"] < 0 or a["inv"]["coin"] < 0:
            out.append(f"agent {aid} inventory {a['inv']}")
        if not all(0 <= v <= 1000 for v in a["needs"].values()):
            out.append(f"agent {aid} needs {a['needs']}")
        for o, r in a["relationships"].items():
            if int(o) == aid or not -1000 <= r["affinity"] <= 1000:
                out.append(f"agent {aid} relationship {o} {r}")
        for b in a["plan"]:
            if not (type(b["start_h"]) is int and type(b["end_h"]) is int and 0 <= b["start_h"] < b["end_h"] <= 24):
                out.append(f"agent {aid} plan block {b}")
        if not (len(a["core"]) <= 6 and all(isinstance(c, str) and len(c) <= 200 for c in a["core"])):
            out.append(f"agent {aid} core {a['core']!r}")
        it = a["intent"]
        if it and not (1 <= len(it["steps"]) <= 4 and 0 <= it["i"] < len(it["steps"])
                       and isinstance(it["goal"], str) and 0 < len(it["goal"]) <= 120):
            out.append(f"agent {aid} intent {it}")
        for s in (it["steps"] if it else []) + ([a["override"]] if a["override"] else []):
            if s["do"] == "stay":  # say-3: an answer's stay, an override where the villager was
                if not (s is a["override"] and s["target"] is None and s["place"] in sim.map["places"]):
                    out.append(f"agent {aid} stay step {s}")
            elif s["do"] == "go":  # the player's go_to (go-to-1): an override on the possessed body, to a place
                if not (s is a["override"] and s["target"] is None and s["place"] in sim.map["places"]
                        and sim.state.get("player", {}).get("agent") == aid):
                    out.append(f"agent {aid} go step {s}")
            elif s["do"] == "roam":  # walk-1: the possessed body stands where the player walked it
                if not (s is a["override"] and s["target"] is None and "at" in a
                        and sim.state.get("player", {}).get("agent") == aid):
                    out.append(f"agent {aid} roam step {s}")
            elif s["do"] not in STEP_DOS and not (s["do"] in RULE_STEPS and s is a["override"]):
                out.append(f"agent {aid} step {s}")
            elif s["do"] in PERSON_STEPS + RULE_STEPS:
                if not (type(s["target"]) is int and 0 <= s["target"] < n and s["target"] != aid):
                    out.append(f"agent {aid} step target {s}")
            elif s["target"] is not None:
                out.append(f"agent {aid} step target {s}")
        if not (a["asleep"] or not a["override"] or a["override"]["left"] > 0):
            out.append(f"agent {aid} override {a['override']}")
    return out


def log_violations(events: list[Event], start: int = 0) -> list[str]:
    """Ids dense from 0, known origins, parents earlier than the event, ticks non-decreasing."""
    out = []
    for i in range(start, len(events)):
        e = events[i]
        if e.id != i:
            out.append(f"event {i} has id {e.id}")
        if e.origin not in ORIGINS:
            out.append(f"event {i} origin {e.origin}")
        if not all(isinstance(p, int) and 0 <= p < e.id for p in e.parents):
            out.append(f"event {i} parents {e.parents}")
        if i and events[i - 1].tick > e.tick:
            out.append(f"event {i} tick {e.tick} after {events[i - 1].tick}")
    return out


def totals(sim: Sim) -> tuple[int, int]:
    ag = sim.state["agents"]
    return sum(a["inv"]["food"] for a in ag) + sim.state["market_food"], sum(a["inv"]["coin"] for a in ag)


class Checker:
    """Wraps sim.step: the food and coin ledgers balance against the tick's events, and the state,
    the new log entries and every agent's traits (which no mind may change) stay valid."""

    def __init__(self, sim: Sim):
        self.sim = sim
        self.traits = [dict(a["traits"]) for a in sim.state["agents"]]
        self.where = {a["id"]: a["place"] for a in sim.state["agents"]}
        self.bodies = Bodies(sim)  # reach-1: where each one stands, followed from the log
        self.things = Things(sim)  # things-1: the ledger of things, followed from the log

    def before(self, acts=()):
        """Call right before sim.step(acts)."""
        sim = self.sim
        self._tick, self._n0, self._totals = sim.state["tick"], len(sim.events), totals(sim)
        self._chest = sim.state.get("chest", 0)
        self._price, self._sell = sim._price(), sim._sell_price()  # in force for the whole step (B5 year-1)
        burned = {act["family"] for act in acts if act["kind"] == "fire"}  # two fires on one family burn once
        self._burned = sum(sim.state["agents"][m]["inv"]["food"] for f in sorted(burned)
                           for m in sim.state["families"][f]["members"])

    def after(self) -> list[str]:
        """Call right after the step; returns its violations, each prefixed with the tick."""
        sim, n0 = self.sim, self._n0
        new = sim.events[n0:]
        produced = sum(e.data.get("food", 0) for e in new if e.kind == "produce") \
            + sum(e.data["food"] for e in new if e.kind == "used")  # things-1: what the ground gave a player
        eaten = sum(1 for e in new if e.kind == "eat")
        spoiled = sum(e.data["market_food_lost"] for e in new if e.kind in ("spoil", "blight"))
        coin_made = sum(e.data.get("coin", 0) for e in new if e.kind == "produce")
        sold = sum(e.data["food"] for e in new if e.kind == "sell")
        bought = sum(e.data["food"] for e in new
                     if e.kind == "receipt" and e.data["action"] == "buy_food" and e.data["ok"])
        (food0, coin0), (food1, coin1) = self._totals, totals(sim)
        want_food = produced - eaten - self._burned - spoiled
        tithed = sum(e.data["coin"] for e in new if e.kind == "tithe")
        spent = sum(e.data["coin"] for e in new if e.kind == "chest_spent")
        alms = sum(e.data["coin"] for e in new if e.kind == "alms")  # chest to villager (B5 alms-1)
        want_coin = coin_made + self._sell * sold - self._price * bought - tithed + alms
        out = []
        if food1 - food0 != want_food:
            out.append(f"food ledger: {food1 - food0} vs {want_food}")
        if coin1 - coin0 != want_coin:
            out.append(f"coin ledger: {coin1 - coin0} vs {want_coin}")
        chest = sim.state.get("chest", 0)
        if chest - self._chest != tithed - spent - alms or chest < 0:
            out.append(f"chest ledger: {chest - self._chest} vs {tithed - spent - alms}")
        out += state_violations(sim)
        out += log_violations(sim.events, n0)
        out += presence_violations(sim.events, n0, self.where, self.bodies, self.things)
        out += thing_violations(sim, self.things)
        out += deal_violations(sim.events, n0, sim.state.get("player", {}).get("agent"))
        if [a["traits"] for a in sim.state["agents"]] != self.traits:
            out.append("a mind changed someone's traits")
        return [f"tick {self._tick}: {v}" for v in out]

    def step(self, acts=()) -> list[str]:
        self.before(acts)
        self.sim.step(acts)
        return self.after()
