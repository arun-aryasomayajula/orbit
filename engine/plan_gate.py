#!/usr/bin/env python3
"""plan_gate.py — the deterministic scope-fence gate (opt-in `plan.enforce: "block"`).

The planner agent writes a `plan_scope` fence naming the files a task is
allowed to touch (`edit`, `create`, `forbid`, `allow_incidental`). Most repos
run `plan.enforce: "verifier"` — the verifier judges scope creep as one more
review signal. `plan.enforce: "block"` opts into a HARD backstop: run.sh calls
this script on the cycle's already-committed change, right before qa_check
(the browser QA gate that superseded runtime_check.py), and refuses to push
a commit that touches files outside the fence.

Cardinal rule: **never block blind**. Every missing or unparseable
precondition — no `plan:` block, `enforce` not `"block"`, no plan file, no
parseable ```yaml fence with a `plan_scope` mapping, a `git show` that fails —
ships (exit 0), because a wiring gap must never look like a scope violation.
Malformed YAML/shapes WARN and are skipped, never crash the gate.

Fence extraction: every fenced ```yaml block in the plan file is a candidate
iff it parses to a dict containing a `plan_scope` key with a well-formed
mapping (each of edit/create/forbid/allow_incidental, if present, a list of
strings). Among candidates, the LAST one in the file wins — the planner's
amendment protocol appends a complete new fence rather than editing the
first, so this is "latest amendment governs", not a merge of all fences.
Enforcement always uses the last VALID candidate — but if ANY fence in the
file looked like a plan_scope block and failed to parse, that WARNs even when
an earlier still-valid fence keeps enforcing: a botched amendment must leave
a signal that it didn't take.

Matching: a committed path is allowed if it matches any entry in
edit ∪ create ∪ allow_incidental. Entry semantics: exact match; directory
prefix if the entry ends in `/`; otherwise an `fnmatch` glob. `forbid`
overrides — a path matching any `forbid` entry is a violation even if also
allowed elsewhere.

Exit codes (run.sh dispatches on EXACTLY 3, same as qa_check.py):
    0   ship — feature off, preconditions missing/unparseable, or in-scope
    2   usage — bad argv / a task id that isn't a lowercase slug
    3   escalate — the commit touches at least one out-of-fence path

Usage:  plan_gate.py <task-id> <worktree>
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

GIT_TIMEOUT = 30
SHIP, USAGE, ESCALATE = 0, 2, 3

# Same "lowercase slug" shape command_center.py enforces for task ids created
# via the dashboard — a task id is engine-internal identifier, not free text.
_TID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")

_YAML_FENCE_RE = re.compile(r"```yaml[ \t]*\r?\n(.*?)```", re.DOTALL)

_SCOPE_KEYS = ("edit", "create", "forbid", "allow_incidental")


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
REVIEWS = AP_STATE / "reviews"


def _load_config() -> dict:
    try:
        cfg = yaml.safe_load((AP_HOME / "config.yaml").read_text())
    except (OSError, yaml.YAMLError):
        return {}
    # A syntactically-valid YAML document whose top level isn't a mapping
    # (a bare list/string/int) is not a config we understand — treat it the
    # same as a missing/corrupt file (feature off) rather than let `.get()`
    # below raise on it.
    return cfg if isinstance(cfg, dict) else {}


def _parse_scope_fence(fence_text: str) -> tuple[dict | None, str | None]:
    """One ```yaml fence body → (scope, None) when it's a well-formed
    plan_scope candidate, or (None, reason) when it either isn't a candidate
    at all (no `plan_scope` key — not a warning, just not ours) or LOOKS like
    one but is malformed (reason is set — the caller may WARN)."""
    try:
        parsed = yaml.safe_load(fence_text)
    except yaml.YAMLError as e:
        return None, f"invalid YAML in a plan_scope fence ({e})"
    if not isinstance(parsed, dict) or "plan_scope" not in parsed:
        return None, None
    scope = parsed["plan_scope"]
    if not isinstance(scope, dict):
        return None, f"plan_scope is not a mapping (got {type(scope).__name__})"
    norm: dict[str, list[str]] = {}
    for key in _SCOPE_KEYS:
        val = scope.get(key) or []
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            return None, f"plan_scope.{key} must be a list of strings (got {val!r})"
        norm[key] = val
    return norm, None


def _extract_scope(text: str) -> tuple[dict | None, str | None]:
    """Scan ALL ```yaml fences in the plan file; the LAST well-formed
    plan_scope candidate wins — enforcement is always against the last VALID
    fence, never a merge. `warn` is set whenever ANY fence in the file looked
    like a plan_scope candidate but failed to parse, REGARDLESS of whether a
    later fence recovers with a valid one: an agent that botches an amendment
    must leave a signal that the botched fence didn't take, even though an
    earlier still-valid fence keeps enforcing."""
    winner = None
    warn = None
    for fence in _YAML_FENCE_RE.findall(text):
        scope, reason = _parse_scope_fence(fence)
        if scope is not None:
            winner = scope
        elif reason:
            warn = reason
    return winner, warn


def _matches_any(path: str, entries: list[str]) -> bool:
    for entry in entries:
        if not entry:
            continue
        if entry.endswith("/"):
            if path == entry.rstrip("/") or path.startswith(entry):
                return True
        elif entry == path:
            return True
        elif fnmatch.fnmatch(path, entry):
            return True
    return False


def _changed_paths(worktree: str) -> tuple[list[str] | None, str | None]:
    """Paths touched by HEAD (the cycle's one atomic commit). `--end-of-options`
    guards the literal ref the same way knowledge.py's _changed_paths_for_marker
    guards an agent-written sha, kept here for consistency even though `HEAD`
    itself is never attacker-controlled."""
    try:
        p = subprocess.run(
            ["git", "-C", worktree, "show", "--name-only", "--format=",
             "--end-of-options", "HEAD"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"git show failed: {e}"
    if p.returncode != 0:
        return None, f"git show HEAD failed: {p.stderr.strip() or p.returncode}"
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()], None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: plan_gate.py <task-id> <worktree>", file=sys.stderr)
        return USAGE
    tid, worktree = argv[0], argv[1]
    if not _TID_RE.match(tid):
        print(f"usage: task id must be a lowercase slug (letters/digits/._-, "
              f"max 80 chars), got {tid!r}", file=sys.stderr)
        return USAGE

    # Same precedent as qa_check.py's _unlink_stale_verdicts() — unlink any stale verdict
    # from a PRIOR run of this tid before doing anything else, so an
    # escalate→answer→re-pick→clean-ship flow never folds an old run's
    # violations into review_packet for a later, actually-clean commit.
    gate_file = REVIEWS / f"task-{tid}-plan-gate.txt"
    try:
        gate_file.unlink(missing_ok=True)
    except OSError:
        pass

    plan_cfg = _load_config().get("plan")
    if not isinstance(plan_cfg, dict) or plan_cfg.get("enforce") != "block":
        return SHIP                      # feature off / verifier-only → nothing to enforce here

    try:
        text = (REVIEWS / f"task-{tid}-plan.md").read_text()
    except OSError:
        return SHIP                      # no plan file — never block blind

    scope, warn = _extract_scope(text)
    if warn:
        if scope is None:
            print(f"WARN: plan_gate: {warn} — shipping without scope enforcement")
        else:
            print(f"WARN: plan_gate: {warn} — amendment did not take; "
                  f"enforcing the last VALID plan_scope fence instead")
    if scope is None:
        return SHIP

    allowed = scope["edit"] + scope["create"] + scope["allow_incidental"]
    forbid = scope["forbid"]

    if not allowed:
        # An empty-but-valid fence (`plan_scope: {}` or all-null/empty keys)
        # has no edit/create/allow_incidental entries at all — enforcing it
        # would escalate EVERY path, a block-always from one degenerate
        # planner output. Owner ruling: treat this as malformed rather than
        # a real "touch nothing" scope — never-block-blind spirit; verifier
        # mode still catches real scope creep as a review signal.
        print("WARN: plan_gate: plan_scope's edit/create/allow_incidental union is "
              "empty — treating as a degenerate fence, shipping without scope enforcement")
        return SHIP

    changed, reason = _changed_paths(worktree)
    if changed is None:
        print(f"WARN: plan_gate: {reason} — shipping without scope enforcement")
        return SHIP

    violations = []
    for path in changed:
        if _matches_any(path, forbid):
            violations.append((path, "forbid"))
        elif not _matches_any(path, allowed):
            violations.append((path, "out-of-scope"))

    if not violations:
        return SHIP

    for path, rule in violations:
        print(f"plan gate: {path} — violates {rule}")
    try:
        gate_file.write_text("\n".join(f"{path}\t{rule}" for path, rule in violations) + "\n")
    except OSError:
        pass
    return ESCALATE


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
