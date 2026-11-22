"""scripts/pr_guard.py on throwaway git repos: weakening fails, renames and clean changes pass."""
import importlib.util
import json
import os
import subprocess

import pytest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("pr_guard", os.path.join(HERE, "..", "scripts", "pr_guard.py"))
pr_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr_guard)

TEST_A = '''import pytest


def test_one():
    assert 1 == 1
    assert 2 == 2


def test_two():
    assert 3 == 3
'''
SIM = 'ENGINE_VERSION = "35"\n\n\ndef step(x):\n    return x + 1\n'
WORLD = 'GOLDEN = {"34": "aa", "35": "bb"}\n\n\ndef test_golden():\n    assert GOLDEN\n'


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _write(repo, files):
    for path, text in files.items():
        p = repo / path
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)


def _commit(repo, files, msg="c"):
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    base = _commit(r, {"tests/test_a.py": TEST_A, "asiimov/sim.py": SIM, "tests/test_world.py": WORLD,
                       "asiimov/other.py": "X = 1\n"})
    return r, base


def _run(r, base, head, tmp_path, label=None, capsys=None):
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps({"pull_request": {"labels": [{"name": label}] if label else []}}))
    rc = pr_guard.main(["--base", base, "--head", head, "--repo", str(r), "--event", str(ev), "--summary", ""])
    out = json.loads(capsys.readouterr().out)
    return rc, out


def _kinds(out):
    return [w["kind"] for w in out["weakening"]]


def test_clean_change_passes(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"asiimov/other.py": "X = 2\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["result"] == "pass"
    assert out["weakening"] == [] and out["protected_paths_touched"] == []
    assert out["golden_or_engine_changed"] is False


def test_identical_base_head_flags_nothing(repo, tmp_path, capsys):
    r, base = repo
    rc, out = _run(r, base, base, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == [] and out["protected_paths_touched"] == []


def test_planted_weakening_is_seen(repo, tmp_path, capsys):
    # Negative control: the same harness that passes above must see a real diff.
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("    assert 2 == 2\n", "")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["asserts removed"]
    assert out["protected_paths_touched"] == ["tests/test_a.py"]


def test_deleted_test_file_fails(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": None})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["test file deleted"]


def test_removed_test_function_fails(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("def test_two():\n    assert 3 == 3\n",
                                                         "def helper():\n    assert 3 == 3\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["test function removed"]
    assert out["weakening"][0]["names"] == ["test_two"]


def test_removed_asserts_fail(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("    assert 1 == 1\n", "    pass\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and out["weakening"][0]["before"] == 3 and out["weakening"][0]["after"] == 2


@pytest.mark.parametrize("deco", ["@pytest.mark.skip(reason='x')", "@pytest.mark.skipif(True, reason='x')",
                                  "@pytest.mark.xfail", "@unittest.skip('x')"])
def test_added_skip_or_xfail_fails(repo, tmp_path, capsys, deco):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": "import unittest\n" + TEST_A.replace("def test_two", deco + "\ndef test_two")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["skip/xfail added"]


@pytest.mark.parametrize("imp,use", [("import pytest as _pytest", "@_pytest.mark.skipif(True, reason='x')"),
                                     ("from pytest import mark", "@mark.xfail"),
                                     ("from unittest import skip", "@skip('x')")])
def test_aliased_skip_fails(repo, tmp_path, capsys, imp, use):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": imp + "\n" + TEST_A.replace("def test_two", use + "\ndef test_two")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["skip/xfail added"]


def test_skip_call_in_body_fails(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("    assert 3 == 3\n", "    pytest.skip('later')\n    assert 3 == 3\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["skip/xfail added"]


def test_pytest_config_deselect_fails(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"pytest.ini": "[pytest]\naddopts = --deselect tests/test_a.py::test_two\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["pytest config"]


def test_rename_passes(repo, tmp_path, capsys):
    r, base = repo
    _git(r, "mv", "tests/test_a.py", "tests/test_moved.py")
    head = _commit(r, {})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []
    assert set(out["protected_paths_touched"]) == {"tests/test_a.py", "tests/test_moved.py"}


def test_rename_out_of_collection_fails(repo, tmp_path, capsys):
    r, base = repo
    _git(r, "mv", "tests/test_a.py", "tests/a_helpers.py")
    head = _commit(r, {})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["test file renamed out of collection"]


def test_override_label_downgrades(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": None})
    rc, out = _run(r, base, head, tmp_path, label="guard-override", capsys=capsys)
    assert rc == 0 and out["result"] == "overridden" and _kinds(out) == ["test file deleted"]
    rc, out = _run(r, base, head, tmp_path, label="something-else", capsys=capsys)
    assert rc == 1 and out["result"] == "fail"


def test_golden_and_engine_change_detected_not_failed(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"asiimov/sim.py": SIM.replace('"35"', '"36"'),
                       "tests/test_world.py": WORLD.replace('"bb"}', '"bb", "36": "cc"}')})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == [] and out["golden_or_engine_changed"] is True
    detail = {d["name"]: d for d in out["golden_or_engine_detail"]}
    assert detail["ENGINE_VERSION"]["old"] == "35" and detail["ENGINE_VERSION"]["new"] == "36"
    assert detail["GOLDEN"]["changed_keys"] == {"36": [None, "cc"]}
    assert out["protected_paths_touched"] == ["tests/test_world.py"]


def test_diff_is_from_merge_base(repo, tmp_path, capsys):
    # A test deleted on the base branch after the PR forked is not the PR's doing.
    r, base = repo
    _git(r, "checkout", "-q", "-b", "pr")
    head = _commit(r, {"asiimov/other.py": "X = 3\n"})
    _git(r, "checkout", "-q", "-")
    moved_base = _commit(r, {"tests/test_a.py": None})
    rc, out = _run(r, moved_base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == [] and out["diff_base"] == base


# --- Binary and non-UTF-8 content (PR #76 added client/godot/art/*.png and crashed the first guard) ---

def _commit_bytes(repo, files, msg="c"):
    for path, data in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\x80"


def test_binary_files_are_named_not_read(repo, tmp_path, capsys):
    r, base = repo
    mid = _commit_bytes(r, {"client/art/a.png": PNG, "tests/data/b.bin": b"\xff\x00\x80"})
    head = _commit_bytes(r, {"client/art/a.png": PNG + b"\x99", "client/art/c.png": PNG})
    for b in (base, mid):
        rc, out = _run(r, b, head, tmp_path, capsys=capsys)
        assert rc == 0 and out["result"] == "pass" and out["weakening"] == []
    rc, out = _run(r, base, mid, tmp_path, capsys=capsys)
    assert rc == 0 and out["protected_paths_touched"] == ["tests/data/b.bin"]


def test_non_utf8_python_is_parsed_from_bytes(repo, tmp_path, capsys):
    # A latin-1 test file with a coding line parses; an undecodable one is "unparseable", never a crash.
    r, base = repo
    latin = b"# -*- coding: latin-1 -*-\n" + TEST_A.encode() + b"\ndef test_three():\n    assert '\xe9' == '\xe9'\n"
    head = _commit_bytes(r, {"tests/test_a.py": latin})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []
    bad = _commit_bytes(r, {"tests/test_a.py": TEST_A.encode() + b"\nX = '\xff\xfe'\n"})
    rc, out = _run(r, base, bad, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["unparseable after change"]


def test_pr_content_is_never_executed(repo, tmp_path, capsys):
    r, base = repo
    marker = tmp_path / "ran"
    evil = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n"
    head = _commit(r, {"conftest.py": evil, "tests/test_evil.py": evil + "\ndef test_e():\n    assert 1 == 1\n",
                       "scripts/pr_guard.py": evil, "pyproject.toml": "[project]\nname = 'x'\n"})
    _run(r, base, head, tmp_path, capsys=capsys)
    assert not marker.exists()


def test_unreadable_event_means_no_override(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": None})
    for content in ("", "not json", "[]"):
        ev = tmp_path / "bad.json"
        ev.write_text(content)
        rc = pr_guard.main(["--base", base, "--head", head, "--repo", str(r), "--event", str(ev), "--summary", ""])
        assert rc == 1 and json.loads(capsys.readouterr().out)["override"] is False


# --- Renames: N test functions out, N in, effective asserts not down (PRs #96 #78 #71 #42 #33) ---

def test_renamed_test_functions_pass(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("def test_two", "def test_two_renamed")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []
    assert out["renamed"] == [{"path": "tests/test_a.py", "from": ["test_two"], "to": ["test_two_renamed"]}]


def test_rename_that_drops_asserts_fails(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("def test_two():\n    assert 3 == 3\n",
                                                         "def test_two_renamed():\n    pass\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["test function removed", "asserts removed"] and out["renamed"] == []


def test_rename_with_fewer_new_names_fails(repo, tmp_path, capsys):
    # Two out, one in: not a rename, even with the asserts kept.
    r, base = repo
    merged = TEST_A.replace("def test_one():", "def test_both():").replace("\n\n\ndef test_two():\n", "\n")
    head = _commit(r, {"tests/test_a.py": merged})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["test function removed"]


# --- Asserts that can't fail ---

@pytest.mark.parametrize("new", ["assert True", "assert 1", "assert 'yes'", "assert 1 == 1 or True",
                                 "assert False or 1"])
def test_trivial_assert_replacing_a_real_one_fails(repo, tmp_path, capsys, new):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("assert 2 == 2", new)})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["asserts removed", "trivial assert added"]


@pytest.mark.parametrize("new", ["assert 2 == 2 or 3 == 3", "assert x or y", "assert not x"])
def test_non_literal_asserts_pass(repo, tmp_path, capsys, new):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("assert 2 == 2", new)})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []


@pytest.mark.parametrize("wrap", ["if False:", "if 0:", "if None:", "if '':"])
def test_assert_under_dead_branch_fails(repo, tmp_path, capsys, wrap):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("    assert 2 == 2\n", f"    {wrap}\n        assert 2 == 2\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["asserts removed", "dead assert added"]


def test_assert_under_else_of_if_true_fails(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace(
        "    assert 2 == 2\n", "    if True:\n        pass\n    else:\n        assert 2 == 2\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["asserts removed", "dead assert added"]


@pytest.mark.parametrize("wrap", ["if True:", "if x:", "if 1:"])
def test_assert_under_live_branch_passes(repo, tmp_path, capsys, wrap):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("    assert 2 == 2\n", f"    {wrap}\n        assert 2 == 2\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []


# --- Raised skips, and skips that can't reach an existing test ---

@pytest.mark.parametrize("imp,stmt", [("import unittest", "raise unittest.SkipTest('x')"),
                                      ("import pytest", "raise pytest.skip.Exception('x')"),
                                      ("from unittest import SkipTest", "raise SkipTest('x')")])
def test_raised_skip_fails(repo, tmp_path, capsys, imp, stmt):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": imp + "\n" + TEST_A.replace("    assert 3 == 3\n", f"    {stmt}\n    assert 3 == 3\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["skip/xfail added"]


def test_raised_other_exception_passes(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("    assert 3 == 3\n", "    if 3 != 3:\n        raise ValueError('x')\n    assert 3 == 3\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []


def test_skip_in_a_new_test_is_reported_not_failed(repo, tmp_path, capsys):
    r, base = repo
    new_test = "\n\n@pytest.mark.skipif(True, reason='x')\ndef test_new():\n    pytest.skip('no data')\n    assert 4 == 4\n"
    head = _commit(r, {"tests/test_a.py": TEST_A + new_test,
                       "tests/test_fresh.py": "import pytest\n\n\ndef test_f():\n    pytest.skip('x')\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []
    assert sorted(out["new_test_skips"], key=lambda d: d["path"]) == [{"path": "tests/test_a.py", "count": 2},
                                                                      {"path": "tests/test_fresh.py", "count": 1}]


def test_skip_in_a_helper_or_module_level_fails(repo, tmp_path, capsys):
    # A helper or module-level skip reaches existing tests, even in the same PR that adds a new test.
    r, base = repo
    for extra in ("\n\ndef _setup():\n    pytest.skip('x')\n", "\n\npytestmark = pytest.mark.skip\n"):
        head = _commit(r, {"tests/test_a.py": TEST_A + extra})
        rc, out = _run(r, base, head, tmp_path, capsys=capsys)
        assert rc == 1 and _kinds(out) == ["skip/xfail added"]


# --- Shadowed test names ---

def test_duplicate_test_name_fails(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A + "\n\ndef test_one():\n    assert 5 == 5\n    assert 6 == 6\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["test shadowed by duplicate name"] and out["weakening"][0]["names"] == ["test_one"]


def test_same_name_in_two_classes_passes(repo, tmp_path, capsys):
    r, base = repo
    classes = "\n\nclass TestA:\n    def test_x(self):\n        assert 1 == 1\n\n\nclass TestB:\n    def test_x(self):\n        assert 1 == 1\n"
    head = _commit(r, {"tests/test_a.py": TEST_A + classes})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []


# --- Parametrize case counts ---

PARAM = '''import pytest


@pytest.mark.parametrize("x", [1, 2, 3])
@pytest.mark.parametrize("y", ["a", "b"])
def test_p(x, y):
    assert x
'''


def test_parametrize_cases_reduced_fails(repo, tmp_path, capsys):
    r, base = repo
    base = _commit(r, {"tests/test_p.py": PARAM})
    for new, after in ((PARAM.replace("[1, 2, 3]", "[1, 2]"), 4), (PARAM.replace('@pytest.mark.parametrize("y", ["a", "b"])\n', ""), 3)):
        head = _commit(r, {"tests/test_p.py": new})
        rc, out = _run(r, base, head, tmp_path, capsys=capsys)
        assert rc == 1 and _kinds(out) == ["parametrize cases reduced"]
        assert out["weakening"][0]["before"] == 6 and out["weakening"][0]["after"] == after


def test_parametrize_cases_kept_or_grown_passes(repo, tmp_path, capsys):
    r, base = repo
    base = _commit(r, {"tests/test_p.py": PARAM})
    for new in (PARAM.replace("[1, 2, 3]", "[1, 2, 4]"), PARAM.replace("[1, 2, 3]", "[1, 2, 3, 4]"),
                PARAM.replace("[1, 2, 3]", "CASES")):  # not a literal: not judged (docs/merge-gate.md)
        head = _commit(r, {"tests/test_p.py": new})
        rc, out = _run(r, base, head, tmp_path, capsys=capsys)
        assert rc == 0 and out["weakening"] == []


# --- Collection config ---

@pytest.mark.parametrize("path,before,after", [
    ("pytest.ini", "[pytest]\ntestpaths = tests\n", "[pytest]\ntestpaths = tests/fast\n"),
    ("pytest.ini", "[pytest]\ntestpaths =\n    tests\n    client\n", "[pytest]\ntestpaths =\n    tests\n"),
    ("tox.ini", "[pytest]\n", "[pytest]\npython_functions = check_*\n"),
    ("setup.cfg", "[tool:pytest]\nnorecursedirs = .git\n", "[tool:pytest]\nnorecursedirs = .git tests\n"),
    ("pyproject.toml", "[tool.pytest.ini_options]\npython_files = ['test_*.py']\n",
     "[tool.pytest.ini_options]\npython_files = ['test_a*.py']\n"),
    ("pyproject.toml", "[tool.pytest]\n", "[tool.pytest]\ntestpaths = ['x']\n"),
    ("pytest.ini", "[pytest]\n", "[pytest]\npython_classes = Nothing\n"),
])
def test_collection_config_change_fails(repo, tmp_path, capsys, path, before, after):
    r, base = repo
    base = _commit(r, {path: before})
    head = _commit(r, {path: after})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["pytest config"]


@pytest.mark.parametrize("path,before,after", [
    ("pytest.ini", "[pytest]\ntestpaths = tests\n", "[pytest]\ntestpaths = tests\nmarkers =\n    slow: slow\n"),
    ("pyproject.toml", "[project]\nname = 'a'\n", "[project]\nname = 'b'\n"),
    ("setup.cfg", "[metadata]\nname = a\n[tool:pytest]\ntestpaths = tests\n",
     "[metadata]\nname = b\n[tool:pytest]\ntestpaths = tests\n"),
])
def test_other_config_change_passes(repo, tmp_path, capsys, path, before, after):
    r, base = repo
    base = _commit(r, {path: before})
    head = _commit(r, {path: after})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []


HOOK = "def pytest_collection_modifyitems(config, items):\n    items[:] = [i for i in items if 'slow' not in i.name]\n"


def test_conftest_collection_hook_added_or_changed_fails(repo, tmp_path, capsys):
    r, base = repo
    added = _commit(r, {"tests/conftest.py": HOOK})
    rc, out = _run(r, base, added, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["collection hook added or changed"]
    changed = _commit(r, {"tests/conftest.py": HOOK.replace("'slow'", "'s'")})
    rc, out = _run(r, added, changed, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["collection hook added or changed"]


def test_conftest_other_change_passes(repo, tmp_path, capsys):
    r, base = repo
    base = _commit(r, {"tests/conftest.py": HOOK})
    head = _commit(r, {"tests/conftest.py": HOOK + "\n\nimport pytest\n\n\n@pytest.fixture\ndef world():\n    return 1\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []


# --- Round 2: anything that can change what pytest collects is protected; pytest_* names in a conftest ---

KEEP = "def _keep(config, items):\n    items[:] = items[:1]\n"


@pytest.mark.parametrize("conftest", [
    KEEP + "\n\npytest_collection_modifyitems = _keep\n",                                   # bound by assignment
    "if True:\n    " + HOOK.replace("\n    ", "\n        "),                                 # nested under if
    "def pytest_ignore_collect(collection_path, config):\n    return 'test_a' in str(collection_path)\n",
    "try:\n    from helpers import pytest_collection_modifyitems\nexcept ImportError:\n    pass\n",
    "from helpers import *\n",
    "pytest_plugins = ['helpers']\n",
    KEEP + "\n\nglobals()['pytest_collection_modifyitems'] = _keep\n",
    "class Plugin:\n    def pytest_deselected(self, items):\n        pass\n",
])
def test_root_conftest_hook_in_any_form_fails(repo, tmp_path, capsys, conftest):
    r, base = repo
    head = _commit(r, {"conftest.py": conftest})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and set(_kinds(out)) == {"collection hook added or changed"}
    assert "conftest.py" in out["protected_paths_touched"]


def test_conftest_guard_condition_flipped_fails(repo, tmp_path, capsys):
    r, base = repo
    base = _commit(r, {"conftest.py": "if False:\n    " + HOOK.replace("\n    ", "\n        ")})
    head = _commit(r, {"conftest.py": "if True:\n    " + HOOK.replace("\n    ", "\n        ")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["collection hook added or changed"] and out["weakening"][0]["change"] == "changed"


def test_root_conftest_fixture_is_protected_not_failed(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"conftest.py": "import pytest\n\n\n@pytest.fixture\ndef world():\n    return 1\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == [] and out["protected_paths_touched"] == ["conftest.py"]


@pytest.mark.parametrize("path", ["conftest.py", "client/conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg",
                                  "tox.ini", "pytest.toml", ".pytest.toml", "sub/pyproject.toml",
                                  "tests/__init__.py", "client/tests/__init__.py", "requirements.txt",
                                  "requirements-dev.txt", "constraints.txt", "ci/requirements/test.txt",
                                  "scripts/check_guard_origin.py", ".pytest.ini", "pytest.py", "sitecustomize.py",
                                  "x.pth", "_pytest/__init__.py", "hypothesis/__init__.py", "newpkg/mod.py"])
def test_collection_and_install_files_are_protected(path):
    assert pr_guard.protected(path)


@pytest.mark.parametrize("path", ["asiimov/sim.py", "client/__init__.py", "docs/requirements.md", "README.md"])
def test_ordinary_files_are_not_protected(path):
    assert not pr_guard.protected(path)


@pytest.mark.parametrize("extra", ["\n__test__ = False\n", "\nif True:\n    __test__ = 0\n",
                                   "\ntest_two.__test__ = False\n", "\nsetattr(test_two, '__test__', False)\n"])
def test_dunder_test_added_fails(repo, tmp_path, capsys, extra):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A + extra})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["collection disabled by __test__"]


CLASS_T = TEST_A + "\n\nclass TestK:\n    def test_m(self):\n        assert 4 == 4\n"


def test_dunder_test_in_class_fails_and_existing_one_passes(repo, tmp_path, capsys):
    r, base = repo
    base = _commit(r, {"tests/test_a.py": CLASS_T + "\n\nclass Helper:\n    __test__ = False\n"})
    head = _commit(r, {"tests/test_a.py": CLASS_T.replace("class TestK:\n", "class TestK:\n    __test__ = False\n")
                       + "\n\nclass Helper:\n    __test__ = False\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["collection disabled by __test__"]
    clean = _commit(r, {"tests/test_a.py": CLASS_T.replace("4 == 4", "4 >= 4") + "\n\nclass Helper:\n    __test__ = False\n"})
    rc, out = _run(r, base, clean, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []


@pytest.mark.parametrize("extra", ["\ndel test_two\n", "\ntest_two = None\n", "\nif True:\n    del test_two\n",
                                   "\nfrom os import sep as test_two\n", "\nclass TestK:\n    pass\n"])
def test_test_removed_by_rebinding_fails(repo, tmp_path, capsys, extra):
    r, base = repo
    # The last form is a later class of the same name replacing the one that holds TestK.test_m.
    base = _commit(r, {"tests/test_a.py": CLASS_T})
    head = _commit(r, {"tests/test_a.py": CLASS_T + extra})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["test removed by rebinding", "test function removed"]


def test_rebinding_other_names_passes(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": "test_two = 1\n" + TEST_A + "\nhelper = None\ndel helper\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []


def test_rename_to_a_rebound_name_is_not_a_rename(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("def test_two", "def test_three") + "\ntest_three = None\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["test removed by rebinding", "test function removed"] and out["renamed"] == []


@pytest.mark.parametrize("new", [
    "class Helper:\n    def test_two_renamed(self):\n        assert 3 == 3\n",              # class not Test*
    "class TestH:\n    def __init__(self):\n        pass\n\n    def test_two_renamed(self):\n        assert 3 == 3\n",
    "def check_two():\n    assert 3 == 3\n",                                              # not test*
])
def test_rename_to_uncollectable_names_fails(repo, tmp_path, capsys, new):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("def test_two():\n    assert 3 == 3\n", new)})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["test function removed"] and out["renamed"] == []


def test_rename_into_a_test_class_passes(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace(
        "def test_two():\n    assert 3 == 3\n", "class TestH:\n    def test_two(self):\n        assert 3 == 3\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["renamed"] == [{"path": "tests/test_a.py", "from": ["test_two"], "to": ["TestH.test_two"]}]


@pytest.mark.parametrize("ctor", ["__init__", "__new__"])
def test_test_class_gaining_a_constructor_fails(repo, tmp_path, capsys, ctor):
    r, base = repo
    base = _commit(r, {"tests/test_a.py": CLASS_T})
    head = _commit(r, {"tests/test_a.py": CLASS_T.replace("class TestK:\n", f"class TestK:\n    def {ctor}(self):\n        pass\n\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["test class gains a constructor", "test function removed"]


def test_unittest_testcase_with_init_stays_collectable(repo, tmp_path, capsys):
    r, base = repo
    case = "import unittest\n\n\nclass Cases(unittest.TestCase):\n    def test_m(self):\n        self.assertTrue(1)\n"
    base = _commit(r, {"tests/test_u.py": case})
    head = _commit(r, {"tests/test_u.py": case.replace("    def test_m", "    def __init__(self, *a):\n        super().__init__(*a)\n\n    def test_m")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []


@pytest.mark.parametrize("imp,stmt", [("from _pytest.outcomes import skip", "skip('x')"),
                                      ("from _pytest.outcomes import Skipped", "raise Skipped('x')"),
                                      ("import _pytest.outcomes", "_pytest.outcomes.xfail('x')"),
                                      ("from _pytest.outcomes import XFailed", "raise XFailed('x')"),
                                      ("from _pytest.outcomes import importorskip", "importorskip('nope')"),
                                      ("import pytest", "pytest.exit('done', returncode=0)"),
                                      ("from _pytest.outcomes import Exit", "raise Exit('x', 0)")])
def test_pytest_internal_skips_fail(repo, tmp_path, capsys, imp, stmt):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": imp + "\n" + TEST_A.replace("    assert 3 == 3\n", f"    {stmt}\n    assert 3 == 3\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["skip/xfail added"]


def test_pytest_internal_fail_passes(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": "from _pytest.outcomes import fail\n" + TEST_A.replace(
        "    assert 3 == 3\n", "    if 3 != 3:\n        fail('x')\n    assert 3 == 3\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []


def test_one_removed_two_added_is_a_rename(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A.replace(
        "def test_two():\n    assert 3 == 3\n", "def test_two_a():\n    assert 3 == 3\n\n\ndef test_two_b():\n    assert 4 == 4\n")})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []
    assert out["renamed"] == [{"path": "tests/test_a.py", "from": ["test_two"], "to": ["test_two_a", "test_two_b"]}]
    dropped = _commit(r, {"tests/test_a.py": TEST_A.replace("    assert 2 == 2\n", "").replace(
        "def test_two():\n    assert 3 == 3\n", "def test_two_a():\n    assert 3 == 3\n\n\ndef test_two_b():\n    pass\n")})
    rc, out = _run(r, base, dropped, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["test function removed", "asserts removed"]


NEEDS = "\n\nneeds_env = pytest.mark.skipif(True, reason='no env')\n"


def test_module_marker_used_only_by_new_tests_is_reported_not_failed(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/test_a.py": TEST_A + NEEDS + "\n\n@needs_env\ndef test_new():\n    assert 4 == 4\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == [] and out["new_test_skips"] == [{"path": "tests/test_a.py", "count": 1}]
    reused = _commit(r, {"tests/test_a.py": TEST_A.replace("def test_two", "@needs_env\ndef test_two") + NEEDS})
    rc, out = _run(r, base, reused, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["skip/xfail added"]
    unused = _commit(r, {"tests/test_a.py": TEST_A + NEEDS})
    rc, out = _run(r, base, unused, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["skip/xfail added"]


def test_existing_marker_applied_to_an_existing_test_fails(repo, tmp_path, capsys):
    r, base = repo
    base = _commit(r, {"tests/test_a.py": TEST_A + NEEDS + "\n\n@needs_env\ndef test_new():\n    assert 4 == 4\n"})
    head = _commit(r, {"tests/test_a.py": TEST_A.replace("def test_two", "@needs_env\ndef test_two") + NEEDS
                       + "\n\n@needs_env\ndef test_new():\n    assert 4 == 4\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["skip/xfail added"]


def test_new_conftest_importorskip_is_reported_not_failed(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"tests/godot/conftest.py": "import pytest\n\nz3 = pytest.importorskip('z3')\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == [] and out["protected_paths_touched"] == ["tests/godot/conftest.py"]
    assert out["new_test_skips"] == [{"path": "tests/godot/conftest.py", "count": 1, "conftest": True}]
    skip = _commit(r, {"tests/godot/conftest.py": "import pytest\n\npytest.skip('x', allow_module_level=True)\n"})
    rc, out = _run(r, base, skip, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["skip/xfail added"]


def test_existing_conftest_gaining_importorskip_fails(repo, tmp_path, capsys):
    r, base = repo
    base = _commit(r, {"tests/conftest.py": "import pytest\n"})
    head = _commit(r, {"tests/conftest.py": "import pytest\n\nz3 = pytest.importorskip('z3')\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["skip/xfail added"]


TEST_B = "def test_b():\n    assert 7 == 7\n"
TWO = "\n\ndef test_two():\n    assert 3 == 3\n"


@pytest.mark.parametrize("dest", ["tests/test_b.py", "tests/test_new.py"])
def test_test_moved_between_files_passes(repo, tmp_path, capsys, dest):
    r, base = repo
    base = _commit(r, {"tests/test_b.py": TEST_B})
    without = TEST_A.replace(TWO, "\n")
    head = _commit(r, {"tests/test_a.py": without, dest: (TEST_B if dest == "tests/test_b.py" else "") + TWO})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == []
    assert out["moved"] == [{"from": "tests/test_a.py", "to": dest, "names": ["test_two"]}]


def test_test_moved_with_asserts_dropped_fails(repo, tmp_path, capsys):
    r, base = repo
    base = _commit(r, {"tests/test_b.py": TEST_B})
    head = _commit(r, {"tests/test_a.py": TEST_A.replace(TWO, "\n"),
                       "tests/test_b.py": TEST_B + "\n\ndef test_two():\n    pass\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["test function removed", "asserts removed"] and out["moved"] == []


def test_moved_test_keeps_its_skip_but_gains_none(repo, tmp_path, capsys):
    r, base = repo
    skipped = TEST_A.replace("def test_two", "@pytest.mark.slow\ndef test_two")
    base = _commit(r, {"tests/test_a.py": skipped.replace("mark.slow", "mark.xfail"), "tests/test_b.py": TEST_B})
    moved_two = "import pytest\n" + TEST_B + "\n\n@pytest.mark.xfail\ndef test_two():\n    assert 3 == 3\n"
    head = _commit(r, {"tests/test_a.py": TEST_A.replace(TWO, "\n"), "tests/test_b.py": moved_two})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 0 and out["weakening"] == [] and out["moved"] != []
    # The same move from a base where test_two had no xfail: the skip arrives with the move.
    _git(r, "checkout", "-q", "-b", "plain", base)
    base2 = _commit(r, {"tests/test_a.py": TEST_A})
    head2 = _commit(r, {"tests/test_a.py": TEST_A.replace(TWO, "\n"), "tests/test_b.py": moved_two})
    rc, out = _run(r, base2, head2, tmp_path, capsys=capsys)
    assert rc == 1 and _kinds(out) == ["skip/xfail added"] and out["moved"] != []


def test_hidden_pytest_ini_deselect_fails(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {".pytest.ini": '[pytest]\naddopts = -k "not test_one"\n'})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert rc == 1 and out["protected_paths_touched"] == [".pytest.ini"]


def test_root_module_shadowing_pytest_is_protected(repo, tmp_path, capsys):
    r, base = repo
    head = _commit(r, {"pytest.py": "import sys\nprint('3 passed')\nsys.exit(0)\n"})
    rc, out = _run(r, base, head, tmp_path, capsys=capsys)
    assert out["protected_paths_touched"] == ["pytest.py"]
