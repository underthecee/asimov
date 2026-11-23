# Where each villager is, rebuilt from the engine's events alone. Render-only: nothing here is sent back.
#
# The engine changes a villager's place or route in exactly two events (sim.py _set_activity and _arrive):
#   activity  {activity, place, arrive[, from]}  a walk when arrive > tick (from: where it starts; absent when
#             the villager keeps its current walk), else already there
#   arrive    {place}                            the walk ends
#   moved     {from, pos, place}                 walk-1: the player walked its body to tile `pos`; `place` is the
#             place holding that tile, or null in the open. It stands on that tile until a walk sets out
# wake and sleep only change the activity. Position math is worldmap.position, in integer tiles, so at a
# tick boundary this matches the engine exactly; tests/test_positions.gd checks it against Sim._pos.
extends RefCounted

var places := {}        # name -> {anchor: [x, y], rect: [x, y, w, h]}
var walk := 40          # tiles per tick, from genesis
var agents := []        # population agents, as in genesis
var families := []
var ticks_per_day := 144
var moves := {}         # agent id -> [[tick, place, route, activity, at], ...]: state after that tick's events
						# (at: the tile a walked body stands on, [x, y], or null at a place's anchor or on a route)
var events := []        # every event received, in id order
var last_tick := -1     # the newest tick with events applied
var holdings := {}      # agent id -> [[tick, food, coin], ...]: what it holds after that tick ran. Only a playback
						# pack has these (tools/make_playback.py, from the engine's replay): a log alone doesn't
						# carry them, and nothing here works them out
var engine := ""        # the engine that wrote the world, from genesis


func load_world(world: Dictionary) -> void:
	## genesis.data, or welcome.world. Resets everything.
	places = world["map"]["places"]
	walk = int(world["map"]["walk"])
	families = world["population"]["families"]
	agents = world["population"]["agents"]
	ticks_per_day = int(world.get("ticks_per_day", 144))
	moves.clear()
	holdings.clear()
	for a in agents:
		moves[int(a["id"])] = [[-1, families[int(a["family"])]["home"], null, "sleep", null]]


func add_event(e: Dictionary) -> void:
	var t := int(e["tick"])
	var d: Dictionary = e["data"]
	if e["kind"] == "genesis":
		events.clear()
		load_world(d)
		engine = str(d.get("engine", ""))
	events.append(e)
	last_tick = maxi(last_tick, t)
	match e["kind"]:
		"activity":
			var aid := int(e["actor"])
			var arrive := int(d.get("arrive", t))
			if arrive > t:
				var route = _last(aid)[2]  # no "from": still on the same walk
				if d.has("from"):
					route = {"from": [int(d["from"][0]), int(d["from"][1])], "to": d["place"],
							"depart": t, "arrive": arrive}
				_put(aid, t, null, route, d["activity"])
			else:  # already there: a walked body keeps its tile within the place
				var m = _last(aid)
				_put(aid, t, d["place"], null, d["activity"], m[4] if m[1] == d["place"] else null)
		"arrive":
			var aid := int(e["actor"])
			_put(aid, t, d["place"], null, _last(aid)[3])
		"moved":
			_put(int(e["actor"]), t, d["place"], null, "visit", [int(d["pos"][0]), int(d["pos"][1])])
		"wake":
			var aid := int(e["actor"])
			var m = _last(aid)
			_put(aid, t, m[1], m[2], "home", m[4])
		"sleep":
			var aid := int(e["actor"])
			var m = _last(aid)
			_put(aid, t, m[1], m[2], "sleep", m[4])


func _last(aid: int) -> Array:
	return moves[aid][-1]


func _put(aid: int, t: int, place, route, activity, at = null) -> void:
	var m: Array = moves[aid]
	if m[-1][0] == t:
		m.pop_back()  # only the last change in a tick is visible between ticks
	m.append([t, place, route, activity, at])


func load_file(path: String) -> String:
	## A recorded world: a log (events.jsonl) or a playback pack (.json or .json.gz, tools/make_playback.py).
	## Returns "" or what went wrong. A pack's holdings are the engine's own, one row per change.
	if not FileAccess.file_exists(path):
		return "no such file: " + path
	var bytes := FileAccess.get_file_as_bytes(path)
	if path.ends_with(".gz"):
		bytes = bytes.decompress_dynamic(-1, FileAccess.COMPRESSION_GZIP)
	var text := bytes.get_string_from_utf8()
	if path.ends_with(".jsonl"):
		for line in text.split("\n", false):
			var e = JSON.parse_string(line)
			if typeof(e) == TYPE_DICTIONARY:  # a torn last line is skipped
				add_event(e)
		return "" if not events.is_empty() else "no events in " + path
	var pack = JSON.parse_string(text)
	if typeof(pack) != TYPE_DICTIONARY or int(pack.get("playback", 0)) != 1:
		return "not a playback pack this client reads: " + path
	for e in pack["events"]:
		add_event(e)
	for h in pack["holdings"]:
		holdings.get_or_add(int(h[1]), []).append([int(h[0]), int(h[2]), int(h[3])])
	last_tick = int(pack["last_tick"])
	return ""


static func _at(rows: Array, view_tick: int) -> Array:
	## The last row with tick <= view_tick - 1: the state at view tick N, after tick N-1 ran and before tick N.
	var lo := 0
	var hi := rows.size() - 1
	while lo < hi:
		var mid := (lo + hi + 1) / 2
		if rows[mid][0] <= view_tick - 1:
			lo = mid
		else:
			hi = mid - 1
	return rows[lo]


func state_at(aid: int, view_tick: int) -> Array:
	## [tick, place, route, activity, at] at view tick N.
	return _at(moves[aid], view_tick)


func holding_at(aid: int, view_tick: int) -> Array:
	## [tick, food, coin] at view tick N, or [] when the world came without holdings (a bare log, a live world).
	return _at(holdings[aid], view_tick) if holdings.has(aid) else []


func present(place: String, view_tick: int) -> Array:
	## Ids of everyone at a place at view tick N, in id order. Walkers are nowhere, as in the engine.
	var out := []
	for a in agents:
		if state_at(int(a["id"]), view_tick)[1] == place:
			out.append(int(a["id"]))
	return out


func anchor(place: String) -> Vector2i:
	var a = places[place]["anchor"]
	return Vector2i(int(a[0]), int(a[1]))


func position(aid: int, view_tick: int) -> Vector2i:
	## The engine's position at a tick boundary (worldmap.position): WALK tiles a tick along the L path, x first.
	var s := state_at(aid, view_tick)
	if s[2] == null:
		return Vector2i(int(s[4][0]), int(s[4][1])) if s[4] != null else anchor(s[1])
	var r: Dictionary = s[2]
	var to := anchor(r["to"])
	var x := int(r["from"][0])
	var y := int(r["from"][1])
	var left := walk * maxi(0, view_tick - int(r["depart"]))
	var dx := mini(left, absi(to.x - x))
	x += dx if to.x >= x else -dx
	var dy := mini(left - dx, absi(to.y - y))
	y += dy if to.y >= y else -dy
	return Vector2i(x, y)


func display_position(aid: int, t: float) -> Vector2:
	## Between boundaries, for drawing only: linear between the two boundary positions.
	var t0 := int(floor(t))
	var a := Vector2(position(aid, t0))
	var b := Vector2(position(aid, t0 + 1))
	return a.lerp(b, t - t0)
