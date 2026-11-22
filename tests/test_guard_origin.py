"""scripts/check_guard_origin.py on stubbed `gh api` output, and the security properties of guard.yml."""
import importlib.util
import json
import os
import re

import pytest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("check_guard_origin", os.path.join(HERE, "..", "scripts", "check_guard_origin.py"))
cgo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cgo)

SHA = "a" * 40


def _check(id_, suite, conclusion="success", name="guard", sha=SHA, app="github-actions", done="2026-01-01T00:00:0"):
    return {"id": id_, "name": name, "head_sha": sha, "status": "completed", "conclusion": conclusion,
            "completed_at": f"{done}{id_ % 10}Z", "check_suite": {"id": suite}, "app": {"slug": app}}


def _wrun(id_, event="pull_request_target", path=".github/workflows/guard.yml", sha=SHA):
    return {"id": id_, "event": event, "path": path, "head_sha": sha}


def _gh(checks, suites, calls=None):
    def gh(path, jq):
        if calls is not None:
            calls.append(path)
        if "/check-runs" in path:
            return checks
        sid = int(re.search(r"check_suite_id=(\d+)", path).group(1))
        return suites.get(sid, [])
    return gh


def _main(capsys, checks, suites, calls=None):
    rc = cgo.main(["--sha", SHA, "--repo", "o/r"], gh=_gh(checks, suites, calls))
    return rc, json.loads(capsys.readouterr().out)


def test_guard_from_pull_request_target_passes(capsys):
    calls = []
    rc, out = _main(capsys, [_check(1, 10), _check(2, 20, name="verify")], {10: [_wrun(100)]}, calls)
    assert rc == 0 and out["ok"] is True and out["guard_check_runs"] == 1
    assert calls == [f"repos/o/r/commits/{SHA}/check-runs?per_page=100",
                     "repos/o/r/actions/runs?check_suite_id=10&per_page=100"]


@pytest.mark.parametrize("checks,suites,why", [
    ([], {}, "no check run named"),
    ([_check(2, 20, name="verify")], {20: [_wrun(200)]}, "no check run named"),
    # A PR's own workflow with a job named guard runs under pull_request, from the PR's copy.
    ([_check(1, 10)], {10: [_wrun(100, event="pull_request", path=".github/workflows/evil.yml")]}, "event 'pull_request'"),
    ([_check(1, 10)], {10: [_wrun(100, path=".github/workflows/evil.yml")]}, "evil.yml"),
    ([_check(1, 10), _check(2, 20)], {10: [_wrun(100)], 20: [_wrun(200, event="push")]}, "event 'push'"),
    ([_check(1, 10)], {}, "no workflow run"),
    ([_check(1, 10, app="some-app")], {10: [_wrun(100)]}, "some-app"),
    ([_check(1, 10, sha="b" * 40)], {10: [_wrun(100)]}, "not " + SHA),
    ([_check(1, 10)], {10: [_wrun(100, sha="b" * 40)]}, "head 'bbbb"),
    ([_check(1, 10, conclusion="failure")], {10: [_wrun(100)]}, "succeeded"),
    ([_check(1, 10), _check(2, 10, conclusion="failure")], {10: [_wrun(100)]}, "latest"),
])
def test_guard_from_elsewhere_or_unsuccessful_fails(capsys, checks, suites, why):
    rc, out = _main(capsys, checks, suites)
    assert rc == 1 and out["ok"] is False and any(why in p for p in out["problems"]), out["problems"]


def test_incomplete_guard_fails(capsys):
    running = dict(_check(2, 10), status="in_progress", conclusion=None)
    rc, out = _main(capsys, [_check(1, 10), running], {10: [_wrun(100)]})
    assert rc == 1 and any("not completed" in p for p in out["problems"])


def test_gh_failure_exits_2(capsys):
    def gh(path, jq):
        raise RuntimeError("HTTP 404")
    assert cgo.main(["--sha", SHA], gh=gh) == 2


# --- guard.yml: the properties docs/merge-gate.md relies on ---

WORKFLOW = os.path.join(HERE, "..", ".github", "workflows", "guard.yml")


def _run_blocks(text):
    """Every `run:` value: the inline text, or the indented block under `run: |`."""
    lines, blocks = text.splitlines(), []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)(?:-\s+)?run:\s*(.*)$", line)
        if not m:
            continue
        indent, rest = len(m.group(1)), m.group(2)
        if rest and rest[0] not in "|>":
            blocks.append(rest)
            continue
        body = []
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            body.append(nxt)
        blocks.append("\n".join(body))
    return blocks


def test_guard_workflow_security_properties():
    with open(WORKFLOW) as f:
        text = f.read()
    on = re.search(r"^on:\n((?:[ \t]+.*\n)+)", text, re.M)
    assert on and re.search(r"^\s+pull_request_target:", on.group(1), re.M)
    assert not re.search(r"^\s+pull_request:", on.group(1), re.M) and "workflow_run" not in on.group(1)
    perms = re.search(r"^permissions:\n((?:[ \t]+.*\n)+)", text, re.M)
    assert perms and [ln.strip() for ln in perms.group(1).splitlines() if ln.strip()] == ["contents: read"]
    assert "persist-credentials: false" in text
    assert "secrets." not in text
    blocks = _run_blocks(text)
    assert blocks and any("python -I scripts/pr_guard.py" in b for b in blocks)
    assert not any(re.search(r"\bpython\b(?! -I)", b) for b in blocks)
    for b in blocks:
        assert "${{" not in b, b
    assert "ref: ${{ github.event.pull_request.base.sha }}" in text
    # Every checkout is the base; the PR head is only ever fetched as git objects.
    refs = re.findall(r"^\s*ref:\s*(.+)$", text, re.M)
    assert refs and all(ref.strip() == "${{ github.event.pull_request.base.sha }}" for ref in refs), refs
    assert "pull_request.head.ref" not in text and "head.repo" not in text


def test_run_block_parser_sees_expressions():
    # Negative control for the check above.
    bad = "jobs:\n  g:\n    steps:\n      - run: |\n          echo ${{ github.event.pull_request.title }}\n      - run: echo ok\n"
    blocks = _run_blocks(bad)
    assert len(blocks) == 2 and "${{" in blocks[0] and blocks[1] == "echo ok"
