#!/usr/bin/env python3
"""qa_run.py — `orbit qa <target> --flow <name>`: run ONE taught flow (see
`orbit teach`, Task 20) via the `/orbit-qa-flow` skill (Task 21) and record
its verdict into the flow's own library metadata.

Manual-CLI posture (loud failures) — unlike qa_check.py's loop gate (which
never blocks a ship on a wiring mistake), an operator running `orbit qa`
directly wants to KNOW when something didn't work: a bad flow name, an
agent that never wrote a verdict, or a malformed one, are all reported as
errors here, not silently shipped through. Regression sweeps / a `qa run
--all` trigger are Phase 5; this is a single named flow only.

Reuse, not copy, of the MCP wiring: `qa_check.build_mcp_config()` (Task 16)
is the exact same Playwright-MCP-autogen helper the loop gate uses — this
module imports it (see `_flow_mcp_config` below for why that import has to
be local/lazy rather than a top-of-file `import qa_check`) and calls it with
a flow-scoped output dir instead of qa_check's task-scoped one.

Verdict contract (see skills/orbit-qa-flow.md, Task 21 — THE contract):
    $AP_STATE/qa/runs/<UTC-ts>-<flow>.json
        {"flow": "<name>", "verdict": "pass"|"fail"|"healed"|"unable",
         "failed_step": <int or null>, "screenshot": "<relative or null>",
         "summary": "<one line>", "healed_candidate": "<heal-file name or null>"}
    Heal candidates (only referenced, never written here) live under
    $AP_STATE/qa/heal/<healed_candidate>.

Locating the run's own verdict: the newest `runs/*-<flow>.json` file with an
mtime at or after this invocation's start time — a STALE file left over from
an earlier run (or a differently-configured target) must never be mistaken
for this run's outcome.

Shape validation (a wiring/agent mistake must never look like a real
verdict): `flow` must match the requested name; `verdict` must be one of the
four; `healed` requires a non-null `healed_candidate` AND that heal file
must actually exist under $AP_STATE/qa/heal/; `fail` requires a non-null
`failed_step`. Anything short of that is reported as a malformed verdict
(exit 1) and the flow's metadata is left untouched — only a genuinely valid
verdict of one of the four kinds gets persisted.

Flow metadata update (ALL four verdicts, once shape-validated):
    <flow_dir>/meta.json:  runs += 1, last_run_at = now (UTC iso),
                           last_verdict = <verdict>
    <flows_dir's parent>/FLOWMAP.md: the flow's row's LAST cell (last
                           verdict) is replaced in place; the other three
                           cells (name/intent/entry path) are left untouched.

Exit codes:
    0   pass, or healed (candidate is printed — "pending approval", the
        dashboard approval UI itself is Phase 5; this is a forward pointer)
    3   fail (the SEEN.md pointer is printed — see skills/orbit-qa-flow.md)
    1   unable, no verdict found (incl. only-stale-files-present), a
        malformed verdict, or the agent itself could not be run/finished
    2   usage / validation error (bad slug, no `qa:` block, unknown flow —
        the last one lists the flows that DO exist)

Config (`.autopilot/config.yaml`, see config/schema.yaml's `qa:` block):
    qa.flows_dir (default "qa/flows"), qa.mcp_config, qa.browser,
    qa.headless, qa.model, qa.timeout_seconds, qa.permission_mode (via the
    top-level `permission_mode`, same key qa_check.py reads).

Test hook: ORBIT_QA_RUN_CMD replaces the `claude --print /orbit-qa-flow ...`
call verbatim — same substitution mechanism as ORBIT_QA_CMD/ORBIT_RUNTIME_CMD/
ORBIT_CODEGEN_CMD, its OWN name so a fake can tell this hook apart from
qa_check.py's loop-gate ORBIT_QA_CMD. AP_HOME/AP_STATE/ORBIT_HOME/FLOW_NAME
(and QA_MCP_CONFIG, regardless of override, same reasoning as qa_check.py)
are exported into the child's env either way.

Usage: qa_run.py <target> --flow <name> [--timeout N]
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from backlog_append import SLUG_RE

ENGINE_DIR = Path(__file__).resolve().parent
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or ENGINE_DIR.parent)
DEFAULT_FLOWS_DIR = "qa/flows"
DEFAULT_TIMEOUT = 1200
DEFAULT_MODEL = "sonnet"
VERDICTS = ("pass", "fail", "healed", "unable")

USAGE = "usage: qa_run.py <target> --flow <name> [--timeout N]"

# Exit codes (manual-CLI posture — see module docstring).
OK, ERR, FAIL, USAGE_ERR = 0, 1, 3, 2


def _err(msg: str) -> None:
    print(f"qa_run: {msg}", file=sys.stderr)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_cfg(target: Path) -> dict:
    cfg_path = target / ".autopilot" / "config.yaml"
    try:
        return yaml.safe_load(cfg_path.read_text()) or {}
    except OSError:
        return {}


# ---------------------------------------------------------------------------
# argv
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str]):
    """Returns (target, flow_name, timeout_arg, error) — error is a
    (message, exit_code) pair, or None on success."""
    if not argv:
        return None, None, None, (USAGE, USAGE_ERR)
    target = Path(argv[0]).resolve()
    rest = argv[1:]
    flow_name = None
    timeout_arg = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("--flow", "--timeout"):
            if i + 1 >= len(rest):
                return None, None, None, (f"missing value for {tok} ({USAGE})", USAGE_ERR)
            if tok == "--flow":
                flow_name = rest[i + 1]
            else:
                timeout_arg = rest[i + 1]
            i += 2
        else:
            return None, None, None, (f"unrecognized argument {tok!r} ({USAGE})", USAGE_ERR)
    if not flow_name:
        return None, None, None, (f"--flow <name> is required ({USAGE})", USAGE_ERR)
    if timeout_arg is not None:
        try:
            timeout_arg = int(timeout_arg)
        except ValueError:
            return None, None, None, (f"--timeout must be an integer ({USAGE})", USAGE_ERR)
    return target, flow_name, timeout_arg, None


# ---------------------------------------------------------------------------
# MCP config — reuse, not copy, of qa_check.py's autogen (Task 16).
# ---------------------------------------------------------------------------

def _flow_mcp_config(qa_cfg: dict, ap_state: Path, flow_name: str) -> Path:
    # Local import: qa_check.py resolves its own AP_STATE/AP_HOME module
    # globals from the environment AT IMPORT TIME (see its `_ap_state()`).
    # This module doesn't know its target (and thus AP_HOME/AP_STATE) until
    # main() parses argv, so os.environ["AP_HOME"]/["AP_STATE"] must already
    # be set to THIS run's target before this import executes — a top-of-
    # file `import qa_check` would run too early and could pick up a stale
    # or entirely absent AP_HOME/AP_STATE. build_mcp_config() itself takes
    # explicit paths and never touches qa_check's own globals either way.
    import qa_check
    output_dir = ap_state / "qa" / f"flow-{flow_name}"
    return qa_check.build_mcp_config(qa_cfg, ap_state / "qa", output_dir)


# ---------------------------------------------------------------------------
# invoking the flow-replay agent
# ---------------------------------------------------------------------------

def _run_flow_agent(qa_cfg: dict, cfg_all: dict, flow_name: str, target: Path,
                     mcp_path: Path, timeout: int, ap_home: Path, ap_state: Path) -> int | None:
    """Returns None on a clean launch/return (proceed to look for a verdict),
    or an exit code to return immediately (claude missing / launch error —
    both bucket into exit 1, "agent error", per the exit-code table)."""
    override = os.environ.get("ORBIT_QA_RUN_CMD")
    if override:
        cmd = shlex.split(override)
    else:
        claude = shutil.which("claude")
        if not claude:
            _err("claude not on PATH — cannot run the QA flow agent")
            return ERR
        cmd = [claude, "--print", f"/orbit-qa-flow {flow_name}",
               "--mcp-config", str(mcp_path),
               "--settings", str(ORBIT_HOME / "config" / "orbit.qa.settings.json"),
               "--permission-mode", cfg_all.get("permission_mode", "acceptEdits"),
               "--model", qa_cfg.get("model", DEFAULT_MODEL),
               "--add-dir", str(ORBIT_HOME)]
    # QA_MCP_CONFIG is set regardless of override, same reasoning as
    # qa_check.py's run_qa_agent: an ORBIT_QA_RUN_CMD override replaces argv
    # wholesale, so this is the only argv-independent way a fake script (or
    # a real skill invocation) can see the resolved MCP config path.
    env = dict(os.environ, AP_HOME=str(ap_home), AP_STATE=str(ap_state),
               ORBIT_HOME=str(ORBIT_HOME), FLOW_NAME=flow_name, QA_MCP_CONFIG=str(mcp_path))
    try:
        subprocess.run(cmd, cwd=str(target), env=env, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        _err(f"QA flow agent for {flow_name!r} failed: {e}")
        return ERR
    return None


# ---------------------------------------------------------------------------
# locating + validating the run verdict
# ---------------------------------------------------------------------------

def _locate_verdict(ap_state: Path, flow_name: str, start: float) -> Path | None:
    """Newest $AP_STATE/qa/runs/*-<flow>.json with mtime >= start — a stale
    file from a PRIOR run of this (or another) flow must never be mistaken
    for this run's own outcome. None if nothing fresh is found.

    The glob is a filename SUFFIX match (`*-<flow>.json`) which a sibling
    flow name can accidentally satisfy too — flow "app" suffix-matches
    "...-my-app.json" just as much as "...-app.json". The filename alone is
    never trusted for identity: each mtime-fresh candidate is parsed and
    only kept if its own JSON `flow` field equals the requested flow."""
    runs_dir = ap_state / "qa" / "runs"
    if not runs_dir.is_dir():
        return None
    best: tuple[float, Path] | None = None
    for p in runs_dir.glob(f"*-{flow_name}.json"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < start:
            continue
        try:
            v = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(v, dict) or v.get("flow") != flow_name:
            continue          # filename-suffix false match — not actually this flow's verdict
        if best is None or mtime > best[0]:
            best = (mtime, p)
    return best[1] if best else None


def _validate_verdict(v, flow_name: str, ap_state: Path) -> str | None:
    """None = shape is valid. Else a human-readable reason it's malformed."""
    if not isinstance(v, dict):
        return f"verdict json is a {type(v).__name__}, not an object"
    if v.get("flow") != flow_name:
        return f"verdict's flow {v.get('flow')!r} does not match requested flow {flow_name!r}"
    verdict = v.get("verdict")
    if verdict not in VERDICTS:
        return f"verdict {verdict!r} is not one of {VERDICTS}"
    if verdict == "healed":
        candidate = v.get("healed_candidate")
        if not candidate:
            return "verdict is 'healed' but healed_candidate is missing"
        # Path-containment discipline, mirrored from qa_check.py's
        # _unsafe_qa_path_reason (same threat class: an agent-written field
        # naming a path must never be trusted to stay inside the directory
        # it's supposed to point into) — reused via a local import for the
        # same reason _flow_mcp_config's import of qa_check is local/lazy.
        import qa_check
        heal_dir = ap_state / "qa" / "heal"
        reason = qa_check._unsafe_qa_path_reason(heal_dir, candidate)
        if reason:
            return f"verdict names heal candidate {candidate!r} which is unsafe ({reason})"
        if not (heal_dir / candidate).is_file():
            return (f"verdict names heal candidate {candidate!r} but no such file exists "
                     f"under {heal_dir}")
    if verdict == "fail":
        if v.get("failed_step") is None:
            return "verdict is 'fail' but failed_step is missing"
        if not v.get("screenshot"):
            return "verdict is 'fail' but screenshot is missing"
    if verdict == "unable" and v.get("failed_step") is not None:
        return "verdict is 'unable' but failed_step is set (environment trouble never pairs with a step)"
    return None


# ---------------------------------------------------------------------------
# flow metadata + FLOWMAP updates — mirrors qa_teach.py's own discipline
# ---------------------------------------------------------------------------

def _update_meta(flow_dir: Path, verdict: str) -> None:
    meta_path = flow_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text())
        if not isinstance(meta, dict):
            raise ValueError
    except (OSError, ValueError):
        meta = {"created_at": None, "last_run_at": None, "last_verdict": None,
                 "runs": 0, "heals": []}
    try:
        runs = int(meta.get("runs") or 0)
    except (TypeError, ValueError):
        runs = 0
    meta["runs"] = runs + 1
    meta["last_run_at"] = _now_iso()
    meta["last_verdict"] = verdict
    try:
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    except OSError as e:
        print(f"WARN: could not update {meta_path} ({e})")


def _update_flowmap_verdict(flowmap_path: Path, name: str, verdict: str) -> None:
    """Replace ONLY the last cell (last verdict) of the flow's own FLOWMAP
    row, callable-sub style (mirrors qa_teach._update_flowmap's regex-match-
    and-replace-in-place discipline) — the other three cells (name/intent/
    entry path) are left byte-for-byte untouched. A missing FLOWMAP.md or a
    row that isn't there (e.g. a hand-built flow dir) is non-fatal — this is
    a secondary, regenerable index, same principle as qa_teach.py's own
    FLOWMAP-write-failure handling."""
    if not flowmap_path.exists():
        print(f"WARN: no FLOWMAP.md at {flowmap_path} — last-verdict cell not updated")
        return
    text = flowmap_path.read_text()
    pat = re.compile(rf"^(\|\s*{re.escape(name)}\s*\|.*\|)\s*[^|\n]*\|[ \t]*$\n?", re.MULTILINE)
    m = pat.search(text)
    if not m:
        print(f"WARN: no FLOWMAP.md row for {name!r} — last-verdict cell not updated")
        return
    new_row = f"{m.group(1)} {verdict} |\n"
    text = text[:m.start()] + new_row + text[m.end():]
    try:
        flowmap_path.write_text(text)
    except OSError as e:
        print(f"WARN: could not update FLOWMAP.md for {name!r} ({e})")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    target, flow_name, timeout_arg, error = _parse_args(argv)
    if error:
        msg, code = error
        _err(msg)
        return code

    if not SLUG_RE.match(flow_name):
        _err(f"{flow_name!r} is not a valid flow name — use a lowercase slug "
             f"(letters/digits/./_/-, starting with a letter or digit)")
        return USAGE_ERR

    ap_home = target / ".autopilot"
    cfg_all = _load_cfg(target)
    qa_cfg = cfg_all.get("qa")
    if not isinstance(qa_cfg, dict):
        _err(f"no 'qa:' block configured in {ap_home / 'config.yaml'} — "
             f"see config/schema.yaml's qa: section and add one first")
        return USAGE_ERR

    flows_dir = qa_cfg.get("flows_dir") or DEFAULT_FLOWS_DIR
    flows_root = ap_home / flows_dir
    flow_dir = flows_root / flow_name
    if not flow_dir.is_dir():
        names = sorted(p.name for p in flows_root.iterdir() if p.is_dir()) \
            if flows_root.is_dir() else []
        avail = ", ".join(names) if names else "(none taught yet — try `orbit teach` first)"
        _err(f"no flow named {flow_name!r} at {flow_dir} — available flows: {avail}")
        return USAGE_ERR

    ap_state = ap_home / "state"
    # notify/qa_check-style precedent (see engine/digest.py): a standalone
    # CLI tool that imports env-resolved engine modules exports AP_HOME/
    # AP_STATE from its own argv-derived target first. setdefault, not a
    # plain assignment — never clobber a caller's own explicit env.
    os.environ.setdefault("AP_HOME", str(ap_home))
    os.environ.setdefault("AP_STATE", str(ap_state))
    os.environ.setdefault("ORBIT_HOME", str(ORBIT_HOME))

    timeout = timeout_arg if timeout_arg is not None else \
        int(qa_cfg.get("timeout_seconds") or DEFAULT_TIMEOUT)

    mcp_path = _flow_mcp_config(qa_cfg, ap_state, flow_name)

    start = time.time()
    launch_rc = _run_flow_agent(qa_cfg, cfg_all, flow_name, target, mcp_path,
                                 timeout, ap_home, ap_state)
    if launch_rc is not None:
        return launch_rc

    verdict_path = _locate_verdict(ap_state, flow_name, start)
    if verdict_path is None:
        _err(f"no run verdict found for flow {flow_name!r} under "
             f"{ap_state / 'qa' / 'runs'} written since this run started — "
             f"the QA flow agent never wrote one")
        return ERR

    try:
        v = json.loads(verdict_path.read_text())
    except (OSError, ValueError) as e:
        _err(f"could not read the run verdict at {verdict_path} ({e})")
        return ERR

    reason = _validate_verdict(v, flow_name, ap_state)
    if reason:
        _err(f"malformed run verdict at {verdict_path}: {reason}")
        return ERR

    verdict = v["verdict"]
    summary = (v.get("summary") or "").strip()

    _update_meta(flow_dir, verdict)
    flowmap_path = flows_root.parent / "FLOWMAP.md"
    _update_flowmap_verdict(flowmap_path, flow_name, verdict)

    if verdict == "pass":
        print(f"{flow_name}: verdict=pass — {summary or 'flow replayed cleanly'}")
        return OK
    if verdict == "healed":
        candidate = v.get("healed_candidate")
        print(f"{flow_name}: verdict=healed — heal candidate {candidate!r} is pending "
              f"approval (approve it on the dashboard) — {summary or 'UI drifted, intent held'}")
        return OK
    if verdict == "fail":
        print(f"{flow_name}: verdict=fail (step {v.get('failed_step')}) — "
              f"{summary or 'intent not achieved'} — see ops/qa/SEEN.md for the recorded bug")
        return FAIL
    # unable
    print(f"{flow_name}: verdict=unable — {summary or 'the environment prevented the attempt'}")
    return ERR


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
