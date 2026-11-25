# Headless, against a running live server started paused with --things and stepped to a waking hour:
#   godot --headless --path client/godot --script res://tests/test_offer.gd -- ws://127.0.0.1:8765/ AGENT
# deal-1: the Trade panel's rules (offer_box.gd) and the words for an offer, its deal and its receipts
# (inhabit_ui.gd), then a live round with whoever the server says is in reach: a gift moves at once; an offer
# that asks for something is put to the villager, who answers yes or no in words, and a yes comes with its
# deal; an offer the villager can't meet is refused with its reason. Exits 1 on any failure or timeout.
extends SceneTree

const LiveClient := preload("res://scripts/live_client.gd")
const Keys := preload("res://scripts/walk_keys.gd")
const Words := preload("res://scripts/inhabit_ui.gd")
const Offer := preload("res://scripts/offer_box.gd")

var god := LiveClient.new()
var me := LiveClient.new()
var started := 0
var done := false
var phase := 0
var ticks := 0
var to := -1
var what := "coin"
var act_id := ""
var sent := -1


func _init() -> void:
	_start()


func _start() -> void:
	var why := _words()
	if why != "":
		return _fail(why)
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


func _words() -> String:
	## The panel's rules and every line, with no server; "" or what reads wrong.
	var v := {"present": [{"id": 4, "name": "Ari", "kin": false, "asleep": false, "pos": [1, 1],
				"carries": [{"id": 9, "kind": "flower"}]}], "kin": [], "seen": [], "food": 2, "coin": 3, "hunger": 100,
			"tick": 80, "asleep": false, "place": "woods", "activity": "explore", "route": null, "pos": [1, 1],
			"things": [], "carried": [{"id": 6, "kind": "flower"}, {"id": 5, "kind": "flower"}],
			"offer": {"offer": [4], "rest": [], "give_food": [4]}}
	var kinds := {}
	Offer.learn(kinds, v)
	if kinds != {6: "flower", 5: "flower", 9: "flower"}:
		return "the kinds learned read %s" % [kinds]
	var caps := Offer.caps(v, 4)
	if caps != {"give": {"food": 2, "coin": 3, "flower": 2}, "want": {"food": 999, "coin": 999, "flower": 1}}:
		return "the caps read %s" % [caps]
	if not Offer.can(v, 4) or Offer.can(v, 7):
		return "who can be offered a trade isn't the server's list"
	var give := Offer.side({"coin": 2, "flower": 1, "food": 0}, Offer.by_kind(v["carried"]))
	var want := Offer.side({"food": 1}, Offer.by_kind(Offer.carries(v, 4)))
	if JSON.stringify(give) != "{\"coin\":2,\"things\":[5]}" or JSON.stringify(want) != "{\"food\":1}":
		return "the sides read %s for %s" % [JSON.stringify(give), JSON.stringify(want)]
	if not Offer.side({"food": 0, "flower": 0}, {"flower": [5]}).is_empty() or not Offer.empty({}) or Offer.empty(give):
		return "an empty side isn't empty"
	# the engine's own wording of a side (tests/test_deal.py pins the same lines against sim.offer_side)
	var said := [Offer.words({"coin": 6}, kinds), Offer.words({"food": 2, "things": [9]}, kinds),
			Offer.words({"food": 2, "coin": 6, "things": [5, 6]}, kinds), Offer.words({"things": [77]}, kinds), Offer.words({}, kinds)]
	if said != ["6 coin", "2 food and a flower", "2 food, 6 coin and 2 flowers", "something", ""]:
		return "a side's words read %s" % [said]
	var sums := [Offer.summary(give, want, "Ari", kinds), Offer.summary(give, {}, "Ari", kinds),
			Offer.summary({}, want, "Ari", kinds), Offer.summary({}, {}, "Ari", kinds)]
	if sums != ["Offer Ari 2 coin and a flower for 1 food.", "Give Ari 2 coin and a flower, asking nothing back.",
			"Ask Ari for 1 food, offering nothing.", ""]:
		return "the panel's line reads %s" % [sums]
	# the server's offer puts the verb on Ari's row and in the E list; pressing it opens the panel (main.gd _choose)
	var in_menu := LiveClient.menu(v).filter(func(it): return it["action"] == "offer" and int(it["target"]) == 4)
	var e := Keys.items(v, 4, {4: "Ari"}, true).filter(func(it): return it["action"] == "offer")
	if in_menu.size() != 1 or e.size() != 1 or e[0]["label"] != "Offer Ari a trade":
		return "the offer isn't on Ari's row and in the E list once: %s, %s" % [in_menu, e]
	var who := {"id": 3, "name": "Me", "home": "home_0"}
	var names := {4: "Ari"}
	var line := func(kind: String, d: Dictionary) -> String:
		return Words.describe({"kind": kind, "actor": 3, "origin": "rule", "tick": 80, "parents": [1], "data": d}, who, names, kinds)
	var got := [
		line.call("offered", {"with": 4, "gives": {"coin": 2}, "gets": {"food": 1}, "place": "woods"}),
		line.call("offered", {"with": 4, "gives": {}, "gets": {"things": [9]}, "place": "woods"}),
		line.call("offered", {"with": 4, "place": "woods"}),
		line.call("deal", {"move": "offer", "from": 3, "to": 4, "gives": {"coin": 2}, "gets": {"food": 1}, "ok": true}),
		line.call("deal", {"move": "offer", "from": 3, "to": 4, "gives": {"things": [5]}, "gets": {}, "ok": true}),
		line.call("deal", {"move": "offer", "from": 3, "to": 4, "gives": {}, "gets": {"food": 1}, "ok": true}),
		line.call("deal", {"move": "offer", "from": 3, "to": 4, "gives": {"coin": 2}, "gets": {"food": 1}, "ok": false,
				"why": "not theirs to give"}),
		line.call("deal", {"move": "offer", "from": 3, "to": 4, "gives": {"coin": 2}, "gets": {"food": 1}, "ok": false,
				"why": "gone"}),
		line.call("receipt", {"action": "offer", "ok": false, "why": "too many offers", "to": 4}),
		line.call("receipt", {"action": "offer", "ok": false, "why": "already traded", "to": 4}),
		line.call("receipt", {"action": "offer", "ok": true, "why": "offered", "to": 4}),
		line.call("receipt", {"action": "offer", "ok": true, "why": "gave", "to": 4})]
	var should := [
		"You offer Ari 2 coin for 1 food.",
		"You ask Ari for a flower.",
		"You offer Ari a trade.",
		"[color=#80d080]You traded with Ari: you gave 2 coin and got 1 food.[/color]",
		"[color=#80d080]You gave Ari a flower.[/color]",
		"[color=#80d080]Ari gave you 1 food.[/color]",
		"[color=#e07070]Ari said yes, but nothing changed hands: they don't have what you asked for.[/color]",
		"[color=#e07070]Ari didn't answer your offer: you had walked away before they could answer.[/color]",
		"[color=#e07070]Your offer to Ari wasn't made: they've heard enough offers from you today.[/color]",
		"[color=#e07070]Your offer to Ari wasn't made: you've already made that trade with them today.[/color]",
		"", ""]
	for i in should.size():
		if got[i] != should[i]:
			return "line %d reads \"%s\", not \"%s\"" % [i, got[i], should[i]]
	return ""


func _on_welcome(m: Dictionary) -> void:
	var act: Dictionary = m["verbs"]["act"]
	if not "offer" in act["action"] or not act.has("give") or not act.has("want"):
		return _fail("the welcome doesn't offer `offer` with its two sides: %s" % [act])
	god.control("step")


func _receipt(id: String) -> Dictionary:
	var ack: Dictionary = me.acks.get(id, {})
	if ack.get("status") != "applied":
		return {}
	for e in me.seen_events:
		if e["kind"] == "receipt" and not e["parents"].is_empty() and int(e["parents"][0]) == int(ack["event"]):
			return e
	return {}


func _mine(kind: String) -> Array:
	## Events of this kind from this body to `to`, seen since the offer in hand was sent.
	return me.seen_events.filter(func(e): return e["kind"] == kind and int(e["tick"]) >= sent \
			and str(e["data"].get("move", "offer")) == "offer" \
			and int(e["data"].get("to", e["data"].get("with", -1))) == to and int(e["actor"]) == me.agent)


func _offer(v: Dictionary, give: Dictionary, want: Dictionary) -> void:
	sent = int(v["tick"])
	act_id = me.act("offer", to, "", "", "", -1, -1, give, want)


func _on_tick(m: Dictionary) -> void:
	var v: Dictionary = m["view"]
	ticks += 1
	if v["asleep"]:
		return _fail("villager %d is asleep at tick %d: start the test at a waking hour" % [me.agent, int(v["tick"])])
	var near: Array = v.get("offer", {}).get("offer", [])
	match phase:
		0:  # someone awake in reach, by the server's own list; walk up to whoever is at this place if nobody is
			if near.is_empty():
				var awake: Array = v["present"].filter(func(p): return not p["asleep"])
				if not awake.is_empty():
					me.walk(Vector2i(int(awake[0]["pos"][0]), int(awake[0]["pos"][1])))
			else:
				to = int(near[0])
				what = "coin" if int(v["coin"]) >= 2 else "food"
				if int(v[what]) < 2:
					return _fail("the body has under 2 food and under 2 coin to offer: %s" % [v])
				_offer(v, {what: 1}, {})  # a gift: nothing asked back
				phase = 1
		1:  # the gift moved at once, with no answer to wait for
			var r := _receipt(act_id)
			if r.is_empty():
				if int(v["tick"]) - sent > 2:
					return _fail("no receipt for the gift")
			elif r["data"].get("why") == "target not here":  # they stepped away as it was sent: find someone again
				phase = 0
			elif not r["data"].get("ok", false) or r["data"].get("why") != "gave":
				return _fail("the gift got %s" % [r["data"]])
			else:
				var deal := _mine("deal")
				if deal.is_empty() or not deal[0]["data"]["ok"] or JSON.stringify(deal[0]["data"]["gives"]) != "{\"%s\":1}" % what:
					return _fail("the gift's deal reads %s" % [deal])
				if not "You gave" in Words.describe(deal[0], me.me, {to: "them"}):
					return _fail("the gift's deal doesn't read as a gift: %s" % Words.describe(deal[0], me.me, {to: "them"}))
				if not near.has(float(to)) and not near.has(to):
					phase = 0
					return god.control("step")
				_offer(v, {what: 1}, {"food" if what == "coin" else "coin": 1})
				phase = 2
		2:  # put to the villager: an `offered`, their answer in words with a yes or no, and a deal only after a yes
			var r := _receipt(act_id)
			if not r.is_empty() and not r["data"].get("ok", false):
				if r["data"].get("why") in ["target not here", "target asleep"]:
					return _fail("the villager left before the offer landed: %s" % [r["data"]])
				print("offer refused by a limit: %s" % Words.describe(r, me.me, {to: "them"}))  # nothing to spare, bad terms: a worded no
				phase = 3
				_offer(v, {what: 1}, {"food": 900})
			elif not r.is_empty():
				var offered := _mine("offered")
				var reply: Array = me.seen_events.filter(func(e): return e["kind"] == "reply" and int(e["actor"]) == to \
						and int(e["tick"]) >= sent and int(e["data"]["with"]) == me.agent and e["data"].has("answer"))
				if r["data"].get("why") != "offered" or offered.is_empty():
					return _fail("the offer got %s and offered %s" % [r["data"], offered])
				if not reply.is_empty():
					var answer := str(reply[0]["data"]["answer"])
					var deal := _mine("deal").filter(func(e): return not e["data"]["gets"].is_empty())
					if str(reply[0]["data"].get("says", "")) == "":
						return _fail("the villager answered the offer with no words: %s" % [reply[0]])
					if (answer == "yes") != (not deal.is_empty() and deal[0]["data"]["ok"]):
						return _fail("answer %s with deal %s" % [answer, deal])
					print("offered %d 1 %s at tick %d, answered %s: %s" % [to, what, sent, answer,
							Words.describe(deal[0], me.me, {to: "them"}) if not deal.is_empty() else "(no deal)"])
					phase = 3
					_offer(v, {what: 1}, {"food": 900})
				elif int(v["tick"]) - sent > 4:
					return _fail("no answer to the offer within 4 ticks")
			elif int(v["tick"]) - sent > 2:
				return _fail("no receipt for the offer")
		3:  # more food than anyone holds: refused with its reason, nothing put to the villager
			var r := _receipt(act_id)
			if r.is_empty():
				if int(v["tick"]) - sent > 2:
					return _fail("no receipt for the offer nobody could meet")
			elif r["data"].get("ok", true) or not r["data"].get("why") in ["not theirs to give", "target not here", "target asleep"]:
				return _fail("an offer for 900 food got %s" % [r["data"]])
			else:
				print("refused: %s" % Words.describe(r, me.me, {to: "them"}))
				done = true
				quit(0)
				return
	if ticks > 200:
		return _fail("nobody awake in reach to trade with in 200 ticks (phase %d)" % phase)
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
