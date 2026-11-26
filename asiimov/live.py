"""Live runner: step a world in real time and serve it to clients over WebSocket (plan/client-protocol.md).

    python -m asiimov.live --out runs/live --agents 25 --seed 1 --tps 2      # new world, or reopen runs/live
    python -m asiimov.live --out runs/live --paused                          # reopen paused

The engine stays the only source of truth. Clients get events and views, and send intents. An intent is
checked here, scheduled in run.json's player list (the same place a scripted --fire goes), and applied by
the engine at the start of the next tick, so a live world is a batch world whose player actions arrived
late. Wall-clock time, clients, controls and acks go to the sidecar runner.jsonl, never to events.jsonl
or the state hash.

A live run against a served model (--mind llm) needs its own charter and its own pod, never one serving a
canonical run (plan/2d-env-eval.md). Test 16b's session is one (plan/next-push-2026-11-09.md, track 3):

    python -m asiimov.live --out runs/s16b --agents 25 --mind llm --llm-url ... --llm-model ... --llm-key ... \
        --host 0.0.0.0 --token SECRET --tps 0.05

--token makes every hello carry that token: on a public port, nobody else attaches or steers the world.
Each tick's wall time and mind-call latencies go to the sidecar (`step`), next to each intent's received
and applied times, so a session's reply latency is measured from runner.jsonl alone.
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import random
import os
import struct
import sys
import threading
import time
from collections import defaultdict

from . import inhabitant, narrate, persist
from .llm import SCHEMAS
from .minds import NoisyMind, ScriptMind, StubMind
from .sim import (CONSENTS, ENGINE_VERSION, GO_ACTS, HAIL_ACTS, HAND_ACTS, THING_ACTS, PLAYER_ACTIONS, SAY_ACTS, SAY_MAX, SUB, TALK_DEFAULTS, TALK_MOVES, WALK_ACTS,
                  TICKS_PER_DAY, act_cost)
from .things import DEFAULTS as THING_DEFAULTS
from .worldmap import bounds, on_map

PROTOCOL = 3               # 2 adds the inhabitant scope (plan/player-body-spec.md); 3 is time-1a: a walk is applied and
                           # answered the moment it arrives, and its `early` message carries the body's new view
SCOPES = ("god", "inhabitant")
MAX_LAG = TICKS_PER_DAY    # an intent seen more than a day ago is stale
ORPHAN_TICKS = 36          # a body still held on reopen is released this long after, unless an inhabitant claims it
PULL_MIN_S = 1.0           # E3: an inhabitant's act runs the next tick at least this long after the last one started
MAX_FRAME = 1 << 20        # client frames
BACKLOG_CHUNK = 500        # events per backlog message
SEND_QUEUE = 4096          # messages a slow client may fall behind before it is dropped


def _atomic_write(path: str, text: str):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class LiveRunner:
    """One world in a run directory, stepped one tick at a time, with intents scheduled between ticks.
    Not thread-safe: submit(), step() and view() must not overlap."""

    def __init__(self, out: str, mind, seed: int = 1, agents: int = 25, order: str = "id",
                 snapshot_every: int = TICKS_PER_DAY, mind_kind: str | None = None, workers: int = 1,
                 talk: dict | None = None, things: dict | None = None):
        self.out = out
        self.mind = mind
        if os.path.exists(os.path.join(out, "events.jsonl")):
            self.run = persist.resume(out, mind, workers, snapshot_every=snapshot_every)
            self.reopened = True
        else:
            self.run = persist.start(out, seed, agents, mind, order, {}, workers, snapshot_every=snapshot_every,
                                     mind_kind=mind_kind, talk=talk, things=things)
            self.reopened = False
        self.sim = self.run.sim
        self.intents: dict[str, dict] = {}  # intent id -> {status, tick, slot, act, event | reason}
        self.side_path = os.path.join(out, "runner.jsonl")
        lines = []
        if os.path.exists(self.side_path):
            with open(self.side_path, "rb") as f:
                lines = f.read().split(b"\n")[:-1]  # a torn last line was never acted on
            with open(self.side_path, "r+b") as f:
                f.truncate(sum(len(l) + 1 for l in lines))
        self.say_wall: dict = {}  # listener id -> wall time of the player's last line to it (script-voice)
        self.side = open(self.side_path, "a")
        self.inhabitant: dict | None = None  # {"client", "agent"} while an inhabitant is connected
        # E4: an inhabitant's act that arrives while a tick runs joins that tick at its fixed point. The server
        # thread queues it here; the tick's thread takes it in _during, and send() hands back the early ack.
        self.late_q: list = []  # (msg, client, scope, send)
        self.late_lock, self.late_wake = threading.Lock(), threading.Event()
        # early(iid, ack, events): set by the Hub. An intent scheduled before the tick is acked at the fixed
        # point too, with its receipt, not at the tick's end (E4).
        self.early = None
        self._tick_n0 = len(self.sim.events)  # where the coming tick's events start: those taken early, then its step's
        self._reconcile(lines)
        # An inhabitant's body still held on reopen (a crash, or a stop while it was connected) is released
        # ORPHAN_TICKS later unless an inhabitant says hello for it. A god-scope possession is left alone,
        # as it is on a god client's disconnect.
        (held, since), last = self._held_from(), self._last_inhabit
        orphan = held is not None and last is not None and last["kind"] == "inhabit" and last["agent"] == held
        self.orphan_at = since + ORPHAN_TICKS if orphan else None
        self._side("open", reopened=self.reopened, view_tick=self.sim.state["tick"], n_events=len(self.sim.events))

    # ------------------------------------------------------------------ sidecar
    def _side(self, kind: str, sync: bool = False, **data):
        wall = round(time.time(), 3)
        act = data.get("act") or {}
        if kind == "received" and act.get("action") == "say":  # script-voice: the click a held reply counts from
            self.say_wall[act.get("target")] = wall
        self.side.write(json.dumps({"wall": wall, "kind": kind, **data}, sort_keys=True) + "\n")
        self.side.flush()
        if sync:
            os.fsync(self.side.fileno())

    def _reconcile(self, lines: list[bytes]):
        """Rebuild intent outcomes from the sidecar, and finish what a kill interrupted: an intent the
        sidecar received but run.json doesn't schedule is scheduled now; one whose tick has run is applied."""
        self._last_inhabit = None  # the newest inhabit or leave record
        for line in lines:
            r = json.loads(line)
            if r["kind"] == "received":
                self.intents[r["id"]] = {"status": "pending", "tick": r["tick"], "slot": r["slot"], "act": r["act"],
                                         "client": r.get("client")}
            elif r["kind"] == "applied":
                self.intents[r["id"]].update(status="applied", event=r["event"])
            elif r["kind"] == "rejected":
                self.intents[r["id"]] = {"status": "rejected", "reason": r["reason"]}
            elif r["kind"] in ("inhabit", "leave"):
                self._last_inhabit = r
        now = self.sim.state["tick"]
        for iid, rec in self.intents.items():
            if rec["status"] == "rejected":
                continue
            acts = self.run.player.get(rec["tick"], [])
            if rec["slot"] == len(acts) and rec["tick"] >= now:
                self._schedule(rec)  # killed between the sidecar and run.json
            elif rec["slot"] >= len(acts) or acts[rec["slot"]] != rec["act"]:
                raise ValueError(f"{self.out}: run.json doesn't schedule intent {iid!r} where the sidecar says")
            if rec["tick"] < now:
                ev = self._player_events(rec["tick"])[rec["slot"]]
                if rec["status"] == "pending":
                    rec.update(status="applied", event=ev.id)
                    self._side("applied", id=iid, tick=rec["tick"], event=ev.id)
                elif ev.id != rec["event"]:
                    raise ValueError(f"{self.out}: intent {iid!r} was acked as event {rec['event']}, the log has {ev.id}")

    def _player_events(self, tick: int) -> list:
        return self._slot_events([e for e in self.sim.events if e.tick == tick], tick)

    def _slot_events(self, events, tick: int) -> list:
        """A tick's player events in run.json's slot order. The engine logs the tick's other actions first
        and its late acts (E4) at the fixed point, which run.json lists after them, so the orders agree."""
        player = [e for e in events if e.origin == "player"]
        acts = self.run.player.get(tick, [])
        if len(player) != len(acts) or [bool(e.data.get("late")) for e in player] != [bool(a.get("late")) for a in acts]:
            raise RuntimeError(f"tick {tick}: {len(player)} player events for {len(acts)} scheduled actions")
        return player

    def _schedule(self, rec: dict):
        self.run.player.setdefault(rec["tick"], []).append(rec["act"])
        cfg = persist.config(self.out)
        cfg["player"] = [[t, act] for t in sorted(self.run.player) for act in self.run.player[t]]
        _atomic_write(os.path.join(self.out, "run.json"), json.dumps(cfg, indent=1))

    # ------------------------------------------------------------------ intents
    def verbs(self) -> dict:
        n = len(self.sim.state["agents"])
        return {"fire": {"family": [0, len(self.sim.state["families"]) - 1]},
                "possess": {"agent": [0, n - 1]}, "release": {},
                "act": {"action": list(PLAYER_ACTIONS), "target": [0, n - 1],
                        "place": sorted(self.sim.map["places"]),  # place: go_to's, and only go_to's
                        "to": bounds(self.sim.map)}}  # to: walk's tile, within [x0, y0, x1, y1] (walk-1)

    def pulls(self, m: dict) -> bool:
        """Whether an inhabitant's act pulls the next tick forward. Every act does but a walk (time-1a): a
        walk is applied the moment it arrives and answered with the body's new view (take_due), so it waits
        for no tick, wherever it lands. A held key, or a wiggle across a place's edge, doesn't speed the
        world. From time-1b the other acts are answered at once too; they still pull, since what they start
        (a trip, a villager's answer, a line said) shows in the next tick. That is pacing, not a rule."""
        args = m.get("args") if isinstance(m.get("args"), dict) else {}
        return args.get("action") not in WALK_ACTS + HAIL_ACTS  # reach-1: nor a hail, which starts nothing a tick shows

    def _held_agent(self) -> int | None:
        return self._held_from()[0]

    def _held_from(self) -> tuple[int | None, int]:
        """The body possessed once every action already scheduled applies, and the tick that state takes
        effect. Scheduled actions can lie past the view: a crash reopen re-runs ticks whose actions run.json
        already holds. Possession changes only through player actions, which all pass through here, so this
        is exact."""
        now = self.sim.state["tick"]
        held, since = self.sim.state.get("player", {}).get("agent"), now
        for t in sorted(t for t in self.run.player if t >= now):
            for act in self.run.player[t]:
                if act["kind"] in ("possess", "release"):
                    held, since = (act["agent"] if act["kind"] == "possess" else None), t + 1
        return held, since

    def _held(self) -> bool:
        return self._held_agent() is not None

    def _internal(self, act: dict) -> None:
        """A player action the runner itself sends (an inhabitant's possess or release, an orphan's release),
        through the same durable path as a client's intent. Its id starts with "~", which clients can't use."""
        tick = self.sim.state["tick"]
        iid = f"~{act['kind']}-{tick}-{len(self.run.player.get(tick, []))}"
        rec = {"status": "pending", "tick": tick, "slot": len(self.run.player.get(tick, [])), "act": act}
        self._side("received", sync=True, id=iid, client="runner", tick=tick, slot=rec["slot"], act=act,
                   seen_tick=tick)
        self.intents[iid] = rec
        self._schedule(rec)

    def inhabit(self, agent, client: str) -> str | None:
        """An inhabitant's hello for `agent`: possess it, or claim it if a reopened world still holds it.
        Returns why not, or None."""
        if not _is_int(agent) or not 0 <= agent < len(self.sim.state["agents"]):
            return "bad_agent"
        held = self._held_agent()
        if self.inhabitant is not None or (held is not None and (held != agent or self.orphan_at is None)):
            return "body_taken"
        self._side("inhabit", sync=True, client=client, agent=agent, claimed=held is not None)  # before the possess
        if held is None:
            self._internal({"kind": "possess", "agent": agent})
        self.orphan_at = None
        self.inhabitant = {"client": client, "agent": agent}
        return None

    def leave(self, client: str):
        """An inhabitant disconnected: release its body, and the mind's own deliberations resume."""
        if self.inhabitant is None or self.inhabitant["client"] != client:
            return
        agent, self.inhabitant = self.inhabitant["agent"], None
        if self._held_agent() == agent:
            self._internal({"kind": "release"})
        self._side("leave", client=client, agent=agent)

    def _check(self, msg: dict, scope: str) -> tuple[dict | None, str | None]:
        """The engine action for an intent, or why it is refused. This checks shape and the one piece of
        state the runner owns (who is possessed); whether the action works is the engine's call when it
        applies (L2), with a receipt."""
        now = self.sim.state["tick"]
        seen = msg.get("seen_tick")
        verb = msg.get("verb")
        if not isinstance(verb, str) or verb not in self.verbs():
            return None, "unknown_verb"
        if scope == "inhabitant" and verb != "act":
            return None, "scope"  # divine verbs and the body's own possession are not an inhabitant's
        args = msg.get("args")
        n = len(self.sim.state["agents"])
        if not isinstance(args, dict):
            return None, "bad_args"
        if verb == "fire":
            ok = set(args) == {"family"} and _is_int(args["family"]) and 0 <= args["family"] < len(self.sim.state["families"])
            act = {"kind": "fire", "family": args.get("family")}
        elif verb == "possess":
            ok = set(args) == {"agent"} and _is_int(args["agent"]) and 0 <= args["agent"] < n
            act = {"kind": "possess", "agent": args.get("agent")}
        elif verb == "release":
            ok, act = args == {}, {"kind": "release"}
        else:
            t, place, text = args.get("target"), args.get("place"), args.get("text")
            go, say = args.get("action") in GO_ACTS, args.get("action") in SAY_ACTS
            walk, to = args.get("action") in WALK_ACTS, args.get("to")  # walk-1: a tile, and nothing else
            move, price = args.get("move"), args.get("price")  # talk-1: the chip on a say, and a buy's shown price
            thing = args.get("thing")  # things-1: the thing a take, drop, use or hand names, by its id
            ok = set(args) <= {"action", "target", "place", "text", "move", "price", "to", "thing"} \
                and (thing is not None) == (args.get("action") in THING_ACTS) \
                and (thing is None or _is_int(thing) and thing >= 0 and (t is not None) == (args["action"] in HAND_ACTS)) \
                and isinstance(args.get("action"), str) \
                and (to is not None) == walk and (not walk or (t is None and on_map(self.sim.map, to))) \
                and ((move is None and price is None) or say and isinstance(move, str) and move in TALK_MOVES
                     and (move == "wants_to_buy_food") == (price is not None)
                     and (price is None or _is_int(price) and price >= 0)) \
                and (text is not None) == say and (not say or (_is_int(t) and isinstance(text, str)
                                                             and 0 < len(text) <= SAY_MAX and text.strip() != ""
                                                             and text.isprintable())) \
                and args["action"] in PLAYER_ACTIONS and (t is None or (_is_int(t) and 0 <= t < n)) \
                and (args["action"] not in HAIL_ACTS or t is not None) \
                and (place is None) != go and (not go or (t is None and isinstance(place, str)
                                                          and place in self.sim.map["places"]))
            act = {"kind": "act", "action": args.get("action")} if t is None else \
                {"kind": "act", "action": args.get("action"), "target": t}
            if go:
                act["place"] = place
            if walk and ok:
                act["to"] = list(to)
            if say:
                act["text"] = text
            if thing is not None:
                act["thing"] = thing
            if move is not None:
                act["move"] = move
                if price is not None:
                    act["price"] = price
        if not ok:
            return None, "bad_args"
        if not _is_int(seen) or not 0 <= seen <= now:
            return None, "bad_seen_tick"
        if now - seen > MAX_LAG:
            return None, "stale"
        if scope == "god" and verb != "fire" and self.inhabitant is not None:
            return None, "inhabited"  # the body is an inhabitant's: god scope keeps fire only
        if scope == "inhabitant" and (self.inhabitant is None or self._held_agent() != self.inhabitant["agent"]):
            return None, "no_body"
        held = self._held()
        if verb == "possess" and held:
            return None, "body_taken"  # release first: one action, one player event (see step)
        if verb in ("act", "release") and not held:
            return None, "no_body"
        return act, None

    def _stamped(self, act: dict, tick: int, f: int | None) -> dict:
        """time-1a (B2.1): the act with its time inside `tick`, f in SUBths, which only the runner sets. Acts
        apply in order of t, so it is never earlier than the act scheduled before it (a change of pace
        between two acts could say otherwise). Left out at 0. `f` None: no clock (a direct caller), no stamp."""
        if act["kind"] != "act" or f is None:
            return act
        ahead = [a for a in self.run.player.get(tick, []) if a["kind"] == "act"]
        f = max([min(max(int(f), 0), SUB - 1)] + [a.get("f", 0) for a in ahead])
        if act_cost(act["action"]):
            # time-1b: an act that arrives while the body is still held by the one before it lands the moment
            # the body is free, if that is inside this tick (the wait is a sim-minute at most); the engine
            # refuses one stamped earlier as "busy". Acts scheduled and not yet taken hold it too.
            free = max([self.sim.state.get("player", {}).get("busy", 0) - tick * SUB]
                       + [a.get("f", 0) + act_cost(a["action"]) for a in ahead if act_cost(a["action"])])
            f = min(max(f, free), SUB - 1)
        return {**act, "f": f} if f else act

    def submit(self, msg: dict, client: str = "local", scope: str = "god", f: int | None = None) -> dict | None:
        """Take an intent. Returns its ack now if it is refused or already decided, or None if it will be
        applied by the next step() (which returns its ack once its event is durable), or sooner by take_due().
        `f`: how far into the coming tick it arrived, in SUBths (the Hub's clock; 0 for a paused world; None
        for a caller with no clock, whose act is left unstamped)."""
        iid = msg.get("id")
        if not isinstance(iid, str) or not 1 <= len(iid) <= 64 or iid.startswith("~"):
            return self._ack(iid if isinstance(iid, str) else None, {"status": "rejected", "reason": "bad_id"})
        if iid in self.intents:
            rec = self.intents[iid]
            return None if rec["status"] == "pending" else self._ack(iid, rec)
        act, reason = self._check(msg, scope)
        if reason:
            rec = self.intents[iid] = {"status": "rejected", "reason": reason}
            self._side("rejected", id=iid, client=client, reason=reason, msg=_clip(msg))
            return self._ack(iid, rec)
        tick = self.sim.state["tick"]
        act = self._stamped(act, tick, f)
        rec = {"status": "pending", "tick": tick, "slot": len(self.run.player.get(tick, [])), "act": act,
               "client": client}
        self._side("received", sync=True, id=iid, client=client, tick=tick, slot=rec["slot"], act=act,
                   seen_tick=msg["seen_tick"])
        self.intents[iid] = rec
        self._schedule(rec)
        return None

    def take_due(self) -> list:
        """time-1a (B2.2): between ticks, hand the engine every action scheduled for the coming tick that it
        hasn't had yet (Sim.take), in slot order, so each is recorded as it arrives, a walk moves the body at
        once and (time-1b) every act but a line said lands at once. Those are settled by that: each is acked
        here, its `received` record durable before, and since its outcome depends only on the actions before
        it, a resume re-runs it to the same event (step() checks). Returns (intent id, ack, the events the
        inhabitant sees of it, the inhabitant's new view) for each act settled; the last two are empty and
        None for one that isn't the inhabitant's. A line said, and anything that isn't an act, waits for its
        tick as before. Without this call the step applies them all, to the same log."""
        sim, tick = self.sim, self.sim.state["tick"]
        acts = self.run.player.get(tick, [])
        had = len(sim._taken) if sim._window == tick else 0
        if had >= sum(1 for a in acts if not a.get("late")):
            return []
        by_slot = {rec["slot"]: iid for iid, rec in self.intents.items() if rec.get("tick") == tick}
        out, n = [], 0
        for slot, act in enumerate(acts):
            if act.get("late"):  # recorded at the fixed point of an earlier run of this tick: the step applies it there
                continue
            n += 1
            if n <= had:
                continue
            n0 = len(sim.events)
            eid = sim.take(act)
            iid = by_slot.get(slot)
            if iid is None or act["kind"] != "act" or act["action"] in SAY_ACTS:
                continue
            rec = self.intents[iid]
            if rec["status"] == "applied":  # acked before a crash: the re-run must make the same event
                if rec["event"] != eid:
                    raise RuntimeError(f"tick {tick}: intent {iid!r} was acked as event {rec['event']}, "
                                       f"the re-run made {eid}")
                continue
            rec.update(status="applied", event=eid)
            self._side("applied", id=iid, tick=tick, event=eid, early=True)
            mine = self.inhabitant is not None and rec.get("client") == self.inhabitant["client"]
            aid = self.inhabitant["agent"] if mine else None
            out.append((iid, self._ack(iid, rec), self._seen(sim.events[n0:], aid),
                        inhabitant.view(sim, aid) if mine else None))
        return out

    def queue_late(self, m: dict, client: str, scope: str, send, f: int | None = None):
        """From the server thread while a tick runs (E4): an inhabitant's act for that tick's fixed point.
        `send(ack, events, view)` is called from the tick's thread with its ack, the events it made and, for a
        walk, the body's new view; or with (None, [], None) if it waits for the next tick, which it joins at
        `f` (how far into that tick's window it arrived) and whose step() returns its ack as usual."""
        with self.late_lock:
            self.late_q.append((m, client, scope, send, f))
        self.late_wake.set()

    def leftover_late(self) -> list:
        """Late acts queued after the tick's fixed point had closed: the Hub submits them for the next tick."""
        with self.late_lock:
            batch, self.late_q = self.late_q, []
        return batch

    def _during(self, sim, done):
        """The tick's fixed point, while the minds' calls are out: take late acts until the calls are back
        and none is queued. Runs in the tick's thread, the only one touching the world."""
        self._ack_scheduled(sim)
        while True:
            with self.late_lock:
                batch, self.late_q = self.late_q, []
                self.late_wake.clear()
            for m, client, scope, send, f in batch:
                send(*self._take_late(m, client, scope, f))
            if not batch and done():
                with self.late_lock:
                    if not self.late_q:
                        return
                continue
            if not batch:
                self.late_wake.wait(0.01)

    def _seen(self, events, aid) -> list[dict]:
        """Of `events` (this tick's, so far), what the inhabitant `aid` gets, as its tick message would."""
        if aid is None:
            return []
        here, tick = set(inhabitant.sight_ids(self.sim, aid)), self.sim.state["tick"]
        near = set(inhabitant.near_ids(self.sim, aid))  # reach-1: words and doings need its place or reach
        return [inhabitant.scrub(e, aid, self.sim.events, near) for e in event_dicts(events)
                if inhabitant.witnessed(e, aid, here, tick, near)]

    def _ack_scheduled(self, sim):
        """At the fixed point, the tick's intents scheduled before it began have applied: ack them now, with
        what they made so far, instead of after the minds' calls (E4). Only with a Hub listening."""
        if self.early is None or self.inhabitant is None:
            return
        tick, me = sim.state["tick"], self.inhabitant["client"]
        due = sorted((rec["slot"], iid) for iid, rec in self.intents.items()  # the inhabitant's acts only
                     if rec["status"] == "pending" and rec["tick"] == tick and rec.get("client") == me
                     and rec["act"]["kind"] == "act")
        if not due:
            return
        evs = sim.events[self._tick_n0:]
        player = [e for e in evs if e.origin == "player"]
        aid = self.inhabitant["agent"] if self.inhabitant else None
        for slot, iid in due:
            if slot >= len(player):  # a late one, recorded in an earlier run of this tick: applies at _play
                continue
            rec = self.intents[iid]
            rec.update(status="applied", event=player[slot].id)
            self._side("applied", id=iid, tick=tick, event=player[slot].id, early=True)
            mine = [e for e in evs if e.id == player[slot].id or (e.parents and e.parents[0] == player[slot].id)]
            self.early(iid, self._ack(iid, rec), self._seen(mine, aid))

    def _take_late(self, m: dict, client: str, scope: str, f: int | None = None) -> tuple[dict | None, list, dict | None]:
        """submit(), at the fixed point: the act applies now if it can (Sim.takes_late), its `received`
        record durable first. Its outcome depends only on what came before the fixed point, so a resume
        re-runs it to the same event (Steering, 10:09Z). One that can't join waits for the next tick, at `f`.
        One that joins is stamped at the running tick's last sim-second (time-1a, B2.1): the tick's sim-time
        stands still while it computes. A walk also returns the body's new view."""
        sim, tick = self.sim, self.sim.state["tick"]
        iid = m.get("id")
        if not isinstance(iid, str) or not 1 <= len(iid) <= 64 or iid.startswith("~"):
            return self._ack(iid if isinstance(iid, str) else None, {"status": "rejected", "reason": "bad_id"}), [], None
        if iid in self.intents:
            rec = self.intents[iid]
            return (None if rec["status"] == "pending" else self._ack(iid, rec)), [], None
        act, reason = self._check(m, scope)
        if reason:
            rec = self.intents[iid] = {"status": "rejected", "reason": reason}
            self._side("rejected", id=iid, client=client, reason=reason, msg=_clip(m))
            return self._ack(iid, rec), [], None
        late = act["kind"] == "act" and sim.takes_late({**act, "f": SUB - 1})
        at = tick if late else tick + 1
        act = self._stamped(act, at, SUB - 1 if late else f)
        if late:
            act = {**act, "late": True}
        rec = {"status": "pending", "tick": at, "slot": len(self.run.player.get(at, [])), "act": act, "client": client}
        self._side("received", sync=True, id=iid, client=client, tick=at, slot=rec["slot"], act=act,
                   seen_tick=m["seen_tick"], late=late)
        self.intents[iid] = rec
        self._schedule(rec)
        if not late:
            return None, [], None
        n0 = len(sim.events)
        eid = sim.late_act(act)
        rec.update(status="applied", event=eid)
        self._side("applied", id=iid, tick=tick, event=eid, early=True)
        aid = self.inhabitant["agent"] if self.inhabitant else None
        view = inhabitant.view(sim, aid) if aid is not None else None
        return self._ack(iid, rec), self._seen(sim.events[n0:], aid), view

    @staticmethod
    def _ack(iid, rec: dict) -> dict:
        out = msg("ack", id=iid, status=rec["status"])
        if rec["status"] == "applied":
            out.update(tick=rec["tick"], event=rec["event"])
        else:
            out["reason"] = rec["reason"]
        return out

    # ------------------------------------------------------------------ stepping
    def step(self) -> tuple[int, list, list[dict]]:
        """Run one tick. Returns (the tick that ran, its events, acks for the intents it applied)."""
        sim, tick = self.sim, self.sim.state["tick"]
        if self.orphan_at is not None and tick >= self.orphan_at:
            self.orphan_at = None
            if self.inhabitant is None and self._held():
                self._internal({"kind": "release"})  # reopened with a body held and nobody came back for it
        n0, c0, t0 = self._tick_n0, len(getattr(self.mind, "calls", ())), time.time()  # from what was taken early
        self.run.advance(tick + 1, during=self._during)
        events = sim.events[n0:]
        self._tick_n0 = len(sim.events)
        lat = [c["latency_s"] for c in getattr(self.mind, "calls", [])[c0:]]
        self._side("step", tick=tick, wall_s=round(time.time() - t0, 3), calls=len(lat),
                   lat_max_s=max(lat, default=0), lat_sum_s=round(sum(lat), 3))
        due = sorted((rec["slot"], iid) for iid, rec in self.intents.items()
                     if rec["status"] == "pending" and rec["tick"] == tick)
        acks = []
        early = [(rec["slot"], iid) for iid, rec in self.intents.items()
                 if rec["status"] == "applied" and rec["tick"] == tick and "slot" in rec]
        if early:  # acked at the fixed point: a re-run of this tick (resume) must make the same event
            player = self._slot_events(events, tick)
            for slot, iid in early:
                if player[slot].id != self.intents[iid]["event"]:
                    raise RuntimeError(f"tick {tick}: intent {iid!r} was acked as event "
                                       f"{self.intents[iid]['event']}, the re-run made {player[slot].id}")
        if due:
            os.fsync(self.run.log.fileno())  # acked only once durable
            player = self._slot_events(events, tick)
            for slot, iid in due:
                rec = self.intents[iid]
                rec.update(status="applied", event=player[slot].id)
                self._side("applied", id=iid, tick=tick, event=player[slot].id)
                acks.append(self._ack(iid, rec))
        return tick, events, acks

    def finish(self):
        """End the recording here (an end event). A live world normally stays open: use close()."""
        self.run.finish()
        self.side.close()

    def close(self):
        """Stop serving; the world stays open. A snapshot at the current tick lets reopen continue right here
        instead of taking resume's crash path, which re-runs the last recorded tick."""
        if not os.path.exists(self.run._snap_path(self.sim.state["tick"])) and len(self.sim.events) == self.run.hashed:
            self.run._snapshot()  # not over actions taken early (time-1a): they aren't in the log before their tick
        os.fsync(self.run.log.fileno())
        self.run.log.close()
        self._side("close", view_tick=self.sim.state["tick"])
        self.side.close()

    # ------------------------------------------------------------------ what clients see
    def view(self) -> dict:
        """Every villager at the current view tick, with the engine's own position."""
        sim, tick = self.sim, self.sim.state["tick"]
        agents = []
        for a in sim.state["agents"]:
            r = a["route"]
            pos = sim.tile(a)  # space-1: its slot at a place, a point on its walk, or the tile the player walked it to
            agents.append({"id": a["id"], "place": a["place"], "activity": a["activity"], "asleep": a["asleep"],
                           "pos": list(pos), "route": None if r is None else
                           {"from": r["from"], "to": r["to"], "depart": r["depart"], "arrive": r["arrive"]}})
        return {"tick": tick, "agents": agents}

    def inhabitant_tick(self, tick: int, events: list[dict], aid: int) -> dict:
        """A tick as an inhabitant gets it: the events it witnessed and its own whitelisted view."""
        here, near = set(inhabitant.sight_ids(self.sim, aid)), set(inhabitant.near_ids(self.sim, aid))
        return msg("tick", tick=tick, events=[inhabitant.scrub(e, aid, self.sim.events, near) for e in events
                                              if inhabitant.witnessed(e, aid, here, tick, near)],
                   view=inhabitant.view(self.sim, aid))

    def welcome(self, scope: str, paused: bool, tps: float) -> dict:
        if scope == "inhabitant":
            aid = self.inhabitant["agent"]
            return msg("welcome", scope=scope, view_tick=self.sim.state["tick"], paused=paused, tps=tps,
                       verbs={"act": self.verbs()["act"]}, world=inhabitant.welcome_world(self.sim, aid),
                       talk={k: self.sim._talk_params()[k] for k in ("consent", "typed")},  # text-1: for the say box
                       view=inhabitant.view(self.sim, aid))
        g = self.sim.events[0].data
        return msg("welcome", scope=scope, view_tick=self.sim.state["tick"], n_events=len(self.sim.events),
                   paused=paused, tps=tps, verbs=self.verbs(),
                   world={"seed": g["seed"], "order": g["order"], "map": g["map"], "population": g["population"],
                          "ticks_per_day": TICKS_PER_DAY})


def msg(type_: str, **fields) -> dict:
    return {"type": type_, "protocol": PROTOCOL, "engine": ENGINE_VERSION, **fields}


def event_dicts(events) -> list[dict]:
    return [json.loads(e.to_json()) for e in events]


def _clip(m: dict) -> str:
    return json.dumps(m, sort_keys=True)[:300]


# ---------------------------------------------------------------------- WebSocket (RFC 6455, server side)
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSError(Exception):
    pass


def frame(op: int, payload: bytes) -> bytes:
    n = len(payload)
    if n < 126:
        head = struct.pack(">BB", 0x80 | op, n)
    elif n < 1 << 16:
        head = struct.pack(">BBH", 0x80 | op, 126, n)
    else:
        head = struct.pack(">BBQ", 0x80 | op, 127, n)
    return head + payload


def _unmask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")


async def handshake(reader, writer) -> bool:
    try:
        req = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return False
    lines = req.decode("latin-1").split("\r\n")
    headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:] if l)}
    key = headers.get("sec-websocket-key")
    if not lines[0].startswith("GET ") or "websocket" not in headers.get("upgrade", "").lower() or not key:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        return False
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
    return True


async def read_message(reader, send) -> str | None:
    """The next text message, or None when the client closes. Answers pings through `send`."""
    parts, opcode = [], None
    while True:
        b1, b2 = await reader.readexactly(2)
        fin, op, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
        if b1 & 0x70 or not b2 & 0x80:
            raise WSError("reserved bits set or unmasked client frame")
        if n == 126:
            (n,) = struct.unpack(">H", await reader.readexactly(2))
        elif n == 127:
            (n,) = struct.unpack(">Q", await reader.readexactly(8))
        if op >= 8 and (not fin or n > 125):
            raise WSError("bad control frame")
        if n + sum(map(len, parts)) > MAX_FRAME:
            raise WSError("message too large")
        mask = await reader.readexactly(4)
        data = _unmask(await reader.readexactly(n), mask)
        if op == 8:
            return None  # Conn.close() answers it
        if op == 9:
            send(frame(10, data))
            continue
        if op == 10:
            continue
        if op in (1, 2):
            if opcode is not None:
                raise WSError("new message inside a fragmented one")
            opcode = op
        elif op != 0 or opcode is None:
            raise WSError(f"unexpected opcode {op}")
        parts.append(data)
        if fin:
            if opcode != 1:
                raise WSError("binary messages are not part of the protocol")
            try:
                return b"".join(parts).decode("utf-8")
            except UnicodeDecodeError as e:
                raise WSError("text frame is not UTF-8") from e


class Conn:
    """One client: its scope once it has said hello, and a send queue drained by its own task."""
    count = 0

    def __init__(self, writer):
        Conn.count += 1
        self.name = f"c{Conn.count}"
        self.writer, self.queue = writer, asyncio.Queue()
        self.scope, self.ready, self.closed, self.agent = None, False, False, None
        self.task = asyncio.ensure_future(self._pump())

    def send_raw(self, data: bytes):
        if self.closed:
            return
        if self.queue.qsize() >= SEND_QUEUE:
            self.close()
            return
        self.queue.put_nowait(data)

    def send(self, m: dict | str):
        self.send_raw(frame(1, (m if isinstance(m, str) else json.dumps(m)).encode()))

    def close(self, error: str | None = None):
        if error and not self.closed:
            self.queue.put_nowait(frame(1, json.dumps(msg("error", reason=error)).encode()))
        if not self.closed:  # the close frame repeats the reason (code 4000): some clients drop data at close
            self.queue.put_nowait(frame(8, struct.pack(">H", 4000) + error.encode()[:120] if error else struct.pack(">H", 1000)))
        self.closed = True
        self.queue.put_nowait(None)

    async def _pump(self):
        try:
            while (data := await self.queue.get()) is not None:
                self.writer.write(data)
                await self.writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self.closed = True
            self.writer.close()


class Hub:
    """Paces the runner and fans out to clients. Everything that touches the runner happens between ticks,
    in the tick loop; a tick itself runs in a worker thread so the server keeps reading while it does."""

    def __init__(self, runner: LiveRunner, tps: float = 1.0, paused: bool = False, token: str | None = None,
                 pull_min: float | None = PULL_MIN_S, narrator=None):
        self.runner, self.tps, self.paused, self.token = runner, tps, paused, token
        # E3 (plan/readable-world-spec.md): an inhabitant's act pulls the next tick forward, at least
        # pull_min seconds after the last one started, instead of waiting out the pace. None: never.
        self.pull_min, self.pull = pull_min, False
        self.pull_why = None  # "reply": the pull is for the answer to the player's line (pt8)
        self.reply_hold = None  # script-voice: a ReplyHold, so the script's answers come as late as a mind's
        # E2 (plan/readable-world-spec.md): words for what the inhabitant sees, written after each tick into
        # narration.jsonl and sent as `heard`. Never part of the world: the log and replay don't see them.
        self.narrator = narrator
        self.narr = open(os.path.join(runner.out, "narration.jsonl"), "a") if narrator is not None else None
        self.narr_slots = asyncio.Semaphore(8)
        self.narr_tasks: set = set()  # held, so a pending narration isn't collected (review of #61)
        self.recent = narrate.Recent()  # each villager's own events of the last day: the words' topics (E2c)
        self.conns: set[Conn] = set()
        self.inbox: list[tuple[Conn, dict]] = []
        self.wake = asyncio.Event()
        self.waiters: dict[str, Conn] = {}
        self.step_once, self.stopping = False, False
        self.stepping = False  # a tick is running in its thread: an inhabitant's act joins it (E4)
        self.loop = None
        self.last = None  # when the last tick started (the loop's clock); None on start and on leaving pause
        self.early: dict = {}  # conn -> ids of events it got early this tick, left out of its tick message
        runner.early = self._early_scheduled

    async def handle(self, reader, writer):
        if not await handshake(reader, writer):
            self.runner._side("refused", reason="handshake")  # pt10: a join that never arrived left no trace
            writer.close()
            return
        conn = Conn(writer)
        self.conns.add(conn)
        try:
            while (text := await read_message(reader, conn.send_raw)) is not None:
                try:
                    m = json.loads(text)
                    if not isinstance(m, dict) or not isinstance(m.get("type"), str):
                        raise ValueError
                except (ValueError, RecursionError):
                    m = None
                if conn.scope is None:
                    err = _hello_error(m) or (self.token is not None and not (isinstance(m.get("token"), str) and
                        hmac.compare_digest(m["token"].encode(), self.token.encode())) and "bad_token")
                    if err:
                        m = m or {}
                        self.runner._side("refused", reason=err, client=conn.name,  # clipped: unauthenticated input
                                          **{k: repr(m.get(k))[:40] for k in ("scope", "agent", "protocol")})
                        conn.close(err)
                        break
                    conn.scope = m.get("scope", "god")
                elif m is None or m.get("type") not in ("intent", "control"):
                    conn.send(msg("ack", id=None, status="rejected", reason="bad_message"))
                    continue
                elif self.stepping and conn.scope == "inhabitant" and conn.ready and m["type"] == "intent" \
                        and m.get("verb") == "act":
                    self._late(conn, m)
                    continue
                self.inbox.append((conn, m))
                self.wake.set()
        except (WSError, asyncio.IncompleteReadError, ConnectionError, OSError) as e:
            conn.close(f"bad_frame: {e}" if isinstance(e, WSError) else None)
        finally:
            conn.close()
            self.conns.discard(conn)
            if conn.scope == "inhabitant":  # its body is released between ticks, like any intent
                self.inbox.append((conn, {"type": "leave"}))
                self.wake.set()

    def _late(self, conn: "Conn", m: dict):
        """E4: an inhabitant's act while a tick runs goes to that tick's fixed point, and its ack and receipt
        come back as soon as it applies, before the minds' calls return."""
        loop = asyncio.get_running_loop()
        if isinstance(m.get("id"), str):
            self.waiters[m["id"]] = conn  # if it waits for the next tick, that step's acks answer it

        def send(ack, evs, view):
            loop.call_soon_threadsafe(self._early_ack, conn, ack, evs, m, view)
        self.runner.queue_late(m, conn.name, conn.scope, send, self.stamp())

    def stamp(self) -> int:
        """time-1a (B2.1): f for an act arriving now, in SUBths of a tick: the real time since the last tick
        started over the pace, clamped below 1 (Steering 2026-11-23 19:03Z). An act for the coming tick
        arrives while the last one computes or in the wait after it, so f starts at the compute time over the
        pace. 0 in a paused world, and before the first tick."""
        if self.paused or self.last is None or self.loop is None:
            return 0
        return max(0, min(int((self.loop.time() - self.last) * self.tps * SUB), SUB - 1))

    def _early_scheduled(self, iid: str, ack: dict, evs: list):
        """From the tick's thread: an intent scheduled before the tick applied at its fixed point."""
        loop = self.loop
        conn = self.waiters.get(iid)
        if conn is not None and loop is not None:
            loop.call_soon_threadsafe(self._early_ack, conn, ack, evs)

    def _early_ack(self, conn: "Conn", ack, evs: list, m: dict | None = None, view: dict | None = None):
        if ack is None:  # it waits for the next tick: pull that one forward (never for a walk)
            self.pull = self.pull or m is None or self.runner.pulls(m)
            return
        if ack.get("id") is not None:
            self.waiters.pop(ack["id"], None)
        if conn not in self.conns:
            return
        if (evs or view is not None) and conn.scope == "inhabitant":
            tick = evs[0]["tick"] if evs else view["tick"]
            conn.send(msg("early", tick=tick, events=evs, **({} if view is None else {"view": view})))
            self.early.setdefault(conn, set()).update(e["id"] for e in evs)
        conn.send(ack)

    def _took(self):
        """time-1a: between ticks, what has arrived is recorded now, and a walk moves the body and is
        answered now, with its `moved` event and the body's new view ahead of its ack."""
        for iid, ack, evs, view in self.runner.take_due():
            conn = self.waiters.get(iid)
            if conn is not None:
                self._early_ack(conn, ack, evs, None, view)

    def _drain(self):
        items, self.inbox = self.inbox, []
        for conn, m in items:
            t = m["type"]
            if t == "hello":
                if conn.scope == "inhabitant":
                    why = self.runner.inhabit(m.get("agent"), conn.name)
                    if why:
                        conn.scope = None  # nothing to release
                        conn.close(why)
                        continue
                    conn.agent = m["agent"]
                self._welcome(conn, m.get("since", 0))
            elif t == "leave":
                self.runner.leave(conn.name)
            elif t == "intent":
                ack = self.runner.submit(m, client=conn.name, scope=conn.scope, f=self.stamp())
                if ack is None:
                    self.waiters[m["id"]] = conn
                    if conn.scope == "inhabitant" and m.get("verb") == "act" and self.runner.pulls(m):
                        self.pull = True
                else:
                    conn.send(ack)
            elif t == "control":
                self._control(conn, m)

    def _welcome(self, conn: Conn, since: int):
        events = self.runner.sim.events
        if since > len(events):
            conn.close("bad_since")
            return
        conn.send(self.runner.welcome(conn.scope, self.paused, self.tps))
        if conn.scope == "inhabitant":  # its own, the ones naming it and public ones; no co-presence history
            mine = [inhabitant.scrub(e, conn.agent, events) for e in event_dicts(events[since:])
                    if inhabitant.witnessed(e, conn.agent, set(), -1)]
            for i in range(0, len(mine), BACKLOG_CHUNK):
                conn.send(msg("events", events=mine[i:i + BACKLOG_CHUNK]))
        else:
            for i in range(since, len(events), BACKLOG_CHUNK):
                conn.send(msg("events", events=event_dicts(events[i:i + BACKLOG_CHUNK])))
        conn.ready = True
        self.runner._side("client", client=conn.name, scope=conn.scope, since=since)

    def _control(self, conn: Conn, m: dict):
        op, tps = m.get("op"), m.get("tps")
        if conn.scope != "god" or op not in ("pause", "run", "step", "speed") or \
                (op == "speed" and not (isinstance(tps, (int, float)) and not isinstance(tps, bool) and 0.01 <= tps <= 1000)):
            conn.send(msg("error", reason="bad_control"))
            return
        if op == "pause":
            self.paused = True
        elif op == "run":
            self.paused = False
        elif op == "step":
            self.step_once = True
        else:
            self.tps = float(tps)
        self.runner._side("control", client=conn.name, op=op, tps=self.tps)
        status = json.dumps(msg("status", paused=self.paused, tps=self.tps, view_tick=self.runner.sim.state["tick"]))
        for c in self.conns:
            if c.ready:
                c.send(status)

    async def tick_loop(self):
        loop = self.loop = asyncio.get_running_loop()
        self.last = None  # when the last tick started; None steps at once (on start and on leaving pause)
        while not self.stopping:
            self._drain()
            self._took()
            if self.paused and not self.step_once:
                self.wake.clear()
                await self.wake.wait()
                self.last = None
                continue
            last = self.last
            wait = 0 if self.step_once or last is None else last + 1 / self.tps - loop.time()
            if self.pull and self.pull_min is not None and last is not None:  # the player acted: don't wait out the pace
                wait = min(wait, last + self.pull_min - loop.time())
            if wait > 0:  # a message is drained and the wait resumes, at the tps it now has: only time steps
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), wait)
                except asyncio.TimeoutError:
                    pass
                continue
            self.step_once, self.last, pulled = False, loop.time(), self.pull
            self.pull, why = False, self.pull_why
            self.pull_why = None
            if pulled:  # logged before the tick: its early ack can end a session before the tick does (E4)
                self.runner._side("pulled", tick=self.runner.sim.state["tick"], **({"why": why} if why else {}))
            self.stepping = True
            try:
                tick, events, acks = await asyncio.to_thread(self.runner.step)
            finally:
                self.stepping = False
            if self.reply_hold is not None:
                await self.reply_hold.hold(self.runner, events)
            p = self.runner.sim.state.get("player")
            if self.pull_min is not None and p is not None and any(
                    e.kind == "said" and e.actor == p["agent"] for e in events):
                # pt8 (Steering 2026-11-16 10:40Z): a line said this tick is answered in the next one, so pull
                # that tick too; the player's words would otherwise wait out the pace for their answer.
                self.pull, self.pull_why = True, "reply"
            for m, client, scope, _, f in self.runner.leftover_late():  # came as the fixed point closed
                ack = self.runner.submit(m, client=client, scope=scope, f=f)
                if ack is None:
                    self.pull = self.pull or self.runner.pulls(m)
                else:
                    c = self.waiters.pop(ack["id"], None) if isinstance(ack.get("id"), str) else None
                    if c:
                        c.send(ack)
            await asyncio.sleep(0)  # early acks the tick's thread queued go out before its tick message
            early, self.early = self.early, {}
            dicts = event_dicts(events)
            text = json.dumps(msg("tick", tick=tick, events=dicts, view=self.runner.view()))
            for c in self.conns:
                if c.ready and c.scope == "inhabitant":
                    sent = early.get(c, ())
                    c.send(self.runner.inhabitant_tick(tick, [e for e in dicts if e["id"] not in sent], c.agent))
                elif c.ready:
                    c.send(text)
            for ack in acks:
                c = self.waiters.pop(ack["id"], None)
                if c:
                    c.send(ack)
            if self.narrator is not None:
                self.recent.feed(self.runner.sim, events)
                for c in self.conns:
                    if c.ready and c.scope == "inhabitant":
                        for it in narrate.items(self.runner.sim, events, c.agent, self.recent):
                            t = asyncio.create_task(self._narrate(c, it))
                            self.narr_tasks.add(t)
                            t.add_done_callback(self.narr_tasks.discard)

    async def _narrate(self, conn: "Conn", it: dict):
        async with self.narr_slots:
            lines = await asyncio.to_thread(self.narrator.lines, it)
        agents = self.runner.sim.state["agents"]
        # pt8: each line says whom it is to (an id) or that it is thinking aloud ("self", shown apart, no bubble)
        out = [{"who": x["who"], "name": agents[x["who"]]["name"], "text": x["text"], "to": x["to"],
                "to_name": None if x["to"] == "self" else agents[x["to"]]["name"], "to_me": x["to"] == it["player"]}
               for x in narrate.addressed(it, lines)]
        self.narr.write(json.dumps({"wall": round(time.time(), 3), "tick": it["tick"], "event": it["event"],
                                    "kind": it["kind"], "player": it["player"], "to_player": it["to_player"],
                                    "narrator": self.narrator.model, "fallback": it.get("fallback", False),
                                    "latency_s": it.get("latency_s"), "prompt_facts": it["people"],
                                    "topics": it.get("topics", []), "denied": it.get("denied", []),
                                    "repeated": it.get("repeated", []), "dropped": it.get("dropped", False),
                                    "lines": out},
                                   sort_keys=True) + "\n")
        self.narr.flush()
        if out and conn.ready and conn.agent == it["player"] and conn in self.conns:  # a dropped exchange: no words
            conn.send(msg("heard", tick=it["tick"], event=it["event"], kind=it["kind"], to_player=it["to_player"],
                          lines=out))


def _hello_error(m) -> str | None:
    if m is None or m.get("type") != "hello":
        return "expected_hello"
    if not _is_int(m.get("protocol")) or m["protocol"] != PROTOCOL:
        return "unknown_protocol"
    scope = m.get("scope", "god")
    if scope not in SCOPES:
        return "bad_scope"
    if scope == "inhabitant" and not _is_int(m.get("agent")):
        return "bad_agent"  # its range is the runner's to check
    since = m.get("since", 0)
    if not _is_int(since) or since < 0:
        return "bad_since"
    return None



class ReplyHold:
    """script-voice (Steering 2026-11-16 10:18Z, plan/test16-live-rule.md): a scripted villager's reply to the
    player is held until a delay drawn from the minds arm's frozen click-to-reply distribution has passed since
    the click, so the script's answers don't come back faster than a mind's. Only the tick's fan-out waits;
    the world log is untouched. Each draw is seeded and logged in runner.jsonl as `held`."""

    def __init__(self, delays: list[float], seed: str):
        if not delays or any(not (0 <= d <= 60) for d in delays):
            raise ValueError("reply delays: a non-empty list of seconds in [0, 60]")
        self.delays, self.rng = sorted(float(d) for d in delays), random.Random(f"reply-hold-{seed}")

    async def hold(self, runner, events):
        p = runner.sim.state.get("player")
        if p is None:
            return
        for e in events:
            if e.kind != "reply" or e.data.get("with") != p["agent"] or e.actor not in runner.say_wall:
                continue
            click = runner.say_wall.pop(e.actor)
            drawn = self.rng.choice(self.delays)
            wait = max(0.0, click + drawn - time.time())
            runner._side("held", reply=e.id, listener=e.actor, drawn_s=drawn, wait_s=round(wait, 3))
            if wait > 0:
                await asyncio.sleep(wait)
            # Review of #81: the `step` record is written before the hold, so a reader timing the reply from it
            # would see the unheld time. `sent` is the wall the held reply actually went out; readers prefer it.
            runner._side("sent", reply=e.id, tick=e.tick)


async def serve(runner: LiveRunner, host: str = "127.0.0.1", port: int = 8765, tps: float = 1.0,
                paused: bool = False, started=None, token: str | None = None, pull_min: float | None = PULL_MIN_S,
                narrator=None, reply_hold=None):
    """Run the hub until cancelled. `started(port, hub)` is called once the socket listens."""
    hub = Hub(runner, tps, paused, token, pull_min, narrator)
    hub.reply_hold = reply_hold
    server = await asyncio.start_server(hub.handle, host, port, limit=1 << 16)
    if started:
        started(server.sockets[0].getsockname()[1], hub)
    try:
        await hub.tick_loop()
    finally:
        hub.stopping = True
        server.close()  # not wait_closed(): it would wait on every open client's read
        for c in list(hub.conns):
            c.close()
            c.writer.close()


def warm_up(clients, log=lambda *a: print(*a, file=sys.stderr, flush=True),
            grammars=()) -> list[tuple[str, float | None]]:
    """One short call to each model server before the world opens (Steering 2026-11-18 05:42Z): aipt2's line-7
    p95 of 6.08 s was its first, cold reply. Not a mind call: nothing is logged in the world or in calls. Each
    server (url, model) once; a failure is printed and the world opens anyway. Returns (label, seconds|None).
    `grammars` (text-1): (name, schema) pairs a structured mind is also asked once with, so the first private
    decision and the first typed answer don't pay their schema compile inside the reply the player waits on."""
    out, seen = [], set()
    for c in clients:
        if c is None or not hasattr(c, "post") or (c.base_url, c.model) in seen:
            continue
        seen.add((c.base_url, c.model))
        # The answer's own grammar for a structured mind, so its first reply doesn't pay the schema compile;
        # json_object for the narrator, as it asks (review of #89).
        fmt = ({"type": "json_schema", "json_schema": {"name": "answer", "schema": SCHEMAS["answer"], "strict": True}}
               if getattr(c, "structured", False) else {"type": "json_object"})
        body = {"model": c.model, "messages": [{"role": "user", "content": 'Say hello. Answer as JSON: {"says": "..."}'}],
                "max_tokens": 24, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False},
                "response_format": fmt}
        if getattr(c, "answer_first", False):
            body["priority"] = 1  # a fcfs server refuses this now, at startup, not partway through (review of #94)
        t0 = time.monotonic()
        retries = getattr(c, "retries", None)
        try:
            if retries is not None:
                c.retries = 0  # one try: a dead server shouldn't hold the world shut for minutes (review of #89)
            c.post("/v1/chat/completions", body)
            dt = round(time.monotonic() - t0, 2)
        except Exception as e:  # noqa: BLE001 - a cold server must not keep the world shut
            dt = None
            log(f"warm-up {c.model}: failed ({type(e).__name__}: {str(e)[:120]})")
            if getattr(c, "answer_first", False) and "400" in str(e):
                raise SystemExit(f"--answer-first: {c.model} refused a priority; start vLLM with "
                                 "--scheduling-policy priority") from e
        else:
            log(f"warm-up {c.model}: {dt} s")
            for name, schema in (grammars if getattr(c, "structured", False) else ()):
                t0 = time.monotonic()
                try:
                    c.post("/v1/chat/completions", {**body, "max_tokens": 32, "response_format": {
                        "type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}})
                    log(f"warm-up {c.model} ({name}): {round(time.monotonic() - t0, 2)} s")
                except Exception as e:  # noqa: BLE001
                    log(f"warm-up {c.model} ({name}): failed ({type(e).__name__}: {str(e)[:120]})")
        finally:
            if retries is not None:
                c.retries = retries
        out.append((c.model, dt))
    return out


def talk_params(consent: str | None, typed: bool, think: int = 0) -> dict | None:
    """A new world's talk parameters as genesis records them: only what was asked for, so a world started with
    neither flag has none and is a default world (its log is engine 35's but for the engine field). A held world
    (consent private, or typed) also states where "They look hungry" starts (text-1, ruling (b))."""
    talk = {**({"consent": consent} if consent and consent != "nature" else {}), **({"typed": 1} if typed else {})}
    if typed or consent == "private":
        talk["hungry_at"] = TALK_DEFAULTS["hungry_at"]
    if think:  # think-1: only when asked for, as the others
        talk["think"] = think
    return talk or None


def narrator_from_args(args, kind: str):
    """The same model narrates both sessions of a blind pair (Steering 2026-11-14): --narrate-url for a
    scripted world, or the llm mind's own server. A template only when no model is given."""
    url = args.narrate_url or (args.llm_url if kind == "llm" else None)
    model = args.narrate_model or (args.llm_model if kind == "llm" else None)
    key = args.narrate_key or (args.llm_key if kind == "llm" else None)
    if (url is None or model is None) and getattr(args, "pod", None):
        from .run import llm_from_args
        m = llm_from_args(args)[0]
        url, model, key = url or m.base_url, model or m.model, key or m.api_key
    choice = args.narrate or ("model" if url and model else "template")
    if choice == "off":
        return None
    if choice == "template":
        return narrate.TemplateNarrator()
    if not (url and model):
        raise SystemExit("--narrate model needs --narrate-url and --narrate-model (or an llm mind)")
    return narrate.LLMNarrator(url, model, key)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="runs/live", help="run directory; reopened if it holds a world")
    ap.add_argument("--agents", type=int, default=25)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--mind", choices=["stub", "noisy", "script", "llm"], default=None,
                    help="default stub; script: the stub saying fixed sentences (test 16's blind pair); on reopen, the run's")
    ap.add_argument("--pod", help="pod id from scripts/runpod_pod.py (url, model and key)")
    ap.add_argument("--llm-url")
    ap.add_argument("--llm-model")
    ap.add_argument("--llm-key")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--answer-first", action="store_true",
                    help="llm mind: spoken answers ahead of plans (the model pod needs --vllm-args "
                         "'--scheduling-policy priority')")
    ap.add_argument("--plan-lane", type=int, default=8,
                    help="llm mind: at most this many plan and reflect calls at the model server at once; answers, "
                         "decisions and hearings skip the lane (Steering 2026-11-25, aipt4's dawn). 0: no lane")
    ap.add_argument("--consent", choices=list(CONSENTS), default=None,
                    help="talk-1, a new world only: who answers a line said with a chip. nature (the default): "
                         "today's roll; mind: the villager's mind (a 32B session); private (text-1): the villager's "
                         "mind, asked before any call reads the player's words, once a day per speaker")
    ap.add_argument("--things", action="store_true",
                    help="things-1, a new world only: things lie in the world from the first possess (berries and "
                         "flowers in the woods, the stall, a bench at each workplace, a bed in each home), and a player "
                         "can pick them up, put them down, use them and hand them over")
    ap.add_argument("--typed", action="store_true",
                    help="text-1, a new world only: a line typed with no chip may be given 1 food, on the decision "
                         "already held (consent private or nature). A script mind needs --hear-url to hear an ask")
    ap.add_argument("--think", type=int, choices=[1, 2], default=0,
                    help="think-1, a new world with --consent private: the private decision thinks first, in one "
                         "short sentence (1) or two or three (2); the thought is logged and never shown")
    ap.add_argument("--hear-url", help="script mind with --typed: the model server that hears whether a line asks "
                    "for food (the minds arm's model), with --hear-model and --hear-key")
    ap.add_argument("--hear-model")
    ap.add_argument("--hear-key")
    ap.add_argument("--workers", type=int, default=None, help="concurrent mind calls (default 64 for llm, else 1)")
    ap.add_argument("--token", default=None, help="every hello must carry this token (use it on a public port)")
    ap.add_argument("--tps", type=float, default=1.0, help="ticks per wall-clock second")
    ap.add_argument("--paused", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--script-voice", action="store_true",
                    help="script mind only: the narrator words its answers to the player (script-voice)")
    ap.add_argument("--reply-delays", help="JSON list of seconds: hold the script's answers to the player "
                    "for a delay drawn from it (the minds arm's frozen click-to-reply distribution)")
    ap.add_argument("--pull-min", type=float, default=PULL_MIN_S,
                    help="an inhabitant's act runs the next tick this many seconds after the last at the soonest (E3); "
                         "negative: wait out the pace as before")
    ap.add_argument("--narrate", choices=["model", "template", "off"], default=None,
                    help="words for what the inhabitant sees (E2). model: --narrate-url/--narrate-model, else the "
                         "llm mind's; default model when one is given, else template")
    ap.add_argument("--narrate-url")
    ap.add_argument("--narrate-model")
    ap.add_argument("--narrate-key")
    ap.add_argument("--no-warm-up", action="store_true", help="skip the one short call to each model server at start")
    args = ap.parse_args(argv)
    if args.token is None and args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"--host {args.host} is not loopback: pass --token")
    kind = args.mind
    if kind is None:
        kind = (persist.config(args.out).get("mind") if os.path.exists(os.path.join(args.out, "run.json")) else None) or "stub"
    if kind not in ("stub", "noisy", "script", "llm"):
        raise SystemExit(f"{args.out} was run with mind {kind!r}")
    if kind == "llm":
        from .run import llm_from_args
        mind = llm_from_args(args)[0]
    else:
        mind = {"stub": StubMind, "noisy": NoisyMind, "script": ScriptMind}[kind]()
    if args.typed and args.consent == "mind":
        raise SystemExit("--typed goes with consent private or nature: with mind, the call that reads the words would choose")
    if args.think and args.consent != "private":
        raise SystemExit("--think goes with --consent private: only the private decision thinks")
    if (args.hear_url or args.hear_model) and (kind != "script" or not (args.hear_url and args.hear_model)):
        raise SystemExit("--hear-url and --hear-model go together, for --mind script in a --typed world")
    runner = LiveRunner(args.out, mind, args.seed, args.agents, mind_kind=kind,
                        workers=args.workers or (64 if kind == "llm" else 1), talk=talk_params(args.consent, args.typed, args.think),
                        things=json.loads(json.dumps(THING_DEFAULTS)) if args.things else None)
    if args.things and "things" not in runner.sim.state.get("stakes", {}):
        raise SystemExit(f"{args.out} was started without --things; it applies to a new world")
    params = runner.sim._talk_params()
    consent = params["consent"]
    if args.consent and args.consent != consent:
        raise SystemExit(f"{args.out} was started with consent {consent!r}; --consent applies to a new world")
    if args.typed and not params["typed"]:
        raise SystemExit(f"{args.out} was started without --typed; it applies to a new world")
    if kind == "llm" and params["typed"]:  # the minds arm hears as the script arm does: its own call, on its own model
        from .llm import Hearer
        mind.hear = Hearer(mind)
        print(f"hearing: {mind.hear.model}", file=sys.stderr, flush=True)
    if args.hear_url:
        if not params["typed"]:
            raise SystemExit("--hear-url is for a --typed world: nothing is heard where typed lines move nothing")
        from .llm import Hearer, LLMMind
        mind.hear = Hearer(LLMMind(args.hear_url, args.hear_model, temperature=args.temperature, api_key=args.hear_key,
                                   answer_first=args.answer_first))
        print(f"script hearing: {mind.hear.model}", file=sys.stderr, flush=True)
    print(f"{'reopened' if runner.reopened else 'new world'} {args.out} at tick {runner.sim.state['tick']}, "
          f"engine {ENGINE_VERSION}, consent {consent}{', typed' if params['typed'] else ''}, "
          f"ws://{args.host}:{args.port}/", file=sys.stderr, flush=True)
    narrator = narrator_from_args(args, kind)
    print(f"narrator: {getattr(narrator, 'model', 'off')}", file=sys.stderr, flush=True)
    hold = None
    if args.script_voice or args.reply_delays:
        if kind != "script":
            raise SystemExit("--script-voice and --reply-delays are for --mind script")
        if args.script_voice:
            if not isinstance(narrator, narrate.LLMNarrator):
                raise SystemExit("--script-voice needs a model narrator (--narrate-url and --narrate-model)")
            mind.voice = narrator
            mind.facts = lambda req: narrate.voice_facts(runner.sim, req.agent_id,
                                                         (req.view.get("occasion") or {}).get("who"))
            print(f"script voice: {narrator.model}", file=sys.stderr, flush=True)
        if args.reply_delays:
            with open(args.reply_delays) as f:
                hold = ReplyHold(json.load(f), f"{args.seed}-{runner.sim.state['tick']}")
    if not args.no_warm_up:
        held = []
        if runner.sim._talk_held():  # text-1's two grammars, compiled before the first line is said
            from .llm import schema_for
            from .minds import MindRequest
            held = [("decide", SCHEMAS["decide"])] + [
                (k, schema_for(MindRequest(k, 0, 0, "", "", {"gives": ["food", "nothing"]}))) for k in ("answer", "hear")]
        warm_up([mind if kind == "llm" else None, getattr(narrator, "client", None),
                 getattr(getattr(mind, "hear", None), "client", None)], grammars=held)
    try:
        asyncio.run(serve(runner, args.host, args.port, args.tps, args.paused, token=args.token,
                          pull_min=None if args.pull_min < 0 else args.pull_min, narrator=narrator,
                          reply_hold=hold))
    except KeyboardInterrupt:
        pass
    finally:
        runner.close()


if __name__ == "__main__":
    main()
