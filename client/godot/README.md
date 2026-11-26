# asiimov Godot client

2026-11-09. Block B10, Track A, PR 2. Godot 4.3-stable, GDScript only. The client draws and sends intents.
The Python engine is the only source of truth, over protocol 3 (`plan/client-protocol.md`).

    python -m asiimov.live --out runs/live --agents 25 --tps 2 --things   # the live world, with things in it
    godot --path client/godot -- --url ws://127.0.0.1:8765/           # live client
    godot --path client/godot -- --log runs/demo/events.jsonl         # a recorded log, with a scrubber
    xvfb-run -a godot --path client/godot --rendering-driver opengl3 -- --log LOG --at 230 --screenshot out.png
    python client/godot/tools/make_playback.py runs/demo runs/demo/playback.json.gz   # a playback pack (G7)
    godot --path client/godot -- --log runs/demo/playback.json.gz     # the same world with holdings; click a place

- `scripts/world_model.gd` rebuilds who is where from events alone. The engine moves villagers only in
  `activity`, `arrive` and (a player's walked step, walk-1) `moved` events, and positions mirror the engine's (space-1): a villager at a place stands on its slot there (`slot`, worldmap.slot's arithmetic), a walker where worldmap.position puts it, at a tick boundary (`position`) or at a time inside a tick (`pos_at`). `test_positions.gd` holds both to the engine. The lerp between boundaries in a recorded world is for drawing only.
- `scripts/live_client.gd` speaks protocol 3: in god scope hello, backlog, ticks, acks, `fire` and controls; in inhabitant scope (`--inhabit N`) its villager's view, witnessed events and `act`. The action menu is the server's `offer`, one button per action (and person it may name), never computed in GDScript.
- `scripts/inhabit_ui.gd` puts an inhabitant's stream into words: what happened (each witnessed event and each receipt as a sentence), its status, today's plan, who is here and what they were last seen doing, and a label for each button. After a press every button waits for the next tick, and buttons the engine can only refuse are left off: asks and theft need the person here, and "ask family" appears only for family who are here (seek, avoid and gifts work from anywhere; a gift walks there first). It reads what the server sent and decides nothing.
- `scripts/walk_keys.gd` (walk-1) holds the keys' arithmetic and nothing else: how far a held key moves the drawn body (the engine's `walk` tiles a tick at the world's pace, capped at 12 tiles a second), which drawn villager is in reach, and the E list, which is the server's `offer` for where the body stands with Talk first. WASD or the arrows walk, E opens the list and 1 to 9 pick from it, T or Enter starts a line to the villager in reach, Esc leaves a box. The body is drawn ahead of the engine between ticks; the tile it reached is sent as a `walk` act at most once a second, on key release and before any other act, and the engine's `moved` answer is what counts.
- time-1b: every act but a line said is answered the moment it arrives, with its receipt and the body's new view in an `early` message, so `main.gd` frees the buttons as soon as that act's ack finds its receipt here, not at the next tick. An act costs the body one sim-minute; the runner lands one sent sooner when the body is free, and `inhabit_ui.gd` words the rare `busy` refusal. A line said still waits for its tick.
- reach-1: a walker in the view's `seen` is drawn along its `track` while the tick's clock is inside its `in`, so someone walking by shows only while in sight. E and T pick whoever is drawn nearest among those here, those within a tile (`reach`) and those the server offers to `hail`: T on a passer too far for a line sends `hail` (its name tag reads "T: call out"), they stop and wait, and once you stand beside them T talks and E lists what the offer names. `world_model.gd` reads `halt` and draws every walker one tick behind its route, as the engine's `Sim.pos`. Words from someone in sight but out of reach read "says something, too far off to hear".
- things-1: in a world started with `--things`, each thing in the view is drawn on its tile (`art.gd`, from shapes for now), one in reach with a ring. `inhabit_ui.gd` turns the offer's `take`, `use`, `drop` and `hand` into items with the kind's own labels (`Words.thing_items`), which the E list shows after whoever is in reach and the menu shows as a "Things" row; a thing is acted on at once, never queued in a plan. The status panel says what is in your hands. The words come from the engine's kinds table (`asiimov/things.py`) through `verb_words.gd`. `tests/test_things.gd` walks to the woods, picks a flower up from the E list, puts it down, and picks berries.
- verbs-1: the words for each verb and each reason (`Words.ACTION`, `SHORT`, `DOING`, `ASKING`, `ASKED`, `VERB`, `NOW`, `WHY`, `TO_YOU`, `BY_THEM`, `HERE_ONLY`, `RATED`) live in `scripts/verb_words.gd`, which `python -m asiimov.verbs` writes from the engine's verb table (`asiimov/verbs.py`). Don't edit that file: change the table and run the command; `tests/test_verbs.py` fails when the two differ.
- time-1a (protocol 3): a walk is answered the moment it arrives, between ticks too, with an `early` message carrying its `moved` event and the body's new view, which `live_client.gd` takes as the view. The engine bounds a walk by walking speed over sim-time, so the body on screen and the engine's tile stay together while a key is held.
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
