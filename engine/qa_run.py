#!/usr/bin/env python3
"""qa_run.py — `orbit qa <target> --flow <name>`: run ONE taught flow (see
`orbit teach`, Task 20) via the `/orbit-qa-flow` skill (Task 21) and record
its verdict into the flow's own library metadata. Also `--regression
[--if-changed]` (Task 27): a cursor-triggered sweep of every APPROVED flow
(taught AND replay-proven — see "Regression sweeps" below), sequentially,
through the exact same per-flow path as `--flow`. Also `--explore` (Task 29):
an opt-in, bounded, same-origin exploratory crawl from `qa.base_url` via the
`/orbit-qa-explore` skill — see "Exploratory crawls" below.

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
    the cursor's `markers_line_count` (`on_merge: false` disables BOTH merge
    signals — the sha check AND the markers-count check — cadence alone may
    still fire). Not due → exit 0 immediately, before the lock and before the
    agent is ever invoked. Due → sweep, then (only once the sweep actually
    completed — never on a lock-skip, never on a crash) the cursor is
    rewritten from freshly-read values.

    Without `--if-changed` (a human "sweep now" click), the cursor is never
    consulted or written — every invocation just sweeps.

    Synchronous, by design, with two bounds on how long that can block the
    caller (run.sh's one_iteration, or a human's dashboard click): a
    SWEEP-level deadline (see below) caps total wall time, and everything
    else about locking exists to keep a second, concurrent invocation from
    piling a duplicate sweep on top while the first is still legitimately
    running.

    Locking: $AP_STATE/qa/.qa-running (json: pid + started_at), created
    exclusively. A fresh lock → exit 0, "already running", agent never
    invoked; a stale one is replaced (WARN printed). "Fresh" is age <
    `qa.timeout_seconds + LOCK_STALE_MARGIN_SECONDS` (60s) — ONE flow's own
    timeout budget plus a margin, deliberately NOT `max_flows x
    timeout_seconds`: a running sweep calls `_touch_lock()` after EVERY flow
    completes (bumping the lock's mtime), so it re-proves its own liveness at
    least once per flow regardless of how many are left — sizing the window
    to the whole sweep's total budget was the earlier (fixed) bug: a
    concurrent invocation would misdiagnose a legitimately still-running
    multi-flow sweep as stale, replace its lock, and double-execute. Release
    is ownership-checked: `_release_lock` only unlinks the lock file if its
    recorded pid is still this process's own — if another process has since
    decided (rightly or not) that the lock looked stale and replaced it, an
    unconditional unlink here would free a lock a DIFFERENT live sweep now
    believes it holds, which is exactly the double-sweep failure mode above.
    A pid mismatch is left in place with a WARN, never force-removed. Release
    is attempted in a finally around the whole sweep either way — including
    when a flow's own verdict is "fail", and even if the sweep itself raises.

    The SECOND, separate lock (`--regression`, `--explore` and `--generate`
    all take it, see `_services_lock`): $AP_STATE/qa/.services.lock, THE
    workspace qa.services mutex, shared with the loop's own QA gate
    (qa_check.py) and the dashboard's persistent Dev session. `.qa-running`
    keeps two QA RUNS from overlapping; this one keeps two qa.services
    CONSUMERS from fighting over the app's fixed local ports, and most of
    those consumers are not QA runs. A busy mutex skips the run with a WARN
    (exit 0, agent never invoked, cursor untouched), the same benign posture
    as a busy `.qa-running`.

    Sweep deadline: `_sweep` stops LAUNCHING new flows once its own elapsed
    time exceeds `SWEEP_DEADLINE_MULTIPLIER (2) x qa.timeout_seconds` — a
    flow already in flight still gets its own full per-flow timeout; only
    flows not yet started are ever dropped, and always with a logged line
    (never a silent cap, same discipline as the max_flows_per_run cap
    above). A deadline-truncated sweep is still a COMPLETED sweep — the
    cursor is written for it exactly like any other completed sweep.

    Pruning: after every completed sweep (even a zero-flow or
    deadline-truncated one), the newest 200 files directly under
    $AP_STATE/qa/runs/ are kept, older ones deleted (count pruned is
    printed) — restricted to files that function's own `iterdir()` lists, so
    it can never reach outside qa/runs/.

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
    0   pass, or healed (candidate is printed — "pending approval"; approve or
        reject it from the dashboard's QA view, which writes the corrected
        steps into the flow's own committed steps.json or archives the
        candidate — see command_center.py's do_qa_action heal-approve/
        heal-reject, Task 26)
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

Exploratory crawls (Task 29, `--explore`): opt-in (`qa.exploratory.enabled`,
default false — Task 25) bounded, same-origin browser walk from
`qa.base_url` via `/orbit-qa-explore` (skills/orbit-qa-explore.md), capped by
`qa.exploratory.max_pages`. Manual-CLI / dashboard trigger only — no
`--if-changed` variant, no run.sh wiring; a human (or a dashboard click)
decides when to explore. `qa.exploratory.enabled` is not true (including no
`qa:` block at all) → exit 2, loud, agent never invoked — same posture as
`--flow`'s own missing-`qa:`-block check. Enabled → invokes the skill exactly
once via `_run_explore_agent` (mirrors `_run_flow_agent`; reuses the SAME
ORBIT_QA_RUN_CMD test hook — the explore skill takes no argument to
substitute per-invocation the way a flow name does, so there's no reason for
a second override name), then locates the newest
`$AP_STATE/qa/explore-<UTC-ts>.json` with mtime >= this run's start (stale-
file discipline mirrors `_locate_verdict`, minus the flow-name identity
check — there's only one kind of explore result, never one scoped per name)
and shape-validates it (`pages_visited`/`new_rows` non-negative ints,
`summary` a string). Missing or malformed → exit 1, loud. Valid → print
"pages visited / new rows / summary", exit 0.

Locking: shares Task 27's `.qa-running` lock with `--regression` — both
drive a real browser against the same `qa.base_url`, so a concurrent sweep
and a concurrent explore must never run at once. A busy (fresh) lock is the
SAME benign "already running" skip `--regression` uses (exit 0, agent never
invoked), not a loud failure — one shared lock file is what actually keeps
the two from overlapping. Released in a `finally`, ownership-checked, exactly
like `--regression`'s own lock (see `_acquire_lock`/`_release_lock` above).

Exit codes (--explore): 0 crawl completed (counts echoed) or lock busy
("already running", benign skip, agent never invoked); 1 no result found /
malformed result / agent could not be launched or finished; 2 usage
(`qa.exploratory.enabled` not true, including no `qa:` block at all, or
`--if-changed`/`--flow`/`--regression` combined with `--explore`).

AI test generation (`--generate [--max N] [--attach]`): an agent reads the
project's knowledge base (`$AP_HOME/knowledge/`), drives candidate user
journeys in a real browser via the `/orbit-qa-generate` skill
(skills/orbit-qa-generate.md), and writes CANDIDATE flows to
`$AP_STATE/qa/generated/<slug>/` for a human to review/approve on the
dashboard (command_center.py's generate-approve/generate-reject) — never
directly into the committed flow library, mirroring the heal-candidate
machines-propose/humans-dispose posture. Unlike `--explore`, this needs no
extra opt-in config block — only a `qa:` block at all (same "manual-CLI,
loud failure" posture as `--flow`'s own missing-block check: exit 2, agent
never invoked). `--max N` caps how many candidate journeys the skill may
attempt this run (exported as `ORBIT_QA_GENERATE_MAX`, default
DEFAULT_GENERATE_MAX below — the skill's own hard cap, never silently
raised). `--attach` (same flag `--flow` has, same `ORBIT_QA_ATTACH=1` env
cue) lets a generate pass attach to an already-warm persistent dev session
instead of bringing qa.services up/down itself.

Shares `--regression`/`--explore`'s own `.qa-running` lock (Task 27/29) —
all three drive a real browser against the same `qa.base_url`, so no two of
them may ever run concurrently; a fresh lock is the same benign "already
running" skip (exit 0, agent never invoked). Invokes `/orbit-qa-generate`
exactly once via `_run_generate_agent` (mirrors `_run_explore_agent`, reusing
the same `ORBIT_QA_RUN_CMD` test hook), then locates the newest
`$AP_STATE/qa/generate-<UTC-ts>.json` with mtime >= this run's start
(stale-file discipline mirrors `_locate_explore_result`) and shape-validates
it (`candidates`/`skipped` lists, `count` a non-negative int matching
`len(candidates)`, `summary` a string). Missing/malformed/agent-error -> exit
1, loud. `count == 0` (a clean run that just found nothing new to propose)
is still exit 1 — parity with `--explore`'s own "0 pages" only ever being a
launch/config failure never applies here since a truly clean pass is exit 0
for explore but NOT useful to a human clicking "Generate tests" with nothing
to review; see `_run_generate`'s own comment for why 0 candidates prints a
message but does not celebrate as exit 0. `count >= 1` -> exit 0, printing
"N candidate test(s) generated — review & approve on the dashboard".

Exit codes (--generate): 0 (>=1 candidate written) or lock busy ("already
running", benign skip, agent never invoked); 1 (0 candidates, missing/
malformed result, or agent could not be launched/finished); 2 usage (no
`qa:` block at all, or `--generate` combined with `--flow`/`--regression`/
`--explore`, or `--if-changed`/`--max` misused).

Config (`.autopilot/config.yaml`, see config/schema.yaml's `qa:` block):
    qa.flows_dir (default "qa/flows"), qa.mcp_config, qa.browser,
    qa.headless, qa.model, qa.timeout_seconds, qa.permission_mode (via the
    top-level `permission_mode`, same key qa_check.py reads), qa.regression
    (on_merge/schedule_hours/max_flows_per_run — Task 25, see above),
    qa.exploratory (enabled/max_pages — Task 25, see above).

Test hook: ORBIT_QA_RUN_CMD replaces the `claude --print /orbit-qa-flow ...`
(or, for --explore, `/orbit-qa-explore`; for --generate, `/orbit-qa-generate`)
call verbatim — same substitution mechanism as ORBIT_QA_CMD/ORBIT_RUNTIME_CMD/
ORBIT_CODEGEN_CMD, its OWN name so a fake can tell this hook apart from
qa_check.py's loop-gate ORBIT_QA_CMD. AP_HOME/AP_STATE/ORBIT_HOME/FLOW_NAME
(and QA_MCP_CONFIG, regardless of override, same reasoning as qa_check.py)
are exported into the child's env either way (FLOW_NAME is naturally absent
for --explore/--generate — there is no single flow name to export;
--generate instead exports ORBIT_QA_GENERATE_MAX).

Attach mode (`--flow` and `--generate` only, Task "persistent QA app
session"): `--attach` signals that a persistent dev session
(command_center.py's "Dev session" panel) already brought `qa.services` up
and is keeping them warm — the flow (or generate pass) must attach to
`qa.base_url` instead of launching (and killing) its own copy every run.
This module never touches qa_env itself for `--flow`/`--generate` (the
`/orbit-qa-flow`/`/orbit-qa-generate` skill does that, per its own step 1) —
`--attach` just sets `ORBIT_QA_ATTACH=1` in the env passed to the `claude`
invocation (or the ORBIT_QA_RUN_CMD test-hook override, same as every other
exported var here), which is the skill's own cue to skip launch/kill
entirely (see skills/orbit-qa-flow.md / skills/orbit-qa-generate.md step 1).
`--attach` with `--regression` or `--explore` is rejected (usage error, exit
2) — out of scope here; a sweep or a crawl always brings its own services
up/down today.

Test chaining / prerequisites (`--flow` only, Task "prerequisites"): a flow
can declare `requires` in its own meta.json (see qa_teach.py's _write_flow).
Before invoking the skill, main() resolves the FULL prerequisite set into a
topologically-ordered chain ending with the target flow (`_resolve_chain`) —
a requires-cycle or a missing prerequisite flow dir is a loud, no-run exit 2,
named in the error. The resolved chain is exported as `ORBIT_QA_CHAIN`
(comma-separated flow names) via `_run_flow_agent`; the skill replays every
flow but the last ungraded, in the SAME browser session, so auth/session
state carries into the target (see skills/orbit-qa-flow.md) — only the
target's own verdict is ever located/recorded (`_locate_verdict`/
`_update_meta` are keyed on the target's name alone, unchanged). A flow with
empty `requires` resolves to a single-element chain (`[flow_name]`) — the
skill treats that identically to no chaining, so this is byte-neutral for
every flow taught before this feature existed. `--regression`'s sweep does
NOT resolve chains (out of scope for now) — each swept flow still gets its
own trivial single-element chain via `_run_flow_agent`'s own default.

Usage: qa_run.py <target> --flow <name> [--timeout N] [--attach]
       qa_run.py <target> --regression [--if-changed] [--timeout N]
       qa_run.py <target> --explore [--timeout N]
       qa_run.py <target> --generate [--max N] [--attach] [--timeout N]
"""
from __future__ import annotations

import contextlib
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

import locks
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
# The lock's staleness window is sized to ONE flow's own timeout budget (+ a
# fixed margin for the overhead between flows — MCP config generation, etc.),
# NEVER to the whole sweep's total budget (max_flows x timeout) — see
# _touch_lock: a live sweep re-proves its own liveness after every flow, so a
# concurrent invocation only ever needs to see freshness within one flow's
# worth of time, however many flows the sweep has left to go.
LOCK_STALE_MARGIN_SECONDS = 60
# Sweep-level deadline: stop LAUNCHING new flows once the sweep's own total
# elapsed time exceeds this multiple of qa.timeout_seconds — bounds a
# synchronous, one-iteration-blocking sweep under a large flow library
# without needing an unbounded per-run timeout. A flow already in flight
# still gets its own full per-flow timeout; only flows not yet STARTED are
# ever dropped by the deadline, and always with a logged line (see _sweep).
SWEEP_DEADLINE_MULTIPLIER = 2
# How long to wait for THE workspace qa.services mutex before skipping the run
# (see _services_lock). Short on purpose: whoever holds it is a loop QA gate,
# a Dev session, or another sweep - all of which last minutes - so waiting it
# out would just stall a loop iteration. A couple of seconds only covers the
# case where the holder is milliseconds from releasing.
SERVICES_LOCK_WAIT_SECONDS = 2

USAGE = ("usage: qa_run.py <target> --flow <name> [--timeout N] [--attach]\n"
         "       qa_run.py <target> --regression [--if-changed] [--timeout N]\n"
         "       qa_run.py <target> --explore [--timeout N]\n"
         "       qa_run.py <target> --generate [--max N] [--attach] [--timeout N]")

# Exit codes (manual-CLI posture — see module docstring).
OK, ERR, FAIL, USAGE_ERR = 0, 1, 3, 2
DEFAULT_GENERATE_MAX = 10


def _err(msg: str) -> None:
    print(f"qa_run: {msg}", file=sys.stderr)


def _open_agent_log(ap_state: Path, name: str):
    """Open <ap_state>/logs/<name>.log for an agent's combined output.

    These agent calls used to send stdout AND stderr to DEVNULL, so a flow
    replay or exploratory crawl that timed out or crashed left one stderr line
    and no way to see what the browser agent actually did — the same defect
    fixed in knowledge.py and qa_check.py. ap_state is a parameter here (not a
    module global as in qa_check.py), so the caller supplies it.

    None on failure rather than raising: losing the log must not fail the run.
    """
    path = ap_state / "logs" / f"{name}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("w")
    except OSError as e:
        _err(f"could not open {path} ({e}) — agent output will be discarded")
        return None


def _sync_qa_command(target: Path, command_file: str) -> None:
    """Copy $ORBIT_HOME/skills/<command_file> into
    <target>/.claude/commands/<command_file> (overwrite) right before the
    agent that invokes `/<command>` runs — the exact same fix, and the exact
    same reason, as knowledge.py's _sync_knowledge_command: a slash command
    resolves from the TARGET repo's own `.claude/commands/`, seeded once at
    onboarding by install/init.py (or `orbit sync`). A target onboarded
    BEFORE a QA skill existed (or an older version of one) keeps running the
    stale/absent copy forever otherwise — confirmed on a real target:
    `/orbit-qa-generate` was never installed, so `claude --print
    /orbit-qa-generate` invoked a command that didn't exist, the agent wrote
    no result JSON, and the run failed silently. Re-syncing just THIS one
    command on every run keeps it current with zero operator steps. Never
    abort the run over it: a permission error / read-only mount is a WARN, not
    a failure — the agent can still run against whatever copy is already
    there."""
    src = ORBIT_HOME / "skills" / command_file
    dest = target / ".claude" / "commands" / command_file
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)
    except OSError as e:
        _err(f"could not sync .claude/commands/{command_file} ({e}) — "
             f"continuing with whatever copy the target already has")


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

_EXCLUSIVE_MODE_TOKENS = ("--flow", "--regression", "--explore", "--generate")


def _parse_args(argv: list[str]):
    """Returns (target, mode, flow_name, if_changed, attach, timeout_arg,
    max_arg, error) — mode is "flow", "regression", "explore" (Task 29), or
    "generate" (AI test generation); error is a (message, exit_code) pair, or
    None on success. The four modes are all mutually exclusive with each
    other; `--if-changed` only ever applies to `--regression` (a human-
    triggered `--explore`/`--generate` has no automated-loop variant to
    gate); `--attach` applies to `--flow` and `--generate` (a persistent dev
    session's services are what a single flow OR a generate pass attaches to
    — a multi-flow sweep or an exploratory crawl always brings its own
    up/down today, out of scope here); `--max` (the generate skill's own
    candidate-journey cap) only ever applies to `--generate`."""
    if not argv:
        return None, None, None, False, False, None, None, (USAGE, USAGE_ERR)
    target = Path(argv[0]).resolve()
    rest = argv[1:]
    mode = None
    flow_name = None
    if_changed = False
    attach = False
    timeout_arg = None
    max_arg = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in _EXCLUSIVE_MODE_TOKENS:
            if mode is not None:
                return None, None, None, False, False, None, None, \
                    (f"--flow, --regression, --explore, and --generate are mutually exclusive ({USAGE})", USAGE_ERR)
            if tok == "--flow":
                if i + 1 >= len(rest):
                    return None, None, None, False, False, None, None, \
                        (f"missing value for --flow ({USAGE})", USAGE_ERR)
                mode = "flow"
                flow_name = rest[i + 1]
                i += 2
            else:
                mode = tok[2:]
                i += 1
        elif tok == "--if-changed":
            if_changed = True
            i += 1
        elif tok == "--attach":
            attach = True
            i += 1
        elif tok == "--timeout":
            if i + 1 >= len(rest):
                return None, None, None, False, False, None, None, (f"missing value for --timeout ({USAGE})", USAGE_ERR)
            timeout_arg = rest[i + 1]
            i += 2
        elif tok == "--max":
            if i + 1 >= len(rest):
                return None, None, None, False, False, None, None, (f"missing value for --max ({USAGE})", USAGE_ERR)
            max_arg = rest[i + 1]
            i += 2
        else:
            return None, None, None, False, False, None, None, (f"unrecognized argument {tok!r} ({USAGE})", USAGE_ERR)
    if mode is None:
        return None, None, None, False, False, None, None, \
            (f"one of --flow <name>, --regression, --explore, or --generate is required ({USAGE})", USAGE_ERR)
    if mode in ("flow", "explore", "generate") and if_changed:
        return None, None, None, False, False, None, None, \
            (f"--if-changed only applies to --regression ({USAGE})", USAGE_ERR)
    if mode not in ("flow", "generate") and attach:
        return None, None, None, False, False, None, None, \
            (f"--attach only applies to --flow or --generate ({USAGE})", USAGE_ERR)
    if mode != "generate" and max_arg is not None:
        return None, None, None, False, False, None, None, \
            (f"--max only applies to --generate ({USAGE})", USAGE_ERR)
    if timeout_arg is not None:
        try:
            timeout_arg = int(timeout_arg)
        except ValueError:
            return None, None, None, False, False, None, None, (f"--timeout must be an integer ({USAGE})", USAGE_ERR)
    if max_arg is not None:
        try:
            max_arg = int(max_arg)
        except ValueError:
            return None, None, None, False, False, None, None, (f"--max must be an integer ({USAGE})", USAGE_ERR)
    return target, mode, flow_name, if_changed, attach, timeout_arg, max_arg, None


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
                     mcp_path: Path, timeout: int, ap_home: Path, ap_state: Path,
                     attach: bool = False, chain: list[str] | None = None) -> int | None:
    """Returns None on a clean launch/return (proceed to look for a verdict),
    or an exit code to return immediately (claude missing / launch error —
    both bucket into exit 1, "agent error", per the exit-code table).
    `attach=True` (--flow's own --attach flag) exports ORBIT_QA_ATTACH=1 —
    see skills/orbit-qa-flow.md step 1: the skill's cue that a persistent
    dev session already owns qa.services, so it must attach to base_url
    instead of launching (and killing) its own copy.

    `chain` (test chaining — Task "prerequisites"): the fully-resolved,
    topologically-ordered prerequisite chain ending with `flow_name` itself
    (see _resolve_chain), exported as ORBIT_QA_CHAIN (comma-separated) — the
    skill's cue to replay every flow BEFORE the last one first, ungraded, in
    the SAME browser session (see skills/orbit-qa-flow.md), so auth/session
    state carries into the target. None or empty (a flow with no
    `requires`) falls back to `[flow_name]` — a single-element chain the
    skill treats identically to no chaining at all, so this is byte-neutral
    for every flow taught before this feature existed."""
    chain_list = list(chain) if chain else [flow_name]
    override = os.environ.get("ORBIT_QA_RUN_CMD")
    if override:
        cmd = shlex.split(override)
    else:
        claude = shutil.which("claude")
        if not claude:
            _err("claude not on PATH — cannot run the QA flow agent")
            return ERR
        _sync_qa_command(target, "orbit-qa-flow.md")
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
               ORBIT_HOME=str(ORBIT_HOME), FLOW_NAME=flow_name, QA_MCP_CONFIG=str(mcp_path),
               ORBIT_QA_CHAIN=",".join(chain_list))
    if attach:
        env["ORBIT_QA_ATTACH"] = "1"
    # flow_name is already SLUG_RE-gated by main() (anchored, no slashes), so
    # it is safe to use directly as a filename component.
    log = _open_agent_log(ap_state, f"qa-flow-{flow_name}")
    try:
        subprocess.run(cmd, cwd=str(target), env=env, timeout=timeout,
                       stdout=log or subprocess.DEVNULL, stderr=subprocess.STDOUT,
                       stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        _err(f"QA flow agent for {flow_name!r} failed: {e}")
        if log is not None:
            _err(f"  what it did before failing: {log.name}")
        return ERR
    finally:
        if log is not None:
            log.close()
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
    """Read-modify-write: only `runs`/`last_run_at`/`last_verdict` are ever
    set on the parsed dict, so any OTHER key already in the file — `heals`,
    and (test chaining, Task "prerequisites") `requires`/`provides_auth` — is
    carried through untouched, never dropped by a verdict update. Only the
    "meta.json couldn't even be parsed" fallback below starts from a bare
    dict with no `requires`/`provides_auth` — there is nothing to preserve
    from a file that wasn't readable in the first place."""
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
# test chaining (Task "prerequisites"): a flow can declare `requires` (other
# flow names) in its own meta.json — see qa_teach.py's _write_flow. --flow
# resolves the full prerequisite set into a topologically-ordered chain
# BEFORE ever invoking the skill (see main()'s --flow path below), so the
# skill can replay every prerequisite first, in order, in ONE browser
# session, and only then replay + grade the target — carrying auth/session
# state forward (skills/orbit-qa-flow.md). --regression's sweep does NOT
# resolve chains (out of scope for now — see _replay_flow's own docstring):
# each swept flow still gets a trivial single-element chain of itself.
# ---------------------------------------------------------------------------

def _read_requires(flows_root: Path, flow_name: str) -> list[str]:
    """This flow's own `requires` list from its meta.json — [] on anything
    short of a clean, well-shaped list of strings (missing file, unreadable,
    non-JSON, non-dict, wrong-typed field, non-string entries silently
    dropped) — mirrors _update_meta's own defensive parsing. Never raises;
    a flow with a hand-edited or legacy (pre-chaining) meta.json is simply
    treated as having no prerequisites, not a crash.

    Each surviving entry is also SLUG_RE-gated (same pattern as
    _select_regression_flows/qa_flow_library gate flow names elsewhere in
    this codebase) — not just `isinstance(r, str)`: a hand-edited meta.json
    could otherwise put a comma-bearing string into `requires`, which would
    corrupt ORBIT_QA_CHAIN's comma-separated parsing, or a `../`-style
    string, which flows straight into `(flows_root / name).is_dir()` in
    _resolve_chain below. SLUG_RE forbids commas/slashes/'..', closing both
    gaps at once; a non-slug entry is simply dropped, same "never raise on a
    hand-edited file" posture as every other check in this function."""
    try:
        meta = json.loads((flows_root / flow_name / "meta.json").read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(meta, dict):
        return []
    req = meta.get("requires")
    if not isinstance(req, list):
        return []
    return [r for r in req if isinstance(r, str) and SLUG_RE.match(r)]


def _resolve_chain(flows_root: Path, flow_name: str) -> tuple[list[str] | None, str | None]:
    """Resolves flow_name's full prerequisite set into a topologically-
    ordered chain ENDING with flow_name itself — [prereq_a, prereq_b, ...,
    flow_name] — via post-order DFS: each flow is only appended once its own
    prerequisites are already in the list, so every prerequisite always
    precedes anything that (transitively) requires it. A flow required by
    more than one flow in the chain appears exactly once, at the earliest
    point its own prerequisites are satisfied (`done` below dedupes).
    Returns (chain, None) on success, or (None, <message>) on either failure
    mode, checked BEFORE any run is attempted:
      - a requires-CYCLE (a flow transitively requires itself) — `visiting`
        catches this (a flow reached while already on the current DFS path).
      - a MISSING prerequisite — a `requires` entry naming a flow whose dir
        doesn't exist at all.
    A flow with empty `requires` (the common case, and every flow taught
    before this feature existed) resolves to `[flow_name]` — a single-
    element chain, byte-neutral with today's un-chained replay.

    Iterative (explicit-stack), NOT Python-recursive: a naive recursive
    post-order DFS here would raise an uncaught RecursionError past roughly
    ~995-deep requires chains (Python's default recursion limit, minus
    whatever the caller — main(), a test runner, etc. — already used), and
    main() has no guard for that — an unhandled traceback / exit 1 would
    violate this module's own "loud, clean exit 2" contract for a usage
    error like a cycle or a missing prerequisite. Each stack frame below is
    `[name, path, reqs, idx]` — `path` is the DFS path to (but not
    including) `name`, for the cycle message; `reqs` is None until this
    frame's entry checks (done/visiting/is_dir) have run once, then becomes
    its (possibly empty) requires list; `idx` is how many of `reqs` this
    frame has already dispatched a child frame for. This mirrors the
    recursive version call-for-call: a frame is entered once (`reqs is
    None`), dispatches one child at a time (never more than one in flight,
    same left-to-right order the recursive calls would have used), and is
    finished (dedup + append to `order`) only once every child has
    returned — so the result (order, dedup, cycle/missing detection) is
    identical to the recursive form, just bounded by heap memory (a Python
    list) instead of the C call stack."""
    order: list[str] = []
    done: set[str] = set()
    visiting: set[str] = set()

    stack: list[list] = [[flow_name, [], None, 0]]
    while stack:
        frame = stack[-1]
        name, path, reqs, idx = frame

        if reqs is None:
            # First (and only) time this frame's own entry checks run —
            # equivalent to the top of the recursive visit()'s call.
            if name in done:
                stack.pop()
                continue
            if name in visiting:
                return None, f"requires-cycle detected: {' -> '.join(path + [name])}"
            if not (flows_root / name).is_dir():
                return None, f"prerequisite flow {name!r} does not exist under {flows_root}"
            visiting.add(name)
            reqs = _read_requires(flows_root, name)
            frame[2] = reqs

        if idx < len(reqs):
            frame[3] = idx + 1
            stack.append([reqs[idx], path + [name], None, 0])
        else:
            visiting.discard(name)
            done.add(name)
            order.append(name)
            stack.pop()

    return order, None


# ---------------------------------------------------------------------------
# the shared per-flow path — used by BOTH --flow and --regression (Task 27:
# "invoke each flow through the SAME per-flow code path... factor a
# callable, don't duplicate").
# ---------------------------------------------------------------------------

def _replay_flow(qa_cfg: dict, cfg_all: dict, flow_name: str, flow_dir: Path, target: Path,
                  ap_home: Path, ap_state: Path, timeout: int, attach: bool = False,
                  chain: list[str] | None = None) -> dict:
    """Builds the flow's own MCP config, invokes the QA flow agent, locates +
    shape-validates the verdict it wrote, and — for any of the four valid
    verdicts — persists it into the flow's meta.json + FLOWMAP.md row (see
    _update_meta/_update_flowmap_verdict above). This is the ONE place that
    logic lives; --flow's main() and --regression's sweep both call this and
    differ only in how they print/aggregate/exit. `attach` (default False,
    so --regression's sweep is byte-for-byte unchanged) is --flow's own
    --attach flag — see _run_flow_agent's docstring. `chain` (default None —
    see _run_flow_agent's own docstring for the None/empty -> [flow_name]
    fallback) is --flow's own resolved prerequisite chain (Task
    "prerequisites"); --regression's sweep never resolves one (each swept
    flow still gets its own trivial single-element chain via the default) —
    chaining is a `--flow`-only feature for now.

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
                                 timeout, ap_home, ap_state, attach=attach, chain=chain)
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
    False (disables both merge signals — the sha check AND the
    markers-line-count check, together) — with both off, --if-changed never
    sweeps on its own (a human can still always force one via --regression
    with no --if-changed)."""
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


def _acquire_lock(lock_path: Path, stale_after_seconds: int, mode: str = "QA") -> bool:
    """True = lock acquired (this call now owns it — the caller MUST release
    it via _release_lock, and MUST call _touch_lock after every flow so this
    process's OWN liveness stays provable). False = a fresh lock already
    exists elsewhere; the caller must NOT proceed to sweep. A lock older than
    stale_after_seconds (qa.timeout_seconds + LOCK_STALE_MARGIN_SECONDS — see
    that constant's own comment for why NOT the whole sweep's budget) is
    presumed dead (a crashed prior sweep never reached its own finally, or
    never got the chance to touch it again) and is replaced, with a WARN —
    never left to wedge every future sweep forever. `mode` is purely cosmetic
    (labels the WARN, e.g. "regression" or "explore") — this same lock file
    and staleness window are shared by every qa_run.py entry point."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "started_at": _now_iso()}) + "\n"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        fd = None

    if fd is not None:
        wrote = False
        try:
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(payload)
                wrote = True
            except OSError:
                pass          # e.g. disk full mid-write — wrote stays False
        finally:
            # A write failure right after a fresh O_CREAT|O_EXCL must never
            # leave an orphaned (empty/partial) lock file for every future
            # sweep to trip over as "someone's already running" forever.
            if not wrote:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
        return wrote

    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        # Gone between the EEXIST and this stat() (raced with another
        # release) — treat as stale, safe to (re)create.
        age = stale_after_seconds
    if age < stale_after_seconds:
        return False
    print(f"WARN: stale {mode} lock at {lock_path} (age {int(age)}s >= "
          f"{stale_after_seconds}s) — replacing")
    try:
        lock_path.write_text(payload)
    except OSError:
        return False
    return True


def _touch_lock(lock_path: Path) -> None:
    """Proves this sweep is still alive after finishing a flow. Called once
    per flow (see _sweep) — the staleness window (_acquire_lock's
    stale_after_seconds) only ever needs to cover ONE flow's own timeout
    budget plus a margin, never the whole sweep's total, because a live
    sweep re-touches this file at least once every flow. Best-effort: a
    failure here just means the NEXT touch (or the eventual release) has to
    carry the proof instead."""
    try:
        os.utime(lock_path, None)          # None = now, both atime and mtime
    except OSError:
        pass


@contextlib.contextmanager
def _services_lock(qa_cfg, ap_state: Path, mode: str):
    """Hold THE workspace qa.services mutex (qa_check.SERVICES_LOCK_NAME)
    across an agent run that will bind qa.services' fixed ports, and yield
    whether the run may proceed.

    Distinct from `.qa-running` and NOT a replacement for it: `.qa-running`
    stops two QA RUNS overlapping in this workspace, while this stops two
    qa.services CONSUMERS fighting for the same local ports - and the other
    consumers are not QA runs at all (the loop's own gate in qa_check.py, and
    the dashboard's persistent Dev session). One lock cannot do both jobs
    because the sets do not nest.

    Yields True when the mutex is held (or when there are no services to
    serialize, in which case there is nothing to hold), False when another
    consumer has it - the caller then skips the run with a WARN rather than
    racing it for the bind and getting a half-up app.
    """
    if not (isinstance(qa_cfg, dict) and qa_cfg.get("services")):
        yield True          # nothing binds a port - nothing to serialize
        return
    # Lazy import for the same reason as _flow_mcp_config's: qa_check resolves
    # AP_STATE/AP_HOME at import time, and this run's values are only in the
    # environment by now.
    import qa_check
    lock_path = qa_check.services_lock_path(ap_state)
    try:
        with locks.hold(lock_path, timeout=SERVICES_LOCK_WAIT_SECONDS, break_stale=True):
            yield True
    except locks.LockTimeout:
        print(f"WARN: {mode}: another qa.services consumer holds {lock_path} "
              f"(the loop's QA gate, a Dev session, or another QA run) - skipping "
              f"this run rather than racing it for the app's ports")
        yield False


def _release_lock(lock_path: Path) -> None:
    """Ownership-checked release: unlinks the lock ONLY if its recorded pid
    is still ours. Between our own acquire/touch calls, another process can
    have decided (correctly or not) that our lock looked stale and replaced
    it with its own — at that point the file no longer represents OUR sweep,
    and unconditionally unlinking it would free a lock a DIFFERENT live
    sweep believes it still holds (the exact double-sweep bug this whole
    ownership check exists to close). Mismatch (or anything unreadable) →
    WARN and leave the file exactly as found; a missing file is simply
    already gone and needs no action either way."""
    try:
        d = json.loads(lock_path.read_text())
    except (OSError, ValueError):
        return
    our_pid = os.getpid()
    if not isinstance(d, dict) or d.get("pid") != our_pid:
        other = d.get("pid") if isinstance(d, dict) else "?"
        print(f"WARN: QA lock at {lock_path} is no longer ours "
              f"(recorded pid {other!r}, we are {our_pid}) — leaving it in place")
        return
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
    """Keep the newest `keep` files directly under runs_dir (oldest deleted
    first). Restricted to files THIS function's own `iterdir()` lists — no
    recursion, no path built from any untrusted field — so it can never
    touch anything outside qa/runs/. Sort key is (mtime, filename): the
    verdict filename itself embeds a UTC timestamp (see _locate_verdict's
    `<UTC-ts>-<flow>.json` shape), so the filename is a deterministic
    tie-break when two files land on the same mtime second — without it, an
    mtime tie could prune a NEWER run instead of an older one, depending only
    on filesystem iteration order."""
    if not runs_dir.is_dir():
        return 0
    try:
        files = [p for p in runs_dir.iterdir() if p.is_file()]
    except OSError:
        return 0
    if len(files) <= keep:
        return 0
    def _sort_key(p: Path):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (mtime, p.name)
    files.sort(key=_sort_key)
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
           flows_root: Path, max_flows: int, timeout_arg: int | None, default_timeout: int,
           lock_path: Path) -> int:
    """Runs the selected approved flows sequentially through _replay_flow,
    prints per-flow + summary lines, prunes qa/runs/, and returns the
    aggregate exit code (0 no fails, 3 any fail — never 1: a crash here is
    the CALLER's infra-error bucket, see _run_regression).

    Synchronous and inline — this whole call blocks its caller (run.sh's
    one_iteration, or a human's dashboard click) for as long as the flows it
    runs take. Two things bound that: `_touch_lock(lock_path)` after every
    flow proves liveness for a concurrent invocation (see _acquire_lock's own
    docstring for why the staleness window doesn't need to scale with
    max_flows), and a SWEEP-level deadline (SWEEP_DEADLINE_MULTIPLIER x
    qa.timeout_seconds, measured from this call's own start) stops launching
    NEW flows once elapsed — a flow already in flight still gets its own
    full per-flow timeout; only not-yet-started flows are ever dropped by
    it, and always with a logged line (same "never a silent cap" discipline
    as the max_flows_per_run cap above). A deadline-truncated sweep is still
    a COMPLETED one — the caller (_run_regression) still writes the cursor
    for it."""
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
    deadline_seconds = SWEEP_DEADLINE_MULTIPLIER * default_timeout
    sweep_start = time.time()
    counts = {"pass": 0, "healed": 0, "fail": 0, "unable": 0}
    for i, flow_name in enumerate(selected):
        if time.time() - sweep_start > deadline_seconds:
            remaining = selected[i:]
            print(f"regression: sweep deadline ({deadline_seconds}s = "
                  f"{SWEEP_DEADLINE_MULTIPLIER}x qa.timeout_seconds) reached after "
                  f"{sum(counts.values())} flow(s) — dropping {len(remaining)} remaining: "
                  f"{', '.join(remaining)}")
            break
        flow_dir = flows_root / flow_name
        result = _replay_flow(qa_cfg, cfg_all, flow_name, flow_dir, target, ap_home, ap_state, timeout)
        tally_kind = result["kind"] if result["kind"] != "error" else "unable"
        counts[tally_kind] += 1
        print(_format_flow_line(flow_name, result))
        # Liveness proof: a concurrent invocation's staleness check only ever
        # needs to see freshness within one flow's own budget, however many
        # flows this sweep has left to run — see _acquire_lock/_touch_lock.
        _touch_lock(lock_path)

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
    # The staleness window covers ONE flow's own budget + a fixed margin —
    # NOT the whole sweep's (max_flows x timeout_seconds) — because _sweep
    # re-touches the lock after every flow; see LOCK_STALE_MARGIN_SECONDS.
    stale_after_seconds = timeout_seconds + LOCK_STALE_MARGIN_SECONDS
    if not _acquire_lock(lock_path, stale_after_seconds, mode="regression"):
        print("regression: a sweep is already running — skipping this pass")
        return OK

    swept_ok = False
    try:
        # The sweep's flows drive a real browser against qa.services, so it is
        # one of the mutex's consumers (see _services_lock). A busy mutex skips
        # the sweep exactly as a busy `.qa-running` lock does - the cursor is
        # left untouched, so the next tick tries again.
        with _services_lock(qa_cfg, ap_state, "regression") as have_ports:
            if not have_ports:
                return OK
            rc = _sweep(target, ap_home, ap_state, cfg_all, qa_cfg, flows_root, max_flows,
                        timeout_arg, timeout_seconds, lock_path)
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
# exploratory crawls (Task 29) — opt-in, bounded, same-origin crawl from
# qa.base_url via /orbit-qa-explore (skills/orbit-qa-explore.md). Manual-CLI/
# dashboard trigger only — no --if-changed variant, no run.sh wiring. Shares
# --regression's own .qa-running lock (see module docstring's "Exploratory
# crawls" section for why: both drive a real browser against the same
# qa.base_url, so the two must never run concurrently).
# ---------------------------------------------------------------------------

EXPLORE_MCP_FILENAME = "mcp-run-explore.json"


def _explore_mcp_config(qa_cfg: dict, ap_state: Path) -> Path:
    # Same local/lazy-import reasoning as _flow_mcp_config above — qa_check.py
    # resolves its own AP_STATE/AP_HOME globals at import time, so AP_HOME/
    # AP_STATE must already be set to THIS run's target before it's imported.
    import qa_check
    output_dir = ap_state / "qa" / "explore"
    return qa_check.build_mcp_config(qa_cfg, ap_state / "qa", output_dir, EXPLORE_MCP_FILENAME)


def _run_explore_agent(qa_cfg: dict, cfg_all: dict, target: Path, mcp_path: Path,
                        timeout: int, ap_home: Path, ap_state: Path) -> int | None:
    """Same shape as _run_flow_agent (returns None on a clean launch/return,
    or an exit code to return immediately). Reuses ORBIT_QA_RUN_CMD — the
    explore skill's own invocation (`/orbit-qa-explore`, no argument) has no
    per-invocation name to substitute the way a flow name does, so there's no
    reason to invent a second override hook distinct from --flow's own."""
    override = os.environ.get("ORBIT_QA_RUN_CMD")
    if override:
        cmd = shlex.split(override)
    else:
        claude = shutil.which("claude")
        if not claude:
            _err("claude not on PATH — cannot run the QA explore agent")
            return ERR
        _sync_qa_command(target, "orbit-qa-explore.md")
        cmd = [claude, "--print", "/orbit-qa-explore",
               "--mcp-config", str(mcp_path),
               "--settings", str(ORBIT_HOME / "config" / "orbit.qa.settings.json"),
               "--permission-mode", cfg_all.get("permission_mode", "acceptEdits"),
               "--model", qa_cfg.get("model", DEFAULT_MODEL),
               "--add-dir", str(ORBIT_HOME)]
    env = dict(os.environ, AP_HOME=str(ap_home), AP_STATE=str(ap_state),
               ORBIT_HOME=str(ORBIT_HOME), QA_MCP_CONFIG=str(mcp_path))
    log = _open_agent_log(ap_state, "qa-explore")
    try:
        subprocess.run(cmd, cwd=str(target), env=env, timeout=timeout,
                       stdout=log or subprocess.DEVNULL, stderr=subprocess.STDOUT,
                       stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        _err(f"QA explore agent failed: {e}")
        if log is not None:
            _err(f"  what it did before failing: {log.name}")
        return ERR
    finally:
        if log is not None:
            log.close()
    return None


def _locate_explore_result(ap_state: Path, start: float) -> Path | None:
    """Newest $AP_STATE/qa/explore-*.json with mtime >= start — mirrors
    _locate_verdict's stale-file discipline (a leftover file from an earlier
    run must never be mistaken for this run's own outcome). No filename-
    identity check is needed here the way a flow's own suffix-glob needs one:
    there is only ONE kind of explore result, never one scoped per name for a
    sibling invocation to spoof."""
    qa_dir = ap_state / "qa"
    if not qa_dir.is_dir():
        return None
    best: tuple[float, Path] | None = None
    for p in qa_dir.glob("explore-*.json"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < start:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, p)
    return best[1] if best else None


def _validate_explore_result(r) -> str | None:
    """None = shape is valid. Else a human-readable reason it's malformed —
    same discipline as _validate_verdict: a wiring/agent mistake must never
    be mistaken for a real result. `isinstance(x, bool)` is excluded from the
    int checks — bool is a subclass of int in Python, and True/False are
    never a legitimate page/row count."""
    if not isinstance(r, dict):
        return f"explore result json is a {type(r).__name__}, not an object"
    pages = r.get("pages_visited")
    if not isinstance(pages, int) or isinstance(pages, bool) or pages < 0:
        return f"pages_visited {pages!r} is not a non-negative integer"
    rows = r.get("new_rows")
    if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
        return f"new_rows {rows!r} is not a non-negative integer"
    if not isinstance(r.get("summary"), str):
        return f"summary {r.get('summary')!r} is not a string"
    return None


def _run_explore(target: Path, ap_home: Path, cfg_all: dict, qa_cfg, timeout_arg: int | None) -> int:
    """--explore (Task 29): opt-in (`qa.exploratory.enabled`) crawl via
    /orbit-qa-explore. No `qa:` block at all, or `qa.exploratory.enabled`
    anything other than `true`, is a loud usage error (exit 2, agent never
    invoked) — same manual-CLI posture as --flow's own missing-`qa:`-block
    check, never silently skipped the way --regression --if-changed's
    loop-safety exception is.

    Shares --regression's own `.qa-running` lock (Task 27): both drive a
    real browser against the same qa.base_url, so the two must never run at
    once. A busy (fresh) lock is the SAME benign "already running" skip
    --regression's own lock-busy path uses (exit 0, agent never invoked),
    not a loud failure — one shared lock file is what actually keeps a
    concurrent sweep and a concurrent explore from overlapping. Released in
    a `finally`, ownership-checked, exactly like --regression's lock."""
    if not isinstance(qa_cfg, dict):
        _err(f"no 'qa:' block configured in {ap_home / 'config.yaml'} — "
             f"see config/schema.yaml's qa: section and add one first")
        return USAGE_ERR

    exploratory_cfg = qa_cfg.get("exploratory")
    exploratory_cfg = exploratory_cfg if isinstance(exploratory_cfg, dict) else {}
    if exploratory_cfg.get("enabled") is not True:
        _err("qa.exploratory.enabled is not true — exploratory crawling is opt-in; "
             "set qa.exploratory.enabled: true in .autopilot/config.yaml (see "
             "config/schema.yaml's qa.exploratory block) to turn it on")
        return USAGE_ERR

    ap_state = ap_home / "state"
    os.environ.setdefault("AP_HOME", str(ap_home))
    os.environ.setdefault("AP_STATE", str(ap_state))
    os.environ.setdefault("ORBIT_HOME", str(ORBIT_HOME))

    timeout_seconds = int(qa_cfg.get("timeout_seconds") or DEFAULT_TIMEOUT)
    timeout = timeout_arg if timeout_arg is not None else timeout_seconds
    qa_dir = ap_state / "qa"
    lock_path = qa_dir / LOCK_NAME
    # Same staleness-window reasoning as --regression's own lock (ONE run's
    # timeout budget + a fixed margin) — a single explore invocation is one
    # agent call, not a multi-flow sweep, so there's no per-step touch to do.
    stale_after_seconds = timeout_seconds + LOCK_STALE_MARGIN_SECONDS
    if not _acquire_lock(lock_path, stale_after_seconds, mode="explore"):
        print("explore: a QA sweep/explore is already running — skipping this run")
        return OK

    try:
        mcp_path = _explore_mcp_config(qa_cfg, ap_state)
        start = time.time()
        # The crawl binds qa.services' ports for the length of the agent call,
        # so it takes the workspace mutex too (see _services_lock).
        with _services_lock(qa_cfg, ap_state, "explore") as have_ports:
            if not have_ports:
                return OK
            launch_rc = _run_explore_agent(qa_cfg, cfg_all, target, mcp_path, timeout,
                                           ap_home, ap_state)
        if launch_rc is not None:
            return launch_rc

        result_path = _locate_explore_result(ap_state, start)
        if result_path is None:
            _err(f"no explore result found under {qa_dir} written since this run started — "
                 f"the QA explore agent never wrote one")
            return ERR

        try:
            r = json.loads(result_path.read_text())
        except (OSError, ValueError) as e:
            _err(f"could not read the explore result at {result_path} ({e})")
            return ERR

        reason = _validate_explore_result(r)
        if reason:
            _err(f"malformed explore result at {result_path}: {reason}")
            return ERR

        summary = (r.get("summary") or "").strip()
        print(f"explore: pages visited={r['pages_visited']} new rows={r['new_rows']} — "
              f"{summary or 'crawl complete'}")
        return OK
    finally:
        _release_lock(lock_path)


# ---------------------------------------------------------------------------
# AI test generation (`--generate`) — an agent reads the project's knowledge
# base and drives candidate user journeys via /orbit-qa-generate
# (skills/orbit-qa-generate.md), writing CANDIDATE flows to
# $AP_STATE/qa/generated/<slug>/ for a human to review/approve on the
# dashboard (command_center.py's generate-approve/generate-reject) — never
# directly into the committed flow library. Shares --regression/--explore's
# own .qa-running lock (see module docstring's "AI test generation" section).
# ---------------------------------------------------------------------------

GENERATE_MCP_FILENAME = "mcp-run-generate.json"


def _generate_mcp_config(qa_cfg: dict, ap_state: Path) -> Path:
    # Same local/lazy-import reasoning as _flow_mcp_config/_explore_mcp_config
    # above — qa_check.py resolves its own AP_STATE/AP_HOME globals at import
    # time, so AP_HOME/AP_STATE must already be set to THIS run's target
    # before it's imported.
    import qa_check
    output_dir = ap_state / "qa" / "generate"
    return qa_check.build_mcp_config(qa_cfg, ap_state / "qa", output_dir, GENERATE_MCP_FILENAME)


def _run_generate_agent(qa_cfg: dict, cfg_all: dict, target: Path, mcp_path: Path,
                         timeout: int, ap_home: Path, ap_state: Path, max_candidates: int,
                         attach: bool = False) -> int | None:
    """Same shape as _run_explore_agent (returns None on a clean launch/
    return, or an exit code to return immediately). Reuses ORBIT_QA_RUN_CMD —
    same substitution mechanism as every other agent invocation in this
    module. `ORBIT_QA_GENERATE_MAX` (this run's own candidate-journey cap,
    from --max or DEFAULT_GENERATE_MAX) is exported unconditionally, same
    posture as QA_MCP_CONFIG; `ORBIT_QA_ATTACH=1` only when --attach."""
    override = os.environ.get("ORBIT_QA_RUN_CMD")
    if override:
        cmd = shlex.split(override)
    else:
        claude = shutil.which("claude")
        if not claude:
            _err("claude not on PATH — cannot run the QA generate agent")
            return ERR
        _sync_qa_command(target, "orbit-qa-generate.md")
        cmd = [claude, "--print", "/orbit-qa-generate",
               "--mcp-config", str(mcp_path),
               "--settings", str(ORBIT_HOME / "config" / "orbit.qa.settings.json"),
               "--permission-mode", cfg_all.get("permission_mode", "acceptEdits"),
               "--model", qa_cfg.get("model", DEFAULT_MODEL),
               "--add-dir", str(ORBIT_HOME)]
    env = dict(os.environ, AP_HOME=str(ap_home), AP_STATE=str(ap_state),
               ORBIT_HOME=str(ORBIT_HOME), QA_MCP_CONFIG=str(mcp_path),
               ORBIT_QA_GENERATE_MAX=str(max_candidates))
    if attach:
        env["ORBIT_QA_ATTACH"] = "1"
    try:
        # Inherit stdout/stderr (NOT DEVNULL) so the agent's own transcript
        # lands in whatever the caller pointed us at — the dashboard's
        # run-generate spawn redirects this whole process into
        # $AP_STATE/logs/qa-generate.log, so a silent "agent never wrote a
        # result" (e.g. a missing /orbit-qa-generate command, a browser that
        # never launched) is now diagnosable from that log instead of leaving
        # nothing behind. `orbit qa --generate` from a terminal simply shows
        # the agent inline, same as any other foreground command.
        subprocess.run(cmd, cwd=str(target), env=env, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        _err(f"QA generate agent failed: {e}")
        return ERR
    return None


def _locate_generate_result(ap_state: Path, start: float) -> Path | None:
    """Newest $AP_STATE/qa/generate-*.json with mtime >= start — mirrors
    _locate_explore_result's stale-file discipline (a leftover file from an
    earlier run must never be mistaken for this run's own outcome). No
    filename-identity check needed — there is only ONE kind of generate
    result per invocation, never one scoped per candidate name."""
    qa_dir = ap_state / "qa"
    if not qa_dir.is_dir():
        return None
    best: tuple[float, Path] | None = None
    for p in qa_dir.glob("generate-*.json"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < start:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, p)
    return best[1] if best else None


def _validate_generate_result(r) -> str | None:
    """None = shape is valid. Else a human-readable reason it's malformed —
    same discipline as _validate_explore_result: a wiring/agent mistake must
    never be mistaken for a real result. `count` must be a non-negative int
    (bool excluded, same reasoning as pages_visited/new_rows) AND must equal
    len(candidates) — the ONE cross-field check this result shape needs that
    explore's doesn't, since `count` is meant to be a pure derivative of
    `candidates`, not an independently-trustable field."""
    if not isinstance(r, dict):
        return f"generate result json is a {type(r).__name__}, not an object"
    candidates = r.get("candidates")
    if not isinstance(candidates, list) or not all(isinstance(c, str) for c in candidates):
        return f"candidates {candidates!r} is not a list of strings"
    skipped = r.get("skipped")
    if not isinstance(skipped, list):
        return f"skipped {skipped!r} is not a list"
    count = r.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return f"count {count!r} is not a non-negative integer"
    if count != len(candidates):
        return f"count {count} does not match len(candidates) ({len(candidates)})"
    if not isinstance(r.get("summary"), str):
        return f"summary {r.get('summary')!r} is not a string"
    return None


def _run_generate(target: Path, ap_home: Path, cfg_all: dict, qa_cfg, timeout_arg: int | None,
                   attach: bool, max_arg: int | None) -> int:
    """--generate: opt-in only in the sense that it needs a `qa:` block at
    all (same loud, no-run exit 2 as --flow's own missing-block check) — NO
    extra config flag to flip, unlike --explore's `qa.exploratory.enabled`;
    generating candidate tests is always available once QA is configured at
    all, since it never writes into the committed flow library itself (see
    skills/orbit-qa-generate.md) — only a human clicking Approve does that.

    Shares --regression/--explore's own `.qa-running` lock: all three drive
    a real browser against the same qa.base_url. A busy (fresh) lock is the
    same benign "already running" skip (exit 0, agent never invoked)."""
    if not isinstance(qa_cfg, dict):
        _err(f"no 'qa:' block configured in {ap_home / 'config.yaml'} — "
             f"see config/schema.yaml's qa: section and add one first")
        return USAGE_ERR

    ap_state = ap_home / "state"
    os.environ.setdefault("AP_HOME", str(ap_home))
    os.environ.setdefault("AP_STATE", str(ap_state))
    os.environ.setdefault("ORBIT_HOME", str(ORBIT_HOME))

    max_candidates = max_arg if max_arg is not None else DEFAULT_GENERATE_MAX
    if max_candidates <= 0:
        max_candidates = DEFAULT_GENERATE_MAX

    timeout_seconds = int(qa_cfg.get("timeout_seconds") or DEFAULT_TIMEOUT)
    timeout = timeout_arg if timeout_arg is not None else timeout_seconds
    qa_dir = ap_state / "qa"
    lock_path = qa_dir / LOCK_NAME
    stale_after_seconds = timeout_seconds + LOCK_STALE_MARGIN_SECONDS
    if not _acquire_lock(lock_path, stale_after_seconds, mode="generate"):
        print("generate: a QA sweep/explore/generate is already running — skipping this run")
        return OK

    try:
        mcp_path = _generate_mcp_config(qa_cfg, ap_state)
        start = time.time()
        # Same as --regression/--explore: the generate agent drives the running
        # app, so it takes the workspace qa.services mutex (see _services_lock).
        with _services_lock(qa_cfg, ap_state, "generate") as have_ports:
            if not have_ports:
                return OK
            launch_rc = _run_generate_agent(qa_cfg, cfg_all, target, mcp_path, timeout, ap_home,
                                            ap_state, max_candidates, attach=attach)
        if launch_rc is not None:
            return launch_rc

        result_path = _locate_generate_result(ap_state, start)
        if result_path is None:
            _err(f"no generate result found under {qa_dir} written since this run started — "
                 f"the QA generate agent never wrote one")
            return ERR

        try:
            r = json.loads(result_path.read_text())
        except (OSError, ValueError) as e:
            _err(f"could not read the generate result at {result_path} ({e})")
            return ERR

        reason = _validate_generate_result(r)
        if reason:
            _err(f"malformed generate result at {result_path}: {reason}")
            return ERR

        summary = (r.get("summary") or "").strip()
        count = r["count"]
        if count <= 0:
            # Manual-CLI posture: a clean pass that found nothing new to
            # propose is still not what a human clicking "Generate tests"
            # wanted to see — unlike --explore (where a clean crawl finding
            # zero new bugs IS the happy path, exit 0), zero candidates here
            # means there's nothing on the dashboard to review, so this is
            # reported the same way --flow's own "unable" bucket is: loud,
            # exit 1, never silently celebrated as success.
            _err(f"0 candidate tests generated — {summary or 'nothing new to propose (already covered, or none found)'}")
            return ERR

        print(f"generate: {count} candidate test(s) generated — review & approve on the dashboard "
              f"— {summary or 'see the dashboard for details'}")
        return OK
    finally:
        _release_lock(lock_path)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    target, mode, flow_name, if_changed, attach, timeout_arg, max_arg, error = _parse_args(argv)
    if error:
        msg, code = error
        _err(msg)
        return code

    ap_home = target / ".autopilot"
    cfg_all = _load_cfg(target)
    qa_cfg = cfg_all.get("qa")

    if mode == "generate":
        # Manual-CLI posture throughout, same as --explore — no loop-safety
        # silent-skip exception here; a missing `qa:` block is a loud exit 2,
        # checked inside _run_generate itself.
        return _run_generate(target, ap_home, cfg_all, qa_cfg, timeout_arg, attach, max_arg)

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

    if mode == "explore":
        # Manual-CLI posture throughout — no loop-safety silent-skip
        # exception here (that's --regression --if-changed's own concern);
        # a missing/disabled qa.exploratory is always a loud exit 2, checked
        # inside _run_explore itself.
        return _run_explore(target, ap_home, cfg_all, qa_cfg, timeout_arg)

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

    # Test chaining (Task "prerequisites"): resolve flow_name's own
    # `requires` (meta.json) into a full, topologically-ordered chain BEFORE
    # ever invoking the skill — a requires-cycle or a missing prerequisite is
    # a loud, no-run usage error (exit 2), same posture as every other
    # --flow validation above. A flow with empty requires resolves to
    # `[flow_name]` — see _resolve_chain/_run_flow_agent's own docstrings for
    # why that's byte-neutral with today's un-chained replay.
    chain, chain_err = _resolve_chain(flows_root, flow_name)
    if chain_err:
        _err(f"cannot resolve prerequisite chain for {flow_name!r}: {chain_err}")
        return USAGE_ERR

    result = _replay_flow(qa_cfg, cfg_all, flow_name, flow_dir, target, ap_home, ap_state, timeout,
                           attach=attach, chain=chain)
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
