# Client protocol

2026-11-09. Status: protocol 3 (B10; protocol 1 was PR 1, protocol 2 adds the inhabitant scope of
plan/player-body-spec.md, protocol 3 is time-1: a walk (time-1a) and every act but `say` (time-1b) is applied and answered the moment
it arrives, and its `early` message carries the body's new view). Implemented by `asiimov/live.py` and `asiimov/inhabitant.py`.
Specs: plan/2d-env-eval.md and plan/client-form-factor.md in the shared folder.

The engine is the only source of truth. A client renders what the engine sends and sends intents.
It never sends state, and nothing it computes (interpolation, its own clock) is stored or sent back.

## Transport
WebSocket (RFC 6455), text frames, one JSON object per frame, UTF-8. Default `ws://127.0.0.1:8765/`.
A frame over 1 MiB from a client closes the connection. Server frames can be larger (a backlog chunk
with long mind outputs), so a client raises its inbound buffer (Godot: `inbound_buffer_size`).

Every server message carries `"protocol": 3` and `"engine": "<ENGINE_VERSION>"`. A client that sees a
protocol it doesn't know disconnects. A new engine version needs no protocol change unless a message
changes shape; new event kinds can appear in any engine, and a client ignores kinds it doesn't draw.

## Ticks and views
The world steps in ticks of 10 sim-minutes (144 a day). "View tick N" is the state between ticks:
tick N-1 has run, tick N hasn't. An intent received while the view is at N is applied when tick N
runs. Wall-clock time paces the runner but never enters the engine, the log or the state hash.

## Scopes
The first message names a scope. The scope decides what the server sends; it is fixed per connection.

| scope | sends |
|---|---|
| `god` | everything: genesis (population with souls, map), every event with its mind output, every villager's view. Dev and review. Accepts `control`. |
| `inhabitant` | only what its villager perceives (below). Never another villager's soul, traits, params or mind output, nor its own villager's numbers. |

### The inhabitant scope
`hello` names the body: `{"type": "hello", "protocol": 3, "scope": "inhabitant", "agent": 4, "since": 0}`.
The runner possesses it for the inhabitant (a `possess` player event at the next tick). One body is held
at a time in a world: a hello while any body is held gets the error `body_taken`, and an `agent` that
isn't a villager's id gets `bad_agent`. When the inhabitant disconnects, the runner records `release`
and the villager's mind deliberates again. While an inhabitant is connected, god scope keeps `fire` and
gets `inhabited` for `possess`, `release` and `act`.

If the world reopens (after a crash or a stop) with an inhabitant's body still held, a hello for that
same villager claims it with no new `possess`. If nobody does within 36 ticks, the runner records
`release`. The 36 ticks count from the reopened view tick, or from the tick after the scheduled
`possess` when a crash re-run left the view before it. A body possessed from god scope is left alone, as it is when a god client
disconnects.

An inhabitant sends only `act` (anything else gets `scope`). After an hour (6 ticks) with no `act`, its
villager's own mind deliberates again; the next `act` takes the body back (plan/player-body-spec.md).
It gets:
- `welcome`: `world` is `{map, ticks_per_day, me: {id, name, role, family, home}, family: [{id, name, role}], people: [{id, name, role}]}`,
  plus `view_tick`, `paused`, `tps`, `verbs` (`act` only), its first `view`, and `talk`: `{consent, typed}`
  (text-1). `typed: 1` means a line said with no `move` may be given 1 food, so the say box needs no chip hint;
  `consent` is `nature`, `mind` or `private` (who answers a line said with a chip). The villager's decision is
  never sent: neither its `mind_decide` (a mind event) nor a `decided` roll.
- `events` backlog and `tick` messages carrying only events it would witness: its own, those naming it
  (`data.with`, `to`, `target` or `household`), public ones (`fire`), and, on the tick they happen,
  those by people at its place. Of another villager's events it gets only what that villager does in
  plain sight, by kind and field (`inhabitant.SEEN`: activity, arrive, wake, sleep, talk, meet, snub,
  eat, sell), plus a receipt whose `to` names it (with `answered` when the player answered it), except one that failed as
  "target not here" or "target asleep". An ask, gift or visit aimed at it adds `data.says`, the
  actor's own reason in its words (plan/player-body-spec.md, "reasons as words"). Never another's intent_*, plan_receipt or day_rhythm.
  A backlog has no co-presence history, so it carries only its own, naming and public events. No mind
  event is ever sent (its own plan is in the view), and every event is sent without its `mind` record or
  chance-stream fields.
- `tick.view`, built field by field from `inhabitant.VIEW_KEYS`: `tick, place, route, pos, activity,
  asleep, hunger, food, coin, plan` (activity, start_h, end_h), `recent, present` (id, name, role, kin,
  has_food, spare: food beyond the one they keep, pos), `seen` (space-1, below), `kin` (id, name, home), `market_food` (at the market only), `occasion` (why, who,
  text), `offer` and `cooldown_until`. `offer` maps each action a deliberation would be offered from
  here to the people it may name (empty for non-person actions). The engine checks again when the act
  applies, so an act can still get `"not offered"`. A field added later needs a spec line.
  With things-1, in a world that has things: `things` lists every fixture (a map feature, always drawn) and
  what else lies in sight, each `{id, kind, pos}` with `reach: true` when it is on the body's tile or the
  next one and `yours: true` for a fixture that is the body's to use. A thing inside a place shows only to a
  body in that place or beside it; one in the open, within 6 tiles. `carried` lists what is in the body's
  hands, `{id, kind}`. `offer` gains `take`, `use` and `drop` (the ids of the things each may name) and
  `hand` (who is in reach and awake, while something is carried). A world without things has none of these.
  With deal-1: `offer.offer` lists who an offer can be put to (anyone awake and in reach), and, in a world
  that has things, each `present` entry and each `seen` entry in reach carries `carries`, what is in that
  villager's hands, `[{id, kind}]` (what an offer may ask them for).
  With walk-1, `pos` is the tile the engine holds for a walked body, and `place` and `route` are both null
  while it stands in the open (`present` is then empty and `offer` holds only what needs nobody).
  With space-1 (plan/v0-contracts-spec.md, B1) every `pos` is the tile the body stands on: for someone at a
  place its slot there, a tile of that place keyed by its id, which the engine derives and never stores; for
  a walker the tile its route puts it on. `seen` lists those in sight who aren't in `present`: with the body
  or them out in the open, everyone within 6 tiles (diagonals count as one). Each is `{id, name, role, asleep,
  place, pos, route}`, with `route` as the view's own (`from, to, depart, arrive`) so a client can draw a
  walker between ticks with worldmap.position. Events
  by those in `seen` are witnessed on their tick as events by those in `present` are. Two places hide each
  other, however close their tiles.
  With reach-1 (plan/player-body-spec.md): a walker's position at time t is its route at t - 1. One of `seen`
  within a tile of the body carries `reach: true` and `asleep`: it can be spoken to and named in an act, and
  `offer` names it. The others carry neither, and nothing but `hail` is offered toward them. Every walker in
  sight at some time in the coming tick is listed, with `in: [f0, f1]` (the span of the tick it is in sight
  for, in 600ths) and `track: [[f, x, y], ...]` (the tiles it is seen on then); a client draws it along
  `track` while its clock is inside `in`, and not otherwise. One that comes into sight later in the tick has
  `route: null` and `pos` where it is first seen. `offer.hail` lists the walkers the body can call to.
  Events by someone in sight who is not at the body's place or in reach arrive cut down to what sight shows
  (`inhabitant.SIGHT`): `activity` (place, arrive, from), `arrive`, `halt`, and `said`, `reply` and `talk`
  with whom, never the words.

"Both" (an inhabitant and a god connection held by one person, gated on run.json `"divine": true`) is
not enforced yet. God scope is the dev and review tool until a game design says what divine power
costs.

## Client to server

`hello` must be the first message:
```json
{"type": "hello", "protocol": 3, "scope": "god", "since": 0}
```
`since` is the first event id the client still needs (default 0). A reattaching client passes the id
after the last event it has; the server sends the gap as backlog and then live ticks, with no event
sent twice and none skipped.

`intent`, a player verb:
```json
{"type": "intent", "id": "c1-0007", "verb": "fire", "args": {"family": 3}, "seen_tick": 210}
```
- `id`: the client's string, 1 to 64 characters, unique per world, not starting with `~` (the runner's own
  intents use that). Resending an id gets the recorded
  outcome again and never applies twice.
- `seen_tick`: the view tick the client was looking at. It's checked, not used to schedule.
- Verbs, each a player action in sim.py `_player` (god scope has all four; inhabitant scope, `act`):
  - `fire` with `{"family": int}`.
  - `possess` with `{"agent": int}`. Refused with `body_taken` while a body is held (release first).
  - `release` with `{}`. Refused with `no_body` if none is held.
  - `act` with `{"action": one of PLAYER_ACTIONS (DELIBERATE_ACTIONS plus `agree` and `decline`, which answer an ask aimed at the body; target: the asker; and `go_to`), "target": int?, "place": str?}`, on the possessed body. `place` names a place on the map (`verbs.act.place`) and goes with `go_to` alone, which takes no target (go-to-1: the body walks there and stays for an activity's time; its receipt says `heading there` or `already here` with the `arrive` tick, and a second receipt says `arrived`). Refused
    with `no_body` if none is held. Whether the act works is the engine's call, and the outcome is a
    `receipt` event citing the `act` event (plan/player-body-spec.md). The ack names the `act` event, not
    the receipt. **Every act but `say` lands the moment it arrives (time-1b):** its receipt comes at once in
    an `early` message, then its `ack`, between ticks too. It holds the body for its cost, one sim-minute
    (none for `agree`, `decline` and `walk`), and the runner stamps an act that arrives sooner than that at
    the moment the body is free, so acts sent back to back each land. One the engine still finds the body
    held for (it arrived in a tick's last sim-minute behind another) gets a failed receipt, `"busy"`.
    **`say` keeps the tick:** it is recorded as it arrives and resolved at its tick's fixed point, one line
    a tick (a second gets `"one act per tick"`), and is acked then. A `say` followed in the same tick by
    `release`, or by a `possess` of another body, gets a `"released"` receipt and never resolves; any other
    act before the release has already landed.
  - `act` with `{"action": "hail", "target": id}` (reach-1, the player's alone): calls to a villager walking by
    within sight (6 tiles) at the act's time. It costs the body nothing and carries no words. The walker stops
    where it is for 3 ticks (a `halt` event by the walker, `{pos, place, depart, arrive}`, citing the act),
    and the receipt says `hailed`; `already stopped` (ok) for one that already waits, which gains no time;
    refused `target not in sight`, or `not walking` for one at a place. A line said to it, or an act on it
    from within reach, starts its wait again. `target` is required.
  - `act` with `{"action": "take" | "drop" | "use", "thing": id}` or `{"action": "hand", "thing": id, "target":
    id}` (things-1, the player's alone, in a world started with `--things`): the body picks a thing up from
    its tile or the next one, puts down one it carries on the tile it stands on, uses one beside it, or
    hands one it carries to someone in reach and awake. Each lands at once and costs a sim-minute. A success
    is a rule event by the body (`took {thing, kind, from}`, `dropped {thing, kind, at}`, `used {thing, kind,
    food}`, `handed {thing, kind, to}`) and a receipt citing it (`took`, `dropped`, `picked`, `handed`). Using
    a fixture (the stall, your bench, your bed) is the verb done there, so its receipt is that verb's
    (`buy_food`, `work`, `rest`). Refusals: `nothing there`, `not in reach`, `can't be carried`, `hands full`,
    `already carried`, `not carried`, `no use`, `not yours`, `target not here`, `target asleep`, `target hands
    full`. `thing` goes only on these four, and `target` only on `hand`; anything else is `bad_args`.
  - `act` with `{"action": "offer", "target": id, "give": side, "want": side}` (deal-1, the player's alone): an
    offer put to a villager awake and in reach. A side is `{"food": n, "coin": n, "things": [ids]}`, each part
    optional, a count from 1 to 999, at most 8 distinct things; `{}` is the empty side, and not both may be
    empty. It lands at once and costs a sim-minute. The engine's limits refuse it with a receipt and nothing
    else (no roll, no mind call): `target not here`, `target asleep`, `not yours to offer`, `not theirs to
    give`, `nothing to spare` (food asked for that would leave them under 1), `bad terms` (they hold a grudge),
    `hands full`, `target hands full`, `too many offers` (a fourth different offer to them in a day) and
    `already traded` (these exact terms, on which they lose, were dealt on today). A gift (`want` empty) moves at once: a `deal`
    `{move: "offer", from, to, gives, gets: {}, ok: true}` by the giver and the receipt `gave`. Any other offer
    writes `offered {with, gives, gets, place}` by the player and the receipt `offered`. The villager's nature
    answers those exact terms once for the day (a `decided` chance event with its roll and limit), and on the
    next tick the villager says that answer in its own words: a `reply` with `answer` `yes` or `no`. After a
    yes the engine checks the limits again and writes one `deal` `{move: "offer", from, to, gives, gets, ok}`
    by the one who offered (`from`), citing the offer, the answer and the decision: both sides move at once,
    or `ok: false` with `why` (a limit above) when the yes can no longer be had. A no writes no deal. If the
    player has walked out of reach before the villager would answer, there is no reply: the `deal` has
    `ok: false` and `why: "gone"`, whatever the answer would have been. `give`
    and `want` go only on `offer`; on anything else, or malformed, they are `bad_args`. `verbs.act.give` and
    `verbs.act.want` list the parts a side may hold. A new verb doesn't change the protocol number (B6.1).
  - `act` with `{"action": "walk", "to": [x, y]}` (walk-1, the player's alone): the body steps toward that tile,
    x first, as far as walking speed allows since its last walk: it earns `map.walk` tiles a tick as sim-time
    passes and keeps at most one tick's walking while it stands (time-1a). A walk that asks for more is cut
    short, not refused. `to` goes with `walk` alone, takes no
    target or place, and must be a tile inside `verbs.act.to` (`[x0, y0, x1, y1]`, inclusive: the places' bounding
    box plus a margin); anything else is refused before it is logged. A walk is not the tick's one act: a walk
    and an act may share a tick, and the act is judged where the body then stands. The engine answers with a
    `moved` event citing the act, `{from, pos, place}`: `place` is the place whose rectangle holds the tile
    (the nearest anchor, then the lowest name, where two overlap), or null out in the open, where the body is
    with nobody. A sleeper's walk, or one at bedtime, gets a failed `receipt` instead ("asleep", "bedtime").
    The body stands where it stopped for a step's time (`DEFAULT_STEP_TICKS`), then its day takes over, as after
    any act. Walking out of a place answers an ask held there as declined.
    **A walk is applied the moment it arrives (time-1a)**, between ticks or at a running tick's fixed point, and
    never pulls a tick forward: its `early` message and `ack` come at once (see `early`).
  - **The time inside the tick (time-1a).** The runner stamps every `act` with `f`, how far into its tick it
    arrived in 600ths of a tick (sim-seconds): the real time since the last tick started over the pace, below
    1; 599 for an act that joins a running tick; 0 in a paused world. An act's sim-time is `tick + f/600`. A
    client never sends `f` (it is `bad_args`); the `act` event records it when it isn't 0, and batch and
    replay use the recorded value. Acts in a tick apply in order of `f`, then of arrival.

`control` (god scope only): `{"type": "control", "op": "pause" | "run" | "step" | "speed", "tps": 2.0}`.
`step` runs one tick while paused; `speed` takes `tps` between 0.01 (100 s a tick, for a person playing) and 1000.

## Server to client

`welcome`, once, answering `hello`:
```json
{"type": "welcome", "protocol": 3, "engine": "16", "scope": "god",
 "world": {"seed": 1, "order": "id", "map": {...}, "population": {...}, "ticks_per_day": 144},
 "view_tick": 211, "n_events": 6012, "paused": false, "tps": 1.0, "verbs": {"fire": {"family": [0, 24]}}}
```
`verbs` gives each verb's argument ranges (here, families 0 to 24 inclusive).

`events`, the backlog, in order, in chunks: `{"type": "events", "events": [...]}`. Each event is
exactly a line of `events.jsonl` (`id, tick, kind, origin, actor, data, parents, mind`). Backlog ends
before the first `tick` message.

`tick`, after each tick runs:
```json
{"type": "tick", "tick": 211, "events": [...],
 "view": {"tick": 212, "agents": [{"id": 0, "place": "home_0", "activity": "home", "asleep": false,
          "pos": [20, -20], "route": null}, ...]}}
```
`tick` is the tick that ran, `events` are the events it emitted, `view` is the state at view tick 212.
For a walker, `place` is null and `route` is `{"from": [x, y], "to": "market", "depart": 205,
"arrive": 214}`. `pos` is the engine's own position (worldmap.position) at the view tick, in tiles.
A client interpolates between views for display only.

`ack`, an intent's outcome, sent to the client that sent it:
- `{"type": "ack", "id": "c1-0007", "status": "applied", "tick": 211, "event": 6013}`, sent only after
  the intent's player event is fsynced in `events.jsonl`. **An inhabitant's `act` is the exception (E4):**
  it is acked at its tick's fixed point, as soon as it applies and before the tick's mind calls return,
  right after an `early` message with its own events (see Durability for why that is safe).
- `{"type": "ack", "id": "c1-0007", "status": "rejected", "reason": "bad_args"}`, sent at the next tick
  boundary. A rejected intent changes nothing in the world. Reasons: `bad_message`, `bad_id`,
  `unknown_verb`, `bad_args`, `bad_seen_tick` (missing, or ahead of the view), `stale` (more than a
  day behind the view), `scope`, `inhabited`, `body_taken`, `no_body`.

`early`, to an inhabitant only (E4, Steering 2026-11-14): the events its own `act` made as it applied
mid-tick, its receipt among them, then its `ack`. That tick's `tick` message leaves these events out.
`{"type": "early", "tick": 211, "events": [...]}`. An `act` that arrives while a tick runs joins that tick
(its `act` event carries `"late": true`), unless the body's own mind is being asked (the idle hand-back),
when it waits for the next tick and is acked as usual.
Protocol 3 (time-1a): a `walk` gets its `early` and `ack` the moment it arrives, between ticks too, with
`"view"`, the inhabitant's whole view from where the body now stands (who is present and in sight there), in
the shape a `tick` message's view has: `{"type": "early", "tick": 212, "events": [act, moved], "view": {...}}`.
`tick` is the tick the events belong to, which between ticks is the one that hasn't run yet.
From time-1b every act but `say` is answered that way, with its receipt among the events and the view after
it: `{"type": "early", "tick": 212, "events": [act, receipt], "view": {...}}`. A `say` arriving between ticks
is recorded at once, resolved in its tick and acked with it, as before.

`heard`, to an inhabitant only, when the runner narrates (readable-world E2; `--narrate`): the spoken words of
something it just witnessed, sent after the tick, so it can arrive after the next `tick` message:
```json
{"type": "heard", "tick": 211, "event": 6011, "kind": "talk", "to_player": false,
 "lines": [{"who": 4, "name": "Ari Vosa", "text": "Morning. Field's dry again."}, {"who": 7, "name": "Bel Rota", "text": "Rain soon, I hope."}]}
```
`kind` is `talk` or `snub` at its place while it's awake there, `refusal` (a villager saying no to its ask), or
`visit` (a villager come to see it). `event` is the event the words are about. Its own villager never
speaks. The words are never part of the world: no event, no replay, no memory. They are kept in the run's
`narration.jsonl` with the narrator and the facts it was given.

`status`, broadcast after a control: `{"type": "status", "paused": true, "tps": 1.0, "view_tick": 212}`.

`error`, then the server closes: a first message that isn't a valid `hello`, an unknown protocol, an
unknown scope (`bad_scope`), an inhabitant hello with a bad `agent` (`bad_agent`) or for a body it can't
take (`body_taken`), a `since` past the log (`bad_since`), a missing or wrong `token` when the server was
started with `--token` (`bad_token`), or a malformed frame. The
close frame repeats the reason with code 4000, since a client can drop a message that arrives just
before a close (Godot does).

## Durability
A run directory is a `persist.py` run plus one sidecar file:
- `run.json` holds the world's inputs. Its `player` list is the schedule of player actions. The live
  runner appends an accepted intent there (atomic write) before the tick it applies at, so
  `persist.resume`, `python -m asiimov.run --resume` and `replay()` all see it like a scripted `--fire`.
- `runner.jsonl` is the sidecar: wall-clock times, clients, controls, each intent's id, scope and
  outcome, and each inhabitant's `inhabit` and `leave` (the newest one decides the orphan release on
  reopen; `inhabit` is fsynced before its `possess` is scheduled). It's runner metadata and never enters `events.jsonl` or the state hash.

An intent's path is: `received` fsynced in the sidecar, then scheduled in `run.json`, then the tick
runs, then `events.jsonl` is fsynced, then `applied` in the sidecar and the ack. A kill at any point
leaves the intent applied (it's in `run.json`), rescheduled from its `received` line on reopen, or
not acknowledged at all (it never reached the sidecar, and the client resends it by id). An intent
the sidecar received is never dropped.

A clean stop (Ctrl-C) snapshots the current tick, so reopening continues at the same view tick with
every event and ack, and a client reattaches with its own `since`.

After a crash, persist resumes from the newest snapshot and re-runs the recorded tail, dropping the last
recorded tick with events (and any empty ticks after it), so the view can move back more than one tick.
A client whose `since` is past the reopened log gets `bad_since` and reloads with `since: 0`. A
player action applies at its tick's fixed point (E4): after the agent loop has built the minds' requests,
before any mind reply applies, those scheduled before the tick first, then late ones in `received` order.
Nothing it reads is a mind reply of that tick, so its event id and outcome survive the re-run and its
ack stands, the early one included (tests/test_late_act.py kills a tick after an early ack). A walk taken between
ticks (time-1a), and from time-1b any act but `say`, depends only on the state at the tick's start and the actions
before it, which `run.json` holds before its ack, so it re-runs to the same events too (tests/test_time.py). With a live LLM mind, the rest of a re-run tick can differ from what was
streamed before the crash.
