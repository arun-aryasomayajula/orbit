#!/usr/bin/env python3
"""epic_plan.py — the planning tier: epic → spec → human approval → child tasks.

The one-commit loop refuses design decisions by construction; epics are how big
work still flows through Orbit without weakening that. An `epic` backlog task is
a CONTAINER — the loop never picks one (backlog_to_tasks holds them uncondition-
ally). Instead it moves through a human-gated pipeline, tracked in the task's
own `status` field (backlog.yaml stays the single source of truth):

  proposed ──plan──▶ planning ──▶ spec_ready ──approve──▶ approved
           ──decompose──▶ decomposing ──▶ decomposed

  plan       headless /orbit-plan writes .autopilot/specs/<id>.md for review
  approve    a HUMAN act (dashboard or CLI) — no model involved
  decompose  headless /orbit-decompose emits child proposals; they merge into
             backlog.yaml as proposed/human with `epic: <id>` — the human then
             queues children like any other task

A failed/incomplete agent step falls back to the previous stable stage. A
mirror of stage + notes is kept in $AP_STATE/epics.json for the dashboard.

Usage:  epic_plan.py <target-repo> plan|approve|decompose|status <epic-id> [--timeout N]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from backlog_append import append_tasks, set_task_field

ENGINE = Path(__file__).resolve().parent
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or ENGINE.parent)
AGENT_TIMEOUT = 1800

# action → (allowed starting stages, in-flight stage, success stage, fallback stage)
TRANSITIONS = {
    "plan":      ({"proposed", "spec_ready"}, "planning", "spec_ready", "proposed"),
    "approve":   ({"spec_ready"}, None, "approved", None),
    "decompose": ({"approved"}, "decomposing", "decomposed", "approved"),
}


def allowed(action: str, stage: str) -> bool:
    return stage in TRANSITIONS[action][0]


class Epic:
    def __init__(self, target: Path):
        self.target = target
        self.ap = target / ".autopilot"
        self.state = self.ap / "state"
        self.backlog = self.ap / "backlog.yaml"
        self.cfg = yaml.safe_load((self.ap / "config.yaml").read_text()) or {}

    def task(self, tid: str) -> dict | None:
        for t in (yaml.safe_load(self.backlog.read_text()) or {}).get("tasks") or []:
            if str(t.get("id")) == tid:
                return t
        return None

    def spec_path(self, tid: str) -> Path:
        return self.ap / "specs" / f"{tid}.md"

    def set_stage(self, tid: str, stage: str, note: str = ""):
        set_task_field(self.backlog, tid, "status", stage)
        reg_file = self.state / "epics.json"
        try:
            reg = json.loads(reg_file.read_text())
        except (OSError, ValueError):
            reg = {"epics": {}}
        e = reg["epics"].setdefault(tid, {})
        e.update(stage=stage, note=note, spec=str(self.spec_path(tid)),
                 updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        reg_file.parent.mkdir(parents=True, exist_ok=True)
        reg_file.write_text(json.dumps(reg, indent=2) + "\n")

    def run_agent(self, command: str, tid: str, timeout: int) -> bool:
        claude = shutil.which("claude")
        if not claude:
            print("✗ claude not on PATH"); return False
        env = dict(os.environ)
        env.update(AP_HOME=str(self.ap), AP_STATE=str(self.state), ORBIT_HOME=str(ORBIT_HOME))
        cmd = [claude, "--print", f"/{command} {tid}",
               "--permission-mode", self.cfg.get("permission_mode", "acceptEdits"),
               "--settings", str(ORBIT_HOME / "config" / "orbit.settings.json"),
               "--model", self.cfg.get("model", "claude-opus-4-8"),
               "--add-dir", str(ORBIT_HOME)]
        print(f"· /{command} {tid} (≤{timeout}s)")
        try:
            p = subprocess.run(cmd, cwd=self.target, env=env, timeout=timeout,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               stdin=subprocess.DEVNULL)
            return p.returncode == 0
        except subprocess.TimeoutExpired:
            print(f"✗ /{command} timed out after {timeout}s")
            return False


def check_epic(ep: Epic, tid: str, action: str) -> dict:
    t = ep.task(tid)
    if not t:
        sys.exit(f"✗ no task '{tid}' in {ep.backlog}")
    if t.get("category") != "epic":
        sys.exit(f"✗ task '{tid}' is category '{t.get('category')}', not epic")
    stage = t.get("status") or "proposed"
    if not allowed(action, stage):
        sys.exit(f"✗ cannot {action} from stage '{stage}' "
                 f"(needs one of: {', '.join(sorted(TRANSITIONS[action][0]))})")
    return t


def do_plan(ep: Epic, tid: str, timeout: int) -> int:
    check_epic(ep, tid, "plan")
    _, running, done, fallback = TRANSITIONS["plan"]
    ep.set_stage(tid, running, "planner running")
    ep.spec_path(tid).parent.mkdir(parents=True, exist_ok=True)
    ep.run_agent("orbit-plan", tid, timeout)
    # The spec ON DISK is the success signal, not the agent's exit code.
    if ep.spec_path(tid).exists() and ep.spec_path(tid).stat().st_size > 200:
        ep.set_stage(tid, done, "spec written — review, then approve")
        print(f"✓ spec ready: {ep.spec_path(tid)}\n  review it, then: orbit epic <target> approve {tid}")
        return 0
    ep.set_stage(tid, fallback, "planner produced no spec")
    print("✗ planner produced no usable spec — epic back to proposed")
    return 1


def do_approve(ep: Epic, tid: str) -> int:
    check_epic(ep, tid, "approve")
    if not ep.spec_path(tid).exists():
        sys.exit(f"✗ spec missing ({ep.spec_path(tid)}) — re-run plan")
    ep.set_stage(tid, "approved", "spec approved by operator")
    print(f"✓ {tid} approved — next: orbit epic <target> decompose {tid}")
    return 0


def do_decompose(ep: Epic, tid: str, timeout: int) -> int:
    check_epic(ep, tid, "decompose")
    _, running, done, fallback = TRANSITIONS["decompose"]
    ep.set_stage(tid, running, "decomposer running")
    children_file = ep.state / "epics" / f"{tid}-children.json"
    children_file.parent.mkdir(parents=True, exist_ok=True)
    ep.run_agent("orbit-decompose", tid, timeout)
    try:
        children = list(json.loads(children_file.read_text()).get("proposals") or [])
    except (OSError, ValueError):
        children = []
    if not children:
        ep.set_stage(tid, fallback, "decomposer produced no children")
        print("✗ no child proposals produced — epic stays approved")
        return 1
    for c in children:
        c["epic"] = tid   # linkage is the wrapper's job, never trusted from the agent
    added, skipped = append_tasks(ep.backlog, children, f"epic:{tid}",
                                  f"decomposed from epic {tid} (spec: .autopilot/specs/{tid}.md)")
    ep.set_stage(tid, done, f"{len(added)} child task(s) proposed")
    print(f"✓ {tid} decomposed → {len(added)} proposed child task(s); queue them from the dashboard")
    for cid in added:
        print(f"    + {cid}")
    for cid, why in skipped:
        print(f"    · skipped {cid}: {why}")
    return 0


def do_status(ep: Epic, tid: str) -> int:
    t = ep.task(tid)
    if not t:
        sys.exit(f"✗ no task '{tid}'")
    print(f"{tid}: stage={t.get('status')} spec={'yes' if ep.spec_path(tid).exists() else 'no'}")
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 3:
        print(__doc__)
        return 2
    target, action, tid = Path(args[0]).resolve(), args[1], args[2]
    timeout = AGENT_TIMEOUT
    for i, a in enumerate(argv):
        if a == "--timeout" and i + 1 < len(argv):
            timeout = int(argv[i + 1])
    if not (target / ".autopilot" / "config.yaml").exists():
        sys.exit(f"✗ {target} has no .autopilot/ — run `orbit init` first")
    ep = Epic(target)
    if action == "plan":
        return do_plan(ep, tid, timeout)
    if action == "approve":
        return do_approve(ep, tid)
    if action == "decompose":
        return do_decompose(ep, tid, timeout)
    if action == "status":
        return do_status(ep, tid)
    print(f"unknown action '{action}' (plan|approve|decompose|status)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
