"""Consequence propagation: who behaves differently after an intervention, against a twin without it.

The metric in plan/prototype-scope.md ("Metrics logged from day one", M1): run the same world with
and without an intervention and count the agents whose behavior differs 1, 3 and 7 sim-days after it.

"Behavior differs" is defined here and nowhere else. An agent's behavior is the sequence of its own
acts, each recorded as (tick, kind, the fields of ACT_FIELDS): where it goes and what it does there
(activity), what it tries and how that ends (receipt, intent_*), who it talks to or turns from (talk,
snub, meet), when it eats, sleeps and wakes, and what it makes and sells. Two agents' behaviors differ
from the first tick at which these sequences disagree; doing the same thing a tick later counts.
Left out on purpose:
  - What the mind said (mind_* raw output), counted separately as "thought": an LLM's text changes on
    any prompt change, so text alone would make every agent whose prompt moved look affected.
  - RNG bookkeeping (stream, index, roll) and quantities derived from state rather than chosen
    (a kin limit): a shifted draw counter with the same outcome is not an act.
  - day_rhythm (a chance draw), plan_receipt (a restatement of the plan) and tithe (a rule taking coin).
  - Things done to the agent (a fire, being talked to): those show up as the agent's own acts or not
    at all.
An agent counts "by day d" when its first difference falls before intervention_tick + d days.

Every count is also given "besides chats", with talk and snub left out of the acts. Those are chance
encounters. Up to engine 10 the partner was an index into the list of who is present at the place, so
one extra person at the plaza re-paired every conversation there with the same draws, and that was the
first difference of almost every agent reached beyond a burned family. From engine 11 (T22) the talk
draws are keyed: a speaker's partner changes only when the old partner left or the new one arrived,
so a chat difference descends from someone else's earlier difference. Read logs from engine 10 or
older with "besides chats".

Each diverged agent is also labelled traced or untraced: traced when its first differing act in the
twin descends, through the log's parents, from an intervention event. Untraced divergence came
through a channel the log's provenance doesn't record: whom an agent saw where (occupancy), the
order of acting, or a per-agent RNG stream whose draw count moved. A high untraced share says the
consequence is real in the world but can't be explained from the log.

For deterministic minds (the stub) a twin is a full rerun and the noise floor is zero, which the
stub CLI checks with a control twin. For sampled minds, read a twin only against noise twins
(asiimov.fork --salt) with the same live set: pass them with --noise.

python -m asiimov.propagation --agents 100 --seed 1 --fire 3@1 --out runs/prop    # stub, $0
python -m asiimov.propagation --agents 100 --seed 1 --fire all@1 --out runs/prop-all  # one twin per family
python -m asiimov.propagation --base runs/demo/events.jsonl --twin runs/twin/events.jsonl --noise runs/noise-a/events.jsonl
"""
import argparse
import json
import os
from collections import defaultdict

from .events import Event, write_jsonl
from .fork import read_log, run_twin
from .minds import StubMind
from .sim import TICKS_PER_DAY, Sim

WINDOWS = (1, 3, 7)
ACT_FIELDS = {
    "activity": ("activity", "place"),
    "receipt": ("action", "ok", "why", "to", "victim", "food"),
    "intent_set": ("steps", "dropped"),
    "intent_step": ("do", "i"),
    "intent_done": ("steps",),
    "intent_dropped": ("i", "why"),
    "intent_failed": ("do", "i", "why"),
    "talk": ("with", "place", "gossip"),
    "snub": ("with",),
    "meet": ("with", "place"),
    "eat": ("household",),
    "sleep": (),
    "wake": ("place",),
    "arrive": ("place",),
    "produce": ("food", "coin"),
    "sell": ("food",),
    "made": ("seed", "tools"),  # goods-1: a unit of work also makes the trade's good
    "deal": ("move", "from", "to", "seed", "tools", "food", "coin", "ok", "why"),  # a supply buy (goods-1); talk's deals
}
NOT_ACTS = ("day_rhythm", "plan_receipt", "tithe", "stock", "sick", "recover", "alms", "sick_news", "trade_due", "used")  # B5 rules: tithe takes coin; stock places
# the harvest day's winter order, whose shop trip and buy are the acts; sick, recover and alms happen to the agent
CHATS = ("talk", "snub")  # chance encounters: whom an agent happens to talk to where it is
CATEGORIES = ("act", "act_besides_chats", "thought")


def _reply_target(e: Event):
    """The target a mind reply named, so a receipt that doesn't record it still says whom the act was at."""
    try:
        out = json.loads(e.mind["raw_output"])
    except (ValueError, RecursionError):  # as the engine's own parse: a hostile reply can nest deep
        return None
    return out.get("target") if isinstance(out, dict) else None


def _project(e: Event, aimed=None):
    """(category, record) for an agent's own event, or None when it is neither an act nor a thought.
    `aimed` maps a mind event's id to the target its reply named; a receipt citing one carries it."""
    if e.actor is None or e.kind in NOT_ACTS:
        return None
    if e.origin == "mind":
        return "thought", (e.tick, e.data.get("kind"), e.mind["raw_output"])
    if e.kind not in ACT_FIELDS:
        # A new kind of act must be classified here before it is measured, or it would silently not count.
        raise KeyError(f"unclassified agent event kind {e.kind!r}: add it to ACT_FIELDS or NOT_ACTS")
    rec = (e.tick, e.kind) + tuple(json.dumps(e.data.get(k), sort_keys=True) for k in ACT_FIELDS[e.kind])
    if e.kind == "receipt" and aimed:
        # Pre-check failures and goal-less "started" receipts don't record the target (review of PR #23).
        rec += tuple(json.dumps(aimed[p], sort_keys=True) for p in e.parents if p in aimed)
    return "act", rec


def streams(events) -> dict[str, dict[int, list]]:
    """{category: {agent: [(record, event)]}} for each of CATEGORIES."""
    out = {c: defaultdict(list) for c in CATEGORIES}
    aimed = {}
    for e in events:
        if e.origin == "mind" and e.data.get("kind") == "deliberate":
            aimed[e.id] = _reply_target(e)
        p = _project(e, aimed)
        if p:
            out[p[0]][e.actor].append((p[1], e))
            if p[0] == "act" and e.kind not in CHATS:
                out["act_besides_chats"][e.actor].append((p[1], e))
    return out


def first_differences(base, twin) -> dict[str, dict[int, tuple[int, Event | None]]]:
    """Per category, agent -> (first tick its records differ, the twin's event there or None)."""
    a, b = streams(base), streams(twin)
    out = {}
    for cat in CATEGORIES:
        res = {}
        for aid in sorted(set(a[cat]) | set(b[cat])):
            xs, ys = a[cat].get(aid, []), b[cat].get(aid, [])
            for i in range(max(len(xs), len(ys))):
                x = xs[i][0] if i < len(xs) else None
                y = ys[i][0] if i < len(ys) else None
                if x != y:
                    # The twin's event here is its first differing act only if it is not later than the
                    # base's: when the twin simply lacks the base's act, it is a later, unrelated one.
                    twin_ev = ys[i][1] if y is not None and (x is None or y[0] <= x[0]) else None
                    res[aid] = (min(r[0] for r in (x, y) if r is not None), twin_ev)
                    break
        out[cat] = res
    return out


def descendants(events, roots) -> set[int]:
    seen = set(roots)
    for e in events:
        if e.id not in seen and any(p in seen for p in e.parents):
            seen.add(e.id)
    return seen


def targets(genesis: dict, events) -> list[int]:
    """Agents an intervention hits directly (a fire: the burned family)."""
    fams = {f["id"]: f["members"] for f in genesis["families"]}
    out = set()
    for e in events:
        if e.origin == "player" and e.kind == "fire":
            out |= set(fams[e.data["family"]])
    return sorted(out)


def report(base, twin, windows=WINDOWS, t0: int | None = None) -> dict:
    """Propagation of the twin's interventions (its player events the base lacks), read against the base.
    Windows start at the first intervention, or at `t0` when given: a noise twin has no intervention
    and is read from its intervention twin's tick, so the two count over the same days."""
    base_player = {(e.tick, e.kind, json.dumps(e.data, sort_keys=True)) for e in base if e.origin == "player"}
    added = [e for e in twin if e.origin == "player"
             and (e.tick, e.kind, json.dumps(e.data, sort_keys=True)) not in base_player]
    t0 = min((e.tick for e in added), default=None) if t0 is None else t0
    diff = first_differences(base, twin)
    if t0 is None:  # a noise or control twin: measure from where the twins start to differ, if they do
        t0 = min((t for c in CATEGORIES for t, _ in diff[c].values()), default=0)
    early = sorted({a for c in CATEGORIES for a, (t, _) in diff[c].items() if t < t0})
    if early:
        raise ValueError(f"agents {early[:10]} act or think differently before the intervention at tick {t0}")
    hit = set(targets(twin[0].data["population"], added))
    cone = descendants(twin, [e.id for e in added])
    n = len(twin[0].data["population"]["agents"])
    out = {"agents": n, "intervention": [{"tick": e.tick, "kind": e.kind, **e.data} for e in added],
           "intervention_tick": t0, "targets": sorted(hit), "by_day": {}}
    for d in windows:
        stop = t0 + d * TICKS_PER_DAY
        acted = {a: ev for a, (t, ev) in diff["act"].items() if t < stop}
        others = [a for a in acted if a not in hit]
        besides = {a for a, (t, _) in diff["act_besides_chats"].items() if t < stop}
        thought = {a for a, (t, _) in diff["thought"].items() if t < stop}
        out["by_day"][str(d)] = {
            "acted_differently": len(acted),
            "of_which_targets": len(hit & set(acted)),
            "beyond_targets": len(others),
            "beyond_targets_traced": sum(1 for a in others if acted[a] is not None and acted[a].id in cone),
            "beyond_targets_besides_chats": len(besides - hit),
            "thought_differently": len(thought),
            "thought_only": len(thought - set(acted)),
            "complete": stop <= _end(twin) and stop <= _end(base),
        }
    out["first_act_difference"] = {str(a): t for a, (t, _) in sorted(diff["act"].items())}
    return out


def _end(events) -> int:
    last = events[-1]
    return last.tick if last.kind == "end" else last.tick + 1


# ---------------------------------------------------------------------------- stub twins ($0)
def stub_world(seed: int, agents: int, days: int, order: str = "id") -> list[Event]:
    sim = Sim(seed, agents, StubMind(), order=order)
    sim.run_until(days * TICKS_PER_DAY)
    sim.finish()
    return sim.events


def stub_twin(base, player: dict) -> list[Event]:
    """A full stub rerun of the base world with `player` actions added (asiimov.fork, serve=False)."""
    return run_twin(base, StubMind(), player=player, serve=False)[0].events


def parse_fire(spec: str, n_families: int) -> list[tuple[int, int]]:
    fam, day = spec.split("@")
    fams = range(n_families) if fam == "all" else [int(fam)]
    return [(f, int(day)) for f in fams]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", help="base log; with --twin, read existing logs (any mind) instead of running stubs")
    ap.add_argument("--twin", help="the intervention twin's log")
    ap.add_argument("--noise", action="append", default=[], help="a noise twin's log (repeatable)")
    ap.add_argument("--agents", type=int, default=100)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--order", default="id")
    ap.add_argument("--fire", default="3@1", help="FAMILY@DAY, or all@DAY for one twin per family")
    ap.add_argument("--days", type=int, help="world length (default: fire day + the longest window)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    if args.base:
        if not args.twin:
            raise SystemExit("--base needs --twin")
        base = read_log(args.base)
        twin_report = report(base, read_log(args.twin))
        t0 = twin_report["intervention_tick"]
        res = {"base": args.base, "twin": args.twin, "twin_report": twin_report,
               "noise": [dict(log=p, **report(base, read_log(p), t0=t0)) for p in args.noise]}
    else:
        day = int(args.fire.split("@")[1])
        days = args.days or day + max(WINDOWS) + 1
        base = stub_world(args.seed, args.agents, days, args.order)
        write_jsonl(base, os.path.join(args.out, "base.jsonl"))
        control = report(base, stub_twin(base, {}))
        if control["first_act_difference"]:
            raise SystemExit(f"a stub rerun without any change diverged: {control['first_act_difference']}")
        fires = parse_fire(args.fire, len(base[0].data["population"]["families"]))
        twins = []
        for fam, d in fires:
            twin = stub_twin(base, {d * TICKS_PER_DAY + 60: [{"kind": "fire", "family": fam}]})
            if len(fires) == 1:
                write_jsonl(twin, os.path.join(args.out, "twin.jsonl"))
            twins.append(report(base, twin))
            r = twins[-1]["by_day"]
            print(f"fire {fam}@{d}: targets {len(twins[-1]['targets'])}, beyond targets (besides chats) by day "
                  + ", ".join(f"{w}: {r[str(w)]['beyond_targets']} ({r[str(w)]['beyond_targets_besides_chats']})"
                              for w in WINDOWS), flush=True)
        res = {"mind": "stub", "seed": args.seed, "agents": args.agents, "days": days, "control_diverged": 0,
               "twins": twins, "summary": sweep_summary(twins)}
    with open(os.path.join(args.out, "propagation.json"), "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps(res.get("summary") or {k: v for k, v in res["twin_report"].items()
                                              if k != "first_act_difference"}, indent=1))


def sweep_summary(twins: list[dict]) -> dict:
    """Across twins, per window: how many reached anyone beyond their targets, and the counts."""
    out = {"twins": len(twins), "by_day": {}}
    for w in WINDOWS:
        rows = [t["by_day"][str(w)] for t in twins]
        beyond = sorted(r["beyond_targets"] for r in rows)
        out["by_day"][str(w)] = {
            "twins_reaching_beyond_targets": sum(1 for b in beyond if b),
            "beyond_targets_max": beyond[-1] if beyond else 0,
            "beyond_targets_median": beyond[len(beyond) // 2] if beyond else 0,
            "beyond_targets_total": sum(beyond),
            "traced_total": sum(r["beyond_targets_traced"] for r in rows),
            "besides_chats_total": sum(r["beyond_targets_besides_chats"] for r in rows),
            "twins_reaching_beyond_targets_besides_chats": sum(1 for r in rows if r["beyond_targets_besides_chats"]),
            "thought_only_total": sum(r["thought_only"] for r in rows),
        }
    return out


if __name__ == "__main__":
    main()
