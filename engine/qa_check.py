#!/usr/bin/env python3
"""qa_check.py — prove a ship in the RUNNING product, not just the diff.

Gates + verifier judge the diff; nothing drives the app — which is how a ship
can be gate-green, verifier-approved, and still unobserved in the product
("fixed but not fixed"). run.sh calls this after the cycle commits and BEFORE
it pushes.

This module is a strict superset of the retired `runtime_check.py`:

  - No `qa:` block in the target's config → LEGACY path, byte-for-byte
    yesterday's runtime_check behaviour: `runtime_check:` config,
    `/orbit-runtime-check <task-id>` skill, `orbit.settings.json`,
    `runtime_check.categories` modes, verdict `reviews/task-<id>-runtime.json`,
    the `ORBIT_RUNTIME_CMD` test hook.
  - `qa:` block present → QA path (supersedes runtime_check: per
    config/schema.yaml): a browser-driving agent (`/orbit-qa-check
    <task-id>`) launches (or attaches to) the product via a per-run
    Playwright MCP server, drives the flow the acceptance criteria name, and
    writes `reviews/task-<id>-qa.json` (see skills/orbit-qa-check.md for the
    full verdict contract). Judged against `qa.gate_categories`. The
    `ORBIT_QA_CMD` test hook replaces the claude call the same way
    `ORBIT_RUNTIME_CMD` does for the legacy path.

Exit codes (run.sh dispatches on EXACTLY 3 — anything else ships):
    0   ship — evidence attached, check skipped/off, or unable-to-run/errored
    3   escalate — legacy: the check RAN, category is `required`, and observed
        behaviour CONTRADICTS a criterion. QA: verdict is `fail` on a
        `required` category and `decide_fail_action` (see below) says so.

Unable-to-run is never a failure in either path (a dev server — or full
stack — that won't boot on this machine must not block ships); a missing or
malformed verdict is likewise never a failure — a wiring/agent mistake must
never look like an observed contradiction. Only an observed contradiction
(legacy) or a `fail` verdict (QA) on a category the operator marked
`required` escalates.

Config (`.autopilot/config.yaml`) — legacy:
    runtime_check:
      launch: "npm run dev"
      url: "http://localhost:3000"
      timeout_seconds: 900
      categories:
        frontend: required
        feature: evidence

Config — QA (see config/schema.yaml for the full block):
    qa:
      services: [...]
      base_url: "http://localhost:3000"
      timeout_seconds: 1200
      model: "sonnet"
      mcp_config: null            # null → engine auto-generates a Playwright MCP config
      browser: "chromium"
      headless: true
      gate_categories:
        frontend: required
        feature: evidence

Note (review finding carried forward): `qa.base_url` is NOT validate-enforced
alongside `qa.services` — a missing/empty base_url must degrade to an
`unable` verdict IN THE AGENT; this wrapper never crashes on it either way,
since it only ever reads the agent's verdict file.

decide_fail_action — the Task 16 seam: when the QA verdict is `fail` on a
`required` category, THIS task always escalates. Task 16 replaces its body
with the dev⇄QA bounce protocol (re-file the task with QA findings, up to
`qa.max_bounces` times, before escalating). The call site does not change.

Test hooks: ORBIT_RUNTIME_CMD (legacy claude call), ORBIT_QA_CMD (QA claude
call) — same mechanism as before, TASK_ID exported either way.

Usage:  qa_check.py <task-id> <worktree>
"""
from __future__ import annotations

import json
import os
import re
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
QA_DIR = AP_STATE / "qa"
DEFAULT_TIMEOUT = 900          # legacy runtime_check default
QA_DEFAULT_TIMEOUT = 1200      # qa: default (config/schema.yaml)
QA_DEFAULT_MODEL = "sonnet"
QA_DEFAULT_BROWSER = "chromium"

SHIP, ESCALATE = 0, 3

# Same "lowercase slug" shape plan_gate.py/command_center.py enforce — a task
# id is an engine-internal identifier, not free text, and every path below is
# derived from it. Validated before any path/file use. Unlike plan_gate's
# USAGE=2, an invalid id here just ships — this module's cardinal rule (both
# paths) is "never block a ship on a wiring mistake".
_TID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


def _load_task(tid: str) -> dict:
    try:
        for t in json.loads((AP_STATE / "queue.json").read_text()).get("tasks", []):
            if str(t.get("id")) == tid:
                return t
    except (OSError, ValueError):
        pass
    return {}


def _unlink_stale_verdicts(tid: str) -> None:
    """Unlink both verdict kinds up front, whichever path is about to run —
    a stale verdict from a PRIOR run of this tid (legacy or QA, config may
    have changed between runs) must never fold into this run's judgment."""
    for name in (f"task-{tid}-runtime.json", f"task-{tid}-qa.json"):
        try:
            (REVIEWS / name).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Legacy path — exactly runtime_check.py's behaviour (module renamed).
# ---------------------------------------------------------------------------

def run_legacy_agent(cfg_all: dict, tid: str, worktree: str, timeout: int) -> None:
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


def legacy_check(cfg_all: dict, tid: str, worktree: str) -> int:
    rc_cfg = cfg_all.get("runtime_check")
    if not rc_cfg:
        return SHIP                      # feature off → yesterday's behaviour
    cat = (_load_task(tid).get("category") or "").strip()
    mode = (rc_cfg.get("categories") or {}).get(cat)
    if mode not in ("evidence", "required"):
        print(f"runtime check: category '{cat or '?'}' not configured — skipped")
        return SHIP

    verdict_file = REVIEWS / f"task-{tid}-runtime.json"
    run_legacy_agent(cfg_all, tid, worktree, int(rc_cfg.get("timeout_seconds") or DEFAULT_TIMEOUT))

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


# ---------------------------------------------------------------------------
# QA path — browser-driving gate (config/schema.yaml `qa:` block).
# ---------------------------------------------------------------------------

def _mcp_args(browser: str, output_dir: str, headless: bool) -> list[str]:
    args = ["@playwright/mcp@latest", "--isolated"]
    if headless:
        args.append("--headless")
    args += ["--browser", browser, "--output-dir", output_dir]
    return args


def _resolve_mcp_config(tid: str, qa_cfg: dict) -> Path:
    """`qa.mcp_config` set → use that path as-is (nothing generated). Else
    write a per-run Playwright MCP config to $AP_STATE/qa/mcp-run.json."""
    configured = qa_cfg.get("mcp_config")
    if configured:
        return Path(configured)
    browser = qa_cfg.get("browser") or QA_DEFAULT_BROWSER
    headless = qa_cfg.get("headless", True)
    output_dir = str(AP_STATE / "qa" / f"task-{tid}")
    mcp_cfg = {"mcpServers": {"playwright": {
        "command": "npx",
        "args": _mcp_args(browser, output_dir, headless),
    }}}
    QA_DIR.mkdir(parents=True, exist_ok=True)
    path = QA_DIR / "mcp-run.json"
    path.write_text(json.dumps(mcp_cfg))
    return path


def run_qa_agent(cfg_all: dict, qa_cfg: dict, tid: str, worktree: str,
                  mcp_path: Path, timeout: int) -> None:
    override = os.environ.get("ORBIT_QA_CMD")
    if override:
        cmd = shlex.split(override)
    else:
        claude = shutil.which("claude")
        if not claude:
            print("WARN: claude not on PATH — QA check skipped")
            return
        cmd = [claude, "--print", f"/orbit-qa-check {tid}",
               "--mcp-config", str(mcp_path),
               "--settings", str(ORBIT_HOME / "config" / "orbit.qa.settings.json"),
               "--permission-mode", cfg_all.get("permission_mode", "acceptEdits"),
               "--model", qa_cfg.get("model", QA_DEFAULT_MODEL),
               "--add-dir", str(ORBIT_HOME)]
    # QA_MCP_CONFIG is set regardless of override — the resolved MCP config
    # path is otherwise only visible via the --mcp-config CLI flag, which an
    # ORBIT_QA_CMD override (like ORBIT_RUNTIME_CMD) replaces wholesale rather
    # than receiving as extra argv. This gives override scripts (and tests)
    # a stable, argv-independent way to see what was resolved.
    env = dict(os.environ, AP_HOME=str(AP_HOME), AP_STATE=str(AP_STATE),
               ORBIT_HOME=str(ORBIT_HOME), TASK_ID=tid, QA_MCP_CONFIG=str(mcp_path))
    try:
        subprocess.run(cmd, cwd=worktree, env=env, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"WARN: QA agent for {tid} failed: {e}")


def decide_fail_action(tid: str, worktree: str, qa_cfg: dict, verdict: dict, mode: str) -> int:
    """Task 16 seam.

    Called exactly once, only when the QA agent's verdict is `fail` on a
    `required` category (`evidence` never reaches here — it ships and logs).
    THIS task always escalates. Task 16 replaces this body with the dev⇄QA
    bounce protocol: re-file the task back onto the queue with the QA
    agent's findings attached, up to `qa.max_bounces` times, THEN escalate
    once the bounce budget is exhausted. The signature already carries
    everything the bounce needs (tid, worktree, qa_cfg for max_bounces,
    the verdict dict, and the gate mode) so Task 16 does not have to touch
    the call site.
    """
    return ESCALATE


def qa_check(cfg_all: dict, qa_cfg: dict, tid: str, worktree: str) -> int:
    cat = (_load_task(tid).get("category") or "").strip()
    gate_categories = qa_cfg.get("gate_categories") or {}
    mode = gate_categories.get(cat)
    if mode not in ("evidence", "required"):
        print(f"qa check: category '{cat or '?'}' not in gate_categories — skipped")
        return SHIP                      # mirrors legacy semantics — nothing runs

    mcp_path = _resolve_mcp_config(tid, qa_cfg)
    timeout = int(qa_cfg.get("timeout_seconds") or QA_DEFAULT_TIMEOUT)
    verdict_file = REVIEWS / f"task-{tid}-qa.json"
    run_qa_agent(cfg_all, qa_cfg, tid, worktree, mcp_path, timeout)

    try:
        raw = verdict_file.read_text()
    except OSError:
        print(f"WARN: QA agent left no verdict for {tid} — shipping without QA evidence")
        return SHIP
    try:
        v = json.loads(raw)
        if not isinstance(v, dict):
            raise ValueError(f"verdict json is a {type(v).__name__}, not an object")
    except ValueError as e:
        print(f"WARN: QA agent left a malformed verdict for {tid} ({e}) — "
              f"shipping without QA evidence")
        return SHIP

    verdict = v.get("verdict")
    ran, contradicts = bool(v.get("ran")), bool(v.get("contradicts"))
    summary = (v.get("summary") or "").strip()
    print(f"qa check ({cat}/{mode}): verdict={verdict} ran={ran} "
          f"contradicts={contradicts} — {summary}")

    if verdict not in ("pass", "fail", "unable"):
        print(f"WARN: QA agent verdict for {tid} was not pass/fail/unable "
              f"({verdict!r}) — shipping without QA evidence")
        return SHIP
    if verdict == "unable" or not ran:
        return SHIP                      # unable-to-run is never a failure
    if verdict == "pass":
        return SHIP
    # verdict == "fail"
    if mode == "evidence":
        return SHIP                      # evidence only, never blocks
    return decide_fail_action(tid, worktree, qa_cfg, v, mode)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: qa_check.py <task-id> <worktree>", file=sys.stderr)
        return SHIP                      # never block a ship on a wiring mistake
    tid, worktree = argv[0], argv[1]
    if not _TID_RE.match(tid):
        print(f"qa_check: task id {tid!r} is not a valid slug — skipped", file=sys.stderr)
        return SHIP

    try:
        cfg_all = yaml.safe_load((AP_HOME / "config.yaml").read_text()) or {}
    except OSError:
        return SHIP

    _unlink_stale_verdicts(tid)

    qa_cfg = cfg_all.get("qa")
    if isinstance(qa_cfg, dict):
        return qa_check(cfg_all, qa_cfg, tid, worktree)
    return legacy_check(cfg_all, tid, worktree)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
