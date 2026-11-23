"""Static checks for the AGENTS.md rules (stdlib ast, no imports of the checked code).

Each rule here is one a reviewer has had to repeat; a lint makes it a check that runs with the tests
(tests/test_lints.py). A rule is only checked where it can be checked precisely: what this module can't
see (a state edit with no event, a float read from JSON at run time, a draw returned by a helper, taint that
crosses a function call) stays with replay and the invariants. The check is syntactic: names are resolved
through the module's imports (`import time as t` is `time`), and nothing is inferred about types.

    python -m asiimov.lint          # findings on the package, exit 1 if any

Rules:
  int     simulation arithmetic is integer-only (sim core)
  rng     randomness only through Rng; no wall clock, process or thread ids, environment, hash(), id(), dynamic
          or star imports, set or directory order (sim core)
  keyed   T22: a counter draw (Rng.draw/normal_milli) in the tick loop only on a registered own-actor stream,
          and a pick never indexes a list (or pops from it) with a draw (sim core)
  order   mind replies are fetched and applied only in Sim._commit, inside a loop over `self._order(...)` (or a
          list built from it in _commit), and only Sim._order reads the order setting (sim.py)

The sim core is every package module the engine imports (CORE_ROOTS and what they import, by any import
form: `from .x import y`, `from . import x`, `from asiimov import x`, `import asiimov.x [as y]`, subpackages
included), minus MIND_SIDE; each must be listed in SIM_CORE, or `python -m asiimov.lint` reports it. Plus the
class bodies of every Sim subclass anywhere in the package (ArmSim is run and replayed by fork.py; ProbeSim and
AuthorshipSim replay logs), all four rules; bases are resolved through imports, aliases and attributes
(`from .sim import Sim as Engine`, `sim.Sim`, `asiimov.sim.Sim`). tests/test_lints.py checks both sets
against what Python itself imports and defines (a subprocess), not against this module.

Known limits (replay and the invariants cover them): taint is per local name, not through attributes
(`self.k = rng.at(...)`; `xs[self.k]` is not seen) or across calls (a helper that returns a draw or a float);
`_from_order` accepts a loop over any list built from `self._order(...)` without checking that the order is the
sort key; names bound by assignment rather than import (`h = hash`, `ask = dispatch`) are not followed, except
module-level aliases of a Sim base.

A false positive is allowed on its own line with a comment, its reason mandatory:
    x = path / "events.jsonl"  # lint: allow int -- pathlib join, not a division
An unknown rule, a missing reason, or a suppression that suppresses nothing is itself a finding.
"""
import ast
import functools
import io
import os
import re
import sys
import tokenize
from dataclasses import dataclass

PKG = os.path.dirname(os.path.abspath(__file__))

# The sim core: what runs or rebuilds a world (asiimov.sim's imports within the package, plus replay and
# persist). tests/test_lints.py checks this against the actual import graph, so a new module the engine
# imports has to be put here or in MIND_SIDE.
SIM_CORE = ("__init__.py", "sim.py", "rng.py", "params.py", "worldmap.py", "memory.py", "events.py", "replay.py", "persist.py", "verbs.py", "things.py")
# Imported by the engine but on the far side of the mind interface: what a mind returns is origin "mind",
# recorded verbatim and replayed from the log, so it may sample (NoisyMind uses os.urandom; a reply carries
# float sampling settings). Not checked by int/rng/keyed. tests/test_lints.py pins this to exactly minds.py.
MIND_SIDE = ("minds.py",)
# Roots of the engine's import graph.
CORE_ROOTS = ("sim.py", "replay.py", "persist.py")

# keyed: streams a counter draw may use in the tick loop, by f-string prefix. A counter on such a stream is
# moved only by draws made for the stream's own agent. How many draws it takes may depend on who is around,
# but every draw it hands out lands on that same agent, so another agent's presence never shifts a draw that
# decides someone else (T22). Any draw that decides between agents, or whose stream others can move, goes
# through Rng.at(stream, tick, ...). Genesis (params.py) draws a fixed sequence from (seed, n_agents) before
# any tick, so it is not in scope for this rule.
DRAW_STREAMS = {
    "jitter:": "an agent's own wake-time jitter, drawn only when that agent schedules its wake",
    "steal:": "the thief's own steal and notice rolls, drawn only when that agent steals",
}
KEYED_GENESIS = ("params.py",)
RNG_CALLS = ("draw", "at", "raw", "normal_milli")
COUNTER_CALLS = ("draw", "normal_milli")

# keyed: an index into a list with a draw, allowed only where the list is no set of candidates (who is
# around can't change it). (file, function, list name) -> why.
INDEX_PICK_ALLOW = {
    ("params.py", "generate_population", "roles_pool"):
        "the role table built from the ROLE_SHARES constant at genesis; a pick of a role, not of a person",
    ("sim.py", "_sickness", "adults"):
        "every adult in the village: roles are fixed at genesis and agents are only added there, so the list "
        "is the same in a twin whatever anyone does; not a pick among those present. Reported, not changed "
        "(changing it changes the log: an engine bump)",
}

# order: functions besides _commit that may reach the mind, -> why. Each is called only from where its
# entry says (checked: ORDER_CALLERS), so the reply still reaches world state only in _commit's _order loop.
ORDER_MIND_REACH = {
    "_fly": "pace-2 (engine 38): called only by _commit; it files this tick's plan/reflect requests in "
            "state['inflight'] and returns the replies due now, unapplied; _commit applies them in its "
            "_order-ranked loop, and a lost call (restart) is asked again here without being applied",
    "_send": "pace-2 (engine 38): called only by _fly; asks one filed request (inline when workers <= 1, else on "
             "a thread) and returns a Future; nothing is applied",
}
ORDER_CALLERS = {"_fly": ("_commit",), "_send": ("_fly",)}
ORDER_READERS = ("_setup", "snapshot", "_order", "__init__")

# int: math functions that stay in the integers.
INT_MATH = ("isqrt", "gcd", "lcm", "comb", "perm", "factorial")
FLOAT_MODULES = ("math", "cmath", "statistics", "decimal", "fractions", "numpy")
FLOAT_CALLS = ("operator.truediv", "operator.itruediv", "operator.__truediv__", "operator.__itruediv__")
POW_CALLS = ("pow", "builtins.pow", "operator.pow", "operator.ipow", "operator.__pow__")
ENTROPY_MODULES = ("random", "secrets", "uuid", "numpy")
CLOCK = {"time": ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns",
                  "process_time", "process_time_ns", "localtime", "gmtime", "ctime", "asctime"),
         "datetime": ("now", "utcnow", "today", "fromtimestamp", "utcfromtimestamp"),
         "date": ("today", "fromtimestamp"),
         "os": ("urandom", "getrandom", "getpid", "getppid", "environ", "environb", "getenv", "getenvb"),
         "threading": ("get_ident", "get_native_id"), "_thread": ("get_ident", "get_native_id")}
DIR_ORDER = {"os": ("listdir", "scandir", "walk"), "glob": ("glob", "iglob")}
PATH_ORDER = ("iterdir", "glob", "rglob", "walk")  # pathlib.Path methods, on any object
TRUEDIV_DUNDER = ("__truediv__", "__rtruediv__", "__itruediv__")
DYNAMIC_IMPORT = ("__import__", "importlib.import_module", "importlib.__import__")
SET_OPS = (ast.BitOr, ast.BitAnd, ast.Sub, ast.BitXor)
SET_METHODS = ("union", "intersection", "difference", "symmetric_difference", "copy")
ORDERED_CONSUMERS = ("list", "tuple", "enumerate", "iter", "next", "zip")

RULES = ("int", "rng", "keyed", "order")
SUPPRESS = re.compile(r"#\s*lint:\s*allow\s+(\S+)(?:\s+--\s*(.*))?$")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self):
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


# ---- tree helpers ------------------------------------------------------------------------------------

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = FUNCS + (ast.Lambda,)


def _ancestors(node):
    while (node := getattr(node, "_lint_parent", None)) is not None:
        yield node


def _func(node) -> str | None:
    for a in _ancestors(node):
        if isinstance(a, FUNCS):
            return a.name
    return None


def _scope(node, kinds=FUNCS):
    """The nearest enclosing function (or the module)."""
    for a in _ancestors(node):
        if isinstance(a, kinds) or isinstance(a, ast.Module):
            return a
    return node


def _scope_chain(node):
    return [a for a in _ancestors(node) if isinstance(a, FUNCS + (ast.Module,))]


def _walk_scope(scope):
    """The nodes of one function's (or the module's) own body, not of functions or classes nested in it."""
    todo = list(scope.body) if isinstance(scope, (ast.Module, *FUNCS)) else [scope]
    while todo:
        n = todo.pop()
        yield n
        if isinstance(n, FUNCS + (ast.ClassDef, ast.Lambda)):
            continue
        todo.extend(ast.iter_child_nodes(n))


def _dotted(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _module_root(name: str | None) -> str:
    return (name or "").split(".")[0]


def _aliases(tree) -> dict[str, str]:
    """Local name -> the dotted name it was imported as, for every import in the module."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    root = a.name.split(".")[0]
                    out[root] = root
        elif isinstance(n, ast.ImportFrom):
            mod = "." * n.level + (n.module or "")
            for a in n.names:
                if a.name != "*":
                    out[a.asname or a.name] = f"{mod}.{a.name}" if mod else a.name
    return out


class Ctx:
    """One module being linted: its tree, the nodes in scope, and per-scope facts computed once."""

    def __init__(self, tree, path, roots):
        self.tree, self.path, self.base = tree, path, os.path.basename(path)
        self.roots = roots
        self.alias = _aliases(tree)
        self._sets, self._taint, self._rngfn, self._assigned, self._kind = {}, {}, {}, {}, {}
        self._fn = {}  # id(node) -> the name of the function it is in
        every, todo = [], [(tree, None)]
        while todo:  # one pass: parents, enclosing function names, and the node list
            n, name = todo.pop()
            every.append(n)
            self._fn[id(n)] = name
            inner = n.name if isinstance(n, FUNCS) else name
            for c in ast.iter_child_nodes(n):
                c._lint_parent = n
                todo.append((c, inner))
        self._nodes = every if roots == [tree] else [n for r in roots for n in ast.walk(r)]

    def func(self, node) -> str | None:
        return self._fn.get(id(node))

    def nodes(self):
        return self._nodes

    def canon(self, node) -> str | None:
        d = _dotted(node)
        if d is None:
            return None
        head, _, rest = d.partition(".")
        head = self.alias.get(head, head)
        return f"{head}.{rest}" if rest else head

    def assignments(self, scope):
        """name -> list of (kind, value): kind 'set' (=, :=, : T =), 'aug' (with its op) or 'other'."""
        if id(scope) in self._assigned:
            return self._assigned[id(scope)]
        out = {}

        def add(target, kind, value):
            if isinstance(target, ast.Name):
                out.setdefault(target.id, []).append((kind, value))
            elif isinstance(target, (ast.Tuple, ast.List)):
                for e in target.elts:
                    add(e.value if isinstance(e, ast.Starred) else e, "unpack", value)

        for n in _walk_scope(scope):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    add(t, "set", n.value)
            elif isinstance(n, ast.AnnAssign):
                add(n.target, "set" if n.value is not None else "other", n.value)
            elif isinstance(n, ast.AugAssign):
                add(n.target, "aug", n)
            elif isinstance(n, ast.NamedExpr):
                add(n.target, "set", n.value)
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
                add(n.target, "other", None)
            elif isinstance(n, ast.withitem) and n.optional_vars is not None:
                add(n.optional_vars, "other", None)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    out.setdefault((a.asname or a.name).split(".")[0], []).append(("other", None))
        if isinstance(scope, FUNCS):
            args = scope.args
            for a in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
                if a is not None:
                    out.setdefault(a.arg, []).append(("other", None))
        self._assigned[id(scope)] = out
        return out

    # -- sets: a local name whose every assignment in its function is a set
    def set_names(self, scope) -> set:
        if id(scope) not in self._sets:
            names, asg = set(), self.assignments(scope)
            changed = True
            while changed:
                changed = False
                for name, srcs in asg.items():
                    if name in names or not any(k == "set" for k, _ in srcs):
                        continue
                    if all((k == "set" and self._setlike(v, names, scope))
                           or (k == "aug" and isinstance(v.op, SET_OPS)) for k, v in srcs):
                        names.add(name)
                        changed = True
            self._sets[id(scope)] = names
        return self._sets[id(scope)]

    def _setlike(self, e, names, scope) -> bool:
        if isinstance(e, (ast.Set, ast.SetComp)):
            return True
        if isinstance(e, ast.Call) and self.canon(e.func) in ("set", "frozenset"):
            return True
        if isinstance(e, ast.Name):
            return e.id in names
        if isinstance(e, ast.BinOp) and isinstance(e.op, SET_OPS):
            return any(self._setlike(s, names, scope) or _keys_view(s) for s in (e.left, e.right))
        if isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr in SET_METHODS:
            return self._setlike(e.func.value, names, scope)
        return False

    def is_set(self, e) -> bool:
        """A set expression, or a local name bound only to sets in the innermost function assigning it."""
        if isinstance(e, ast.Name):
            for sc in _scope_chain(e):
                if e.id in self.assignments(sc):
                    return e.id in self.set_names(sc)
            return False
        sc = _scope(e)
        return self._setlike(e, self.set_names(sc), sc)

    # -- rng callables: x = self.rng.draw / getattr(self.rng, "draw")
    def rng_fns(self, scope) -> dict:
        if id(scope) not in self._rngfn:
            out = {}
            for name, srcs in self.assignments(scope).items():
                kinds = [_rng_ref(v) if k == "set" else None for k, v in srcs]
                if kinds and all(kinds) and len(set(kinds)) == 1:
                    out[name] = kinds[0]
            self._rngfn[id(scope)] = out
        return self._rngfn[id(scope)]

    def draw_kind(self, node) -> str | None:
        """Which Rng method a call is (draw, at, raw, normal_milli), or None."""
        if not isinstance(node, ast.Call):
            return None
        if id(node) not in self._kind:
            self._kind[id(node)] = self._draw_kind(node)
        return self._kind[id(node)]

    def _draw_kind(self, node) -> str | None:
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in RNG_CALLS:
            return f.attr
        if (k := _rng_ref(f)) is not None and not isinstance(f, ast.Attribute):
            return k  # getattr(rng, "draw")(...)
        if isinstance(f, ast.Name):
            for sc in _scope_chain(node):
                if f.id in self.assignments(sc):
                    return self.rng_fns(sc).get(f.id)
        return None

    def draws(self, node) -> bool:
        return any(self.draw_kind(c) for c in _walk_unkeyed(node))

    # -- taint: local names whose value comes from a draw, to a fixpoint
    def tainted(self, scope) -> set:
        if id(scope) not in self._taint:
            names, asg = set(), self.assignments(scope)
            changed = True
            while changed:
                changed = False
                for name, srcs in asg.items():
                    if name in names:
                        continue
                    for k, v in srcs:
                        e = v.value if k == "aug" else v
                        if k in ("set", "unpack", "aug") and e is not None and (
                                self.draws(e) or any(isinstance(c, ast.Name) and c.id in names
                                                     for c in _walk_unkeyed(e))):
                            names.add(name)
                            changed = True
                            break
            self._taint[id(scope)] = names
        return self._taint[id(scope)]

    def is_tainted(self, e) -> bool:
        names = set().union(*(self.tainted(sc) for sc in _scope_chain(e)))
        return self.draws(e) or any(isinstance(c, ast.Name) and c.id in names for c in _walk_unkeyed(e))


def _keys_view(e) -> bool:
    return (isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr == "keys"
            and not e.args and not e.keywords)


def _rng_ref(e) -> str | None:
    """`rng.draw` (uncalled) or `getattr(rng, "draw")`: which Rng method it names."""
    if isinstance(e, ast.Attribute) and e.attr in RNG_CALLS:
        return e.attr
    if (isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id == "getattr" and len(e.args) >= 2
            and isinstance(e.args[1], ast.Constant) and e.args[1].value in RNG_CALLS):
        return e.args[1].value
    return None


def _walk_unkeyed(node):
    """ast.walk, minus the key= argument of min/max/sorted (where a draw scores candidates)."""
    todo = [node]
    while todo:
        n = todo.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("min", "max", "sorted")
                    and isinstance(c, ast.keyword) and c.arg == "key"):
                continue
            todo.append(c)


def _outer(node) -> bool:
    """Not the `.value` of an attribute (so `a.b.c` is looked at once, as a whole)."""
    p = getattr(node, "_lint_parent", None)
    return not (isinstance(p, ast.Attribute) and p.value is node)


def _named(cx, node):
    """(canonical dotted name, parts) for an outermost Name/Attribute in Load context, else None."""
    if not isinstance(node, (ast.Name, ast.Attribute)) or not isinstance(node.ctx, ast.Load) or not _outer(node):
        return None
    c = cx.canon(node)
    return (c, c.split(".")) if c else None


def _builtin(c: str | None) -> str | None:
    """`builtins.hash` is `hash`."""
    return c[len("builtins."):] if c and c.startswith("builtins.") else c


def _call_name(cx, node) -> str | None:
    """The canonical name a call calls (`from builtins import hash as h; h(x)` is `hash`)."""
    return _builtin(cx.canon(node.func)) if isinstance(node, ast.Call) else None


def _imported_as(cx, node, name) -> bool:
    """`node` names `name`: bare, or through an import (`from .minds import dispatch as ask`, `minds.dispatch`,
    `asiimov.minds.dispatch`). An attribute of a non-imported object (`self.dispatch`) is not it."""
    d = _dotted(node)
    if d is None:
        return False
    if d == name and d not in cx.alias:
        return True
    head = d.split(".")[0]
    return head in cx.alias and (cx.canon(node) or "").split(".")[-1] == name


def _in_sorted(node) -> bool:
    return any(isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id == "sorted"
               for a in _ancestors(node))


def _nonneg_int(e) -> bool:
    return isinstance(e, ast.Constant) and type(e.value) is int and e.value >= 0


# ---- int -------------------------------------------------------------------------------------------

def check_int(cx) -> list[Finding]:
    out = []

    def f(node, msg):
        out.append(Finding(cx.path, node.lineno, "int", msg))

    for node in cx.nodes():
        if isinstance(node, ast.Constant) and isinstance(node.value, (float, complex)):
            f(node, f"float literal {node.value!r}")
        elif isinstance(node, (ast.BinOp, ast.AugAssign)) and isinstance(node.op, ast.Div):
            f(node, "true division `/` (use `//`)")
        elif isinstance(node, (ast.BinOp, ast.AugAssign)) and isinstance(node.op, ast.Pow):
            if not _nonneg_int(node.right if isinstance(node, ast.BinOp) else node.value):
                f(node, "`**` with an exponent that is not a non-negative int literal (can give a float)")
        elif isinstance(node, ast.Call) and _call_name(cx, node) in ("float", "complex"):
            f(node, f"`{_call_name(cx, node)}(` call")
        elif isinstance(node, ast.Call) and (_call_name(cx, node) in POW_CALLS or cx.canon(node.func) in POW_CALLS) \
                and len(node.args) == 2 and not node.keywords and not _nonneg_int(node.args[1]):
            f(node, f"`{cx.canon(node.func)}(x, e)` with an exponent that is not a non-negative int literal")
        elif isinstance(node, ast.Attribute) and node.attr in TRUEDIV_DUNDER:
            f(node, f"`.{node.attr}` is true division")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if _module_root(a.name) in FLOAT_MODULES and a.name not in ("math", "cmath"):  # math: uses checked
                    f(node, f"import of float module `{a.name}`")
        elif isinstance(node, ast.ImportFrom) and _module_root(node.module) in FLOAT_MODULES:
            names = [a.name for a in node.names]
            if node.module != "math" or any(n not in INT_MATH for n in names):
                f(node, f"import from float module `{node.module}`: {', '.join(names)}")
        elif (n := _named(cx, node)) is not None:
            c, parts = n
            if c in FLOAT_CALLS:
                f(node, f"`{c}` is true division")
            elif parts[0] in ("math", "cmath") and len(parts) >= 2 and (parts[0] == "cmath" or parts[1] not in INT_MATH):
                f(node, f"`{c}` is float arithmetic")
    return out


# ---- rng -------------------------------------------------------------------------------------------

def check_rng(cx) -> list[Finding]:
    out = []

    def f(node, msg):
        out.append(Finding(cx.path, node.lineno, "rng", msg))

    for node in cx.nodes():
        if isinstance(node, ast.Import):
            for a in node.names:
                if _module_root(a.name) in ENTROPY_MODULES:
                    f(node, f"import of `{a.name}` (draw through Rng)")
        elif isinstance(node, ast.ImportFrom):
            if _module_root(node.module) in ENTROPY_MODULES:
                f(node, f"import from `{node.module}` (draw through Rng)")
            if any(a.name == "*" for a in node.names):
                f(node, f"`from {'.' * node.level}{node.module or ''} import *` hides which names the sim core uses")
            for a in node.names:
                if a.name in CLOCK.get(_module_root(node.module), ()) or a.name in DIR_ORDER.get(node.module, ()):
                    f(node, f"import of `{node.module}.{a.name}`")
        elif (n := _named(cx, node)) is not None:
            c, parts = n
            if c in DYNAMIC_IMPORT:
                f(node, f"`{c}`: a dynamic import hides what the sim core runs")
            elif any(parts[i] in CLOCK.get(parts[i - 1], ()) for i in range(1, len(parts))):
                f(node, f"`{c}`: wall clock, process or thread id, environment or OS entropy in the sim core")
            elif len(parts) >= 2 and parts[-1] in DIR_ORDER.get(parts[-2], ()):
                if c == "os.walk" or not _in_sorted(node):
                    f(node, f"`{c}` order is the filesystem's: wrap it in sorted()"
                            + (" (and sort each step's lists)" if c == "os.walk" else ""))
            elif (len(parts) >= 2 and parts[0] in ("random", "secrets", "uuid")) or parts[:2] == ["numpy", "random"]:
                f(node, f"`{c}` (draw through Rng)")
        if isinstance(node, ast.Call) and _call_name(cx, node) in ("hash", "id"):
            f(node, f"`{cx.canon(node.func)}(` varies between processes (PYTHONHASHSEED, addresses)")
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in PATH_ORDER
              and node.func.attr not in DIR_ORDER.get(_dotted(node.func.value) and cx.canon(node.func.value), ())
              and not _in_sorted(node)):  # Path(...).iterdir()/glob()/rglob()/walk(); os.walk, glob.glob are above
            f(node, f"`.{node.func.attr}(` order is the filesystem's: wrap it in sorted()")
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and cx.is_set(node.iter):
            f(node.iter, "iterating a set: its order is not the log's (sort it)")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ORDERED_CONSUMERS \
                and any(cx.is_set(a) for a in node.args):
            f(node, f"`{node.func.id}(` of a set: its order is not the log's (sort it)")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join" \
                and node.args and cx.is_set(node.args[0]):
            f(node, "`.join(` of a set: its order is not the log's (sort it)")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "pop" \
                and not node.args and cx.is_set(node.func.value):
            f(node, "`.pop()` of a set takes an arbitrary element (sort it, or take min())")
    return out


# ---- keyed (T22) -------------------------------------------------------------------------------------

def _stream_prefix(cx, arg, func_node) -> str | None:
    """The literal start of a stream argument: a str constant, an f-string's leading text, or a local name
    assigned exactly once in the function from one of those."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.JoinedStr):
        head = arg.values[0] if arg.values else None
        return head.value if isinstance(head, ast.Constant) and isinstance(head.value, str) else ""
    if isinstance(arg, ast.Name) and func_node is not None:
        srcs = cx.assignments(func_node).get(arg.id, [])
        if len(srcs) == 1 and srcs[0][0] == "set":
            return _stream_prefix(cx, srcs[0][1], None)
    return None


def check_keyed(cx) -> list[Finding]:
    out = []

    def f(node, msg):
        out.append(Finding(cx.path, node.lineno, "keyed", msg))

    def pick(n, target_node, what):
        target = _dotted(target_node) or "<expr>"
        if target.split(".")[-1].isupper():
            return  # a module constant table (SYLLABLES): no candidates to add or drop
        if (cx.base, cx.func(n), target) in INDEX_PICK_ALLOW:
            return
        f(n, f"`{target}{what[0]}` {what[1]} by a draw: pick the candidate with the lowest keyed score instead (T22)")

    for node in cx.nodes():
        kind = cx.draw_kind(node)
        if cx.base not in KEYED_GENESIS and cx.base != "rng.py" and kind in COUNTER_CALLS:
            fn = next((a for a in _ancestors(node) if isinstance(a, FUNCS)), None)
            prefix = _stream_prefix(cx, node.args[0], fn) if node.args else None
            if prefix is None:
                f(node, "counter draw on a stream this lint can't read: name it with a literal or f-string")
            elif not any(prefix.startswith(p) for p in DRAW_STREAMS):
                f(node, f"counter draw on stream {prefix!r}...: use Rng.at(stream, tick, ...) (T22), "
                        "or register an own-actor stream in asiimov/lint.py DRAW_STREAMS")
        # Index picks: x[<draw>] or x.pop(<draw>), directly or through local names derived from one (to a
        # fixpoint, in the function or one enclosing it). A draw used only as the key of min/max/sorted over
        # the candidates is the lowest-keyed-score pick T22 asks for, not an index.
        if isinstance(node, ast.Subscript) and not isinstance(node.ctx, ast.Store) and cx.is_tainted(node.slice):
            pick(node, node.value, ("[...]", "indexed"))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "pop"
              and node.args and cx.is_tainted(node.args[0])):
            pick(node, node.func.value, (".pop(...)", "popped"))
    return sorted(set(out), key=lambda x: (x.line, x.message))


# ---- order (sim.py and Sim subclasses) -----------------------------------------------------------------

def _from_order(cx, e, fn) -> bool:
    """`self._order(...)`, or a local of `fn` every assignment of which is built from one (to a fixpoint)."""
    asg = cx.assignments(fn) if fn is not None else {}
    names, changed = set(), True
    while changed:
        changed = False
        for name, srcs in asg.items():
            if name not in names and srcs and all(
                    k == "set" and any((isinstance(c, ast.Call) and _dotted(c.func) == "self._order")
                                       or (isinstance(c, ast.Name) and c.id in names) for c in ast.walk(v))
                    for k, v in srcs):
                names.add(name)
                changed = True
    if isinstance(e, ast.Call) and _dotted(e.func) == "enumerate" and e.args:
        e = e.args[0]
    return (isinstance(e, ast.Call) and _dotted(e.func) == "self._order") or (isinstance(e, ast.Name) and e.id in names)


def check_order(cx) -> list[Finding]:
    out = []

    def f(node, msg):
        out.append(Finding(cx.path, node.lineno, "order", msg))

    for node in cx.nodes():
        if not isinstance(node, (ast.Name, ast.Attribute, ast.Call)):
            continue
        fn = cx.func(node)
        reach = fn in ("_commit", *ORDER_MIND_REACH)
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load) and _outer(node) \
                and _imported_as(cx, node, "dispatch") and fn != "_commit":
            f(node, f"`{_dotted(node)}` (dispatch) used in `{fn}`: mind requests are sent only from Sim._commit")
        elif isinstance(node, ast.Attribute) and node.attr == "decide" and isinstance(node.ctx, ast.Load) \
                and fn not in ORDER_MIND_REACH:
            f(node, "`.decide` called directly: go through dispatch in Sim._commit")
        elif (isinstance(node, ast.Attribute) and node.attr == "mind" and isinstance(node.value, ast.Name)
              and node.value.id == "self" and fn not in ("_setup", "__init__") and not reach):
            f(node, f"`self.mind` used in `{fn}`: the mind is reached only from Sim._commit")
        elif (isinstance(node, ast.Attribute) and node.attr == "order" and isinstance(node.value, ast.Name)
              and node.value.id == "self" and fn not in ORDER_READERS):
            f(node, f"`self.order` read in `{fn}`: who goes first is decided only in Sim._order")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self" \
                and node.attr in ORDER_CALLERS and fn not in ORDER_CALLERS[node.attr] \
                and not (isinstance(node.ctx, ast.Store) or fn == node.attr):
            f(node, f"`self.{node.attr}` used in `{fn}`: it reaches the mind, and only "
                    f"{', '.join(ORDER_CALLERS[node.attr])} may call it")
        elif isinstance(node, ast.Call) and _imported_as(cx, node.func, "_parse"):
            loop = next((a for a in _ancestors(node) if isinstance(a, (ast.For, ast.AsyncFor))), None)
            func = next((a for a in _ancestors(node) if isinstance(a, FUNCS)), None)
            if fn != "_commit" or loop is None or not _from_order(cx, loop.iter, func):
                f(node, "a mind reply is applied outside a loop over `self._order(...)` in Sim._commit")
    return out


CHECKS = {"int": check_int, "rng": check_rng, "keyed": check_keyed, "order": check_order}


# ---- suppressions -------------------------------------------------------------------------------------

def _suppressions(src, path):
    """{(line, rule): reason} from `# lint: allow <rule> -- <reason>` comments, and findings for bad ones."""
    sup, bad = {}, []
    if "lint:" not in src:
        return sup, bad
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError):
        return sup, bad
    for t in toks:
        if t.type != tokenize.COMMENT or not re.match(r"#\s*lint:", t.string):
            continue
        line = t.start[0]
        m = SUPPRESS.match(t.string.strip())
        if m is None:
            bad.append(Finding(path, line, "suppress", f"unreadable suppression {t.string!r}: "
                                                       "write `# lint: allow <rule> -- <reason>`"))
        elif m.group(1) not in RULES:
            bad.append(Finding(path, line, "suppress", f"suppression of unknown rule {m.group(1)!r} "
                                                       f"(rules: {', '.join(RULES)})"))
        elif not (m.group(2) or "").strip():
            bad.append(Finding(path, line, "suppress", f"suppression of `{m.group(1)}` without a reason "
                                                       "(`-- <why this is not a violation>`)"))
        else:
            sup[(line, m.group(1))] = m.group(2).strip()
    return sup, bad


def lint_source(src: str, path: str, rules=RULES, classes=None) -> list[Finding]:
    """Lint one module; with `classes` (names), only those class bodies."""
    tree = ast.parse(src, filename=path)
    roots = [tree] if classes is None else [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name in classes]
    cx = Ctx(tree, path, roots)
    found = set()
    for r in rules:
        found.update(CHECKS[r](cx))
    sup, out = _suppressions(src, path)
    used = set()
    for x in found:
        if (x.line, x.rule) in sup:
            used.add((x.line, x.rule))
        else:
            out.append(x)
    spans = [(r.lineno, r.end_lineno) for r in roots] if classes is not None else [(1, float("inf"))]
    for line, rule in sorted(set(sup) - used):
        if rule in rules and any(a <= line <= b for a, b in spans):
            out.append(Finding(path, line, "suppress", f"suppression of `{rule}` suppresses nothing: remove it"))
    return sorted(out, key=lambda x: (x.line, x.rule, x.message))


@functools.lru_cache(maxsize=512)
def _tree(src: str):
    """A parsed module, by source text (the package walk reads each file more than once). Read-only use."""
    return ast.parse(src)


def _parse(path):
    with open(path) as fh:
        src = fh.read()
    try:
        return _tree(src)
    except SyntaxError:
        return ast.parse(src, filename=path)  # the error with its file name


def _py_files(pkg: str) -> list[str]:
    """Every .py file under the package, subpackages included: paths relative to it, '/'-separated, sorted."""
    out = []
    for d, dirs, files in os.walk(pkg):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__" and not x.startswith("."))
        out += [os.path.relpath(os.path.join(d, x), pkg).replace(os.sep, "/") for x in files if x.endswith(".py")]
    return sorted(out)


def _modname(pkg: str, rel: str) -> str:
    """'sub/x.py' -> 'asiimov.sub.x'; '__init__.py' -> 'asiimov'."""
    parts = rel[:-3].split("/")
    return ".".join([os.path.basename(pkg)] + (parts[:-1] if parts[-1] == "__init__" else parts))


def _modfile(pkg: str, mod: str) -> str | None:
    """'asiimov.sub.x' -> 'sub/x.py' (or 'sub/x/__init__.py'), if it is a module of the package."""
    head, *parts = mod.split(".")
    if head != os.path.basename(pkg):
        return None
    rel = "/".join(parts)
    for cand in ([rel + ".py"] if parts else []) + [(rel + "/" if parts else "") + "__init__.py"]:
        if os.path.isfile(os.path.join(pkg, cand)):
            return cand
    return None


def _from_base(pkg: str, rel: str, node) -> str | None:
    """The absolute module an ImportFrom imports from (relative levels resolved against `rel`)."""
    if node.level == 0:
        return node.module
    here = _modname(pkg, rel).split(".")
    if not rel.endswith("__init__.py"):
        here = here[:-1]  # a module's package
    if node.level - 1 > len(here) - 1:
        return None
    base = here[:len(here) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _module_aliases(pkg: str, rel: str, tree) -> dict[str, str]:
    """Local name -> absolute dotted name, for every import in the module (relative ones resolved)."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and (base := _from_base(pkg, rel, n)):
            for a in n.names:
                if a.name != "*":
                    out[a.asname or a.name] = f"{base}.{a.name}"
    return out


def _imported_modules(pkg: str, rel: str, tree) -> set[str]:
    """Package modules (file names) a module imports, in any form, with the packages they sit in."""
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and (base := _from_base(pkg, rel, n)):
            mods.add(base)
            mods |= {f"{base}.{a.name}" for a in n.names if a.name != "*"}  # a submodule, if it is one
    files = set()
    for m in mods:
        parts = m.split(".")
        for i in range(1, len(parts) + 1):  # importing a.b.c runs a and a.b first
            if (f := _modfile(pkg, ".".join(parts[:i]))) is not None:
                files.add(f)
    return files


def core_imports(pkg: str = PKG) -> set[str]:
    """Package modules (paths relative to the package) reachable from CORE_ROOTS through every import form,
    with the package __init__ files Python runs on the way."""
    seen, todo = set(), list(CORE_ROOTS)
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        parts = rel.split("/")
        todo += [f for i in range(len(parts)) if os.path.isfile(os.path.join(pkg, f := "/".join(parts[:i] + ["__init__.py"])))]
        todo += sorted(_imported_modules(pkg, rel, _parse(os.path.join(pkg, rel))))
    return seen


def core_files(pkg: str = PKG) -> list[str]:
    """The files lint_package checks whole: what the engine imports minus MIND_SIDE (plus SIM_CORE, which is
    reported if it disagrees)."""
    have = [n for n in SIM_CORE if os.path.isfile(os.path.join(pkg, n))]
    return sorted((core_imports(pkg) - set(MIND_SIDE)) | set(have))


def sim_subclasses(pkg: str = PKG) -> dict[str, list[str]]:
    """file -> names of the classes in it that subclass sim.Sim (directly or through another), anywhere in the
    package. Bases are resolved through each module's imports and module-level aliases, so `Engine` after
    `from .sim import Sim as Engine`, `sim.Sim` and `asiimov.sim.Sim` are all Sim."""
    name = os.path.basename(pkg)
    classes, alias = {}, {}  # qualified class -> (file, class name, qualified bases); module -> {local: qualified}
    for rel in _py_files(pkg):
        tree = _parse(os.path.join(pkg, rel))
        mod = _modname(pkg, rel)
        local = _module_aliases(pkg, rel, tree)
        found = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        defined = {n.name for n in found}
        for n in tree.body:  # Engine = Sim / Engine = sim.Sim at module level
            if isinstance(n, ast.Assign) and (d := _dotted(n.value)) is not None:
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        local[t.id] = d
        alias[mod] = local

        def qual(d, local=local, defined=defined, mod=mod):
            head, _, rest = d.partition(".")
            if head in defined and head not in local:
                return f"{mod}.{d}"
            for _ in range(8):  # module-level aliases chain (Engine = Base; Base = sim.Sim)
                if head not in local or local[head] == d:
                    break
                d = local[head] + (f".{rest}" if rest else "")
                head, _, rest = d.partition(".")
                if head in defined:
                    return f"{mod}.{d}"
            return d

        for n in found:
            classes[f"{mod}.{n.name}"] = (rel, n.name, [qual(d) for b in n.bases if (d := _dotted(b))])

    def resolve(q):
        """Follow re-exports (`from .sim import Sim` in another module, then `that_module.Sim`)."""
        for _ in range(16):
            if q in classes:
                return q
            mod, _, attr = q.rpartition(".")
            nxt = alias.get(mod, {}).get(attr)
            if nxt is None or nxt == q:
                return q
            q = nxt
        return q

    root = f"{name}.sim.Sim"
    subs, changed = {root}, True
    while changed:
        changed = False
        for q, (_, _, bases) in classes.items():
            if q not in subs and any(resolve(b) in subs for b in bases):
                subs.add(q)
                changed = True
    out = {}
    for q, (rel, cls, _) in classes.items():
        if q != root and q in subs:
            out.setdefault(rel, []).append(cls)
    return {k: v for k, v in sorted(out.items())}


def unclassed(pkg: str = PKG) -> list[Finding]:
    """A module the engine imports that is neither SIM_CORE nor MIND_SIDE, or a SIM_CORE entry it doesn't."""
    out, imported, rel = [], core_imports(pkg), os.path.dirname(pkg)
    for name in core_files(pkg):
        path = os.path.relpath(os.path.join(pkg, name), rel)
        if name not in SIM_CORE:
            out.append(Finding(path, 1, "core", "the engine imports this module but it is not classed: add it to "
                                                "SIM_CORE (checked) or MIND_SIDE in asiimov/lint.py"))
        elif name not in imported:
            out.append(Finding(path, 1, "core", f"in SIM_CORE but not imported from {', '.join(CORE_ROOTS)}: remove it"))
    return out


def foreign_imports(pkg: str = PKG) -> list[Finding]:
    """An engine file importing anything but the standard library or the package itself: such a module (a
    repo-root helper, a third-party library) would run on the replay path without being linted. Imports
    inside functions or try/except count too."""
    out, top, rel = [], os.path.basename(pkg), os.path.dirname(pkg)
    for name in core_files(pkg):
        p = os.path.join(pkg, name)
        for node in ast.walk(_parse(p)):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            else:
                continue
            for m in mods:
                root = m.split(".")[0]
                if root != top and root not in sys.stdlib_module_names:
                    out.append(Finding(os.path.relpath(p, rel), node.lineno, "core",
                                       f"the engine imports {m}, which is neither stdlib nor {top}"))
    return out


def lint_package(pkg: str = PKG) -> list[Finding]:
    out = set(unclassed(pkg)) | set(foreign_imports(pkg))
    rel = os.path.dirname(pkg)
    core = core_files(pkg)

    def read(name):
        p = os.path.join(pkg, name)
        with open(p) as fh:
            return fh.read(), os.path.relpath(p, rel)

    for name in core:
        src, path = read(name)
        rules = ("int", "rng", "keyed") + (("order",) if name == "sim.py" else ())
        out.update(lint_source(src, path, rules))
    for name, classes in sim_subclasses(pkg).items():
        if name == "sim.py":
            continue  # all four rules already run on the whole of sim.py
        src, path = read(name)
        out.update(lint_source(src, path, ("order",) if name in core else RULES, classes=set(classes)))
    return sorted(out, key=lambda x: (x.path, x.line, x.rule, x.message))


if __name__ == "__main__":
    found = lint_package()
    for x in found:
        print(x)
    sys.exit(1 if found else 0)
