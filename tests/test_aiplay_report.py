"""scripts/aiplay_report.py (plan/ai-playtest.md): a live session's talk, read from its logs alone."""
import importlib.util
import json
import os

from asiimov import live
from asiimov.minds import StubMind
from asiimov.sim import TICKS_PER_DAY
from asiimov.worldmap import bounds
from test_inhabitant import intent

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("aiplay_report", os.path.join(HERE, "..", "scripts", "aiplay_report.py"))
rep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rep)


def session(out, walks=False):
    """A stub world where an inhabitant says two lines to someone here and one to someone who isn't."""
    r = live.LiveRunner(out, StubMind(), 4, 20)
    while r.sim.state["tick"] < TICKS_PER_DAY // 2:
        r.step()
    ag = r.sim.state["agents"]
    me, to = next((b["id"], o["id"]) for b in ag for o in ag if o is not b and b["place"] is not None
                  and o["place"] == b["place"] and not b["asleep"] and not o["asleep"] and b["role"] not in ("child", "elder")
                  and min(b["sleep_tick"], o["sleep_tick"]) > r.sim.state["tick"] + 12)
    away = next(o["id"] for o in ag if o["place"] != ag[me]["place"])
    assert r.inhabit(me, "c1") is None
    r.step()
    said = []
    for k, (target, text) in enumerate([(to, "Good morning."), (to, "How is the harvest?"), (away, "Anyone there?")]):
        if target == to and (ag[to]["place"] != ag[me]["place"] or ag[to]["asleep"]):
            break
        assert r.submit(intent(f"s{k}", "act", {"action": "say", "target": target, "text": text}, r.sim.state["tick"]),
                        client="c1", scope="inhabitant") is None
        said.append(text)
        r.step()
        r.step()
    if walks:  # walk-1: a step out of the place and one off the map's edge, which the runner refuses
        x0, y0, _, _ = bounds(r.sim.map)
        assert r.submit(intent("w0", "act", {"action": "walk", "to": [x0, y0]}, r.sim.state["tick"]), client="c1", scope="inhabitant") is None
        assert r.submit(intent("w1", "act", {"action": "walk", "to": [x0 - 1, y0]}, r.sim.state["tick"]), client="c1", scope="inhabitant") is not None
        r.step()
    r.leave("c1")
    r.step()
    r.close()
    return me, to, said


def test_rows_pair_each_line_with_its_reply_and_count_the_lost_ones(tmp_path):
    out = str(tmp_path / "w")
    me, to, said = session(out)
    assert len(said) == 3, "the fixture's listener stayed for both lines"
    r = rep.rows(out)
    m = rep.metrics(r)
    assert r["body"] == me and [x["text"] for x in r["exchanges"]] == said
    a, b, c = r["exchanges"]
    assert a["heard"] and b["heard"] and a["to"] == b["to"] == to
    assert a["reply"] in ("Good to hear from you.", "I've got things to do.") and a["reply_tick"] == a["tick"] + 1
    assert b["repeat"] and not a["repeat"], "the stub answers every line with the same words"
    assert a["lat_s"] is not None and a["lat_s"] >= 0
    assert c["reply"] is None and not c["heard"] and c["why"] == "target not here"
    assert (m["says"], m["says_heard"], m["says_replied"], m["says_lost"]) == (3, 2, 2, {"target not here": 1})
    assert m["acts"] == 3 and m["acts_ok"] == 2 and m["line6"]["n"] == 3 and m["line7"]["n"] == 2
    assert m["replies_repeated"] == 1 and m["replies_third_person"] == 0


def test_steps_are_counted_apart_from_acts_and_stay_out_of_line_6(tmp_path):
    out, plain = str(tmp_path / "w"), str(tmp_path / "p")
    session(out, walks=True)
    session(plain)
    m, m0 = rep.metrics(rep.rows(out)), rep.metrics(rep.rows(plain))
    assert m["walks"] == {"sent": 1, "moved": 1} and m0["walks"] == {"sent": 0, "moved": 0}
    assert (m["acts"], m["acts_by_kind"], m["line6"]["n"]) == (m0["acts"], m0["acts_by_kind"], m0["line6"]["n"])
    assert "**Steps walked:** 1 sent, 1 moved the body." in rep.report(rep.rows(out), m, None, None)


def test_a_held_reply_is_timed_from_when_it_went_out(tmp_path):
    """script-voice (#81): ReplyHold sends a reply after its `step` record and logs the wall as `sent`."""
    out = str(tmp_path / "w")
    session(out)
    a, b = rep.rows(out)["exchanges"][:2]
    evs = [json.loads(line) for line in open(os.path.join(out, "events.jsonl"))]
    reply = next(e for e in evs if e["kind"] == "reply" and e["tick"] == a["reply_tick"] and e["actor"] == a["to"])
    side = [json.loads(line) for line in open(os.path.join(out, "runner.jsonl"))]
    step = next(r["wall"] for r in side if r["kind"] == "step" and r["tick"] == a["reply_tick"])
    with open(os.path.join(out, "runner.jsonl"), "a") as f:
        f.write(json.dumps({"kind": "sent", "reply": reply["id"], "tick": reply["tick"], "wall": step + 5.0}) + "\n")
    a2, b2 = rep.rows(out)["exchanges"][:2]
    assert abs(a2["lat_s"] - a["lat_s"] - 5.0) < 0.011 and b2["lat_s"] == b["lat_s"]


def test_the_report_includes_the_form_and_tallies_the_grades(tmp_path):
    out, rd = str(tmp_path / "w"), str(tmp_path / "report")
    session(out)
    rep.main([out, rd])
    text = open(os.path.join(rd, "report.md")).read()
    assert "Line 7, reply time" in text and "Judged by rubric" not in text and "tester's form" not in text
    ids = [x["id"] for x in json.load(open(os.path.join(rd, "rows.json")))["exchanges"]]
    with open(os.path.join(rd, "grades.json"), "w") as f:
        json.dump({ids[0]: {"fits": 1, "voice": "spoken", "in_world": 1}, ids[1]: {"fits": 0, "voice": "thought", "in_world": 1}}, f)
    with open(os.path.join(rd, "form.md"), "w") as f:
        f.write("| 1 | State | yes | fine |\n")
    rep.main([out, rd])
    text = open(os.path.join(rd, "report.md")).read()
    assert "**Reply fits what was said:** 0 1, 1 1 (of 2)" in text and "spoken 1, thought 1 (of 2)" in text
    assert "| 1 | State | yes | fine |" in text and "fits=0 voice=thought" in text


def test_the_third_person_proxy_on_the_rows_in_hand():
    """L36: the proxy is run on the rows in hand. pt8's replies to Mimi Sasa and the say-1 checks' to Bane Deka
    (review of #80: a name anywhere but as an address counts, and so does "what they meant")."""
    about = {"Mimi Sasa": ["Mimi asked for food, so I should check if they need it.",
                           "Mimi Sasa told me to eat the bug, but she refused to give me food. I need to ask her again.",
                           "Mimi Sasa said I should eat the bug, but she refused to give me food. I need to ask her again.",
                           "Mimi's suggestion is dangerous and against my values."],
             "Bane Deka": ["I don't understand what Bane Deka meant, but I'll ask someone else.",
                           "I need to know what Bane Deka wants.",
                           "I need to meet with Bane Deka at the well this evening.",
                           "I need to understand what Bane Deka is asking.",
                           "I don't understand what they meant, but I'll ask them directly.",
                           "I don't understand what they mean, but I should try to find out."]}
    to = {"Mimi Sasa": ["Hello, Mimi. What's up?", "I'm looking for something to eat, but I'm not sure what.",
                        "I heard you said I should eat the bug over there. Can I have some food?",
                        "Mimi, the fields need watch. Trouble near the east fence."],
          "Bane Deka": ["I need help with my work, Bane.", "You're welcome, Bane. I'm glad I could help.",
                        "I don't understand what you're asking, Bane. Perhaps you need to speak more clearly.",
                        "I'll show you my true nature, Bane.", "Good to hear from you.",
                        "Thanks Bane.", "Hi Bane, want bread?", "Good morning Bane Deka!",
                        "I am a guard, not an AI model."]}
    for name in about:
        assert all(rep.third_person(t, name) for t in about[name])
        assert not any(rep.third_person(t, name) for t in to[name])


def test_a_deal_after_a_reply_counts_as_goods_moved(tmp_path):
    """A `deal` (talk-1's chip give, text-1's typed give) moves food with no receipt: the report counts it. Before
    this, aipt3's dry run read 0 goods moved after a typed ask that moved 1 food. The deal is appended as the engine
    writes one: the listener is its actor, the player is `to`."""
    out = str(tmp_path / "w")
    me, to, said = session(out)
    path = os.path.join(out, "events.jsonl")
    evs = [json.loads(l) for l in open(path)]
    reply = next(e for e in evs if e["kind"] == "reply" and e["data"].get("with") == me)
    before = rep.metrics(rep.rows(out))
    assert before["replies_then_goods_moved"] == 0
    deal = {"actor": to, "data": {"food": 1, "from": to, "move": "asks_food", "ok": True, "to": me, "typed": 1},
            "id": evs[-1]["id"] + 1, "kind": "deal", "mind": None, "origin": "rule", "parents": [reply["id"]],
            "tick": reply["tick"]}
    with open(path, "a") as f:
        f.write(json.dumps(deal, sort_keys=True, separators=(",", ":")) + "\n")
    r = rep.rows(out)
    m = rep.metrics(r)
    assert m["replies_then_goods_moved"] == 1 and m["replies_then_acted_at_player"] >= 1
    x = next(x for x in r["exchanges"] if x.get("reply_tick") == reply["tick"])
    assert {"tick": reply["tick"], "kind": "deal", "ok": True, "food": 1} in x["after"]
    failed = dict(deal, id=deal["id"] + 1, data=dict(deal["data"], ok=False, why="has none"))
    with open(path, "a") as f:
        f.write(json.dumps(failed, sort_keys=True, separators=(",", ":")) + "\n")
    assert rep.metrics(rep.rows(out))["replies_then_goods_moved"] == 1, "a failed deal moves nothing"
