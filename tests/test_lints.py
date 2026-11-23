"""The AGENTS.md rules as static checks (asiimov/lint.py). The package must be clean, and every rule is
shown failing on a planted violation (a snippet, and the same edit made to the real sim.py) and passing
on a compliant one: a check that has never been seen to fail proves nothing."""
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap

import pytest

from asiimov import lint

SIM = os.path.join(lint.PKG, "sim.py")


def found(src, rule, path="asiimov/sim.py"):
    return [f for f in lint.lint_source(textwrap.dedent(src), path, (rule,))]


def test_the_package_is_clean():
    assert [str(f) for f in lint.lint_package()] == []


# What Python itself imports and defines, asked in a fresh interpreter (-I: no cwd, no PYTHONPATH), so these
# tests don't take lint's word for the file sets it checks.
PROBE = r"""
import importlib, json, os, sys
root = sys.argv[1]
sys.path.insert(0, root)
pkg = os.path.join(root, "asiimov")
def rel(m):
    f = os.path.abspath(m.__file__)
    assert f.startswith(pkg + os.sep), f
    return os.path.relpath(f, pkg).replace(os.sep, "/")
def ours():
    return {n: m for n, m in list(sys.modules.items()) if n == "asiimov" or n.startswith("asiimov.")}
for r in ("sim", "replay", "persist"):
    importlib.import_module("asiimov." + r)
core = sorted(rel(m) for m in ours().values())
failed = []
for d, dirs, files in os.walk(pkg):
    dirs[:] = sorted(x for x in dirs if x != "__pycache__")
    for f in sorted(files):
        if f.endswith(".py"):
            parts = os.path.relpath(os.path.join(d, f), pkg)[:-3].split(os.sep)
            name = ".".join(["asiimov"] + (parts[:-1] if parts[-1] == "__init__" else parts))
            try:
                importlib.import_module(name)
            except Exception as e:
                failed.append([name, repr(e)])
from asiimov.sim import Sim
subs, todo = {}, list(Sim.__subclasses__())
while todo:
    c = todo.pop()
    todo += c.__subclasses__()
    m = sys.modules[c.__module__]
    if c.__module__.startswith("asiimov"):
        subs.setdefault(rel(m), set()).add(c.__name__)
print(json.dumps({"core": core, "failed": failed, "subs": {k: sorted(v) for k, v in subs.items()}}))
"""


def runtime(root):
    """The engine's import closure (files), and the Sim subclasses defined, per Python, for the package in root."""
    r = subprocess.run([sys.executable, "-I", "-c", PROBE, str(root)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["failed"] == [], out["failed"]  # every module imports, so the subclass census is complete
    return set(out["core"]), out["subs"]


@functools.lru_cache(1)
def real_runtime():
    core, subs = runtime(os.path.dirname(lint.PKG))
    return frozenset(core), subs


def test_the_sim_core_is_what_the_engine_imports():
    """A module the engine comes to import has to be classed as core (checked) or mind side (not). Checked
    against what Python imports, not against lint's own import walk."""
    core, _ = real_runtime()
    assert {"sim.py", "events.py", "rng.py", "minds.py"} <= core
    assert set(lint.core_files()) == core - set(lint.MIND_SIDE)
    assert set(lint.SIM_CORE) == core - set(lint.MIND_SIDE)
    assert lint.core_imports() == core


def test_the_mind_side_is_only_minds_py():
    """minds.py is the far side of the mind interface (its outputs are origin "mind", recorded verbatim and
    replayed from the log, so it may sample: NoisyMind uses os.urandom). Nothing else is exempt: moving a
    core module here would let it drift out of every check while the import-graph test stays green."""
    assert lint.MIND_SIDE == ("minds.py",)


def test_every_sim_subclass_in_the_package_is_found():
    """ArmSim is run and replayed by fork.py; ProbeSim and AuthorshipSim replay logs. Their bodies are linted.
    The AST finder must agree with Sim.__subclasses__() after importing every module."""
    _, subs = real_runtime()
    assert lint.sim_subclasses() == subs == {"scorecard.py": ["ArmSim"], "probe.py": ["ProbeSim"],
                                             "authorship.py": ["AuthorshipSim"]}


# ---- every rule is applied to every file it covers (B1) ----------------------------------------------
# The plants go into a copy of the real package, chosen from the import graph and fixed names here, not from
# lint's own tables: editing SIM_CORE, MIND_SIDE, the per-file rules or the subclass scan must fail this.

CORE_PLANT = """
_LINT_PLANT_INT = 7 / 2
_LINT_PLANT_RNG = hash("plant")
def _lint_plant_keyed(rng, ids):
    return ids[rng.at("plant", 0, 0, 1)]
"""
COUNTER_PLANT = """
def _lint_plant_counter(rng):
    return rng.draw("plant", 0, 1)
"""
ORDER_PLANT = """
def _lint_plant_order(self):
    return self.mind.decide(None)
"""
CLASS_PLANT = """\
    def _lint_plant(self, ids):
        a = 7 / 2
        b = hash("plant")
        c = ids[self.rng.at("plant", 0, 0, 1)]
        d = self.rng.draw("plant", 0, 1)
        e = self.mind.decide(None)
"""
GENESIS_OR_RNG = {"params.py", "rng.py"}  # counter draws there are genesis's fixed sequence, or Rng itself
SUBCLASSES = {"scorecard.py": "ArmSim", "probe.py": "ProbeSim", "authorship.py": "AuthorshipSim"}


def _line(text, needle):
    lines = text.splitlines()
    hits = [i + 1 for i, l in enumerate(lines) if needle in l]
    assert len(hits) == 1, (needle, hits)
    return hits[0]


def test_a_violation_planted_in_every_covered_file_is_reported(tmp_path):
    pkg = tmp_path / "asiimov"
    shutil.copytree(lint.PKG, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    core, subs = real_runtime()
    core = core - {"minds.py"}  # the import closure per Python, not per lint
    assert {"sim.py", "memory.py", "params.py", "rng.py", "events.py"} <= core
    assert {k: v[0] for k, v in subs.items()} == SUBCLASSES
    expected = set()
    for name in sorted(core):
        path = pkg / name
        text = path.read_text().rstrip("\n") + "\n" + CORE_PLANT
        if name not in GENESIS_OR_RNG:
            text += COUNTER_PLANT
        if name == "sim.py":
            text += ORDER_PLANT
        path.write_text(text)
        rel = f"asiimov/{name}"
        expected |= {(rel, _line(text, "7 / 2"), "int"), (rel, _line(text, 'hash("plant")'), "rng"),
                     (rel, _line(text, 'ids[rng.at("plant"'), "keyed")}
        if name not in GENESIS_OR_RNG:
            expected.add((rel, _line(text, 'rng.draw("plant"'), "keyed"))
        if name == "sim.py":
            expected.add((rel, _line(text, "self.mind.decide(None)"), "order"))
    for name, cls in SUBCLASSES.items():
        path = pkg / name
        text = path.read_text()
        head = re.search(rf"^class {cls}\(.*\):\n", text, re.M)
        text = text[:head.end()] + CLASS_PLANT + text[head.end():]
        path.write_text(text)
        rel = f"asiimov/{name}"
        expected |= {(rel, _line(text, "a = 7 / 2"), "int"), (rel, _line(text, 'b = hash("plant")'), "rng"),
                     (rel, _line(text, "c = ids[self.rng.at"), "keyed"), (rel, _line(text, "d = self.rng.draw"), "keyed"),
                     (rel, _line(text, "e = self.mind.decide(None)"), "order")}
    found = {(f.path, f.line, f.rule) for f in lint.lint_package(str(pkg))}
    assert sorted(expected - found) == [], "planted violations not reported"
    assert sorted(found - expected) == [], "findings beyond the plants"


def test_each_allowed_index_pick_is_a_live_exception(monkeypatch):
    """No stale entries: without the allowlist, each entry is exactly one finding, and nothing else is."""
    allowed = dict(lint.INDEX_PICK_ALLOW)
    monkeypatch.setattr(lint, "INDEX_PICK_ALLOW", {})
    hits = []
    for name in sorted({p for p, _, _ in allowed}):
        with open(os.path.join(lint.PKG, name)) as fh:
            hits += lint.lint_source(fh.read(), f"asiimov/{name}", ("keyed",))
    assert len(hits) == len(allowed)
    assert {(os.path.basename(f.path), f.message.split("`")[1].split("[")[0]) for f in hits} == \
        {(p, name) for p, _, name in allowed}


def test_each_function_allowed_to_reach_the_mind_is_a_live_exception(monkeypatch):
    """Without ORDER_MIND_REACH, the pace-2 in-flight functions are flagged, and nothing else newly is."""
    src = _clean_sim()
    monkeypatch.setattr(lint, "ORDER_MIND_REACH", {})
    hits = lint.lint_source(src, "asiimov/sim.py", ("order",))
    assert hits and {f.message.split("`")[-2] for f in hits if "used in" in f.message} \
        == {"_fly", "_send"}, hits
    assert all("used in" in f.message or "called directly" in f.message for f in hits), hits


# ---- int --------------------------------------------------------------------------------------------

INT_BAD = """
import math
from statistics import mean
a = b / c
a /= 2
x = 0.5
y = float(n)
z = math.sqrt(n)
p = n ** -1
q = n ** k
"""
INT_GOOD = """
import math
from math import gcd, isqrt
a = b // c
a //= 2
x = 2 ** 32 + 10 ** 9
y = math.isqrt(n) + gcd(a, b) + isqrt(n)
z = n * 300 // 1000
"""


def test_int_flags_float_arithmetic():
    lines = sorted(f.line for f in found(INT_BAD, "int"))
    assert lines == [3, 4, 5, 6, 7, 8, 9, 10], lines


def test_int_passes_integer_arithmetic():
    assert found(INT_GOOD, "int") == []


# ---- rng --------------------------------------------------------------------------------------------

RNG_BAD = """
import random
from secrets import token_hex
import numpy as np
r = random.random()
s = np.random.rand()
t = time.time()
u = datetime.now()
v = os.urandom(4)
h = hash(name)
i = id(obj)
for x in set(xs):
    pass
ys = [y for y in {1, 2}]
zs = list(set(xs))
fs = os.listdir(d)
"""
RNG_GOOD = """
import hashlib
for x in sorted(set(xs)):
    pass
fs = sorted(f for f in os.listdir(d) if f.endswith(".json"))
r = self.rng.at(f"talk:{aid}", tick, 0, 999)
k = hashlib.sha256(b"x").hexdigest()
seen = {1, 2}
ok = 3 in seen
"""


def test_rng_flags_entropy_clocks_and_unordered_iteration():
    lines = sorted({f.line for f in found(RNG_BAD, "rng")})
    assert lines == [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15, 16], lines


def test_rng_passes_rng_draws_and_sorted_iteration():
    assert found(RNG_GOOD, "rng") == []


# ---- keyed (T22) ------------------------------------------------------------------------------------

KEYED_BAD = """
def talk(self, ids, tick, aid):
    _, roll = self.rng.draw(f"talk:{aid}", 0, 999)
    z = self.rng.normal_milli("mood", 100)
    w = self.rng.draw(make_stream(), 0, 1)
    b = ids[self.rng.at("pick", tick, 0, len(ids) - 1)]
    i = self.rng.at("pick2", tick, 0, len(ids) - 1)
    c = ids[i]
    _, j = self.rng.draw(f"jitter:{aid}", 0, len(ids) - 1)
    d = ids[j]
"""
KEYED_GOOD = """
SYLLABLES = ("ka", "mi")
def talk(self, ids, tick, aid, a):
    idx, jitter = self.rng.draw(f"jitter:{a['id']}", -3, 3)
    stream = f"steal:{a['id']}"
    i1, roll = self.rng.draw(stream, 0, 999)
    hit = self.rng.at(f"talk:{aid}", tick, 0, 999)
    pick_stream = f"pair:{aid}:{tick}"
    b = ids[min((o for o in ids if o != aid), key=lambda o: (self.rng.raw(pick_stream, o), o))]
    s = SYLLABLES[self.rng.draw(f"jitter:{aid}", 0, 1)[1]]
    v = self.rng.draw(f"jitter:{aid}", 0, 1)[1]
"""


def test_keyed_flags_unregistered_counter_draws_and_index_picks():
    lines = sorted(f.line for f in found(KEYED_BAD, "keyed"))
    assert lines == [3, 4, 5, 6, 8, 10], lines


def test_keyed_passes_registered_streams_keyed_draws_and_lowest_score_picks():
    assert found(KEYED_GOOD, "keyed") == []


def test_keyed_leaves_genesis_counter_draws_alone():
    src = 'def gen(rng):\n    age = rng.draw("agent:0", 6, 80)[1]\n'
    assert found(src, "keyed", "asiimov/params.py") == []
    assert len(found(src, "keyed", "asiimov/sim.py")) == 1


# ---- order ------------------------------------------------------------------------------------------

ORDER_BAD = """
class Sim:
    def _tick(self, reqs):
        replies = dispatch(self.mind, reqs, 1)
        r = self.mind.decide(reqs[0])
        if self.order == "shuffle":
            pass
        apply(0, _parse(r))
    def _commit(self):
        for aid in sorted(self.pending):
            apply(0, _parse(replies[aid]))
"""
ORDER_GOOD = """
class Sim:
    def _setup(self, mind, order):
        self.mind, self.order = mind, order
    def _order(self, ids):
        return sorted(ids) if self.order == "id" else ids
    def _commit(self):
        replies = dict(zip(aids, dispatch(self.mind, reqs, self.workers)))
        for aid in self._order(aids):
            apply(eid, _parse(replies[aid]))
"""


def test_order_flags_replies_fetched_or_applied_outside_commit_order():
    lines = sorted(f.line for f in found(ORDER_BAD, "order"))
    assert lines == [4, 4, 5, 5, 6, 8, 11], lines  # line 4: dispatch and self.mind; 5: self.mind and .decide


def test_order_passes_the_commit_shape():
    assert found(ORDER_GOOD, "order") == []


# ---- the same violations planted in the real engine -------------------------------------------------

PLANTED = [
    ("int", "a[\"wake_tick\"] = wake_tick", "a[\"wake_tick\"] = wake_tick / 1"),
    ("rng", "        ids = sorted(ids)\n", "        ids = list(set(ids))\n"),
    ("keyed", "roll = self.rng.at(stream, tick, 0, 999)", "_, roll = self.rng.draw(stream, 0, 999)"),
    ("keyed", "b = agents[min((o for o in ids if o != aid), key=lambda o: (self.rng.raw(pick_stream, o), o))]",
     "b = agents[ids[self.rng.at(pick_stream, tick, 0, len(ids) - 1)]]"),
    ("order", "for aid in self._order(d_ids):", "for aid in d_ids:"),
    ("order", "enumerate(self._order(set(self.pending) | set(due)))", "enumerate(sorted(set(self.pending) | set(due)))"),
    ("order", "due = self._fly()", "due = self._fly()\n        self._send(None)"),
]


@functools.lru_cache(1)
def _clean_sim():
    with open(SIM) as f:
        src = f.read()
    assert lint.lint_source(src, "asiimov/sim.py", lint.RULES) == []
    return src


@pytest.mark.parametrize("rule,before,after", PLANTED, ids=[f"{r}-{i}" for i, (r, _, _) in enumerate(PLANTED)])
def test_a_violation_planted_in_sim_py_is_flagged(rule, before, after):
    src = _clean_sim()
    assert src.count(before) == 1, before
    hits = lint.lint_source(src.replace(before, after), "asiimov/sim.py", lint.RULES)
    assert hits and {f.rule for f in hits} == {rule}, hits


# ---- renamed imports, and the calls a rename used to hide (B2) --------------------------------------

ALIAS_BAD = """
import time as t
import math as m
import os as o
import numpy as n
from time import perf_counter as clock
from math import sqrt as root
from os import listdir as ls
from operator import truediv as td
import importlib as il
a = t.time()
b = m.sqrt(x)
c = o.getpid()
d = n.random.rand()
e = clock()
f = root(x)
g = ls(path)
h = td(p, q)
i = il.import_module("random")
"""


def test_renamed_imports_are_resolved_before_matching():
    int_lines = sorted(f.line for f in found(ALIAS_BAD, "int"))
    rng_lines = sorted(f.line for f in found(ALIAS_BAD, "rng"))
    # int: import numpy (5), from math import sqrt (7), m.sqrt (12), root (16), td (18)
    assert int_lines == [5, 7, 12, 16, 18], int_lines
    # rng: import numpy (5), from time import perf_counter (6), from os import listdir (8), t.time (11), o.getpid (13),
    # n.random.rand (14), clock (15), ls (17), il.import_module (19)
    assert rng_lines == [5, 6, 8, 11, 13, 14, 15, 17, 19], rng_lines


def test_an_alias_of_an_allowed_name_stays_allowed():
    src = """
    import math as m
    from math import isqrt as isq
    import os as o
    a = m.isqrt(n) + isq(n) + m.gcd(a, b)
    fs = sorted(o.listdir(d))
    """
    assert found(src, "int") == [] and found(src, "rng") == []


# ---- pow, operator division, dynamic imports, process ids -------------------------------------------

MORE_BAD = """
a = pow(x, -1)
b = pow(x, k)
c = operator.truediv(x, y)
d = operator.itruediv(x, y)
e = __import__("random")
f = importlib.import_module("time")
g = os.getpid()
"""
MORE_GOOD = """
a = pow(x, 2) + pow(x, k, m) + pow(x, -1, m)
b = operator.floordiv(x, y)
"""


def test_pow_operator_division_dynamic_imports_and_pids_are_flagged():
    assert sorted(f.line for f in found(MORE_BAD, "int")) == [2, 3, 4, 5]
    assert sorted(f.line for f in found(MORE_BAD, "rng")) == [6, 7, 8]
    assert found(MORE_GOOD, "int") == [] and found(MORE_GOOD, "rng") == []


# ---- a set reached through a local name ------------------------------------------------------------

SET_BAD = """
def f(xs, d, e):
    s = set(xs)
    for x in s:
        pass
    t = {1, 2}
    u = list(t)
    k = d.keys() | e.keys()
    v = [y for y in k]
    w = s - t
    z = tuple(w)
    c = {x for x in xs}
    q = c.pop()
    j = ", ".join(s)
    g: set = frozenset(xs)
    g |= s
    for x in g:
        pass
    def inner():
        for x in s:
            pass
"""
SET_GOOD = """
def f(xs, d):
    s = set(xs)
    ok = 3 in s and len(s)
    for x in sorted(s):
        pass
    r = set(xs)
    r = sorted(r)
    for x in r:
        pass
    for k in d.keys():
        pass
    p = [1, 2]
    p.pop()
    n = s.pop if False else None
def g(s):
    for x in s:
        pass
"""


def test_iterating_a_local_set_is_flagged():
    lines = sorted(f.line for f in found(SET_BAD, "rng"))
    assert lines == [4, 7, 9, 11, 13, 14, 17, 20], lines


def test_a_local_set_that_is_sorted_or_reassigned_is_not():
    assert found(SET_GOOD, "rng") == []


# ---- index-pick taint, through locals and aliases ----------------------------------------------------

TAINT_BAD = """
def pick(self, ids, tick, aid):
    a = self.rng.at("p", tick, 0, 9)
    b = a + 1
    c: int = b * 2
    x = ids[c]
    k = 0
    k += self.rng.at("q", tick, 0, 9)
    y = ids[k]
    z = ids.pop(a)
    g = getattr(self.rng, "at")
    w = ids[g("r", tick, 0, 9)]
    v = ids[getattr(self.rng, "at")("s", tick, 0, 9)]
    d = self.rng.draw
    _, r = d(f"jitter:{aid}", 0, 9)
    u = ids[r]
    _, m = d("mood", 0, 9)
"""
TAINT_GOOD = """
def pick(self, ids, tick, aid):
    a = self.rng.at("p", tick, 0, 9)
    k = 0
    k += 1
    x = ids[k]
    y = ids.pop()
    b = min(ids, key=lambda o: self.rng.raw(f"p:{tick}", o))
    z = ids[ids.index(b)]
    d = self.rng.at
    hit = d("q", tick, 0, 999) < 300
"""


def test_index_picks_are_traced_through_locals_pops_and_rng_aliases():
    lines = sorted(f.line for f in found(TAINT_BAD, "keyed"))
    assert lines == [6, 9, 10, 12, 13, 16, 17], lines


def test_untainted_indexes_and_keyed_picks_pass():
    assert found(TAINT_GOOD, "keyed") == []


# ---- order: the pace-2 shape (engine 38) -------------------------------------------------------------

PACE2_GOOD = """
class Sim:
    def _fly(self):
        reply = self.mind.decide(req)
        self._fut[aid] = self._send(req)
        return {aid: (entry, reply)}
    def _send(self, req):
        mind = self.mind
        return pool.submit(mind.decide, req)
    def _commit(self):
        due = self._fly()
        replies = dict(zip(aids, dispatch(self.mind, reqs, self.workers)))
        rank = {aid: i for i, aid in enumerate(self._order(set(self.pending) | set(due)))}
        entries = sorted([(rank[aid], aid, replies[aid]) for aid in aids])
        for i, (_, aid, reply) in enumerate(entries):
            apply(eid, _parse(reply))
"""
PACE2_BAD = """
class Sim:
    def step(self):
        due = self._fly()
        self._send(req)
    def _other(self):
        return self.mind.decide(req)
    def _commit(self):
        rank = {aid: i for i, aid in enumerate(sorted(self.pending))}
        entries = sorted([(rank[aid], aid, replies[aid]) for aid in aids])
        for i, (_, aid, reply) in enumerate(entries):
            apply(eid, _parse(reply))
"""


def test_order_accepts_the_pace2_in_flight_calls():
    assert found(PACE2_GOOD, "order") == []


def test_order_flags_in_flight_calls_reached_elsewhere_and_an_apply_loop_not_from_order():
    lines = sorted(f.line for f in found(PACE2_BAD, "order"))
    assert lines == [4, 5, 7, 7, 12], lines  # 7: self.mind and .decide outside _commit/_fly/_send


# ---- inline suppressions ---------------------------------------------------------------------------

def test_a_suppression_with_a_reason_silences_its_rule_on_its_line_only():
    src = ("p = base / 'x'  # lint: allow int -- pathlib join, not a division\n"
           "q = a / b\n"
           "r = 2 ** bits  # lint: allow int -- bits is a non-negative int width\n")
    assert [(f.line, f.rule) for f in lint.lint_source(src, "asiimov/sim.py", ("int",))] == [(2, "int")]


@pytest.mark.parametrize("comment,why", [
    ("# lint: allow int", "without a reason"),
    ("# lint: allow int --  ", "without a reason"),
    ("# lint: allow floats -- because", "unknown rule"),
    ("# lint: allow rng -- not this one", "suppresses nothing"),
    ("# lint: alow int -- typo", "unreadable"),
])
def test_a_bad_or_unused_suppression_is_a_finding(comment, why):
    src = f"p = base / 'x'  {comment}\n"
    hits = lint.lint_source(src, "asiimov/sim.py", ("int", "rng"))
    assert any(f.rule == "suppress" and why in f.message for f in hits), hits
    assert why != "suppresses nothing" or any(f.rule == "int" for f in hits)


def test_suppression_text_inside_a_string_is_not_a_suppression():
    src = 'p = a / b\ns = "# lint: allow int -- in a string"\n'
    assert [(f.line, f.rule) for f in lint.lint_source(src, "asiimov/sim.py", ("int",))] == [(1, "int")]


# ---- the import closure follows every import form, subpackages too (round 2, B1) --------------------

HELPER = "def half(x):\n    return x / 2\n"
IMPORT_FORMS = [
    ("helpers.py", "from asiimov import helpers"),
    ("helpers.py", "import asiimov.helpers"),
    ("helpers.py", "import asiimov.helpers as h"),
    ("helpers.py", "from . import helpers"),
    ("helpers.py", "from .helpers import half"),
    ("helpers.py", "from asiimov.helpers import half"),
    ("sub/helpers.py", "from .sub import helpers"),
    ("sub/helpers.py", "import asiimov.sub.helpers"),
    ("sub/helpers.py", "from asiimov.sub.helpers import half"),
]


def _only_py(d, names):
    return [n for n in names if n == "__pycache__" or (not n.endswith(".py") and not os.path.isdir(os.path.join(d, n)))]


def _copy(tmp_path):
    pkg = tmp_path / "asiimov"
    shutil.copytree(lint.PKG, pkg, ignore=_only_py)
    return pkg


@pytest.mark.parametrize("where,stmt", IMPORT_FORMS, ids=[s for _, s in IMPORT_FORMS])
def test_a_module_the_engine_imports_in_any_form_is_linted_and_must_be_classed(tmp_path, where, stmt):
    pkg = _copy(tmp_path)
    (pkg / where).parent.mkdir(exist_ok=True)
    if where.startswith("sub/"):
        (pkg / "sub" / "__init__.py").write_text("")
    (pkg / where).write_text(HELPER)
    sim = (pkg / "sim.py").read_text()
    head = sim.index("\nfrom .minds import")
    (pkg / "sim.py").write_text(sim[:head] + "\n" + stmt + sim[head:])
    rel = f"asiimov/{where}"
    assert where in lint.core_files(str(pkg))  # linted whole by lint_package (the plant test proves that)
    want = {rel} | ({"asiimov/sub/__init__.py"} if where.startswith("sub/") else set())
    assert {f.path for f in lint.unclassed(str(pkg))} == want
    if stmt == IMPORT_FORMS[0][1]:  # once, end to end
        hits = {(f.path, f.line, f.rule) for f in lint.lint_package(str(pkg))}
        assert (rel, 2, "int") in hits and (rel, 1, "core") in hits, hits


def test_the_closure_with_a_subpackage_matches_python(tmp_path):
    """One copy with several forms at once, checked against what Python imports."""
    pkg = _copy(tmp_path)
    (pkg / "sub").mkdir()
    (pkg / "sub" / "__init__.py").write_text("from . import deep\n")
    (pkg / "sub" / "deep.py").write_text("from ..helpers import half\n")
    (pkg / "helpers.py").write_text(HELPER)
    (pkg / "other.py").write_text("X = 1\n")
    sim = (pkg / "sim.py").read_text()
    head = sim.index("\nfrom .minds import")
    (pkg / "sim.py").write_text(sim[:head] + "\nimport asiimov.sub as s\nfrom asiimov import other" + sim[head:])
    core, _ = runtime(tmp_path)
    assert {"sub/__init__.py", "sub/deep.py", "helpers.py", "other.py"} <= core
    assert lint.core_imports(str(pkg)) == core


# ---- Sim subclasses through aliases, attributes and subpackages (round 2, B3) ------------------------

ARMS = {
    "arm2.py": "from .sim import Sim as Engine\nclass Arm2(Engine):\n{body}",
    "arm3.py": "from asiimov import sim\nclass Arm3(sim.Sim):\n{body}",
    "arm4.py": "import asiimov.sim\nclass Arm4(asiimov.sim.Sim):\n{body}",
    "arm5.py": "import asiimov.sim as core\nBase = core.Sim\nclass Arm5(Base):\n{body}",
    "arm6.py": "from .scorecard import ArmSim as A\nclass Arm6(A):\n{body}",
    "arm7.py": "from . import arm2\nclass Arm7(arm2.Engine):\n{body}",
    "sub/arm8.py": "from ..sim import Sim\nclass Arm8(Sim):\n{body}",
}
ARM_BODY = "    def _lint_plant(self):\n        return 7 / 2\n"


def test_sim_subclasses_are_found_through_aliases_attributes_and_subpackages(tmp_path):
    pkg = _copy(tmp_path)
    (pkg / "sub").mkdir()
    (pkg / "sub" / "__init__.py").write_text("")
    for name, src in ARMS.items():
        (pkg / name).write_text(src.format(body=ARM_BODY))
    want = {name: [re.search(r"class (\w+)", src).group(1)] for name, src in ARMS.items()}
    found_ast = lint.sim_subclasses(str(pkg))
    _, found_rt = runtime(tmp_path)
    assert found_rt == found_ast
    assert {k: v for k, v in found_ast.items() if k in ARMS} == want
    hits = {(f.path, f.rule) for f in lint.lint_package(str(pkg))}
    assert {(f"asiimov/{name}", "int") for name in ARMS} <= hits, hits


# ---- calls canonicalised through the import table (round 2, B4) --------------------------------------

CANON_RNG_BAD = """
from builtins import hash as h
from builtins import id as ident
import builtins
import builtins as b
a = h(x)
c = ident(x)
d = builtins.hash(x)
e = b.id(x)
"""
CANON_RNG_GOOD = """
a = self.hash(x)
b = obj.id(x)
"""


def test_hash_and_id_are_flagged_through_builtins_and_aliases():
    assert sorted(f.line for f in found(CANON_RNG_BAD, "rng")) == [6, 7, 8, 9]
    assert found(CANON_RNG_GOOD, "rng") == []


CANON_ORDER_BAD = """
from .minds import dispatch as ask_all
from . import minds
from .sim import _parse as p
from asiimov import sim
class Sim:
    def _tick(self, reqs):
        a = ask_all(m, reqs, 1)
        b = minds.dispatch(m, reqs, 1)
        p(r)
        sim._parse(r)
"""
CANON_ORDER_GOOD = """
from .minds import dispatch as ask_all
from .sim import _parse as p
class Sim:
    def _commit(self):
        replies = ask_all(self.mind, reqs, self.workers)
        for aid in self._order(aids):
            apply(eid, p(replies[aid]))
    def _tick(self):
        self.dispatch(x)
        self._parse(x)
"""


def test_dispatch_and_parse_are_flagged_through_aliases():
    assert sorted(f.line for f in found(CANON_ORDER_BAD, "order")) == [8, 9, 10, 11]
    assert found(CANON_ORDER_GOOD, "order") == []


# ---- more nondeterminism sources (round 2, non-blocking) -------------------------------------------

MORE_RNG_BAD = """
from .events import *
for d, ds, fs in os.walk(root):
    pass
xs = sorted(os.walk(root))
ps = list(Path(root).iterdir())
gs = [p for p in Path(root).glob("*.json")]
rs = root.rglob("*")
lt = time.localtime()
gt = time.gmtime(0)
ct = time.ctime()
ft = datetime.datetime.fromtimestamp(0)
ev = os.environ.get("SEED")
ge = os.getenv("SEED")
ti = threading.get_ident()
"""
MORE_RNG_GOOD = """
ps = sorted(Path(root).iterdir())
gs = sorted(Path(root).glob("*.json"))
fs = sorted(glob.glob("*.json"))
"""


def test_star_imports_walks_path_order_local_clocks_environment_and_thread_ids_are_flagged():
    lines = sorted(f.line for f in found(MORE_RNG_BAD, "rng"))
    assert lines == [2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], lines  # 4 is `pass`
    assert found(MORE_RNG_GOOD, "rng") == []


def test_dunder_true_division_is_flagged():
    assert [f.line for f in found("a = x.__truediv__(y)\nb = x.__floordiv__(y)\n", "int")] == [1]


@pytest.mark.parametrize("stmt", ["import simhelpers\n", "try:\n    import simhelpers as _sh\nexcept ImportError:\n    _sh = None\n",
                                  "def _late():\n    from simhelpers import half\n    return half\n", "import numpy.random\n"])
def test_an_engine_import_from_outside_the_package_and_stdlib_is_a_finding(tmp_path, stmt):
    pkg = _copy(tmp_path)
    (pkg / "events.py").write_text((pkg / "events.py").read_text() + "\n" + stmt)
    found = [x for x in lint.lint_package(str(pkg)) if x.rule == "core"]
    assert len(found) == 1 and found[0].path.endswith("events.py"), found


def test_stdlib_and_package_imports_in_the_engine_are_not_foreign(tmp_path):
    pkg = _copy(tmp_path)
    (pkg / "events.py").write_text((pkg / "events.py").read_text() + "\nimport json\nfrom asiimov import rng as _r\n")
    assert lint.foreign_imports(str(pkg)) == []
