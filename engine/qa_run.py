#!/usr/bin/env python3
"""qa_run.py — `orbit qa <target> --flow <name>`: run ONE taught flow (see
`orbit teach`, Task 20) via the `/orbit-qa-flow` skill (Task 21) and record
its verdict into the flow's own library metadata. Also `--regression
[--if-changed]` (Task 27): a cursor-triggered sweep of every APPROVED flow
(taught AND replay-proven — see "Regression sweeps" below), sequentially,
through the exact same per-flow path as `--flow`.

Manual-CLI posture (loud failures) — unlike qa_check.py's loop gate (which
never blocks a ship on a wiring mistake), an operator running `orbit qa`
directly wants to KNOW when something didn't work: a bad flow name, an
agent that never wrote a verdict, or a malformed one, are all reported as
errors here, not silently shipped through. `--regression` WITHOUT
`--if-changed` (a human dashboard click — see command_center.py's
do_qa_action "run-regression") keeps this same loud posture. `--regression
--if-changed` (the run.sh loop hook) is the one exception: loop safety over
completeness, same contract as knowledge.py's autorefresh — see "Regression
sweeps" below for exactly where that posture changes and why.

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

The above (locate → validate → persist) is now factored into ONE callable,
`_replay_flow()`, so `--flow` and `--regression` share it byte-for-byte —
see its own docstring.

Regression sweeps (Task 27, `--regression [--if-changed]`):
    "Approved" flow = a SLUG_RE-valid directory directly under
    qa.flows_dir that has a `steps.json` (written by the replay skill only
    after a first fully-clean pass — see skills/orbit-qa-flow.md and
    qa_teach.py's own docstring) — i.e. replay-proven at least once, not
    merely taught. Sorted by name; capped at `qa.regression.max_flows_per_run`
    (default 10) — a cap NEVER silently drops flows: the dropped names are
    always printed.

    `--if-changed` gates the sweep on a cursor,
    $AP_STATE/qa/.regression-cursor ({"base_sha", "markers_line_count",
    "swept_at"}): due if EITHER `qa.regression.schedule_hours` has elapsed
    since the cursor's `swept_at` (0 = cadence permanently off), OR
    `qa.regression.on_merge` is true AND either `git -C <repo> rev-parse
    --verify origin/<base_branch>` (hex-validated) differs from the cursor's
    `base_sha`, or `$AP_STATE/merge_markers.jsonl`'s line count differs from
    the cursor's `markers_line_count` (`on_merge: false` disables BOTH of
    these merge-detection signals — cadence alone may still fire). Not due
    → exit 0 immediately, before the lock and before the agent is ever
    invoked. Due → sweep, then (only once the sweep actually completed —
    never on a lock-skip, never on a crash) the cursor is rewritten from
    freshly-read values.

    Without `--if-changed` (a human "sweep now" click), the cursor is never
    consulted or written — every invocation just sweeps.

    Locking: $AP_STATE/qa/.qa-running (json: pid + started_at), created
    exclusively. A fresh lock (age < qa.timeout_seconds) → exit 0, "already
    running", agent never invoked. A stale one is replaced (WARN printed).
    Always released in a finally — including when a flow's own verdict is
    "fail", and even if the sweep itself raises.

    Pruning: after every completed sweep (even a zero-flow one), the newest
    200 files directly under $AP_STATE/qa/runs/ are kept, older ones deleted
    (count pruned is printed) — restricted to files that function's own
    `iterdir()` lists, so it can never reach outside qa/runs/.

    Summary: one printed line per flow (`_replay_flow`'s own outcome — an
    infra "error" outcome from that callable folds into the "unable" tally,
    exactly like --flow's own exit-1 bucket) plus one aggregate line ("N
    pass / N healed / N fail / N unable"). Exit 0 if no flow failed, 3 if any
    did, 1 only on a sweep-level infrastructure crash (a lock conflict is
    NOT this — it's exit 0, per above).

    Loop-safety ordering exception (see run.sh's one_iteration, right after
    the knowledge autorefresh line): a target with NO `qa:` block at all is
    the common case (QA is opt-in), not a misconfiguration — for
    `--regression --if-changed` ONLY, main() checks for a missing `qa:`
    block BEFORE any other validation and returns 0 silently (no printed
    line at all) so a target that never opted into QA never accumulates a
    WARN in orbit.log every single cycle. `--regression` without
    `--if-changed` (the human click) and `--flow` both keep the loud exit-2
    posture for the exact same missing-block condition — see main()'s
    ordering.

Exit codes (--flow):
    0   pass, or healed (candidate is printed — "pending approval", the
        dashboard approval UI itself is Phase 5; this is a forward pointer)
    3   fail (the SEEN.md pointer is printed — see skills/orbit-qa-flow.md)
    1   unable, no verdict found (incl. only-stale-files-present), a
        malformed verdict, or the agent itself could not be run/finished
    2   usage / validation error (bad slug, no `qa:` block, a malformed
        `qa.flows_dir` — same `_bad_flows_dir_reason` shape check `orbit
        teach` enforces, see qa_teach.py — or an unknown flow: the last one
        lists the flows that DO exist)

Exit codes (--regression): see "Regression sweeps" above — 0 no fails / lock
busy / not due, 3 any fail, 1 infra crash, 2 usage (bad flows_dir, or no
`qa:` block on a non-`--if-changed` invocation).

Config (`.autopilot/config.yaml`, see config/schema.yaml's `qa:` block):
    qa.flows_dir (default "qa/flows"), qa.mcp_config, qa.browser,
    qa.headless, qa.model, qa.timeout_seconds, qa.permission_mode (via the
    top-level `permission_mode`, same key qa_check.py reads), qa.regression
    (on_merge/schedule_hours/max_flows_per_run — Task 25, see above).

Test hook: ORBIT_QA_RUN_CMD replaces the `claude --print /orbit-qa-flow ...`
call verbatim — same substitution mechanism as ORBIT_QA_CMD/ORBIT_RUNTIME_CMD/
ORBIT_CODEGEN_CMD, its OWN name so a fake can tell this hook apart from
qa_check.py's loop-gate ORBIT_QA_CMD. AP_HOME/AP_STATE/ORBIT_HOME/FLOW_NAME
(and QA_MCP_CONFIG, regardless of override, same reasoning as qa_check.py)
are exported into the child's env either way.

Usage: qa_run.py <target> --flow <name> [--timeout N]
       qa_run.py <target> --regression [--if-changed] [--timeout N]
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
from qa_teach import _bad_flows_dir_reason

ENGINE_DIR = Path(__file__).resolve().parent
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or ENGINE_DIR.parent)
DEFAULT_FLOWS_DIR = "qa/flows"
DEFAULT_TIMEOUT = 1200
DEFAULT_MODEL = "sonnet"
VERDICTS = ("pass", "fail", "healed", "unable")

# --- regression sweeps (Task 27) ---
DEFAULT_MAX_FLOWS_PER_RUN = 10
DEFAULT_SCHEDULE_HOURS = 24
REGRESSION_CURSOR_NAME = ".regression-cursor"
LOCK_NAME = ".qa-running"
MAX_RUN_FILES = 200
GIT_TIMEOUT = 30
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)

USAGE = ("usage: qa_run.py <target> --flow <name> [--timeout N]\n"
         "       qa_run.py <target> --regression [--if-changed] [--timeout N]")

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
    """Returns (target, mode, flow_name, if_changed, timeout_arg, error) —
    mode is "flow" or "regression"; error is a (message, exit_code) pair, or
    None on success."""
    if not argv:
        return None, None, None, False, None, (USAGE, USAGE_ERR)
    target = Path(argv[0]).resolve()
    rest = argv[1:]
    mode = None
    flow_name = None
    if_changed = False
    timeout_arg = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--flow":
            if mode == "regression":
                return None, None, None, False, None, \
                    (f"--flow and --regression are mutually exclusive ({USAGE})", USAGE_ERR)
            if i + 1 >= len(rest):
                return None, None, None, False, None, (f"missing value for --flow ({USAGE})", USAGE_ERR)
            mode = "flow"
            flow_name = rest[i + 1]
            i += 2
        elif tok == "--regression":
            if mode == "flow":
                return None, None, None, False, None, \
                    (f"--flow and --regression are mutually exclusive ({USAGE})", USAGE_ERR)
            mode = "regression"
            i += 1
        elif tok == "--if-changed":
            if_changed = True
            i += 1
        elif tok == "--timeout":
            if i + 1 >= len(rest):
                return None, None, None, False, None, (f"missing value for --timeout ({USAGE})", USAGE_ERR)
            timeout_arg = rest[i + 1]
            i += 2
        else:
            return None, None, None, False, None, (f"unrecognized argument {tok!r} ({USAGE})", USAGE_ERR)
    if mode is None:
        return None, None, None, False, None, \
            (f"either --flow <name> or --regression is required ({USAGE})", USAGE_ERR)
    if mode == "flow" and if_changed:
        return None, None, None, False, None, (f"--if-changed only applies to --regression ({USAGE})", USAGE_ERR)
    if timeout_arg is not None:
        try:
            timeout_arg = int(timeout_arg)
        except ValueError:
            return None, None, None, False, None, (f"--timeout must be an integer ({USAGE})", USAGE_ERR)
    return target, mode, flow_name, if_changed, timeout_arg, None


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
    return qa_check.build_mcp_config(qa_cfg, ap_state / "qa", output_dir,
                                      f"mcp-run-flow-{flow_name}.json")


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
# the shared per-flow path — used by BOTH --flow and --regression (Task 27:
# "invoke each flow through the SAME per-flow code path... factor a
# callable, don't duplicate").
# ---------------------------------------------------------------------------

def _replay_flow(qa_cfg: dict, cfg_all: dict, flow_name: str, flow_dir: Path, target: Path,
                  ap_home: Path, ap_state: Path, timeout: int) -> dict:
    """Builds the flow's own MCP config, invokes the QA flow agent, locates +
    shape-validates the verdict it wrote, and — for any of the four valid
    verdicts — persists it into the flow's meta.json + FLOWMAP.md row (see
    _update_meta/_update_flowmap_verdict above). This is the ONE place that
    logic lives; --flow's main() and --regression's sweep both call this and
    differ only in how they print/aggregate/exit.

    Returns {"kind": "pass"|"fail"|"healed"|"unable"|"error",
             "verdict": <parsed+validated verdict dict, or None>,
             "error": <human message, set only when kind == "error">}.
    kind == "error" is every infra outcome that isn't itself one of the four
    valid verdicts — the agent never launched, no verdict file appeared in
    time, the file couldn't be read, or its shape didn't validate — exactly
    the same bucket --flow's own exit-1 path has always covered.
    """
    mcp_path = _flow_mcp_config(qa_cfg, ap_state, flow_name)
    start = time.time()
    launch_rc = _run_flow_agent(qa_cfg, cfg_all, flow_name, target, mcp_path,
                                 timeout, ap_home, ap_state)
    if launch_rc is not None:
        return {"kind": "error", "verdict": None,
                "error": f"QA flow agent for {flow_name!r} could not be launched or finished"}

    verdict_path = _locate_verdict(ap_state, flow_name, start)
    if verdict_path is None:
        return {"kind": "error", "verdict": None,
                "error": f"no run verdict found for flow {flow_name!r} under "
                         f"{ap_state / 'qa' / 'runs'} written since this run started — "
                         f"the QA flow agent never wrote one"}

    try:
        v = json.loads(verdict_path.read_text())
    except (OSError, ValueError) as e:
        return {"kind": "error", "verdict": None,
                "error": f"could not read the run verdict at {verdict_path} ({e})"}

    reason = _validate_verdict(v, flow_name, ap_state)
    if reason:
        return {"kind": "error", "verdict": None,
                "error": f"malformed run verdict at {verdict_path}: {reason}"}

    verdict = v["verdict"]
    _update_meta(flow_dir, verdict)
    flowmap_path = flow_dir.parent.parent / "FLOWMAP.md"
    _update_flowmap_verdict(flowmap_path, flow_name, verdict)
    return {"kind": verdict, "verdict": v, "error": None}


# ---------------------------------------------------------------------------
# regression sweeps (Task 27) — selection, cursor, lock, pruning.
# ---------------------------------------------------------------------------

def _resolve_repo(target: Path, cfg: dict) -> Path:
    repo_rel = cfg.get("repo") or "."
    return (target / repo_rel).resolve() if not os.path.isabs(repo_rel) else Path(repo_rel)


def _current_base_sha(repo: Path, base_branch: str) -> str | None:
    """`git -C <repo> rev-parse --verify origin/<base_branch>`, hex-validated.
    None on ANY failure (not a git repo, no such ref, git missing, timeout,
    non-hex output) — the caller treats that as "unknown", never as a false
    changed/unchanged signal. `--verify` forces a single clean revision or a
    clean failure — a bare `rev-parse` instead ECHOES an unrecognized-
    looking token (e.g. an injected `--end-of-options`) back as an extra
    output line rather than erroring, which would corrupt a single-value
    capture. `--end-of-options` still guards against `base_branch` itself
    looking like a flag (e.g. a hand-edited config's base_branch starting
    with '-')."""
    if not isinstance(base_branch, str) or not base_branch:
        return None
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options",
             f"origin/{base_branch}"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    sha = p.stdout.strip()
    return sha if _SHA_RE.match(sha) else None


def _markers_line_count(ap_state: Path) -> int:
    path = ap_state / "merge_markers.jsonl"
    try:
        text = path.read_text()
    except OSError:
        return 0
    return sum(1 for ln in text.splitlines() if ln.strip())


def _parse_iso(s) -> "datetime | None":
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_cursor(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _write_cursor(path: Path, base_sha, markers_line_count: int, swept_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"base_sha": base_sha, "markers_line_count": markers_line_count,
                                 "swept_at": swept_at}, indent=2) + "\n")


def _regression_due(repo: Path, base_branch: str, ap_state: Path, cursor: dict | None,
                     on_merge: bool, schedule_hours: int) -> bool:
    """True if an --if-changed sweep should run now: EITHER the cadence timer
    elapsed (schedule_hours > 0 and the cursor's swept_at is missing,
    unparseable, or old enough) OR (on_merge and (the trunk sha moved OR
    merge_markers.jsonl's line count differs) versus the cursor).
    schedule_hours == 0 permanently disables the cadence signal; on_merge ==
    False permanently disables BOTH merge-detection signals — with both off,
    --if-changed never sweeps on its own (a human can still always force one
    via --regression with no --if-changed)."""
    now = time.time()
    cadence_due = False
    if schedule_hours > 0:
        swept_dt = _parse_iso((cursor or {}).get("swept_at"))
        cadence_due = swept_dt is None or (now - swept_dt.timestamp()) >= schedule_hours * 3600

    merge_due = False
    if on_merge:
        base_sha = _current_base_sha(repo, base_branch)
        markers_count = _markers_line_count(ap_state)
        prev_sha = (cursor or {}).get("base_sha")
        prev_count = (cursor or {}).get("markers_line_count")
        if base_sha is not None and base_sha != prev_sha:
            merge_due = True
        if markers_count != prev_count:
            merge_due = True

    return cadence_due or merge_due


def _acquire_lock(lock_path: Path, stale_after_seconds: int) -> bool:
    """True = lock acquired (this call now owns it — the caller MUST release
    it in a finally). False = a fresh lock already exists elsewhere; the
    caller must NOT proceed to sweep. A lock older than stale_after_seconds
    is presumed dead (a crashed prior sweep never reached its own finally)
    and is replaced, with a WARN — never left to wedge every future sweep
    forever."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "started_at": _now_iso()}) + "\n"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        return True
    except FileExistsError:
        pass
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        # Gone between the EEXIST and this stat() (raced with another
        # release) — treat as stale, safe to (re)create.
        age = stale_after_seconds
    if age < stale_after_seconds:
        return False
    print(f"WARN: stale regression lock at {lock_path} (age {int(age)}s >= "
          f"{stale_after_seconds}s) — replacing")
    try:
        lock_path.write_text(payload)
    except OSError:
        return False
    return True


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass


def _select_regression_flows(flows_root: Path, max_flows: int) -> tuple[list[str], list[str]]:
    """Approved flows: SLUG_RE-valid directories directly under flows_root
    that have a steps.json — replay-proven at least once (see
    qa_teach.py/skills/orbit-qa-flow.md: steps.json is written only after a
    first fully-clean replay). Sorted by name for a deterministic, testable
    cap: the first `max_flows` are swept, the rest are dropped — never
    silently, callers print the dropped names. Returns (selected, dropped)."""
    if not flows_root.is_dir():
        return [], []
    names = sorted(p.name for p in flows_root.iterdir() if p.is_dir() and SLUG_RE.match(p.name))
    approved = [n for n in names if (flows_root / n / "steps.json").is_file()]
    if len(approved) > max_flows:
        return approved[:max_flows], approved[max_flows:]
    return approved, []


def _prune_runs(runs_dir: Path, keep: int = MAX_RUN_FILES) -> int:
    """Keep the newest `keep` files directly under runs_dir (oldest-mtime
    deleted first). Restricted to files THIS function's own `iterdir()`
    lists — no recursion, no path built from any untrusted field — so it can
    never touch anything outside qa/runs/."""
    if not runs_dir.is_dir():
        return 0
    try:
        files = [p for p in runs_dir.iterdir() if p.is_file()]
    except OSError:
        return 0
    if len(files) <= keep:
        return 0
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    files.sort(key=_mtime)
    doomed = files[: len(files) - keep]
    pruned = 0
    for p in doomed:
        try:
            p.unlink()
            pruned += 1
        except OSError:
            pass
    return pruned


def _format_flow_line(flow_name: str, result: dict) -> str:
    """One printed line for a single flow's outcome in a regression sweep —
    same information as --flow's own messages, phrased for a multi-flow
    summary rather than a single-flow exit."""
    if result["kind"] == "error":
        return f"{flow_name}: verdict=unable — {result['error']}"
    verdict = result["kind"]
    v = result["verdict"]
    summary = (v.get("summary") or "").strip()
    if verdict == "healed":
        candidate = v.get("healed_candidate")
        return (f"{flow_name}: verdict=healed — heal candidate {candidate!r} pending "
                f"approval — {summary or 'UI drifted, intent held'}")
    if verdict == "fail":
        return (f"{flow_name}: verdict=fail (step {v.get('failed_step')}) — "
                f"{summary or 'intent not achieved'}")
    if verdict == "unable":
        return f"{flow_name}: verdict=unable — {summary or 'the environment prevented the attempt'}"
    return f"{flow_name}: verdict=pass — {summary or 'flow replayed cleanly'}"


def _sweep(target: Path, ap_home: Path, ap_state: Path, cfg_all: dict, qa_cfg: dict,
           flows_root: Path, max_flows: int, timeout_arg: int | None, default_timeout: int) -> int:
    """Runs the selected approved flows sequentially through _replay_flow,
    prints per-flow + summary lines, prunes qa/runs/, and returns the
    aggregate exit code (0 no fails, 3 any fail — never 1: a crash here is
    the CALLER's infra-error bucket, see _run_regression)."""
    selected, dropped = _select_regression_flows(flows_root, max_flows)
    if dropped:
        print(f"regression: {len(selected) + len(dropped)} approved flow(s) found, cap is "
              f"{max_flows} — dropped {len(dropped)}: {', '.join(dropped)}")

    if not selected:
        print("regression: no approved flows (with steps.json) to sweep")
        pruned = _prune_runs(ap_state / "qa" / "runs")
        if pruned:
            print(f"regression: pruned {pruned} old run file(s) from qa/runs/ (keeping newest "
                  f"{MAX_RUN_FILES})")
        return OK

    timeout = timeout_arg if timeout_arg is not None else default_timeout
    counts = {"pass": 0, "healed": 0, "fail": 0, "unable": 0}
    for flow_name in selected:
        flow_dir = flows_root / flow_name
        result = _replay_flow(qa_cfg, cfg_all, flow_name, flow_dir, target, ap_home, ap_state, timeout)
        tally_kind = result["kind"] if result["kind"] != "error" else "unable"
        counts[tally_kind] += 1
        print(_format_flow_line(flow_name, result))

    total = sum(counts.values())
    print(f"regression: {total} flow(s) swept — {counts['pass']} pass / {counts['healed']} healed / "
          f"{counts['fail']} fail / {counts['unable']} unable")

    pruned = _prune_runs(ap_state / "qa" / "runs")
    if pruned:
        print(f"regression: pruned {pruned} old run file(s) from qa/runs/ (keeping newest "
              f"{MAX_RUN_FILES})")

    return FAIL if counts["fail"] > 0 else OK


def _run_regression(target: Path, ap_home: Path, cfg_all: dict, qa_cfg: dict,
                     if_changed: bool, timeout_arg: int | None) -> int:
    ap_state = ap_home / "state"
    os.environ.setdefault("AP_HOME", str(ap_home))
    os.environ.setdefault("AP_STATE", str(ap_state))
    os.environ.setdefault("ORBIT_HOME", str(ORBIT_HOME))

    flows_dir = qa_cfg.get("flows_dir") or DEFAULT_FLOWS_DIR
    bad = _bad_flows_dir_reason(flows_dir)
    if bad:
        _err(f"qa.flows_dir {flows_dir!r} is invalid ({bad})")
        return USAGE_ERR
    flows_root = ap_home / flows_dir

    reg_cfg = qa_cfg.get("regression")
    reg_cfg = reg_cfg if isinstance(reg_cfg, dict) else {}
    on_merge = reg_cfg.get("on_merge")
    if not isinstance(on_merge, bool):
        on_merge = True
    try:
        schedule_hours = int(reg_cfg.get("schedule_hours", DEFAULT_SCHEDULE_HOURS))
    except (TypeError, ValueError):
        schedule_hours = DEFAULT_SCHEDULE_HOURS
    try:
        max_flows = int(reg_cfg.get("max_flows_per_run", DEFAULT_MAX_FLOWS_PER_RUN))
    except (TypeError, ValueError):
        max_flows = DEFAULT_MAX_FLOWS_PER_RUN
    if max_flows <= 0:
        max_flows = DEFAULT_MAX_FLOWS_PER_RUN

    qa_dir = ap_state / "qa"
    cursor_path = qa_dir / REGRESSION_CURSOR_NAME
    repo = _resolve_repo(target, cfg_all)
    base_branch = cfg_all.get("base_branch") or "main"

    if if_changed:
        cursor = _read_cursor(cursor_path)
        if not _regression_due(repo, base_branch, ap_state, cursor, on_merge, schedule_hours):
            return OK
        print("regression: change detected (or cadence due) — sweeping")

    timeout_seconds = int(qa_cfg.get("timeout_seconds") or DEFAULT_TIMEOUT)
    lock_path = qa_dir / LOCK_NAME
    if not _acquire_lock(lock_path, timeout_seconds):
        print("regression: a sweep is already running — skipping this pass")
        return OK

    swept_ok = False
    try:
        rc = _sweep(target, ap_home, ap_state, cfg_all, qa_cfg, flows_root, max_flows,
                    timeout_arg, timeout_seconds)
        swept_ok = True
    except Exception as e:                       # noqa: BLE001 — loop safety: never let a
        # sweep-level crash propagate past this module — see the "infra"
        # exit-1 bucket in the module docstring.
        print(f"WARN: regression sweep crashed ({e})")
        rc = ERR
    finally:
        _release_lock(lock_path)

    if if_changed and swept_ok:
        base_sha = _current_base_sha(repo, base_branch)
        markers_count = _markers_line_count(ap_state)
        _write_cursor(cursor_path, base_sha, markers_count, _now_iso())

    return rc


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    target, mode, flow_name, if_changed, timeout_arg, error = _parse_args(argv)
    if error:
        msg, code = error
        _err(msg)
        return code

    ap_home = target / ".autopilot"
    cfg_all = _load_cfg(target)
    qa_cfg = cfg_all.get("qa")

    if mode == "regression":
        if if_changed and not isinstance(qa_cfg, dict):
            # Loop-safety ordering exception, THIS mode only — see the module
            # docstring's "Regression sweeps" section. A target with no
            # `qa:` block at all is the common (opted-out) case, not a
            # misconfiguration; the run.sh hook must never accumulate a WARN
            # for it every single cycle.
            return OK
        if not isinstance(qa_cfg, dict):
            _err(f"no 'qa:' block configured in {ap_home / 'config.yaml'} — "
                 f"see config/schema.yaml's qa: section and add one first")
            return USAGE_ERR
        return _run_regression(target, ap_home, cfg_all, qa_cfg, if_changed, timeout_arg)

    # --- --flow path (manual-CLI posture, unchanged) ---
    if not SLUG_RE.match(flow_name):
        _err(f"{flow_name!r} is not a valid flow name — use a lowercase slug "
             f"(letters/digits/./_/-, starting with a letter or digit)")
        return USAGE_ERR

    if not isinstance(qa_cfg, dict):
        _err(f"no 'qa:' block configured in {ap_home / 'config.yaml'} — "
             f"see config/schema.yaml's qa: section and add one first")
        return USAGE_ERR

    flows_dir = qa_cfg.get("flows_dir") or DEFAULT_FLOWS_DIR
    bad = _bad_flows_dir_reason(flows_dir)
    if bad:
        _err(f"qa.flows_dir {flows_dir!r} is invalid ({bad})")
        return USAGE_ERR

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

    result = _replay_flow(qa_cfg, cfg_all, flow_name, flow_dir, target, ap_home, ap_state, timeout)
    if result["kind"] == "error":
        _err(result["error"])
        return ERR

    verdict = result["kind"]
    v = result["verdict"]
    summary = (v.get("summary") or "").strip()

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
