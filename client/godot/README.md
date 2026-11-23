# asiimov Godot client

2026-11-09. Block B10, Track A, PR 2. Godot 4.3-stable, GDScript only. The client draws and sends intents.
The Python engine is the only source of truth, over protocol 2 (`plan/client-protocol.md`).

    python -m asiimov.live --out runs/live --agents 25 --tps 2        # the live world
    godot --path client/godot -- --url ws://127.0.0.1:8765/           # live client
    godot --path client/godot -- --log runs/demo/events.jsonl         # a recorded log, with a scrubber
    xvfb-run -a godot --path client/godot --rendering-driver opengl3 -- --log LOG --at 230 --screenshot out.png
    python client/godot/tools/make_playback.py runs/demo runs/demo/playback.json.gz   # a playback pack (G7)
    godot --path client/godot -- --log runs/demo/playback.json.gz     # the same world with holdings; click a place

- `scripts/world_model.gd` rebuilds who is where from events alone. The engine moves villagers only in
  `activity`, `arrive` and (a player's walked step, walk-1) `moved` events, and positions use worldmap.position's integer math. Interpolation between
  ticks is for drawing only.
- `scripts/live_client.gd` speaks protocol 2: in god scope hello, backlog, ticks, acks, `fire` and controls; in inhabitant scope (`--inhabit N`) its villager's view, witnessed events and `act`. The action menu is the server's `offer`, one button per action (and person it may name), never computed in GDScript.
- `scripts/inhabit_ui.gd` puts an inhabitant's stream into words: what happened (each witnessed event and each receipt as a sentence), its status, today's plan, who is here and what they were last seen doing, and a label for each button. After a press every button waits for the next tick, and buttons the engine can only refuse are left off: asks and theft need the person here, and "ask family" appears only for family who are here (seek, avoid and gifts work from anywhere; a gift walks there first). It reads what the server sent and decides nothing.
- `scripts/walk_keys.gd` (walk-1) holds the keys' arithmetic and nothing else: how far a held key moves the drawn body (the engine's `walk` tiles a tick at the world's pace, capped at 12 tiles a second), which drawn villager is in reach, and the E list, which is the server's `offer` for where the body stands with Talk first. WASD or the arrows walk, E opens the list and 1 to 9 pick from it, T or Enter starts a line to the villager in reach, Esc leaves a box. The body is drawn ahead of the engine between ticks; the tile it reached is sent as a `walk` act at most once a second, on key release and before any other act, and the engine's `moved` answer is what counts.
- `tests/test_walk.gd` (walk-1): the keys' arithmetic, a walked body in the world model, and a live walk out into the open and into the market, with a step and an act in one tick. Needs a paused live world stepped to a waking hour (`python scripts/s16b.py op URL "" step --until 60`).
- `tests/test_inhabit.gd` (bar 6): inhabits a villager while a god client steps the world, presses menu buttons and checks every applied act's receipt arrives in its own stream.
- `scripts/main.gd` draws the map from genesis, villagers colored by activity, a timeline with a scrubber,
  play/pause and fire.

## Tests (headless, also in CI: `.github/workflows/godot.yml`, the pinned binary, SHA-512 checked)

    python client/godot/tests/make_fixture.py /tmp/fx --agents 100 --days 3 --seed 4
    godot --headless --path client/godot --script res://tests/test_positions.gd -- /tmp/fx
    python -m asiimov.live --out /tmp/live --paused &   # then:
    godot --headless --path client/godot --script res://tests/test_live.gd -- ws://127.0.0.1:8765/ 300

`test_positions.gd` checks every villager at every tick boundary against `Sim._pos`, read from the live sim
by `make_fixture.py`, never against the viewer. `test_live.gd` steps a paused live world and checks each
tick's rebuilt positions against the engine's `pos` in that tick's view, and that one `fire` is acked as
applied with its event's id.

`test_playback.gd` (G7's bar) checks a playback pack, as the client reads it, against the engine's replay at
every 10th view tick: positions, holdings and who is at each place.

## Playback packs and exports (G7)

A log alone doesn't carry what villagers hold. `tools/make_playback.py LOG OUT.json.gz` replays the log on the
engine, every event checked against the record, and writes a pack: the events without their mind records, plus
each villager's food and coin after every tick where they changed. The client reads the rows and works out
nothing. A log replays only on the engine that wrote it, so an older log needs `--engine DIR`, a checkout of
that engine (`git worktree add DIR COMMIT`). A live run's log, which has no end event, replays as far as it goes.

`export_presets.cfg` has four presets: Web (no threads, so any static host serves it with no special headers),
Linux, Windows and macOS (universal, ad-hoc signed: a downloaded copy needs right-click, Open). An export that
finds `res://bundled/playback.json.gz` plays it when started with no `--url` or `--log`; the folder is not in
git, CI fills it before it exports. With the 4.3 export templates installed:

    python client/godot/tools/make_playback.py runs/demo client/godot/bundled/playback.json.gz
    mkdir -p build/web && godot --headless --path client/godot --export-release Web
    node client/godot/tests/web_smoke.js build/web shot.png   # serves build/web, opens it in headless Chromium

CI (`godot.yml`, job `export`) does this from a clean checkout on every push to main and every PR, runs the
Linux build and the web build with no engine behind them, and uploads all four builds as one artifact.
