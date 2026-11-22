"""Bounded symbolic check of Rng.draw / Rng.at over their actual AST.

The checker parses the method source, insists the first statement (after an optional docstring) is
exactly `if hi < lo: raise ValueError(<constants>)` with no else, drops only that guard under the
precondition hi >= lo, and interprets the rest with Z3 over a small whitelist of node types. Any
other node raises Unsupported, which is reported as a failure, never skipped.

The defs are parsed from the class body in the module's file, not taken from inspect.getsource (which
follows __wrapped__). live_identity() then requires the live Rng.draw / Rng.at to be exactly those defs
(plain function in Rng.__dict__, no wrapper, same file/name/line, code == compiled from the file).

Model:
  lo, hi, index       z3 Int (Python ints are unbounded; so are z3 Ints)
  stream              z3 String (Python str equality == z3 String equality)
  self.counters       two z3 Arrays String->Bool (key present) and String->Int (value)
  self.raw(s, i)      a fresh z3 Int r per call, assumed 0 <= r < 2**64 (SHA-256 is not modelled)
  a % b               Python floor mod, encoded explicitly; b != 0 is a proof obligation
"""
import ast
import hashlib
import importlib.util
import inspect
import os
import sys
import builtins
import textwrap
import types

import z3

TIMEOUT_MS = 10_000
RAW_BOUND = 2 ** 64
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


class Unsupported(Exception):
    """A node outside the whitelist, or a guard of the wrong shape."""


# ----------------------------------------------------------------------------- guard shape
_GUARD = ast.parse("if hi < lo:\n    raise ValueError('x')").body[0]


def _split_guard(fn: ast.FunctionDef):
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]  # docstring
    if not body:
        raise Unsupported(f"{fn.name}: empty body")
    g = body[0]
    ok = (isinstance(g, ast.If) and ast.dump(g.test) == ast.dump(_GUARD.test) and not g.orelse
          and len(g.body) == 1 and isinstance(g.body[0], ast.Raise) and g.body[0].cause is None
          and isinstance(g.body[0].exc, ast.Call)
          and ast.dump(g.body[0].exc.func) == ast.dump(_GUARD.body[0].exc.func)
          and not g.body[0].exc.keywords
          and all(isinstance(a, ast.Constant) for a in g.body[0].exc.args))
    if not ok:
        raise Unsupported(f"{fn.name}: first statement is not exactly `if hi < lo: raise ValueError(...)`:"
                          f" {ast.unparse(g)!r}")
    return body[1:]


# ----------------------------------------------------------------------------- interpreter
def py_mod(a, b):
    """Python a % b (sign of the divisor). z3 `%` is Euclidean; they agree for b > 0."""
    return z3.If(b > 0, a % b, -((-a) % (-b)))


def _is_self_counters(n):
    return (isinstance(n, ast.Attribute) and n.attr == "counters" and isinstance(n.ctx, ast.Load)
            and isinstance(n.value, ast.Name) and n.value.id == "self")


class State:
    def __init__(self, tag):
        self.has = z3.Array(f"has0_{tag}", z3.StringSort(), z3.BoolSort())
        self.val = z3.Array(f"val0_{tag}", z3.StringSort(), z3.IntSort())

    def get(self, key, default):
        return z3.If(self.has[key], self.val[key], default)


class Interp:
    def __init__(self, env, tag):
        self.env = dict(env)
        self.tag = tag
        self.state = State(tag)
        self.has0, self.val0 = self.state.has, self.state.val
        self.assumptions = []
        self.obligations = []  # must hold for the run not to raise (e.g. modulus != 0)
        self.raws = []

    def expr(self, n):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id not in self.env:
                raise Unsupported(f"unknown name {n.id}")
            return self.env[n.id]
        if isinstance(n, ast.Constant) and type(n.value) is int:
            return z3.IntVal(n.value)
        if isinstance(n, ast.Constant) and type(n.value) is str:
            return z3.StringVal(n.value)
        if isinstance(n, ast.BinOp) and type(n.op) in (ast.Add, ast.Sub, ast.Mod):
            a, b = self.expr(n.left), self.expr(n.right)
            if not (z3.is_int(a) and z3.is_int(b)):
                raise Unsupported(f"non-int operands: {ast.unparse(n)}")
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            self.obligations.append(b != 0)
            return py_mod(a, b)
        if isinstance(n, ast.Call) and not n.keywords and isinstance(n.func, ast.Attribute):
            f = n.func
            if f.attr == "get" and _is_self_counters(f.value) and len(n.args) == 2:
                k, d = self.expr(n.args[0]), self.expr(n.args[1])
                self._need_str(k)
                return self.state.get(k, d)
            if f.attr == "raw" and isinstance(f.value, ast.Name) and f.value.id == "self" and len(n.args) == 2:
                s, i = self.expr(n.args[0]), self.expr(n.args[1])
                self._need_str(s)
                r = z3.Int(f"raw{len(self.raws)}_{self.tag}")
                self.assumptions += [r >= 0, r < RAW_BOUND]
                self.raws.append((r, s, i))
                return r
        raise Unsupported(f"expression not in whitelist: {ast.dump(n)}")

    @staticmethod
    def _need_str(k):
        if not z3.is_string(k):
            raise Unsupported("counter key / raw stream is not a str")

    def run(self, stmts):
        for s in stmts:
            if isinstance(s, ast.Assign) and len(s.targets) == 1:
                t, v = s.targets[0], self.expr(s.value)
                if isinstance(t, ast.Name) and t.id not in ("self",):
                    self.env[t.id] = v
                    continue
                if isinstance(t, ast.Subscript) and isinstance(t.ctx, ast.Store) and _is_self_counters(t.value):
                    k = self.expr(t.slice)
                    self._need_str(k)
                    if not z3.is_int(v):
                        raise Unsupported("counter value is not an int")
                    self.state.has = z3.Store(self.state.has, k, True)
                    self.state.val = z3.Store(self.state.val, k, v)
                    continue
            if isinstance(s, ast.Return) and s.value is not None:
                if isinstance(s.value, ast.Tuple):
                    return tuple(self.expr(e) for e in s.value.elts)
                return self.expr(s.value)
            raise Unsupported(f"statement not in whitelist: {ast.unparse(s)!r}")
        raise Unsupported("method falls off the end without a return")


# ----------------------------------------------------------------------------- properties
def _solve(assumptions, extra=None):
    s = z3.Solver()
    s.set("timeout", TIMEOUT_MS)
    s.add(*assumptions)
    if extra is not None:
        s.add(extra)
    return s, s.check()


def _prove(name, assumptions, post, model_vars):
    """PASS only if assumptions are satisfiable and assumptions & not(post) is unsat."""
    _, vac = _solve(assumptions)
    if vac != z3.sat:
        return {"property": name, "result": "FAIL", "reason": f"assumptions alone are {vac} (vacuous)"}
    s, r = _solve(assumptions, z3.Not(z3.And(*post)))
    if r == z3.unsat:
        return {"property": name, "result": "PASS"}
    if r == z3.sat:
        m = s.model()
        cex = {k: str(m.eval(v, model_completion=True)) for k, v in model_vars.items()}
        return {"property": name, "result": "FAIL", "reason": "counterexample", "counterexample": cex}
    return {"property": name, "result": "FAIL", "reason": f"solver returned {r} ({s.reason_unknown()})"}


def _fn(func_source, name):
    tree = ast.parse(textwrap.dedent(func_source))
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
    if len(fns) != 1:
        raise Unsupported(f"expected exactly one def {name}, found {len(fns)}")
    fn = fns[0]
    if fn.decorator_list or fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs or fn.args.defaults:
        raise Unsupported(f"{name}: unexpected signature")
    return fn


def check_draw(func_source):
    fn = _fn(func_source, "draw")
    if [a.arg for a in fn.args.args] != ["self", "stream", "lo", "hi"]:
        raise Unsupported("draw: signature is not (self, stream, lo, hi)")
    rest = _split_guard(fn)
    stream, lo, hi = z3.String("stream"), z3.Int("lo"), z3.Int("hi")
    it = Interp({"stream": stream, "lo": lo, "hi": hi}, "draw")
    ret = it.run(rest)
    if not (isinstance(ret, tuple) and len(ret) == 2):
        raise Unsupported("draw must return a 2-tuple")
    index, value = ret
    pre = [hi >= lo] + it.assumptions
    old = z3.If(it.has0[stream], it.val0[stream], 0)
    other = z3.String("other_key")
    mv = {"lo": lo, "hi": hi, "stream": stream, "value": value, "index": index, "old_counter": old,
          "new_counter": it.state.get(stream, z3.IntVal(0)), "other_key": other,
          **{f"raw{i}": r for i, (r, _, _) in enumerate(it.raws)}}
    return [
        _prove("P1 draw range", pre, it.obligations + [lo <= value, value <= hi], mv),
        _prove("P2 draw counter", pre, it.obligations + [
            index == old,
            it.state.has[stream], it.state.val[stream] == old + 1,
            z3.Implies(other != stream, z3.And(it.state.has[other] == it.has0[other],
                                               z3.Implies(it.has0[other], it.state.val[other] == it.val0[other])))],
            mv),
    ]


def check_at(func_source):
    fn = _fn(func_source, "at")
    if [a.arg for a in fn.args.args] != ["self", "stream", "index", "lo", "hi"]:
        raise Unsupported("at: signature is not (self, stream, index, lo, hi)")
    rest = _split_guard(fn)
    stream, index, lo, hi = z3.String("stream"), z3.Int("index"), z3.Int("lo"), z3.Int("hi")
    it = Interp({"stream": stream, "index": index, "lo": lo, "hi": hi}, "at")
    value = it.run(rest)
    if isinstance(value, tuple) or not z3.is_int(value):
        raise Unsupported("at must return an int expression")
    pre = [hi >= lo] + it.assumptions
    k = z3.String("any_key")
    mv = {"lo": lo, "hi": hi, "stream": stream, "index": index, "value": value, "any_key": k,
          **{f"raw{i}": r for i, (r, _, _) in enumerate(it.raws)}}
    return [
        _prove("P1 at range", pre, it.obligations + [lo <= value, value <= hi], mv),
        _prove("P3 at counters unchanged", pre, it.obligations + [
            it.state.has[k] == it.has0[k], it.state.val[k] == it.val0[k]], mv),
    ]


def check_methods(draw_source, at_source):
    """Run every property; Unsupported becomes a FAIL entry. Returns list of result dicts."""
    out = []
    for label, fn, src in (("draw", check_draw, draw_source), ("at", check_at, at_source)):
        try:
            out += fn(src)
        except Unsupported as e:
            out.append({"property": f"{label} (shape)", "result": "FAIL", "reason": f"Unsupported: {e}"})
    return out


PROVED = ("draw", "at")
IDENTITY = ("__init__", "raw", "draw", "at")  # live-code identity is required for each of these


def _method_nodes(module_source, filename="<rng.py>", names=PROVED):
    """{name: FunctionDef} for `names`, each defined directly (once) in the body of the unique top-level class Rng."""
    tree = ast.parse(module_source, filename=filename)
    cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Rng"]
    if len(cls) != 1:
        raise Unsupported("no unique class Rng")
    out = {}
    for name in names:
        fns = [n for n in cls[0].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
        if len(fns) != 1 or not isinstance(fns[0], ast.FunctionDef):
            raise Unsupported(f"class Rng must define exactly one plain def {name}, found {len(fns)}")
        out[name] = fns[0]
    return out


def method_sources(module_source):
    """(draw_source, at_source) from a whole rng.py source string (AST only; nothing is imported)."""
    nodes = _method_nodes(module_source)
    return tuple(ast.get_source_segment(module_source, nodes[n]) for n in ("draw", "at"))


def check_source(module_source):
    """AST-only check of a source string. Does not show that this code is what runs (see check_loaded)."""
    try:
        d, a = method_sources(module_source)
    except (Unsupported, SyntaxError) as e:
        return [{"property": "module shape", "result": "FAIL", "reason": f"Unsupported: {e!r}"}]
    return check_methods(d, a)


def sha(text):
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def _code_const(code, name):
    found = [c for c in code.co_consts if isinstance(c, types.CodeType) and c.co_name == name]
    if len(found) != 1:
        raise Unsupported(f"expected one compiled code object {name!r} in {code.co_name!r}, found {len(found)}")
    return found[0]


_ATTR_HOOKS = ("__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__init_subclass__",
               "__set_name__", "__class_getitem__")


def live_identity(mod, module_source, path):
    """Raise Unsupported unless mod.Rng.__init__ / raw / draw / at are exactly the plain functions compiled from
    the defs in the class body of `module_source` (the file at `path`): no wrapper (functools.wraps /
    __wrapped__), no decorator, same file, line, name, globals, builtins and an identical code object; Rng does
    not override __new__ or define a class-level `counters`; and a fresh Rng(1) is a plain Rng whose counters
    is a dict and which has no per-instance draw / at / raw.

    The module is imported into this process and is trusted not to tamper with the checker or the runtime
    (README, trusted base); these checks catch accidental wrapping or rebinding, not an adversary."""
    nodes = _method_nodes(module_source, path, IDENTITY)
    cls = vars(mod).get("Rng")
    if not isinstance(cls, type) or type(cls) is not type or cls.__bases__ != (object,):
        raise Unsupported("Rng is not a plain class (metaclass, bases or rebinding)")
    if cls.__module__ != mod.__name__ or cls.__qualname__ != "Rng":
        raise Unsupported(f"Rng was not defined in this module ({cls.__module__}.{cls.__qualname__})")
    hooks = [h for h in _ATTR_HOOKS if h in vars(cls)]
    if hooks:
        raise Unsupported(f"Rng defines attribute hooks {hooks}")
    if "ValueError" in vars(mod):
        raise Unsupported("module rebinds ValueError, so the guard may not raise the builtin")
    for attr in ("__new__", "counters"):
        if attr in vars(cls):
            raise Unsupported(f"Rng defines {attr} in its class dict")
    compiled_cls = _code_const(compile(module_source, path, "exec", dont_inherit=True), "Rng")
    for name, node in nodes.items():
        f = vars(cls).get(name)
        if type(f) is not types.FunctionType:
            raise Unsupported(f"Rng.__dict__[{name!r}] is {type(f).__name__}, not a plain function")
        if getattr(cls, name) is not f:
            raise Unsupported(f"Rng.{name} does not resolve to Rng.__dict__[{name!r}]")
        if inspect.unwrap(f) is not f or hasattr(f, "__wrapped__"):
            raise Unsupported(f"Rng.{name} is a wrapper (__wrapped__ present)")
        c = f.__code__
        want = (os.path.realpath(path), name, node.lineno)
        got = (os.path.realpath(c.co_filename), c.co_name, c.co_firstlineno)
        if got != want:
            raise Unsupported(f"Rng.{name} code is {got}, but the def in the class body is {want}")
        if f.__qualname__ != f"Rng.{name}" or f.__globals__ is not vars(mod) or f.__closure__ \
                or f.__defaults__ or f.__kwdefaults__:
            raise Unsupported(f"Rng.{name}: qualname, globals, closure or defaults differ from a plain def")
        if f.__builtins__ is not builtins.__dict__:
            raise Unsupported(f"Rng.{name} does not run with the real builtins")
        if c != _code_const(compiled_cls, name):
            raise Unsupported(f"Rng.{name} bytecode differs from the def compiled from {path}")
    try:
        r = cls(1)
    except Exception as e:  # noqa: BLE001
        raise Unsupported(f"Rng(1) raised {type(e).__name__}: {e}") from e
    if type(r) is not cls:
        raise Unsupported(f"Rng(1) returned a {type(r).__name__}, not an Rng")
    if type(vars(r).get("counters")) is not dict:
        raise Unsupported("Rng(1).counters is not a plain dict instance attribute")
    shadowed = [n for n in ("draw", "at", "raw") if n in vars(r)]
    if shadowed:
        raise Unsupported(f"Rng(1) shadows {shadowed} per instance")


def check_loaded(mod, path):
    """Check a loaded module: live-code identity, then the proofs over the defs parsed from its file.
    Returns (results, provenance)."""
    with open(path, "rb") as f:
        file_bytes = f.read()
    src = file_bytes.decode("utf-8")
    prov = {"z3_version": z3.get_version_string(), "rng_py_sha256": sha(file_bytes)}
    try:
        live_identity(mod, src, path)
        res = [{"property": "live code identity", "result": "PASS"}]
    except Unsupported as e:
        res = [{"property": "live code identity", "result": "FAIL", "reason": f"Unsupported: {e}"}]
    try:
        d, a = method_sources(src)
    except Unsupported as e:
        return res + [{"property": "module shape", "result": "FAIL", "reason": f"Unsupported: {e}"}], prov
    prov.update(draw_source_sha256=sha(d), at_source_sha256=sha(a))
    return res + check_methods(d, a), prov


def load_file(path, module_name):
    """Import the file at `path` as a fresh module (not registered in sys.modules)."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_file(path, module_name="_rng_under_check"):
    """Import `path` and check it like check_actual. A load error becomes a FAIL entry."""
    try:
        mod = load_file(path, module_name)
    except Exception as e:  # noqa: BLE001  a mutant that fails to import is a rejected mutant
        return [{"property": "module load", "result": "FAIL", "reason": f"{type(e).__name__}: {e}"}], {}
    return check_loaded(mod, path)


def check_actual():
    """Check the imported asiimov.rng. Returns (results, provenance). Import errors propagate to the caller."""
    from asiimov import rng
    return check_loaded(rng, rng.__file__)


if __name__ == "__main__":
    import json
    res, prov = check_actual()
    print(json.dumps({"provenance": prov, "results": res}, indent=2))
    raise SystemExit(0 if all(r["result"] == "PASS" for r in res) else 1)
