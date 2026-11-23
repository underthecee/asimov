# The client: draws the map from genesis, villagers from events, a timeline with a scrubber, and sends fire.
#   godot --path client/godot -- --url ws://127.0.0.1:8765/     live, against python -m asiimov.live
#   ... --url URL --inhabit 3 [--token T]                         live as villager 3: what it sees, in words, and
#                                                                 its action menu
#   ... --ui-scale 1.5                                            everything larger (default 1.25, times the screen's
#                                                                 own scale); Ctrl/Cmd + and - change it while playing
#   godot --path client/godot -- --log runs/demo/events.jsonl   a recorded log
#   ... --log PACK.json.gz                                        a playback pack (tools/make_playback.py): the log
#                                                                 plus the engine's holdings; click a place to see
#                                                                 who is there and what they hold
#   an exported build with res://bundled/playback.json.gz plays that pack when given no --url or --log (G7)
#   ... --log LOG --at TICK --screenshot out.png                  save one frame and quit (needs a display: xvfb-run;
#                                                                 live, it waits about 3 s first)
# Interpolation between ticks is for drawing only. Nothing drawn here is sent to the engine.
extends Node2D

const WorldModel := preload("res://scripts/world_model.gd")
const LiveClient := preload("res://scripts/live_client.gd")
const Words := preload("res://scripts/inhabit_ui.gd")
const Plan := preload("res://scripts/plan_queue.gd")
const TalkBox := preload("res://scripts/talk_box.gd")
const Art := preload("res://scripts/art.gd")
const Keys := preload("res://scripts/walk_keys.gd")
const PICKED_W := 380.0  # recorded mode: the "who is here" column
const BUNDLED := "res://bundled/playback.json.gz"  # the world an export carries (CI puts it there before it exports)
const COLORS := {"sleep": Color(0.35, 0.35, 0.5), "home": Color(0.6, 0.6, 0.7), "work": Color(0.85, 0.6, 0.2),
		"shop": Color(0.3, 0.7, 0.4), "socialize": Color(0.8, 0.4, 0.7), "rest": Color(0.5, 0.7, 0.9),
		"explore": Color(0.9, 0.9, 0.5)}  # sim.py ACTIVITIES plus sleep; anything newer is drawn white

var model: WorldModel
var client: LiveClient = null
var replay_tick := 0.0      # recorded mode: where the scrubber is
var playing := false
var follow := true          # live mode: the view follows the newest tick
var scrub_tick := 0
var ui := {}
var shot := {}              # --screenshot: {path, frames left}
var names := {}             # inhabitant: id -> name, from what the server sent (welcome, present, kin)
var roles := {}             # inhabitant: id -> role, from the welcome's people and the present list
var doing := {}             # inhabitant: what each villager here was last seen doing
var pending := {}           # inhabitant: the act sent and not yet resolved {id, label, tick, event?, step?}
var plan_q := []            # inhabitant: the player's own plan (readable-world C2), head first
var plan_state := ""        # the head step's state: "" (not sent), "wait", "hold" or "failed: WHY"
var plan_act := -1          # the act event of the head step, once acked
var plan_receipts := []     # receipts (and a seek's meet) so far that cite plan_act
var plan_sent := 0          # the view tick the head step was sent at
var plan_failed := 0        # the view tick the head step failed at (it is skipped a tick later)
var leaving := {}           # inhabitant: villager id -> the tick they answered you and walked away (pt11); off the Talk list that tick and the next two
var last_said := {}         # inhabitant: villager id -> the last line said to them, put back if it didn't reach them
var talk := TalkBox.new()   # inhabitant: whom the Talk box's line is for (aipt3: only the player's pick changes a pick)
var asked := {}             # inhabitant: villager id -> whether your last line to them asked for food (a chip, or it read so)
var heard_recent := []      # inhabitant: keys of the last overheard exchanges, so a word-for-word repeat isn't shown
var map_rects := {}         # inhabitant: place name -> its rect on screen, for clicks
const CROWD_MAX := 12        # polish: at most this many people drawn at your place; the rest are "+N more here"
var map_w := 420.0          # inhabitant: the map's column; the feed and panels take the rest (sized in _layout)
var side_w := 400.0         # inhabitant: the right-hand column (you, plan, here, actions, talk)
var ui_scale := 1.25        # P-pass (joon 2026-11-18 "very small and hard to see"): everything x this, on top of
							# the screen's own scale (2 on a Retina Mac); --ui-scale S, and Ctrl/Cmd + / - live
var zoom := 1.8             # inhabitant: map zoom (V4), wheel over the map; past 1 the map follows you
var bubbles := []           # inhabitant: words shown over their speaker (V3) {who, text, mine, born}
var drawn_at := {}          # inhabitant: id -> where that villager was drawn this frame (for bubbles)
const BUBBLE_S := 6.0       # a bubble shows this long, then fades; the line stays in "Said near you"
var say_once := ""          # --say TEXT (dev, screenshots): say it once to the first villager here, awake
var walk_pos := Vector2.ZERO # inhabitant, walk-1: where your body is drawn while the keys walk it, in tiles
var walking := false         # the keys have the body: it is drawn at walk_pos, ahead of the engine's next tick
var walk_at := -99.0         # when the last walk was sent, in seconds
var walk_place = ""          # the place the engine last said you walked into (null: the open), for the log line
var reach := -1              # the villager within reach of E, by where they are drawn
var e_items := []            # the E list while it is open: [{label, action, target}]
var picked := ""            # recorded mode: the place clicked, shown in the "who is here" panel
var announced := false      # recorded mode: "playing" printed once, for the export's smoke test


func _ready() -> void:
	texture_filter = CanvasItem.TEXTURE_FILTER_NEAREST  # pixel art stays crisp when scaled (V-pass)
	var args := _args()
	ui_scale = clampf(float(args.get("ui-scale", str(ui_scale))), 0.5, 4.0)
	# A Retina screen gets a window sized to it; at scale 1 (CI, aiplay, most monitors) the window keeps its
	# size, from the project or --resolution (Godot strips engine flags from the args, so they can't be read).
	_apply_scale(DisplayServer.screen_get_scale() > 1.0)
	get_window().size_changed.connect(_layout)  # the window, not the viewport: in canvas_items mode the viewport keeps its units
	get_window().dpi_changed.connect(_layout)  # moved to a screen of another scale (review of #92)
	_build_ui()
	var log_path: String = args.get("log", "")
	if log_path == "" and not args.has("url") and FileAccess.file_exists(BUNDLED):
		log_path = BUNDLED  # an export with a world in it plays that world, with no engine to connect to
		playing = true
	if log_path != "":
		model = WorldModel.new()
		var err := model.load_file(log_path)
		if err != "":
			ui["status"].text = err
			printerr("playback: " + err)
		else:
			ui["status"].text = "recorded world, engine %s, %d events%s" % [model.engine, model.events.size(),
					"" if not model.holdings.is_empty() else " (a bare log: no holdings)"]
			print("playback: ready, engine %s, %d villagers, view ticks 0-%d" % [model.engine, model.agents.size(), model.last_tick + 1])
		replay_tick = float(args.get("at", "0"))
		picked = str(args.get("pick", ""))
		_build_playback_ui()
	else:
		client = LiveClient.new()
		model = client.model
		client.token = args.get("token", "")
		if args.has("inhabit"):
			client.scope = "inhabitant"
			client.agent = int(args["inhabit"])
			client.ticked.connect(_on_inhabit_tick)
			client.early.connect(_on_early)
			client.welcomed.connect(_on_inhabit_welcome)
			client.heard.connect(_on_heard)
			_build_inhabit_ui()
			for k in ["family", "fire", "play", "menu_scroll", "slider", "clock"]:  # divine verbs and controls aren't an inhabitant's
				ui[k].visible = false
		client.failed.connect(func(r): ui["status"].text = "disconnected: " + r)
		client.acked.connect(_on_ack)
		client.connect_to(args.get("url", "ws://127.0.0.1:8765/"))
		ui["status"].text = "connecting"
	say_once = str(args.get("say", ""))
	if args.has("screenshot"):  # live: wait for the welcome and a few ticks first
		shot = {"path": args["screenshot"], "frames": 5 if log_path != "" else 180}


func _args() -> Dictionary:
	var out := {}
	var a := OS.get_cmdline_user_args()
	for i in a.size() - 1:
		if a[i].begins_with("--"):
			out[a[i].substr(2)] = a[i + 1]
	return out


func _apply_scale(fit_window: bool) -> void:
	## P-pass: the UI is drawn at the screen's scale times ui_scale. A Retina Mac reports 2: without it, a window
	## of 1280x800 pixels is a quarter of the screen and its 16 px text reads as 8 pt. The layout is in these
	## scaled units, so a bigger scale gives fewer, larger units, and _layout sizes the columns to them.
	if DisplayServer.get_name() == "headless":  # no screen: a 100x100 window, nothing to scale for
		return
	var w := get_window()
	var s := maxf(DisplayServer.screen_get_scale(w.current_screen), 1.0) * ui_scale
	w.content_scale_mode = Window.CONTENT_SCALE_MODE_CANVAS_ITEMS
	w.content_scale_aspect = Window.CONTENT_SCALE_ASPECT_EXPAND
	if fit_window:
		var room := DisplayServer.screen_get_usable_rect(w.current_screen)
		var want := Vector2(1280, 860) * s
		var got := Vector2i(mini(int(want.x), int(room.size.x * 0.94)), mini(int(want.y), int(room.size.y * 0.92)))
		w.size = got
		w.position = room.position + (room.size - got) / 2
	w.content_scale_size = Vector2i(Vector2(w.size) / s)
	w.content_scale_factor = 1.0
	_layout()


func _layout() -> void:
	## The columns as shares of the (scaled) window, so the panels fit at any scale: the map a third, the right
	## column about a third with room for its buttons, the feed the rest.
	var w := get_window()
	var s := maxf(DisplayServer.screen_get_scale(w.current_screen), 1.0) * ui_scale
	var want := Vector2i(Vector2(w.size) / s)
	if DisplayServer.get_name() != "headless" and w.content_scale_size != want:
		w.content_scale_size = want
	var width := get_viewport_rect().size.x
	map_w = clampf(width * 0.32, 280.0, 560.0)
	side_w = clampf(width * 0.33, 330.0, 520.0)
	if ui.has("side"):
		ui["side"].offset_left = -side_w
		ui["feed_box"].offset_left = map_w + 12
		ui["feed_box"].offset_right = -(side_w + 16)
	queue_redraw()


func _build_ui() -> void:
	var layer := CanvasLayer.new()
	add_child(layer)
	var bar := HBoxContainer.new()
	bar.set_anchors_and_offsets_preset(Control.PRESET_BOTTOM_WIDE)
	bar.offset_top = -40
	layer.add_child(bar)
	var play := Button.new()
	play.text = "play/pause"
	play.pressed.connect(_toggle_play)
	bar.add_child(play)
	var slider := HSlider.new()
	slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	slider.step = 1
	slider.value_changed.connect(_scrub)
	bar.add_child(slider)
	var clock := Label.new()
	bar.add_child(clock)
	var fam := SpinBox.new()
	fam.min_value = 0
	bar.add_child(fam)
	var fire := Button.new()
	fire.text = "fire"
	fire.pressed.connect(func(): if client: ui["status"].text = "sent " + client.fire(int(fam.value)))
	bar.add_child(fire)
	var status := Label.new()
	status.position = Vector2(8, 8)
	layer.add_child(status)
	var scroll := ScrollContainer.new()  # inhabitant mode: one button per offered action (and person)
	scroll.set_anchors_and_offsets_preset(Control.PRESET_RIGHT_WIDE)
	scroll.offset_left = -210
	scroll.offset_top = 8
	scroll.offset_bottom = -48
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	layer.add_child(scroll)
	var menu := VBoxContainer.new()
	menu.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(menu)
	ui = {"slider": slider, "clock": clock, "family": fam, "status": status, "menu": menu, "fire": fire, "play": play,
			"menu_scroll": scroll, "layer": layer}


func _build_playback_ui() -> void:
	## Recorded mode: no divine verbs, and a panel for the place clicked.
	for k in ["family", "fire", "menu_scroll"]:
		ui[k].visible = false
	ui["status"].visible = false  # its text heads the panel instead, clear of the map
	var box := PanelContainer.new()  # the right-hand column; the map keeps the rest (PICKED_W in _draw)
	box.set_anchors_and_offsets_preset(Control.PRESET_RIGHT_WIDE)
	box.offset_left = -PICKED_W
	box.offset_top = 8
	box.offset_right = -8
	box.offset_bottom = -48
	ui["layer"].add_child(box)
	var body := RichTextLabel.new()  # scrolls when a place is crowded
	body.bbcode_enabled = true
	box.add_child(body)
	ui["picked"] = body


func _refresh_picked() -> void:
	## Who is at the picked place at the tick shown, and what each holds: the pack's rows, nothing worked out here.
	if not ui.has("picked"):
		return
	var lines := ["[color=#9fb4d8]%s[/color]" % ui["status"].text, ""]
	if not model.places.has(picked):
		ui["picked"].text = "\n".join(lines + ["Click a place to see who is there."])
		return
	var t := int(_tick_shown())
	var ids := model.present(picked, t)
	lines.append("[b]%s[/b]: %d here" % [picked.replace("_", " "), ids.size()])
	for id in ids:
		var a: Dictionary = model.agents[id]
		var h := model.holding_at(id, t)
		lines.append("%s, %s, %s%s" % [a["name"], a.get("role", ""), model.state_at(id, t)[3],
				"" if h.is_empty() else ": %d food, %d coin" % [h[1], h[2]]])
	ui["picked"].text = "\n".join(lines)


func _panel(parent: Control, title: String, expand := false) -> RichTextLabel:
	var head := Label.new()
	head.text = title
	head.add_theme_color_override("font_color", Color(0.65, 0.75, 0.9))
	parent.add_child(head)
	var body := RichTextLabel.new()
	body.bbcode_enabled = true
	body.fit_content = not expand
	body.scroll_active = expand
	if expand:
		body.size_flags_vertical = Control.SIZE_EXPAND_FILL
		body.scroll_following = true
	parent.add_child(body)
	return body


func _build_inhabit_ui() -> void:
	## Inhabitant mode: you, who's here and your buttons on the right; what happened, your plan and talk in the middle.
	var side := VBoxContainer.new()
	side.set_anchors_and_offsets_preset(Control.PRESET_RIGHT_WIDE)
	side.offset_left = -side_w
	side.offset_right = -8
	side.offset_top = 8
	side.offset_bottom = -12
	ui["layer"].add_child(side)
	ui["side"] = side
	# P-pass: at a larger text size the right column had no room left for its action buttons. What you did and
	# heard, your own plan and the Talk box now share the middle column; the right one keeps you, who's here and
	# what you can do.
	var feed_box := VBoxContainer.new()
	feed_box.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	feed_box.offset_left = map_w + 12  # the map takes the left column
	feed_box.offset_right = -(side_w + 16)
	feed_box.offset_top = 8
	feed_box.offset_bottom = -12
	ui["layer"].add_child(feed_box)
	ui["feed_box"] = feed_box
	ui["feed"] = _panel(feed_box, "What's happening", true)
	ui["feed"].size_flags_stretch_ratio = 1.3
	# aipt1 (fix 1): a yes/no ask waits on you for a few ticks, then your habits answer. It sat among 25 action rows
	# and was missed; it now comes first, above everything, with its own buttons.
	var asked := RichTextLabel.new()
	asked.bbcode_enabled = true
	asked.fit_content = true
	side.add_child(asked)
	ui["asked"] = asked
	var answers := VBoxContainer.new()
	side.add_child(answers)
	ui["answers"] = answers
	ui["you"] = _panel(side, "You")
	ui["plan"] = _panel(side, "Today's plan")
	ui["here"] = _panel(side, "Here with you")
	# A crowded place (25+ people) ran the list off the window and pushed the actions and the Talk box out of view
	# (AI-playtester find, 2026-11-16): the list now scrolls in a capped box.
	ui["here"].fit_content = false
	ui["here"].scroll_active = true
	ui["here"].custom_minimum_size = Vector2(0, 110)
	ui["myplan"] = _panel(feed_box, "Your plan")
	var ctl := HFlowContainer.new()
	feed_box.add_child(ctl)
	var ahead := CheckButton.new()
	ahead.text = "Plan ahead"
	ahead.tooltip_text = "On: actions and places you press line up in your plan instead of happening now."
	ahead.toggled.connect(func(on): _menu_key = ""; _refresh_inhabit())
	ctl.add_child(ahead)
	ui["ahead"] = ahead
	for k in ["Next", "Retry", "Clear"]:
		var b := Button.new()
		b.text = k
		b.pressed.connect(_plan_button.bind(k))
		ctl.add_child(b)
		ui["plan_" + k.to_lower()] = b
	var head := Label.new()
	head.text = "What you can do"
	head.add_theme_color_override("font_color", Color(0.65, 0.75, 0.9))
	side.add_child(head)
	var wait := Label.new()
	wait.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	side.add_child(wait)
	ui["wait"] = wait
	var scroll := ScrollContainer.new()
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	side.add_child(scroll)
	var menu := VBoxContainer.new()
	menu.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(menu)
	ui["menu"] = menu
	# say-1: talk to someone here in your own words; their mind answers on the next tick.
	ui["said"] = _panel(feed_box, "Said near you", true)  # readable-world C3: talk you can hear, in words
	ui["said"].append_text("[color=#8a93a6]Conversations near you, and what people say to you, show up here.[/color]\n")
	var talk_head := Label.new()
	talk_head.text = "Talk"
	talk_head.add_theme_color_override("font_color", Color(0.65, 0.75, 0.9))
	feed_box.add_child(talk_head)
	var row := HBoxContainer.new()
	feed_box.add_child(row)
	var who := OptionButton.new()
	who.custom_minimum_size = Vector2(120, 0)
	who.clip_text = true
	who.item_selected.connect(func(i):
		talk.pick(who.get_item_id(i))
		_refresh_talk())
	# aipt3: the list is left alone while it is open (a rebuild under the click sent lines to the wrong villager);
	# it catches up as it closes.
	who.get_popup().popup_hide.connect(func(): _refresh_talk.call_deferred())
	row.add_child(who)
	ui["talk_who"] = who
	var line := LineEdit.new()
	line.max_length = 140
	line.placeholder_text = "Say something..."
	line.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	line.text_submitted.connect(func(_t): _say())
	line.text_changed.connect(func(_t):
		line.select_all_on_focus = false  # a typed edit keeps the box's text
		_ask_hint())
	row.add_child(line)
	ui["talk_line"] = line
	var say := Button.new()
	say.text = "Say"
	say.pressed.connect(_say)
	row.add_child(say)
	ui["talk_say"] = say
	# talk-1: a chip says what the line asks for; the villager answers yes or no, and a yes moves the food. Just
	# talk (the default) asks nothing, whatever the words say.
	# Its own row above the Talk row, so the line keeps its width and its place at the bottom (the aiplay smoke
	# clicks it there).
	var chips := HBoxContainer.new()
	var ask_label := Label.new()
	ask_label.text = "Ask for:"
	ask_label.add_theme_color_override("font_color", Color(0.65, 0.75, 0.9))
	chips.add_child(ask_label)
	var chip := OptionButton.new()
	chip.add_item("Nothing, just talk", 0)
	chip.add_item("Ask for food", 1)
	chip.add_item("Buy food", 2)
	chip.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	chip.tooltip_text = "What your line asks for. They answer yes or no; a yes hands the food over."
	chips.add_child(chip)
	feed_box.add_child(chips)
	feed_box.move_child(chips, row.get_index())
	ui["talk_chip"] = chip
	chip.item_selected.connect(func(_i): _ask_hint())
	var hint := Label.new()  # the chip hint: shown only while a typed line reads like an ask and no chip is picked
	hint.add_theme_color_override("font_color", Color(0.95, 0.8, 0.45))
	hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hint.visible = false
	feed_box.add_child(hint)
	feed_box.move_child(hint, row.get_index())
	ui["talk_hint"] = hint
	# aipt3: whom Say sends to, or why it can't and that the line is kept. Always one line high, so the Talk row
	# under it doesn't move when it changes (a click meant for the list once hit "Plan ahead").
	var note := Label.new()
	note.add_theme_color_override("font_color", Color(0.65, 0.75, 0.9))
	note.clip_text = true
	note.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	feed_box.add_child(note)
	feed_box.move_child(note, row.get_index())
	ui["talk_note"] = note


func _ask_hint() -> void:
	var hint: Label = ui["talk_hint"]
	hint.text = Words.ask_hint(str(ui["talk_line"].text), int(ui["talk_chip"].selected), _typed())
	hint.visible = hint.text != ""


func _typed() -> bool:
	## text-1: whether this world gives on plain words (the welcome's talk.typed), so the chip hint would be false.
	return client != null and int(client.welcome.get("talk", {}).get("typed", 0)) == 1


var _menu_key := ""
var _caught_up := false


func _learn_names(v: Dictionary) -> void:
	for p in v.get("present", []) + v.get("kin", []):
		names[int(p["id"])] = p["name"]
		if p.has("role"):
			roles[int(p["id"])] = p["role"]


func _on_inhabit_welcome(m: Dictionary) -> void:
	ui["status"].text = ""
	names[int(m["world"]["me"]["id"])] = m["world"]["me"]["name"]
	for f in m["world"]["family"] + m["world"].get("people", []):
		names[int(f["id"])] = f["name"]
		roles[int(f["id"])] = f.get("role", "")
	_learn_names(m["view"])
	walk_place = m["view"].get("place")
	_refresh_inhabit()
	get_tree().create_timer(1.0).timeout.connect(_catch_up)  # the backlog follows the welcome in chunks


func _catch_up() -> void:
	## Today's earlier events from the welcome's backlog, once, so a paused or slow world isn't a blank feed.
	if _caught_up or client.view.is_empty():
		return
	_caught_up = true
	var v: Dictionary = client.view
	var day0 := int(v["tick"]) - int(v["tick"]) % client.ticks_per_day()
	_feed(client.seen_events.filter(func(e): return int(e["tick"]) >= day0))


func _feed(evs: Array) -> void:
	for e in evs:
		if e["kind"] == "reply" and e["data"].has("why") and not asked.get(int(e["actor"]), true):
			e = e.duplicate(true)  # why they can't give is said under an ask for food, not under small talk
			e["data"].erase("why")
		var line := Words.describe(e, client.me, names)
		if line != "":
			var at := "[color=#8a93a6]%s[/color]  %s\n" % [Words.clock(int(e["tick"]), client.ticks_per_day()).split(", ")[1], line]
			_append(ui["feed"], at)
			if e["kind"] == "reply" and str(e["data"].get("next", "")) == "leave" and e["data"].get("with") != null \
					and int(e["data"]["with"]) == int(client.me["id"]):
				leaving[int(e["actor"])] = int(e["tick"])  # pt11: not a Talk target while they walk off
			if e["kind"] in ["said", "reply", "deal", "ask"]:  # say-1: spoken words (talk-1: and deals, asks) go where talk is read
				_append(ui["said"], at)
				var words := str(e["data"].get("text" if e["kind"] == "said" else "says", ""))
				if words != "" and _caught_up:
					_bubble(int(e["actor"]), words, e["data"].get("with") != null and int(e["data"]["with"]) == int(client.me["id"]), 0.0)


func _append(panel: RichTextLabel, text: String) -> void:
	## aipt1 fix 5: the log panels didn't follow new lines; show the newest after each one.
	panel.append_text(text)
	panel.scroll_to_line(maxi(0, panel.get_line_count() - 1))


func _on_heard(m: Dictionary) -> void:
	var said_lines := []
	for x in m.get("lines", []):
		said_lines.append([x.get("who"), x.get("text")])
	var key := JSON.stringify(said_lines)
	if heard_recent.has(key):
		return  # aipt1 fix 5: 46 of 181 overheard lines repeated word for word; a repeat isn't shown again
	heard_recent.append(key)
	if heard_recent.size() > 40:
		heard_recent.pop_front()
	var line := Words.heard(m, client.ticks_per_day())
	if line != "":
		_append(ui["said"], line + "\n")
	var lines: Array = m.get("lines", [])
	for i in lines.size():  # one after another, as a conversation is spoken; thinking aloud gets no bubble
		if str(lines[i].get("to", "")) == "self":
			continue
		var to_me: bool = lines[i].get("to") != null and str(lines[i].get("to")) == str(client.me["id"])
		_bubble(int(lines[i]["who"]), str(lines[i]["text"]), to_me or (bool(m.get("to_player", false)) and not lines[i].has("to")), i * 2.0)


func _bubble(who: int, text: String, to_me: bool, delay: float) -> void:
	## V3: words over their speaker, for BUBBLE_S seconds (drawing only; the log keeps every line).
	bubbles = bubbles.filter(func(b): return b["who"] != who or b["born"] > Time.get_ticks_msec() / 1000.0)
	bubbles.append({"who": who, "text": text, "mine": who == int(client.me["id"]), "to_me": to_me,
			"born": Time.get_ticks_msec() / 1000.0 + delay})


func _on_inhabit_tick(m: Dictionary) -> void:
	var v: Dictionary = m["view"]
	_learn_names(v)
	var here := {}
	for p in v["present"]:
		here[int(p["id"])] = true
	for id in doing.keys():
		if not here.has(id):
			doing.erase(id)
	var evs: Array = m["events"]
	if not _caught_up:  # the first tick before the catch-up timer: today's backlog, this tick's included
		_caught_up = true
		var day0 := int(v["tick"]) - int(v["tick"]) % client.ticks_per_day()
		evs = client.seen_events.filter(func(e): return int(e["tick"]) >= day0)
	_feed(evs)
	_walk_lines(evs)
	for e in evs:
		if e.get("actor") != null and here.has(int(e["actor"])):
			match e["kind"]:
				"activity":
					doing[int(e["actor"])] = "came to " + str(Words.VERB.get(str(e["data"].get("activity")), e["data"].get("activity")))
				"talk":
					doing[int(e["actor"])] = "talking with " + Words._who(e["data"].get("with"), int(client.me["id"]), names)
				"eat":
					doing[int(e["actor"])] = "eating"
				"sleep":
					doing[int(e["actor"])] = "asleep"
	_take_receipts(evs)
	_plan_skip_failed()
	_lost_lines(evs)
	if say_once != "" and not v["asleep"]:
		for p in v["present"]:
			if not p.get("asleep", false):
				client.act("say", int(p["id"]), "", say_once)
				say_once = ""
				break
	if not pending.is_empty() and not pending.get("step", false) and int(v["tick"]) > int(pending["tick"]):
		pending = {}  # its receipt is in this tick's lines
	_plan_run()
	_refresh_inhabit()


func _on_early(m: Dictionary) -> void:
	## E4: the player's act applied mid-tick; its receipt is here now, not with the tick.
	if not _caught_up:
		return  # the catch-up on the first tick reads seen_events, these included
	_feed(m["events"])
	_walk_lines(m["events"])
	_take_receipts(m["events"])
	_lost_lines(m["events"])
	_plan_run()
	_refresh_inhabit()


func _on_ack(m: Dictionary) -> void:
	if client.scope != "inhabitant":
		ui["status"].text = "intent %s: %s" % [m["id"], m["status"]]
	elif m["status"] == "applied" and pending.get("id") == m["id"]:
		pending["event"] = int(m["event"])
		if pending.get("step", false):
			plan_act = int(m["event"])
			_take_receipts(client.seen_events.filter(func(e): return int(e["tick"]) >= int(pending["tick"])))
	elif m["status"] == "rejected" and pending.get("id") == m["id"]:
		_append(ui["feed"], "[color=#e07070]Not sent: %s (%s).[/color]\n" % [pending["label"], m.get("reason", "")])
		if pending.get("step", false):
			plan_state = "failed: " + str(m.get("reason", "rejected"))
		if pending.has("say_to"):  # aipt3: a refused line isn't dropped either
			_return_line(int(pending["say_to"]))
		pending = {}
		_refresh_inhabit()


func _take_receipts(evs: Array) -> void:
	## Receipts for the plan's step in hand; settle it, and send the next step when it's done.
	if plan_act < 0 or plan_q.is_empty():
		return
	for e in evs:
		if e["kind"] in ["receipt", "meet"] and not e["parents"].is_empty() and int(e["parents"][0]) == plan_act \
				and not plan_receipts.any(func(r): return r["id"] == e["id"]):
			plan_receipts.append(e)
	var st := Plan.settle(plan_q[0], plan_receipts, int(client.view.get("tick", 0)) - plan_sent)
	if st.begins_with("failed") and not plan_state.begins_with("failed"):
		plan_failed = int(client.view.get("tick", 0))
	plan_state = st
	if st != "wait" and pending.get("step", false):
		pending = {}
	if st == "done":
		var bought := Plan.bought(plan_q[0], plan_receipts)
		_plan_pop()
		if bought and not plan_q.is_empty() and plan_q[0]["action"] == "buy_food":
			_plan_pop()  # aipt1 fix 2: the market trip bought on arrival; a queued buy would only fail "not offered"


func _plan_skip_failed() -> void:
	## aipt1 fix 3: a failed step showed its reason for a tick, then stayed at the head and blocked the plan until
	## Clear. It now says why in the log and the plan goes on with the next step.
	if not plan_state.begins_with("failed") or plan_q.is_empty() or int(client.view.get("tick", 0)) <= plan_failed:
		return
	var why := plan_state.trim_prefix("failed: ")
	_append(ui["feed"], "[color=#e07070]Skipped in your plan: %s (%s).[/color]\n" % [plan_q[0]["label"], Words.WHY.get(why, why)])
	_plan_pop()


func _plan_pop() -> void:
	plan_q.pop_front()
	plan_state = ""
	plan_act = -1
	plan_receipts = []


func _plan_button(k: String) -> void:
	match k:
		"Next":
			if plan_state == "hold":
				_plan_pop()
		"Retry":
			if plan_state.begins_with("failed"):
				plan_state = ""
				plan_act = -1
				plan_receipts = []
		"Clear":
			plan_q = []
			plan_state = ""
			plan_act = -1
			plan_receipts = []
	_plan_run()
	_refresh_inhabit()


func _plan_run() -> void:
	## Sends the head step when nothing else is in flight and it hasn't been sent.
	if plan_q.is_empty() or plan_state != "" or not pending.is_empty() or client.view.is_empty() or client.view["asleep"]:
		return
	var s: Dictionary = plan_q[0]
	pending = {"id": client.act(s["action"], int(s["target"]), str(s["place"])), "label": s["label"],
			"tick": int(client.view["tick"]), "step": true}
	plan_state = "wait"
	plan_sent = int(client.view["tick"])


var _talk_key := ""
var _talk_to := -1


func _hearers() -> Array:
	## Who can hear you now: the people here and awake (the server checks again when it applies).
	var v: Dictionary = client.view
	var ids := []
	if not v.get("asleep", true):
		for p in v.get("present", []):
			# Someone who answered you and walked away is still listed for the tick they leave on: a line to them
			# would arrive after they'd gone (pt11, Steering 2026-11-18 23:52Z).
			if not p.get("asleep", false) and int(v["tick"]) - int(leaving.get(int(p["id"]), -99)) > 2:
				ids.append(int(p["id"]))
	return ids


func _refresh_talk() -> void:
	## Whom the line goes to is the Talk box's own rule (talk_box.gd): a pick stays the pick, and a line being
	## written keeps its listener.
	var v: Dictionary = client.view
	var ids := _hearers()
	var box: LineEdit = ui["talk_line"]
	var has_text := str(box.text).strip_edges() != ""
	var was := _talk_to
	_talk_to = talk.settle(ids, has_text)
	var who: OptionButton = ui["talk_who"]
	var key := JSON.stringify([ids, _talk_to])
	if key != _talk_key and not who.get_popup().visible:
		_talk_key = key
		who.clear()
		for i in ids:
			who.add_item(TalkBox.who(i, names, roles), i)
		who.select(who.get_item_index(_talk_to) if _talk_to >= 0 else -1)
	if not who.get_popup().visible:  # the list names each one's role; the button, the name alone (the note has both)
		who.text = str(names.get(_talk_to, "")) if _talk_to >= 0 else ("Pick who" if not ids.is_empty() else "")
	ui["talk_note"].text = talk.note(ids, has_text, names, roles)
	ui["talk_note"].tooltip_text = ui["talk_note"].text
	if was >= 0 and _talk_to < 0 and has_text and not box.has_focus():
		box.select_all_on_focus = true  # a line left waiting for someone who has gone: the next click selects it whole
	# aipt2 x5: Enter while a line was on its way kept the text, and the next words were typed onto it. The box
	# waits with the button until the line has landed. aipt3: with nobody to hear, it stays open, so a line typed
	# as they left is kept and can still be changed.
	var waiting: bool = not pending.is_empty() and pending.has("say_to")
	box.editable = not waiting
	box.placeholder_text = "Waiting to be heard..." if waiting else \
			("Nobody here to talk to." if ids.is_empty() else "Say something...")
	ui["talk_say"].disabled = _talk_to < 0 or not pending.is_empty()
	var chip: OptionButton = ui["talk_chip"]
	var price := int(v.get("food_price", 0))
	chip.set_item_text(2, "Buy food for %d coin" % price)
	chip.set_item_disabled(2, int(v.get("coin", 0)) < price)  # greyed when the coin is short
	if chip.selected == 2 and int(v.get("coin", 0)) < price:
		chip.select(0)
	chip.disabled = _talk_to < 0 or waiting


func _say() -> void:
	var box: LineEdit = ui["talk_line"]
	var text := str(box.text).strip_edges()
	if text == "":
		return
	_refresh_talk()
	var why := talk.unsent(_hearers(), names, roles, not pending.is_empty())
	if why != "":
		# aipt3: Say with nobody to hear (they had left, or the timetable had moved you) did nothing and said nothing,
		# and the next words were typed into the old line. Now the log says so, and the kept line is selected whole,
		# so typing replaces it and Say still sends it as it is.
		_append(ui["feed"], "[color=#e07070]%s[/color]\n" % why)
		box.select_all_on_focus = true
		box.release_focus()  # a focused box ignores select_all_on_focus: the next click must focus it anew
		box.select_all()
		return
	var t := _talk_to
	var chip: OptionButton = ui["talk_chip"]
	var move: String = ["", "asks_food", "wants_to_buy_food"][maxi(0, chip.selected)]
	var price := int(client.view.get("food_price", 0)) if move == "wants_to_buy_food" else -1
	pending = {"id": client.act("say", t, "", text, move, price), "label": "Say to %s" % names.get(t, "#%d" % t),
			"tick": int(client.view["tick"]), "say_to": t}
	asked[t] = move != "" or Words.reads_like_ask(text)
	chip.select(0)  # one ask per line: the next line just talks unless a chip is picked again
	last_said[t] = text
	box.text = ""
	box.select_all_on_focus = false
	_ask_hint()
	_refresh_inhabit()


func _return_line(t: int) -> void:
	## A line that was sent and not heard: back in the box, or, with a newer line there, written out in the log.
	if not last_said.has(t):
		return
	var box: LineEdit = ui["talk_line"]
	var back := TalkBox.lost(str(box.text), str(last_said[t]), str(names.get(t, "them")))
	if back["returned"]:
		# aipt2 x12: a click put the caret inside the old line and new words went into its middle. The
		# returned line is selected whole, and a click selects it again, so typing replaces it.
		box.text = back["box"]
		box.select_all_on_focus = true
		box.release_focus()  # a focused box ignores select_all_on_focus: the next click must focus it anew
		box.select_all()
		_ask_hint()
	_append(ui["feed"], "[color=#8a93a6]%s[/color]\n" % back["log"])
	last_said.erase(t)


func _lost_lines(evs: Array) -> void:
	## aipt1 fix 4: a line that didn't reach its listener goes back in the box (the log says why). aipt3: with a
	## newer line in the box it used to be dropped; now the log keeps its words.
	for e in evs:
		var d: Dictionary = e.get("data", {})
		if e["kind"] == "receipt" and e.get("actor") != null and int(e["actor"]) == int(client.me["id"]) \
				and str(d.get("action", "")) == "say" and not d.get("ok", false) and d.get("to") != null:
			_return_line(int(d["to"]))


func _choose(action: String, target: int, place: String, label: String) -> void:
	## A button or a map place: into the plan with "Plan ahead" on, else done now. A yes or no to an ask always
	## goes now: it has a few ticks before your habits answer (aipt1 fix 3).
	if ui["ahead"].button_pressed and not action in ["agree", "decline"]:
		if plan_q.size() < Plan.MAX_STEPS:
			plan_q.append(Plan.step(action, target, place, label))
		_plan_run()
	elif pending.is_empty():
		pending = {"id": client.act(action, target, place), "label": label, "tick": int(client.view["tick"])}
	_refresh_inhabit()


func _can_walk() -> bool:
	## Whether this world's engine takes `walk` (walk-1): the welcome lists the tiles it may name.
	return client != null and client.scope == "inhabitant" and client.welcome.get("verbs", {}).get("act", {}).has("to")


func _typing() -> bool:
	var f := get_viewport().gui_get_focus_owner()
	return f is LineEdit or f is TextEdit


func _me_tiles() -> Vector2:
	## Where your body is drawn, in tiles: where the keys have walked it, else where the engine has it.
	return walk_pos if walking else Vector2(client.view["pos"][0], client.view["pos"][1])


func _walk_keys(delta: float) -> void:
	## walk-1: W A S D or the arrows walk your body. The engine holds where it stands and moves it at most
	## `walk` tiles a tick; between ticks the body is drawn where the keys took it, at that same speed, and the
	## tile is sent as a `walk`. When the keys are let go and every walk is answered, the engine's tile is drawn.
	var v: Dictionary = client.view
	if v.is_empty() or not _can_walk():
		return
	var engine := Vector2(v["pos"][0], v["pos"][1])
	var dir := Vector2i.ZERO
	if not _typing() and not v.get("asleep", true) and not client.paused:
		dir.x = int(Input.is_physical_key_pressed(KEY_D) or Input.is_key_pressed(KEY_RIGHT)) \
				- int(Input.is_physical_key_pressed(KEY_A) or Input.is_key_pressed(KEY_LEFT))
		dir.y = int(Input.is_physical_key_pressed(KEY_S) or Input.is_key_pressed(KEY_DOWN)) \
				- int(Input.is_physical_key_pressed(KEY_W) or Input.is_key_pressed(KEY_UP))
	var now := Time.get_ticks_msec() / 1000.0
	var per_tick := int(client.welcome["world"]["map"]["walk"])
	if dir != Vector2i.ZERO:
		if not walking:
			walking = true
			walk_pos = engine
		e_items = []
		walk_pos = Keys.step(walk_pos, dir, Keys.speed(per_tick, client.tps) * delta, client.welcome["verbs"]["act"]["to"])
		if absf(walk_pos.x - engine.x) + absf(walk_pos.y - engine.y) > 2.5 * per_tick:
			walk_pos = engine  # the engine fell far behind what the screen showed: its tile stands
		_walk_want()
		if client.walk_want != null and now - walk_at >= Keys.SEND_S:
			client.flush_walk()
			walk_at = now
	elif walking:
		walk_pos = Vector2(Keys.tile(walk_pos))  # it stops on a tile
		_walk_want()
		if client.walk_want != null:
			client.flush_walk()
			walk_at = now
		elif client.walks.is_empty():
			walking = false  # every step is answered: the engine's tile is where it stands
			client.walk_sent = null


func _walk_want() -> void:
	var t := Keys.tile(walk_pos)
	var last = client.walk_sent if client.walk_sent != null else Keys.tile(Vector2(client.view["pos"][0], client.view["pos"][1]))
	client.walk_want = t if t != last else null


func _walk_lines(evs: Array) -> void:
	## A line when a walk takes you into a place or out of one, and the reason when the engine refuses a step.
	for e in evs:
		if e.get("actor") == null or int(e["actor"]) != int(client.me["id"]):
			continue
		if e["kind"] == "moved" and e["data"].get("place") != walk_place:
			walk_place = e["data"].get("place")
			_append(ui["feed"], "[color=#8a93a6]%s[/color]  %s\n" % [Words.clock(int(e["tick"]), client.ticks_per_day()).split(", ")[1],
					"You walk into %s." % Words.place(walk_place, client.me) if walk_place != null else "You step out into the open."])
		elif e["kind"] in ["arrive", "activity"] and e["data"].get("place") != null:
			walk_place = e["data"]["place"] if int(e["data"].get("arrive", e["tick"])) == int(e["tick"]) else null
		elif e["kind"] == "receipt" and str(e["data"].get("action", "")) == "walk" and not e["data"].get("ok", false):
			walking = false  # asleep, or bedtime: the engine has the body
			client.walk_want = null
			client.walk_sent = null


func _e_key() -> void:
	## E: what can be done here, with whoever stands nearest and at this place, picked with 1 to 9.
	if not e_items.is_empty():
		e_items = []
		return
	e_items = Keys.items(client.view, reach, names, _hearers().has(reach))


func _e_pick(i: int) -> void:
	if i < 0 or i >= e_items.size():
		return
	var it: Dictionary = e_items[i]
	e_items = []
	if it["action"] == "talk":
		_talk_with(int(it["target"]))
	else:
		_choose(it["action"], int(it["target"]), "", it["label"])


func _talk_with(t: int) -> void:
	## The Talk box on this villager, ready to type.
	if t >= 0 and _hearers().has(t):
		talk.pick(t)
		_talk_key = ""
		_refresh_talk()
	ui["talk_line"].grab_focus()


func _input(ev: InputEvent) -> void:
	# Esc leaves the Talk box, so the keys walk again (a focused box takes W, A, S and D as letters).
	if ev is InputEventKey and ev.pressed and ev.keycode == KEY_ESCAPE and _typing():
		get_viewport().gui_release_focus()
		get_viewport().set_input_as_handled()


func _unhandled_input(ev: InputEvent) -> void:
	if ev is InputEventKey and ev.pressed and (ev.ctrl_pressed or ev.meta_pressed) \
			and ev.keycode in [KEY_EQUAL, KEY_PLUS, KEY_KP_ADD, KEY_MINUS, KEY_KP_SUBTRACT, KEY_0]:
		# P-pass: Ctrl/Cmd + and - make everything larger or smaller, Ctrl/Cmd 0 goes back to the start size.
		ui_scale = 1.25 if ev.keycode == KEY_0 else clampf(ui_scale * (1.1 if ev.keycode in [KEY_EQUAL, KEY_PLUS, KEY_KP_ADD] else 1 / 1.1), 0.6, 3.0)
		_layout()
		get_viewport().set_input_as_handled()
		return
	if client != null and client.scope == "inhabitant" and ev is InputEventKey and ev.pressed and not ev.echo \
			and not (ev.ctrl_pressed or ev.meta_pressed or ev.alt_pressed) and _can_walk():
		var handled := true
		if ev.physical_keycode == KEY_E:
			_e_key()
		elif ev.keycode == KEY_ESCAPE:
			e_items = []
		elif ev.physical_keycode == KEY_T or ev.keycode in [KEY_ENTER, KEY_KP_ENTER]:
			e_items = []
			_talk_with(reach)
		elif ev.keycode >= KEY_1 and ev.keycode <= KEY_9 and not e_items.is_empty():
			_e_pick(ev.keycode - KEY_1)
		else:
			handled = false
		if handled:
			get_viewport().set_input_as_handled()
			return
	if client == null and ev is InputEventMouseButton and ev.pressed and ev.button_index == MOUSE_BUTTON_LEFT:
		picked = ""  # recorded mode: a click picks the place under it, or nothing
		for name in map_rects:
			if map_rects[name].has_point(ev.position):
				picked = name
		return
	if client == null or client.scope != "inhabitant" or not (ev is InputEventMouseButton) or not ev.pressed:
		return
	if ev.position.x >= map_w:
		return
	if ev.button_index in [MOUSE_BUTTON_WHEEL_UP, MOUSE_BUTTON_WHEEL_DOWN]:  # V4: zoom the map
		zoom = clampf(zoom * (1.15 if ev.button_index == MOUSE_BUTTON_WHEEL_UP else 1.0 / 1.15), 1.0, 4.0)
		get_viewport().set_input_as_handled()
		return
	if ev.button_index != MOUSE_BUTTON_LEFT:
		return
	get_viewport().gui_release_focus()  # a click on the map leaves the Talk box, so the keys walk again
	e_items = []
	for name in map_rects:
		if map_rects[name].has_point(ev.position):
			_choose("go_to", -1, name, "Go to " + Words.place(name, client.me))
			get_viewport().set_input_as_handled()
			return


func _refresh_inhabit() -> void:
	var v: Dictionary = client.view
	if v.is_empty():
		return
	var per_day := client.ticks_per_day()
	ui["you"].text = Words.status(v, client.me, per_day)
	ui["plan"].text = Words.plan(v, per_day, names)
	ui["here"].text = Words.here(v, doing)
	ui["asked"].text = Words.asks(v, names, 1.0 / maxf(client.tps, 0.001))  # what waits on your answer, if anything
	ui["myplan"].text = Plan.lines(plan_q, plan_state, Words.WHY)
	ui["plan_next"].disabled = plan_state != "hold"
	ui["plan_retry"].disabled = not plan_state.begins_with("failed")
	ui["plan_clear"].disabled = plan_q.is_empty()
	_rebuild_menu()
	_refresh_talk()


func _rebuild_menu() -> void:
	## The menu is the server's offer for this view, rebuilt only when it or the pending act changes.
	var v: Dictionary = client.view
	var items := LiveClient.menu(v)
	var cooling := int(v["tick"]) < int(v["cooldown_until"])
	var ahead: bool = ui["ahead"].button_pressed
	var key := JSON.stringify([items, pending.get("id"), cooling, v.get("asks", []), ahead])
	if key == _menu_key:
		return
	_menu_key = key
	var box: VBoxContainer = ui["menu"]
	for c in box.get_children():
		c.queue_free()
	for c in ui["answers"].get_children():
		c.queue_free()
	if ahead:  # planning: the acts that name nobody can be lined up from anywhere; the engine checks each when it comes up
		var row := HFlowContainer.new()
		var lab := Label.new()
		lab.text = "Plan from anywhere:"
		row.add_child(lab)
		for act in Plan.ANYWHERE:
			var pb := Button.new()
			pb.text = Words.action_label(act, "")
			pb.pressed.connect(_choose.bind(act, -1, "", Words.action_label(act, "")))
			row.add_child(pb)
		box.add_child(row)
		var hint := Label.new()
		hint.text = "Click a place on the map to walk there."
		box.add_child(hint)
	if items.is_empty():
		var l := Label.new()
		l.text = "Nothing to do while asleep." if v["asleep"] else "Nothing to choose right now."
		box.add_child(l)
	var present := {}
	for p in v["present"]:
		present[int(p["id"])] = p["kin"]
	var rows := {}  # one row of short buttons per person; the actions that name nobody come first
	var askers := {}
	for q in v.get("asks", []):
		askers[int(q["from"])] = true
	items = items.filter(func(x): return askers.has(int(x["target"]))) + \
			items.filter(func(x): return not askers.has(int(x["target"])))  # who's waiting on you comes first
	for it in items:
		var t := int(it["target"])
		if not Words.shown(it, present):
			continue
		var b := Button.new()
		b.text = Words.action_label(it["action"], names.get(t, "#%d" % t)) if t < 0 else Words.SHORT.get(it["action"], it["action"])
		b.disabled = not pending.is_empty() and not ahead
		if cooling and it["action"] in Words.RATED and not ahead:
			b.disabled = true
			b.tooltip_text = "Too soon after your last ask or purchase."
		var label := Words.action_label(it["action"], names.get(t, "#%d" % t))
		b.pressed.connect(_choose.bind(it["action"], int(it["target"]), "", label))
		if t < 0:
			b.alignment = HORIZONTAL_ALIGNMENT_LEFT
			box.add_child(b)
			continue
		if it["action"] in ["agree", "decline"]:  # the answer to an ask: its own row at the top (aipt1 fix 1)
			b.disabled = not pending.is_empty()  # never queued behind a plan: it goes now (aipt1 fix 3)
			if not rows.has("answer%d" % t):
				var who := Label.new()
				who.text = "Answer %s:" % names.get(t, "#%d" % t)
				who.add_theme_color_override("font_color", Color(0.94, 0.75, 0.38))
				rows["answer%d" % t] = HFlowContainer.new()
				rows["answer%d" % t].add_child(who)
				ui["answers"].add_child(rows["answer%d" % t])
			b.add_theme_color_override("font_color", Color(0.94, 0.75, 0.38))
			rows["answer%d" % t].add_child(b)
			continue
		if not rows.has(t):
			var who := Label.new()
			var tags := [roles.get(t, "")] if roles.get(t, "") != "" else []
			if present.get(t, false):
				tags.append("family")
			who.text = names.get(t, "#%d" % t) + (" (%s)" % ", ".join(tags) if not tags.is_empty() else "") + ":"
			rows[t] = HFlowContainer.new()
			rows[t].add_child(who)
			box.add_child(rows[t])
		rows[t].add_child(b)


func _wait_text() -> String:
	var left := 0.0 if client.paused else maxf(0.0, 1.0 / client.tps - (Time.get_ticks_msec() - client.tick_wall) / 1000.0)
	var next := "the world is paused" if client.paused else "next tick in %ds" % ceili(left)
	if not pending.is_empty():
		return "Sent: %s. It happens on the next tick (%s)." % [pending["label"], next]
	return "Each tick is 10 minutes of village time; %s. One action per tick." % next


func _toggle_play() -> void:
	if client:
		client.control("run" if client.paused else "pause")
	else:
		playing = not playing


func _scrub(v: float) -> void:
	if client:
		follow = int(v) >= client.view_tick
		scrub_tick = int(v)
	else:
		replay_tick = v


func _process(delta: float) -> void:
	if client:
		client.poll()
	elif playing:
		replay_tick = minf(replay_tick + delta * 6.0, model.last_tick + 1)
		if not announced and replay_tick >= 10.0:
			announced = true
			print("playback: playing, view tick %d" % int(replay_tick))
	if client == null:
		_refresh_picked()
	if client and client.scope == "inhabitant" and ui.has("wait"):
		ui["wait"].text = _wait_text()
		_walk_keys(delta)
	var newest := client.view_tick if client else model.last_tick + 1
	var s: HSlider = ui["slider"]
	s.max_value = newest
	s.set_value_no_signal(_tick_shown() if not (client and follow) else newest)
	ui["family"].max_value = maxi(0, model.families.size() - 1)
	var t := int(_tick_shown())
	ui["clock"].text = "day %d %02d:%02d (tick %d)" % [t / model.ticks_per_day, (t % model.ticks_per_day) / 6,
			(t % 6) * 10, t]
	queue_redraw()
	if not shot.is_empty():
		shot["frames"] -= 1
		if shot["frames"] == 0:
			get_viewport().get_texture().get_image().save_png(shot["path"])
			get_tree().quit()


func _tick_shown() -> float:
	if client:
		return client.display_tick() if follow else float(scrub_tick)
	return replay_tick


func _draw() -> void:
	if client and client.scope == "inhabitant":
		_draw_inhabitant()
		return
	if model == null or model.places.is_empty():
		return
	var size := get_viewport_rect().size
	if ui.has("picked"):
		size.x -= PICKED_W
	var lo := Vector2(INF, INF)
	var hi := Vector2(-INF, -INF)
	for p in model.places.values():
		var r = p["rect"]
		lo = lo.min(Vector2(r[0], r[1]))
		hi = hi.max(Vector2(r[0] + r[2], r[1] + r[3]))
	var k := minf((size.x - 40) / (hi.x - lo.x), (size.y - 100) / (hi.y - lo.y))
	var origin := (size - Vector2(0, 60) - (hi - lo) * k) / 2.0  # centred above the bar
	var to_screen := func(v: Vector2) -> Vector2: return (v - lo) * k + origin
	_draw_ground(model.places, to_screen, lo, hi, k)
	for name in model.places:
		var r = model.places[name]["rect"]
		var rect := Rect2(to_screen.call(Vector2(r[0], r[1])), Vector2(r[2], r[3]) * k)
		if not Art.place(self, name, rect, _tile_px(k)):
			draw_rect(rect, Color(0.25, 0.3, 0.25) if name.begins_with("home") else Color(0.3, 0.35, 0.45))
		if not name.begins_with("home"):
			Art.label(self, rect.position + Vector2(2, 12), name, 10)
	var t := _tick_shown()
	var at_anchor := {}  # a place's anchor -> its rect on screen: villagers there spread over the place
	for name in model.places:
		var r = model.places[name]["rect"]
		var an = model.places[name]["anchor"]
		at_anchor[Vector2(an[0], an[1])] = Rect2(to_screen.call(Vector2(r[0], r[1])), Vector2(r[2], r[3]) * k)
		map_rects[name] = at_anchor[Vector2(an[0], an[1])]
	if map_rects.has(picked):
		draw_rect(map_rects[picked].grow(2), Color(1, 0.9, 0.4), false, 2.0)
	var vpx := maxf(k * 1.6, 9.0)
	for a in model.agents:
		var aid := int(a["id"])
		var p := model.display_position(aid, t)
		var spot: Vector2 = Art.spot_in(at_anchor[p], aid, vpx) if at_anchor.has(p) else to_screen.call(p)
		var st = model.state_at(aid, int(t))
		Art.villager(self, spot, str(a.get("role", "")), vpx, false, st[3] == "sleep")


func _tile_px(k: float) -> float:
	return maxf(k * 2.5, 8.0)  # one 16 px ground tile covers 2.5 m


func _draw_ground(places: Dictionary, to_screen: Callable, lo: Vector2, hi: Vector2, k: float) -> void:
	## Grass under the whole map, and dirt paths from the plaza to every place (drawing only).
	var pad := Vector2(4, 4)
	Art.ground(self, Rect2(to_screen.call(lo - pad), (hi - lo + pad * 2) * k), _tile_px(k))
	if not places.has("plaza"):
		return
	var hub: Vector2 = to_screen.call(Vector2(places["plaza"]["anchor"][0], places["plaza"]["anchor"][1]))
	for name in places:
		if name != "plaza" and not name.begins_with("home"):  # homes sit on the grass: their paths were a web
			var a = places[name]["anchor"]
			Art.path(self, hub, to_screen.call(Vector2(a[0], a[1])), maxf(k * 1.2, 3.0))


func _draw_inhabitant() -> void:
	## Only what the server sent this villager: the map, itself and the people at its place.
	if client.places.is_empty() or client.view.is_empty():
		return
	var size := get_viewport_rect().size
	if ui.has("picked"):
		size.x -= PICKED_W
	var lo := Vector2(INF, INF)
	var hi := Vector2(-INF, -INF)
	for p in client.places.values():
		var r = p["rect"]
		lo = lo.min(Vector2(r[0], r[1]))
		hi = hi.max(Vector2(r[0] + r[2], r[1] + r[3]))
	var k0 := minf((map_w - 40.0) / (hi.x - lo.x), (size.y - 60) / (hi.y - lo.y))  # the whole map fits at zoom 1
	var k := k0 * zoom
	var origin := Vector2(20, 20)
	if zoom > 1.0:  # V4: the map follows you, centred in its column
		var me_m := _me_tiles()
		origin = Vector2(map_w / 2.0, (size.y - 30) / 2.0) - (me_m - lo) * k
		# Polish (aipt2: "the map is cropped at the left"): the camera stops at the village's edges, so near an
		# edge the map's border shows whole instead of the column running past it into half-drawn houses.
		var world := (hi - lo) * k
		var col := Vector2(map_w, size.y - 30)
		for i in 2:
			if world[i] + 40.0 <= col[i]:
				origin[i] = (col[i] - world[i]) / 2.0
			else:
				origin[i] = clampf(origin[i], col[i] - 20.0 - world[i], 20.0)
	var font := ThemeDB.fallback_font
	var kin_homes := {}
	for m in client.view.get("kin", []):
		kin_homes[str(m["home"])] = true
	var going := ""  # where the plan's walk, or the walk under way, is headed
	if not plan_q.is_empty() and plan_q[0]["action"] == "go_to":
		going = str(plan_q[0]["place"])
	elif client.view.get("route") != null:
		going = str(client.view["route"]["to"])
	map_rects = {}
	var tags := []  # [label, rect]: drawn last, over the people (P-pass)
	var to_screen := func(p: Vector2) -> Vector2: return (p - lo) * k + origin
	_draw_ground(client.places, to_screen, lo, hi, k)
	for name in client.places:
		var r = client.places[name]["rect"]
		var rect := Rect2((Vector2(r[0], r[1]) - lo) * k + origin, Vector2(r[2], r[3]) * k)
		map_rects[name] = rect.grow(2)
		if not Art.place(self, name, rect, _tile_px(k)):
			draw_rect(rect, Color(0.25, 0.3, 0.25) if name.begins_with("home") else Color(0.3, 0.35, 0.45))
		if name == going:
			draw_rect(rect, Color(0.5, 0.85, 0.6), false, 2.0)
		var mine: bool = name == str(client.me.get("home", ""))
		if mine or name == str(client.view.get("place")):
			draw_rect(rect, Color(0.95, 0.85, 0.4), false, 2.0)
		# Only the homes that are yours to know get a label (yours, your family's): at 100 villagers the
		# others' labels overlap into noise. Public places keep their names.
		var label: String = "you" if mine else ("family" if kin_homes.has(name) else ("" if name.begins_with("home") else name.replace("_", " ")))
		if label != "":
			tags.append([label, rect])
	var v: Dictionary = client.view
	var at := (_me_tiles() - lo) * k + origin
	var px := maxf(k * 2.2, 12.0)
	var here: Rect2 = map_rects.get(str(v.get("place")), Rect2(at - Vector2(px, px), Vector2(px, px) * 2)).grow(-2)
	var seen := _map_view().grow(-px)
	# Zoomed in, a big place runs off the column: spread the people over what shows. Not where the keys walk
	# the body (walk-1): there each one keeps a steady spot in the place, so you can walk up to them.
	if here.intersects(seen) and not _can_walk():
		here = here.intersection(seen)
	drawn_at = {}
	# Polish (aipt2: "the villagers cluster so I can't tell who is who"): a crowd draws at most CROWD_MAX people,
	# whoever you're talking with and whoever is speaking first, then family, then by id; the rest are a "+N" tag.
	var talking := -1
	if ui.has("talk_who"):
		talking = _talk_to
	var now := Time.get_ticks_msec() / 1000.0
	var speaking := {}
	for b in bubbles:
		if now - b["born"] < BUBBLE_S:
			speaking[int(b["who"])] = true
	var crowd: Array = v["present"].duplicate()
	crowd.sort_custom(func(x, y): return [int(x["id"]) != talking, not speaking.has(int(x["id"])), not x["kin"], int(x["id"])] \
			< [int(y["id"]) != talking, not speaking.has(int(y["id"])), not y["kin"], int(y["id"])])
	var extra := maxi(0, crowd.size() - CROWD_MAX)
	for p in crowd.slice(0, CROWD_MAX):
		var spot := Art.spot_in(here, int(p["id"]), px * 0.85)
		drawn_at[int(p["id"])] = spot - Vector2(0, px * 0.85)
		Art.villager(self, spot, str(p.get("role", "")), px * 0.85, false, bool(p.get("asleep", false)))
	if extra > 0:
		tags.append(["+%d more here" % extra, Rect2(here.position + Vector2(0, here.size.y + 22), Vector2(here.size.x, 0))])
	if drawn_at.has(talking):  # the one you're talking with, named over their head
		var who: String = str(names.get(talking, ""))
		if who != "":
			tags.append([who, Rect2(drawn_at[talking] - Vector2(0, 4), Vector2.ZERO)])
	Art.villager(self, at, str(client.me.get("role", "")), px, true, bool(v["asleep"]))
	reach = Keys.nearest(drawn_at, int(client.me["id"]), at - Vector2(0, px * 0.85), px * 3.5) if _can_walk() and v.get("place") != null else -1
	if reach >= 0:  # who E and T reach: a ring at their feet and their name
		draw_arc(drawn_at[reach] + Vector2(0, px * 0.85), px * 0.55, 0.0, TAU, 28, Color(0.55, 0.9, 1.0, 0.95), 2.0)
		if reach != talking:
			tags.append([str(names.get(reach, "")), Rect2(drawn_at[reach] - Vector2(0, 4), Vector2.ZERO)])
	drawn_at[int(client.me["id"])] = at - Vector2(0, px * 1.1)
	var night := night_shade(fmod(client.display_tick(), float(client.ticks_per_day())) * 24.0 / client.ticks_per_day())
	if night > 0.0:  # day and night (V-pass, left from #76): over the ground and people, under every word
		draw_rect(_map_view(), Color(0.04, 0.06, 0.2, night))
	_draw_tags(tags)
	_draw_bubbles()
	_draw_e_list()
	# The map's column ends at map_w: cover what a zoomed map draws beyond it, under the panels, and its foot.
	draw_rect(Rect2(map_w, 0, size.x - map_w, size.y), RenderingServer.get_default_clear_color())
	draw_rect(Rect2(0, size.y - 30, map_w, 30), RenderingServer.get_default_clear_color())
	Art.label(self, Vector2(12, size.y - 10), "WASD: walk   E: do   T: talk   wheel: zoom"
			if _can_walk() else "Click a place to walk there; scroll to zoom.", 14, Color(0.75, 0.82, 0.95))


func _draw_e_list() -> void:
	## The E list beside your body: what the server offers here, one key each.
	if e_items.is_empty():
		return
	var font := ThemeDB.fallback_font
	var fs := 15
	var head := "With %s" % str(names.get(reach, "")) if reach >= 0 and int(e_items[0]["target"]) == reach else "Here"
	var w := font.get_string_size(head, HORIZONTAL_ALIGNMENT_LEFT, -1, fs).x
	for i in e_items.size():
		w = maxf(w, font.get_string_size("%d  %s" % [i + 1, e_items[i]["label"]], HORIZONTAL_ALIGNMENT_LEFT, -1, fs).x)
	# Docked at the foot of the map's column, so it never covers you or whoever you're with.
	var view := _map_view()
	var box := Rect2(Vector2(6, 0), Vector2(minf(w + 20, map_w - 12.0), (e_items.size() + 1) * (fs + 6) + 10))
	box.position.y = maxf(4.0, view.end.y - box.size.y - 4.0)
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(0.08, 0.09, 0.13, 0.94)
	sb.border_color = Color(0.55, 0.9, 1.0, 0.9)
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(5)
	draw_style_box(sb, box)
	draw_string(font, box.position + Vector2(10, fs + 4), head, HORIZONTAL_ALIGNMENT_LEFT, -1, fs, Color(0.65, 0.75, 0.9))
	for i in e_items.size():
		draw_string(font, box.position + Vector2(10, (i + 2) * (fs + 6) + 2), "%d  %s" % [i + 1, e_items[i]["label"]],
				HORIZONTAL_ALIGNMENT_LEFT, -1, fs, Color(1, 0.97, 0.88))


static func night_shade(hour: float) -> float:
	## How dark the map is at this hour, 0 by day to 0.42 at night, with an hour and a half of dusk and dawn.
	## The names on the map and the speech bubbles are drawn over it, so the dark never hides a word (Steering
	## 2026-11-16 08:53Z: the tint must never hurt readability).
	const DARK := 0.42
	if hour >= 7.0 and hour <= 19.0:
		return 0.0
	if hour > 19.0 and hour < 20.5:
		return DARK * (hour - 19.0) / 1.5
	if hour > 5.5 and hour < 7.0:
		return DARK * (7.0 - hour) / 1.5
	return DARK


func _draw_tags(tags: Array) -> void:
	## P-pass (aipt2: "label map places in full"): a place's name in full on a dark tag over its top edge, kept
	## inside the map's column, never cut to the place's width or hidden under the crowd.
	var font := ThemeDB.fallback_font
	var fs := 15
	var view := _map_view()
	for t in tags:
		var rect: Rect2 = t[1]
		if not view.intersects(rect):
			continue
		var tw := font.get_string_size(t[0], HORIZONTAL_ALIGNMENT_LEFT, -1, fs).x
		var box := Rect2(Vector2(rect.get_center().x - tw / 2.0 - 5, rect.position.y - fs - 6), Vector2(tw + 10, fs + 6))
		box.position.x = clampf(box.position.x, 2.0, map_w - box.size.x - 2.0)
		box.position.y = clampf(box.position.y, 2.0, view.end.y - box.size.y - 2.0)
		var sb := StyleBoxFlat.new()
		sb.bg_color = Color(0.1, 0.1, 0.14, 0.78)
		sb.set_corner_radius_all(4)
		draw_style_box(sb, box)
		draw_string(font, box.position + Vector2(5, fs + 1), t[0], HORIZONTAL_ALIGNMENT_LEFT, -1, fs, Color(1, 0.97, 0.88))


func _map_view() -> Rect2:
	return Rect2(0, 0, map_w, get_viewport_rect().size.y - 30)  # the map's column above its hint line


func _draw_bubbles() -> void:
	## V3: each line over its speaker for BUBBLE_S seconds, fading out; a line to you is warm yellow.
	var now := Time.get_ticks_msec() / 1000.0
	bubbles = bubbles.filter(func(b): return now - b["born"] < BUBBLE_S)
	var font := ThemeDB.fallback_font
	var fs := 13
	# At most 4 at once, so a busy plaza stays readable: lines to you first, then yours, then the newest.
	var seen := _map_view()
	# Only over a speaker drawn inside the map's column (review of #76): the line is in "Said near you" anyway.
	var shown := bubbles.filter(func(b): return now >= b["born"] and drawn_at.has(b["who"]) and seen.has_point(drawn_at[b["who"]]))
	shown.sort_custom(func(x, y): return [int(x["to_me"]), int(x["mine"]), x["born"]] > [int(y["to_me"]), int(y["mine"]), y["born"]])
	var placed := []
	for b in shown:  # P-pass (aipt2): a warm ring on whoever is talking to you, so you can find them in a crowd
		if b["to_me"]:
			var a := clampf((BUBBLE_S - (now - b["born"])) / 1.0, 0.0, 1.0)
			draw_arc(drawn_at[b["who"]] + Vector2(0, 10), 13.0, 0.0, TAU, 28, Color(1.0, 0.82, 0.3, a), 2.5)
	for b in shown.slice(0, 4):
		var age: float = now - b["born"]
		var alpha := clampf((BUBBLE_S - age) / 1.0, 0.0, 1.0)
		var text: String = b["text"]
		if text.length() > 90:
			text = text.substr(0, 88) + "\u2026"
		var w := minf(font.get_string_size(text, HORIZONTAL_ALIGNMENT_LEFT, -1, fs).x, 200.0)
		var lines := font.get_multiline_string_size(text, HORIZONTAL_ALIGNMENT_LEFT, w, fs)
		var box := Rect2(drawn_at[b["who"]] - Vector2(w / 2.0 + 6, lines.y + 14), Vector2(w + 12, lines.y + 8))
		box.position.x = clampf(box.position.x, 4.0, map_w - box.size.x - 4.0)
		var y0 := box.position.y
		for other in placed:  # stack upward past any bubble already drawn, never over it
			if box.grow(2).intersects(other):
				box.position.y = other.position.y - box.size.y - 4
		placed.append(box)
		var stacked := box.position.y != y0  # a stacked bubble's tail would cross the ones below it
		var fill := Color(1.0, 0.93, 0.62, 0.95 * alpha) if b["to_me"] else (Color(0.86, 0.93, 1.0, 0.95 * alpha) if b["mine"] else Color(1, 1, 1, 0.95 * alpha))
		var tip: Vector2 = drawn_at[b["who"]] - Vector2(0, 4)
		if not stacked:
			draw_colored_polygon(PackedVector2Array([Vector2(tip.x - 5, box.end.y - 1), Vector2(tip.x + 5, box.end.y - 1), tip]), fill)
		draw_style_box(_bubble_box(fill, alpha), box)
		draw_multiline_string(font, box.position + Vector2(6, fs + 2), text, HORIZONTAL_ALIGNMENT_LEFT, w, fs, -1,
				Color(0.12, 0.1, 0.14, alpha))


func _bubble_box(fill: Color, alpha: float) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = fill
	sb.set_corner_radius_all(6)
	sb.border_color = Color(0.15, 0.12, 0.18, 0.8 * alpha)
	sb.set_border_width_all(1)
	return sb
