"""PR guard: report what a change touches that the merge policy cares about, and fail on weakened tests.

    python scripts/pr_guard.py --base BASE_SHA --head HEAD_SHA [--repo DIR]

A tripwire, not a proof: syntactic detection always has evasions, so every file that can change what pytest
collects or what CI installs is a protected path (reported, and the merge policy escalates to the owner), and
only clear weakening patterns fail the job.

Diffs merge-base(BASE, HEAD)..HEAD with rename detection (git diff -M), so a moved test file is not a deletion
and commits that landed on the base branch after the PR forked are not counted against it. It only runs git
and parses content (ast.parse, configparser, tomllib); nothing from the PR is executed, imported or installed.
Only .py files and pytest config files (pytest.ini, .pytest.ini, pytest.toml, .pytest.toml, setup.cfg, tox.ini,
pyproject.toml, conftest.py) are read, as bytes; any other path (images, binaries) is only named. Prints a JSON
summary (also appended to $GITHUB_STEP_SUMMARY when set):

- protected_paths_touched: tests/, verification/, .github/, AGENTS.md, scripts/pr_guard.py,
  scripts/check_guard_origin.py, scripts/review_status.py, and at any depth conftest.py, pytest.ini,
  .pytest.ini, pytest.toml, .pytest.toml, setup.cfg, tox.ini, pyproject.toml, __init__.py under a tests/ directory,
  root-level .py/.pth files, new top-level directories, and
  requirements*/constraints* .txt/.in files (or anything in a requirements/ directory)
- golden_or_engine_changed (+ golden_or_engine_detail): ENGINE_VERSION (asiimov/sim.py) or a module-level
  GOLDEN* constant (tests/test_world.py) changed
- renamed / moved: test functions that changed name within a file, or moved between test files (see below)
- new_test_skips: skips that can only reach tests the PR adds, and importorskip in a conftest.py the PR adds
- weakening, each with a kind:
  - "test file deleted" / "test file renamed out of collection" (out of pytest's test_*.py / *_test.py)
  - "test function removed": an existing collectable test is gone. Collectable means pytest's defaults: a
    test* function at module level, or a test* method of a class named Test* without __init__/__new__ (or of
    any unittest TestCase subclass). A test moved to another test file (same qualified name removed here and
    added there, with the effective assert total of the linked files not lower) is listed under `moved`. When
    N tests are removed and at least N collectable tests added in one file and its effective assert count did
    not drop, it is listed under `renamed`.
  - "test removed by rebinding": `del test_x`, `test_x = ...`, an import or class of that name after the def,
    in the module or class scope (if/try/with/for bodies included). A rebound test is not collectable.
  - "test class gains a constructor": a Test* class that held existing tests gained __init__ or __new__.
  - "collection disabled by __test__": a new statement binding or naming `__test__` (module or class
    `__test__ = False`, `test_x.__test__ = False`, setattr(..., "__test__", ...)) in an existing test file.
  - "asserts removed": fewer effective asserts in an existing test file. An assert on a constant truthy
    literal (`assert True`, `assert 1`), one with an `or <truthy literal>` operand, and one under a dead
    branch (`if False:`, `if 0:`, the `else` of `if True:`) does not count as effective.
  - "trivial assert added" / "dead assert added": more of those in an existing test file.
  - "skip/xfail added": a new reference to pytest.mark.skip/skipif/xfail, pytest.skip/xfail/importorskip/exit
    (and their .Exception), _pytest.outcomes skip/Skipped/xfail/XFailed/importorskip/exit/Exit, MARK_GEN
    skip/skipif/xfail, unittest.skip*/expectedFailure/SkipTest, import aliases resolved, in code that reaches
    existing tests: an existing test function, module or class level code, a helper or fixture, a non-test
    module or conftest.py. A skip inside a test the PR adds (or in a newly added test file), or a module-level
    marker (`needs_env = pytest.mark.skipif(...)`) used only by such tests, cannot weaken an existing test
    and is listed under `new_test_skips` instead; so is importorskip in a conftest.py the PR adds (a
    protected path, so it is escalated anyway).
  - "test shadowed by duplicate name": a test function name defined twice in one scope (only the last runs).
  - "parametrize cases reduced": an existing test's literal @pytest.mark.parametrize case count went down.
  - "pytest config": a change to addopts, testpaths, python_files, python_functions, python_classes or
    norecursedirs in pytest.ini / pytest.toml / setup.cfg / tox.ini / pyproject.toml, or an added --deselect, -k not,
    --ignore or collect_ignore line in those or a conftest.py.
  - "collection hook added or changed": in a conftest.py at any depth, any pytest_* name (and `import *`)
    added, changed or removed, however it is bound: a def at any depth (under if/try, in a plugin class), an
    assignment (`pytest_collection_modifyitems = _keep`, `pytest_plugins = [...]`), an import, or a
    "pytest_*" string; a change to an enclosing if/try header counts as a change.
  - "unparseable after change": a .py file that parsed before no longer does.

Exit 1 when there is weakening, unless the PR carries the label `guard-override` (read from the event payload
at $GITHUB_EVENT_PATH, no network; an unreadable payload means no labels); then it is still reported and the
exit is 0. Protected paths and golden/engine changes alone never fail: they are reported for the merge policy
to escalate. Known limits: docs/merge-gate.md.
"""
import argparse
import ast
import configparser
import difflib
import json
import os
import re
import subprocess
import sys
from collections import Counter

try:
    import tomllib
except ImportError:  # Python < 3.11: pyproject.toml falls back to the line check
    tomllib = None

PROTECTED_DIRS = ("tests/", "verification/", ".github/")
PROTECTED_FILES = ("AGENTS.md", "scripts/pr_guard.py", "scripts/check_guard_origin.py", "scripts/review_status.py")
# Anything that can change what pytest collects or what CI installs, at any depth.
PROTECTED_BASENAMES = ("conftest.py", "pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "setup.cfg",
                       "tox.ini", "pyproject.toml")
# `python -m pytest` puts the checkout first on sys.path, so a root-level module or a new top-level package
# (pytest.py, _pytest/, hypothesis/, sitecustomize.py, ...) can replace what CI imports. Any root .py/.pth file
# and any top-level directory not listed here is protected.
TOP_LEVEL = ("asiimov", "client", "docs", "plan", "scripts", "tests", "verification", ".github")
REQUIREMENTS = re.compile(r"(^|/)((requirements|constraints)[^/]*\.(txt|in)|requirements/[^/]+)$")
OVERRIDE_LABEL = "guard-override"
SKIPS = {"pytest.mark.skip", "pytest.mark.skipif", "pytest.mark.xfail", "pytest.skip", "pytest.xfail",
         "pytest.importorskip", "pytest.skip.Exception", "pytest.xfail.Exception", "unittest.skip",
         "unittest.skipIf", "unittest.skipUnless", "unittest.expectedFailure", "unittest.SkipTest",
         "unittest.case.SkipTest", "pytest.exit",
         # The implementation module behind pytest.skip / xfail / importorskip / exit and their exceptions.
         "_pytest.outcomes.skip", "_pytest.outcomes.Skipped", "_pytest.outcomes.xfail", "_pytest.outcomes.XFailed",
         "_pytest.outcomes.importorskip", "_pytest.outcomes.exit", "_pytest.outcomes.Exit",
         "_pytest.mark.MARK_GEN.skip", "_pytest.mark.MARK_GEN.skipif", "_pytest.mark.MARK_GEN.xfail",
         "_pytest.mark.structures.MARK_GEN.skip", "_pytest.mark.structures.MARK_GEN.skipif",
         "_pytest.mark.structures.MARK_GEN.xfail", "_pytest.skipping.skip", "_pytest.skipping.xfail"}
IMPORTORSKIP = {"pytest.importorskip", "_pytest.outcomes.importorskip"}
PARAMETRIZE = "pytest.mark.parametrize"
PYTEST_CONFIG = ("pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "setup.cfg", "tox.ini", "pyproject.toml", "conftest.py")
INI_SECTIONS = {"pytest.ini": ("pytest",), ".pytest.ini": ("pytest",), "tox.ini": ("pytest",), "setup.cfg": ("tool:pytest",)}
COLLECTION_KEYS = ("addopts", "testpaths", "python_files", "python_functions", "python_classes", "norecursedirs")
COLLECTION_KEY_LINE = re.compile(r"\b(" + "|".join(COLLECTION_KEYS) + r")\b")
DESELECT = re.compile(r"--deselect|-k\s*[\"']?\s*not\b|--ignore|collect_ignore")


def git(repo, *args):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def show(repo, sha, path):
    """File bytes at sha, or None when absent. Bytes, never decoded here: a file need not be UTF-8."""
    p = subprocess.run(["git", "-C", repo, "show", f"{sha}:{path}"], capture_output=True)
    return p.stdout if p.returncode == 0 else None


def text(data):
    return None if data is None else data.decode("utf-8", errors="replace")


def changes(repo, base, head):
    """[(status letter, old path, new path)] for base..head, renames followed."""
    out = git(repo, "diff", "--name-status", "-M", "-z", base, head).split("\0")
    res, i = [], 0
    while i < len(out) and out[i]:
        st = out[i][0]
        if st in "RC":
            res.append((st, out[i + 1], out[i + 2]))
            i += 3
        else:
            res.append((st, out[i + 1], out[i + 1]))
            i += 2
    return res


def protected(path):
    parts = path.split("/")
    return (path.startswith(PROTECTED_DIRS) or path in PROTECTED_FILES or parts[-1] in PROTECTED_BASENAMES
            or (parts[-1] == "__init__.py" and "tests" in parts[:-1]) or bool(REQUIREMENTS.search(path))
            or (len(parts) == 1 and path.endswith((".py", ".pth"))) or (len(parts) > 1 and parts[0] not in TOP_LEVEL))


def is_test_file(path):
    name = os.path.basename(path)
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def is_config(path):
    return os.path.basename(path) in PYTEST_CONFIG


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def parse(data):
    """ast of the bytes (coding declarations honoured), or None when absent or unparseable."""
    if data is None:
        return None
    try:
        return ast.parse(data)
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return None


def collectable_class(cls):
    """pytest's default rule: a class named Test* without __init__/__new__, or any unittest TestCase subclass."""
    if any((dotted(b) or "").endswith("TestCase") for b in cls.bases):
        return True
    ctor = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in ("__init__", "__new__")
               for n in cls.body)
    return cls.name.startswith("Test") and not ctor


def test_defs(tree):
    """[(qualified name, node)] of collectable test functions, duplicates kept: test_x at module level, or
    Class.test_x for methods of a collectable class (nested collectable classes included)."""
    out = []

    def walk(body, prefix):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
                out.append((prefix + n.name, n))
            elif isinstance(n, ast.ClassDef) and collectable_class(n):
                walk(n.body, prefix + n.name + ".")
    walk(tree.body, "")
    return out


def uncollectable_classes(tree):
    """{qualified class name} of classes named Test* that have test methods but a constructor."""
    out = set()

    def walk(body, prefix):
        for n in body:
            if isinstance(n, ast.ClassDef):
                tests = any(isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) and c.name.startswith("test")
                            for c in n.body)
                if n.name.startswith("Test") and tests and not collectable_class(n):
                    out.add(prefix + n.name)
                walk(n.body, prefix + n.name + ".")
    walk(tree.body, "")
    return out


def test_names(tree):
    return {name for name, _ in test_defs(tree)}


def scope_statements(body):
    """Statements of a module or class body in order, descending into if/try/with/for/while but not into
    function or class bodies."""
    for n in body:
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                for c in getattr(n, field, None) or []:
                    yield from scope_statements([c] if isinstance(c, ast.stmt) else c.body)


def bound_names(stmt):
    """Names a simple statement binds or deletes: assignment and for targets, imports, del."""
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split(".")[0] for a in stmt.names}
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
        targets = [stmt.target]
    elif isinstance(stmt, ast.Delete):
        targets = stmt.targets
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        targets = [i.optional_vars for i in stmt.items if i.optional_vars is not None]
    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {stmt.name}
    out, todo = set(), list(targets)
    while todo:  # names only, through tuple/list/starred unpacking; `test_x.attr = ...` rebinds nothing
        t = todo.pop()
        if isinstance(t, ast.Name):
            out.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            todo += t.elts
        elif isinstance(t, ast.Starred):
            todo.append(t.value)
    return out


def rebound_tests(tree):
    """{qualified test name} rebound or deleted after its def in the same module or class scope (`del test_x`,
    `test_x = None`, a later import or class of that name): pytest collects the last binding, so the test is
    gone. A second `def` of the same name is the shadowing check's business."""
    out = set()

    def walk(body, prefix):
        defined = {}  # name -> qualified test names that binding holds (a test function, or a class's tests)
        for n in scope_statements(body):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
                defined[n.name] = {prefix + n.name}  # a redefinition is a shadowed test, reported as such
                continue
            for name in bound_names(n) & set(defined):
                out.update(defined.pop(name))
            if isinstance(n, ast.ClassDef):
                held = {q for q, _ in test_defs(ast.Module(body=[n], type_ignores=[]))}
                if held:
                    defined[n.name] = {prefix + q for q in held}
                walk(n.body, prefix + n.name + ".")
    walk(tree.body, "")
    return out


def dunder_test(tree):
    """[ast dump] per statement that binds or names `__test__` (module or class `__test__ = False`,
    `test_x.__test__ = False`, setattr(obj, "__test__", ...)): any of them can switch collection off. A
    compound statement (if, with, a def's decorators) is represented by the expression naming it."""
    parent = {id(c): n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    out = []
    for n in ast.walk(tree):
        store = isinstance(getattr(n, "ctx", None), (ast.Store, ast.Del))
        if (isinstance(n, ast.Name) and n.id == "__test__" and store) or \
                (isinstance(n, ast.Attribute) and n.attr == "__test__" and store) or \
                (isinstance(n, ast.Constant) and n.value == "__test__"):
            stmt, expr = n, n
            while not isinstance(stmt, ast.stmt):
                expr, stmt = stmt, parent[id(stmt)]
            out.append(ast.dump(expr if hasattr(stmt, "body") else stmt))
    return out


def aliases(tree):
    alias = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            alias.update({a.asname or a.name: a.name for a in n.names})
        elif isinstance(n, ast.ImportFrom) and n.module:
            alias.update({a.asname or a.name: f"{n.module}.{a.name}" for a in n.names})
    return alias


def resolve(node, alias):
    name = dotted(node)
    if not name:
        return None
    root, _, rest = name.partition(".")
    return alias.get(root, root) + ("." + rest if rest else "")


def skip_refs(tree):
    """[(owner, resolved name)] per skip/xfail reference: owner is the qualified test function it sits in
    (decorators included), or None for anything else (module or class level, helpers, fixtures). A reference
    assigned to a module-level name (`needs_env = pytest.mark.skipif(...)`) is attributed to each place that
    name is used instead, one entry per use (None for a use outside a test); unused, it is one None entry."""
    alias = aliases(tree)
    owner = {}
    for name, fn in test_defs(tree):
        for n in ast.walk(fn):
            owner[id(n)] = name
    marker = {}
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                for n in ast.walk(stmt.value):
                    marker[id(n)] = targets[0].id
    uses = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            uses.setdefault(n.id, []).append(owner.get(id(n)))
    refs = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.Attribute, ast.Name)) and isinstance(getattr(n, "ctx", None), ast.Load):
            name = resolve(n, alias)
            if name in SKIPS:
                if id(n) in marker:
                    refs += [(o, name) for o in uses.get(marker[id(n)]) or [None]]
                else:
                    refs.append((owner.get(id(n)), name))
    return refs


def truthy_constant(node):
    return isinstance(node, ast.Constant) and bool(node.value)


def trivial(test):
    """True for an assert that can't fail on its value: a truthy literal, or an `or` with a truthy literal."""
    if truthy_constant(test):
        return True
    return isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or) and any(map(truthy_constant, test.values))


def assert_counts(tree):
    """{"effective", "trivial", "dead"} assert counts; dead = inside the never-taken branch of `if <literal>:`."""
    counts = {"effective": 0, "trivial": 0, "dead": 0}

    def visit(node, dead):
        if isinstance(node, ast.Assert):
            counts["dead" if dead else "trivial" if trivial(node.test) else "effective"] += 1
        if isinstance(node, ast.If) and isinstance(node.test, ast.Constant):
            taken = bool(node.test.value)
            for c in node.body:
                visit(c, dead or not taken)
            for c in node.orelse:
                visit(c, dead or taken)
            return
        for c in ast.iter_child_nodes(node):
            visit(c, dead)
    visit(tree, False)
    return counts


def param_cases(fn, alias):
    """Product of literal @pytest.mark.parametrize case counts (1 when none), or None if any is not a literal."""
    total = 1
    for d in fn.decorator_list:
        if not (isinstance(d, ast.Call) and resolve(d.func, alias) == PARAMETRIZE):
            continue
        values = d.args[1] if len(d.args) > 1 else next((k.value for k in d.keywords if k.arg == "argvalues"), None)
        if not isinstance(values, (ast.List, ast.Tuple, ast.Set)):
            return None
        total *= len(values.elts)
    return total


STMT_LISTS = ("body", "orelse", "finalbody", "handlers", "cases")


def hooks(tree):
    """{pytest_* name or "*": sorted [binding]} for every way a conftest can bind a pytest hook or plugin name:
    a def (at any depth: under if/try, in a class registered as a plugin), an assignment or other binding
    (`pytest_collection_modifyitems = _keep`, `pytest_plugins = [...]`), an import (`from x import pytest_y`, or
    `import *`), or a "pytest_*" string (globals()["pytest_y"] = ...). Each binding is its own dump plus the
    headers of the statements enclosing it, so flipping `if False:` to `if True:` above a hook changes it."""
    out = {}

    def add(name, ctx, node):
        out.setdefault(name, []).append(ctx + "|" + ast.dump(node))

    def header(n):
        return type(n).__name__ + ":" + "".join(ast.dump(v) if isinstance(v, ast.AST) else
                                                "".join(ast.dump(x) for x in v if isinstance(x, ast.AST))
                                                if isinstance(v, list) else repr(v)
                                                for f, v in ast.iter_fields(n) if f not in STMT_LISTS)

    def names_in(nodes):
        for top in nodes:
            for n in ast.walk(top):
                if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load) and n.id.startswith("pytest_"):
                    yield n.id
                elif isinstance(n, ast.Constant) and isinstance(n.value, str) and re.fullmatch(r"pytest_\w+", n.value):
                    yield n.value
                elif isinstance(n, ast.alias):
                    bound = "*" if n.name == "*" else (n.asname or n.name).split(".")[0]
                    if bound == "*" or bound.startswith("pytest_"):
                        yield bound

    def walk(body, ctx):
        for n in body:
            if isinstance(n, ast.excepthandler) or (hasattr(ast, "match_case") and isinstance(n, ast.match_case)):
                walk(n.body, ctx + "/" + header(n))
                continue
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name.startswith("pytest_"):
                add(n.name, ctx, n)
            heads = [v for f, v in ast.iter_fields(n) if f not in STMT_LISTS]
            heads = [x for v in heads for x in (v if isinstance(v, list) else [v]) if isinstance(x, ast.AST)]
            if not any(isinstance(getattr(n, f, None), list) and getattr(n, f) for f in STMT_LISTS):
                heads = [n]
            for name in sorted(set(names_in(heads))):
                add(name, ctx, n)
            for f in STMT_LISTS:
                sub = getattr(n, f, None)
                if isinstance(sub, list) and sub:
                    walk(sub, ctx + "/" + header(n) + "." + f)
    walk(tree.body if tree else [], "")
    return {k: sorted(v) for k, v in out.items()}


def module_constants(tree, want):
    """{name: literal value or source text} for module-level assignments whose name passes want(name)."""
    out = {}
    for n in tree.body if tree else []:
        targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AnnAssign) else []
        for t in targets:
            if isinstance(t, ast.Name) and want(t.id) and n.value is not None:
                try:
                    out[t.id] = ast.literal_eval(n.value)
                except ValueError:
                    out[t.id] = ast.unparse(n.value)
    return out


def golden_or_engine(name):
    return name == "ENGINE_VERSION" or name.startswith("GOLDEN")


def const_diff(name, old, new):
    if isinstance(old, dict) and isinstance(new, dict):
        d = {k: [old.get(k), new.get(k)] for k in sorted(set(old) | set(new), key=str) if old.get(k) != new.get(k)}
        return {"name": name, "changed_keys": d}
    return {"name": name, "old": old, "new": new}


def pytest_options(src, basename):
    """{key: value} of the pytest section, {} when the file or section is absent, None when unparseable."""
    if src is None:
        return {}
    try:
        if basename.endswith(".toml"):
            if tomllib is None:
                return None
            doc = tomllib.loads(src)
            table = doc.get("tool", {}).get("pytest", {}) if basename == "pyproject.toml" else doc.get("pytest", {})
            # pytest 9 reads native [tool.pytest] / [pytest] keys as well as the older ini_options table.
            opts = {**{k: v for k, v in table.items() if k != "ini_options"}, **table.get("ini_options", {})}
            return {k: json.dumps(v, sort_keys=True) for k, v in opts.items()}
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.read_string(src)
        for section in INI_SECTIONS.get(basename, ()):
            if cp.has_section(section):
                return {k: " ".join(v.split()) for k, v in cp.items(section)}
        return {}
    except Exception:  # noqa: BLE001 - any parse failure means "use the line check"
        return None


def config_weakening(where, old, new):
    """At most one "pytest config" entry per file: the collection keys that changed and the suspect diff lines."""
    basename = os.path.basename(where)
    keys, lines, structured = {}, [], False
    if basename != "conftest.py":
        o, n = pytest_options(old, basename), pytest_options(new, basename)
        if o is not None and n is not None:
            keys = {k: [o.get(k), n.get(k)] for k in COLLECTION_KEYS if o.get(k) != n.get(k)}
            structured = True
    for line in difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), lineterm="", n=0):
        if line.startswith(("+++", "---")) or not line.startswith(("+", "-")):
            continue
        if (line.startswith("+") and DESELECT.search(line)) or \
                (basename != "conftest.py" and not structured and COLLECTION_KEY_LINE.search(line)):
            lines.append(line)
    if not (keys or lines):
        return []
    return [{"kind": "pytest config", "path": where, "keys": keys, "lines": lines}]


def reaches(owner, keep, module_level):
    """Whether a skip owned by owner can reach an existing test: one inside a kept test, or module level code
    (when the file has existing tests at all)."""
    return (owner is None and module_level) or owner in keep


def find_moves(recs):
    """Tests moved between test files: a name removed from one file and added to another. Files linked by
    candidate moves form groups; a group's moves are accepted only when the group's effective assert total
    did not drop. Returns ([{"from", "to", "names"}], {path: moved out}, {path: moved in}, {grouped paths})."""
    gone = {r["path"]: set(r["old_names"] - r["new_names"]) for r in recs if r["existing"]}
    added = {r["path"]: set(r["new_names"] - r["old_names"]) for r in recs}
    by_path = {r["path"]: r for r in recs}
    edges = []
    for src in sorted(gone):
        for dst in sorted(added):
            m = gone[src] & added[dst] if dst != src else set()
            if m:
                edges.append((src, dst, m))
                gone[src] -= m
                added[dst] -= m
    group = {}

    def find(x):
        while group.get(x, x) != x:
            x = group[x]
        return x
    for src, dst, _ in edges:
        group[find(src)] = find(dst)
    totals = {}
    for path in {p for e in edges for p in e[:2]}:
        t = totals.setdefault(find(path), [0, 0])
        t[0] += by_path[path]["a_old"]
        t[1] += by_path[path]["a_new"]
    moves, out, into, grouped = [], {}, {}, set()
    for src, dst, m in edges:
        before, after = totals[find(src)]
        if after >= before:
            moves.append({"from": src, "to": dst, "names": sorted(m)})
            out.setdefault(src, set()).update(m)
            into.setdefault(dst, set()).update(m)
            grouped |= {src, dst}
    return moves, out, into, grouped


def check(repo, base, head):
    diff_base = git(repo, "merge-base", base, head).strip()
    touched, golden, renamed, new_skips = [], [], [], []
    slots, recs = [], []  # weakening per changed file, in diff order; test files judged after moves are known
    for st, old_path, new_path in changes(repo, diff_base, head):
        for p in dict.fromkeys((old_path, new_path)):
            if protected(p) and p not in touched:
                touched.append(p)
        py = old_path.endswith(".py") or new_path.endswith(".py")
        if not (py or is_config(old_path) or is_config(new_path)):
            continue  # never read: images, binaries, docs
        weak = []
        slots.append(weak)
        old = show(repo, diff_base, old_path) if st != "A" else None
        new = show(repo, head, new_path) if st != "D" else None
        where = new_path if st != "D" else old_path
        if is_config(old_path) or is_config(new_path):
            weak += config_weakening(where, text(old), text(new))
        if not where.endswith(".py"):
            continue
        t_old, t_new = parse(old), parse(new)
        if t_old is not None and new is not None and t_new is None:
            weak.append({"kind": "unparseable after change", "path": where})
            continue
        conftest = "conftest.py" in (os.path.basename(old_path), os.path.basename(new_path))
        if conftest:
            h_old, h_new = hooks(t_old), hooks(t_new)
            for k in sorted(set(h_old) | set(h_new)):
                if h_old.get(k) != h_new.get(k):
                    change = "added" if k not in h_old else "removed" if k not in h_new else "changed"
                    weak.append({"kind": "collection hook added or changed", "path": where, "hook": k,
                                 "change": change})
        if t_new is not None:
            c_old, c_new = module_constants(t_old, golden_or_engine), module_constants(t_new, golden_or_engine)
            for k in sorted(set(c_old) | set(c_new)):
                if c_old.get(k) != c_new.get(k):
                    golden.append({"path": where, **const_diff(k, c_old.get(k), c_new.get(k))})
        if st == "D":
            if is_test_file(old_path):
                weak.append({"kind": "test file deleted", "path": old_path})
            continue
        if t_new is None:
            continue
        test_new = is_test_file(new_path)
        existing = st != "A" and t_old is not None
        if existing and is_test_file(old_path) and not test_new:
            weak.append({"kind": "test file renamed out of collection", "path": old_path, "to": new_path})
            continue
        if not test_new:
            # Not a test module: every skip reference can reach existing tests (helpers, fixtures, conftest),
            # except importorskip in a conftest.py the PR adds, which is reported (it is a protected path).
            r_old, r_new = skip_refs(t_old) if existing else [], skip_refs(t_new)
            fresh = 0
            if conftest and not existing:
                fresh = sum(name in IMPORTORSKIP for _, name in r_new)
            if len(r_new) - fresh > len(r_old):
                weak.append({"kind": "skip/xfail added", "path": where, "count": len(r_new) - fresh - len(r_old)})
            if fresh:
                new_skips.append({"path": where, "count": fresh, "conftest": True})
            continue
        existing = existing and is_test_file(old_path)  # a module renamed into collection counts as new
        names_new = [n for n, _ in test_defs(t_new)]
        dup_old = {}
        if existing:
            for n, _ in test_defs(t_old):
                dup_old[n] = dup_old.get(n, 0) + 1
        shadowed = sorted({n for n in names_new if names_new.count(n) > 1 and names_new.count(n) > dup_old.get(n, 0)})
        if shadowed:
            weak.append({"kind": "test shadowed by duplicate name", "path": where, "names": shadowed})
        rb_new = rebound_tests(t_new)
        rb_old = rebound_tests(t_old) if existing else set()
        rec = {"path": where, "existing": existing, "t_old": t_old if existing else None, "t_new": t_new,
               "old_names": test_names(t_old) - rb_old if existing else set(), "new_names": test_names(t_new) - rb_new,
               "a_old": assert_counts(t_old)["effective"] if existing else 0,
               "a_new": assert_counts(t_new)["effective"], "head": weak, "tail": []}
        recs.append(rec)
        if not existing:
            continue
        if Counter(dunder_test(t_new)) - Counter(dunder_test(t_old)):
            weak.append({"kind": "collection disabled by __test__", "path": where})
        if rb_new - rb_old:
            weak.append({"kind": "test removed by rebinding", "path": where, "names": sorted(rb_new - rb_old)})
        old_all = test_names(t_old)
        lost = sorted(c for c in uncollectable_classes(t_new) - uncollectable_classes(t_old)
                      if any(n.startswith(c + ".") for n in old_all))
        if lost:
            weak.append({"kind": "test class gains a constructor", "path": where, "classes": lost})
        a_old, a_new = assert_counts(t_old), assert_counts(t_new)
        tail = rec["tail"]
        for k, kind in (("trivial", "trivial assert added"), ("dead", "dead assert added")):
            if a_new[k] > a_old[k]:
                tail.append({"kind": kind, "path": where, "before": a_old[k], "after": a_new[k]})
        alias_old, alias_new = aliases(t_old), aliases(t_new)
        fns_old = dict(test_defs(t_old))
        for name, fn in test_defs(t_new):
            if name in fns_old:
                p_old, p_new = param_cases(fns_old[name], alias_old), param_cases(fn, alias_new)
                if p_old is not None and p_new is not None and p_new < p_old:
                    tail.append({"kind": "parametrize cases reduced", "path": where, "name": name,
                                 "before": p_old, "after": p_new})
    moved, moved_out, moved_in, grouped = find_moves(recs)
    carried = {}  # skip references that travelled with a moved test count as already reaching it
    for m in moved:
        src = next(r for r in recs if r["path"] == m["from"])
        carried[m["to"]] = carried.get(m["to"], 0) + sum(o in m["names"] for o, _ in skip_refs(src["t_old"]))
    for r in recs:
        path, weak = r["path"], r["head"]
        into = moved_in.get(path, set())
        gone = sorted(r["old_names"] - r["new_names"] - moved_out.get(path, set()))
        added = sorted(r["new_names"] - r["old_names"] - into)
        if r["existing"]:
            asserts_ok = r["a_new"] >= r["a_old"] or path in grouped
            is_rename = bool(gone) and len(added) >= len(gone) and asserts_ok
            if is_rename:
                renamed.append({"path": path, "from": gone, "to": added})
            elif gone:
                weak.append({"kind": "test function removed", "path": path, "names": gone})
            if not asserts_ok:
                weak.append({"kind": "asserts removed", "path": path, "before": r["a_old"], "after": r["a_new"]})
            keep = ((r["old_names"] | r["new_names"]) if is_rename else (r["old_names"] & r["new_names"])) | into
            module_level = True
            reach_old = sum(reaches(o, keep, True) for o, _ in skip_refs(r["t_old"]))
        else:
            keep, module_level, reach_old = into, bool(into), 0
        weak += r["tail"]
        r_new = skip_refs(r["t_new"])
        reach_new = sum(reaches(o, keep, module_level) for o, _ in r_new)
        reach_old += carried.get(path, 0)
        if reach_new > reach_old:
            weak.append({"kind": "skip/xfail added", "path": path, "count": reach_new - reach_old})
        if len(r_new) > reach_new:
            new_skips.append({"path": path, "count": len(r_new) - reach_new})
    return {"diff_base": diff_base, "protected_paths_touched": touched,
            "golden_or_engine_changed": bool(golden), "golden_or_engine_detail": golden,
            "weakening": [w for slot in slots for w in slot], "renamed": renamed, "moved": moved,
            "new_test_skips": new_skips}


def labels(event_path):
    """Label names from the event payload; none when it is missing or unreadable (so no override)."""
    try:
        with open(event_path) as f:
            ev = json.load(f)
        return [lb.get("name") for lb in (ev.get("pull_request") or {}).get("labels") or []]
    except (TypeError, OSError, ValueError, AttributeError):
        return []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH"))
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    a = ap.parse_args(argv)
    res = {"base": a.base, "head": a.head, **check(a.repo, a.base, a.head)}
    res["override"] = OVERRIDE_LABEL in labels(a.event)
    res["result"] = "pass" if not res["weakening"] else "overridden" if res["override"] else "fail"
    out = json.dumps(res, indent=2, default=str)
    print(out)
    if a.summary:
        with open(a.summary, "a") as f:
            f.write(f"## PR guard: {res['result']}\n\n```json\n{out}\n```\n")
    return 1 if res["result"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
