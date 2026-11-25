"""Every outcome the engine can give a player's act reads as words in the client (readable-world C1).

G5's play checklist, line 3: every act shows what happened and why, in words, refusals included. The
client's Words.WHY (written to client/godot/scripts/verb_words.gd from the verb table) must word every `why` a receipt can carry. The
engine's set is read from sim.py's source (a literal `why` passed to _receipt, or returned with an ok
flag) and from a seeded session of random player acts, so a new `why` fails here until it's worded."""
import ast
import os
import random
import re

from asiimov import sim as S
from asiimov.minds import StubMind

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GD = os.path.join(ROOT, "client", "godot", "scripts", "verb_words.gd")


def client_whys() -> set[str]:
    src = open(GD).read()
    block = src[src.index("const WHY := {"):]
    block = block[:block.index("}\n")]
    return set(re.findall(r'"([^"]+)":', block))


# The values an f-string `why` can take, by the name it formats (review of #60: sim._give's f"no {what} to give").
FSTRING_VALUES = {"what": ("food", "coin")}


def _strings(w) -> set[str]:
    """Every string a `why` expression can be, or an AssertionError naming what the scan can't expand."""
    if isinstance(w, ast.Constant) and isinstance(w.value, str):
        return {w.value}
    if isinstance(w, ast.IfExp):
        return _strings(w.body) | _strings(w.orelse)
    if isinstance(w, ast.JoinedStr):
        outs = {""}
        for part in w.values:
            if isinstance(part, ast.Constant):
                outs = {o + part.value for o in outs}
            else:
                name = getattr(part.value, "id", None)
                assert name in FSTRING_VALUES, f"teach the scan the values of {ast.unparse(part.value)!r} (line {w.lineno})"
                outs = {o + v for o in outs for v in FSTRING_VALUES[name]}
        return outs
    return set()  # a variable: its values are literals the scan finds where they're made


def source_whys() -> set[str]:
    tree = ast.parse(open(S.__file__).read())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "_receipt" and len(node.args) >= 5:
            out |= _strings(node.args[4])
        if isinstance(node, ast.Tuple) and len(node.elts) == 2 and isinstance(node.elts[0], ast.Constant) \
                and isinstance(node.elts[0].value, bool) and isinstance(node.elts[1], ast.Constant) \
                and isinstance(node.elts[1].value, str):
            out.add(node.elts[1].value)
    return out


def session_whys() -> set[str]:
    seen = set()
    for seed in range(3):
        sim, rng = S.Sim(seed, 24, StubMind()), random.Random(seed)
        sim.step([{"kind": "possess", "agent": 3}])
        for _ in range(2 * S.TICKS_PER_DAY):
            acts = []
            if rng.random() < 0.4:
                act = {"kind": "act", "action": rng.choice(S.PLAYER_ACTIONS)}
                if act["action"] in getattr(S, "GO_ACTS", ()):
                    act["place"] = rng.choice(sorted(sim.map["places"]))
                elif act["action"] in getattr(S, "SAY_ACTS", ()):  # say-1: a line to someone
                    act.update(target=rng.randrange(24), text="Good day to you.")
                elif act["action"] in getattr(S, "WALK_ACTS", ()):  # walk-1: a step to a tile
                    act["to"] = [rng.randint(-8, 8), rng.randint(-8, 8)]
                elif act["action"] in getattr(S, "HAIL_ACTS", ()):  # reach-1: a call to someone walking by
                    act["target"] = rng.randrange(24)
                elif rng.random() < 0.8:
                    act["target"] = rng.randrange(24)
                acts = [act] * (2 if rng.random() < 0.05 else 1)
            sim.step(acts)
        seen |= {e.data["why"] for e in sim.events if e.kind == "receipt"}
    return seen


def test_the_scan_finds_the_engines_whys():
    found = source_whys()
    assert {"refused", "kin refused", "nothing to spare", "target asleep", "not offered", "caught",
            "no food to give", "no coin to give"} <= found


def test_every_why_a_receipt_can_carry_is_worded_in_the_client():
    missing = (source_whys() | session_whys()) - client_whys()
    assert not missing, f"add these to verbs.REASONS and run `python -m asiimov.verbs`: {sorted(missing)}"
