"""The village map: where each place is, and how long walking between two points takes.

Integer tiles of TILE_M metres; the plaza's anchor is (0, 0). Every place has a rectangle (x, y, w, h)
for drawing and an anchor tile for distance. Distance is Manhattan along an L path (x first, then y),
nothing blocks it, and travel takes ceil(distance / WALK) ticks. The layout is a function of the number
of families alone, with no randomness, and genesis records it, so a replay checks it like any event.
Prompts keep naming places; coordinates are for the engine and the client (plan/t6-map-spec.md).
"""
TILE_M = 20
WALK = 40          # tiles per tick: 800 m per 10 minutes, about 4.8 km/h
RING0 = 20         # the first ring of home lots, in tiles from the plaza's anchor
RING_STEP = 8      # rings and lots are this many tiles apart
EDGE = 12          # field and woods sit this far beyond the first ring with no homes


def _rect(anchor, w, h):
    return [anchor[0] - w // 2, anchor[1] - h // 2, w, h]


def _ring(d: int) -> list[tuple[int, int]]:
    """Lot anchors on the square ring at Chebyshev distance d, clockwise from its top-left corner."""
    s = range(-d, d, RING_STEP)
    return ([(x, -d) for x in s] + [(d, y) for y in s] + [(-x, d) for x in s] + [(-d, -y) for y in s])


def build_map(homes: list[str]) -> dict:
    """The layout for these homes, which take lots in the order given (family id order)."""
    lots, d = [], RING0
    while len(lots) < len(homes):
        lots += _ring(d)[:len(homes) - len(lots)]
        d += RING_STEP
    edge = d + EDGE  # d is now the first ring with no homes
    places = {"plaza": (0, 0, 10, 10), "market": (14, 0, 8, 6), "workshop": (-14, 0, 8, 6),
              "field": (0, edge, 30, 16), "woods": (0, -edge, 40, 16)}
    out = {name: {"anchor": [x, y], "rect": _rect((x, y), w, h)} for name, (x, y, w, h) in places.items()}
    for home, (x, y) in zip(homes, lots):
        out[home] = {"anchor": [x, y], "rect": _rect((x, y), 6, 6)}
    return {"tile_m": TILE_M, "walk": WALK, "places": out}


def place_at(world_map: dict, pos) -> str | None:
    """The place whose rectangle holds this tile, or None out in the open (walk-1). Where two rectangles
    share a tile (a home lot beside the market does), the nearer anchor wins, then the lower name, so the
    answer never depends on the order places were listed in."""
    x, y = pos
    best = None
    for name, p in world_map["places"].items():
        rx, ry, w, h = p["rect"]
        if rx <= x < rx + w and ry <= y < ry + h:
            key = (distance(pos, p["anchor"]), name)
            if best is None or key < best:
                best = key
    return None if best is None else best[1]


SEE = 6    # tiles (Chebyshev) a body in the open sees, and is seen from (space-1, B1)
REACH = 1  # tiles (Chebyshev) to what a body in the open can touch: its own tile or the next one


def own_tiles(world_map: dict, place: str) -> list[list[int]]:
    """The tiles of `place`: those of its rectangle that place_at gives to it, row by row. Never empty: a
    place's anchor is its own."""
    rx, ry, w, h = world_map["places"][place]["rect"]
    return [[x, y] for y in range(ry, ry + h) for x in range(rx, rx + w) if place_at(world_map, [x, y]) == place]


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def slot(tiles: list, aid: int) -> list[int]:
    """Where villager `aid` stands among a place's `tiles` (space-1): keyed by its id alone, so it never
    depends on who else is there and nothing is stored. Consecutive ids land far apart (a stride near the
    golden section of the count, coprime with it), and two ids share a tile only when they differ by a
    multiple of the count."""
    n = len(tiles)
    s = max(1, n * 618 // 1000)
    while _gcd(s, n) != 1:
        s += 1
    return list(tiles[(aid * s + n // 2) % n])


def near(a, b, tiles: int) -> bool:
    """Whether two tiles are within `tiles` of each other, diagonals counting as one (Chebyshev)."""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1])) <= tiles


MARGIN = 4  # tiles of open ground around the places' rectangles that a body may stand on


def bounds(world_map: dict) -> list[int]:
    """[x0, y0, x1, y1], inclusive: the tiles a body may stand on, the places' bounding box plus MARGIN."""
    rs = [p["rect"] for p in world_map["places"].values()]
    return [min(r[0] for r in rs) - MARGIN, min(r[1] for r in rs) - MARGIN,
            max(r[0] + r[2] for r in rs) - 1 + MARGIN, max(r[1] + r[3] for r in rs) - 1 + MARGIN]


def on_map(world_map: dict, pos) -> bool:
    """Whether `pos` is a tile a body may stand on: [x, y], whole numbers, within bounds()."""
    if not (isinstance(pos, (list, tuple)) and len(pos) == 2 and all(type(v) is int for v in pos)):
        return False
    x0, y0, x1, y1 = bounds(world_map)
    return x0 <= pos[0] <= x1 and y0 <= pos[1] <= y1


def step_toward(frm, to, left: int) -> list[int]:
    """Up to `left` tiles from `frm` toward `to` along the L path, x first, as position() walks it."""
    (x, y), (tx, ty) = frm, to
    dx = min(left, abs(tx - x))
    x += dx if tx >= x else -dx
    dy = min(left - dx, abs(ty - y))
    y += dy if ty >= y else -dy
    return [x, y]


def distance(a, b) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def travel_ticks(a, b) -> int:
    return -(-distance(a, b) // WALK)


def position(route: dict, anchor_to, tick: int) -> list[int]:
    """Where a walker on `route` is at `tick`: WALK tiles per tick along the L path, x first."""
    (x, y), (tx, ty) = route["from"], anchor_to
    left = WALK * max(0, tick - route["depart"])
    dx = min(left, abs(tx - x))
    x += dx if tx >= x else -dx
    dy = min(left - dx, abs(ty - y))
    y += dy if ty >= y else -dy
    return [x, y]
