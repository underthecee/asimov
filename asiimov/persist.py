"""Crash-safe runs. The log goes to disk tick by tick with a snapshot every so often, so a run
that dies can be resumed, and it finishes with exactly the log an uninterrupted run writes.

A run directory holds:
  run.json               the world's inputs: seed, agents, order, player actions, target length in
                         days and the mind's model (resume refuses a different model)
  events.jsonl           append-only, whole ticks only; flushed every tick, fsynced before each snapshot
  snapshots/<tick>.json  Sim.snapshot() at the start of that tick, plus the sha256 of the log before it
  crash.jsonl            events of the tick that was running when the run died (inspection only)

Resume restores the newest snapshot whose log prefix checks out, then re-runs the ticks recorded
after it with their recorded mind outputs (every event is checked against the record), then
continues with the live mind. Only the last recorded tick of a crashed run is asked again.
"""
import hashlib
import json
import os
from collections import defaultdict

from .events import Event, write_jsonl
from .minds import ReplayMind
from .params import generate_population
from .sim import DEFAULT_STAKES, TICKS_PER_DAY, Sim


def _atomic_write(path: str, text: str):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Run:
    """A world running into a directory. Make one with start() or resume(), then advance() and finish()."""

    def __init__(self, out: str, sim: Sim, player: dict, on_disk: int, hashed: int, snapshot_every: int,
                 days: int | None = None):
        self.out, self.sim, self.player, self.snapshot_every = out, sim, player, snapshot_every
        self.days = days  # the world's planned length, from run.json
        self.on_disk = on_disk  # events already in events.jsonl
        self.hashed = hashed    # events covered by self.sha, the running sha256 of the log
        self.sha = hashlib.sha256("".join(e.to_json() + "\n" for e in sim.events[:hashed]).encode())
        self.log = open(os.path.join(out, "events.jsonl"), "a")
        os.makedirs(os.path.join(out, "snapshots"), exist_ok=True)

    def advance(self, end_tick: int, during=None):
        """Run up to (not including) end_tick. `during` is Sim.step's: the live runner's late acts (E4)."""
        while self.sim.state["tick"] < end_tick:
            self._tick(during)

    def finish(self):
        """Snapshot the final state, append the end event and close the log."""
        if not os.path.exists(self._snap_path(self.sim.state["tick"])) and len(self.sim.events) == self.hashed:
            self._snapshot()  # never over actions taken ahead of their tick (Sim.take): the log doesn't hold them yet
        self.sim.finish()
        self._append()
        os.fsync(self.log.fileno())
        self.log.close()

    def _tick(self, during=None):
        sim = self.sim
        try:
            acts = list(self.player.get(sim.state["tick"], []))
            sim.step(acts) if during is None else sim.step(acts, during=during)
        except BaseException:
            # Whole ticks are already on disk. The unfinished one can't be resumed from, but its
            # recorded mind outputs are evidence of whatever killed the run, so set them aside.
            if len(sim.events) > self.hashed:
                write_jsonl(sim.events[self.hashed:], os.path.join(self.out, "crash.jsonl"))
            raise
        self._append()
        if sim.state["tick"] % self.snapshot_every == 0:
            self._snapshot()

    def _append(self):
        for e in self.sim.events[self.hashed:]:
            line = e.to_json() + "\n"
            self.sha.update(line.encode())
            if e.id >= self.on_disk:  # while resume re-runs the recorded tail, those lines are already there
                self.log.write(line)
        self.hashed = len(self.sim.events)
        self.on_disk = max(self.on_disk, self.hashed)
        self.log.flush()  # a killed process loses at most the tick it was in

    def _snap_path(self, tick: int) -> str:
        return os.path.join(self.out, "snapshots", f"{tick:08d}.json")

    def _snapshot(self):
        os.fsync(self.log.fileno())  # a snapshot must never cite log lines that aren't on disk
        snap = self.sim.snapshot()
        snap["log_sha256"] = self.sha.hexdigest()
        _atomic_write(self._snap_path(snap["tick"]), json.dumps(snap))  # no sort_keys: see Sim.snapshot


def config(out: str) -> dict:
    with open(os.path.join(out, "run.json")) as f:
        return json.load(f)


def start(out: str, seed: int, agents: int, mind, order: str = "id", player: dict | None = None,
          workers: int = 1, snapshot_every: int = TICKS_PER_DAY, days: int | None = None,
          mind_kind: str | None = None, talk: dict | None = None, things: dict | None = None) -> Run:
    """Start a new world in `out`. Refuses a directory that already holds a log. `talk`: talk-1's parameters
    (sim.TALK_DEFAULTS) where they differ, recorded in genesis; a world without them is a default world.
    `things`: things-1's parameters (things.DEFAULTS), recorded in genesis; a world without them has no things."""
    player = player or {}
    if os.path.exists(os.path.join(out, "events.jsonl")):
        raise FileExistsError(f"{out} already holds a run; resume it or pick another directory")
    os.makedirs(out, exist_ok=True)
    cfg = {"seed": seed, "agents": agents, "order": order,
           "player": [[t, act] for t in sorted(player) for act in player[t]],
           "days": days, "mind": mind_kind, "model": getattr(mind, "model", None), **({"talk": talk} if talk else {}),
           **({"things": things} if things else {})}
    _atomic_write(os.path.join(out, "run.json"), json.dumps(cfg, indent=1))
    pop = None
    if talk or things:
        pop = generate_population(seed, agents)
        pop["stakes"] = {**json.loads(json.dumps(DEFAULT_STAKES)), **({"talk": dict(talk)} if talk else {}),
                         **({"things": json.loads(json.dumps(things))} if things else {})}
    run = Run(out, Sim(seed, agents, mind, genesis=pop, order=order, workers=workers), player, 0, 0, snapshot_every,
              days)
    run._append()
    run._snapshot()  # tick 0, so a run that dies on its first day still resumes
    return run


def resume(out: str, mind, workers: int = 1, snapshot_every: int = TICKS_PER_DAY, days: int | None = None) -> Run:
    """Continue the world in `out` from where its log stops, whether it crashed or finished.
    `days` changes the planned length (to extend a world); otherwise run.json's is kept."""
    cfg = config(out)
    model = getattr(mind, "model", None)
    if cfg.get("model") is not None and model != cfg["model"]:
        # One world, one mind: continuing a Qwen world with the stub would silently mix two minds.
        raise ValueError(f"{out} was run with mind {cfg['model']!r}; refusing to continue it with {model!r}")
    if days is not None and days != cfg.get("days"):
        cfg["days"] = days
        _atomic_write(os.path.join(out, "run.json"), json.dumps(cfg, indent=1))
    player = defaultdict(list)
    for t, act in cfg["player"]:
        player[t].append(act)
    log_path = os.path.join(out, "events.jsonl")
    with open(log_path, "rb") as f:
        lines = f.read().split(b"\n")[:-1]  # a torn last line (no newline) is dropped
    snap, n = _newest_valid_snapshot(out, lines)
    events = [Event(**json.loads(l)) for l in lines]
    if events and events[-1].kind == "end":
        end = events.pop()  # extending a finished world: the end moves to the new last tick
        while len(events) > n and events[-1].tick == end.tick:
            events.pop()  # actions taken ahead of a tick that never ran (Sim.take): run.json holds them, it runs them
    elif len(events) > n:
        # A kill mid-write can leave the last recorded tick partial, so it is dropped and re-run live.
        last = events[-1].tick
        while len(events) > n and events[-1].tick == last:
            events.pop()
    with open(log_path, "r+b") as f:  # drop the torn line and the old end; keep the recorded tail
        f.truncate(sum(len(l) + 1 for l in lines[:len(events)]))
    tail_minds = [e for e in events[n:] if e.origin == "mind"]
    sim = Sim.restore(snap, events, ReplayMind(tail_minds), workers, expected=events)
    run = Run(out, sim, player, len(events), n, snapshot_every, cfg.get("days"))
    while len(sim.events) < len(events):
        run._tick()
    sim.expected, sim.mind = None, mind
    return run


def _newest_valid_snapshot(out: str, lines: list[bytes]) -> tuple[dict, int]:
    d = os.path.join(out, "snapshots")
    for name in sorted((f for f in os.listdir(d) if f.endswith(".json")), reverse=True):
        try:
            with open(os.path.join(d, name)) as f:
                snap = json.load(f)
        except ValueError:
            continue
        n = snap["n_events"]
        if n <= len(lines) and hashlib.sha256(b"".join(l + b"\n" for l in lines[:n])).hexdigest() == snap["log_sha256"]:
            return snap, n
    raise ValueError(f"no snapshot in {d} matches events.jsonl")
