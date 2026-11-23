# Walking with the keys and the E key's list (walk-1), apart from the widgets: main.gd reads the keys and draws.
# The engine holds where the body stands. Between its ticks this moves the body on screen at the engine's own
# walking speed, for drawing only; the tile is sent as a `walk` intent and the engine's answer is the truth.
extends RefCounted

const Words := preload("res://scripts/inhabit_ui.gd")
const LiveClient := preload("res://scripts/live_client.gd")

const MAX_TILES_S := 12.0  # a fast local world (stub minds at 2 ticks a second) would otherwise fling the body
const SEND_S := 1.0        # while walking, the tile is sent at most this often; the engine takes WALK tiles a tick
const MAX_ITEMS := 9       # the E list is picked with the keys 1 to 9
const AFAR := ["seek", "avoid"]  # going to find someone, or keeping away: not for someone you are standing beside


static func speed(walk_per_tick: int, tps: float) -> float:
	## Tiles a second on screen: the engine's WALK tiles a tick at the world's pace, so the engine can always
	## follow what the screen showed.
	return minf(float(walk_per_tick) * tps, MAX_TILES_S)


static func step(pos: Vector2, dir: Vector2i, tiles: float, bounds: Array) -> Vector2:
	## `pos` moved `tiles` along `dir` (each of x and y is -1, 0 or 1). The engine counts tiles along x plus
	## tiles along y, so a diagonal splits the distance between them. Kept on the map (`bounds`: x0, y0, x1, y1).
	if dir == Vector2i.ZERO:
		return pos
	var each := tiles / float(absi(dir.x) + absi(dir.y))
	var out := pos + Vector2(dir) * each
	return Vector2(clampf(out.x, float(bounds[0]), float(bounds[2])), clampf(out.y, float(bounds[1]), float(bounds[3])))


static func tile(pos: Vector2) -> Vector2i:
	return Vector2i(roundi(pos.x), roundi(pos.y))


static func nearest(drawn_at: Dictionary, me_id: int, at: Vector2, radius: float) -> int:
	## The villager drawn nearest the body on screen, within `radius`, or -1. Drawing only: the engine knows
	## who is at the place, not where in it they stand, and checks every act itself.
	var best := -1
	var best_d := radius
	for id in drawn_at:
		if int(id) == me_id:
			continue
		var d: float = at.distance_to(drawn_at[id])
		if d < best_d or (d == best_d and (best < 0 or int(id) < best)):
			best = int(id)
			best_d = d
	return best


static func track_tile(p: Dictionary, f: float):
	## reach-1: where a walker the server lists in `seen` is, `f` 600ths into the tick, along the tiles it is
	## seen on (`track`: [f, x, y] each), or null outside the span it is in sight for (`in`). Drawing only.
	var span: Array = p["in"]
	if f < float(span[0]) or f > float(span[1]) + 1.0:
		return null
	var a: Array = p["track"][0]
	for b in p["track"]:
		if float(b[0]) >= f:
			var w := 0.0 if b[0] == a[0] else (f - float(a[0])) / (float(b[0]) - float(a[0]))
			return Vector2(a[1], a[2]).lerp(Vector2(b[1], b[2]), clampf(w, 0.0, 1.0))
		a = b
	return Vector2(a[1], a[2])


static func items(v: Dictionary, target: int, names: Dictionary, can_hear: bool) -> Array:
	## things-1: then what can be done with a thing in reach or in your hands (each also carries `thing`).
	## The E list: what can be done with the villager in reach (talk, then whatever the server offers that names
	## them: for someone walking by in sight, calling out to them), then what the server offers that names nobody. Each {label, action, target}; "talk" is the client's
	## own (it puts the Talk box on them). The offer is the server's: nothing is added to it here.
	var present := Words.near(v)  # at the body's place, or in reach of it (reach-1)
	var out := []
	var menu := LiveClient.menu(v)
	if target >= 0:
		var who := str(names.get(target, "them"))
		if can_hear:
			out.append({"label": "Talk to %s" % who, "action": "talk", "target": target})
		for it in menu:
			if int(it["target"]) == target and Words.shown(it, present) and not it["action"] in AFAR:
				out.append({"label": Words.action_label(it["action"], who), "action": it["action"], "target": target})
	for it in Words.thing_items(v, names):  # things-1: what lies in reach or is in your hands
		if int(it["target"]) < 0 or int(it["target"]) == target:
			out.append(it)
	for it in menu:
		if int(it["target"]) < 0:
			out.append({"label": Words.action_label(it["action"], ""), "action": it["action"], "target": -1})
	return out.slice(0, MAX_ITEMS)
