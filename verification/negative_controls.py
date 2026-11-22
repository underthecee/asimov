"""Negative controls: mutants of rng.py the checker must reject, and an inert checker the harness must catch."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import symbolic_rng  # noqa: E402

ORIG_MOD = "% (hi - lo + 1)"


def _source():
    with open(os.path.join(symbolic_rng.REPO, "asiimov", "rng.py")) as f:
        return f.read()


def _mutate(src, old, new, count):
    if src.count(old) != count:
        raise RuntimeError(f"mutation anchor {old!r} found {src.count(old)} times, expected {count}")
    return src.replace(old, new)


def mutants():
    src = _source()
    return {
        "modulus_plus_2": (_mutate(src, ORIG_MOD, "% (hi - lo + 2)", 2), {"P1 draw range", "P1 at range"}),
        "counter_bump_2": (_mutate(src, "self.counters[stream] = index + 1", "self.counters[stream] = index + 2", 1),
                           {"P2 draw counter"}),
        "wrong_stream": (_mutate(src, "self.counters[stream] = index + 1", "self.counters['other'] = index + 1", 1),
                         {"P2 draw counter"}),
        "guard_le": (_mutate(src, "if hi < lo:", "if hi <= lo:", 2), {"draw (shape)", "at (shape)"}),
        "at_writes_counter": (_mutate(src, "        return lo + self.raw(stream, index)",
                                      "        self.counters[stream] = index\n        return lo + self.raw(stream, index)", 1),
                              {"P3 at counters unchanged"}),
        "draw_returns_index_plus_1": (_mutate(src, "return index, lo + self.raw", "return index + 1, lo + self.raw", 1),
                                      {"P2 draw counter"}),
    }


# Live-code mutants: the class-body defs are untouched (so an AST-only proof passes), but what runs differs.
WRAPS_DRAW = """

import functools
_orig_draw = Rng.draw


@functools.wraps(_orig_draw)
def _draw(self, stream, lo, hi):
    index, value = _orig_draw(self, stream, lo, hi)
    return index, value + (hi - lo + 1)


Rng.draw = _draw
"""
INIT_WRAPPER = """

_orig_init = Rng.__init__


def _init(self, seed):
    _orig_init(self, seed)
    self.draw = lambda stream, lo, hi: (0, hi + 1)


Rng.__init__ = _init
"""
INIT_BODY_SHADOW = ("        self.counters: dict[str, int] = {}\n",
                    "        self.counters: dict[str, int] = {}\n        self.draw = lambda stream, lo, hi: (0, hi + 1)\n")
REASSIGN_AT = """

def _at(self, stream, index, lo, hi):
    return hi + 1


Rng.at = _at
"""


def live_mutants():
    src = _source()
    return {
        "unmodified_copy": (src, set(), None),
        "functools_wraps_draw": (src + WRAPS_DRAW, {"live code identity"}, "draw"),
        "reassigned_at": (src + REASSIGN_AT, {"live code identity"}, "at"),
        # __init__ replaced by a wrapper that shadows self.draw per instance
        "init_wrapper_shadows_draw": (src + INIT_WRAPPER, {"live code identity"}, "draw"),
        # the class-body __init__ itself shadows self.draw (all identity checks on functions pass; the instance
        # check must catch it)
        "init_body_shadows_draw": (_mutate(src, *INIT_BODY_SHADOW, 1), {"live code identity"}, "draw"),
    }


def run_live_controls(checker=None):
    """Import each live mutant from a temp file and run the actual-module check on it (symbolic_rng.check_file).
    The unmodified copy must pass every property; each mutant must fail `live code identity`, and is shown to
    be a real behavior change by a concrete out-of-range value. Returns (ok, details)."""
    checker = checker or (lambda path: symbolic_rng.check_file(path)[0])
    ok, details = True, []
    with tempfile.TemporaryDirectory(prefix="rng-live-control-") as tmp:
        for k, (name, (src, expected, method)) in enumerate(live_mutants().items()):
            path = os.path.join(tmp, f"rng_{name}.py")
            with open(path, "w") as f:
                f.write(src)
            res = checker(path)
            failed = {r["property"] for r in res if r["result"] != "PASS"}
            d = {"mutant": name, "expected_fail": sorted(expected), "failed": sorted(failed),
                 "reasons": {r["property"]: r.get("reason", "") for r in res if r["result"] != "PASS"},
                 "ast_only_check_source_failed": sorted(r["property"] for r in symbolic_rng.check_source(src)
                                                        if r["result"] != "PASS")}
            if expected:
                d["caught"] = expected <= failed
                mod = symbolic_rng.load_file(path, f"_rng_live_control_{k}")
                v = mod.Rng(1).draw("s", 0, 0)[1] if method == "draw" else mod.Rng(1).at("s", 0, 0, 0)
                d["concrete"] = {"method": method, "lo": 0, "hi": 0, "value": v, "violates": v != 0}
                d["caught"] &= d["concrete"]["violates"]
            else:
                d["caught"] = not failed and len(res) >= 5  # positive control: identity + 4 proofs all PASS
            ok &= d["caught"]
            details.append(d)
    return ok, details


def concrete_range_violation(module_source, cex, method):
    """Re-run the mutant concretely with raw() returning the counterexample's raw value."""
    ns = {}
    exec(compile(module_source, "<mutant rng.py>", "exec"), ns)
    raw0 = int(cex["raw0"])

    class Fixed(ns["Rng"]):
        def raw(self, stream, index):
            return raw0

    lo, hi = int(cex["lo"]), int(cex["hi"])
    r = Fixed(0)
    v = r.draw("s", lo, hi)[1] if method == "draw" else r.at("s", 0, lo, hi)
    return {"lo": lo, "hi": hi, "raw": raw0, "value": v, "violates": not (lo <= v <= hi)}


def run_controls(checker=symbolic_rng.check_source):
    """Every mutant must FAIL on its expected properties. Returns (ok, details)."""
    ok, details = True, []
    for name, (src, expected) in mutants().items():
        res = checker(src)
        failed = {r["property"] for r in res if r["result"] != "PASS"}
        d = {"mutant": name, "expected_fail": sorted(expected), "failed": sorted(failed),
             "caught": expected <= failed}
        if name == "modulus_plus_2" and d["caught"]:
            d["replay"] = []
            for r in res:
                if r["property"] in expected:
                    if "counterexample" not in r:
                        d["caught"] = False
                        continue
                    method = "draw" if "draw" in r["property"] else "at"
                    rep = concrete_range_violation(src, r["counterexample"], method)
                    rep["solver_value"] = r["counterexample"]["value"]
                    d["replay"].append(rep)
                    d["caught"] &= rep["violates"] and str(rep["value"]) == rep["solver_value"]
        ok &= d["caught"]
        details.append(d)
    return ok, details


def always_pass(_src):
    return [{"property": p, "result": "PASS"} for p in
            ("P1 draw range", "P2 draw counter", "P1 at range", "P3 at counters unchanged")]


def inert_checker_detected():
    """Both harnesses must report the always-PASS checker as a failure."""
    ok, details = run_controls(always_pass)
    live_ok, live_details = run_live_controls(lambda _path: always_pass(None))
    return (not ok and not live_ok), {"ast": details, "live": live_details}


if __name__ == "__main__":
    import json
    ok, d = run_controls()
    live_ok, ld = run_live_controls()
    inert, _ = inert_checker_detected()
    print(json.dumps({"controls_ok": ok, "details": d, "live_controls_ok": live_ok, "live_details": ld,
                      "inert_detected": inert}, indent=2))
    raise SystemExit(0 if ok and live_ok and inert else 1)
