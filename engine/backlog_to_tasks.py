#!/usr/bin/env python3
"""backlog_to_tasks.py — build the loop's queue (queue.json) from backlog.yaml.

backlog.yaml (under AP_HOME) is the single source of truth. Before the queue is
built, every non-native source configured in the target's config.yaml `sources:`
list is folded INTO backlog.yaml by its adapter (see run_source_adapters), so the
queue is built from backlog alone.

Only tasks that are BOTH `autopilot: allow` AND `status: queued` (in a SAFE
category, and not skipped) are emitted into the queue — those are the ones the
loop may pick and ship autonomously. `review-only` / `human` / `proposed` tasks
stay in backlog.yaml only; they show on the command-center board but the loop
never sees them. Re-running is idempotent (queue.json is regenerated each time).

Usage:  python3 backlog_to_tasks.py            # write queue.json
        python3 backlog_to_tasks.py --dry-run  # print what would change
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from backlog_lint import lint_task

# ORBIT_HOME: the engine repo root (adapters live in $ORBIT_HOME/adapters/).
# Fallback: two dirs up from this script ($ORBIT_HOME/engine/backlog_to_tasks.py).
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or Path(__file__).resolve().parent.parent)


def _ap_home() -> Path:
    # AP_HOME = <target-repo>/.autopilot (config.yaml, backlog.yaml, router.yaml, tracks/).
    home = os.environ.get("AP_HOME")
    if not home:
        sys.exit("AP_HOME unset — run inside a orbit target repo (set AP_HOME=<repo>/.autopilot)")
    return Path(home)


def _ap_state() -> Path:
    # AP_STATE = runtime state (queue.json, skips.txt, ...): env AP_STATE, else $AP_HOME/state.
    st = os.environ.get("AP_STATE")
    return Path(st) if st else _ap_home() / "state"


AP_HOME = _ap_home()
AP_STATE = _ap_state()
BACKLOG = AP_HOME / "backlog.yaml"
BACKLOG_REF = ".autopilot/backlog.yaml"       # stable, repo-relative ref for task provenance
QUEUE = AP_STATE / "queue.json"               # output: the loop's queue (generated)
SKIPS = AP_STATE / "skips.txt"

SAFE_CATEGORIES = {
    "testing", "documentation", "dependencies", "code_quality", "developer_experience",
}
# Categories the loop MAY work when explicitly promoted (autopilot:allow). Safe
# chores PLUS review-gated product work (bug/feature/refactor) — the operator opts
# each in via the dashboard. HUMAN-ONLY categories (security, infrastructure,
# release_management, git_practices) are NEVER emitted; the cycle also refuses them.
EMITTABLE_CATEGORIES = SAFE_CATEGORIES | {"bug", "feature", "refactor"}


def load_skips() -> set[str]:
    """Ids the operator skipped from the command center (one id per line)."""
    if not SKIPS.exists():
        return set()
    return {ln.strip() for ln in SKIPS.read_text().splitlines() if ln.strip() and not ln.startswith("#")}


def build_task_string(t: dict) -> str:
    """Fold title + context + acceptance criteria into the `task` string so the
    maker/verifier get the full CONTRACT even reading only this field."""
    parts = [t["title"].strip()]
    ctx = (t.get("context") or "").strip()
    if ctx:
        parts.append("\n\nWHY:\n" + ctx)
    acc = t.get("acceptance_criteria") or []
    acc = [a for a in acc if isinstance(a, str) and a.strip()]
    if acc:
        parts.append("\n\nDONE WHEN (acceptance criteria — the verifier grades these):\n"
                     + "\n".join(f"- {a}" for a in acc))
    ans = (t.get("operator_answer") or "").strip()
    if ans:
        parts.append("\n\nOPERATOR ANSWER (a human answered this task's earlier escalation — "
                     "treat it as part of the contract):\n" + ans)
    return "".join(parts)


def to_queue_entry(t: dict) -> dict:
    """Map a backlog task → the loop's queue task schema (+ contract fields)."""
    return {
        "id": str(t["id"]),
        "task": build_task_string(t),
        "assignee": "",
        "status": "backlog",  # the loop's picker treats `backlog` as pickable
        "priority": t.get("priority", "medium"),
        "source": "backlog",
        "ref": BACKLOG_REF,
        "branch": "",
        "depends_on": "",
        "category": t.get("category", ""),
        # Extra fields the cycle/verifier can read:
        "autopilot": t.get("autopilot", "human"),
        "forced": bool(t.get("forced", False)),   # operator override (human-only category)
        "acceptance_criteria": t.get("acceptance_criteria") or [],
    }


def run_source_adapters() -> None:
    """Fold each configured non-native source INTO backlog.yaml before the queue
    is built. `backlog` is native (always present, no adapter). For every OTHER
    name in the target config.yaml `sources:` list (e.g. foundry, logwatch, qa),
    run $ORBIT_HOME/adapters/<name>_to_backlog.py if it exists; skip silently if
    not. If `sources` is just `[backlog]`, no adapters run.
    """
    cfg_path = AP_HOME / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except OSError:
        return
    sources = cfg.get("sources") or ["backlog"]
    for name in sources:
        if name == "backlog":
            continue
        adapter = ORBIT_HOME / "adapters" / f"{name}_to_backlog.py"
        if adapter.exists():
            print(f"folding source '{name}' → backlog.yaml ({adapter})")
            subprocess.run([sys.executable, str(adapter)], cwd=str(ORBIT_HOME))


def main() -> int:
    dry = "--dry-run" in sys.argv
    run_source_adapters()
    backlog = yaml.safe_load(BACKLOG.read_text()) or {}
    btasks = backlog.get("tasks") or []
    skips = load_skips()

    emit, held = [], []
    for t in btasks:
        tid = str(t.get("id", "")).strip()
        if not tid:
            continue
        allow = t.get("autopilot") == "allow"
        queued = t.get("status") == "queued"
        cat = t.get("category", "")
        if tid in skips:
            held.append((tid, "skipped via command center"))
        elif not queued:
            held.append((tid, f"status={t.get('status')}"))
        elif not allow:
            held.append((tid, f"autopilot={t.get('autopilot')} (review/human — board only)"))
        elif cat not in EMITTABLE_CATEGORIES and not t.get("forced"):
            # allow + queued but a HUMAN-ONLY category (security/infra/release/git):
            # never AUTO-emit. The operator can still hand it to the loop explicitly
            # via the dashboard "⚠ Force to agent" action, which sets forced:true.
            held.append((tid, f"autopilot=allow but category '{cat}' is human-only and not forced — NOT emitted"))
        elif (hard := lint_task(t)[0]):
            # Contract too thin to build against — an under-specified task burns a
            # full opus cycle and escalates. Fix the contract, or waive with
            # `lint_ok: true` (dashboard "✓ Lint OK" action).
            held.append((tid, "lint: " + "; ".join(hard) + " — fix the contract or set lint_ok: true"))
        else:
            emit.append(to_queue_entry(t))

    # backlog.yaml is the SINGLE source of truth — every configured source adapter
    # (run above) folds INTO it, so the loop queue is built from backlog alone.
    # queue.json lives under AP_STATE (not in the worktree, which the loop resets).
    doc = {"version": 2, "tasks": emit, "archive": []}

    print(f"backlog: {len(btasks)} tasks | emitted to loop queue: {len(emit)} | held off: {len(held)}")
    for tid, why in held:
        print(f"  · held {tid}: {why}")
    print(f"  → queue.json: {len(emit)} loop-pickable task(s)")

    if dry:
        print("(--dry-run: queue.json NOT written)")
        return 0
    QUEUE.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {QUEUE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
