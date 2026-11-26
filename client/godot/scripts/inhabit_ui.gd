# Plain words for an inhabitant: what each event it witnessed means, its status, its plan, who is here and
# what each button does. Only reads what the server sent (the view, the witnessed events and the names in
# them); it never decides anything, and an act is still the server's offer, sent as it is.
extends RefCounted

# verbs-1: the words for each verb and for each reason a receipt can carry are the verb table's
# (asiimov/verbs.py), written out as verb_words.gd by `python -m asiimov.verbs`. Change them there.
const VerbWords := preload("res://scripts/verb_words.gd")
const Offer := preload("res://scripts/offer_box.gd")
const VERB := VerbWords.VERB
const ACTION := VerbWords.ACTION
const NOW := VerbWords.NOW
const SHORT := VerbWords.SHORT
const DOING := VerbWords.DOING
const WHY := VerbWords.WHY
const TO_YOU := VerbWords.TO_YOU
const ANSWERS := ["gave", "sold", "refused", "kin gave food", "kin refused"]  # an ask's yes or no
# What a villager did with your own act aimed at them, by name (%s): the outcome first, then the why.
const BY_THEM := VerbWords.BY_THEM
const ASKING := VerbWords.ASKING
const ASKED := VerbWords.ASKED
const HERE_ONLY := VerbWords.HERE_ONLY  # sim.INSTANT_STEPS: refused with the target elsewhere
const RATED := VerbWords.RATED  # held to the cooldown (sim.RATED_ACTS)
# things-1: the verbs that name a thing, and by kind a thing's name and the label of each verb it allows
# (asiimov/things.py). A kind with no label for a verb doesn't take it.
const THING_ACTS := VerbWords.THING_ACTS
const OFFER_ACTS := VerbWords.OFFER_ACTS
const THING_NAME := VerbWords.THING_NAME
const THING_USE := VerbWords.THING_USE
const THING_TAKE := VerbWords.THING_TAKE
const THING_DROP := VerbWords.THING_DROP
const THING_HAND := VerbWords.THING_HAND
const THING_GIFT := VerbWords.THING_GIFT


static func thing_items(v: Dictionary, names: Dictionary) -> Array:
	## things-1: what the server offers to do with a thing, each {label, action, target, thing}: use, take and
	## put down name the things; hand names who is in reach, once for each kind of thing carried. Straight
	## from the offer: nothing is added, and two of a kind show once.
	var kinds := {}
	for t in v.get("things", []) + v.get("carried", []):
		kinds[int(t["id"])] = str(t["kind"])
	var out := []
	var had := {}
	var offer: Dictionary = v.get("offer", {})
	for pair in [["use", THING_USE], ["take", THING_TAKE], ["drop", THING_DROP]]:
		for id in offer.get(pair[0], []):
			var kind: String = kinds.get(int(id), "")
			if pair[1].has(kind) and not had.has([pair[0], kind]):
				had[[pair[0], kind]] = true
				out.append({"label": pair[1][kind], "action": pair[0], "target": -1, "thing": int(id)})
	for c in v.get("carried", []):
		var kind := str(c["kind"])
		for t in offer.get("hand", []):
			if THING_HAND.has(kind) and not had.has(["hand", kind, int(t)]):
				had[["hand", kind, int(t)]] = true
				out.append({"label": THING_HAND[kind] % str(names.get(int(t), "them")), "action": "hand",
						"target": int(t), "thing": int(c["id"])})
	return out


static func _thing_receipt(d: Dictionary, me_id: int, names: Dictionary) -> String:
	## things-1: your own act on a thing, in words: what you did, or why it didn't work.
	var thing: String = THING_NAME.get(str(d.get("kind", "")), "it")
	var why := str(d.get("why", ""))
	if d.get("ok", false):
		match why:
			"took":
				return "You pick up %s." % thing
			"dropped":
				return "You put down %s." % thing
			"handed":
				return "[color=#80d080]You give %s to %s.[/color]" % [thing, _who(d.get("to"), me_id, names)]
			"picked":
				return "[color=#80d080]You pick from %s: %d more food.[/color]" % [thing, int(d.get("food", 0))]
	var said: String = WHY.get(why, why)
	return "Didn't work with %s: %s." % [thing, said]


static func shown(item: Dictionary, present: Dictionary) -> bool:
	## Whether a menu item gets a button. Asks and theft need the target here ("target not here"), and
	## asking as kin needs family here ("not kin"): those can only be refused, so they're left off. Seek,
	## avoid and the gifts work from anywhere (a gift walks to the target first). `present`: id -> kin.
	var t := int(item["target"])
	if t >= 0 and item["action"] in HERE_ONLY and not present.has(t):
		return false
	return item["action"] != "ask_kin" or present.get(t, false)


static func clock(tick: int, per_day: int) -> String:
	var m := (tick % per_day) * 1440 / per_day
	return "Day %d, %02d:%02d" % [tick / per_day + 1, m / 60, m % 60]


static func place(p, me: Dictionary) -> String:
	if p == null:
		return "on the road"
	if str(p) == str(me.get("home", "")):
		return "your home"
	return "the " + str(p).replace("_", " ")


static func hunger_word(h: int) -> String:
	if h < 300:
		return "full"
	if h < 550:
		return "a little hungry"
	if h < 800:
		return "hungry"
	return "starving"


static func _who(id, me_id: int, names: Dictionary, cap := false) -> String:
	if id == null:
		return "Someone" if cap else "someone"
	if int(id) == me_id:
		return "You" if cap else "you"
	return names.get(int(id), "Someone" if cap else "someone")


static func action_label(action: String, target_name: String) -> String:
	var s: String = ACTION.get(action, action.replace("_", " "))
	return s % target_name if "%s" in s else s


static func describe(e: Dictionary, me: Dictionary, names: Dictionary, kinds := {}) -> String:
	## One line for an event the villager witnessed, or "" for bookkeeping it needn't read. Another's act aimed
	## at this villager adds what that villager said of it, its own mind's words (the server's `says`).
	## deal-1: `kinds` names a thing's kind by id (offer_box.learn), for an offer or a deal that names things.
	var line := _line(e, me, names, kinds)
	if line == "" or e.get("kind") == "reply":  # a reply's `says` is its line already
		return line
	return line + said(e.get("data", {}))


static func said(d: Dictionary) -> String:
	## The quoted line a villager says with its act aimed at you, or "".
	var w = d.get("says")
	return "\n    [i]\"%s\"[/i]" % str(w).replace("[", "[lb]") if w is String and w != "" else ""


static func _line(e: Dictionary, me: Dictionary, names: Dictionary, kinds := {}) -> String:
	var me_id := int(me.get("id", -1))
	var d: Dictionary = e.get("data", {})
	var mine: bool = e.get("actor") != null and int(e["actor"]) == me_id
	var a := _who(e.get("actor"), me_id, names, true)
	match e["kind"]:
		"possess":
			return "You take over %s." % me.get("name", "your villager")
		"release":
			return "You let go. %s's own habits take over." % me.get("name", "Your villager")
		"wake":
			return "%s wake%s up." % [a, "" if mine else "s"]
		"sleep":
			return "%s go%s to sleep." % [a, "" if mine else "es"]
		"activity":
			if str(d.get("activity", "")) == "home":
				return "%s head%s home." % [a, "" if mine else "s"]
			if not d.has("activity"):  # seen from afar (reach-1's sight): where they go, not what for
				return "%s set%s off for %s." % [a, "" if mine else "s", place(d.get("place"), me)]
			var v: String = VERB.get(str(d.get("activity", "")), str(d.get("activity", "")))
			return "%s head%s off to %s at %s." % [a, "" if mine else "s", v, place(d.get("place"), me)]
		"arrive":
			return "%s arrive%s at %s." % [a, "" if mine else "s", place(d.get("place"), me)]
		"halt":  # reach-1: a walker called to, or spoken to, stops where it is and waits a little
			return "%s stop%s and wait%s." % [a, "" if mine else "s", "" if mine else "s"]
		"eat":
			if d.get("household") != null and mine:
				return "You eat from %s's food." % _who(d["household"], me_id, names)
			return "%s eat%s." % [a, "" if mine else "s"]
		"produce":
			if d.has("coin"):
				return "You earn %d coin for your work." % int(d["coin"])
			return "You harvest %d food." % int(d.get("food", 0))
		"sell":
			return "%s sell%s %d food at the market." % [a, "" if mine else "s", int(d.get("food", 0))]
		"tithe":
			return "You pay %d coin to the village chest." % int(d.get("coin", 0))
		"stock":
			return "You stock up for the winter."
		"sick":
			return "You fall sick." if mine else ""
		"recover":
			return "You get over the sickness." if mine else ""
		"talk":
			var line := "%s talk%s with %s." % [a, "" if mine else "s", _who(d.get("with"), me_id, names)]
			if not d.has("gossip"):  # reach-1: seen from too far to hear
				return line
			for g in d.get("gossip", []):
				var k := str(g[1])
				if k.begins_with("thief:"):
					line += " %s hears that %s is a thief." % [_who(g[0], me_id, names, true),
							_who(int(k.split(":")[1]), me_id, names)]
			return line
		"meet":
			return "%s come%s to see %s." % [a, "" if mine else "s", _who(d.get("with"), me_id, names)]
		"snub":
			return "%s turn%s away from %s." % [a, "" if mine else "s", _who(d.get("with"), me_id, names)]
		"fire":
			return "Fire! A family's food stores burn."
		"ask":
			var what: String = ASKED.get(str(d.get("action", "")), "asked you something")
			if d.get("to") != null and int(d["to"]) == me_id:
				return "%s %s. Give, or not now?" % [a, what]
			return "%s %s." % [a, what.replace("you", _who(d.get("to"), me_id, names))]
		"said":  # say-1: words spoken aloud, the player's own included; talk-1: and the chip it was said with
			if not d.has("text"):  # reach-1: seen speaking, from too far to hear the words
				return "%s says something to %s, too far off to hear." % [a, _who(d.get("with"), me_id, names)]
			var ask := ""
			match str(d.get("move", "")):
				"asks_food":
					ask = " (asking for 1 food)"
				"wants_to_buy_food":
					ask = " (offering %d coin for 1 food)" % int(d.get("price", 0))
			return "%s say%s to %s: \u201c%s\u201d%s" % [a, "" if mine else "s", _who(d.get("with"), me_id, names),
					str(d.get("text", "")).replace("[", "[lb]"), ask]
		"offered":  # deal-1: an offer put to a villager; they answer in their own words, and a yes makes the deal
			var to := _who(d.get("with"), me_id, names)
			if not d.has("gives"):  # seen from too far to hear the terms
				return "%s offer%s %s a trade." % [a, "" if mine else "s", to]
			if Offer.empty(d["gives"]):
				return "%s ask%s %s for %s." % [a, "" if mine else "s", to, Offer.words(d["gets"], kinds)]
			return "%s offer%s %s %s for %s." % [a, "" if mine else "s", to, Offer.words(d["gives"], kinds),
					Offer.words(d["gets"], kinds)]
		"deal":  # talk-1: what a yes moved, or why it moved nothing
			return deal_line(d, me_id, names, kinds)
		"reply":  # say-1: a villager's answer to what was said to it
			if not d.has("says"):  # reach-1: seen answering, from too far to hear
				return "%s answers %s, too far off to hear." % [a, _who(d.get("with"), me_id, names)]
			var w := str(d.get("says", ""))
			if w == "":
				return "%s doesn't answer." % a
			var to_you: bool = d.get("with") != null and int(d["with"]) == me_id
			var line := "%s answers%s: \u201c%s\u201d" % [a, "" if to_you else " " + _who(d.get("with"), me_id, names),
					w.replace("[", "[lb]")]
			if to_you and str(d.get("next", "")) == "leave":  # pt11: 5 of 7 lost lines went to someone who had just left
				line += " %s walks away." % a
			var cant := why_not(d, a) if to_you else ""
			if cant != "":  # aipt3: 22 of 46 typed asks went to someone with nothing to spare, and no reply said so
				return "[color=#f0c060]%s[/color]\n    [color=#8a93a6]%s[/color]" % [line, cant]
			return "[color=#f0c060]%s[/color]" % line if to_you else line
		"plan_receipt":
			return "You plan your day." if d.get("ok", false) else ""
		"receipt":
			return _receipt(e, d, mine, a, me, names)
	return ""  # act (the button press itself), day_rhythm, intent_* bookkeeping


static func _receipt(e: Dictionary, d: Dictionary, mine: bool, a: String, me: Dictionary, names: Dictionary) -> String:
	var me_id := int(me.get("id", -1))
	var why := str(d.get("why", ""))
	var said: String = WHY.get(why, why)
	if "%d" in said:
		said = said % int(d.get("food", 0))
	var doing: String = DOING.get(str(d.get("action", "")), str(d.get("action", "")).replace("_", " "))
	if not mine:  # someone's act aimed at this villager; its mind decided the ask, and your villager's nature the answer
		var line := "%s %s" % [a, ASKED.get(str(d.get("action", "")), "tried to " + doing + " with you")]
		var answer: String = TO_YOU.get(why, "")
		if why in ANSWERS and not d.has("answered"):  # nobody pressed yes or no: the villager's habits answered
			answer = "you didn't answer, so your habits did: " + answer
		return line + (": " + answer + "." if answer != "" else ".")
	if str(d.get("action", "")) in THING_ACTS:
		return _thing_receipt(d, me_id, names)
	if str(d.get("action", "")) in OFFER_ACTS:  # deal-1
		if d.get("ok", false):
			return ""  # the offer's own line says it, or the gift's deal
		return "[color=#e07070]Your offer to %s wasn't made: %s.[/color]" % [
				_who(d["to"], me_id, names) if d.get("to") != null else "them", said]
	if e.get("origin") == "rule" and why in ["heading to market", "started", "waited"] and d.get("ok", false):
		return ""  # the activity line that follows says it
	if str(d.get("action", "")) == "say" and not d.get("ok", false):  # aipt1: a lost line is said in the log
		var gone := _who(d.get("to"), me_id, names) if d.get("to") != null else "them"
		match why:
			"target not here":
				return "[color=#e07070]%s has gone; your words didn't reach them.[/color]" % gone.capitalize()
			"target asleep":
				return "[color=#e07070]%s is asleep and didn't hear you.[/color]" % gone.capitalize()
	if str(d.get("action", "")) == "go_to":  # go-to-1: where you're walking, and when you'll be there
		var where := place(d.get("place"), me)
		if why == "arrived":
			return ""  # the arrive event says it
		if why == "already here":
			return "You're already at %s, so you stay." % where
		var mins := (int(d.get("arrive", e["tick"])) - int(e["tick"])) * 10
		return "You set off for %s; you'll be there in about %d minutes." % [where, mins]
	var whom = d.get("to", d.get("victim"))  # a theft's receipt names its victim
	if whom != null and int(whom) != me_id and why in ["gave", "sold"]:
		var who := _who(whom, me_id, names)
		match str(d.get("action", "")):
			"give_food":
				return "You gave %d food to %s." % [int(d.get("food", 1)), who]
			"give_coin":
				return "You gave %d coin to %s." % [int(d.get("coin", 1)), who]
			"buy_from":
				return "%s sold you food." % who
			"ask_food", "ask_kin":
				return "You asked %s for food. %s gave you some." % [who, who]
	if whom != null and int(whom) != me_id and why in BY_THEM:
		var them := _who(whom, me_id, names)
		var lead: String = ASKING.get(str(d.get("action", "")), "")
		return ((lead % them + ". ") if lead != "" and why not in ["stolen", "caught"] else "") + BY_THEM[why] % them
	if d.get("to") != null and int(d["to"]) != me_id:  # whom it named, in the line's own words
		var whom_w := _who(d["to"], me_id, names)
		var with_whom: String = VerbWords.DOING_WHOM.get(str(d.get("action", "")), "")
		doing = with_whom % whom_w if with_whom != "" else "%s with %s" % [doing, whom_w]
	return ("Done: %s, %s." if d.get("ok", false) else "Didn't work: %s, because %s.") % [doing, said]


# aipt3 (Steering 2026-11-20 09:07Z): why a villager could give nothing, as the engine told that villager's own
# prompt (sim.TALK_WHY), with the persons turned round for the one who asked. The keys are TALK_WHY's and
# tests/test_text1.py pins each line beside the engine's. A villager's own no has no line here: its words say it.
const WHY_NOT := {"food": "%s has no food to spare", "coin": "you don't have %d coin",
		"terms": "%s is on bad terms with you", "said_yes": "%s already said yes to you today",
		"wait": "%s turned you down not long ago", "apart": "%s is not with you any more"}


static func why_not(d: Dictionary, name: String) -> String:
	## The line under a villager's answer to you when the engine ruled a yes out, or "".
	var why := str(d.get("why", ""))
	if not WHY_NOT.has(why):
		return ""
	var s: String = WHY_NOT[why]
	s = s % int(d.get("price", 0)) if "%d" in s else s % name
	return s[0].to_upper() + s.substr(1) + "."


const DEAL_WHY := {"not together": "you're no longer together", "asleep": "they fell asleep",
		"nothing to spare": "they have nothing to spare now", "no coin": "you don't have the coin now"}


static func _offer_deal(d: Dictionary, me_id: int, names: Dictionary, kinds: Dictionary) -> String:
	## deal-1: an offer's deal. `from` made the offer and `to` answered it; both sides moved at once, or the
	## yes could no longer be had and nothing moved (`why`, a reason of the offer verb's own).
	var mine: bool = d.get("from") != null and int(d["from"]) == me_id
	var a := _who(d.get("from"), me_id, names, true)
	var b := _who(d.get("to"), me_id, names)
	var g: Dictionary = d.get("gives", {})
	var w: Dictionary = d.get("gets", {})
	if str(d.get("why", "")) == "gone":  # the offerer left before the answer: none was given
		return "[color=#e07070]%s didn't answer %s offer: %s.[/color]" % [_who(d.get("to"), me_id, names, true),
				"your" if mine else _who(d.get("from"), me_id, names) + "'s", WHY.get("gone", "gone") if mine else "they had gone"]
	if not d.get("ok", false):
		var why: String = WHY.get(str(d.get("why", "")), str(d.get("why", "")))
		return "[color=#e07070]%s said yes, but nothing changed hands: %s.[/color]" % [_who(d.get("to"), me_id, names, true), why]
	var line := "%s and %s make a trade." % [a, b]
	if Offer.empty(w) and not Offer.empty(g):  # a gift: nothing asked back
		line = "%s gave %s %s." % [a, b, Offer.words(g, kinds)]
	elif Offer.empty(g) and not Offer.empty(w):  # an ask: nothing offered
		line = "%s gave %s %s." % [_who(d.get("to"), me_id, names, true), _who(d.get("from"), me_id, names), Offer.words(w, kinds)]
	elif not Offer.empty(g):
		line = "%s traded with %s: %s gave %s and got %s." % [a, b, "you" if mine else "they", Offer.words(g, kinds), Offer.words(w, kinds)]
	return "[color=#80d080]%s[/color]" % line if mine else line


static func deal_line(d: Dictionary, me_id: int, names: Dictionary, kinds := {}) -> String:
	## talk-1: "Vone gave you 1 food", "You bought 1 food from Vone for 6 coin", or why a yes didn't settle.
	if str(d.get("move", "")) == "offer":
		return _offer_deal(d, me_id, names, kinds)
	var giver := _who(d.get("from"), me_id, names, true)
	var to := _who(d.get("to"), me_id, names)
	var buy: bool = str(d.get("move", "")) == "wants_to_buy_food"
	if not d.get("ok", false):
		var why: String = DEAL_WHY.get(str(d.get("why", "")), str(d.get("why", "")))
		return "[color=#e07070]%s said yes, but nothing changed hands: %s.[/color]" % [giver, why]
	if d.get("to") != null and int(d["to"]) == me_id:
		if buy:
			return "[color=#80d080]You bought 1 food from %s for %d coin.[/color]" % [giver, int(d.get("coin", 0))]
		return "[color=#80d080]%s gave you 1 food.[/color]" % giver
	if buy:
		return "%s sold 1 food to %s for %d coin." % [giver, to, int(d.get("coin", 0))]
	return "%s gave 1 food to %s." % [giver, to]


# talk-1's chip hint (Steering 2026-11-19 02:22Z): 32B replies to chipless asks claimed a handover in .27 of samples
# (bar .10), so the say box suggests the chip when a typed line reads like an ask. A client hint only: the line is
# sent as typed, and the engine never reads the words for consent.
const HINT_FOOD := "\\b(food|eat|eaten|eating|meal|meals|bite|loaf|loaves|bread|crust|supper|breakfast|dinner|hungry|famished|starving|starve|snack|feed me)\\b"
const HINT_BUY := "\\b(buy|sell|pay|paid|coin|coins|trade|price)\\b"
const HINT_ASK := "\\?|\\b(can|could|would|will|give|spare|share|hand|let|need|please|any chance|have)\\b"
const HINT_ASK_TEXT := "Asking for food? Pick \"Ask for food\" above. Without it this line only talks, and nothing changes hands."
const HINT_BUY_TEXT := "Buying food? Pick \"Buy food\" above. Without it this line only talks, and nothing changes hands."


static func _has(pattern: String, text: String) -> bool:
	return RegEx.create_from_string("(?i)" + pattern).search(text) != null


static func ask_hint(text: String, chip: int, typed := false) -> String:
	## The hint under the chip row while "Nothing, just talk" is picked and the typed line reads like an ask.
	## Quiet in a typed world (text-1; aipt3: there it was false, plain words had just been given food).
	if typed or chip != 0 or not _has(HINT_FOOD, text):
		return ""
	if _has(HINT_BUY, text):
		return HINT_BUY_TEXT
	return HINT_ASK_TEXT if _has(HINT_ASK, text) else ""


static func reads_like_ask(text: String) -> bool:
	## Whether a typed line reads like asking for food or to buy it: the chip hint's own reading. For display only
	## (whether "they have no food to spare" is worth a line under the answer); the engine never sees it.
	return _has(HINT_FOOD, text) and (_has(HINT_ASK, text) or _has(HINT_BUY, text))


static func status(v: Dictionary, me: Dictionary, per_day: int, got_past: bool = false) -> String:
	## `got_past`: an act of yours has gone through since the last one that failed, so a "could not" still on the
	## body's mind is old news and isn't shown.
	var lines := ["[b]%s[/b], %s" % [me.get("name", "?"), me.get("role", "")], clock(int(v["tick"]), per_day)]
	if v["asleep"]:
		lines.append("Asleep at %s." % place(v["place"], me))
	elif v.get("route") != null:
		lines.append("Walking to %s, there at %s." % [place(v["route"]["to"], me),
				clock(int(v["route"]["arrive"]), per_day).split(", ")[1]])
	elif v["place"] == null:  # walk-1: you walked out of every place
		lines.append("Out in the open, between places.")
	else:
		lines.append("%s at %s." % [NOW.get(str(v["activity"]), str(v["activity"])), place(v["place"], me)])
	lines.append("Hunger %d/1000, %s." % [int(v["hunger"]), hunger_word(int(v["hunger"]))])
	lines.append("You carry %d food and %d coin." % [int(v["food"]), int(v["coin"])])
	if not v.get("carried", []).is_empty():  # things-1: what is in your hands
		var held := []
		for c in v["carried"]:
			held.append(str(THING_GIFT.get(str(c["kind"]), c["kind"])))
		lines.append("In your hands: %s." % ", ".join(held))
	var price := int(v.get("food_price", 0))
	if price > 0:
		lines.append("Food costs %d coin at the market; your coin buys %d." % [price, int(v["coin"]) / price])
	var need := needs(v)
	if need != "":
		lines.append("[color=#f0c060]%s[/color]" % need)
	if v.has("market_food"):
		lines.append("The market has %d food." % int(v["market_food"]))
	if v.get("occasion") != null and not (got_past and str(v["occasion"].get("why", "")) == "failed"):
		lines.append("[color=#f0c060]On your mind: %s.[/color]" % no_ids(str(v["occasion"]["text"])))
	return "\n".join(lines)


static func no_ids(text: String) -> String:
	## The engine's own " (#12)" after a name is for minds, not for the player (aipt1 0060).
	var re := RegEx.new()
	re.compile(" ?\\(#\\d+\\)")
	return re.sub(text, "", true)


static func needs(v: Dictionary) -> String:
	## What this body needs now, from its own numbers: food first, as its mind would see it.
	var price := int(v.get("food_price", 0))
	var h := int(v["hunger"])
	if int(v["food"]) == 0 and h >= 550:
		if price > 0 and int(v["coin"]) >= price:
			return "You need food: buy some at the market, or ask someone."
		return "You need food and can't pay for it: ask family or someone here, or work for coin."
	if int(v["food"]) == 0:
		return "You have no food left for later."
	return ""


static func intent(v: Dictionary, names: Dictionary) -> String:
	## Its own mind's current intent: what this body meant to do, with the step it is on marked.
	var it = v.get("intent")
	if it == null:
		return ""
	var steps := []
	for i in it["steps"].size():
		var st: Dictionary = it["steps"][i]
		var who: String = "" if st.get("target") == null else names.get(int(st["target"]), "someone")
		var txt := str(VERB.get(str(st["do"]), st["do"]))
		if str(st["do"]) in ACTION:
			txt = action_label(str(st["do"]), who)
			txt = txt[0].to_lower() + txt.substr(1)
		steps.append("[b]%s[/b]" % txt if i == int(it["i"]) else txt)
	return "You meant to %s: %s." % [str(it["goal"]).trim_suffix("."), " → ".join(steps)]


static func plan(v: Dictionary, per_day: int, names := {}) -> String:
	var meant := intent(v, names)
	if v["plan"].is_empty():
		return (meant + "\n" if meant != "" else "") + "No plan for today yet."
	var hour := float(int(v["tick"]) % per_day) * 24.0 / per_day
	var out := []
	for b in v["plan"]:
		var line := "%02d–%02d  %s" % [int(b["start_h"]), int(b["end_h"]), VERB.get(str(b["activity"]), b["activity"])]
		out.append("[b]> %s[/b]" % line if hour >= float(b["start_h"]) and hour < float(b["end_h"]) else "   " + line)
	if meant != "":
		out.push_front(meant)
	return "\n".join(out)


static func asks(v: Dictionary, names: Dictionary, secs_per_tick: float) -> String:
	## Asks waiting on this villager's answer (player-ask-1), with how long is left. talk-1 (design §2.4): left
	## unanswered while you play, it's a no; only a body you've left idle answers by its habits.
	var out := []
	for q in v.get("asks", []):
		var left := int(q["until"]) - int(v["tick"])
		out.append("[color=#f0c060][b]%s %s.[/b]%s\nGive, or not now? You have %d tick%s (about %ds); no answer counts as not now.[/color]"
				% [q["name"], ASKED.get(str(q["action"]), "asked you something"), said(q), left, "" if left == 1 else "s",
				int(left * secs_per_tick)])
	return "\n".join(out)


static func in_reach(v: Dictionary) -> Array:
	## reach-1: those the body can speak to and act on though they share no place with it: within a tile of it,
	## across open ground. The server marks them (`reach`) in `seen`.
	return v.get("seen", []).filter(func(p): return p.get("reach", false))


static func near(v: Dictionary) -> Dictionary:
	## id -> kin, for everyone at the body's place or in reach of it: whom an act that needs its target here may name.
	var kin := {}
	for m in v.get("kin", []):
		kin[int(m["id"])] = true
	var out := {}
	for p in v.get("present", []):
		out[int(p["id"])] = bool(p.get("kin", false))
	for p in in_reach(v):
		out[int(p["id"])] = kin.has(int(p["id"]))
	return out


static func waiting(v: Dictionary, halts: Dictionary) -> Array:
	## Those who stopped for you and still stand out of reach: `halts` is id -> the tick they walk on (from the
	## `halt` events). Each is in sight, not at your place and not within a tile of you. Ids, in the view's order.
	var out := []
	for p in v.get("seen", []):
		if halts.has(int(p["id"])) and int(v["tick"]) < int(halts[int(p["id"])]) and not p.get("reach", false):
			out.append(int(p["id"]))
	return out


static func here(v: Dictionary, doing: Dictionary, halts: Dictionary = {}, names: Dictionary = {}, per_day: int = 144) -> String:
	var close := in_reach(v)
	var out := []
	for id in waiting(v, halts):  # the one you called to: named, and where to go, above everyone else
		out.append("[color=#8ce6ff]%s has stopped and waits for you until %s. Walk up to them.[/color]" % [
				str(names.get(id, "Someone")).replace("[", "[lb]"), clock(int(halts[id]), per_day).split(", ")[1]])
	if v["present"].is_empty() and close.is_empty():
		if not out.is_empty():
			return "\n".join(out)
		if v["place"] != null:
			return "Nobody else is here."
		if not v.get("offer", {}).get("hail", []).is_empty():
			return "Someone is walking by. Press T to call out to them."
		return "You're on the road." if v.get("route") != null else "Nobody: you're out in the open. Walk into a place to meet people."
	var kin := near(v)
	for p in v["present"] + close:
		var tags := []
		if kin.get(int(p["id"]), false):
			tags.append("family")
		if p.get("spare", p.get("has_food", false)):  # food beyond the one they keep: what an ask or a trade can reach
			tags.append("has food")
		if p.get("reach", false):
			tags.append("beside you")
		if halts.has(int(p["id"])) and int(v["tick"]) < int(halts[int(p["id"])]):
			tags.append("waiting for you")
		var line := "%s, %s" % [p["name"], p["role"]]
		if not tags.is_empty():
			line += " (" + ", ".join(tags) + ")"
		if doing.has(int(p["id"])):
			line += ": " + doing[int(p["id"])]
		out.append(line)
	return "\n".join(out)



static func heard(m: Dictionary, per_day: int) -> String:
	## Words spoken near you (readable-world E2): who said what, quoted; what was said to you in the ask colour.
	## A villager's words are text, never markup.
	var t := clock(int(m["tick"]), per_day).split(", ")[1]
	var out := []
	for x in m.get("lines", []):
		var who := str(x["name"]).replace("[", "[lb]")
		var text := str(x["text"]).replace("[", "[lb]")
		if str(x.get("to", "")) == "self":  # pt8: thinking aloud, set apart: grey, in italics, no quotes
			out.append("[color=#8a93a6][i]%s, to themself: %s[/i][/color]" % [who, text])
			continue
		var to := ""
		if x.get("to_name") != null and str(x.get("to_name", "")) != "":  # pt8: every line says whom it is to
			to = " \u2192 " + ("you" if bool(x.get("to_me", false)) else str(x["to_name"]).replace("[", "[lb]"))
		var line := "[b]%s[/b]%s: \u201c%s\u201d" % [who, to, text]
		out.append("[color=#f0c060]%s[/color]" % line if m.get("to_player", false) else line)
	if out.is_empty():
		return ""
	return "[color=#8a93a6]%s[/color]  %s" % [t, "\n         ".join(out)]
