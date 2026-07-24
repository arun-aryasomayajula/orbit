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

Exit codes (run.sh dispatches on EXACTLY 3 or 4 — anything else ships):
    0   ship — evidence attached, check skipped/off, or unable-to-run/errored
    3   escalate — legacy: the check RAN, category is `required`, and observed
        behaviour CONTRADICTS a criterion. QA: verdict is `fail` on a
        `required` category and `decide_fail_action` (see below) says so
        (either genuinely at the bounce cap, or as the safe fallback for any
        unexpected error inside the bounce path itself).
    4   QA bounce — verdict is `fail` on a `required` category, the task's
        `qa_bounces` counter is still under `qa.max_bounces`: the SAME task
        is re-queued (status: queued) with the QA findings folded into its
        contract for the next pick. run.sh does not push or escalate on 4.

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

decide_fail_action — the dev⇄QA bounce protocol: when the QA verdict is
`fail` on a `required` category, re-file the SAME task back onto the queue
with the QA agent's findings folded into its contract (`qa_findings`, read by
backlog_to_tasks.build_task_string — see Task 15), up to `qa.max_bounces`
times (default 2; the task's own `qa_bounces` counter on its backlog block
tracks how many bounces it has already had). Once the cap is reached, escalate
instead — with the QA evidence (verdict + screenshot + findings report)
attached to the ledger entry for a human to review. Any unexpected error
inside the bounce path (e.g. an unwritable/missing backlog.yaml) falls back to
escalating too: a half-applied bounce must never ship. The call site does not
change; run.sh dispatches on the return value (4 → bounce, 3 → escalate).

Test hooks: ORBIT_RUNTIME_CMD (legacy claude call), ORBIT_QA_CMD (QA claude
call) — same mechanism as before, TASK_ID exported either way.

Usage:  qa_check.py <task-id> <worktree> [patch-path]
        patch-path is optional (argv-2-only invocations still work) — it's
        the wrapper's captured backup patch, folded into the bounce's
        `qa_findings` note purely for operator/context value.
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

from backlog_append import get_task_field, set_task_field
import notify


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
DEFAULT_MAX_BOUNCES = 2         # qa.max_bounces default (config/schema.yaml)
ENGINE_DIR = Path(__file__).resolve().parent

SHIP, ESCALATE, BOUNCE = 0, 3, 4

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
    Path(output_dir).mkdir(parents=True, exist_ok=True)   # --output-dir target, ahead of the race
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


def _engine_env() -> dict:
    return dict(os.environ, AP_HOME=str(AP_HOME), AP_STATE=str(AP_STATE),
               ORBIT_HOME=str(ORBIT_HOME))


def _ledger(*args: str) -> subprocess.CompletedProcess:
    """Run one ledger.py verb, tolerating it not being on the ledger at all
    (attach/qa-verdict silently no-op on a missing entry — see callers)."""
    return subprocess.run(
        [sys.executable, str(ENGINE_DIR / "ledger.py"), *args],
        cwd=str(ENGINE_DIR), env=_engine_env(), capture_output=True, text=True, timeout=30)


def _dashboard_url() -> str:
    # Same fallback run.sh's own DASH uses — ORBIT_DASHBOARD_URL is exported
    # into this process's env by config.py's shellenv when run.sh invokes us.
    return os.environ.get("ORBIT_DASHBOARD_URL") or "http://127.0.0.1:8787"


def _unsafe_qa_path_reason(base: Path, rel) -> str | None:
    """None = `rel` resolves safely under `base` (QA_DIR, $AP_STATE/qa/).
    Mirrors knowledge.py's `_unsafe_doc_reason`/`_unsafe_patch_reason`
    containment discipline: a QA verdict's `screenshots` list is written by
    the QA agent — untrusted enough that it must never be able to point an
    `attach` call at a path outside qa/ (absolute, or `../` traversal)."""
    if not isinstance(rel, str) or not rel:
        return "not a valid relative path"
    if os.path.isabs(rel):
        return "absolute paths are not allowed — must be relative to qa/"
    try:
        resolved = (base / rel).resolve()
        base_resolved = base.resolve()
    except (OSError, ValueError) as e:      # ValueError: e.g. an embedded NUL byte
        return f"could not resolve path ({e})"
    if not resolved.is_relative_to(base_resolved):
        return "escapes qa/ (path traversal)"
    return None


def _read_qa_bounces(backlog: Path, tid: str) -> int:
    raw = get_task_field(backlog, tid, "qa_bounces")
    if isinstance(raw, str):
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
            raw = raw[1:-1]        # hand-quoted qa_bounces: "2" — strip before int()
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _escalate_with_evidence(tid: str, summary: str, verdict: dict) -> int:
    """Cap reached (or evidence-preserving path in general): stamp the QA
    verdict and attach what evidence exists, then let run.sh's rc==3 branch
    do the actual escalate + notify (never double-notify from here).

    CRITICAL: ledger.py's attach/qa-verdict verbs silently no-op (exit 0, no
    output) when `tid` has no ledger entry — a wrong/stale tid would
    otherwise discard this evidence with no trace. Check the entry exists
    first and WARN loudly if it doesn't, so that failure mode is visible
    instead of silent."""
    if not _ledger("state", tid).stdout.strip():
        print(f"WARN: no ledger entry for '{tid}' — the qa-verdict/attach calls below "
              f"will silently no-op; QA evidence will NOT be recorded for review")
    _ledger("qa-verdict", tid, "fail", summary)

    # Attach the RELATIVE-to-qa/ string, not the resolved absolute path —
    # command_center.py's /qa-shot and /qa-report routes (Task 17) only ever
    # serve paths relative to $AP_STATE/qa/ and reject absolute `p` outright.
    # The containment check still resolves against QA_DIR to validate (catches
    # traversal/symlink escapes); only the string handed to `ledger.py attach`
    # changes, so evidence attached here can actually render on the dashboard.
    shots = verdict.get("screenshots")
    if isinstance(shots, list) and shots:
        first = shots[0]
        reason = _unsafe_qa_path_reason(QA_DIR, first)
        if reason:
            print(f"WARN: QA verdict screenshot {first!r} for {tid} rejected ({reason}) — not attached")
        else:
            _ledger("attach", tid, "screenshot", first)

    findings_rel = f"task-{tid}/findings.md"
    findings_path = QA_DIR / findings_rel
    if findings_path.exists():
        _ledger("attach", tid, "report", findings_rel)
    else:
        print(f"WARN: no findings report at {findings_path} for {tid} — not attached")

    return ESCALATE


def _bounce(tid: str, qa_cfg: dict, summary: str, patch_path: str,
            bounces: int, max_bounces: int) -> int:
    """Re-file the SAME task with the QA findings folded into its contract
    and re-queue it — mirrors command_center.do_answer's requeue precedent
    (field write → status queued → ledger clear → converter re-run)."""
    backlog = AP_HOME / "backlog.yaml"
    new_bounces = bounces + 1
    findings_report = f"$AP_STATE/qa/task-{tid}/findings.md"
    patch_note = patch_path or "none kept"
    message = " ".join(
        f"{summary} — full report: {findings_report} "
        f"(previous attempt patch: {patch_note})".split())

    if not set_task_field(backlog, tid, "qa_bounces", str(new_bounces)):
        raise RuntimeError(f"task {tid!r} not found in {backlog} — cannot bounce")
    set_task_field(backlog, tid, "status", "queued")
    set_task_field(backlog, tid, "qa_findings", json.dumps(message))

    # Audit order matters even though `clear` immediately erases the entry
    # `qa-verdict` just stamped (clear wins — nothing about this task's QA
    # history survives on the ledger past a bounce): stamp first anyway, so
    # ledger.json's on-disk history (if anything snapshots it) shows the
    # verdict was recorded before the re-pick, not skipped.
    _ledger("qa-verdict", tid, "fail", summary)
    _ledger("clear", tid)

    subprocess.run([sys.executable, str(ENGINE_DIR / "backlog_to_tasks.py")],
                   cwd=str(ENGINE_DIR), env=_engine_env(), capture_output=True, timeout=60)

    # Past this point the bounce is DONE (backlog re-filed, ledger cleared,
    # queue regenerated) — notify is best-effort UI sugar, not part of the
    # bounce's atomicity. It can't raise today (notify.py swallows every
    # exception itself), but that safety lives in another file; guard it
    # here too so a future change to notify.py can never let this function's
    # own exception propagate up into decide_fail_action's outer try/except
    # and turn an already-committed bounce into a return of ESCALATE (3) —
    # which would desync run.sh's dispatch from what actually happened.
    try:
        notify.send("🔁 Orbit QA bounce",
                    f"task {tid}: QA failed (bounce {new_bounces}/{max_bounces}) — "
                    f"re-queued with findings in the contract", _dashboard_url())
    except Exception as e:
        print(f"WARN: notify.send raised for {tid}'s bounce ({e!r}) — "
              f"bounce already committed, continuing")
    return BOUNCE


def _decide_fail_action(tid: str, worktree: str, qa_cfg: dict, verdict: dict, mode: str,
                        patch_path: str) -> int:
    summary = (verdict.get("summary") or "").strip()
    mb = qa_cfg.get("max_bounces")
    max_bounces = int(mb) if mb is not None else DEFAULT_MAX_BOUNCES   # 0 is a valid cap
    backlog = AP_HOME / "backlog.yaml"
    bounces = _read_qa_bounces(backlog, tid)

    if bounces < max_bounces:
        return _bounce(tid, qa_cfg, summary, patch_path, bounces, max_bounces)
    return _escalate_with_evidence(tid, summary, verdict)


def decide_fail_action(tid: str, worktree: str, qa_cfg: dict, verdict: dict, mode: str,
                       patch_path: str = "") -> int:
    """The dev⇄QA bounce protocol.

    Called exactly once, only when the QA agent's verdict is `fail` on a
    `required` category (`evidence` never reaches here — it ships and logs).
    `qa_bounces < qa.max_bounces` (default 2) → bounce the SAME task back
    onto the queue with the QA findings folded into its contract (exit 4).
    At the cap → escalate with the QA evidence attached to the ledger (exit
    3; run.sh's rc==3 branch does the actual escalate + notify).

    Any unexpected exception ANYWHERE in this — bounce or escalate branch —
    falls back to a plain escalate (exit 3, WARN). Escalation is the safe
    failure mode here: a bounce that only half-applied (e.g. the backlog
    field write landed but the ledger clear didn't) must never look like a
    clean re-queue, and a task must never silently ship on an error in this
    path either.
    """
    try:
        return _decide_fail_action(tid, worktree, qa_cfg, verdict, mode, patch_path)
    except Exception as e:
        print(f"WARN: decide_fail_action raised for {tid} ({e!r}) — "
              f"escalating instead (safe fallback, bounce or escalate must not half-apply)")
        return ESCALATE


def qa_check(cfg_all: dict, qa_cfg: dict, tid: str, worktree: str, patch_path: str = "") -> int:
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
    return decide_fail_action(tid, worktree, qa_cfg, v, mode, patch_path)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: qa_check.py <task-id> <worktree> [patch-path]", file=sys.stderr)
        return SHIP                      # never block a ship on a wiring mistake
    tid, worktree = argv[0], argv[1]
    patch_path = argv[2] if len(argv) > 2 else ""
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
        return qa_check(cfg_all, qa_cfg, tid, worktree, patch_path)
    return legacy_check(cfg_all, tid, worktree)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
