"""scripts/review_status.py: head and blocking guards, and the exact status request, with gh stubbed out."""
import importlib.util
import os

import pytest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("review_status", os.path.join(HERE, "..", "scripts", "review_status.py"))
review_status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review_status)

HEAD = "a" * 40


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_gh(*args):
        seen.append(args)
        if args[:2] == ("pr", "view"):
            return '{"headRefOid": "%s"}' % HEAD
        return "{}"
    monkeypatch.setattr(review_status, "gh", fake_gh)
    return seen


def _argv(**kw):
    a = {"pr": "7", "sha": HEAD, "state": "success", "blocking": "0", "summary": "no findings"}
    a.update(kw)
    out = []
    for k, v in a.items():
        out += [f"--{k}", v]
    return out


def test_success_posts_status_on_head(calls):
    assert review_status.main(_argv(url="https://x/y")) == 0
    assert calls[0] == ("pr", "view", "7", "--json", "headRefOid")
    post = calls[1]
    assert post[:4] == ("api", "--method", "POST", f"repos/{{owner}}/{{repo}}/statuses/{HEAD}")
    assert "context=review/adversarial" in post and "state=success" in post
    assert "target_url=https://x/y" in post and "description=0 blocking: no findings" in post


def test_stale_sha_refused(calls, capsys):
    assert review_status.main(_argv(sha="b" * 40)) == 2
    assert len(calls) == 1 and "not PR #7's head" in capsys.readouterr().err


def test_success_with_blocking_refused(calls):
    assert review_status.main(_argv(blocking="1")) == 2
    assert calls == []


def test_failure_with_blocking_posts(calls):
    assert review_status.main(_argv(state="failure", blocking="3", summary="3 issues")) == 0
    assert "state=failure" in calls[1]


def test_dry_run_prints_and_does_not_post(calls, capsys):
    assert review_status.main(_argv() + ["--dry-run"]) == 0
    assert len(calls) == 1  # only the head lookup
    out = capsys.readouterr().out
    assert out.startswith("gh api --method POST") and "context=review/adversarial" in out


def test_bad_state_rejected(calls):
    with pytest.raises(SystemExit):
        review_status.main(_argv(state="pending"))
    assert calls == []
