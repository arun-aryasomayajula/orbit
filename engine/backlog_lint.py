#!/usr/bin/env python3
"""backlog_lint.py — contract-quality gate for backlog tasks.

Under-specified tasks are the #1 cause of no-commit escalations (the maker
guesses, the verifier rejects, a full opus cycle is burned). This linter grades
each task's CONTRACT before it can be emitted into the loop queue:

  HARD failures (task is NOT emitted unless `lint_ok: true` waives it):
    - missing/unknown category
    - no acceptance_criteria AND context under 40 chars (no contract at all)
  SOFT warnings (emitted, but flagged on the dashboard):
    - no acceptance_criteria (verifier has nothing objective to grade)
    - only one acceptance criterion
    - no context (maker gets a title, not intent)
    - very short title

Usage:  python3 backlog_lint.py            # lint the whole backlog, print a report
Import: from backlog_lint import lint_task # → (hard: list[str], soft: list[str])
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

KNOWN_CATEGORIES = {
    "testing", "documentation", "dependencies", "code_quality", "developer_experience",
    "bug", "feature", "refactor",
    "security", "infrastructure", "release_management", "git_practices",
    "observability",   # in real use in backlog.yaml; human-only for the emit gate
}


def lint_task(t: dict) -> tuple[list[str], list[str]]:
    """Grade one backlog task's contract → (hard_failures, soft_warnings)."""
    hard, soft = [], []
    cat = (t.get("category") or "").strip()
    ctx = (t.get("context") or "").strip()
    acc = [a for a in (t.get("acceptance_criteria") or []) if isinstance(a, str) and a.strip()]
    title = (t.get("title") or "").strip()

    if not cat:
        hard.append("no category")
    elif cat not in KNOWN_CATEGORIES:
        hard.append(f"unknown category '{cat}'")
    if not acc and len(ctx) < 40:
        hard.append("no acceptance criteria and (almost) no context — contract too thin to build against")

    if not acc:
        soft.append("no acceptance criteria — the verifier has nothing objective to grade")
    elif len(acc) == 1:
        soft.append("single acceptance criterion — consider splitting the contract out")
    if not ctx:
        soft.append("no context (WHY) — the maker only gets a title")
    if len(title) < 15:
        soft.append("very short title")

    if t.get("lint_ok"):
        hard = []  # operator explicitly waived the hard gate
    return hard, soft


def main() -> int:
    import yaml
    # backlog.yaml lives under AP_HOME (<target-repo>/.autopilot).
    home = os.environ.get("AP_HOME")
    if not home:
        sys.exit("AP_HOME unset — run inside a ratchet target repo (set AP_HOME=<repo>/.autopilot)")
    backlog = yaml.safe_load((Path(home) / "backlog.yaml").read_text()) or {}
    tasks = backlog.get("tasks") or []
    n_hard = n_soft = 0
    for t in tasks:
        hard, soft = lint_task(t)
        if hard:
            n_hard += 1
            print(f"✗ {t.get('id')}: " + "; ".join(hard))
        elif soft:
            n_soft += 1
    print(f"\n{len(tasks)} tasks · {n_hard} hard-failed (held from queue) · {n_soft} with soft warnings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
