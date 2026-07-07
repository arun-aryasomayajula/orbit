#!/usr/bin/env python3
"""review_packet.py — assemble a per-ship review packet the human can read in
2 minutes instead of re-deriving everything from git.

Called by run.sh right after it pushes a task's branch. Combines:
  - the task CONTRACT (title + WHY + acceptance criteria, from queue.json)
  - the commit (sha, message, diffstat — from the worktree git)
  - the agent's own review notes (verifier verdict + checker summary, written
    by the cycle to reviews/task-<id>-notes.md)
  - the backup patch path + a prefilled Bitbucket PR-create URL

Output: $AP_STATE/reviews/task-<id>.md  (rendered by the command center).

Usage:  review_packet.py <task-id> <git-dir> <branch> [dest-branch]
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _ap_state() -> Path:
    # AP_STATE = runtime state (reviews/, queue.json, ledger.json): env AP_STATE,
    # else $AP_HOME/state. Fail loudly if neither is set.
    st = os.environ.get("AP_STATE")
    if st:
        return Path(st)
    home = os.environ.get("AP_HOME")
    if not home:
        sys.exit("AP_STATE (and AP_HOME) unset — run inside a ratchet target repo "
                 "(set AP_HOME=<repo>/.autopilot, or AP_STATE explicitly)")
    return Path(home) / "state"


AP_STATE = _ap_state()
REVIEWS = AP_STATE / "reviews"
# Bitbucket "org/repo" for the prefilled PR-create URL; empty → URL omitted.
BB_REPO = os.environ.get("RATCHET_BB_REPO", "")


def _git(gitdir: str, *args) -> str:
    try:
        return subprocess.run(["git", "-C", gitdir, *args],
                              capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ""


def _load_task(tid: str) -> dict:
    try:
        for t in json.loads((AP_STATE / "queue.json").read_text()).get("tasks", []):
            if str(t.get("id")) == tid:
                return t
    except Exception:
        pass
    return {}


def _ledger_entry(tid: str) -> dict:
    try:
        return json.loads((AP_STATE / "ledger.json").read_text()).get("entries", {}).get(tid, {})
    except Exception:
        return {}


def build(tid: str, gitdir: str, branch: str, dest: str) -> Path:
    REVIEWS.mkdir(parents=True, exist_ok=True)
    task = _load_task(tid)
    led = _ledger_entry(tid)
    sha = _git(gitdir, "rev-parse", "HEAD").strip()

    subject = _git(gitdir, "log", "-1", "--format=%s", sha).strip()
    body = _git(gitdir, "log", "-1", "--format=%b", sha).strip()
    stat = _git(gitdir, "show", "--stat", "--format=", sha).strip()

    notes_file = REVIEWS / f"task-{tid}-notes.md"
    notes = notes_file.read_text().strip() if notes_file.exists() else "(the cycle left no review notes)"

    pr_url = (f"https://bitbucket.org/{BB_REPO}/pull-requests/new"
              f"?source={branch}&dest={dest}") if BB_REPO else ""

    contract = task.get("task") or f"(task {tid} not found in queue.json — contract unavailable)"
    lines = [
        f"# Review packet — task `{tid}`",
        "",
        f"- **Shipped**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- **Branch**: `{branch}` @ `{sha[:10]}`",
        *( [f"- **Open a PR**: {pr_url}"] if pr_url else [] ),
        f"- **Backup patch**: `{led.get('patch') or '(none recorded)'}`",
        "",
        "## The contract (what was asked)",
        "",
        "```",
        contract,
        "```",
        "",
        "## The commit",
        "",
        f"**{subject}**",
        "",
        *( [body, ""] if body else [] ),
        "```",
        stat or "(diffstat unavailable)",
        "```",
        "",
        "## Agent review notes (verifier + checker)",
        "",
        notes,
        "",
        "## Reviewer checklist",
        "",
        "- [ ] Diff matches the contract (no scope creep, nothing missing)",
        "- [ ] Tests actually assert the new behaviour (not just green)",
        "- [ ] No guardrail violations (async IO, SQL identifiers, secrets)",
        "- [ ] Merge the PR, or ✗ Reject on the dashboard with a reason",
    ]
    out = REVIEWS / f"task-{tid}.md"
    out.write_text("\n".join(lines) + "\n")
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: review_packet.py <task-id> <git-dir> <branch> [dest-branch]", file=sys.stderr)
        return 2
    tid = argv[0]
    if not re.match(r"^[A-Za-z0-9._-]{1,80}$", tid):
        print(f"invalid task id: {tid}", file=sys.stderr)
        return 2
    dest = argv[3] if len(argv) > 3 else os.environ.get("RATCHET_BASE_BRANCH", "main")
    out = build(tid, argv[1], argv[2], dest)
    print(f"review packet → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
