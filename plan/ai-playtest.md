# AI playtest

2026-11-16. Gate: G5, "It's playable" (DEMO.md). joon's ask (10:48Z): agents that playtest on their own, set
criteria, give feedback and suggest fixes. An AI session is a cheap check before a person's playtest. It
never replaces joon's own form: G5's lines are joon's to answer.

## How it plays
The tester plays the **real Godot client through its window**: `scripts/aiplay.py` runs the client on a
virtual screen (Xvfb) in a cloud container and gives the agent screenshots, mouse clicks, the wheel, typing
and window resizing. It needs no Mac and no person's screen. Nothing in the harness speaks the protocol;
the client does, as it does for a person.

Why the screen and not a bot on the WebSocket: the form's lines 1–5 are about what the player can see and
do, and the client makes the words, the panels and the buttons in GDScript. A socket bot would score the
protocol, not the game. The talk criteria and lines 6–7 are read from the server's logs afterwards, so they
don't depend on how the tester played.

## Run one session
1. **World:** as plan/playtest-dec7.md §2 (pod, `asiimov.live --paused --token --things`, step to the body's waking
   tick, `results/tools/live/pacer.py`). The pacer starts the world when the tester joins and pauses it
   when it leaves.
2. **Tester:** `python scripts/aiplay.py start DIR --url wss://POD-8765.proxy.runpod.net/ --inhabit N
   --token T --size 1440x900 --record` (add `--relay` from a cloud container, whose TLS gateway Godot doesn't
   trust; the token is on godot's command line, visible in `ps`, which is fine on a private machine), then an agent that can read images follows the brief below, using
   only `aiplay.py` on DIR. `aiplay.py stop DIR` ends the session (the player leaves).
3. **Report:** fetch the run directory, then `python scripts/aiplay_report.py RUN_DIR DIR` writes
   `rows.json` and `report.md`. A second agent grades `rows.json` by the rubric below into
   `DIR/grades.json`; run the report again to fold in the grades and the tester's `DIR/form.md`.

`aiplay_report.py` also reads a person's session (it reproduces pt8's lines 6 and 7 from its logs).

## The tester's brief
You are playtesting a village game as a first-time player. You are villager N. Play only through the
window: `aiplay.py shot | click | scroll | type | key | hold | resize` on DIR (`hold DIR w+d 3` holds keys down together for 3 s, to walk); a screenshot's pixels are the click
coordinates. Don't read the repo, the logs or the run directory: what you can't learn from the screen, a
player can't either, and that is a finding. The world moves one tick (10 village minutes) about every 20 s
while you are awake, and you get one action a tick. Keep `DIR/notes.md` as you go: the game clock, what
you did, what you expected, what happened, and the screenshot number that shows it.

Look at a fresh screenshot before each click: the layout shifts as villagers come and go. A position read
off a cropped screenshot is the crop's X, Y further right and down in the window. Play the whole time you
are given; if the game moves you on its own or seems stuck, note it and keep playing.

Do these, in about this order, in the time you are given:
1. **Orient.** From the screen alone: who are you, what do you need, what do you hold, where are you?
2. **Move.** Walk to two places by clicking the map.
3. **Plan.** Line up two steps with "Plan ahead" and watch them settle.
4. **Act.** Try each kind of action you are offered at least once. Read what happened and why.
   Find something lying in the world, pick it up, carry it and give it to someone; pick food where it grows.
5. **Talk** (at least half your time). Hold three or more conversations of three or more turns, with
   different villagers:
   - small talk, then ask about their day or work, and check the answer against what the screen says of them;
   - a need: you want food. Ask in your own words and see whether anything changes beyond words;
   - a proposal: ask someone to do something with you or for you, and see whether they do;
   - memory: go back to someone you talked to and refer to it;
   - one line from outside the world ("are you an AI?") and one rude line.
6. **Listen.** Read "Said near you". Note lines that fit nothing, are said to no one, or repeat.

Then stop the session and write `DIR/form.md`:
- the form's seven lines (below), each yes or no with a note and a screenshot number. For lines 6 and 7
  give your feel; the log measures them;
- voice (people or stiff), look (did the art, bubbles and camera help), and the worst moment;
- any criterion you think the form misses, with your own score;
- up to seven fixes, ranked: what you saw (screenshot), why it hurts play, the smallest change to try.

## Criteria
| # | Criterion | Read by |
|---|---|---|
| 1 | State: I could always see my needs, food, coin, role and place | tester, screen |
| 2 | Plan: I could set a plan and watch it step by step | tester, screen |
| 3 | Outcomes: every act showed what happened and why, in words | tester, screen |
| 4 | Talk near me: I could read what villagers near me said | tester, screen |
| 5 | Space: I could see places and villagers and move among them | tester, screen |
| 6 | Pace: acts land within 3 s at p95 | log |
| 7 | Talking to villagers: replies fit, within 6 s at p95 | log (time), judge (fit) |
| 8 | Lines lost: lines the listener never heard (they left, or slept) | log |
| 9 | Voice: the reply is said to me, not a thought about me | judge; log proxy |
| 10 | Stays in the world on out-of-world and rude lines | judge |
| 11 | Repeats: the same reply, or the same nearby line, word for word | log |
| 12 | Talk leads somewhere: after a reply, the villager acts toward me, and goods or places change | log, tester |
| 13 | Directed talk: each line near me is said to someone, about something that is theirs | judge |
| 14 | The world comes to me: asks, visits and gifts aimed at me unprompted | log |

Criterion 12 is joon's direction of 2026-11-16 (talk, then agreement, then a change of state). Engine 30
bars gifts inside a talk window, so "asked for food in words, got food" can't pass yet; the report says how
often it was tried.

## The judge's rubric (`grades.json`: `{row id: {...}}`)
Grade from `rows.json` alone. For each exchange with a reply (`x…`):
- `fits`: 1 if the reply answers what the player said, 0 if it ignores it or answers something else.
- `voice`: `spoken` (words said to the player), `thought` (reasoning or a report about the player, in the
  third person or as a plan), or `mixed`.
- `in_world`: 1 if it stays in the village and in character, 0 if not. A villager denying it is an AI, in
  character, is 1.
- `note`: a few words, only when a grade needs them.

For each narrated item (`h…`):
- `directed`: 1 if it is clear who each line is said to and why it concerns them, 0 if it is news told to
  no one.
- `natural`: 1 if it reads like people talking, 0 if recited or stiff.

## What a player can do
The engine's whole list for a player, written from the verb table (`python -c "from asiimov import verbs;
print(verbs.player_list())"`; `tests/test_verbs.py` fails when this section and the table differ). It is for
whoever sets a session up and reads its report: the tester still learns the game from the screen alone.

- `buy_food`: Buy food at the market; holds the body 1 sim-minute.
- `go_market`: Go to the market; holds the body 1 sim-minute.
- `ask_kin`: Ask someone (family) for food, naming a villager; holds the body 1 sim-minute.
- `ask_food`: Ask someone for food, naming a villager; holds the body 1 sim-minute.
- `buy_from`: Buy food from someone, naming a villager; holds the body 1 sim-minute.
- `steal`: Steal food from someone, naming a villager; holds the body 1 sim-minute.
- `give_food`: Give food to someone, naming a villager; holds the body 1 sim-minute.
- `give_coin`: Give a coin to someone, naming a villager; holds the body 1 sim-minute.
- `seek`: Go and find someone, naming a villager; holds the body 1 sim-minute.
- `avoid`: Keep away from someone, naming a villager; holds the body 1 sim-minute.
- `work`: Go to work; holds the body 1 sim-minute.
- `socialize`: Go and see people; holds the body 1 sim-minute.
- `home`: Go home; holds the body 1 sim-minute.
- `rest`: Rest; holds the body 1 sim-minute.
- `explore`: Explore; holds the body 1 sim-minute.
- `wait`: Wait; holds the body 1 sim-minute.
- `agree`: Say yes to someone, naming a villager; costs the body no time.
- `decline`: Say no to someone, naming a villager; costs the body no time.
- `go_to`: Walk to a place on the map, naming a place; holds the body 1 sim-minute.
- `say`: Say a line to someone, naming a villager, with the words; costs the body no time.
- `walk`: Step to the next tile, naming a tile; costs the body no time.
- `hail`: Call out to someone, naming a villager; costs the body no time.
- `take`: Pick up a thing beside you, naming a thing; holds the body 1 sim-minute.
- `drop`: Put down a thing you carry, naming a thing; holds the body 1 sim-minute.
- `use`: Use a thing beside you or in your hands, naming a thing; holds the body 1 sim-minute.
- `hand`: Hand a thing you carry to someone, naming a thing and a villager; holds the body 1 sim-minute.

## What the report adds from Build 1 (Steering 2026-11-23)
- After a hail: any errand of a hailed villager that then fails with "time ran out" (reach-1).
- After a flower: what the villager said or did next, in its own words (things-1).
- Food picked from the woods against food bought, for the session (`used` events against `buy_food`
  receipts).

## Limits
- Since the P-pass (2026-11-18) the UI is drawn 1.25x larger and the right-hand column holds only you, who's
  here and the action buttons; your plan and the Talk box sit in the middle column. A tall window (`--size
  1440x1600`) still shows more buttons at once; crop positions changed, so take them from a fresh screenshot.
- One tester is one world: a world holds one player. Several testers need several worlds.
- The tester is slower than a person (several seconds a look), so its feel for pace is weak; lines 6 and 7
  come from the log.
- A tester's yes on lines 1–5 is evidence, not G5: joon answers the form.
