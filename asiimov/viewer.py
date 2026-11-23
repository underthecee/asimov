"""Read-only replay viewer: turn a recorded world log into one self-contained HTML page.

    python -m asiimov.viewer runs/demo/events.jsonl --out runs/demo/view.html
    python -m asiimov.viewer results/raw/t1m-4b/full/events.jsonl.gz --out view.html

The page shows a map of places with who is where at any tick, a scrubber over the
whole run, each villager's timeline (their own events and the ones aimed at them), mind outputs
with their reasons, and every event's causal parents. It needs no server, no network and no
model, and works on logs from any engine, complete or cut off by a crash.

Internal tooling only: it is not the game client and decides nothing about its form (T6b).
A log whose genesis carries a map (asiimov/worldmap.py) is drawn as geography, with walkers between
places; an older log is drawn as a schematic of named places.

Where each villager is comes from the log alone: every place change is an emitted event (wake,
activity, arrive, moved, sleep), so `moves()` is exact for a complete log and needs no replay. Inventory and
needs are not shown, because the log doesn't carry them; that would need a same-engine replay.
"""
import argparse
import gzip
import json
import os
import sys
from pathlib import Path

from .sim import TICKS_PER_DAY

TEMPLATE = Path(__file__).with_name("viewer.html")


def read_log(path: str) -> tuple[list[dict], int]:
    """Events from a .jsonl or .jsonl.gz log, or a run directory holding one. A crashed run can end in
    a half-written line; it is skipped and counted, never repaired."""
    if os.path.isdir(path):
        for name in ("events.jsonl", "events.jsonl.gz"):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise SystemExit(f"no events.jsonl or events.jsonl.gz in {path}")
    opener = gzip.open if path.endswith(".gz") else open
    events, bad = [], 0
    with opener(path, "rt") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    if not events or events[0]["kind"] != "genesis":
        raise SystemExit(f"{path} does not start with a genesis event")
    return events, bad


def moves(events: list[dict]) -> dict[int, list[list]]:
    """Each villager's [tick, place, activity] changes. At tick t a villager is at the last entry with
    tick <= t, which is their state after tick t ran. tests/test_viewer.py checks this against the sim.
    On a map log a walker's place is None and its entry carries [from, to, depart]: it left `from` at
    tick `depart` for place `to`, at the map's walking speed. A player's walked body (walk-1) standing in
    the open has place None and [tile, None, tick]: it stands on `tile`, bound for nowhere."""
    pop = events[0]["data"]["population"]
    home = {a["id"]: pop["families"][a["family"]]["home"] for a in pop["agents"]}
    out = {aid: [[0, h, "sleep"]] for aid, h in home.items()}  # genesis: everyone asleep at home

    def put(aid, tick, place, activity, walk=()):
        m = out[aid]
        if m[-1][0] == tick:
            m.pop()  # several changes in one tick: only the last is visible between ticks
        if m and m[-1][1:] == [place, activity, *walk]:
            return
        m.append([tick, place, activity, *walk])

    for e in events:
        aid, kind, d = e["actor"], e["kind"], e["data"]
        if kind == "wake":
            put(aid, e["tick"], d.get("place", out[aid][-1][1]), "home")
        elif kind == "activity" and d.get("arrive", e["tick"]) > e["tick"]:  # walking (map logs)
            walk = [d["from"], d["place"], e["tick"]] if "from" in d else out[aid][-1][3:]
            put(aid, e["tick"], None, d["activity"], walk)
        elif kind == "activity":
            put(aid, e["tick"], d["place"], d["activity"])
        elif kind == "arrive":
            put(aid, e["tick"], d["place"], out[aid][-1][2])
        elif kind == "moved":  # walk-1: a player's step; in the open the body stands on its tile
            put(aid, e["tick"], d["place"], "visit", [] if d["place"] is not None else [d["pos"], None, e["tick"]])
        elif kind == "sleep":
            put(aid, e["tick"], home[aid], "sleep")
    return out


def ending(last: dict) -> str:
    """How the log stops. Scorecard arms are saved without an end event but stop at a day's checkpoint."""
    if last["kind"] == "end":
        return "complete log"
    if last["kind"] == "checkpoint":
        return f"no end event; stops at the end of day {last['data']['day']}"
    return f"no end event; cut off at tick {last['tick']}, mid-day"


def build(events: list[dict], source: str = "", skipped: int = 0) -> dict:
    """The page's data: the population, each villager's moves, and the events in a compact form."""
    g = events[0]["data"]
    pop = g["population"]
    last = events[-1]
    complete = last["kind"] == "end"
    end_tick = last["tick"] if complete else last["tick"] + 1
    places = {f["home"] for f in pop["families"]}
    compact, models = [], set()
    for e in events:
        if e["kind"] == "activity" or "place" in e["data"]:
            places.add(e["data"].get("place"))
        m = e.get("mind")
        if m:
            models.add(m.get("model"))
            m = {"model": m.get("model"), "raw": m.get("raw_output"),
                 "in": m.get("prompt_tokens"), "out": m.get("output_tokens")}
        data = e["data"] if e["kind"] != "genesis" else {k: v for k, v in e["data"].items() if k not in ("population", "map")}
        compact.append([e["id"], e["tick"], e["kind"], e["origin"], e["actor"], data, e["parents"], m])
    places.discard(None)
    mv = moves(events)
    return {
        "source": source, "engine": g.get("engine"), "seed": g.get("seed"), "order": g.get("order"),
        "ticks_per_day": TICKS_PER_DAY, "end_tick": end_tick, "complete": complete, "skipped_lines": skipped,
        "ending": ending(last),
        "models": sorted(x for x in models if x),
        "agents": pop["agents"], "families": pop["families"], "places": sorted(places), "map": g.get("map"),
        "moves": [mv[a["id"]] for a in pop["agents"]], "events": compact,
    }


def render(view: dict) -> str:
    blob = json.dumps(view, separators=(",", ":")).replace("<", "\\u003c")  # no text in the log can end the script
    return TEMPLATE.read_text().replace("/*__VIEW__*/null", blob, 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("log", help="events.jsonl, events.jsonl.gz, or a run directory")
    ap.add_argument("--out", default="view.html")
    args = ap.parse_args(argv)
    events, bad = read_log(args.log)
    view = build(events, args.log, bad)
    Path(args.out).write_text(render(view))
    note = "" if view["complete"] else f" ({view['ending']})"
    if bad:
        note += f"; skipped {bad} unreadable line(s)"
    print(f"{args.out}: {len(events)} events, {len(view['agents'])} villagers, "
          f"{view['end_tick'] / TICKS_PER_DAY:.1f} days{note}", file=sys.stderr)


if __name__ == "__main__":
    main()
