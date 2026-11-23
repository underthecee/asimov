# Headless, against a running live server started paused with --things and stepped to a waking hour:
#   godot --headless --path client/godot --script res://tests/test_things.gd -- ws://127.0.0.1:8765/ AGENT
# things-1: the words for things (inhabit_ui.gd, from the engine's table), the E list and the menu with things
# in them, and a live round: the body walks to the woods, picks up a flower from the E list, sees it in its
# hands, puts it down, and picks berries into food. Everything it does is the server's offer. Exits 1 on any
# failure or timeout.
extends SceneTree

const LiveClient := preload("res://scripts/live_client.gd")
const Keys := preload("res://scripts/walk_keys.gd")
const Words := preload("res://scripts/inhabit_ui.gd")

var god := LiveClient.new()
var me := LiveClient.new()
var started := 0
var done := false
var phase := 0
var ticks := 0
var woods := Vector2i.ZERO
var flower := -1
var bush := -1
var food := 0
var act_id := ""


func _init() -> void:
	_start()


func _start() -> void:
	# The words: every thing verb's label comes from the thing's kind, and the lists hold only what is offered.
	var v := {"present": [{"id": 4, "name": "Ari", "kin": false}], "kin": [], "food": 2, "coin": 3, "hunger": 100,
			"tick": 80, "asleep": false, "place": "woods", "activity": "explore", "route": null,
			"things": [{"id": 7, "kind": "flower", "pos": [1, 1], "reach": true}, {"id": 8, "kind": "berries", "pos": [1, 2], "reach": true},
				{"id": 0, "kind": "stall", "pos": [14, 0]}],
			"carried": [{"id": 5, "kind": "flower"}, {"id": 6, "kind": "flower"}],
			"offer": {"take": [7], "use": [8], "drop": [5, 6], "hand": [4], "rest": [], "give_food": [4]}}
	var labels := []
	for it in Words.thing_items(v, {4: "Ari"}):
		labels.append("%s/%s/%d/%d" % [it["label"], it["action"], it["target"], it["thing"]])
	if labels != ["Pick berries/use/-1/8", "Pick up the flower/take/-1/7", "Put down the flower/drop/-1/5",
			"Give the flower to Ari/hand/4/5"]:
		return _fail("the thing items read %s" % [labels])
	for it in LiveClient.menu(v):
		if it["action"] in Words.THING_ACTS:
			return _fail("the menu of person buttons holds a thing verb: %s" % [it])
	var e := []
	for it in Keys.items(v, 4, {4: "Ari"}, true):
		e.append(str(it["action"]))
	if e != ["talk", "give_food", "use", "take", "drop", "hand", "rest"]:
		return _fail("the E list beside Ari reads %s" % [e])
	e = []
	for it in Keys.items(v, -1, {}, false):
		e.append(str(it["action"]))
	if e != ["use", "take", "drop", "rest"]:  # nobody in reach: nothing is handed over
		return _fail("the E list with nobody in reach reads %s" % [e])
	var who := {"id": 3, "name": "Me", "home": "home_0"}
	var took := Words.describe({"kind": "receipt", "actor": 3, "origin": "rule", "tick": 80, "parents": [1, 2],
			"data": {"action": "take", "ok": true, "why": "took", "thing": 7, "kind": "flower"}}, who, {4: "Ari"})
	var full := Words.describe({"kind": "receipt", "actor": 3, "origin": "rule", "tick": 80, "parents": [1],
			"data": {"action": "take", "ok": false, "why": "hands full", "thing": 7, "kind": "flower"}}, who, {4: "Ari"})
	var gave := Words.describe({"kind": "receipt", "actor": 3, "origin": "rule", "tick": 80, "parents": [1, 2],
			"data": {"action": "hand", "ok": true, "why": "handed", "thing": 5, "kind": "flower", "to": 4}}, who, {4: "Ari"})
	var picked := Words.describe({"kind": "receipt", "actor": 3, "origin": "rule", "tick": 80, "parents": [1, 2],
			"data": {"action": "use", "ok": true, "why": "picked", "thing": 8, "kind": "berries", "food": 1}}, who, {4: "Ari"})
	if not "You pick up the flower." in took or not "your hands are full" in full or not "You give the flower to Ari." in gave \
			or not "1 more food" in picked:
		return _fail("a thing's receipts read: %s | %s | %s | %s" % [took, full, gave, picked])
	if not "In your hands: a flower, a flower." in Words.status(v, who, 144):
		return _fail("the status doesn't say what is carried: %s" % Words.status(v, who, 144))
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
	for verb in Words.THING_ACTS:
		if not verb in act["action"]:
			return _fail("the welcome doesn't offer %s" % verb)
	var an = m["world"]["map"]["places"]["woods"]["anchor"]
	woods = Vector2i(int(an[0]), int(an[1]))
	god.control("step")


func _tile(p) -> Vector2i:
	return Vector2i(int(p[0]), int(p[1]))


func _nearest(v: Dictionary, kind: String) -> Dictionary:
	var best := {}
	for t in v.get("things", []):
		if t["kind"] == kind and (best.is_empty() or _far(v, t) < _far(v, best) or (_far(v, t) == _far(v, best) and int(t["id"]) < int(best["id"]))):
			best = t
	return best


func _far(v: Dictionary, t: Dictionary) -> int:
	return absi(int(t["pos"][0]) - int(v["pos"][0])) + absi(int(t["pos"][1]) - int(v["pos"][1]))


func _item(v: Dictionary, action: String, thing: int) -> Dictionary:
	## The E list's entry for this act on this thing, or {}: the test only presses what the list offers.
	for it in Keys.items(v, -1, {}, false):
		if it["action"] == action and int(it.get("thing", -1)) == thing:
			return it
	return {}


func _receipt(id: String) -> Dictionary:
	var ack: Dictionary = me.acks.get(id, {})
	if ack.get("status") != "applied":
		return {}
	for e in me.seen_events:
		if e["kind"] == "receipt" and not e["parents"].is_empty() and int(e["parents"][0]) == int(ack["event"]):
			return e
	return {}


func _press(v: Dictionary, action: String, thing: int) -> bool:
	var it := _item(v, action, thing)
	if it.is_empty():
		_fail("the E list doesn't offer %s on thing %d: offer %s" % [action, thing, v["offer"]])
		return false
	act_id = me.act(action, -1, "", "", "", -1, thing)
	return true


func _on_tick(m: Dictionary) -> void:
	var v: Dictionary = m["view"]
	ticks += 1
	if v["asleep"]:
		return _fail("villager %d is asleep at tick %d: start the test at a waking hour" % [me.agent, int(v["tick"])])
	if not v.has("things") or not v.has("carried"):
		return _fail("the view has no things: was the world started with --things?")
	match phase:
		0:  # anywhere: the fixtures are on the map, and what grows in the woods isn't seen from elsewhere
			var kinds := {}
			for t in v["things"]:
				kinds[t["kind"]] = int(kinds.get(t["kind"], 0)) + 1
			if int(kinds.get("stall", 0)) != 1 or int(kinds.get("bench", 0)) < 3 or int(kinds.get("bed", 0)) < 1:
				return _fail("the fixtures aren't all in the view: %s" % [kinds])
			if v["place"] != "woods" and (kinds.has("flower") or kinds.has("berries")):
				return _fail("what lies in the woods shows from %s" % [v["place"]])
			phase = 1
			me.walk(woods)
		1:  # in the woods: walk up to the nearest flower and pick it up from the E list
			var fl := _nearest(v, "flower")
			if v["place"] != "woods" or fl.is_empty():
				me.walk(woods)
			elif not fl.get("reach", false):
				me.walk(_tile(fl["pos"]))
			else:
				flower = int(fl["id"])
				if not _press(v, "take", flower):
					return
				phase = 2
		2:  # in your hands, off the ground, and the list now offers to put it down
			var r := _receipt(act_id)
			if r.is_empty() or r["data"].get("why") != "took":
				return _fail("the flower wasn't taken: %s" % [r])
			if v["carried"] != [{"id": float(flower), "kind": "flower"}] and v["carried"] != [{"id": flower, "kind": "flower"}]:
				return _fail("the view doesn't show the flower carried: %s" % [v["carried"]])
			for t in v["things"]:
				if int(t["id"]) == flower:
					return _fail("a carried flower still lies on the ground")
			if not "In your hands: a flower." in Words.status(v, me.me, me.ticks_per_day()):
				return _fail("the status doesn't say the flower is carried")
			if not _press(v, "drop", flower):
				return
			phase = 3
		3:  # put down where the body stands, then on to the nearest berries
			var r := _receipt(act_id)
			if r.is_empty() or r["data"].get("why") != "dropped" or not v["carried"].is_empty():
				return _fail("the flower wasn't put down: %s, %s" % [r, v["carried"]])
			var lies := false
			for t in v["things"]:
				lies = lies or (int(t["id"]) == flower and _tile(t["pos"]) == _tile(v["pos"]))
			if not lies:
				return _fail("the flower doesn't lie where the body stands")
			phase = 4
			me.walk(_tile(_nearest(v, "berries")["pos"]))
		4:
			var b := _nearest(v, "berries")
			if b.is_empty():
				return _fail("no berries in the woods")
			if not b.get("reach", false):
				me.walk(_tile(b["pos"]))
			else:
				bush = int(b["id"])
				food = int(v["food"])
				if not _press(v, "use", bush):
					return
				phase = 5
		5:
			var r := _receipt(act_id)
			if r.is_empty() or r["data"].get("why") != "picked" or int(v["food"]) != food + 1:
				return _fail("the berries weren't picked into food: %s, food %d after %d" % [r, int(v["food"]), food])
			for t in v["things"]:
				if int(t["id"]) == bush:
					return _fail("picked berries are still there")
			print("things ok: %d ticks, a flower taken and put down, berries picked into food (%d to %d)" % [ticks, food, int(v["food"])])
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
