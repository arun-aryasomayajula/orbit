#!/usr/bin/env python3
"""runtime_check.py — prove a ship in the RUNNING product, not just the diff.

Gates + verifier judge the diff; nothing drives the app — which is how a ship
can be gate-green, verifier-approved, and still unobserved in the product
("fixed but not fixed"). run.sh calls this after the cycle commits and BEFORE
it pushes: a runtime agent (`/orbit-runtime-check <task-id>`) launches or
attaches to the app per the target's `runtime_check:` config, drives the flow
the acceptance criteria name, and writes:

    $AP_STATE/reviews/task-<id>-runtime.md     evidence for the review packet
    $AP_STATE/reviews/task-<id>-runtime.json   {"ran": bool, "contradicts": bool, "summary": str}

Exit codes (run.sh dispatches on EXACTLY 3 — anything else ships):
    0   ship — evidence attached, or check skipped/unable-to-run/errored
    3   escalate — the check RAN, the category is `required`, and observed
        behaviour CONTRADICTS an acceptance criterion

Unable-to-run is never a failure (a dev server that won't boot on this
machine must not block ships); only an observed contradiction escalates, and
only for categories the operator marked `required`. No `runtime_check:` block
→ this is a no-op, exactly yesterday's behaviour.

Config (`.autopilot/config.yaml`):
    runtime_check:
      launch: "npm run dev"            # optional — how the agent starts the app
      url: "http://localhost:3000"     # optional — where to reach it
      timeout_seconds: 900
      categories:                      # only listed categories are checked
        frontend: required             # contradiction blocks the ship → escalate
        feature: evidence              # evidence only, never blocks

Test hook: ORBIT_RUNTIME_CMD replaces the claude call (TASK_ID exported).

Usage:  runtime_check.py <task-id> <worktree>
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


def _ap_state() -> Path:
    st = os.environ.get("AP_STATE")
    if st:
        return Path(st)
    home = os.environ.get("AP_HOME")
    if not home:
        sys.exit("AP_STATE (and AP_HOME) unset — run inside a orbit target repo")
    return Path(home) / "state"


AP_STATE = _ap_state()
AP_HOME = Path(os.environ.get("AP_HOME") or AP_STATE.parent)
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or Path(__file__).resolve().parent.parent)
REVIEWS = AP_STATE / "reviews"
DEFAULT_TIMEOUT = 900

SHIP, ESCALATE = 0, 3


def _load_task(tid: str) -> dict:
    try:
        for t in json.loads((AP_STATE / "queue.json").read_text()).get("tasks", []):
            if str(t.get("id")) == tid:
                return t
    except (OSError, ValueError):
        pass
    return {}


def run_agent(cfg_all: dict, tid: str, worktree: str, timeout: int) -> None:
    override = os.environ.get("ORBIT_RUNTIME_CMD")
    if override:
        cmd = shlex.split(override)
    else:
        claude = shutil.which("claude")
        if not claude:
            print("WARN: claude not on PATH — runtime check skipped")
            return
        cmd = [claude, "--print", f"/orbit-runtime-check {tid}",
               "--permission-mode", cfg_all.get("permission_mode", "acceptEdits"),
               "--settings", str(ORBIT_HOME / "config" / "orbit.settings.json"),
               "--model", cfg_all.get("model", "claude-opus-4-8"),
               "--add-dir", str(ORBIT_HOME)]
    env = dict(os.environ, AP_HOME=str(AP_HOME), AP_STATE=str(AP_STATE),
               ORBIT_HOME=str(ORBIT_HOME), TASK_ID=tid)
    try:
        subprocess.run(cmd, cwd=worktree, env=env, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"WARN: runtime agent for {tid} failed: {e}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: runtime_check.py <task-id> <worktree>", file=sys.stderr)
        return SHIP                      # never block a ship on a wiring mistake
    tid, worktree = argv[0], argv[1]
    try:
        cfg_all = yaml.safe_load((AP_HOME / "config.yaml").read_text()) or {}
    except OSError:
        return SHIP
    rc_cfg = cfg_all.get("runtime_check")
    if not rc_cfg:
        return SHIP                      # feature off → yesterday's behaviour
    cat = (_load_task(tid).get("category") or "").strip()
    mode = (rc_cfg.get("categories") or {}).get(cat)
    if mode not in ("evidence", "required"):
        print(f"runtime check: category '{cat or '?'}' not configured — skipped")
        return SHIP

    verdict_file = REVIEWS / f"task-{tid}-runtime.json"
    try:
        verdict_file.unlink()
    except OSError:
        pass
    run_agent(cfg_all, tid, worktree, int(rc_cfg.get("timeout_seconds") or DEFAULT_TIMEOUT))

    try:
        v = json.loads(verdict_file.read_text())
    except (OSError, ValueError):
        print(f"WARN: runtime agent left no verdict for {tid} — shipping without runtime evidence")
        return SHIP
    ran, contradicts = bool(v.get("ran")), bool(v.get("contradicts"))
    summary = (v.get("summary") or "").strip()
    print(f"runtime check ({cat}/{mode}): ran={ran} contradicts={contradicts} — {summary}")
    if not ran:
        return SHIP                      # unable-to-run is never a failure
    if contradicts and mode == "required":
        return ESCALATE                  # observed contradiction on a required surface
    return SHIP


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
