"""AI playtest report: what a live session's logs say about talk, beside the tester's form and a judge's grades.

    python scripts/aiplay_report.py RUN_DIR OUT_DIR

RUN_DIR is the live run (events.jsonl, runner.jsonl and, when it narrated, narration.jsonl). It writes
OUT_DIR/rows.json (every exchange, every narrated item, and every act aimed at the player, each with an id)
and OUT_DIR/report.md. When OUT_DIR/form.md (the tester's answers) or OUT_DIR/grades.json (a judge's grades
of rows.json by plan/ai-playtest.md's rubric) is there, the report includes it, so run it again after each.
It works on a person's session too: nothing here depends on who played.

Lines 6 and 7 use the definitions of results/tools/playtest_lat.py and playtest_reply.py (server-side wall
times: an act received to applied; a say received to the step that holds its reply, or to a held reply's `sent` record). Those tools stay the
G5 reading; this prints the same numbers per exchange.
"""
import json
import os
import re
import sys
from collections import Counter

AFTER_TICKS = 12   # how long after a reply the listener's acts toward the player are listed (2 sim-hours)
LINE6_S, LINE7_S = 3.0, 6.0  # G5's bars, p95


# What a villager does toward the player. A `deal` (talk-1, text-1) is goods moved by talk: the listener is its actor
# and `from`, the player is always `to`, for a give and a sale alike.
TOWARD_KINDS = ("ask", "meet", "receipt", "deal")


def _jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def q(xs: list, p: float):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 2) if xs else None


def third_person(says: str, name: str) -> bool:
    """A floor for thoughts shown as speech: the reply talks about the player, not to them. It names the
    player anywhere but as an address ("Mimi asked...", "what Bane Deka meant"; not "Hello, Mimi." or
    "Bane, come here"), or says "what they meant". A thought that never mentions the player ("I should check
    the market") is not caught: the judge's `voice` grade is the reading. On pt8's 8 replies and the say-1
    checks' 84 (results/s16b/say1) it marks the 11 that speak of the player and none of the 9 addresses."""
    first, rest = name.split()[0], " ".join(name.split()[1:])
    who = re.escape(first) + (rf"(?: {re.escape(rest)})?" if rest else "")
    for m in re.finditer(rf"\b{who}\b", says):
        before, after = says[:m.start()].rstrip(), says[m.end():]
        greeted = re.search(r"(?i)\b(?:hi|hello|hey|thanks|thank you|morning|afternoon|evening|night|bye|welcome)$", before)
        address = (not before or before[-1] in ",.!?;:" or greeted) and (not after or after[0] in ",.!?")
        if not address:
            return True
    return re.search(r"\bwhat (?:they|he|she) (?:meant|means?)\b", says) is not None


def _mind_out(e: dict | None) -> dict:
    try:
        out = json.loads(((e or {}).get("mind") or {}).get("raw_output") or "")
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def rows(run_dir: str) -> dict:
    evs = _jsonl(os.path.join(run_dir, "events.jsonl"))
    side = _jsonl(os.path.join(run_dir, "runner.jsonl"))
    narr = _jsonl(os.path.join(run_dir, "narration.jsonl"))
    joins = [r for r in side if r["kind"] == "inhabit"]
    if not joins:
        raise SystemExit(f"{run_dir}: nobody inhabited this world")
    body = joins[0]["agent"]
    leaves = [r for r in side if r["kind"] == "leave" and r["wall"] >= joins[0]["wall"]]
    t0, t1 = joins[0]["wall"], (leaves[-1]["wall"] if leaves else side[-1]["wall"])
    names = {a["id"]: a["name"] for a in evs[0]["data"]["population"]["agents"]}
    roles = {a["id"]: a["role"] for a in evs[0]["data"]["population"]["agents"]}
    step_wall, sent_wall = {}, {}
    for r in side:
        if r["kind"] == "step":
            step_wall.setdefault(r["tick"], r["wall"])
        elif r["kind"] == "sent":  # script-voice: a held reply went out at this wall, after its step record
            sent_wall[r["reply"]] = r["wall"]

    # The player's acts: received to applied (line 6), and each act's receipt.
    got = {r["id"]: r for r in side if r["kind"] == "received" and r.get("client") not in (None, "runner")
           and r.get("act", {}).get("kind") == "act"}
    applied = {r["id"]: r for r in side if r["kind"] == "applied" and r["id"] in got}
    receipt = {}  # an act's first receipt (a go_to gets a second when it arrives)
    for e in evs:
        if e["kind"] == "receipt" and e["actor"] == body and e["parents"]:
            receipt.setdefault(e["parents"][0], e)
    # walk-1: a step is not the tick's act and is sent as often as once a second, so steps are counted apart
    # and kept out of line 6. The engine answers a step with `moved`, or a failed receipt.
    moved = {e["parents"][0] for e in evs if e["kind"] == "moved" and e["actor"] == body and e["parents"]}
    steps = [applied.get(i, {}).get("event") in moved for i, r in got.items() if r["act"]["action"] == "walk"]
    got = {i: r for i, r in got.items() if r["act"]["action"] != "walk"}
    acts = []
    for i, r in got.items():
        ap = applied.get(i)
        rc = receipt.get(ap.get("event")) if ap else None
        acts.append({"action": r["act"]["action"], "tick": r["tick"], "ok": bool(rc and rc["data"].get("ok")),
                     "why": rc["data"].get("why") if rc else "not applied",
                     "lat_s": round(ap["wall"] - r["wall"], 3) if ap else None})

    # Exchanges: each line the player said, the reply to it (a reply answers the listener's latest line
    # before it, as in playtest_reply.py), and what the listener then did toward the player.
    said = [e for e in evs if e["kind"] == "said" and e["actor"] == body]
    replies = [e for e in evs if e["kind"] == "reply" and e["data"].get("with") == body]
    by_id = {e["id"]: e for e in evs}
    answered = {}
    for rep in replies:
        s = max((e for e in said if e["data"]["with"] == rep["actor"] and e["tick"] < rep["tick"]),
                key=lambda e: e["tick"], default=None)
        if s is not None and s["id"] not in answered:
            answered[s["id"]] = rep
    exchanges, used = [], set()
    for r in (x for x in got.values() if x["act"]["action"] == "say"):
        act, row = r["act"], None
        s = next((e for e in said if e["id"] not in used and e["data"]["with"] == act.get("target")
                  and e["data"]["text"] == act.get("text") and e["tick"] >= r["tick"]), None)
        row = {"id": f"x{len(exchanges) + 1}", "tick": r["tick"], "to": act.get("target"),
               "to_name": names.get(act.get("target")), "to_role": roles.get(act.get("target")),
               "text": act.get("text"), "heard": s is not None, "reply": None}
        if s is None:
            ap = applied.get(r["id"])
            rc = receipt.get(ap.get("event")) if ap else None
            row["why"] = rc["data"].get("why") if rc else "not applied"
        else:
            used.add(s["id"])
            rep = answered.get(s["id"])
            if rep is None:
                row["why"] = "a newer line replaced it, or it was never answered"
            else:
                out = _mind_out(by_id.get(rep["parents"][0]) if rep["parents"] else None)
                w = sent_wall.get(rep["id"], step_wall.get(rep["tick"]))
                row.update(reply=rep["data"].get("says", ""), reply_tick=rep["tick"],
                           lat_s=None if w is None else round(w - r["wall"], 2),
                           third_person=third_person(rep["data"].get("says", ""), names[body]),
                           repeat=any(x["reply"] == rep["data"].get("says") and x["to"] == rep["actor"] for x in exchanges),
                           mind={k: out.get(k) for k in ("action", "target", "goal", "then") if out.get(k) not in (None, "", [])},
                           after=[{"tick": e["tick"], "kind": e["kind"], **{k: e["data"][k] for k in ("action", "ok", "why", "food", "coin") if k in e["data"]}}
                                  for e in evs if e["actor"] == rep["actor"] and rep["tick"] <= e["tick"] <= rep["tick"] + AFTER_TICKS
                                  and e["kind"] in TOWARD_KINDS and any(e["data"].get(k) == body for k in ("to", "with", "target"))])
        exchanges.append(row)

    heard = [{"id": f"h{i + 1}", "tick": n["tick"], "kind": n["kind"], "to_player": n["to_player"], "fallback": n.get("fallback", False),
              "topics": [t["text"] for t in n.get("topics", [])], "lines": [{"name": l["name"], "text": l["text"]} for l in n["lines"]]}
             for i, n in enumerate(narr) if n.get("player") == body]
    toward = [{"id": f"t{i + 1}", "tick": e["tick"], "who": names.get(e["actor"]), "kind": e["kind"],
               **{k: e["data"][k] for k in ("action", "ok", "why", "food", "coin") if k in e["data"]}}
              for i, e in enumerate(e for e in evs if e["actor"] not in (None, body) and e["kind"] in TOWARD_KINDS
                                    and any(e["data"].get(k) == body for k in ("to", "with", "target"))
                                    and step_wall.get(e["tick"], 0) >= t0)]
    held_ticks = sorted(t for t, w in step_wall.items() if t0 <= w <= t1)
    return {"run": os.path.abspath(run_dir), "engine": evs[0]["data"].get("engine"), "body": body, "name": names[body],
            "role": roles[body], "held_min": round((t1 - t0) / 60, 1),
            "ticks": [held_ticks[0], held_ticks[-1]] if held_ticks else None,
            "acts": acts, "walks": {"sent": len(steps), "moved": sum(steps)}, "exchanges": exchanges, "heard": heard, "toward": toward}


def metrics(r: dict) -> dict:
    ex, acts, heard = r["exchanges"], r["acts"], r["heard"]
    got = [x for x in ex if x["reply"] is not None]
    lat = [x["lat_s"] for x in got if x["lat_s"] is not None]
    act_lat = [a["lat_s"] for a in acts if a["lat_s"] is not None]
    lines = [l["text"] for h in heard for l in h["lines"]]
    seen = Counter(lines)
    aimed = [x for x in got if x["mind"].get("target") == r["body"] or any(s.get("target") == r["body"] for s in x["mind"].get("then", []))]
    return {
        "acts": len(acts), "acts_ok": sum(a["ok"] for a in acts),
        "acts_by_kind": dict(Counter(a["action"] for a in acts)),
        "refusals": dict(Counter(a["why"] for a in acts if not a["ok"])),
        "walks": r.get("walks", {"sent": 0, "moved": 0}),
        "line6": {"n": len(act_lat), "p50": q(act_lat, .5), "p95": q(act_lat, .95), "max": q(act_lat, 1),
                  "pass": bool(act_lat) and q(act_lat, .95) <= LINE6_S},
        "says": len(ex), "says_heard": sum(x["heard"] for x in ex), "says_replied": len(got),
        "says_lost": dict(Counter(x["why"] for x in ex if x["reply"] is None)),
        "line7": {"n": len(lat), "p50": q(lat, .5), "p95": q(lat, .95), "max": q(lat, 1),
                  "pass": bool(lat) and q(lat, .95) <= LINE7_S},
        "replies_third_person": sum(x["third_person"] for x in got),
        "replies_repeated": sum(x["repeat"] for x in got),
        "replies_empty": sum(not x["reply"].strip() for x in got),
        "replies_with_act_at_player": len(aimed),
        "replies_then_acted_at_player": sum(bool(x["after"]) for x in got),
        "replies_then_goods_moved": sum(any(a.get("ok") and a["kind"] in ("receipt", "deal") for a in x["after"]) for x in got),
        "heard_items": len(heard), "heard_to_player": sum(h["to_player"] for h in heard),
        "heard_fallback": sum(h["fallback"] for h in heard),
        "heard_opened_on_a_topic": sum(bool(h["topics"]) for h in heard),
        "heard_lines": len(lines), "heard_lines_repeated": sum(n - 1 for n in seen.values() if n > 1),
        "toward_player": dict(Counter(f"{t['kind']}:{t.get('action', '')}".rstrip(":") for t in r["toward"])),
    }


def _tally(grades: dict, ids: list, key: str) -> str:
    vals = [grades[i][key] for i in ids if i in grades and key in grades[i]]
    if not vals:
        return "not graded"
    c = Counter(str(v) for v in vals)
    return ", ".join(f"{k} {n}" for k, n in sorted(c.items())) + f" (of {len(vals)})"


def report(r: dict, m: dict, form: str | None, grades: dict | None) -> str:
    g = grades or {}
    xs, hs = [x["id"] for x in r["exchanges"] if x["reply"] is not None], [h["id"] for h in r["heard"]]
    l6, l7 = m["line6"], m["line7"]
    out = [f"# AI playtest report: {r['name']} ({r['role']}, villager {r['body']}), engine {r['engine']}", "",
           f"Held {r['held_min']} min" + (f", ticks {r['ticks'][0]}–{r['ticks'][1]}" if r["ticks"] else "") + f". Run: `{r['run']}`.", "",
           "## Measured from the logs", "",
           f"- **Acts:** {m['acts']} sent, {m['acts_ok']} ok. By kind: {m['acts_by_kind']}. Refused or failed: {m['refusals'] or 'none'}.",
           f"- **Steps walked:** {m['walks']['sent']} sent, {m['walks']['moved']} moved the body.",
           f"- **Line 6, pace:** {'PASS' if l6['pass'] else 'FAIL'}. Received to applied, n {l6['n']}: p50 {l6['p50']} s, p95 {l6['p95']} s, max {l6['max']} s (bar {LINE6_S:g} s at p95).",
           f"- **Line 7, reply time:** {'PASS' if l7['pass'] else 'FAIL'}. Click to reply, n {l7['n']}: p50 {l7['p50']} s, p95 {l7['p95']} s, max {l7['max']} s (bar {LINE7_S:g} s at p95).",
           f"- **Lines said:** {m['says']}; heard by the listener {m['says_heard']}; answered {m['says_replied']}. Lost: {m['says_lost'] or 'none'}.",
           f"- **Replies that speak of the player, not to them (a floor for thoughts shown as speech):** {m['replies_third_person']} of {m['says_replied']}.",
           f"- **Replies repeated word for word by the same villager:** {m['replies_repeated']}. Empty replies: {m['replies_empty']}.",
           f"- **Does talk lead anywhere:** in {m['replies_with_act_at_player']} of {m['says_replied']} replies the villager's own next act or plan was aimed at the player; "
           f"after {m['replies_then_acted_at_player']} they did act toward the player within {AFTER_TICKS} ticks (a visit, an ask, a gift or a sale); "
           f"after {m['replies_then_goods_moved']} food or coin changed hands.",
           f"- **Talk near the player:** {m['heard_items']} narrated items ({m['heard_to_player']} to the player, {m['heard_fallback']} template fallbacks, "
           f"{m['heard_opened_on_a_topic']} opened on a news topic); {m['heard_lines_repeated']} of {m['heard_lines']} lines repeat an earlier line word for word.",
           f"- **The world coming to the player:** {m['toward_player'] or 'nothing'}.", ""]
    if grades is not None:
        out += ["## Judged by rubric (plan/ai-playtest.md)", "",
                f"- **Reply fits what was said:** {_tally(g, xs, 'fits')}.",
                f"- **Voice of the reply:** {_tally(g, xs, 'voice')}.",
                f"- **Stays in the world:** {_tally(g, xs, 'in_world')}.",
                f"- **Narrated lines are said to someone:** {_tally(g, hs, 'directed')}.",
                f"- **Narrated lines read like people:** {_tally(g, hs, 'natural')}.", ""]
    out += ["## Every exchange", "", "| # | tick | to | the player said | the villager answered | s | their next act | grade |", "|---|---|---|---|---|---|---|---|"]
    for x in r["exchanges"]:
        cell = lambda s: str(s).replace("|", "/").replace("\n", " ")
        gr = g.get(x["id"], {})
        grade = " ".join(f"{k}={v}" for k, v in gr.items() if k != "note") + (f" ({gr['note']})" if gr.get("note") else "")
        if x["reply"] is None:
            out.append(f"| {x['id']} | {x['tick']} | {x['to_name']} | {cell(x['text'])} | *no reply: {x['why']}* | | | |")
        else:
            mind = x["mind"]
            aft = [f"t{a['tick']} {a.get('action', a['kind'])}" + ("" if "ok" not in a else " ok" if a["ok"] else f" failed ({a.get('why')})")
                   for a in x["after"]]
            nxt = f"{mind.get('action', '')}" + (f" → {mind['goal']}" if mind.get("goal") else "") \
                + (f"; toward the player after: {', '.join(aft[:3])}" + (f" and {len(aft) - 3} more" if len(aft) > 3 else "") if aft else "")
            out.append(f"| {x['id']} | {x['tick']} | {x['to_name']} ({x['to_role']}) | {cell(x['text'])} | {cell(x['reply'])} | {x['lat_s']} | {cell(nxt)} | {cell(grade)} |")
    if form:
        out += ["", "## The tester's form and feedback", "", form.strip()]
    return "\n".join(out) + "\n"


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        raise SystemExit(__doc__)
    run_dir, out_dir = args
    os.makedirs(out_dir, exist_ok=True)
    r = rows(run_dir)
    m = metrics(r)
    with open(os.path.join(out_dir, "rows.json"), "w") as f:
        json.dump({**r, "metrics": m}, f, indent=1)
    form = grades = None
    if os.path.exists(os.path.join(out_dir, "form.md")):
        with open(os.path.join(out_dir, "form.md")) as f:
            form = f.read()
    if os.path.exists(os.path.join(out_dir, "grades.json")):
        with open(os.path.join(out_dir, "grades.json")) as f:
            grades = json.load(f)
    with open(os.path.join(out_dir, "report.md"), "w") as f:
        f.write(report(r, m, form, grades))
    print(json.dumps(m, indent=1))


if __name__ == "__main__":
    main()
