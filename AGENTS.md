# AGENTS.md

Project conventions, the working standard and decisions live in the asiimov shared folder
(`AGENTS.md`, `decisions.md`, `plan/`). This file only covers the code in this repo.

## What this is
The world core for Prototype 0 (see `plan/prototype-scope.md` and `plan/agent-individuality.md`
in the shared folder): a deterministic, event-sourced village of agents with individual
parameters, per-agent markdown memory, and minds behind a narrow interface.

## Rules for changes
- Every state change happens inside an emitted event with an `origin` (rule, chance, mind, player)
  and its causal `parents`. No silent state edits.
- Simulation arithmetic is integer-only; randomness goes through `Rng.draw(stream, ...)` so it is
  recomputable from the log. A draw whose counter could move because of who else is around (a chat,
  an ask, a pick among those present) uses the keyed `Rng.at(stream, tick, ...)` instead, and a pick
  among candidates takes the lowest keyed score per candidate, never an index into the list (T22), so
  a twin with one more person somewhere doesn't re-roll everyone else.
- A tick collects mind requests, then commits replies in `Sim._order` (the one place that decides who
  acts first; `--order id|shuffle`). Never apply a reply where it is requested: concurrency must not
  change the log.
- A complete log starts with `genesis` (naming `ENGINE_VERSION`) and ends with `end`. Bump
  `ENGINE_VERSION` and the golden hash in `tests/test_world.py` whenever a change alters the log
  for the same inputs.
- Runs persist as they go (`asiimov/persist.py`): resuming a crashed run must write the same bytes
  an uninterrupted run writes. `tests/test_persist.py` enforces this.
- Mind outputs are recorded verbatim. `replay()` must reproduce the log and final hash without
  calling any model. `tests/test_world.py` enforces this; keep it green.
- `StubMind` and `NoisyMind` are stand-ins for the GPU-served model, not the product's minds.
  Don't tune game behavior to them.
- The rules above that can be read off the code are checked statically by `asiimov/lint.py`
  (`tests/test_lints.py`, or `python -m asiimov.lint`) on the sim core: integer arithmetic, randomness
  only through `Rng`, counter draws only on registered own-actor streams and no index picks (T22), and
  replies fetched and applied only in `Sim._commit` under `_order`. A new exception gets a commented
  entry there (or `# lint: allow <rule> -- <reason>` on the line), never a silent one; a new module the
  engine imports must be classed core or mind side. It is a syntactic check with known limits (a draw or
  float returned by a helper, taint across function calls, semantic state edits outside events); replay
  and the invariants remain the authority.

## Run
    pip install pytest hypothesis
    python -m pytest -q
    python -m asiimov.run --agents 100 --days 7 --seed 1 --fire 3@1 --out runs/demo
    python -m asiimov.run --resume --days 10 --out runs/demo   # continue after a crash, or run longer
    python -m asiimov.scorecard --mind stub --out runs/scorecard-stub   # variety tests, plan/variety-scorecard.md
    python -m asiimov.diagnostics runs/demo/events.jsonl   # plan copy rate, value-word core share (any engine)
    python -m asiimov.authorship --log runs/demo/events.jsonl --out runs/authorship   # who authors each waking tick (test 7)
    python -m asiimov.fork --base runs/demo/events.jsonl --out runs/twin --fire 2@1 --pod POD_ID   # twin: served where prompts match, live elsewhere (tests 8, 9, 15)
    python -m asiimov.propagation --agents 100 --seed 1 --fire all@1 --out runs/prop   # who acts differently 1, 3, 7 days after a fire (stub twins, $0)
    python -m asiimov.soak --agents 100 --days 24 --seed 1 --out runs/soak   # long run: invariants every tick, replay from disk
    python -m asiimov.viewer runs/demo/events.jsonl --out runs/demo/view.html   # read-only page: map, timelines, minds, causes (any engine)
    python -m asiimov.live --out runs/live --agents 25 --tps 2 --things   # live world over WebSocket for clients (plan/client-protocol.md); stub/noisy minds only
    python scripts/aiplay.py start runs/ai --url ws://127.0.0.1:8765/ --inhabit 3   # an agent plays the real client on a virtual screen (plan/ai-playtest.md)
