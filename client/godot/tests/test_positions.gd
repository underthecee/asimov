# Headless: the client's positions, rebuilt from a recorded log, equal the engine's at every tick boundary and at
# times inside a tick (space-1: a villager's slot at a place, a walker between tiles).
#   godot --headless --path client/godot --script res://tests/test_positions.gd -- FIXTURE_DIR
# FIXTURE_DIR comes from tests/make_fixture.py. Exits 1 on any mismatch.
extends SceneTree

const WorldModel := preload("res://scripts/world_model.gd")


func _init() -> void:
	var dir: String = OS.get_cmdline_user_args()[0]
	var model := WorldModel.new()
	for line in FileAccess.get_file_as_string(dir.path_join("events.jsonl")).split("\n", false):
		model.add_event(JSON.parse_string(line))
	var want: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(dir.path_join("expected.json")))
	var checked := 0
	var mid_walk := 0
	var bad := 0
	for i in want["ticks"].size():
		var t := int(want["ticks"][i])
		for aid in want["pos"][i].size():
			var p = want["pos"][i][aid]
			var exp := Vector2i(int(p[0]), int(p[1]))
			var got := model.position(aid, t)
			var s := model.state_at(aid, t)
			var exp_place = want["place"][i][aid]
			checked += 1
			if exp_place == null:  # the engine has this villager on a route
				mid_walk += 1
			if got != exp or s[1] != exp_place:
				bad += 1
				if bad <= 10:
					printerr("tick %d villager %d: client %s at %s, engine %s at %s" % [t, aid, got, s[1], exp, exp_place])
	# space-1 (B1.5): the same function at times inside a tick, where a walker is between tiles.
	var between := 0
	var slots := {}
	for row in want["frac"]:
		var t := float(row[0]) + float(row[1]) / 600.0
		var got := model.pos_at(int(row[2]), t)
		var exp := Vector2(float(row[3]), float(row[4]))
		if got != got.floor():
			between += 1
		if model.state_at(int(row[2]), int(row[0]))[2] == null:
			slots[got] = true
		if got.distance_to(exp) > 0.001:
			bad += 1
			if bad <= 10:
				printerr("time %d+%d/600 villager %d: client %s, engine %s" % [int(row[0]), int(row[1]), int(row[2]), got, exp])
	print("checked %d positions at %d boundaries, %d mid-walk, %d at times inside a tick (%d between tiles, %d slots), %d mismatches"
			% [checked, want["ticks"].size(), mid_walk, want["frac"].size(), between, slots.size(), bad])
	if model.events.is_empty() or mid_walk == 0 or between == 0 or slots.size() < 10:
		printerr("vacuous: no events, no villager caught mid-walk or between tiles, or too few slots")
		bad += 1
	quit(1 if bad else 0)
