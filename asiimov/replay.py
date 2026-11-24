"""Replay a recorded log without calling any model, checking every event matches."""
from collections import defaultdict

from .events import Event
from .minds import ReplayDivergence, ReplayMind
from .sim import ENGINE_VERSION, Sim


def replay(events: list[Event]) -> Sim:
    genesis, end = events[0], events[-1]
    assert genesis.kind == "genesis"
    engine = genesis.data.get("engine")
    if engine != ENGINE_VERSION:
        raise ReplayDivergence(f"log was written by engine {engine}, this is engine {ENGINE_VERSION}")
    if end.kind != "end":
        raise ReplayDivergence("log has no end event: the run stopped mid-way; resume it (asiimov.run --resume)")
    pop = genesis.data["population"]
    mind_events = [e for e in events if e.origin == "mind"]
    player = defaultdict(list)
    for e in events:
        if e.origin == "player":
            player[e.tick].append({"kind": e.kind, **e.data})
    sim = Sim(genesis.data["seed"], len(pop["agents"]), ReplayMind(mind_events), genesis=pop, expected=events,
              order=genesis.data["order"])
    sim.run_until(end.tick, player)
    for act in player.get(end.tick, []):  # time-1a: taken as they arrived, in a tick that never ran (Sim.take)
        sim.take(act)
    sim.finish()
    if len(sim.events) != len(events):
        raise ReplayDivergence(f"replay produced {len(sim.events)} events, log has {len(events)}")
    return sim
