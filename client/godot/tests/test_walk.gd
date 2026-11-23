# Headless, against a running live server started paused, stepped to a waking hour (python -m asiimov.live --paused):
#   godot --headless --path client/godot --script res://tests/test_walk.gd -- ws://127.0.0.1:8765/ AGENT
# walk-1: the keys' arithmetic and the E list (walk_keys.gd), a walked body in the world model, and a live
# walk: an inhabitant sends `walk`, a god client steps the world, and the test checks the engine's `moved`
# answer, the view's tile and place, that a step and an act share a tick with the step first, and that the E
# list holds only what the server offers. Exits 1 on any failure or timeout.
extends SceneTree

const LiveClient := preload("res://scripts/live_client.gd")
const WorldModel := preload("res://scripts/world_model.gd")
const Keys := preload("res://scripts/walk_keys.gd")

var god := LiveClient.new()
var me := LiveClient.new()
var started := 0
var done := false
var phase := 0
var open_tile := Vector2i(0, 8)   # beside the plaza, in no place's rectangle
var market_tile := Vector2i.ZERO
var rest_id := ""
var walk_id := ""
var ticks := 0


func _init() -> void:
	_start()


func _start() -> void:
	# The keys: a diagonal splits its tiles between x and y, the map's edge stops it, a fast world is capped.
	var b := [-10, -10, 10, 10]
	if Keys.step(Vector2(0, 0), Vector2i(1, 0), 3.0, b) != Vector2(3, 0) \
			or Keys.step(Vector2(0, 0), Vector2i(1, -1), 3.0, b) != Vector2(1.5, -1.5) \
			or Keys.step(Vector2(9, 0), Vector2i(1, 0), 3.0, b) != Vector2(10, 0) \
			or Keys.step(Vector2(2, 2), Vector2i.ZERO, 3.0, b) != Vector2(2, 2) \
			or Keys.tile(Vector2(1.6, -1.4)) != Vector2i(2, -1) \
			or not is_equal_approx(Keys.speed(40, 0.05), 2.0) or Keys.speed(40, 2.0) != Keys.MAX_TILES_S:
		return _fail("the keys' arithmetic is off")
	var drawn := {4: Vector2(100, 100), 7: Vector2(130, 100), 3: Vector2(0, 0)}
	if Keys.nearest(drawn, 3, Vector2(104, 100), 40.0) != 4 or Keys.nearest(drawn, 3, Vector2(126, 100), 40.0) != 7 \
			or Keys.nearest(drawn, 3, Vector2(300, 300), 40.0) != -1 or Keys.nearest({3: Vector2.ZERO}, 3, Vector2.ZERO, 40.0) != -1:
		return _fail("the villager in reach is not the nearest one drawn")
	# The E list: talk first for someone who can hear, then what the server offers naming them (but not going
	# to find them: they're beside you), then the rest.
	var v := {"present": [{"id": 4, "name": "Ari", "kin": false}], "kin": [{"id": 9, "name": "Bel"}],
			"offer": {"work": [], "ask_food": [4, 9], "ask_kin": [9], "give_food": [4, 9], "seek": [4, 9], "rest": []}}
	var labels := []
	for it in Keys.items(v, 4, {4: "Ari"}, true):
		labels.append("%s/%s/%d" % [it["label"], it["action"], it["target"]])
	if labels.size() != 5 or labels[0] != "Talk to Ari/talk/4" or not labels[1].ends_with("/ask_food/4") \
			or not labels[2].ends_with("/give_food/4") or not labels[3].ends_with("/work/-1") or not labels[4].ends_with("/rest/-1"):
		return _fail("the E list for a villager reads %s" % [labels])
	var alone := Keys.items(v, -1, {}, false)
	if alone.size() != 2 or alone[0]["action"] != "work" or alone[1]["action"] != "rest":
		return _fail("the E list with nobody in reach reads %s" % [alone])
	# The world model: a walked body stands on its tile, keeps it while it stays at the place, and loses it
	# when a walk sets out.
	var wm := WorldModel.new()
	wm.load_world({"map": {"walk": 40, "places": {"home_0": {"anchor": [20, 20], "rect": [17, 17, 6, 6]},
			"plaza": {"anchor": [0, 0], "rect": [-5, -5, 10, 10]}}},
			"population": {"families": [{"home": "home_0"}], "agents": [{"id": 0, "family": 0}]}})
	wm.add_event({"tick": 5, "kind": "moved", "actor": 0, "data": {"from": [20, 20], "pos": [3, 4], "place": "plaza"}})
	wm.add_event({"tick": 6, "kind": "activity", "actor": 0, "data": {"activity": "rest", "place": "plaza", "arrive": 6}})
	wm.add_event({"tick": 8, "kind": "moved", "actor": 0, "data": {"from": [3, 4], "pos": [0, 8], "place": null}})
	wm.add_event({"tick": 9, "kind": "activity", "actor": 0, "data": {"activity": "home", "place": "home_0", "arrive": 10, "from": [0, 8]}})
	wm.add_event({"tick": 10, "kind": "arrive", "actor": 0, "data": {"place": "home_0"}})
	var home: Vector2i = wm.slot("home_0", 0)  # space-1: at home it stands on its slot, a tile of the home
	if wm.place_at(home) != "home_0" or wm.position(0, 5) != home or wm.position(0, 6) != Vector2i(3, 4) or wm.position(0, 8) != Vector2i(3, 4) \
			or wm.state_at(0, 7)[1] != "plaza" or wm.position(0, 9) != Vector2i(0, 8) or wm.state_at(0, 9)[1] != null \
			or wm.present("plaza", 9) != [] or wm.state_at(0, 10)[2]["from"] != [0, 8] or wm.position(0, 11) != home:
		return _fail("the world model doesn't place a walked body: %s" % [wm.moves[0]])
	var a := OS.get_cmdline_user_args()
	me.scope = "inhabitant"
	me.agent = int(a[1])
	god.failed.connect(func(r): _fail("god client failed: " + r))
	me.failed.connect(func(r): _fail("inhabitant client failed: " + r))
	me.welcomed.connect(_on_welcome)
	me.ticked.connect(_on_tick)
	started = Time.get_ticks_msec()
	if god.connect_to(a[0]) != OK or me.connect_to(a[0]) != OK:
		_fail("cannot connect to " + a[0])


func _on_welcome(m: Dictionary) -> void:
	var act: Dictionary = m["verbs"]["act"]
	if not act.has("to") or act["to"].size() != 4 or not "walk" in act["action"]:
		return _fail("the welcome doesn't offer walk with its tiles: %s" % [act.keys()])
	var stall = m["world"]["map"]["places"]["market"]["anchor"]
	market_tile = Vector2i(int(stall[0]) + 1, int(stall[1]) + 1)
	god.control("step")


func _moved(id: String) -> Dictionary:
	## The engine's `moved` for a walk intent, by the act event its ack names; {} if there is none.
	var ack: Dictionary = me.acks.get(id, {})
	if ack.get("status") != "applied":
		return {}
	for e in me.seen_events:
		if e["kind"] == "moved" and not e["parents"].is_empty() and int(e["parents"][0]) == int(ack["event"]):
			return e
	return {}


func _tile(p) -> Vector2i:
	return Vector2i(int(p[0]), int(p[1]))


func _on_tick(m: Dictionary) -> void:
	var v: Dictionary = m["view"]
	ticks += 1
	if v["asleep"]:
		return _fail("villager %d is asleep at tick %d: start the test at a waking hour" % [me.agent, int(v["tick"])])
	match phase:
		0:  # out into the open, a tick at a time: the engine walks at most `walk` tiles a tick
			if _tile(v["pos"]) == open_tile:
				if v["place"] != null or v["route"] != null or not v["present"].is_empty():
					return _fail("in the open it should be at no place, with nobody: %s" % [v["place"]])
				var mv := _moved(walk_id)
				if mv.is_empty() or _tile(mv["data"]["pos"]) != open_tile or mv["data"]["place"] != null:
					return _fail("the last walk has no `moved` answer at the tile: %s" % [mv])
				phase = 1
			else:
				walk_id = me.walk(open_tile)
		1:  # a step shown on screen and not yet sent goes out before the act, and both land in one tick
			me.walk_want = market_tile
			rest_id = me.act("rest")
			if me.walk_sent != market_tile or me.walk_want != null or me.walks.size() != 1:
				return _fail("the act didn't send the waiting step first")
			walk_id = me.walks.keys()[0]
			phase = 2
		2:
			var mv := _moved(walk_id)
			var ack: Dictionary = me.acks.get(rest_id, {})
			if mv.is_empty() or ack.get("status") != "applied":
				return _fail("the step and the act weren't both applied: %s, %s" % [mv, ack])
			if int(mv["tick"]) != int(ack["tick"]) or int(mv["parents"][0]) >= int(ack["event"]):
				return _fail("the step should land in the act's tick, before it")
			var receipt := {}
			for e in me.seen_events:
				if e["kind"] == "receipt" and not e["parents"].is_empty() and int(e["parents"][0]) == int(ack["event"]):
					receipt = e
			if receipt.is_empty() or receipt["data"].get("why") == "one act per tick":
				return _fail("a walk must not use up the tick's one act: %s" % [receipt])
			phase = 3
			walk_id = me.walk(market_tile)
		3:  # into the market: the engine says so, and the E list is the server's offer for where it stands
			if _tile(v["pos"]) != market_tile:
				walk_id = me.walk(market_tile)
			elif v["place"] != "market" or not v.has("market_food"):
				return _fail("on a market tile the engine should say it is at the market: %s" % [v["place"]])
			else:
				var offered := 0
				for it in Keys.items(v, -1, {}, false):
					if not v["offer"].has(it["action"]) or it["target"] != -1:
						return _fail("the E list holds %s, which the server didn't offer" % [it])
					offered += 1
				if offered == 0:
					return _fail("the E list is empty at the market")
				for p in v["present"]:
					var list := Keys.items(v, int(p["id"]), {int(p["id"]): p["name"]}, not p.get("asleep", false))
					for it in list:
						if it["action"] != "talk" and not (v["offer"].has(it["action"])):
							return _fail("the E list for %s holds %s, which the server didn't offer" % [p["name"], it])
				print("walk ok: %d ticks, out to %s, in to the market at %s, a step and an act in one tick" % [ticks, open_tile, market_tile])
				done = true
				quit(0)
				return
	if ticks > 40:
		return _fail("still in phase %d after %d ticks" % [phase, ticks])
	god.control("step")


func _process(_delta: float) -> bool:
	if done:
		return true
	god.poll()
	me.poll()
	if Time.get_ticks_msec() - started > 60000:
		_fail("timed out in phase %d after %d ticks" % [phase, ticks])
	return done


func _fail(why: String) -> void:
	if done:
		return
	done = true
	printerr("FAIL: " + why)
	quit(1)
