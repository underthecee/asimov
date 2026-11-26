# Protocol 3 client (plan/client-protocol.md): hello, then welcome, backlog and ticks.
# God scope rebuilds every villager into a WorldModel. Inhabitant scope (agent >= 0) keeps only what the
# server sends its villager: the latest whitelisted view and the events it witnessed. Its action menu is the
# server's offer, never computed here. Sends intents and controls only; never state.
extends RefCounted

const VerbWords := preload("res://scripts/verb_words.gd")

const PROTOCOL := 3
const WorldModel := preload("res://scripts/world_model.gd")

signal welcomed(msg: Dictionary)
signal ticked(msg: Dictionary)
signal early(msg: Dictionary)  # E4: an act's own events, sent as it applies mid-tick; its tick message leaves them out
signal acked(msg: Dictionary)
signal status_changed(msg: Dictionary)
signal failed(reason: String)
signal heard(msg: Dictionary)  # inhabitant: words spoken near it (readable-world E2), after the tick

var model := WorldModel.new()
var ws := WebSocketPeer.new()
var url := ""
var scope := "god"
var agent := -1         # inhabitant scope: the villager this client is
var token := ""         # the server's --token, when it has one
var me := {}            # inhabitant scope: welcome.world.me
var places := {}        # inhabitant scope: the map's places
var seen_events := []   # inhabitant scope: every event the server sent it, in order
var welcome := {}
var view := {}          # the latest engine view: {tick, agents: [{id, place, activity, asleep, pos, route}]}
var view_tick := 0
var tick_wall := 0.0    # Time.get_ticks_msec() when the latest view arrived, for interpolation only
var tps := 1.0
var paused := false
var acks := {}          # intent id -> ack
var walk_want = null    # inhabitant, walk-1: the tile the player has walked to on screen and not yet sent (Vector2i)
var walk_sent = null    # the last tile sent
var walks := {}         # walk intents sent and not yet acked: id -> tile
var _hello_sent := false
var _closed := false
var _reload := false   # the server refused our since (the world reopened shorter after a crash)
var _seq := 0


func connect_to(u: String) -> int:
	url = u
	ws.inbound_buffer_size = 64 << 20  # backlog chunks and the welcome can be several MB
	ws.outbound_buffer_size = 1 << 20
	return ws.connect_to_url(url)


func poll() -> void:
	ws.poll()
	var state := ws.get_ready_state()
	if state == WebSocketPeer.STATE_OPEN and not _hello_sent:
		_hello_sent = true
		var hello := {"type": "hello", "protocol": PROTOCOL, "scope": scope, "since": model.events.size()}
		if token != "":
			hello["token"] = token
		if scope == "inhabitant":
			hello["agent"] = agent
			hello["since"] = 0
		_send(hello)
	while ws.get_available_packet_count() > 0:  # also while closing: an error arrives just before the close
		_handle(ws.get_packet().get_string_from_utf8())
	if state != WebSocketPeer.STATE_CLOSED:
		return
	if ws.get_close_reason() == "bad_since" and not model.events.is_empty():
		_reload = true  # Godot can drop the error message that came just before the close; the close repeats it
	if _reload:  # start over from event 0, as the protocol asks after bad_since
		_reload = false
		_hello_sent = false
		model.events.clear()
		model.last_tick = -1
		connect_to(url)
	elif not _closed:
		_closed = true
		failed.emit("closed %d %s" % [ws.get_close_code(), ws.get_close_reason()])


func _handle(text: String) -> void:
	var m = JSON.parse_string(text)
	if typeof(m) != TYPE_DICTIONARY:
		failed.emit("not a JSON object")
		return
	if int(m.get("protocol", -1)) != PROTOCOL:
		failed.emit("unknown protocol %s" % m.get("protocol"))
		ws.close()
		return
	match m["type"]:
		"welcome":
			welcome = m
			view_tick = int(m["view_tick"])
			tps = float(m["tps"])
			paused = m["paused"]
			if scope == "inhabitant":
				me = m["world"]["me"]
				places = m["world"]["map"]["places"]
				seen_events.clear()
				view = m["view"]
			elif model.events.is_empty():
				model.load_world(m["world"])
			welcomed.emit(m)
		"events":
			for e in m["events"]:
				_add(e)
		"early":
			for e in m["events"]:
				_add(e)
			if m.has("view") and scope == "inhabitant":
				view = m["view"]  # protocol 3: a walk's answer carries where the body now stands and who is there
			early.emit(m)
		"tick":
			for e in m["events"]:
				_add(e)
			view = m["view"]
			view_tick = int(view["tick"])
			tick_wall = Time.get_ticks_msec()
			ticked.emit(m)
		"ack":
			acks[str(m["id"])] = m
			walks.erase(str(m["id"]))
			acked.emit(m)
		"heard":
			heard.emit(m)
		"status":
			paused = m["paused"]
			tps = float(m["tps"])
			status_changed.emit(m)
		"error":
			if m["reason"] == "bad_since" and not model.events.is_empty():
				_reload = true
				return
			failed.emit(str(m["reason"]))


func _add(e: Dictionary) -> void:
	if scope == "inhabitant":
		seen_events.append(e)
	else:
		model.add_event(e)


func _send(m: Dictionary) -> void:
	ws.send_text(JSON.stringify(m))


func fire(family: int) -> String:
	## Sends the fire intent; the ack arrives once the engine has applied it and the log is on disk.
	_seq += 1
	var id := "g%x%x-%d" % [Time.get_ticks_usec(), randi(), _seq]  # unique per world, as the protocol asks
	_send({"type": "intent", "id": id, "verb": "fire", "args": {"family": family}, "seen_tick": view_tick})
	return id


func act(action: String, target: int = -1, place: String = "", text: String = "", move: String = "",
		price: int = -1, thing: int = -1) -> String:
	## things-1: `thing` is the id of the thing a take, drop, use or hand names, and only theirs.
	## Inhabitant scope: the body's act. Its ack names the act event; the receipt follows in a tick's events.
	## `place` is go_to's (engine 24), and only go_to's; `text` is say's (say-1), and only say's. `move` (talk-1) is
	## a say's chip, "asks_food" or "wants_to_buy_food"; a buy carries the `price` shown.
	flush_walk()  # the engine judges an act where the body stands: the step taken on screen goes first
	_seq += 1
	var id := "i%x%x-%d" % [Time.get_ticks_usec(), randi(), _seq]
	var args := {"action": action}
	if target >= 0:
		args["target"] = target
	if place != "":
		args["place"] = place
	if text != "":
		args["text"] = text
	if move != "":
		args["move"] = move
	if price >= 0:
		args["price"] = price
	if thing >= 0:
		args["thing"] = thing
	_send({"type": "intent", "id": id, "verb": "act", "args": args, "seen_tick": view_tick})
	return id


func walk(tile: Vector2i) -> String:
	## Inhabitant scope, walk-1: step the body toward `tile`. The engine moves it as far as walking speed allows
	## since its last walk (time-1a), the moment the walk arrives, and answers at once with a `moved` event
	## carrying where it stands and which place that is, and with its new view. Not the tick's one act.
	_seq += 1
	var id := "w%x%x-%d" % [Time.get_ticks_usec(), randi(), _seq]
	walk_sent = tile
	walk_want = null
	walks[id] = tile
	_send({"type": "intent", "id": id, "verb": "act", "args": {"action": "walk", "to": [tile.x, tile.y]},
			"seen_tick": view_tick})
	return id


func flush_walk() -> void:
	if walk_want != null and walk_want != walk_sent:
		walk(walk_want)
	walk_want = null


static func menu(v: Dictionary) -> Array:
	## The buttons for a view: one per offered action, or per action and person it may name. Straight from
	## the server's offer; the client adds nothing and drops nothing.
	var names := {}
	for p in v.get("present", []) + v.get("kin", []) + v.get("seen", []):  # seen: in reach, or walking by (reach-1)
		names[int(p["id"])] = p["name"]
	var out := []
	var offer: Dictionary = v.get("offer", {})
	for action in offer:
		if action in VerbWords.THING_ACTS:
			continue  # things-1: these name things, and have their own list (inhabit_ui.thing_items)
		var people: Array = offer[action]
		if people.is_empty():
			out.append({"label": action.replace("_", " "), "action": action, "target": -1})
		for t in people:
			out.append({"label": "%s %s" % [action.replace("_", " "), names.get(int(t), "#%d" % int(t))],
					"action": action, "target": int(t)})
	return out


func control(op: String, rate: float = 0.0) -> void:
	var m := {"type": "control", "op": op}
	if op == "speed":
		m["tps"] = rate
	_send(m)


func display_tick() -> float:
	## The view tick plus how far into the next tick the wall clock is, for drawing only.
	if paused or view.is_empty():
		return float(view_tick)
	return view_tick + clampf((Time.get_ticks_msec() - tick_wall) / 1000.0 * tps, 0.0, 1.0)


func ticks_per_day() -> int:
	if scope == "inhabitant":
		return int(welcome.get("world", {}).get("ticks_per_day", 144))
	return model.ticks_per_day
