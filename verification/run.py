"""One command for the Rng.draw / Rng.at verification: python verification/run.py --out DIR

Deletes DIR/receipt.json first, runs every check, and writes a fresh receipt atomically at the end
(status "pass" or "fail"). A check that crashes (import error, syntax error in rng.py, ...) is recorded as a
failed check with its traceback, and a fail receipt is still written. Exit status is nonzero if anything failed.
"""
import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import re
import tempfile
import traceback

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
REPO = os.path.dirname(HERE)

SCOPE = ("Rng.draw and Rng.at in asiimov/rng.py. P1: for all ints lo <= hi and any raw value 0 <= r < 2**64, "
         "lo <= value <= hi (both methods). P2: draw returns the prior counters.get(stream, 0) as index, sets "
         "counters[stream] to index + 1 and leaves every other key unchanged. P3: at leaves counters unchanged. "
         "P4: hi < lo raises ValueError before any counter mutation (exact guard-shape check + concrete tests; "
         "not proved by Z3).")
TRUSTED = ["CPython semantics of int +, -, % (floor mod), dict.get and dict item assignment",
           "Z3 (Int/String/Array theories) answers sat/unsat correctly",
           "verification/symbolic_rng.py interprets each whitelisted AST node as CPython would "
           "(Python % encoded explicitly; z3 Ints unbounded like Python ints)",
           "CPython semantics of `if hi < lo: raise ValueError(...)` as the first statement (raises before any "
           "later statement runs); this is what P4 rests on, together with the concrete tests",
           "asiimov.rng (or the --source file) is imported into the checker process and is trusted not to tamper "
           "with the checker or the Python runtime (e.g. patching symbolic_rng, z3, inspect or builtins); the "
           "live-code identity checks catch accidental wrapping, rebinding or shadowing, not an adversarial module",
           "the real Rng.raw returns an int in [0, 2**64) (the proofs assume this; SHA-256 is not modelled)",
           "self.counters is a plain dict on the instance, and __init__/__new__ do not swap the class or shadow "
           "methods per instance beyond what the single Rng(1) construction check observes"]
LIMITS = ["raw() is abstracted to an arbitrary value in [0, 2**64); SHA-256 correctness and statistical "
          "quality are out of scope",
          "Arguments are modelled as int/str; calls with other types (bool, float, int subclasses) are not covered",
          "self.counters is modelled as a plain dict; concurrent mutation or a subclass overriding raw/counters "
          "is not covered",
          "P4 is shown by the exact guard shape plus concrete tests, not by Z3 or symbolic execution of the raise path",
          "With --source, the solver and live-identity checks target that file, but the pytest checks still run "
          "against the real asiimov/rng.py; P4 is not tested concretely on the --source file",
          "Hypothesis tests are sampled, not exhaustive",
          "Only the two methods are checked; callers (normal_milli, sim) are not"]

SCHEMA = {
    "type": "object",
    "required": ["schema_version", "status", "target", "git", "python_version", "z3_version", "source_hashes",
                 "checks", "proof_scope", "trusted_base", "limitations"],
    "properties": {
        "schema_version": {"const": 3},
        "status": {"enum": ["pass", "fail", "demo"]},
        "target": {"type": "object", "required": ["solver", "pytest"],
                   "properties": {"solver": {"type": "object", "required": ["kind", "path"],
                                             "properties": {"kind": {"enum": ["actual", "source_override"]},
                                                            "path": {"type": "string"}}},
                                  "pytest": {"type": "string"}}},
        "git": {"type": "object", "required": ["revision", "dirty"],
                "properties": {"revision": {"type": "string"}, "dirty": {"type": "boolean"}}},
        "python_version": {"type": "string"},
        "z3_version": {"type": "string"},
        "source_hashes": {"type": "object", "required": ["rng_py_sha256", "tests_test_keyed_draws_py_sha256"],
                          "additionalProperties": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
        "checks": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["name", "command", "exit_code", "result", "target"],
            "properties": {"name": {"type": "string"}, "command": {"type": "string"}, "target": {"type": "string"},
                           "exit_code": {"type": "integer"}, "result": {"enum": ["pass", "fail"]}}}},
        "proof_scope": {"type": "string"},
        "trusted_base": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    # A passing receipt must name the hashed method sources the proofs ran over, and must target the real file.
    "if": {"properties": {"status": {"const": "pass"}}},
    "then": {"properties": {"source_hashes": {"required": ["draw_source_sha256", "at_source_sha256"]},
                            "target": {"properties": {"solver": {"properties": {"kind": {"const": "actual"}}}}}}},
}

HASHED = {"rng_py": "asiimov/rng.py", "tests_test_keyed_draws_py": "tests/test_keyed_draws.py",
          **{"verification_" + n.replace(".", "_"): "verification/" + n
             for n in ("run.py", "symbolic_rng.py", "negative_controls.py", "test_rng_methods.py", "README.md")}}
PYTEST_TARGET = "asiimov/rng.py (the real module, imported by the tests; never the --source file)"


def git(*args):
    """(returncode, raw stdout). Not stripped: porcelain lines start with a meaningful space."""
    p = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)
    return p.returncode, p.stdout


def porcelain_paths():
    """Paths from `git status --porcelain -z` (both sides of a rename/copy). None if git failed."""
    rc, out = git("status", "--porcelain", "-z")
    if rc != 0:
        return None
    toks, paths, i = out.split("\0"), [], 0
    while i < len(toks):
        t = toks[i]
        i += 1
        if not t:
            continue
        xy, path = t[:2], t[3:]
        paths.append(path)
        if "R" in xy or "C" in xy:  # -z: the source path follows as its own token
            if i < len(toks) and toks[i]:
                paths.append(toks[i])
            i += 1
    return paths


def crashed(name, command, target):
    return {"name": name, "command": command, "target": target, "exit_code": 1, "result": "fail",
            "error": traceback.format_exc()}


def stage(name, command, target, fn):
    """Run fn() -> (ok, details); any exception becomes a failed check carrying the traceback."""
    try:
        ok, details = fn()
    except Exception:  # noqa: BLE001
        return crashed(name, command, target)
    return {"name": name, "command": command, "target": target, "exit_code": 0 if ok else 1,
            "result": "pass" if ok else "fail", "details": details}


_COUNT = re.compile(r"(\d+) (passed|failed|skipped|xfailed|xpassed|deselected|errors?|warnings?)")


def pytest_check(name, path, scratch):
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", path]
    try:
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=60,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                "HYPOTHESIS_STORAGE_DIRECTORY": os.path.join(scratch, "hypothesis")})
    except subprocess.TimeoutExpired:
        return {"name": name, "command": " ".join(cmd), "target": PYTEST_TARGET, "exit_code": 124,
                "result": "fail", "summary": "test command exceeded 60 seconds"}
    tail = (p.stdout + p.stderr).strip().splitlines()[-1:] or [""]
    counts = {}
    for n, kind in _COUNT.findall(tail[0]):
        counts[kind.rstrip("s") if kind.startswith(("error", "warning")) else kind] = int(n)
    # Pass needs exit 0, at least one test passed, and nothing skipped, xfailed or deselected.
    ok = (p.returncode == 0 and counts.get("passed", 0) > 0
          and not any(counts.get(k) for k in ("skipped", "xfailed", "xpassed", "deselected", "failed", "error")))
    return {"name": name, "command": " ".join(cmd), "target": PYTEST_TARGET, "exit_code": p.returncode,
            "result": "pass" if ok else "fail", "summary": tail[0], "counts": counts}


def file_hashes():
    out = {}
    for key, rel in HASHED.items():
        try:
            with open(os.path.join(REPO, rel), "rb") as f:
                out[key + "_sha256"] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            pass  # a missing file leaves its hash out; the checks that need it fail on their own
    return out


def write_atomic(out, receipt_path, receipt):
    fd, tmp = tempfile.mkstemp(dir=out, prefix=".receipt-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(receipt, f, indent=2)
        f.write("\n")
    os.replace(tmp, receipt_path)


def run_checks(a, out, checks, hashes, info):
    """Append every check to `checks`. Never raises for a check failure or crash."""
    mods = {}
    for mod in ("jsonschema", "z3", "symbolic_rng", "negative_controls"):
        try:
            mods[mod] = __import__(mod)
        except Exception:  # noqa: BLE001
            checks.append(crashed(f"import {mod}", f"import {mod}", "verification dependencies"))
    if "z3" in mods:
        info["z3_version"] = mods["z3"].get_version_string()
    sym, neg = mods.get("symbolic_rng"), mods.get("negative_controls")

    if a.source:
        path = os.path.abspath(a.source)
        info["target"] = {"kind": "source_override", "path": path}
        name = "z3 symbolic P1/P2/P3 over AST + live-code identity (--source file, imported from a temp name)"
        cmd = f"symbolic_rng.check_file({path!r})"
    else:
        path = os.path.join(REPO, "asiimov", "rng.py")
        info["target"] = {"kind": "actual", "path": path}
        name = "z3 symbolic P1/P2/P3 over AST + live-code identity (imported asiimov.rng)"
        cmd = "symbolic_rng.check_actual()"
    solver_target = f"{info['target']['kind']}: {path}"

    def solver():
        if sym is None:
            raise RuntimeError("symbolic_rng failed to import")
        results, prov = sym.check_file(path) if a.source else sym.check_actual()
        prov.pop("z3_version", None)
        file_sha = prov.pop("rng_py_sha256", None)
        if a.source:
            if file_sha:
                hashes["source_override_sha256"] = file_sha
        elif file_sha and file_sha != hashes.get("rng_py_sha256"):
            raise RuntimeError("asiimov/rng.py changed while the run was in progress")
        hashes.update(prov)  # draw/at source hashes of the solver target
        return all(r["result"] == "PASS" for r in results) and len(results) >= 5, results

    checks.append(stage(name, cmd, solver_target, solver))
    ctl_target = "mutants generated from the real asiimov/rng.py (in memory and in temp files)"
    if neg is None:
        checks.append({"name": "negative controls", "command": "import negative_controls", "target": ctl_target,
                       "exit_code": 1, "result": "fail", "error": "negative_controls failed to import"})
    else:
        checks.append(stage("negative controls: AST mutants rejected, counterexample replayed concretely",
                            "negative_controls.run_controls()", ctl_target, neg.run_controls))
        checks.append(stage("negative controls: live-code mutants (functools.wraps, reassignment, __init__ shadowing self.draw) rejected by "
                            "the actual-module check; unmodified copy passes",
                            "negative_controls.run_live_controls()", ctl_target, neg.run_live_controls))
        checks.append(stage("negative control: always-PASS checker is caught by both harnesses",
                            "negative_controls.inert_checker_detected()", "the harness itself",
                            neg.inert_checker_detected))

    checks.append(pytest_check("existing tests/test_keyed_draws.py", "tests/test_keyed_draws.py", out))
    checks.append(pytest_check("concrete verification/test_rng_methods.py", "verification/test_rng_methods.py", out))
    return mods.get("jsonschema")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="directory for receipt.json")
    ap.add_argument("--source", help="check this rng.py instead of the imported one (to demo a failing run)")
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    if os.path.commonpath([out, HERE]) == HERE:
        sys.exit("refusing to write the receipt inside verification/")
    os.makedirs(out, exist_ok=True)
    receipt_path = os.path.join(out, "receipt.json")
    if os.path.exists(receipt_path):
        os.remove(receipt_path)

    # Stale evidence is gone; optional dependencies and checker code are imported inside run_checks.
    hashes = file_hashes()
    checks, info = [], {"z3_version": "unavailable", "target": {"kind": "source_override" if a.source else "actual",
                                                              "path": a.source or "asiimov/rng.py"}}
    jsonschema = None
    try:
        jsonschema = run_checks(a, out, checks, hashes, info)
    except Exception:  # noqa: BLE001  a bug in the runner itself still yields a fail receipt
        checks.append(crashed("runner", "run.py run_checks", "the harness itself"))

    receipt = None
    try:
        rc, rev = git("rev-parse", "HEAD")
        paths = porcelain_paths()
        if rc != 0 or paths is None:
            checks.append({"name": "git provenance", "command": "git rev-parse HEAD; git status --porcelain -z",
                           "target": "the repository", "exit_code": rc or 1, "result": "fail",
                           "error": "git failed; the receipt cannot name a revision"})
        all_pass = bool(checks) and all(c["result"] == "pass" for c in checks)
        receipt = {
            "schema_version": 3,
            # A --source run is a demo of the checker on another file: never "pass".
            "status": ("demo" if a.source else "pass") if all_pass else "fail",
            "checks_all_passed": all_pass,
            "target": {"solver": info["target"], "pytest": PYTEST_TARGET},
            "git": {"revision": rev.strip() if rc == 0 else "unknown", "dirty": paths is None or bool(paths),
                    "dirty_outside_verification": paths is None
                    or any(not p.startswith("verification/") for p in paths)},
            "python_version": platform.python_version(),
            "z3_version": info["z3_version"],
            "source_hashes": hashes,
            "checks": checks,
            "proof_scope": SCOPE,
            "trusted_base": TRUSTED,
            "limitations": LIMITS,
        }
    except Exception:  # noqa: BLE001  e.g. git not on PATH
        checks.append(crashed("receipt provenance", "git rev-parse HEAD; git status --porcelain -z",
                              "the repository"))
        receipt = {"schema_version": 3, "status": "fail", "checks_all_passed": False,
                   "target": {"solver": info["target"], "pytest": PYTEST_TARGET},
                   "git": {"revision": "unknown", "dirty": True, "dirty_outside_verification": True},
                   "python_version": platform.python_version(), "z3_version": info["z3_version"],
                   "source_hashes": hashes, "checks": checks, "proof_scope": SCOPE, "trusted_base": TRUSTED,
                   "limitations": LIMITS}
    if jsonschema is None:
        receipt["status"] = "fail"
        receipt["schema_validation"] = "not run: jsonschema unavailable"
    else:
        try:
            jsonschema.validate(receipt, SCHEMA)
        except Exception as e:  # noqa: BLE001
            receipt["status"] = "fail"
            receipt["schema_validation"] = f"failed: {type(e).__name__}: {e}"
    write_atomic(out, receipt_path, receipt)
    for c in checks:
        print(f"{c['result'].upper():4}  {c['name']}")
    print(f"status: {receipt['status']}  receipt: {receipt_path}")
    # 0 only for "pass"; a --source demo whose checks all passed exits 2; anything that failed exits 1.
    return {"pass": 0, "demo": 2}.get(receipt["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
