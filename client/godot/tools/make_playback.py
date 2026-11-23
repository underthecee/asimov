"""Turn a recorded world log into a playback pack for the Godot client (G7).

    python client/godot/tools/make_playback.py LOG OUT.json.gz [--engine DIR] [--expected FILE --every 10]

LOG is events.jsonl(.gz) or a run directory. The pack holds the log's events (without mind records, which
the client doesn't draw) and every villager's holdings after each tick, taken from the engine's own replay
of the log: every event is checked against the record as it replays, so a pack is only written for a log
this engine reproduces. The client plays the pack back and computes no world rule.

A log replays only on the engine that wrote it. For an older log, pass --engine a checkout of that engine
(git worktree add DIR COMMIT). A live run's log has no end event; it replays as far as it was recorded.
A log copied while its world runs, or left by a kill, can stop inside a tick: a torn last line is dropped, and
a last tick that doesn't replay whole is left out (the log is flushed tick by tick, so earlier ticks are whole).

--expected writes the engine's state at every Nth view tick (positions, places, holdings, who is at each
place), for tests/test_playback.gd.
"""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
import sys
from collections import defaultdict

FORMAT = 1


def read_log(path: str) -> bytes:
    if os.path.isdir(path):
        path = os.path.join(path, "events.jsonl")
    with (gzip.open if path.endswith(".gz") else open)(path, "rb") as f:
        return f.read()


def _tile(sim, a) -> list:
    """The tile the client draws the body on (space-1). An engine before space-1 has no Sim.tile, and the
    client draws its villagers on the same slots, which need only the map and the id: this checkout's
    worldmap gives them, loaded by path because `asiimov` may be the older engine's (--engine)."""
    if hasattr(sim, "tile"):
        return sim.tile(a)
    if a["route"] or "at" in a:
        return list(sim._pos(a))
    spec = importlib.util.spec_from_file_location(
        "_worldmap_now", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "asiimov", "worldmap.py"))
    wm = sys.modules.get("_worldmap_now")
    if wm is None:
        wm = sys.modules["_worldmap_now"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wm)
    return wm.slot(wm.own_tiles(sim.map, a["place"]), a["id"])


def replay_frames(raw: bytes, every: int):
    """Replay the log tick by tick. Returns (events, holdings, expected, last_tick): holdings is
    [[tick, id, food, coin], ...], a villager's state after that tick ran, listed only when it changed
    (tick -1 is the start); expected is the engine's state at every `every`th view tick and the last."""
    from asiimov.events import Event
    from asiimov.minds import ReplayDivergence, ReplayMind
    from asiimov.sim import ENGINE_VERSION, Sim

    lines = raw.split(b"\n")
    if lines[-1].strip():
        print("the last line is torn (no newline): dropped", file=sys.stderr)
    events = [Event(**json.loads(l)) for l in lines[:-1] if l.strip()]
    if not events or events[0].kind != "genesis":
        raise SystemExit("not a world log: the first event isn't genesis")
    genesis = events[0]
    engine = genesis.data.get("engine")
    if engine != ENGINE_VERSION:
        raise SystemExit(f"log was written by engine {engine}, this checkout is engine {ENGINE_VERSION}: "
                         f"pass --engine a checkout of engine {engine}")
    ended = events[-1].kind == "end"
    stop = events[-1].tick if ended else events[-1].tick + 1  # the view tick the log reaches
    player = defaultdict(list)
    for e in events:
        if e.origin == "player":
            player[e.tick].append({"kind": e.kind, **e.data})
    pop = genesis.data["population"]
    sim = Sim(genesis.data["seed"], len(pop["agents"]), ReplayMind([e for e in events if e.origin == "mind"]),
              genesis=pop, expected=events, order=genesis.data["order"])
    holdings, last = [], {}
    expected = {"ticks": [], "pos": [], "place": [], "food": [], "coin": [], "present": []}

    def record(tick):
        for a in sim.state["agents"]:
            now = (a["inv"]["food"], a["inv"]["coin"])
            if last.get(a["id"]) != now:
                last[a["id"]] = now
                holdings.append([tick, a["id"], *now])

    def state():
        agents = sim.state["agents"]
        present = defaultdict(list)
        for a in agents:
            if a["place"] is not None:
                present[a["place"]].append(a["id"])
        return {"ticks": sim.state["tick"], "pos": [_tile(sim, a) for a in agents],
                "place": [a["place"] for a in agents], "food": [a["inv"]["food"] for a in agents],
                "coin": [a["inv"]["coin"] for a in agents], "present": dict(present)}

    def expect(row):
        if not expected["ticks"] or expected["ticks"][-1] != row["ticks"]:
            for k in expected:
                expected[k].append(row[k])

    record(-1)
    while sim.state["tick"] < stop:
        tick, whole = sim.state["tick"], len(sim.events)
        if tick % every == 0:
            expect(state())
        open_end = not ended and tick == stop - 1  # only a log with no end can stop inside its last tick
        before = state() if open_end else None      # a step that fails leaves the sim half-way
        try:
            sim.step(player.get(tick, []))
        except ReplayDivergence as err:
            if not open_end:
                raise
            print(f"tick {tick}, the last recorded, doesn't replay whole and is left out "
                  f"({len(events) - whole} events): {str(err)[:120]}", file=sys.stderr)
            expect(before)
            return events[:whole], holdings, expected, tick - 1
        record(tick)
    expect(state())
    if ended:
        sim.finish()
    if len(sim.events) != len(events):
        raise ReplayDivergence(f"replay produced {len(sim.events)} events, log has {len(events)}")
    return events, holdings, expected, stop - 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("log", help="events.jsonl(.gz) or a run directory")
    ap.add_argument("out", help="the pack: .json.gz (or .json)")
    ap.add_argument("--engine", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."),
                    help="a checkout of the engine that wrote the log (default: this one)")
    ap.add_argument("--expected", help="also write the engine's state at every --every'th view tick")
    ap.add_argument("--every", type=int, default=10)
    args = ap.parse_args(argv)
    sys.path.insert(0, os.path.abspath(args.engine))
    raw = read_log(args.log)
    events, holdings, expected, last_tick = replay_frames(raw, args.every)
    rows = []
    for e in events:
        row = json.loads(e.to_json())
        del row["mind"]
        rows.append(row)
    pack = {"playback": FORMAT, "engine": events[0].data["engine"], "last_tick": last_tick,
            "source": {"events": len(events), "sha256": hashlib.sha256(raw).hexdigest()},
            "events": rows, "holdings": holdings}
    text = json.dumps(pack, sort_keys=True, separators=(",", ":")).encode()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    if args.out.endswith(".gz"):
        with open(args.out, "wb") as f, gzip.GzipFile(filename="", fileobj=f, mode="wb", mtime=0) as z:  # same log, same bytes
            z.write(text)
    else:
        with open(args.out, "wb") as f:
            f.write(text)
    if args.expected:
        with open(args.expected, "w") as f:
            json.dump(expected, f)
    print(f"{args.out}: engine {pack['engine']}, {len(rows)} events, view ticks 0-{last_tick + 1}, "
          f"{len(holdings)} holdings changes, {os.path.getsize(args.out)} bytes")


if __name__ == "__main__":
    main()
