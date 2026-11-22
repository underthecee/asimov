# Verification: `Rng.draw` and `Rng.at`

Scope: `asiimov/rng.py`, only the methods `Rng.draw` and `Rng.at`.

- P1 range: for all ints `lo <= hi` and any raw value `0 <= r < 2**64`, `lo <= value <= hi` (both methods).
- P2: `draw` returns the old `counters.get(stream, 0)` as its index, sets `counters[stream]` to index + 1, and leaves every other key alone.
- P3: `at` leaves `counters` unchanged.
- P4: `hi < lo` raises `ValueError` before any counter changes (both methods).

P4 is **not** proved by Z3. It is established by the exact guard-shape check (the first statement must be
`if hi < lo: raise ValueError(<constants>)`, and the module must not rebind `ValueError`) plus the concrete
tests. It trusts CPython's semantics of that `if`/`raise`: nothing after it runs. Z3 proves P1 to P3 under
`hi >= lo` only.

SHA-256 correctness and statistical quality are out of scope.
These properties also do not prove the exact random value, the arguments passed
to `raw`, or the world's replay hash. A different in-range value can satisfy this
contract. The existing `tests/test_world.py` checks the golden world log separately.

## Evidence tiers
1. **Symbolic proof over the actual AST** (`symbolic_rng.py`). The defs of `draw` and `at` are parsed from
   the body of `class Rng` in the file the module was loaded from (not via `inspect.getsource`, which follows
   `__wrapped__`). A **live-code identity** check must also pass for `__init__`, `raw`, `draw` and `at`:
   - `Rng.__dict__[name]` is a plain function, and `getattr(Rng, name)` is that same object.
   - `inspect.unwrap(f) is f`, with no `__wrapped__`, no closure and no defaults.
   - `__globals__` is the module, and `__builtins__` is the real `builtins.__dict__`.
   - `f.__code__` has the def's `co_filename`, `co_name` and `co_firstlineno`, and is `==` to the code
     object compiled from the parsed file.

   `Rng` must be a plain class defined in that module. It must not have attribute hooks, its own `__new__`
   or a class-level `counters`. A fresh `Rng(1)` must have `type(r) is Rng` and a plain-dict
   `r.counters`, and must not have `draw`, `at` or `raw` in `vars(r)`.

   These checks catch the accidental cases the controls below exercise: a `functools.wraps` wrapper,
   reassigning a method after the class body, or an `__init__` that shadows `self.draw`. They are not a
   defense against a module that sets out to fool the checker (see the trusted base). The first
   statement must be exactly `if hi < lo: raise ValueError(...)`. Under `hi >= lo` only that guard is
   dropped, and the rest is interpreted with Z3 (10 s timeout per query). There's a whitelist of node
   types; anything else fails. `raw()` returns a fresh Int in `[0, 2**64)`. Python `%` is encoded
   explicitly as floor mod. For each property the assumptions must be sat (not vacuous), and
   assumptions plus the negated postcondition must be unsat. If it's sat, the model is reported as a
   counterexample. If it's unknown, the check fails.
2. **Trusted base** (not proved here):
   - CPython semantics, including the `raise ValueError` that P4 rests on.
   - Z3.
   - The interpreter's meaning for each whitelisted node matches CPython's.
   - `asiimov.rng` (or the `--source` file) is **imported into the checker process** and is trusted not to
     tamper with the checker or the Python runtime. For example, it must not patch `symbolic_rng`, `z3`,
     `inspect` or `builtins`, and must not rebind things after the checks run. The module is not treated as
     adversarial.
   - The real `Rng.raw` returns an int in `[0, 2**64)`. The proofs assume this.
   - `counters` is a plain dict on the instance.
   - `__init__`/`__new__` do not swap the class or shadow methods per instance. Only one `Rng(1)` is
     observed, so behavior that depends on the seed, or on later calls, is not ruled out.
3. **Concrete runtime tests** (`test_rng_methods.py`, plus the existing `tests/test_keyed_draws.py`).
   These use the real SHA raw, huge and negative bounds, single-point ranges, malformed ranges
   (counters byte-identical afterwards) and independent streams, and check that `at` reproduces `draw`.

Negative controls (`negative_controls.py`): these mutants must be rejected:
- modulus `+2`. The counterexample is re-run concretely and must break the range.
- counter `+2`
- the wrong stream gets bumped
- the guard changed to `hi <= lo`
- `at` writes `self.counters[stream]` (P3)
- `draw` returns `index + 1` as its index (P2)
- live-code mutants, imported from a temp file and checked with the same actual-module check
  (`symbolic_rng.check_file`):
  - a `functools.wraps` wrapper around `draw`
  - a plain reassignment of `Rng.at`
  - an `__init__` wrapper that sets `self.draw`
  - an edit inside the class-body `__init__` that sets `self.draw`. Every function-identity check passes on
    this one, so only the instance check catches it.

  The AST-only check passes on all four, so each must fail on `live code identity`, and each one is run
  concretely to show it really returns an out-of-range value. An unmodified copy loaded the same way must
  pass everything (positive control).

An always-PASS checker plugged into both harnesses (AST and live) must be reported as a failure.

A pytest check passes only if it exits 0, at least one test passes, and nothing is skipped, xfailed or
deselected.

## Run
    pip install pytest hypothesis jsonschema z3-solver
    python verification/run.py --out DIR          # writes DIR/receipt.json, nonzero exit on any failure
    python verification/run.py --out DIR --source MUTANT_rng.py   # demo: solver checks a different file

Exit status: 0 only for status `pass`. A `--source` run never has status `pass`. If all of its checks
passed, its status is `demo` and it exits 2. Otherwise its status is `fail` and it exits 1. Every other
failure also exits 1. The receipt also has `checks_all_passed`. The schema rejects a `pass` receipt whose
solver target is not `actual`.

`--source` targets differ by check, and the receipt says so. The solver and live-identity check import
the given file under a temp module name and check it. The negative controls are still generated from the
real `asiimov/rng.py`, and both pytest checks still run against the real `asiimov/rng.py`, never the
mutant. So a `--source` run does **not** test P4 concretely on the mutant, and its pytest results say nothing
about it. The receipt's `target.solver` (`kind`: `actual` or `source_override`, `path`) and `target.pytest`
fields, and a `target` on every check, record this. `source_hashes.rng_py_sha256` is always the real file.
With `--source`, `source_override_sha256` is the mutant, and `draw_source_sha256`/`at_source_sha256`
describe the solver target.

`DIR/receipt.json` is deleted at the start and written atomically at the end, whether the run passes
or fails. If a check crashes (for example a syntax error in `rng.py`, or a missing dependency), that check is
recorded as `fail` with its traceback, a fail receipt is still written, and the exit status is nonzero. A
crash in the runner itself becomes a `runner` fail check. If git fails or is missing, that becomes a
`git provenance` or `receipt provenance` fail check, with revision `unknown`. The one gap is an error while
writing the receipt file itself (for example a read-only `DIR`). That gives a nonzero exit but no receipt. The receipt records the git revision and dirty flag
(from `git status --porcelain -z`), Python and Z3 versions, source hashes, every check's command, target,
exit code and result, and the scope, trusted base and limitations.
The receipt also hashes `tests/test_keyed_draws.py`, the checker, controls, tests, runner and this
specification. A `pass` receipt must include the draw/at source hashes (schema `if`/`then`).
Optional dependencies are imported after the old receipt is removed. Each pytest
command has a 60-second timeout; a stalled test fails with exit code 124.

## Loop
Dori loop: the worker changes code, the checker runs (`run.py`), the verifier reads the receipt
(status, hashes match the reviewed revision, negative controls caught), and then the worker repairs.
The acceptance properties above stay fixed while the implementation is repaired.
