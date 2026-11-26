# deal-1: the Trade panel's own rules, apart from the widgets (main.gd draws them): what each side may hold, the
# two sides an `offer` act carries, and a side in words. The engine checks every offer itself (who is in reach,
# who holds what, hands, terms), settles it by its own rule, and the villager answers in its own words.
extends RefCounted

const COUNTS := ["food", "coin"]  # sim.OFFER_COUNTS: what a side holds by count
const MAX := 999                  # sim.OFFER_MAX
const MAX_THINGS := 8             # sim.OFFER_THINGS: the most things one side names


static func by_kind(things: Array) -> Dictionary:
	## {kind: [ids, lowest first]} for a list of {id, kind}: what a body carries.
	var out := {}
	for t in things:
		var kind := str(t["kind"])
		if not out.has(kind):
			out[kind] = []
		out[kind].append(int(t["id"]))
	for kind in out:
		out[kind].sort()
	return out


static func carries(v: Dictionary, who: int) -> Array:
	## What the villager `who` has in its hands, as the view shows it for someone in reach ([] for anyone else).
	for p in v.get("present", []) + v.get("seen", []):
		if int(p["id"]) == who:
			return p.get("carries", [])
	return []


static func can(v: Dictionary, who: int) -> bool:
	## Whether the server offers a trade with `who` in this view (awake and in reach).
	for t in v.get("offer", {}).get("offer", []):
		if int(t) == who:
			return true
	return false


static func caps(v: Dictionary, who: int) -> Dictionary:
	## The most of each name a side can hold, {"give": {name: n}, "want": {name: n}}: yours is what you have;
	## theirs is any count of food and coin (their stores aren't yours to see) and the things in their hands.
	var give := {"food": mini(int(v.get("food", 0)), MAX), "coin": mini(int(v.get("coin", 0)), MAX)}
	var want := {"food": MAX, "coin": MAX}
	var mine := by_kind(v.get("carried", []))
	for kind in mine:
		give[kind] = mine[kind].size()
	var theirs := by_kind(carries(v, who))
	for kind in theirs:
		want[kind] = theirs[kind].size()
	return {"give": give, "want": want}


static func side(picked: Dictionary, held: Dictionary) -> Dictionary:
	## One side as the act carries it ({"food": n, "coin": n, "things": [ids]}, each part only when it holds
	## something) from the counts picked by name and the ids `held` by kind: the lowest ids of each kind first.
	var out := {}
	for k in COUNTS:
		var n := clampi(int(picked.get(k, 0)), 0, MAX)
		if n > 0:
			out[k] = n
	var ids := []
	var kinds := held.keys()
	kinds.sort()
	for kind in kinds:
		for id in held[kind].slice(0, maxi(0, int(picked.get(kind, 0)))):
			if ids.size() < MAX_THINGS:
				ids.append(int(id))
	if not ids.is_empty():
		out["things"] = ids
	return out


static func empty(s: Dictionary) -> bool:
	return int(s.get("food", 0)) <= 0 and int(s.get("coin", 0)) <= 0 and s.get("things", []).is_empty()


static func words(s: Dictionary, kinds: Dictionary) -> String:
	## A side in words, as the engine words it (sim.offer_side): "6 coin", "2 food and a flower". `kinds` names
	## each thing's kind by id; a thing it doesn't know is "something". "" for the empty side.
	var parts := []
	for k in COUNTS:
		if int(s.get(k, 0)) > 0:
			parts.append("%d %s" % [int(s[k]), k])
	var n := {}
	for id in s.get("things", []):
		var kind := str(kinds.get(int(id), "something"))
		n[kind] = int(n.get(kind, 0)) + 1
	var names := n.keys()
	names.sort()
	for kind in names:
		if kind == "something":
			parts.append("something" if n[kind] == 1 else "%d things" % n[kind])
		elif n[kind] == 1:
			parts.append("%s %s" % ["an" if kind[0] in "aeiou" else "a", kind])
		else:
			parts.append("%d %ss" % [n[kind], kind])
	if parts.size() < 2:
		return "" if parts.is_empty() else parts[0]
	return ", ".join(parts.slice(0, -1)) + " and " + parts[-1]


static func summary(give: Dictionary, want: Dictionary, who: String, kinds: Dictionary) -> String:
	## What the Offer button will send, in a line; "" when neither side holds anything.
	if empty(give) and empty(want):
		return ""
	if empty(want):
		return "Give %s %s, asking nothing back." % [who, words(give, kinds)]
	if empty(give):
		return "Ask %s for %s, offering nothing." % [who, words(want, kinds)]
	return "Offer %s %s for %s." % [who, words(give, kinds), words(want, kinds)]


static func learn(kinds: Dictionary, v: Dictionary) -> void:
	## Remember each thing's kind by id from a view, so a later line can name a thing that has changed hands.
	for t in v.get("things", []) + v.get("carried", []):
		kinds[int(t["id"])] = str(t["kind"])
	for p in v.get("present", []) + v.get("seen", []):
		for t in p.get("carries", []):
			kinds[int(t["id"])] = str(t["kind"])
