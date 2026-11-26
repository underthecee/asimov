"""Run a world: python -m asiimov.run --agents 100 --days 3 --seed 1 --out runs/demo

The log and daily snapshots go to --out as the run goes. If the run dies (or to run it longer),
continue it with: python -m asiimov.run --resume --out runs/demo [--pod ID for an LLM world]
Seed, agents, order, --fire, --mind and the planned --days come from the first run; pass --days
to extend a world. Resume refuses a mind whose model differs from the one the world was run with.
"""
import argparse
import json
import os
import time

from . import persist
from .memory import render_files
from .metrics import summarize
from .llm import LLMMind
from .minds import NoisyMind, StubMind
from .sim import ORDERS, TICKS_PER_DAY


POD_DIR = os.path.join(os.path.dirname(__file__), "..", "runs", "pods")


def llm_from_args(args) -> tuple[LLMMind, dict | None]:
    """Build an LLMMind from --pod (a record written by scripts/runpod_pod.py) or explicit flags."""
    rec = None
    if getattr(args, "pod", None):
        with open(os.path.join(POD_DIR, f"{args.pod}.json")) as f:
            rec = json.load(f)
    url = args.llm_url or (rec or {}).get("url")
    model = args.llm_model or (rec or {}).get("model")
    key = args.llm_key or (rec or {}).get("api_key")
    if not url or not model:
        raise SystemExit("LLM mind needs --pod, or --llm-url and --llm-model")
    return LLMMind(url, model, api_key=key, temperature=getattr(args, "temperature", 0.7),
                   answer_first=getattr(args, "answer_first", False),
                   plan_lane=getattr(args, "plan_lane", 0) or 0), rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=100)
    ap.add_argument("--days", type=int, default=None, help="world length (default 3; on --resume, the planned length)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--mind", choices=["stub", "noisy", "llm"], default=None, help="default stub; on --resume, the run's")
    ap.add_argument("--fire", type=str, default=None, help="player fire as FAMILY@DAY, e.g. 3@1")
    ap.add_argument("--out", default="runs/demo")
    ap.add_argument("--resume", action="store_true", help="continue the run in --out (crashed or finished)")
    ap.add_argument("--pod", help="pod id from scripts/runpod_pod.py (url, model and key)")
    ap.add_argument("--llm-url")
    ap.add_argument("--llm-model")
    ap.add_argument("--llm-key")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--order", choices=ORDERS, default="id", help="who acts first when agents contest")
    ap.add_argument("--workers", type=int, default=None,
                    help="concurrent mind calls per tick (default 64 for llm, else 1); never changes the log")
    args = ap.parse_args()
    rec = None
    if args.mind is None:
        args.mind = (persist.config(args.out).get("mind") if args.resume else None) or "stub"
    if args.mind == "llm":
        mind, rec = llm_from_args(args)
    else:
        mind = StubMind() if args.mind == "stub" else NoisyMind()
    workers = args.workers or (64 if args.mind == "llm" else 1)
    t0 = time.monotonic()
    if args.resume:
        run = persist.resume(args.out, mind, workers, days=args.days)
        print(f"resumed at tick {run.sim.state['tick']}", flush=True)
    else:
        player = {}
        if args.fire:
            fam, day = (int(x) for x in args.fire.split("@"))
            player[day * TICKS_PER_DAY + 60] = [{"kind": "fire", "family": fam}]
        run = persist.start(args.out, args.seed, args.agents, mind, args.order, player, workers,
                            days=args.days or 3, mind_kind=args.mind)
    if run.days is None:
        raise SystemExit(f"{args.out}/run.json records no planned length; pass --days")
    sim = run.sim
    end = run.days * TICKS_PER_DAY
    try:
        while sim.state["tick"] < end:
            run.advance(min(end, (sim.state["tick"] // TICKS_PER_DAY + 1) * TICKS_PER_DAY))
            extra = f", model wall {mind.stats().get('wall_s_in_model', 0)}s" if args.mind == "llm" else ""
            print(f"tick {sim.state['tick']} at {time.monotonic() - t0:.0f}s, {len(sim.events)} events{extra}",
                  flush=True)
    except BaseException:
        pod = " --pod POD_ID" if args.mind == "llm" else ""
        print(f"run aborted at tick {sim.state['tick']}; continue it with: "
              f"python -m asiimov.run --resume --out {args.out}{pod}", flush=True)
        raise
    run.finish()
    for a in sim.state["agents"]:
        d = os.path.join(args.out, "agents", f"{a['id']:03d}")
        os.makedirs(d, exist_ok=True)
        for name, text in render_files(a, sim.state).items():
            with open(os.path.join(d, name), "w") as f:
                f.write(text + "\n")
    summary = summarize(sim.events, sim.metrics["activity"], len(sim.state["agents"]))
    summary["final_hash"] = sim.state_hash()
    summary["wall_s"] = round(time.monotonic() - t0, 1)
    if args.mind == "llm":
        summary["llm"] = mind.stats()
        cost = (rec or {}).get("cost_per_hr")
        if cost:
            summary["llm"]["cost_per_hr"] = cost
            summary["llm"]["usd_this_run"] = round(float(cost) * summary["wall_s"] / 3600, 3)
    with open(os.path.join(args.out, "metrics.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
