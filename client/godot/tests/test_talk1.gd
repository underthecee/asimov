# Headless, against a running live server started paused (python -m asiimov.live --paused), talk-1 or later:
#   godot --headless --path client/godot --script res://tests/test_talk1.gd -- ws://127.0.0.1:8765/ AGENT
# talk-1: the player says a line with the "ask for food" chip. The `said` event must carry the move, the listener's
# `reply` must carry a yes or no, and a yes must come with a `deal` to this body that reads as words.
extends SceneTree

const LiveClient := preload("res://scripts/live_client.gd")
const Words := preload("res://scripts/inhabit_ui.gd")

const LINE := "Could you spare me something to eat?"

var god := LiveClient.new()
var me := LiveClient.new()
var act_id := ""
var act_event := -1
var to := -1
var sent_tick := -1
var ticks := 0
var started := 0
var done := false


func _init() -> void:
	var names := {4: "Ari"}
	var me_d := {"id": 3, "name": "Me"}
	var asked := Words.describe({"kind": "said", "actor": 3, "origin": "rule",
			"data": {"with": 4, "text": "Food?", "move": "wants_to_buy_food", "price": 5}}, me_d, names)
	var gave := Words.describe({"kind": "deal", "actor": 4, "origin": "rule",
			"data": {"move": "asks_food", "from": 4, "to": 3, "food": 1, "ok": true}}, me_d, names)
	var sold := Words.describe({"kind": "deal", "actor": 4, "origin": "rule",
			"data": {"move": "wants_to_buy_food", "from": 4, "to": 3, "food": 1, "coin": 5, "ok": true}}, me_d, names)
	var failed := Words.describe({"kind": "deal", "actor": 4, "origin": "rule",
			"data": {"move": "asks_food", "from": 4, "to": 3, "food": 1, "ok": false, "why": "nothing to spare"}}, me_d, names)
	# goods-1: a supply buy reads as a sale of the good, whichever side you are on, and a made unit as made
	var from_me := Words.describe({"kind": "deal", "actor": 4, "origin": "rule",
			"data": {"move": "trade", "from": 3, "to": 4, "tools": 2, "coin": 2, "ok": true}}, me_d, names)
	var others := Words.describe({"kind": "deal", "actor": 4, "origin": "rule",
			"data": {"move": "trade", "from": 9, "to": 4, "seed": 1, "coin": 1, "ok": true}}, me_d, {4: "Ari", 9: "Sil"})
	var made := Words.describe({"kind": "made", "actor": 3, "origin": "rule", "data": {"tools": 1}}, me_d, names)
	if not ("Ari bought 2 tools from you for 2 coin." in from_me) or others != "Ari bought 1 seed and feed from Sil for 1 coin." \
			or made != "You make 1 tools.":
		printerr("goods-1 doesn't read right: %s | %s | %s" % [from_me, others, made])
		quit(1)
		return
	if not ("offering 5 coin for 1 food" in asked) or not ("Ari gave you 1 food." in gave) \
			or not ("You bought 1 food from Ari for 5 coin." in sold) or not ("nothing to spare" in failed and "Ari said yes" in failed):
		printerr("talk-1 doesn't read right: %s | %s | %s | %s" % [asked, gave, sold, failed])
		quit(1)
		return
	# The chip hint (Steering 02:22Z): every acceptance ask and buy line, and the headline asks, get it with no chip;
	# small talk, complaints and any line with a chip picked don't.
	var asks := ["can i have some food?", "I'm hungry. Could you spare a little food?", "Could you give me something to eat?",
			"I haven't eaten since yesterday. Can you help me out with a meal?", "hey, give me some of your food",
			"Any chance of a bite to eat from you?", "Spare a meal for a neighbour?", "I'm starving. Share your food with me, please.",
			"i really need coins or food, and isrota directed me to you; anything you can do..?",
			"Could you give me a loaf of food? I'm a guard, I can pay later."]
	var buys := ["I'll pay you for some food. Will you sell me one?", "Can I buy a meal off you? I have coin.",
			"How about I give you coin and you give me one food?"]
	var talk := ["Hello there.", "How was the harvest?", "no. i dont think i received your food.", "Nice weather today?"]
	for t in asks:
		if Words.ask_hint(t, 0) != Words.HINT_ASK_TEXT and Words.ask_hint(t, 0) != Words.HINT_BUY_TEXT:
			_fail_now("no chip hint for an ask: " + t)
			return
	for t in buys:
		if Words.ask_hint(t, 0) != Words.HINT_BUY_TEXT:
			_fail_now("no buy hint for: " + t)
			return
	for t in talk:
		if Words.ask_hint(t, 0) != "":
			_fail_now("a hint for small talk: " + t)
			return
	if Words.ask_hint(asks[0], 1) != "" or Words.ask_hint(buys[0], 2) != "":
		_fail_now("a hint with a chip picked")
		return
	var a := OS.get_cmdline_user_args()
	me.scope = "inhabitant"
	me.agent = int(a[1])
	god.failed.connect(func(x): _fail("god client failed: " + x))
	me.failed.connect(func(x): _fail("inhabitant client failed: " + x))
	me.welcomed.connect(func(_m): god.control("step"))
	me.ticked.connect(_on_tick)
	me.acked.connect(_on_ack)
	god.connect_to(a[0])
	me.connect_to(a[0])
	started = Time.get_ticks_msec()


func _on_ack(m: Dictionary) -> void:
	if m["id"] == act_id:
		if m["status"] != "applied":
			_fail("say rejected: %s" % [m])
		act_event = int(m["event"])


func _on_tick(m: Dictionary) -> void:
	ticks += 1
	var v: Dictionary = m["view"]
	if act_id == "" and not v["asleep"] and v["place"] != null:
		var awake: Array = v["present"].filter(func(p): return not p["asleep"])
		if not awake.is_empty():
			to = int(awake[0]["id"])
			act_id = me.act("say", to, "", LINE, "asks_food")
			sent_tick = int(v["tick"])
	if act_event >= 0:
		var rec: Array = me.seen_events.filter(func(e): return e["kind"] == "receipt" and not e["parents"].is_empty() \
				and int(e["parents"][0]) == act_event)
		if not rec.is_empty() and (not rec[0]["data"]["ok"] or rec[0]["data"]["why"] != "said"):
			_fail("say got %s" % [rec[0]["data"]])
		var said: Array = me.seen_events.filter(func(e): return e["kind"] == "said" and int(e["actor"]) == me.agent)
		var reply: Array = me.seen_events.filter(func(e): return e["kind"] == "reply" and int(e["actor"]) == to \
				and int(e["data"]["with"]) == me.agent)
		if not said.is_empty() and (str(said[0]["data"]["text"]) != LINE or str(said[0]["data"].get("move", "")) != "asks_food"):
			_fail("said carries %s" % [said[0]["data"]])
		if not rec.is_empty() and not said.is_empty() and not reply.is_empty():
			var answer := str(reply[0]["data"].get("answer", ""))
			if not answer in ["yes", "no"]:
				_fail("the reply carries no yes or no: %s" % [reply[0]["data"]])
			var deal: Array = me.seen_events.filter(func(e): return e["kind"] == "deal" and int(e["data"]["to"]) == me.agent)
			if answer == "yes" and deal.is_empty():
				_fail("a yes with no deal: %s" % [reply[0]])
			if answer == "no" and not deal.is_empty():
				_fail("a no with a deal: %s" % [deal[0]])
			var line := Words.describe(deal[0], me.me, {to: "them"}) if not deal.is_empty() else "(no deal)"
			print("asked %d for food at tick %d, answered %s by tick %d: %s" % [to, sent_tick, answer, int(v["tick"]), line])
			done = true
			quit(0)
			return
		if int(v["tick"]) - sent_tick > 4:
			_fail("no reply within 4 ticks: receipt %s, said %s" % [rec, said])
	if ticks > 200:
		_fail("nobody awake to talk to in 200 ticks")
	god.control("step")


func _fail(why: String) -> void:
	if done:
		return
	done = true
	printerr(why)
	quit(1)


func _process(_delta: float) -> bool:
	if done:
		return true
	god.poll()
	me.poll()
	if Time.get_ticks_msec() - started > 60000:
		_fail("timed out after %d ticks" % ticks)
	return false


func _fail_now(msg: String) -> void:
	printerr("talk-1: " + msg)
	quit(1)
