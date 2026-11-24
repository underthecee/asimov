"""Test 16b session tools (plan/s16b-session.md): the operator's god connection, who the player met, and latency.

    python scripts/s16b.py op wss://POD-8765.proxy.runpod.net/ TOKEN step|run|pause|speed TPS [--until TICK]
    python scripts/s16b.py met RUN_DIR             # everyone at the possessed villager's place while it was held
    python scripts/s16b.py latency RUN_DIR         # act received->applied and tick wall times, p50/p95

`op` speaks protocol 3 in god scope with the session token, sends one control (with --until it steps or
runs until the view reaches that tick, then pauses) and exits. `met` (the rating form) re-runs the log with its recorded mind
replies, so it reads the engine's own occupancy, never the client's.
"""
import argparse
import asyncio
import base64
import json
import os
import ssl
import struct
import sys
from collections import defaultdict
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from asiimov.events import read_jsonl  # noqa: E402
from asiimov.inhabitant import present_ids  # noqa: E402
from asiimov.minds import ReplayMind  # noqa: E402
from asiimov.sim import Sim  # noqa: E402


async def _ws(url: str):
    u = urlparse(url)
    port = u.port or (443 if u.scheme == "wss" else 80)
    r, w = await asyncio.open_connection(u.hostname, port, ssl=ssl.create_default_context() if u.scheme == "wss" else None,
                                         limit=1 << 26)
    key = base64.b64encode(os.urandom(16)).decode()
    w.write(f"GET {u.path or '/'} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
    head = await r.readuntil(b"\r\n\r\n")
    if not head.startswith(b"HTTP/1.1 101"):
        raise SystemExit(f"handshake failed: {head[:80]!r}")

    def send(obj):
        data, mask = json.dumps(obj).encode(), os.urandom(4)
        n = len(data)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
        w.write(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    async def recv():
        while True:
            b1, b2 = await r.readexactly(2)
            n = b2 & 0x7F
            if n == 126:
                (n,) = struct.unpack(">H", await r.readexactly(2))
            elif n == 127:
                (n,) = struct.unpack(">Q", await r.readexactly(8))
            data = await r.readexactly(n)
            if b1 & 0x0F == 8:
                return None
            if b1 & 0x0F == 1:
                return json.loads(data)

    return send, recv, w


async def op(args):
    send, recv, w = await _ws(args.url)
    send({"type": "hello", "protocol": 3, "scope": "god", "token": args.token, "since": 0})
    m = await recv()
    if m is None or m["type"] == "error":
        raise SystemExit(f"refused: {m}")
    if m["type"] == "welcome":
        print(f"view tick {m['view_tick']}, paused {m['paused']}, tps {m['tps']}")
    ctl = {"type": "control", "op": args.op}
    if args.op == "speed":
        ctl["tps"] = args.tps
    send(ctl)
    while (m := await recv()) is not None and m["type"] != "status":
        if m["type"] == "error":
            raise SystemExit(f"refused: {m}")
    print(f"status: paused {m and m['paused']}, tps {m and m['tps']}, view tick {m and m['view_tick']}")
    if args.until is not None:
        while True:
            m = await recv()
            if m is None:
                break
            if m["type"] == "tick":
                t = m["view"]["tick"]
                if t >= args.until:
                    send({"type": "control", "op": "pause"})
                    await w.drain()
                    print(f"paused at view tick {t}")
                    break
                if args.op == "step":
                    send({"type": "control", "op": "step"})
    await w.drain()
    w.close()


def met(run_dir: str) -> dict:
    """{name: ticks together} for everyone at the possessed villager's place while it was held and awake
    (asleep, the player sees no one)."""
    events = read_jsonl(os.path.join(run_dir, "events.jsonl"))
    g = events[0]
    player = defaultdict(list)
    for e in events:
        if e.origin == "player":
            player[e.tick].append({"kind": e.kind, **e.data})
    sim = Sim(g.data["seed"], len(g.data["population"]["agents"]), ReplayMind([e for e in events if e.origin == "mind"]),
              genesis=g.data["population"], order=g.data["order"])
    last = max(e.tick for e in events)
    together, body = defaultdict(int), None
    while sim.state["tick"] <= last:
        sim.step(player.get(sim.state["tick"], []))
        held = sim.state.get("player", {}).get("agent")
        body = held if held is not None else body
        if held is not None and not sim.state["agents"][held]["asleep"]:
            for o in present_ids(sim, held):
                together[o] += 1
    names = {a["id"]: a["name"] for a in sim.state["agents"]}
    return {"body": body, "met": {names[o]: n for o, n in sorted(together.items(), key=lambda kv: -kv[1])}}


def latency(run_dir: str) -> dict:
    side = [json.loads(l) for l in open(os.path.join(run_dir, "runner.jsonl"))]
    got = {r["id"]: r["wall"] for r in side if r["kind"] == "received" and not r["id"].startswith("~")}
    acts = sorted(r["wall"] - got[r["id"]] for r in side if r["kind"] == "applied" and r["id"] in got)
    steps = sorted(r["wall_s"] for r in side if r["kind"] == "step")
    lat = sorted(r["lat_max_s"] for r in side if r["kind"] == "step" and r["calls"])
    q = lambda xs, p: round(xs[min(len(xs) - 1, int(len(xs) * p))], 2) if xs else None
    return {"acts": len(acts), "act_received_to_applied_s": {"p50": q(acts, .5), "p95": q(acts, .95)},
            "ticks": len(steps), "tick_wall_s": {"p50": q(steps, .5), "p95": q(steps, .95)},
            "slowest_mind_call_per_tick_s": {"p50": q(lat, .5), "p95": q(lat, .95)}}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("op")
    p.add_argument("url")
    p.add_argument("token")
    p.add_argument("op", choices=["step", "run", "pause", "speed"])
    p.add_argument("tps", type=float, nargs="?", default=1.0)
    p.add_argument("--until", type=int)
    for name in ("met", "latency"):
        sub.add_parser(name).add_argument("run_dir")
    args = ap.parse_args()
    if args.cmd == "op":
        asyncio.run(op(args))
    else:
        print(json.dumps((met if args.cmd == "met" else latency)(args.run_dir), indent=1))


if __name__ == "__main__":
    main()
