# The player's body (B10 PR 3, Track B spec)

2026-11-09. Status: proposal, signed off by Steering 2026-11-09 with five changes, which are folded in. This is
the spec, written before any engine code. The build is an engine
bump under plan/engine-numbering-rule.md (shared folder): branch label `pb-1`, and Steering assigns the
integer at merge. It needs review and Steering's sign-off before the build starts.

joon, 2026-11-09 02:53Z: "player could be both, most likely an inhabitant." So the main mode is
inhabiting one villager, and divine verbs are a second, separate family. Open for joon: what the body
does when the player gives no orders (the decision card in thread "Build the Godot client for the live
world"). This spec is written for the recommended option, autopilot by the villager's own mind, and
notes what changes under the others.

## Principle
The player takes over the villager's **in-the-moment decisions**, and nothing else. The engine already
has exactly one place where those are made: a deliberation, whose output `_resolve(a, out, eid)` turns
into an action now and maybe an override step (sim.py `_resolve`). A player order is a deliberation
whose output comes from a person instead of a model. It's recorded as an `origin="player"` event, and
`_resolve` runs it with every check a mind's choice gets: `_target_error`, the live place checks in
`_person_act` (L2), receipts and chance rolls. One thing a mind has that `_resolve` doesn't check is the
offer: a mind is held to `_offer` only by its reply schema, and `_resolve` itself never reads the offer
(review of #38: with 0 coin, `_resolve(go_market)` still starts a shop step). So the player path adds
an explicit offer check (change 2). With it, the player can't do anything a villager couldn't choose,
and replay holds by S2: player events are recorded inputs, like `fire`.

## Engine changes (in `Sim._player`, plus the deliberation point)
1. `possess {agent}` (player event): sets `state["player"] = {"agent": id, "event": eid}`. The key is
   added only on the first possess, so a world with no player has the same state, hash and log as
   today, apart from the genesis engine field. Re-possessing moves the body. `release` removes the key
   and clears the body's `occasion`, so its mind isn't asked about something the player already
   handled. D2 (#34, label d2-1) adds a mind ask, `_work_end`. Whichever of d2-1 and pb-1 merges second
   makes `_work_end` skip the possessed villager, with a test.
2. `act {action, target?}` (player event, on the possessed villager only). **When it resolves:** in the
   same phase as a mind's deliberation, not at the start of the tick (Steering: otherwise the player wins
   every contest). The `act` player event is **recorded at the start of the tick**, as `fire` is, which
   keeps the client's `seen_tick` model, but it is only queued there. In the agent loop, where
   `_maybe_deliberate` would run for this villager (after arrivals, occupancy, `_body`, `_act` and
   `_eat`), the engine:
   - computes `view = _view(a, occupancy)`, `_situation` (which fills `view["people"]`) and
     `offer = _offer(a, view)`, exactly as a deliberation request would;
   - registers it in `pending` with the player's output as its reply and the act event as its parent.
     A player entry is never dispatched to the mind and emits no `mind_*` event, so bar 4 holds and
     ReplayMind's order of recorded mind events is untouched. `_commit` applies it in its `_order` slot
     straight from the recorded act.

   `_commit` then applies it in `_order` among the minds' replies. So its receipts, rolls and ordering
   against other villagers are those a mind's identical deliberation would get on that tick. At commit
   it runs these checks in order, each a failed receipt with a named reason and no other effect:
   - `action not in offer` gives `"not offered"`, with the offer recorded in the receipt;
   - a target outside `view["people"]` for a person action gives `"unknown target"`;
   - then `_resolve(a, {"action": action, "target": target}, eid)`.

   **Rate:** an act in `INSTANT_STEPS` (ask_kin, ask_food, buy_from, steal), or `buy_food`, sets
   `cooldown_until = tick + 12`, as a deliberation does (sim.py `_maybe_deliberate`). A rolled act
   inside that window gets `"cooldown"` and nothing else. Acts that only change the body's own activity
   or start a reach step (work, socialize, home, rest, explore, go_market, seek, avoid, give_*, wait)
   aren't limited, since they roll nothing.

   **One act per body per tick** (`pending` holds one entry per agent). The first act in schedule order
   is taken, and later ones that tick get `"one act per tick"`. (time-1b: this now holds for a line said
   only. Every other act lands the moment it is recorded and holds the body for its cost; one inside that
   is refused `"busy"`. See "Small acts at their own time" below.) If the body is asleep, on its bedtime
   walk, or falling asleep at home that tick (the sleep branch, which files its reflection), the act
   gets a failed receipt: `"asleep"`, `"bedtime"` or `"falling asleep"`. An act queued before a
   `release` (or a `possess` that moves the body) in the same tick gets `"released"` at the release
   and never resolves, so the freed mind is never asked about it (review of #39; from time-1b only a line
   said is ever queued, and an act before a release in its tick has already landed). No queued act ends a tick
   without a receipt (S8). Queued acts never carry over to the next
   tick: the client resends after reading the receipt. Because every player event (fire, possess, act,
   release) is still emitted at the start of the tick in schedule order, live.py's slot-to-event
   mapping (`step`, `_reconcile`) and the protocol's "a player action applies at the start of its
   tick" both hold. The protocol line becomes "is recorded at the start of its tick". An act's
   receipt follows later in the same tick, and the ack names the act event. The `goal`/`then` intent fields aren't exposed,
   because the player is the intent. Replay is unchanged: `replay()` passes the player events of each
   tick back to `step`, and `_player` queues the acts again.
3. Guard: `_maybe_deliberate` skips the possessed villager while it has had an order (its `possess`
   or an `act`, refused or not) in the last `IDLE_TICKS` = 6 ticks, an hour. After an hour with no order
   its own mind deliberates again, on its occasions as any villager's does, until the next order takes
   the body back (joon chose this on 2026-11-09, engine label idle-1). `state["player"]["order"]` holds
   the last order's tick. Like `release`, the hand-back never asks the mind about the player's own act:
   an occasion filed by a receipt whose parent is a player `act` (a refusal or failure, on the act's tick
   or later, when a step the act started fails) is dropped when the guard lifts (review of #42). Its occasions (hungry, robbed, refused, fire and the rest of `OCCASIONS`) still
   fire, and they're shown to the player as prompts to act (the inhabitant view's `occasion`).
4. As in the recommended option, wake plans and night reflection still go to the villager's mind, so
   it keeps one coherent memory and a day plan. The habit/plan layer runs between orders, and an order
   overrides the current step as a deliberation's override does today. (Option 1, standing still, also
   skips plan and reflect and leaves `plan` empty. Option 2 adds an idle counter that lifts the step-3
   guard after 6 ticks without an order.)

No prompt text changes. Other villagers see the possessed villager as an ordinary person. So L11, L18
and L19 have nothing to re-measure, and no test reads a decision this changes, except the possessed
villager's own. The scorecard has no way to exclude a villager yet, so the build adds one: any villager
with a `possess` event in the log is dropped from every scorecard and diagnostic reading, with the count
dropped reported. B5/B6's test-11 arm
doesn't apply: this adds no mind choices.

D2's rule, that a reply lands on the tick it's asked, holds. Everything another villager asks of the
player's body (ask_kin, ask_food, buy_from, steal against it) was resolved by rule rolls, so it never
waited on the player. From player-ask-1 the three asks wait up to 6 ticks for the player's answer (below);
the asker's tick never blocks, its step just waits, and steal still rolls. Free speech, when B6 adds it, is the one place a mind would wait for the
player: the player's reply lands on the next tick as a player event, never blocking a tick. That's an
explicit exception for the player's body only, and it needs Steering's sign-off when B6 specs it.

## Protocol 2: the inhabitant scope
`hello {"protocol": 2, "scope": "inhabitant", "agent": id}`. The runner sends `possess` as an intent
on hello. Two inhabitants can't hold one body: the second gets `body_taken`. When an inhabitant
disconnects, the runner sends `release`, and the mind's normal deliberations resume. If the runner
crashed, `state["player"]` is still held when the world reopens. If no inhabitant has said hello for
that body within 36 ticks (6 sim-hours) of reopening, the runner records a `release`.

What an inhabitant gets, and nothing more (the server filters; the client is never trusted to hide):
- `welcome`: the map, its own villager (name, role, family, home), its family's ids and names, and every
  villager's id, name and role (`people`: a village knows its names; client pass before 16b's rated
  session, 2026-11-10). Not the
  population's souls, traits or params, not even its own villager's numbers. The soul shows in behavior.
- Per tick, a `view` written field by field, as a **whitelist**, never a copy of `Sim._view` (which
  carries `traits`): `tick`, `place`, `route` (from, to, depart, arrive), `pos`, `activity`, `asleep`,
  `hunger`, `food`, `coin`, `plan` (blocks: activity, start_h, end_h), `recent` (its notes),
  `present` (each: id, name, role, kin, has_food, spare, pos), `kin` (each: id, name, home), `market_food`
  (only at the market), `occasion` (why and who, as the deliberate situation words it), `offer`
  (the actions `_offer` allows now, with the people each may name), `cooldown_until`, `asks` (player-ask-1, below), `food_price` (the
  price in force, which every villager's prompt already states) and `intent` (its own villager's current
  intent: goal, steps as do and target, and the step it is on; null when it has none). Any field
  added later needs a spec line. Bar 5 checks the keys against this list. Positions of other villagers appear only for those at its place (and its
  family's homes, which are known).
- Events: only those it would witness. That means events with it as actor or target, events whose
  `data.to` or receipt names it (but not another's receipt that failed as "target not here" or "target
  asleep", nor "no coin", which would show another's purse: that act never reached it, so it saw nothing;
  kept when it settles a held ask it saw), events at its place this tick by people present, and public events
  (`fire`, which the village sees). No mind output, prompt hash or raw text of any other villager, and
  for its own villager only the plan blocks, never the raw reply.
  Of another villager's events, only what it does in plain sight (kinds and fields in
  `inhabitant.SEEN`). A talk's `gossip` between two people at its place is passed: overheard talk is
  heard. Its own `intent_*` events carry their `goal` and `steps`: the player sees what its own body meant
  to do (Steering, 2026-11-10, after test 16b attempt 1). No other villager's mind words are sent, but
  for one: reasons as words (joon's pick on Steering's card, 2026-11-10). Another villager's act aimed at
  it (an `ask`, a `meet`, or a receipt of `ask_kin`, `ask_food`, `buy_from`, `give_food` or `give_coin` whose
  `to` names it) carries `says`: the `reason` (else `goal`) field, alone, of that villager's own mind reply
  that chose the act or its intent (`inhabitant.says`, at most 200 characters; walked only through that
  villager's own events). Not a theft, an avoid or a snub, which aren't said to anyone's face; not the
  answer receipt of a held ask, whose words were on the ask; nothing for a rolled refusal of its own ask,
  which no mind decided. Each `asks` entry in the view carries the same `says`. View only: no model call,
  and the engine and log are unchanged.
- Intents: `act` with the action and target, and `seen_tick`. Divine verbs are refused with `scope`.

## Asks aimed at the player's body (player-ask-1, Steering 2026-11-10, after test 16b attempt 1)
- An `ask_kin`, `ask_food` or `buy_from` aimed at the possessed body while it isn't idle, that passes the
  usual checks (together, awake, food to spare, coin), isn't rolled. It's logged as an `ask` event (actor:
  the asker; data: action, to; parent: the asker's act or step event), which the body witnesses, and waits
  in `state.player.asks` with `until = tick + ASK_WAIT_TICKS` (6).
- The player answers with an `act` of `agree` or `decline`, target the asker (`sim.ANSWER_ACTS`, held to no
  offer; an answer with no ask from that villager gets "no ask"). Its own receipt says "said yes" or "said no".
- Unanswered by `until`, or when the player acts otherwise first, or lets go, the body's own nature answers
  with the same roll as for anyone (origin chance). An away player never stalls the asker.
- The asker's receipt cites the ask and the answer (the player's receipt). Its conditions are checked again
  at the answer. The asker's step waits (`holder.asked`) and ends on that receipt. An instant step aimed at
  the body while the asker's own act's ask still waits on it doesn't start until that ask is answered
  (`_asking`), so a reply "ask, then buy" never fails as "already asked" (review on #53).
- The inhabitant view gains `asks` (each: from, name, action, until) and, while awake, offers `agree` and
  `decline` naming the askers. It lists only asks with `until > tick`: the step at `until` settles the ask
  by the body's nature before any act applies, so the last answerable tick is `until - 1` (review on #53).
- A world with no player is unchanged but for the engine field (tests/test_player_ask.py).

A god-scope session is unchanged and keeps `fire`. "Both" is one person holding two connections, an
inhabitant and a god, allowed only when the world's run.json says `"divine": true`. The god connection
is the dev/review tool until a game design says what divine power costs.

## Walking tile by tile (walk-1, joon 2026-11-22: "a real playable experience. wasd, interaction keys")
- `walk {to: [x, y]}` is a player act and no mind's (`sim.WALK_ACTS`, in `PLAYER_ACTIONS` only), so a world
  with no walk writes the log engine 38 writes, but for the engine field (tests/test_walk.py).
- The engine holds the tile (`agent["at"]`), not the client. A walked body's place is the place whose rectangle
  holds its tile (`worldmap.place_at`), or None in the open, where it is with nobody and nothing that needs
  someone is offered. Every rule that asks where a villager is (`Sim._pos`, occupancy, the offer, the view)
  reads the same tile, so a later rule about things on tiles has the body's tile to check.
- A walk is applied when it is recorded, at the start of its tick or late at the fixed point, and is not the
  tick's one act. It moves at most `WALK` tiles a tick over all the walks of that tick, x first, the same path
  and speed as a villager's route. (time-1a, below: recorded as it arrives, and bounded over sim-time.)
- Each walk emits `moved {from, pos, place}` (origin rule, parent: the act). A sleeper's walk, or one at or
  after bedtime, is a failed receipt ("asleep", "bedtime"); the engine walks the body home at bedtime as it
  does every villager.
- The body stands where it stopped (`override {do: "roam"}`, activity `visit`) for `DEFAULT_STEP_TICKS`, then
  the day's plan takes over; any other act replaces the stand, and `go_to`, `seek` and a gift set out from the
  tile. A walk stops a route where the body is. A seeker bound for a body in the open waits where it is.
- Walking out of a place settles the asks held there as declined (`_settle_ask`), before the asker's wait ends.
- The live runner pulled a tick for a walk only when its tile was in another place than the body's; since
  time-1a no walk pulls one (below). The client draws its own body between ticks at the engine's speed and takes the
  engine's tile when the keys are up and every walk is acked; it predicts nothing else.
- This replaces the `walk {place}` idea under "What would change this": a tile, not a place.

## Every body on a tile (space-1, plan/v0-contracts-spec.md B1)
- `Sim.pos(a, t)` is a pure function of the state and a sim-time `t` in ticks, whole or a Fraction (the core
  has no floats): the tile the player walked the body to; else a walker's point on its route at `t - 1`
  (`worldmap.position`, between tiles when `t` is; reach-1, below: a body is seen one tick behind its route);
  else its slot at its place. `Sim.tile(a, t)` is the floor.
- A slot is a tile of the place's own tiles (`worldmap.own_tiles`: its rectangle's tiles that `place_at` gives
  to it), picked by the id alone (`worldmap.slot`), so it never depends on who else is there. Two ids share a
  tile only when they differ by a multiple of the place's tile count. Nothing is stored for a villager, routes
  still run anchor to anchor, and a world with no player writes engine 38's log tick for tick: the no-player
  bar, seeds 1 to 3 at 100 villagers over 3 days, log and every tick's state hash (tests/test_space.py).
- `Sim.reach(a, b, t)`: at one place, or one of them in the open and their tiles at most 1 apart (Chebyshev).
  `Sim.sees(a, b, t)`: at one place, or one of them in the open and at most 6 apart. Two places hide each other.
  The inhabitant view's `seen` and what it witnesses use `sees`. In space-1 the player's acts still need the target at
  its place: reaching a walker in the open, and its stopping to listen, are reach-1, after time-1 (Steering
  2026-11-23 18:49Z), because a walker's position between ticks can't be played before acts carry a time.
- A walk sets out from the tile the body stands on (its slot, until it has walked), not from the anchor.
- The client draws every body from these fields. `world_model.gd` mirrors `slot` and `position`, and
  `tests/test_positions.gd` holds it to the engine at every tick boundary and at times inside a tick.

## Time inside the tick (time-1a, plan/v0-contracts-spec.md B2.1 and B2.2)
- Every player act may carry `f`, its time inside its tick in 600ths (`sim.SUB`, sim-seconds): t = tick + f/600.
  Only the live runner sets it, from the hub's clock: the real time since the last tick started over the pace,
  below 1; 599 for an act that joins a running tick, whose sim-time stands still while it computes; 0 in a
  paused world (Steering 2026-11-23 19:03Z). The `act` event records `f` when it isn't 0, and batch and replay
  use the recorded value. Acts apply in order of t: the engine refuses an `f` earlier than the act before it,
  and the runner never stamps one.
- A walk is bounded by walking speed over sim-time, in place of walk-1's allowance per tick. The body earns
  `WALK` tiles a tick as time passes, keeps at most one tick's walking while it stands (which a body that
  hasn't walked has), and a walk spends a tile for a tile: `state.player.walk = [the last walk's t in 600ths,
  what it kept, in 600ths of a tile]`. A walk is clamped toward `to`, never refused for asking too far. Over
  any stretch the body moves at most `WALK` tiles a tick plus what it had kept, and from a walk that was cut
  short it is never a whole tile ahead of walking speed (tests/test_time.py). This is B2.2's bound with the
  kept walking capped at `WALK` tiles in all, so walks stamped on whole ticks move exactly as walk-1's did.
- `Sim.take(action)` records a player action the moment it arrives between ticks, and a walk moves the body
  then. It is the first loop of `Sim.step` run early: the step skips what was taken (its list must begin with
  those actions), so batch, replay and resume, which hand the step the whole list, write the same events. A log
  may end on actions taken ahead of a tick that never ran; replay takes them the same way.
- The live runner takes every scheduled action as it arrives (`LiveRunner.take_due`), in slot order. A walk is
  acked then, with its `moved` event and the inhabitant's new view (protocol 3); other acts are recorded then
  and resolved in their tick. No walk pulls a tick: Review's two notes on #110 (a pull judged by the place of
  `to`, and a wiggle across an edge pulling a tick per key) are gone with the pull. No snapshot is written
  over actions taken ahead of their tick.
- Still on the tick: every other act's outcome (time-1b lands the small ones at their t), minds, needs, the
  economy, villagers' arrivals. A world with no player writes engine 38's log (the no-player bar).

## Small acts at their own time (time-1b, plan/v0-contracts-spec.md B2.3)
- Every player act but a line said lands the moment it is recorded (`Sim._land`, from `Sim.take` between
  ticks, from the step's first loop in batch and replay, and from `Sim.late_act` at a running tick's fixed
  point). It is judged with who is where right then, and its receipt follows its `act` event at once. What it
  starts (a trip, an ask a villager's mind must answer) still runs on the ticks after.
- An act holds the body for its cost: `sim.COST`, one sim-minute (60 of the 600ths) for every act, and 0 for
  a line said, an answer (agree, decline) and a walk (`sim.act_cost`), until verbs-1's table gives each verb
  its own. `state.player.busy` is the time, in 600ths, the body is free again. An act that finds the body
  held is refused `"busy"`. This replaces "one act per tick" for everything but words, so two acts a
  sim-minute apart in one tick both land (B2.7).
- A sleeper's act, or one at bedtime, is refused at once (`"asleep"`, `"bedtime"`, `"falling asleep"`), by the
  same test the tick's sleep branch makes.
- The live runner stamps an act with a cost at the moment the body is free, when it arrives sooner than
  that, so it lands instead of being refused: a plan's steps sent back to back each land a sim-minute after
  the last. There is no separate cap in real seconds: at the session's pace one sim-minute is 2 s, and a cap
  that rejects would fail those steps and a walk sent on key release. An act that arrives in the last
  sim-minute of a tick behind another is refused `"busy"`; one that arrives while a tick computes and finds
  the body held waits for the next tick and lands at its free moment.
- **Fallback (B2.6): a line said stays on the tick.** `say` is recorded as it arrives and resolved at the
  tick's fixed point, one line a tick (`"one act per tick"` for a second), as before. Reason: the talk path
  (text-1, think-1, apart-1, pace-2) times the villager's reply, the room's hearing and the pull of the
  answering tick from the tick a line lands in, and 45 pinned tests hold those logs. Moving it is a change to
  the talk path's order of events, not a small PR, and it belongs with the text thread's work.
- The player and mind twin bar (an act and a mind's intent of the same shape resolve the same) now holds at
  the fixed point, where a mind's intent resolves: tests compare the act sent as a late act.
- A world with no player writes engine 38's log (the no-player bar).

## Reaching across open ground (reach-1, plan/v0-contracts-spec.md B1.1, B1.3, B1.4, B2.4)
Steering ruled each point on 2026-11-23 (20:24Z, 20:26Z, 20:39Z).
- **A body is seen one tick behind its route.** A route's `depart` and `arrive` are the ticks the engine's
  step moves it, and the step for tick t runs at the end of t's window, so a walk that leaves in step t is
  walked during window t + 1. `Sim.pos(a, t)` is therefore the route at `t - 1`: a walk of n ticks is seen
  over n windows from its start to its destination, and the walker stands on the destination from its
  arrival step. Before this a walker was shown a tick ahead, and a walk of one tick was never seen on the
  road. It is a derived position only: nothing stored or logged moves, and reach, sight, the view, the
  client's drawing, `world_model.gd`, the page viewer and the playback check all read this one convention.
- **`Sim._near(a, b)`** is what the player's acts ask in place of "at one place": when one of the two is the
  possessed body, `Sim.reach` at the act's own time (one place, or one of them in the open and their tiles
  at most a tile apart); between two villagers, `_together`, as ever. It stands at the nine sites that asked
  for one place: asks, kin asks, theft, gifts and seeking (`REACH_STEPS`), talk and its limit, a held ask and
  its answer, and a line said. A body told to give to someone already in reach stays where it stands.
- **The offer names those in reach.** `Sim._view(a, occupancy, reach=True)` adds, for the possessed body,
  those within a tile of it across open ground, so the view's `offer` names them and the engine accepts them.
- **`hail {target}`**, the player's alone, cost 0, no words, no mind call: a villager walking by within
  sight (6 tiles) at the act's time stops where it is. Refused `"target not in sight"`, or `"not walking"`
  for one at a place or already at its destination's anchor. A walker with an errand of its own stops too
  and keeps the errand. One already stopped answers `"already stopped"` and gains no time.
- **A halt re-bases the route**, with no new state: `from` becomes the tile the walker stands on, `depart`
  the tick its wait ends (less one, by the convention above), `arrive` follows. A `halt` rule event
  `{pos, place, depart, arrive}` cites its cause and tells a reader of the log where it stands and until
  when. The wait is `LISTEN_TICKS` (3), started again by each line said to it and by each act of the
  player's on it from within reach (asks, theft, gifts), never by another hail. Its answer to a line works
  as at a place: stay keeps it standing for the occasion's time, leave sets it off at once (an `activity`
  event with its `from`).
- **Words need the same place or reach.** An inhabitant gets the full event only from someone at its place
  or in reach. From someone in sight and no nearer it gets `inhabitant.SIGHT`'s fields: that it set out,
  arrived or stopped, and that it spoke or answered and to whom, with no text. In the view's `seen`, `asleep`
  shows only for those in reach (`reach: true`). This is view-side: the log is unchanged.
- **Who walks past.** Where every walker will be until the next step is already fixed, so the view lists in
  `seen` each walker that is in sight at some time in the coming tick, with `in` (the span of the tick it is
  in sight for, in 600ths) and `track` (the tiles it is seen on then, about a tile apart). Nothing of it is
  told outside that span. `offer.hail` names those of them the body can call to. Without this a walker that
  crosses the body's sight inside one tick, 12 tiles of a 40-tile tick, would never be listed.
- **Third parties in the open are unchanged** (sight-1, later): a line in the open is heard by its listener.
- The log-only presence check follows bodies from the log (`invariants.Bodies`): an act of the player's with
  one party in the open must be within reach at its time (sight for a hail), and a `halt` must be of a
  walker on the tile its route puts it on.
- A world with no player writes engine 38's log (the no-player bar).

## One table of verbs (verbs-1, plan/v0-contracts-spec.md B4)
A pure move: nothing a mind, a player or a rule can do changes, and the log and every prompt are
byte-identical to reach-1's (only the engine field differs).
- **`asiimov/verbs.py` holds `VERBS`**, one row a verb, 28 rows: the 16 a mind may choose, the player's
  six (`agree`, `decline`, `go_to`, `say`, `walk`, `hail`), and the rules' six (`shop`, `trade` and the
  neighbour's four answers). A row says who may use the verb (`who`: mind, player, rule), what it names
  (`takes`: a person, a place, a tile, a line) and among whom a mind may name it (`among`), what it needs
  (`needs`: awake, the body free, the cooldown, reach, sight, kin, the market, coin, food, an open ask, a
  walker), its `cost` in sim-minutes of the body's time (1, or 0), how it runs (`kind`: at once, an
  ask, a walk to someone, a hold, an activity, a walk to a place; `step`, its place in a plan), the `Sim`
  method that carries it out (`handler`), every reason its receipt can carry (`reasons`), and its `words`.
- **Read from the table:** the engine's lists (`DELIBERATE_ACTIONS`, `PLAYER_ACTIONS`, `ACTIVITIES`,
  `PERSON_STEPS`, `STEP_DOS`, `INSTANT_STEPS`, `ASK_STEPS`, `RATED_ACTS`, `REACH_STEPS`, `RULE_STEPS`,
  `GIFTS` and the four one-verb groups), the minds' menu (`ACTION_CLAUSES`, `action_clauses(price)`), the
  reply schema's `NO_TARGET` and `PERSON_ACTIONS`, `act_cost`, and the dispatch of a mind's or a player's
  act to its handler. `tests/test_verbs.py` holds each list to the literal it replaced, in its order.
- **Words.** A row's words are the mind's menu line, the player's label and short label, what a watcher
  sees it doing, how an ask reads to each side, a plan step and the narrator's line for each outcome
  (`told`). `REASONS` words every `why` a receipt can carry, as the actor reads it and as the other side
  does. `python -m asiimov.verbs` writes them to `client/godot/scripts/verb_words.gd`, which
  `inhabit_ui.gd` loads in place of its own tables; `narrate.py` reads `told` and `SEEN`; and
  `plan/ai-playtest.md` carries `verbs.player_list()`. Tests fail when the file or the doc is stale, when a
  verb a mind may choose lacks a word, when a `why` in the source or in a random session lacks its words,
  or when a receipt carries a `why` its row doesn't list.
- **Needs are declared; the handlers still check.** Each handler keeps its own order of checks, because
  the first failing check picks the receipt's `why` and a table-driven order would change which reason a
  doubly-barred act gets, and so the log. The declared needs are tested against behaviour (busy inside a
  cost, asleep, reach) and are what things-1 and deal-1 add rows against.
- **The bank** (B4.2): `tests/data/verbs_bank.json` holds the player's own recorded acts and lines from
  the aipt3 and pt11 sessions (seed, size and talk settings with them) and each set of verbs their minds'
  menus left out. The sessions are replayed on stub minds, and each log, whose mind events carry every
  prompt's hash, must hash to what reach-1 gave; so must every menu and schema text at three prices.
  It also holds a world with no player (stub, seed 1, 100 villagers, 3 days, and the same world with one
  forced sick case): a recording mind hashes every prompt the engine hands it, byte for byte, and both
  hashes are reach-1's (Steering 2026-11-23 22:00Z, for goods-1 R6). The hashes are of the text the engine
  builds for a real mind (`MindRequest.prompt`, and the log's `prompt_hash` of it); nothing in them is the
  stub's, which only supplies the replies that move the world on.
- **Left where they are**, as rules more than verbs: `TALK_BARRED`, `WAIT_STEPS`, `STEP_TICKS`, the sick
  lists, `memory.E9_ACTIONS` (an old prompt's literal text), `inhabitant.SAYS_ACTS`, the stub minds'
  lines, and the client's `Plan.ANYWHERE`, `Plan.WALK_FIRST` and `walk_keys.AFAR`.
- **A new verb is a row**, its handler, and `python -m asiimov.verbs`; one a mind can choose also needs
  its bank and bars (B7.2).

## Things in the world (things-1, plan/v0-contracts-spec.md B3; Steering 2026-11-23 22:03Z)
- **One ledger.** Food and coin stay counts. A unique thing is a record in `state["things"]["list"]`, `{id,
  kind, at: [x, y] or null, holder: villager id or null}`, with exactly one of `at` and `holder` set; ids
  count up and are never reused.
- **One kinds table**, `asiimov/things.py` `THINGS`, five of the six kinds Build 1 allows: whether it can be
  carried, what using it does, whose it is to use, what that costs, where it stands or grows, its words.
  - `berries`: grow in the woods, 6 lying there after each dawn. Using one gives 1 food and the thing goes.
  - `flower`: grows in the woods, 4 after each dawn. Carried, put down, handed over. No use.
  - `stall` (the market), `bench` (one at each workplace), `bed` (one in each home): fixtures. Using one is
    the verb done there (`buy_food`, `work`, `rest`), by that verb's own rule, offer and receipt. A bench
    and a bed are for the body whose workplace or home it is.
- **A stake, `things`** (`{carry: 4, grows: {berries: 6, flower: 4}}`, recorded in genesis), set by `--things`
  on a new live world. A world without it has no things, and neither has a world with no player: things
  are made at the first `possess` (B3.4). So every earlier log stands, the no-player bar included. Every
  Build 1 launcher passes `--things`.
- **Every change is an event** (B3.3). `things` (at the first possess, citing it): every fixture and the
  first growth. `grew` (at 06:00, when the ground made anything): each growing kind brought back to its
  count on free tiles of its place, picked by the lowest keyed score (stream `things:<kind>:<day>`, index:
  the tile's number among the place's tiles; T22). `took`, `dropped`, `handed`: a thing changes hands or
  tiles. `used`: it goes, and its food is counted in the food ledger. Nothing else makes, moves or ends one.
- **Four verbs, rows of the verb table, the player's alone:** `take`, `drop`, `use`, `hand`, each naming a
  thing by id (`hand` also a person), each a sim-minute. Reach to a thing is by tile, inside a place too: on
  the body's tile or the next one at the act's own time. `hand` needs the receiver in reach (`_near`),
  awake, and with a hand free; a villager holds what it is handed (the record's `holder`; no villager field)
  and does nothing with it in Build 1.
- **A gift of a thing follows give_food's rule:** the receiver warms to the giver (by 100 in things-1; from
  deal-1 by what the thing is worth, see Offers below), both remember it
  ("gave a flower to X", "Y gave me a flower"), and the receiver may stop to think ("Y gave you a flower").
  That is the only text a thing puts in a prompt. `tests/test_things.py` holds the row check Steering asked
  for: per kind, the receiver's prompt differs from its prompt after a gift of food only in that sentence
  and that note, which name exactly the giver and the thing (and, from deal-1, in the receiver's affinity
  line for the giver, checked to the number).
- **The view** (B3.5): `things`, `carried`, and `offer.take`, `use`, `drop`, `hand` (plan/client-protocol.md).
  A thing inside a place shows only to a body in that place or beside it; one in the open, within 6 tiles;
  a fixture always. A fixture's use is offered only beside it, to its owner, when its verb is offered.
- **Invariants** (`invariants.Things`, `thing_violations`): the ledger is rebuilt from the log alone and must
  equal the state's; a take, a put-down and a use happen within a tile of the thing; each record has one of
  a tile and a holder, a tile on the map, a known kind; no fixture is held; nobody carries more than the cap.
- **Tests:** `tests/test_things.py` (21) and `client/godot/tests/test_things.gd`.

## Offers (deal-1, plan/v0-contracts-spec.md B5; Steering 2026-11-25 04:00Z, 04:06Z, 04:35Z, 04:38Z and 04:50Z)
- **One verb, a row of the verb table, the player's alone:** `offer`, naming a villager in reach and two
  sides, what the player gives and what it wants: counts of food and coin and things by id. It costs a
  sim-minute. The world's rule settles it; the villager only says the answer in its own words.
- **Limits first** (`Sim._offer_limit`), each a worded receipt with no roll and no mind call: out of reach,
  asleep, the player lacks what it offers, the villager lacks what is wanted, the villager would keep under
  1 food, a grudge (affinity under -300), or hands that would be over-full. The same check runs again when
  the answer's prompt is built and again after the answer's words.
- **A gift** (nothing wanted) moves at once, by give_food's rule: the receiver warms, both remember it, and
  the receiver may stop to think.
- **One warmth rule for every gift** (`Sim._gift_warmth`; Steering 04:35Z and 04:38Z): 100 x min(worth,
  price) // price. give_food (1 food) and give_coin (a meal's price) are worth the full 100, as before. An
  offer's gift, and from deal-1 on a thing handed over with `hand`, warm by what they are worth: a picked
  flower (worth 1) or a single coin gives 100 // price, where things-1 gave a handed flower 100. Affinity
  feeds the next offer's limit, so warmth has to cost what it is worth.
- **The roll** (`Sim._offer_roll_limit`): each side is worth its coin (food at the stall's price, a coin 1, a
  thing its kind's `worth` in `asiimov/things.py`; every carried kind has one). The terms run from ask_food's
  base (100, nothing for something) through buy_from's (350, an even trade) to 950 (something for nothing);
  the villager's agreeableness, its affinity for the player and kinship are added as `_share_answer` adds
  them, clamped to 0..950. So 1 food for its price has buy_from's odds and a bare ask for 1 food has
  ask_food's (ask_kin's for family); `tests/test_deal.py` pins both against the real acts in twin worlds.
  **The loss cap:** where the villager would be out more than one food's price, the limit is multiplied by
  price / loss, so a lopsided offer's odds fall as fast as its stake grows.
- **Keyed and held:** one roll per exact terms (things by kind) per pair per day, on the stream
  `offer:<receiver>:<giver>:<terms>` at index day (T22), written as `decided` with the roll, the limit and
  what it rests on. A second offer of the same terms finds the held answer; at most 3 different offers are
  decided per pair per day (`too many offers`); terms the villager loses on are done for the day once
  dealt (`already traded`), so a lucky yes can't be repeated. A fair or better deal's held yes can be had
  again, each time within the limits (Steering 04:35Z).
- **The spoken answer:** the offer takes a line's place (`offered`, the listening hold, a `spoken` occasion
  that carries the offer), and the villager answers on the next tick whatever the talk pacing. Its prompt
  (`offer_tail`, `offer_choice`) states the offer and the answer already made; the reply's schema is say-3's
  (`says`, `next`), with no answer to pick, so a failed or odd reply changes nothing but the words. The answer
  prompt's holdings line names what the hands hold (`carry_words`: "5 food, 30 coin and a flower"; empty hands
  read "5 food and 30 coin", as before), so "your flower" or "your hands are full" rests on a fact it shows.
- **Gone:** if the one who offered is out of reach when the villager would answer, no answer is asked for and
  nothing is said: a `deal` with `ok: false` and `why: "gone"` cites the offer and the decision (Steering
  04:50Z). The terms stay open for the day.
- **The deal:** after a yes the limits are checked live and one `deal` moves both sides, citing the offer,
  the answer and the decision; its actor is the one who offered. A yes that can no longer be had writes
  `deal` with `ok: false` and its reason, so a yes never fizzles silently. A rolled no writes no deal. A
  trade warms the player to the villager by 50; both remember it.
- **No player, no offer:** a world with no player, or a played session with no offer in it and nothing
  handed over, differs from things-1's log only in the genesis `engine` field (tested against the hash
  things-1's own code wrote).
- **Invariants** (`invariants._offer_deal_violations`, `Things.see`): an offer's deal is well formed, cites
  what it must (the act for a gift; the offer, the receiver's answer and its earlier yes with roll under
  limit otherwise), and things move only out of hands that hold them.
- **The client:** a villager's row and the E list carry "offer a trade", which opens the Trade panel
  (`offer_box.gd`, `main.gd`): count boxes for what you give and what you want, a line saying what will be
  sent, and Offer. The feed words the offer, the answer, the deal or why nothing moved.
- **Tests:** `tests/test_deal.py` (39); the things row check in `tests/test_things.py` now also reads the affinity line and `client/godot/tests/test_offer.gd`.

## Bars (the build must pass these before review)
1. **No-player worlds unchanged:** for seeds 1 to 3 at 100 villagers over 3 days, the log is identical
   to the previous engine's apart from the genesis `engine` field, and the state hash at every
   checkpoint matches.
2. **Live = batch** with possess, act and release at scripted ticks, hash for hash, plus `replay()` and
   the S8 kill/reopen fuzz from tests/test_live.py, extended to the new verbs. That includes a kill while
   a body is possessed: on reopen with no inhabitant, a `release` is recorded 36 ticks later, and the log
   still equals batch with that release scripted.
3. **Same rules as a mind (S1):** a hostile player sends every action with random, missing, wrong-typed
   and absent targets every tick for 3 days. Invariants and ledgers are checked every tick
   (test_invariants). Rolled acts per villager per day never exceed what a mind could reach (the
   deliberation cooldown's maximum, 144 / 12 = 12), and every refusal is a receipt with the same reason a mind's identical choice
   gets. Every act outside that tick's offer is refused with `"not offered"` and changes nothing else
   (this is checked against a twin where the act is absent: the same log apart from the act and
   receipt events). For offered acts: replaying each one as a recorded mind deliberation for the same
   villager at the same tick gives the same receipts, because both resolve in `_commit` in `_order`,
   including under contention (two villagers stealing from one target in the same tick).
4. **No deliberation while ordered:** zero `deliberate` mind events for the possessed villager within
   `IDLE_TICKS` of its last order. After that its mind deliberates again, and the next order stops it.
   Its plan and reflect events continue.
5. **Inhabitant leak test:** over a 3-day world with a player in the inhabitant scope, every message
   sent is scanned. It must contain no `soul`, `traits`, `params`, `raw_output` or `prompt_hash` of any
   villager, and no position of a villager who isn't at the player's place, unless it's family at
   home. Fuzz the hello (`agent` out of range, wrong type, already taken).
6. **Client:** the Godot client binds to its villager, shows the offered actions as buttons (the menu
   comes from the server, never computed in GDScript), and its headless test drives `act` and checks
   the receipts against the stream.

## Cost
$0, using stub and noisy minds. A real-mind playtest needs its own charter, on a pod that isn't serving
a canonical run (T21's batching caveat).

## What would change this
- If joon picks option 1 (stands still), step 4 changes, and the possessed villager's memory has
  gaps. Bar 4 then expects no plan or reflect either.
- If playtests show that orders through the deliberation menu feel too coarse (no free walking to a
  tile), a `walk {place}` verb maps onto `_set_activity` with the `explore` activity. It's still one
  rules path, but it's a new action the minds don't have, so it would need its own spec line and L19
  check.
