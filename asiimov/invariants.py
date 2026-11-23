"""World invariants and the food/coin ledger, checked tick by tick.

Shared by the property tests (tests/test_invariants.py, which assert there are none) and the long
unattended run (asiimov/soak.py, which counts them and keeps going). Each check returns a list of
violation strings instead of raising, so a long run can report every one it hits.
"""
from .events import ORIGINS, Event
from .sim import GOODS_KEYS, PERSON_STEPS, RULE_STEPS, STEP_DOS, TRADE_GOOD, Sim
from .worldmap import travel_ticks

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
    if r is None:
        ok = a["place"] in places and (not a["asleep"] or a["place"] == a["home"])
        return [] if ok else [f"agent {aid} at {a['place']!r} (asleep {a['asleep']})"]
    ok = (a["place"] is None and not a["asleep"] and r["to"] in places
          and sim.state["tick"] <= r["arrive"] <= r["depart"] + longest
          and r["arrive"] == r["depart"] + travel_ticks(r["from"], places[r["to"]]["anchor"]))
    return [] if ok else [f"agent {aid} place {a['place']!r} route {r}"]


# Events that need two agents together, and the field naming the other one. A receipt counts when it
# succeeded, or for a steal that was caught (it was attempted face to face).
TOGETHER = {"talk": "with", "meet": "with", "snub": "with"}


def presence_violations(events: list[Event], start: int, where: dict) -> list[str]:
    """From the log alone, in event order: every talk, meet, snub, gift, ask, steal and buy happens
    between agents at the same place (never two walkers), and every buy at the market. `where` maps
    agent id to place (None while walking) as of events[start]; it is updated in place."""
    out = []
    for e in events[start:]:
        d, a = e.data, e.actor
        if e.kind == "activity":
            where[a] = d["place"] if d.get("arrive", e.tick) == e.tick else None
        elif e.kind in ("arrive", "wake"):
            where[a] = d["place"]
        other = d.get(TOGETHER.get(e.kind, ""))
        if e.kind == "receipt" and (d["ok"] or d["why"] == "caught"):
            other = d.get("to", d.get("victim"))
            if d["action"] == "buy_food" and where.get(a) != "market":
                out.append(f"event {e.id}: bought away from the market")
        if other is not None and (where.get(a) is None or where.get(a) != where.get(other)):
            out.append(f"event {e.id}: {e.kind} between {a} at {where.get(a)!r} and {other} at {where.get(other)!r}")
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
        if d.get("move") == "trade":
            out += trade_deal_violations(events, e)
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


def trade_deal_violations(events: list[Event], e: Event) -> list[str]:
    """goods-1 (contracts B5.6): a supply buy's `deal` is the buyer's act. It cites the buyer's own `trade_due` naming
    the seller, moves at least one unit of the one good the seller's trade makes, for the price a unit in coin, and
    the buyer's receipt for the run follows at once and cites it."""
    d, pop = e.data, events[0].data["population"]
    tr = pop.get("stakes", {}).get("trade", {})
    if not any(k in tr for k in GOODS_KEYS):
        return [f"event {e.id}: trade deal in a world without goods"]
    due = events[e.parents[0]] if e.parents else None
    good = TRADE_GOOD.get(pop["agents"][d["from"]]["role"]) if type(d.get("from")) is int else None
    n = d.get(good) if good else None
    ok = (d.get("ok") is True and e.actor == d.get("to") and d["from"] != d["to"] and good is not None
          and type(n) is int and n >= 1 and d.get("coin") == n * tr["price"]
          and set(d) == {"move", "from", "to", good, "coin", "ok"}
          and due is not None and due.kind == "trade_due" and due.actor == d["to"] and due.data["to"] == d["from"])
    r = events[e.id + 1] if e.id + 1 < len(events) else None
    cited = (r is not None and r.kind == "receipt" and r.actor == e.actor and e.id in r.parents
             and r.data.get("action") == "trade" and r.data.get("ok") is True and r.data.get("to") == d.get("from")
             and r.data.get("coin") == d.get("coin"))
    return [] if ok and cited else [f"event {e.id}: trade deal {d} cites {e.parents}"]


def goods_violations(sim: Sim) -> list[str]:
    """goods-1: a villager holds no more of its own good than `hold`, no more of an input than `ahead` days' worth,
    and none of a good it neither makes nor needs."""
    tr = sim._goods()
    if not tr:  # a world on supply-1's stamps, or with no trade: nobody holds a good at all
        return [f"agent {a['id']} holds {g} in a world without goods"
                for a in sim.state["agents"] for g in TRADE_GOOD.values() if g in a["inv"]]
    out = []
    for a in sim.state["agents"]:
        needs = {TRADE_GOOD[n] for n in tr["needs"].get(a["role"], [])}
        for g in TRADE_GOOD.values():
            cap = tr["hold"] if TRADE_GOOD.get(a["role"]) == g else tr["ahead"] if g in needs else 0
            if not 0 <= a["inv"][g] <= cap:
                out.append(f"agent {a['id']} ({a['role']}) holds {a['inv'][g]} {g}, cap {cap}")
    return out


def goods_totals(sim: Sim) -> dict:
    return {g: sum(a["inv"].get(g, 0) for a in sim.state["agents"]) for g in TRADE_GOOD.values()}


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

    def before(self, acts=()):
        """Call right before sim.step(acts)."""
        sim = self.sim
        self._tick, self._n0, self._totals = sim.state["tick"], len(sim.events), totals(sim)
        self._goods = goods_totals(sim)
        self._chest = sim.state.get("chest", 0)
        self._price, self._sell = sim._price(), sim._sell_price()  # in force for the whole step (B5 year-1)
        burned = {act["family"] for act in acts if act["kind"] == "fire"}  # two fires on one family burn once
        self._burned = sum(sim.state["agents"][m]["inv"]["food"] for f in sorted(burned)
                           for m in sim.state["families"][f]["members"])

    def after(self) -> list[str]:
        """Call right after the step; returns its violations, each prefixed with the tick."""
        sim, n0 = self.sim, self._n0
        new = sim.events[n0:]
        produced = sum(e.data.get("food", 0) for e in new if e.kind == "produce")
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
        for g, held in goods_totals(sim).items():  # goods-1: a good is made only at work and goes only by use
            want = sum(e.data.get(g, 0) for e in new if e.kind == "made") - sum(e.data.get(g, 0) for e in new if e.kind == "used")
            if held - self._goods[g] != want:
                out.append(f"{g} ledger: {held - self._goods[g]} vs {want}")
        out += goods_violations(sim)
        out += state_violations(sim)
        out += log_violations(sim.events, n0)
        out += presence_violations(sim.events, n0, self.where)
        out += deal_violations(sim.events, n0, sim.state.get("player", {}).get("agent"))
        if [a["traits"] for a in sim.state["agents"]] != self.traits:
            out.append("a mind changed someone's traits")
        return [f"tick {self._tick}: {v}" for v in out]

    def step(self, acts=()) -> list[str]:
        self.before(acts)
        self.sim.step(acts)
        return self.after()
