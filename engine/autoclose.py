#!/usr/bin/env python3
"""Auto-close verified no-op backlog tasks before each cycle.

A task whose recorded commit (ledger `sha`, or the tip of its pushed remote
branch) is already an ANCESTOR of the base branch has SHIPPED — its work is on
the trunk. Left open, the loop re-picks it, finds nothing to change, and
re-escalates forever (the "NO-OP, Nth recurrence" churn that fills the operator
inbox). This reconcile removes such tasks from backlog.yaml and flips their
ledger entry to 'merged', so they never re-inject and never reach the inbox again.

Deterministic — the only signal is `git merge-base --is-ancestor <target> <base>`
(local; refs already fetched by the wrapper). No model, no destructive git. A
target ref that can't be resolved fails the check (safe: the task is kept, not
closed). Human-rejected ships are never auto-closed.

Run by run.sh before task selection each cycle:
    python3 engine/autoclose.py             # apply
    python3 engine/autoclose.py --dry-run   # preview only
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml


def reconcile(tasks: list, entries: dict, is_ancestor, now: str):
    """Pure core: decide which tasks to auto-close.

    A task is closed iff its ledger entry carries a target commit (sha, else the
    remote branch ref) that is an ancestor of the base branch — and it is not a
    human-rejected ship. Returns (closed_ids, kept_tasks, updated_entries).
    """
    closed, kept = [], []
    out = dict(entries)
    for t in tasks:
        tid = str(t.get("id"))
        e = entries.get(tid) or {}
        state = e.get("state")
        # Prefer the immutable sha; fall back to the remote branch ref (kept with
        # its origin/ prefix so it resolves) or a bare branch name → origin/<branch>.
        target = e.get("sha") or e.get("remote_ref") \
            or (f"origin/{e['branch']}" if e.get("branch") else None)
        if state == "rejected" or not target:
            kept.append(t)
            continue
        if is_ancestor(target):
            closed.append(tid)
            merged = dict(e)
            merged.update(state="merged", auto_closed=True, updated_at=now,
                          note=f"auto-closed: {target} is an ancestor of the base branch")
            out[tid] = merged
        else:
            kept.append(t)
    return closed, kept, out


# ── runtime wiring (paths + git; resolved lazily so the module imports test-clean) ──
def _paths():
    home = os.environ.get("AP_HOME")
    if not home:
        raise SystemExit("AP_HOME unset — run inside an orbit target repo.")
    ap_home = Path(home)
    state = Path(os.environ.get("AP_STATE") or ap_home / "state")
    repo = Path(os.environ.get("ORBIT_REPO") or ap_home.parent)
    return ap_home / "backlog.yaml", state / "ledger.json", repo


def _base_branch(ap_home_parent_cfg: Path) -> str:
    for var in ("ORBIT_BASE_BRANCH", "AP_BASE_BRANCH"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        cfg = yaml.safe_load(ap_home_parent_cfg.read_text()) or {}
        if cfg.get("base_branch"):
            return str(cfg["base_branch"])
    except Exception:
        pass
    return "main"


def main():
    ap = argparse.ArgumentParser(description="Auto-close shipped no-op backlog tasks")
    ap.add_argument("--dry-run", action="store_true", help="report what would close; write nothing")
    args = ap.parse_args()

    backlog_path, ledger_path, repo = _paths()
    base = _base_branch(backlog_path.parent / "config.yaml")
    base_ref = f"origin/{base}"

    backlog = yaml.safe_load(backlog_path.read_text()) or {}
    tasks = backlog.get("tasks", []) or []
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"entries": {}}
    entries = ledger.get("entries", {})

    def is_ancestor(target: str) -> bool:
        return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", target, base_ref],
                              capture_output=True).returncode == 0

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    closed, kept, new_entries = reconcile(tasks, entries, is_ancestor, now)

    if not closed:
        print(f"autoclose: nothing to close (base {base})")
        return
    for tid in closed:
        print(f"autoclose: {'[dry-run] would close' if args.dry_run else 'closed'} {tid} "
              f"({new_entries[tid].get('sha') or new_entries[tid].get('remote_ref')} on {base})")
    if args.dry_run:
        print(f"autoclose: {len(closed)} task(s) would close — no changes written")
        return

    backlog["tasks"] = kept
    tmpb = backlog_path.with_suffix(".yaml.tmp")
    tmpb.write_text(yaml.safe_dump(backlog, sort_keys=False, default_flow_style=False,
                                   width=100, allow_unicode=True))
    tmpb.replace(backlog_path)
    ledger["entries"] = new_entries
    tmpl = ledger_path.with_suffix(".json.tmp")
    tmpl.write_text(json.dumps(ledger, indent=2))
    tmpl.replace(ledger_path)
    print(f"autoclose: closed {len(closed)} verified no-op task(s) — removed from backlog, marked merged")


if __name__ == "__main__":
    main()
