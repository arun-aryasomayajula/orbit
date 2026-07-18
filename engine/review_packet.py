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
        sys.exit("AP_STATE (and AP_HOME) unset — run inside a orbit target repo "
                 "(set AP_HOME=<repo>/.autopilot, or AP_STATE explicitly)")
    return Path(home) / "state"


AP_STATE = _ap_state()
REVIEWS = AP_STATE / "reviews"
# Bitbucket "org/repo" for the prefilled PR-create URL; empty → URL omitted.
BB_REPO = os.environ.get("ORBIT_BB_REPO", "")


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


# Paths whose change means the diff touched a surface Orbit normally escalates
# (auth/payments/migrations/secrets/CI). Flagged in the decision header so the
# reviewer's eye lands on the riskiest file first, never as a merge blocker.
SENSITIVE_RE = re.compile(
    r"auth|login|session|password|payment|billing|invoice|migrat|secret|credential"
    r"|\.env|\.github/workflows|\.gitlab-ci|Jenkinsfile", re.IGNORECASE)


def _numstat(gitdir: str, sha: str) -> list[tuple[int, int, str]]:
    """[(adds, dels, path)] for the ship commit; binary files count as 0."""
    rows = []
    for ln in _git(gitdir, "show", "--numstat", "--format=", sha).splitlines():
        parts = ln.split("\t")
        if len(parts) == 3:
            a, d, p = parts
            rows.append((int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0, p))
    return rows


def decision_header(task: dict, led: dict, files: list[tuple[int, int, str]], sha: str) -> list[str]:
    """The decide-in-a-minute block: scope, risk, where to look, how to undo."""
    adds, dels = sum(f[0] for f in files), sum(f[1] for f in files)
    sensitive = [p for _, _, p in files if SENSITIVE_RE.search(p)]
    top = sorted(files, key=lambda f: f[0] + f[1], reverse=True)[:3]
    lines = [
        "## Decide fast",
        "",
        f"- **Scope**: {len(files)} file(s), +{adds} −{dels} · "
        f"category `{task.get('category') or '?'}` · priority {task.get('priority') or '?'}",
        ("- **Sensitive surfaces**: ⚠ " + ", ".join(f"`{p}`" for p in sensitive[:5])
         if sensitive else "- **Sensitive surfaces**: none touched"),
    ]
    if top:
        lines.append("- **Look here first**: " +
                     ", ".join(f"`{p}` (+{a} −{d})" for a, d, p in top))
    undo = f"`git revert {sha[:10]}`"
    if led.get("patch"):
        undo += f" · backup patch: `{led['patch']}`"
    lines.append(f"- **Undo after merge**: {undo}")
    return lines + [""]


def build(tid: str, gitdir: str, branch: str, dest: str) -> Path:
    REVIEWS.mkdir(parents=True, exist_ok=True)
    task = _load_task(tid)
    led = _ledger_entry(tid)
    sha = _git(gitdir, "rev-parse", "HEAD").strip()

    subject = _git(gitdir, "log", "-1", "--format=%s", sha).strip()
    body = _git(gitdir, "log", "-1", "--format=%b", sha).strip()
    stat = _git(gitdir, "show", "--stat", "--format=", sha).strip()
    files = _numstat(gitdir, sha)

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
        *decision_header(task, led, files, sha),
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
    dest = argv[3] if len(argv) > 3 else os.environ.get("ORBIT_BASE_BRANCH", "main")
    out = build(tid, argv[1], argv[2], dest)
    print(f"review packet → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
