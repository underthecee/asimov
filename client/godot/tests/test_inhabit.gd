# Headless, against a running live server started paused (python -m asiimov.live --paused):
#   godot --headless --path client/godot --script res://tests/test_inhabit.gd -- ws://127.0.0.1:8765/ AGENT [ticks]
# Bar 6 (plan/player-body-spec.md): one client inhabits villager AGENT, a god client steps the world. Every few
# ticks the inhabitant presses a button from its menu, which comes from the server's offer, and the test
# checks each applied act's receipt arrives in the inhabitant's own stream, citing the act event the ack
# named. It also checks the view's keys against the spec's whitelist, and that what it witnessed reads as words. Exits 1 on any failure or timeout.
extends SceneTree

const LiveClient := preload("res://scripts/live_client.gd")
const WorldModel := preload("res://scripts/world_model.gd")
const Words := preload("res://scripts/inhabit_ui.gd")
const Plan := preload("res://scripts/plan_queue.gd")
const VIEW_KEYS := ["tick", "place", "route", "pos", "activity", "asleep", "hunger", "food", "coin", "plan",
		"recent", "present", "kin", "market_food", "occasion", "offer", "cooldown_until", "food_price", "intent", "asks", "seen"]

var god := LiveClient.new()
var me := LiveClient.new()
var want_ticks := 200
var seen := 0
var pressed := []       # intent ids sent
var buttons := 0
var started := 0
var done := false
var heard_n := 0        # `heard` messages: words spoken near the villager (readable-world E2/C3)


func _init() -> void:
	# Which buttons are left off: only the ones the engine can only refuse (sim.INSTANT_STEPS elsewhere,
	# ask_kin of non-family). Family elsewhere keeps the gifts, seek and avoid: a gift walks there.
	var elsewhere := {}
	var here_kin := {7: true}
	var here_other := {8: false}
	for c in [["give_food", 5, elsewhere, true], ["give_coin", 5, elsewhere, true], ["avoid", 5, elsewhere, true],
			["seek", 5, elsewhere, true], ["ask_food", 5, elsewhere, false], ["buy_from", 5, elsewhere, false],
			["steal", 5, elsewhere, false], ["ask_kin", 5, elsewhere, false], ["ask_kin", 7, here_kin, true],
			["ask_kin", 8, here_other, false], ["ask_food", 8, here_other, true], ["work", -1, elsewhere, true]]:
		if Words.shown({"action": c[0], "target": c[1]}, c[2]) != c[3]:
			printerr("button %s for %d shown should be %s" % [c[0], c[1], c[3]])
			quit(1)
			return
	# An ask waiting on the villager's answer (player-ask-1) reads as one, with the time left, and the
	# answer buttons are shown wherever the asker is.
	var waiting := Words.asks({"tick": 100, "asks": [{"from": 4, "name": "Ari", "action": "ask_food", "until": 104}]}, {}, 20.0)
	var said := Words.describe({"kind": "ask", "actor": 4, "origin": "rule", "data": {"action": "buy_from", "to": 3}},
			{"id": 3, "name": "Me"}, {4: "Ari"})
	if not ("Ari asked you for food" in waiting and "4 ticks" in waiting and "80s" in waiting) \
			or said != "Ari wanted to buy food from you. Give, or not now?" \
			or not Words.shown({"action": "agree", "target": 4}, {}) or not Words.shown({"action": "decline", "target": 4}, {}):
		printerr("an ask doesn't read right: %s | %s" % [waiting, said])
		quit(1)
		return
	# Reasons as words (Steering, 2026-11-10): an act aimed at you carries what its villager said, quoted
	# under the line and above the yes and no; a bracket in a mind's words is text, never markup.
	var why := "I need food and my family [b]should[/b] support me"
	var w2 := Words.asks({"tick": 100, "asks": [{"from": 4, "name": "Ari", "action": "ask_kin", "until": 104, "says": why}]}, {}, 20.0)
	var g := Words.describe({"kind": "receipt", "actor": 4, "origin": "rule",
			"data": {"action": "give_food", "ok": true, "why": "gave", "to": 3, "says": "return the favour"}},
			{"id": 3, "name": "Me"}, {4: "Ari"})
	var plain := Words.describe({"kind": "meet", "actor": 4, "origin": "rule", "data": {"with": 3}}, {"id": 3, "name": "Me"}, {4: "Ari"})
	if not ("[i]\"I need food and my family [lb]b]should[lb]/b] support me\"[/i]\nGive, or not now? You have" in w2) \
			or not g.ends_with("\n    [i]\"return the favour\"[/i]") or "\"" in plain:
		printerr("a villager's words don't read right: %s | %s | %s" % [w2, g, plain])
		quit(1)
		return
	# An ask nobody answered says the villager's habits answered it; one the player answered doesn't.
	var rolled := Words.describe({"kind": "receipt", "actor": 4, "origin": "chance",
			"data": {"action": "ask_kin", "ok": true, "why": "kin gave food", "to": 3}}, {"id": 3, "name": "Me"}, {4: "Ari"})
	var pressed := Words.describe({"kind": "receipt", "actor": 4, "origin": "rule",
			"data": {"action": "ask_kin", "ok": true, "why": "kin gave food", "to": 3, "answered": 1}}, {"id": 3, "name": "Me"}, {4: "Ari"})
	if rolled != "Ari asked you for food: you didn't answer, so your habits did: you gave them food." \
			or pressed != "Ari asked you for food: you gave them food.":
		printerr("an answer doesn't read right: %s | %s" % [rolled, pressed])
		quit(1)
		return
	# Your own act's outcome reads by name, refusals included (readable-world C1), and a walk says when.
	var me3 := {"id": 3, "name": "Me", "home": "home_1"}
	var mine := func(d: Dictionary) -> String:
		return Words.describe({"kind": "receipt", "actor": 3, "origin": "rule", "tick": 50, "data": d}, me3, {4: "Ari"})
	var lines := [mine.call({"action": "ask_food", "ok": false, "why": "refused", "to": 4}),
			mine.call({"action": "ask_kin", "ok": false, "why": "kin has nothing to spare", "to": 4}),
			mine.call({"action": "ask_food", "ok": true, "why": "gave", "to": 4}),
			mine.call({"action": "steal", "ok": false, "why": "caught", "victim": 4}),
			mine.call({"action": "give_coin", "ok": true, "why": "gave", "to": 4, "coin": 3}),
			mine.call({"action": "go_to", "ok": true, "why": "heading there", "place": "woods", "arrive": 53}),
			mine.call({"action": "go_to", "ok": true, "why": "already here", "place": "home_1", "arrive": 50}),
			mine.call({"action": "go_to", "ok": true, "why": "arrived", "place": "woods"}),
			mine.call({"action": "ask_food", "ok": false, "why": "asleep"}),
			mine.call({"action": "steal", "ok": true, "why": "stolen", "victim": 4}),
			mine.call({"action": "give_coin", "ok": false, "why": "no coin to give", "to": 4})]
	var want := ["You asked Ari for food. Ari said no.", "You asked Ari for food. Ari had nothing to spare.",
			"You asked Ari for food. Ari gave you some.", "Ari caught you stealing.", "You gave 3 coin to Ari.",
			"You set off for the woods; you'll be there in about 30 minutes.", "You're already at your home, so you stay.",
			"", "Didn't work: ask for food, because you were asleep.", "You stole food from Ari and got away with it.",
			"Didn't work: give a coin (Ari), because you have no coin to give."]
	if lines != want:
		printerr("your own outcomes don't read right:\n%s" % "\n".join(lines))
		quit(1)
		return
	# Words spoken near you (C3): quoted, named, markup-safe, and what's said to you in the ask colour.
	var h1 := Words.heard({"tick": 61, "kind": "talk", "to_player": false, "lines": [{"who": 4, "name": "Ari", "text": "Rain [b]soon[/b]?"},
			{"who": 5, "name": "Bo", "text": "Hope so."}]}, 144)
	var h2 := Words.heard({"tick": 61, "kind": "refusal", "to_player": true, "lines": [{"who": 4, "name": "Ari", "text": "Not today."}]}, 144)
	if not ("10:10" in h1 and "[b]Ari[/b]: \u201cRain [lb]b]soon[lb]/b]?\u201d" in h1 and "[b]Bo[/b]" in h1) \
			or not ("#f0c060" in h2 and "Not today." in h2) or "#f0c060" in h1:
		printerr("words said near you don't read right: %s | %s" % [h1, h2])
		quit(1)
		return
	# pt8: every line says whom it is to, and thinking aloud is set apart, in grey.
	var h3 := Words.heard({"tick": 61, "kind": "talk", "to_player": false, "lines": [
			{"who": 4, "name": "Ari", "text": "Morning.", "to": 5, "to_name": "Bo", "to_me": false},
			{"who": 5, "name": "Bo", "text": "Long day.", "to": "self", "to_name": null, "to_me": false},
			{"who": 4, "name": "Ari", "text": "You too?", "to": 3, "to_name": "Mimi", "to_me": true}]}, 144)
	if not ("[b]Ari[/b] \u2192 Bo: \u201cMorning.\u201d" in h3 and "[i]Bo, to themself: Long day.[/i]" in h3
			and "[b]Ari[/b] \u2192 you:" in h3):
		printerr("addressees don't read right: %s" % h3)
		quit(1)
		return
	# AI playtest 1: no ids in the player's mind line; a lost line says so; a market trip that bought ends a queued buy.
	var mind := Words.status({"tick": 50, "place": "plaza", "route": null, "activity": "work", "asleep": false, "hunger": 100,
			"food": 1, "coin": 3, "role": "guard", "occasion": {"text": "Bane Deka (#0) asked you for food"}}, {"id": 3, "name": "Me"}, 144)
	var lost := Words.describe({"kind": "receipt", "actor": 3, "origin": "rule", "tick": 50,
			"data": {"action": "say", "ok": false, "why": "target not here", "to": 4}}, {"id": 3, "name": "Me"}, {4: "Ari"})
	var trip := [{"kind": "receipt", "data": {"ok": true, "why": "bought", "action": "buy_food"}}]
	# pt11: an answer that walks off says so, before the player types to someone who's leaving.
	var off := Words.describe({"kind": "reply", "actor": 4, "origin": "rule", "tick": 50,
			"data": {"with": 3, "says": "Goodbye.", "next": "leave"}}, {"id": 3, "name": "Me"}, {4: "Ari"})
	var stays := Words.describe({"kind": "reply", "actor": 4, "origin": "rule", "tick": 50,
			"data": {"with": 3, "says": "Sure.", "next": "stay"}}, {"id": 3, "name": "Me"}, {4: "Ari"})
	if not "Ari walks away." in off or "walks away" in stays or not "Ari has gone" in lost:
		printerr("pt11 words: %s | %s | %s" % [off, stays, lost])
		quit(1)
		return
	if "(#0)" in mind or not "Bane Deka asked you for food" in mind or not "didn't reach" in lost \
			or not Plan.bought(Plan.step("go_market"), trip) or Plan.bought(Plan.step("buy_food"), trip) \
			or Plan.bought(Plan.step("go_market"), [{"kind": "receipt", "data": {"ok": true, "why": "no stock"}}]):
		printerr("aipt1 words: %s | %s" % [mind, lost])
		quit(1)
		return
	me.heard.connect(func(m): heard_n += 1)
	var a := OS.get_cmdline_user_args()
	if a.size() > 2:
		want_ticks = int(a[2])
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
	if int(m["world"]["me"]["id"]) != me.agent or m["verbs"].keys() != ["act"]:
		_fail("welcome is not for villager %d in inhabitant scope: %s" % [me.agent, m["world"]["me"]])
	_check_view(m["view"])
	god.control("step")


func _on_tick(m: Dictionary) -> void:
	var v: Dictionary = m["view"]
	_check_view(v)
	var menu := LiveClient.menu(v)
	var want := 0
	for action in v["offer"]:
		want += maxi(1, v["offer"][action].size())
	if menu.size() != want:
		_fail("menu has %d buttons for an offer of %d" % [menu.size(), want])
	buttons += menu.size()
	seen += 1
	if not menu.is_empty() and seen % 3 == 0:
		var b: Dictionary = menu[(seen / 3) % menu.size()]
		var named := []  # JSON numbers arrive as floats
		for t in v["offer"].get(b["action"], []):
			named.append(int(t))
		if not v["offer"].has(b["action"]) or (b["target"] >= 0 and not b["target"] in named):
			_fail("button %s is not in the offer" % b)
		pressed.append(me.act(b["action"], b["target"]))
	if seen < want_ticks:
		god.control("step")


func _check_view(v: Dictionary) -> void:
	for k in v:
		if not k in VIEW_KEYS:
			_fail("view field %s is not in the spec's whitelist" % k)
	for p in v["present"]:  # space-1: each stands on a tile of the place it shares with the body
		var r = me.places[str(v["place"])]["rect"]
		if p["pos"][0] < r[0] or p["pos"][0] >= r[0] + r[2] or p["pos"][1] < r[1] or p["pos"][1] >= r[1] + r[3]:
			_fail("a present villager's pos %s is not in its place %s" % [p["pos"], v["place"]])
	for p in v["seen"]:  # and the only other positions are of those in sight across open ground
		# reach-1: a walker's tiles are told for the span of the tick it is in sight for (`track`), each within
		# sight of where the body is at that moment, which moves too if it is walking.
		var pts: Array = p.get("track", []).duplicate()
		if not p.has("track") or int(p["in"][0]) == 0:
			pts.append([0, p["pos"][0], p["pos"][1]])
		for q in pts:
			var mine := Vector2(v["pos"][0], v["pos"][1])
			if v["route"] != null:
				var an = me.places[str(v["route"]["to"])]["anchor"]
				mine = WorldModel.route_pos(v["route"], Vector2i(int(an[0]), int(an[1])), int(me.welcome["world"]["map"]["walk"]),
						float(v["tick"]) + float(q[0]) / 600.0 - 1.0).floor()
			if (v["place"] != null and p["place"] != null) or maxf(absf(q[1] - mine.x), absf(q[2] - mine.y)) > 6:
				_fail("a seen villager at %s (%s) is out of sight of %s (%s)" % [q, p["place"], mine, v["place"]])


func _process(_delta: float) -> bool:
	if done:
		return true
	god.poll()
	me.poll()
	if Time.get_ticks_msec() - started > 60000:
		_fail("timed out after %d ticks" % seen)
	if seen >= want_ticks and not done:
		var receipts := {}
		for e in me.seen_events:
			if e["kind"] == "receipt" and int(e["actor"]) == me.agent and not e["parents"].is_empty():
				receipts[int(e["parents"][0])] = e["data"]["why"]
		var applied := 0
		var receipted := 0
		for id in pressed:
			var ack: Dictionary = me.acks.get(id, {})
			if ack.get("status") == "applied":
				applied += 1
				receipted += int(receipts.has(int(ack["event"])))
			elif ack.get("status") == "rejected":
				_fail("act %s was rejected: %s" % [id, ack])
		if done:
			return true
		var names := {}  # every witnessed event reads as words, and every one of its own receipts says something
		var worded := 0
		for e in me.seen_events:
			var line := Words.describe(e, me.me, names)
			worded += int(line != "")
			if e["kind"] == "receipt" and int(e["actor"]) == me.agent and line == "" and not e["data"]["ok"]:
				_fail("a refused act has no words: %s" % e)
		if done:
			return true
		if worded == 0:
			_fail("no witnessed event read as words")
			return true
		if applied < 20 or receipted != applied or buttons == 0:  # an ack follows the tick that carries its receipt
			_fail("%d acts sent, %d applied, %d with receipts, %d buttons shown" % [pressed.size(), applied, receipted, buttons])
			return true
		if heard_n == 0:  # the runner's default narrator (a template with no model) speaks for what it witnessed
			_fail("heard nothing said near villager %d in %d ticks" % [me.agent, seen])
			return true
		print("inhabited villager %d for %d ticks: %d buttons shown, %d acts applied, %d with receipts, %d heard"
				% [me.agent, seen, buttons, applied, receipted, heard_n])
		done = true
		quit(0)
	return false


func _fail(why: String) -> void:
	if done:
		return
	printerr(why)
	done = true
	quit(1)
