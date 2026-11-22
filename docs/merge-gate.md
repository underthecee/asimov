# Merge gate

Three signals on every PR, each checkable on the PR's head commit instead of taken on trust:

- `verify` (.github/workflows/verify.yml): `python verification/run.py --out DIR` with pinned z3-solver,
  pytest, hypothesis and jsonschema. Fails on a nonzero exit or a receipt whose status is not `pass`; the
  receipt is uploaded as the `verification-receipt` artifact. Scope and limits: verification/README.md.
- `guard` (.github/workflows/guard.yml): `scripts/pr_guard.py` diffs merge-base..head and prints JSON with
  `protected_paths_touched`, `golden_or_engine_changed`, `weakening`, `renamed`, `moved` and `new_test_skips`.
  It is a tripwire: syntactic detection always has evasions, so every file that can change what pytest
  collects or what CI installs (any conftest.py, pytest.ini, .pytest.ini, pytest.toml, .pytest.toml, setup.cfg,
  tox.ini, pyproject.toml, `__init__.py` under tests/, requirements/constraints files, root-level .py/.pth files
  and new top-level directories that could shadow pytest or its plugins, plus tests/, verification/,
  .github/ and the gate scripts) is a protected path, and only clear weakening patterns fail the job.
  Weakening (the kinds are listed in the script's docstring: a deleted test file, a removed or rebound test,
  a test class given a constructor, `__test__` set, fewer effective asserts, a trivial or dead-branch assert,
  an added skip/xfail/exit reaching an existing test, a shadowed test name, fewer literal parametrize cases, a
  pytest collection-config change, any `pytest_*` name added, changed or removed in a conftest.py) fails the
  job unless the PR has the `guard-override` label. Protected paths and golden/engine changes are reported,
  not failed; the merge policy escalates on them.
- `review/adversarial` (commit status): after an independent review of a specific head SHA, the reviewer runs

      python scripts/review_status.py --pr N --sha HEAD_SHA --state success|failure --blocking K --summary "..." [--url URL]

  It refuses (exit 2) if HEAD_SHA is no longer the PR's head, or `success` with K > 0. `--dry-run` prints the
  `gh api` request without posting. A new push leaves the status behind on the old SHA, so the review must
  be redone for the new head.

Making these required checks is a branch-protection setting on GitHub; this repo only provides them.

## Checking that `guard` is the real guard

A required check is matched by name. A PR can add its own workflow (say `.github/workflows/evil.yml`) with a
job named `guard`; under `pull_request` that workflow runs from the PR's copy and can report success on the
head SHA next to, or instead of, the real one. (Such a PR touches `.github/`, a protected path, but the
name collision itself is invisible in the checks list.) So before merging, Dori runs

    python scripts/check_guard_origin.py --sha HEAD_SHA [--repo joonk7809/asimov]

which reads `repos/{o}/{r}/commits/{sha}/check-runs`, and for each check run named `guard` its check suite's
workflow run (`repos/{o}/{r}/actions/runs?check_suite_id=ID`). It exits 0 only if at least one `guard` check
run exists on HEAD_SHA, every one was made by `github-actions` for that SHA from a workflow run with event
`pull_request_target` and path `.github/workflows/guard.yml` (for that event GitHub uses the base branch's
file), at least one succeeded, the latest completed one succeeded and none is still running. Any other
result (exit 1, or 2 when `gh` fails) blocks the merge and goes to the owner. It only issues GET requests.
Observed on a public repository (python/cpython, 2026-11-20): a `pull_request_target` run's check suite and
check runs sit on the PR head SHA and its workflow run reports `event` and `path` as used here; if GitHub
changes that, the tool finds no `guard` run and fails closed.

## Who runs whose code

- `guard` runs on `pull_request_target`: GitHub takes the workflow file from the base branch, so a PR can't
  edit guard.yml or scripts/pr_guard.py to pass itself. The job checks out the base (`persist-credentials:
  false`, token `contents: read`, no secrets), fetches `refs/pull/N/head` as git objects only, checks the
  fetched head is the event's head SHA, and runs the base's `scripts/pr_guard.py`, which only runs git and
  parses PR content (ast.parse, configparser, tomllib); it never executes, imports or installs anything from
  the PR. SHAs and the PR number reach the shell through `env`, never `${{ }}` inside `run`. If the base has
  no scripts/pr_guard.py the job fails; it never falls back to the PR's copy. Consequence: the PR that first
  adds guard.yml and pr_guard.py gets no guard run, and a change to the guard only takes effect for PRs opened
  against a base that already contains it.
- `verify` and `test` necessarily run the PR's own code (`verification/run.py`, the tests, `asiimov/`), and
  run their own workflow file under `pull_request`. They have `contents: read` and no secrets, but their
  verdict on a PR is only as trustworthy as the PR's own copy of those files. So a PR that touches `.github/`
  or `verification/` (both reported in `protected_paths_touched`) requires escalation to the owner; a green
  `verify` on such a PR is not evidence on its own.
- The base the guard trusts is the PR's base branch. A PR aimed at a branch other than `main` is judged by
  that branch's guard.

## Known limits of pr_guard.py

It compares syntax, not behaviour, and is a tripwire: the protected-path report and owner escalation are the
backstop for everything below. Engine code itself can neuter pytest at runtime (e.g. patching
`_pytest.python.Function.runtest` when tests import it); no syntactic check catches that, and only review
and the verify gate's own trusted base stand against it. Weakenings it does not fail on:

- an early `return` (or any unconditional exit, `os._exit`, `sys.exit` in a fixture) placed before asserts,
  and asserts after it;
- asserts wrapped in `try: ... except: pass` (or `except AssertionError`), or `with pytest.raises(...)` around
  them, or `contextlib.suppress`;
- asserts moved into a helper (counted per file, so a helper in another file moves them out of view; a
  helper in the same file keeps the count but nothing checks it is still called);
- dynamic skips: `pytest.skip` reached through `getattr`, a variable assigned anywhere but module level, a
  function returning a marker, a fixture in another file that the guard can't tie to the test,
  `pytest.mark.skip` built at runtime, markers added by a plugin, `-m`/`-p` given on the command line in CI
  rather than in config (CI files are under .github/, protected), or a skip condition changed
  (`skipif(False)` to `skipif(True)`) without adding a reference;
- a constant condition that is not a literal (`if DEBUG:` with `DEBUG = False`, `if 1 > 2:`), and trivial
  asserts that are not literals (`assert x == x`, `assert not False`);
- parametrize cases given by a name or call rather than a literal list (not judged), and class- or
  module-level `pytestmark` parametrize;
- collectability is judged by pytest's default rules; a changed python_functions / python_classes is caught
  as "pytest config", but tests collected under non-default rules are judged by the defaults;
- `__test__` set or tests rebound from outside the test file (a helper module, a conftest fixture), and
  rebinding through `globals()`, `setattr` on the module or `exec`;
- hooks and plugins outside a conftest.py: `pytest_generate_tests` or other hooks in a test module, a plugin
  module imported by a conftest under a name that is not `pytest_*` and not `*` (the import of the module
  itself is not flagged, though a conftest is a protected path), an installed plugin's entry point
  (requirements and CI files are protected), sitecustomize/.pth files;
- pytest.toml / pyproject.toml are only parsed structurally on Python >= 3.11 (tomllib); on older Pythons
  they fall back to a line check that misses multi-line value changes;
- renames are matched by count only: N tests removed and at least N collectable tests added in one file with
  the effective assert count not lower is reported as `renamed`, whatever the new tests test (an owner
  decision: of the last 60 merges, #42 #71 #78 split one test into several and #96 renamed one);
- moves are matched by qualified name: a test removed from one test file and added to another is `moved` when
  the linked files' effective assert total did not drop, whatever the moved test now contains;
- skips in newly added tests, or a module-level marker used only by them, are not weakening (they can't
  weaken a test that existed); they are listed in `new_test_skips` for the reviewer. So is `importorskip` in
  a conftest.py the PR adds, although at module level it skips every test under that directory when the
  import fails: it is reported as a protected path and escalated, not failed;
- a PR whose head isn't a descendant of a commit shared with the base (an unrelated history) fails at
  `git merge-base`.
