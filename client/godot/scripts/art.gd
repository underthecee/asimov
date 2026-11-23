# The client's look (V-pass, joon 2026-11-16): ground tiles, buildings and villagers drawn from the original
# pixel art in res://art (client/godot/tools/make_art.py). Drawing only: where a villager stands comes from the
# engine's events and views, never from here. Images load at runtime, so they need no editor import; when one
# is missing, the plain shapes it replaces are drawn instead.
extends RefCounted

const ROLE_TINT := {"farmer": Color(0.55, 0.78, 0.35), "guard": Color(0.4, 0.55, 0.9), "trader": Color(0.72, 0.5, 0.85),
		"crafter": Color(0.95, 0.62, 0.3), "child": Color(0.98, 0.85, 0.4), "elder": Color(0.75, 0.75, 0.8),
		"healer": Color(0.45, 0.85, 0.8)}
const LABEL := Color(1, 1, 1)
const SHADOW := Color(0.08, 0.07, 0.1, 0.85)

static var _tex := {}


static func tex(name: String) -> Texture2D:
	if not _tex.has(name):
		var path := "res://art/%s.png" % name
		if ResourceLoader.exists(path):  # an export (or a project the editor has opened) holds the imported image
			_tex[name] = load(path)
		else:  # a plain checkout: straight from the file
			var img := Image.load_from_file(ProjectSettings.globalize_path(path))
			_tex[name] = ImageTexture.create_from_image(img) if img != null else null
	return _tex[name]


static func roof_tint(name: String) -> Color:
	## A family's roof colour, steady per home: a hue from its name.
	var h := absi(hash(name + "~roof") * 2654435761) % 12  # 12 well-spread hues: neighbours rarely match
	return Color.from_hsv(float(h) / 12.0, 0.45, 0.92)


static func ground(ci: CanvasItem, area: Rect2, tile_px: float) -> void:
	var g := tex("grass")
	if g == null:
		ci.draw_rect(area, Color(0.2, 0.26, 0.2))
		return
	var n := Vector2i(ceili(area.size.x / tile_px), ceili(area.size.y / tile_px))
	for y in n.y:
		for x in n.x:
			ci.draw_texture_rect(g, Rect2(area.position + Vector2(x, y) * tile_px, Vector2.ONE * tile_px), false)


static func path(ci: CanvasItem, a: Vector2, b: Vector2, width: float) -> void:
	ci.draw_line(a, b, Color(0.77, 0.63, 0.43), width)
	ci.draw_line(a, b, Color(0.66, 0.52, 0.35), maxf(width * 0.35, 1.0))


static func _fill(ci: CanvasItem, rect: Rect2, name: String, tile_px: float) -> void:
	var t := tex(name)
	if t == null:
		return
	var n := Vector2i(ceili(rect.size.x / tile_px), ceili(rect.size.y / tile_px))
	for y in n.y:
		for x in n.x:
			var cell := Rect2(rect.position + Vector2(x, y) * tile_px, Vector2.ONE * tile_px).intersection(rect)
			if cell.size.x > 0 and cell.size.y > 0:
				ci.draw_texture_rect_region(t, cell, Rect2(Vector2.ZERO, cell.size / tile_px * 16.0))


static func _sprite(ci: CanvasItem, name: String, centre: Vector2, px: float, mod := Color.WHITE) -> void:
	var t := tex(name)
	if t != null:
		ci.draw_texture_rect(t, Rect2(centre - Vector2(px, px) / 2.0, Vector2(px, px)), false, mod)


static func place(ci: CanvasItem, name: String, rect: Rect2, tile_px: float) -> bool:
	## Draws one place; false if the art is missing (the caller draws its plain rect).
	if tex("grass") == null:
		return false
	var c := rect.get_center()
	var s := minf(rect.size.x, rect.size.y)
	if name.begins_with("home"):
		_sprite(ci, "house_base", c, s * 1.15)
		_sprite(ci, "house_roof", c, s * 1.15, roof_tint(name))
	elif name == "plaza":
		_fill(ci, rect, "cobble", tile_px)
		_sprite(ci, "well", c, s * 0.5)
	elif name == "market":
		_fill(ci, rect, "dirt", tile_px)
		for i in 3:
			_sprite(ci, "stall", rect.position + Vector2(rect.size.x * (i + 0.5) / 3.0, rect.size.y * 0.5), minf(rect.size.x / 3.0, rect.size.y) * 0.95)
	elif name == "workshop":
		_fill(ci, rect, "dirt", tile_px)
		_sprite(ci, "workshop", c, s * 1.1)
	elif name == "field":
		_fill(ci, rect, "crops", tile_px)
	elif name == "woods":
		_fill(ci, rect, "woodsfloor", tile_px)
		var rng := RandomNumberGenerator.new()
		rng.seed = 7  # the same trees every frame
		var tp := tile_px * 1.2
		for i in int(rect.size.x * rect.size.y / (tp * tp) * 0.6):
			_sprite(ci, "tree", rect.position + Vector2(rng.randf_range(tp / 2, rect.size.x - tp / 2), rng.randf_range(tp / 2, rect.size.y - tp / 2)), tp)
	else:
		_fill(ci, rect, "dirt", tile_px)
	return true


static func spot_in(rect: Rect2, id: int, px: float) -> Vector2:
	## Where to draw villager `id` standing somewhere in a place: a steady spot by id, inside the rect, so a
	## crowd spreads over the place instead of stacking (drawing only; the engine says only "at this place").
	var inner := rect.grow(-minf(px * 0.5, minf(rect.size.x, rect.size.y) * 0.3))
	var h := hash(id * 7919 + 13)
	return inner.position + Vector2(float(h % 997) / 997.0 * inner.size.x, float((h / 997) % 991) / 991.0 * inner.size.y) + Vector2(0, px * 0.4)


static func villager(ci: CanvasItem, at: Vector2, role: String, px: float, mine := false, asleep := false) -> void:
	## One villager standing at `at` (its feet). The player's own gets a marker above it.
	if tex("villager_base") == null:
		ci.draw_circle(at, px / 4.0, ROLE_TINT.get(role, Color.WHITE))
		return
	var c := at - Vector2(0, px * 0.4)
	ci.draw_circle(at, px * 0.22, Color(0, 0, 0, 0.25))  # a soft shadow
	var dim := Color(0.7, 0.7, 0.85) if asleep else Color.WHITE
	_sprite(ci, "villager_tint", c, px, ROLE_TINT.get(role, Color(0.9, 0.9, 0.9)) * dim)
	_sprite(ci, "villager_base", c, px, dim)
	if mine:
		_sprite(ci, "marker", c - Vector2(0, px * 0.75), px * 0.6)
	if asleep:
		label(ci, c + Vector2(px * 0.3, -px * 0.45), "z", 10)


static func thing(ci: CanvasItem, at: Vector2, kind: String, px: float, reach := false, yours := false) -> void:
	## things-1: one thing on its tile, drawn from shapes (no sprite yet): what grows is small and round, a
	## fixture is a block. One in reach gets the ring E and T give a villager; a fixture that is yours, a gold edge.
	var r := px * 0.28
	match kind:
		"berries":
			ci.draw_circle(at, r, Color(0.2, 0.45, 0.22))
			for d in [Vector2(-0.4, -0.2), Vector2(0.35, -0.3), Vector2(0.0, 0.35)]:
				ci.draw_circle(at + d * r, r * 0.28, Color(0.85, 0.2, 0.3))
		"flower":
			ci.draw_line(at + Vector2(0, r), at, Color(0.3, 0.6, 0.3), maxf(1.0, r * 0.2))
			for i in 5:
				ci.draw_circle(at + Vector2(cos(i * TAU / 5.0), sin(i * TAU / 5.0)) * r * 0.45, r * 0.3, Color(0.95, 0.6, 0.8))
			ci.draw_circle(at, r * 0.25, Color(0.98, 0.85, 0.3))
		"stall":
			ci.draw_rect(Rect2(at - Vector2(r * 1.3, r * 0.6), Vector2(r * 2.6, r * 1.3)), Color(0.55, 0.38, 0.22))
			ci.draw_rect(Rect2(at - Vector2(r * 1.5, r * 1.1), Vector2(r * 3.0, r * 0.55)), Color(0.85, 0.3, 0.25))
		"bench":
			ci.draw_rect(Rect2(at - Vector2(r * 1.1, r * 0.35), Vector2(r * 2.2, r * 0.7)), Color(0.6, 0.45, 0.28))
		"bed":
			ci.draw_rect(Rect2(at - Vector2(r * 1.1, r * 0.6), Vector2(r * 2.2, r * 1.2)), Color(0.75, 0.78, 0.9))
			ci.draw_rect(Rect2(at - Vector2(r * 1.1, r * 0.6), Vector2(r * 0.6, r * 1.2)), Color(0.95, 0.95, 1.0))
		_:
			ci.draw_circle(at, r * 0.6, Color(0.8, 0.8, 0.8))
	if yours:
		ci.draw_rect(Rect2(at - Vector2(r * 1.4, r * 0.9), Vector2(r * 2.8, r * 1.8)), Color(0.95, 0.85, 0.4), false, 1.5)
	if reach:
		ci.draw_arc(at, r * 1.7, 0.0, TAU, 24, Color(0.55, 0.9, 1.0, 0.95), 1.5)


static func label(ci: CanvasItem, at: Vector2, text: String, size := 11, col := LABEL, width := -1.0) -> void:
	## Text with a dark edge, readable on any ground and under the night tint.
	var font := ThemeDB.fallback_font
	for d in [Vector2(-1, 0), Vector2(1, 0), Vector2(0, -1), Vector2(0, 1)]:
		ci.draw_string(font, at + d, text, HORIZONTAL_ALIGNMENT_LEFT, width, size, SHADOW)
	ci.draw_string(font, at, text, HORIZONTAL_ALIGNMENT_LEFT, width, size, col)
