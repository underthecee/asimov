"""Post the adversarial review's verdict as a commit status (context `review/adversarial`) on a PR's head.

    python scripts/review_status.py --pr N --sha HEAD_SHA --state success|failure --blocking K \\
        --summary "..." [--url URL] [--dry-run]

Refuses (exit 2) when --sha is not the PR's current head (`gh pr view N --json headRefOid`), so a verdict
can't land on a commit the reviewer didn't read, and refuses `success` while blocking findings remain. Posts
with `gh api repos/{owner}/{repo}/statuses/{sha}`; --dry-run prints that request instead. See docs/merge-gate.md.
"""
import argparse
import json
import shlex
import subprocess
import sys

CONTEXT = "review/adversarial"


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def pr_head(pr):
    return json.loads(gh("pr", "view", str(pr), "--json", "headRefOid"))["headRefOid"]


def request(a):
    desc = f"{a.blocking} blocking: {a.summary}"
    fields = {"state": a.state, "context": CONTEXT, "description": desc[:140]}  # GitHub caps descriptions at 140
    if a.url:
        fields["target_url"] = a.url
    cmd = ["api", "--method", "POST", f"repos/{{owner}}/{{repo}}/statuses/{a.sha}"]
    for k, v in fields.items():
        cmd += ["-f", f"{k}={v}"]
    return cmd


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--state", choices=["success", "failure"], required=True)
    ap.add_argument("--blocking", type=int, required=True)
    ap.add_argument("--summary", required=True)
    ap.add_argument("--url")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.blocking < 0:
        print("refusing: --blocking must be >= 0", file=sys.stderr)
        return 2
    if a.state == "success" and a.blocking > 0:
        print(f"refusing: state success with {a.blocking} blocking finding(s)", file=sys.stderr)
        return 2
    head = pr_head(a.pr)
    if head != a.sha:
        print(f"refusing: {a.sha} is not PR #{a.pr}'s head ({head})", file=sys.stderr)
        return 2
    cmd = request(a)
    if a.dry_run:
        print(shlex.join(["gh", *cmd]))
        return 0
    gh(*cmd)
    print(f"posted {CONTEXT}={a.state} on {a.sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
