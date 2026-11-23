"""things-1 (plan/v0-contracts-spec.md B3): the kinds of unique things, one table.

Food and coin stay counts: they are the same everywhere and every rule counts them. A thing is one record,
`{id, kind, at: [x, y] or None, holder: villager id or None}`, with exactly one of `at` and `holder` set. This
table says for each kind whether it can be carried, what using it does, what that costs the body in
sim-minutes, where it first stands or grows, and its words. A new kind is a row here.

  carry   whether a body can pick it up, carry it, put it down and hand it over.
  use     None; {"food": n}: the thing is used up and the body gains n food; or {"verb": v}: the verb done
          here, by the verb's own rule (a fixture: the map shows where a verb you already have is done).
  own     for a fixture: whose it is to use. None (anyone's), "work" (the body's workplace) or "home".
  cost    sim-minutes using it holds the body.
  stands  for a fixture: the places that hold one ("market", "work": every workplace, "home": every home)
          and its tile there, as an offset from the place's anchor.
  grows   for what the ground makes: the place, and how many lie there after each dawn.
  words   name ("the flower"), and the player's label for each verb it allows.
"""

CARRY = 4  # things a body carries at most

THINGS = {
    "berries": {"carry": False, "use": {"food": 1}, "own": None, "cost": 1, "stands": None, "grows": ("woods", 6),
                "words": {"name": "the berry bush", "use": "Pick berries"}},
    "flower": {"carry": True, "use": None, "own": None, "cost": 1, "stands": None, "grows": ("woods", 4),
               "words": {"name": "the flower", "take": "Pick up the flower", "drop": "Put down the flower",
                         "hand": "Give the flower to %s", "gift": "a flower"}},
    "stall": {"carry": False, "use": {"verb": "buy_food"}, "own": None, "cost": 1, "stands": ("market", (0, 0)),
              "grows": None, "words": {"name": "the stall", "use": "Buy food at the stall"}},
    "bench": {"carry": False, "use": {"verb": "work"}, "own": "work", "cost": 1, "stands": ("work", (-2, 1)),
              "grows": None, "words": {"name": "your bench", "use": "Work at your bench"}},
    "bed": {"carry": False, "use": {"verb": "rest"}, "own": "home", "cost": 1, "stands": ("home", (0, 0)),
            "grows": None, "words": {"name": "your bed", "use": "Rest in your bed"}},
}
assert len(THINGS) <= 6  # B3.2: Build 1's starter set, fixtures included

FIXTURES = tuple(k for k, row in THINGS.items() if row["stands"])
GROWN = tuple(k for k, row in THINGS.items() if row["grows"])
CARRIED = tuple(k for k, row in THINGS.items() if row["carry"])
assert not set(FIXTURES) & set(CARRIED) and not set(FIXTURES) & set(GROWN)

# The world's parameters for things, as genesis records them under stakes["things"] (a world without the
# key has no things: every world before things-1, and every world with no player).
DEFAULTS = {"carry": CARRY, "grows": {k: THINGS[k]["grows"][1] for k in GROWN}}


def client_words() -> dict:
    """The client's words for things, by kind: its name and a label for each verb it allows."""
    return {key: {kind: row["words"][key] for kind, row in THINGS.items() if key in row["words"]}
            for key in ("name", "use", "take", "drop", "hand", "gift")}
