"""Check that the `guard` check on a commit came from the base branch's guard workflow, not a same-named job.

    python scripts/check_guard_origin.py --sha HEAD_SHA [--repo OWNER/NAME]

A PR can add its own workflow with a job named `guard` (under `pull_request` it runs the PR's copy), and a
check run is matched by name only. This reads, with `gh api` (GET only):

    repos/{repo}/commits/{sha}/check-runs          every check run on the commit
    repos/{repo}/actions/runs?check_suite_id=ID    the workflow run behind each `guard` check run's suite

and exits 0 only if at least one check run named `guard` exists on SHA, every one of them was made by GitHub
Actions for SHA and belongs to a workflow run whose event is `pull_request_target` and whose path is
`.github/workflows/guard.yml` (for that event GitHub runs the base branch's file), at least one succeeded and
the most recent one succeeded. Otherwise it prints why and exits 1; a gh failure exits 2. It never posts.
"""
import argparse
import json
import subprocess
import sys

CHECK_NAME = "guard"
EVENT = "pull_request_target"
PATH = ".github/workflows/guard.yml"
APP = "github-actions"


def gh_lines(path, jq):
    """One JSON value per line from a paginated `gh api` GET."""
    p = subprocess.run(["gh", "api", "--paginate", path, "--jq", jq], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"gh api {path}: {p.stderr.strip()}")
    return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]


def fetch(repo, sha, gh=gh_lines):
    """(check runs on sha, {check suite id: [workflow runs]}) for the runs named guard."""
    runs = gh(f"repos/{repo}/commits/{sha}/check-runs?per_page=100", ".check_runs[]")
    suites = {}
    for c in runs:
        if c.get("name") == CHECK_NAME:
            sid = (c.get("check_suite") or {}).get("id")
            if sid is not None and sid not in suites:
                suites[sid] = gh(f"repos/{repo}/actions/runs?check_suite_id={sid}&per_page=100", ".workflow_runs[]")
    return runs, suites


def verify(sha, check_runs, suites):
    """(ok, [problem]) for the check runs on sha and the workflow runs of their suites."""
    guards = [c for c in check_runs if c.get("name") == CHECK_NAME]
    if not guards:
        return False, [f"no check run named {CHECK_NAME!r} on {sha}"]
    problems = []
    for c in guards:
        cid = c.get("id")
        if c.get("head_sha") != sha:
            problems.append(f"check run {cid} is for {c.get('head_sha')}, not {sha}")
        if (c.get("app") or {}).get("slug") != APP:
            problems.append(f"check run {cid} was made by app {(c.get('app') or {}).get('slug')!r}, not {APP!r}")
        wruns = suites.get((c.get("check_suite") or {}).get("id")) or []
        if not wruns:
            problems.append(f"check run {cid} has no workflow run behind its check suite")
        for w in wruns:
            if w.get("event") != EVENT or w.get("path") != PATH or w.get("head_sha") != sha:
                problems.append(f"check run {cid} comes from workflow run {w.get('id')}: event {w.get('event')!r}, "
                                f"path {w.get('path')!r}, head {w.get('head_sha')!r} (want {EVENT!r}, {PATH!r}, {sha})")
    done = [c for c in guards if c.get("status") == "completed"]
    if not any(c.get("conclusion") == "success" for c in done):
        problems.append(f"no {CHECK_NAME!r} check run on {sha} succeeded")
    elif done:
        latest = max(done, key=lambda c: (c.get("completed_at") or "", c.get("id") or 0))
        if latest.get("conclusion") != "success":
            problems.append(f"the latest {CHECK_NAME!r} check run ({latest.get('id')}) concluded "
                            f"{latest.get('conclusion')!r}")
    if any(c.get("status") != "completed" for c in guards):
        problems.append(f"a {CHECK_NAME!r} check run on {sha} has not completed")
    return not problems, problems


def main(argv=None, gh=gh_lines):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sha", required=True)
    ap.add_argument("--repo", default="joonk7809/asimov")
    a = ap.parse_args(argv)
    try:
        runs, suites = fetch(a.repo, a.sha, gh)
    except (RuntimeError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    ok, problems = verify(a.sha, runs, suites)
    n = sum(c.get("name") == CHECK_NAME for c in runs)
    print(json.dumps({"sha": a.sha, "repo": a.repo, "guard_check_runs": n, "ok": ok, "problems": problems}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
