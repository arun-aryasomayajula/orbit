#!/usr/bin/env python3
"""PRD run controller: marker I/O, sha stamp chain, stage gates, revise
cascade. The CONTROLLER OWNS THE VERDICT - a stage's Tier-A eval verdict
(prd_evals.py) and this module's gate logic are the sole authority on
whether a run advances; nothing an agent/claude writes into an artifact is
trusted to self-report success. (Task 4 wires the actual claude invocation
per stage - this module only records and gates what's already on disk.)
Design reference: dwh-suite's prd_run.py.

Marker: research/<run>/prd-run.json - {run_id, title, brief, created,
status, stages: {<stage>: {sha, prev_sha, stamped_at, evals: {ok, errors}}},
approvals: {scope, prd}}. Writes are atomic (mkstemp + os.replace).

Stage order: intake -> [gate: approve-scope] -> research -> synthesis ->
draft -> review -> [gate: approve-prd] -> done. Status is either a stage
name ("that stage is next"), "awaiting-scope-approval", "awaiting-signoff",
or "done". A stage whose Tier-A eval fails still gets stamped (evals.ok:
false, errors recorded) but status does NOT advance - the run stays put
until a human or a later agent fixes the artifact and re-records.

CLI:
  prd_flow.py new <target> <run> <title> [--brief <text>]
  prd_flow.py status <target> <run>
  prd_flow.py record <target> <run> <stage>
  prd_flow.py approve-scope <target> <run>
  prd_flow.py approve-prd <target> <run>
  prd_flow.py revise <target> <run> <stage>
"""
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prd_evals


class GateError(RuntimeError):
    """Raised for out-of-order stage recording or missing approval gates."""


STAGES = ["intake", "research", "synthesis", "draft", "review"]
ARTIFACTS = {
    "intake": "01-intake.json",
    "research": "02-research.json",
    "synthesis": "03-synthesis.json",
    "draft": "04-prd.md",
    "review": "05-review.json",
}
# status a stage's marker moves to once its Tier-A eval passes.
NEXT_STATUS = {
    "intake": "awaiting-scope-approval",
    "research": "synthesis",
    "synthesis": "draft",
    "draft": "review",
    "review": "awaiting-signoff",
}
MARKER_NAME = "prd-run.json"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_dir(target, run):
    return os.path.join(target, "research", run)


def _marker_path(target, run):
    return os.path.join(_run_dir(target, run), MARKER_NAME)


def _artifact_path(target, run, stage):
    return os.path.join(_run_dir(target, run), ARTIFACTS[stage])


def _atomic_write_json(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".prd-run-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def new(target, run_id, title, brief=""):
    """Create research/<run>/ and its marker at status 'intake'."""
    os.makedirs(_run_dir(target, run_id), exist_ok=True)
    marker = {
        "run_id": run_id,
        "title": title,
        "brief": brief,
        "created": _now(),
        "status": "intake",
        "stages": {},
        "approvals": {"scope": None, "prd": None},
    }
    _atomic_write_json(_marker_path(target, run_id), marker)
    return marker


def load_marker(target, run):
    with open(_marker_path(target, run)) as f:
        return json.load(f)


def _write_marker(target, run, marker):
    _atomic_write_json(_marker_path(target, run), marker)


def _load_stage_json(target, run, stage):
    with open(_artifact_path(target, run, stage)) as f:
        return json.load(f)


def _check_review(d):
    # Review stage's own eval (contracts.md): 05-review.json must carry a
    # verdicts list and a boolean ok. Structural only - the human sign-off
    # gate (approve_prd) is what actually decides the run's fate, using the
    # verdicts a person reads, not this boolean.
    errs = []
    if not isinstance(d.get("verdicts"), list):
        errs.append("review: 'verdicts' must be a list")
    if not isinstance(d.get("ok"), bool):
        errs.append("review: 'ok' must be a boolean")
    return errs


def _run_evals(target, run, stage, raw):
    if stage == "intake":
        return prd_evals.check_intake(json.loads(raw))
    if stage == "research":
        return prd_evals.check_research(json.loads(raw))
    if stage == "synthesis":
        research = _load_stage_json(target, run, "research")
        return prd_evals.check_synthesis(json.loads(raw), research)
    if stage == "draft":
        research = _load_stage_json(target, run, "research")
        return prd_evals.check_prd_md(raw.decode("utf-8"), research)
    if stage == "review":
        return _check_review(json.loads(raw))
    raise GateError(f"no eval dispatch for stage '{stage}'")  # unreachable given STAGES


def record_stage(target, run, stage):
    """Stamp `stage`'s artifact (sha + chained prev_sha) and run its Tier-A
    eval. Refuses (GateError) if `stage` isn't the marker's current status
    or its artifact file is missing. On eval pass, advances status; on
    fail, the stamp is still recorded (evals.ok: false) but status holds."""
    if stage not in STAGES:
        raise GateError(f"unknown stage '{stage}' (one of: {', '.join(STAGES)})")
    marker = load_marker(target, run)
    if marker["status"] != stage:
        raise GateError(
            f"cannot record '{stage}': current status is '{marker['status']}'")
    path = _artifact_path(target, run, stage)
    if not os.path.exists(path):
        raise GateError(f"cannot record '{stage}': artifact not found at {path}")
    with open(path, "rb") as f:
        raw = f.read()
    sha = hashlib.sha256(raw).hexdigest()
    idx = STAGES.index(stage)
    prev_sha = None
    if idx > 0:
        prev_stamp = marker["stages"].get(STAGES[idx - 1])
        prev_sha = prev_stamp["sha"] if prev_stamp else None
    errs = _run_evals(target, run, stage, raw)
    ok = not errs
    marker["stages"][stage] = {
        "sha": sha,
        "prev_sha": prev_sha,
        "stamped_at": _now(),
        "evals": {"ok": ok, "errors": errs},
    }
    if ok:
        marker["status"] = NEXT_STATUS[stage]
    _write_marker(target, run, marker)
    return marker


def verify(target, run):
    """Recompute every stamped stage's sha against its current artifact
    file; return one message per stage whose artifact is missing or whose
    sha no longer matches (tamper report - the dashboard surfaces these)."""
    marker = load_marker(target, run)
    problems = []
    for stage, stamp in marker.get("stages", {}).items():
        path = _artifact_path(target, run, stage)
        if not os.path.exists(path):
            problems.append(f"{stage}: artifact missing at {path}")
            continue
        with open(path, "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        if actual != stamp.get("sha"):
            problems.append(f"{stage}: sha mismatch - artifact modified after stamping")
    return problems


def approve_scope(target, run):
    marker = load_marker(target, run)
    if marker["status"] != "awaiting-scope-approval":
        raise GateError(
            f"cannot approve scope: status is '{marker['status']}', "
            "expected 'awaiting-scope-approval'")
    marker["approvals"]["scope"] = {"at": _now()}
    marker["status"] = "research"
    _write_marker(target, run, marker)
    return marker


def approve_prd(target, run):
    marker = load_marker(target, run)
    if marker["status"] != "awaiting-signoff":
        raise GateError(
            f"cannot approve prd: status is '{marker['status']}', "
            "expected 'awaiting-signoff'")
    marker["approvals"]["prd"] = {"at": _now()}
    marker["status"] = "done"
    _write_marker(target, run, marker)
    return marker


def revise(target, run, stage):
    """Drop `stage`'s stamp and every later stage's, reset the approvals
    that gate stages at or after it, and rewind status to `stage`."""
    if stage not in STAGES:
        raise GateError(f"unknown stage '{stage}' (one of: {', '.join(STAGES)})")
    marker = load_marker(target, run)
    idx = STAGES.index(stage)
    for s in STAGES[idx:]:
        marker["stages"].pop(s, None)
    if idx == 0:  # revising intake invalidates the scope approval it gates
        marker["approvals"]["scope"] = None
    marker["approvals"]["prd"] = None  # any revision before 'done' reopens signoff
    marker["status"] = stage
    _write_marker(target, run, marker)
    return marker


def _cli_new(rest):
    target, run, title = rest[0], rest[1], rest[2]
    brief = rest[rest.index("--brief") + 1] if "--brief" in rest else ""
    return new(target, run, title, brief=brief)


CMDS = {
    "new": _cli_new,
    "status": lambda rest: load_marker(rest[0], rest[1]),
    "record": lambda rest: record_stage(rest[0], rest[1], rest[2]),
    "approve-scope": lambda rest: approve_scope(rest[0], rest[1]),
    "approve-prd": lambda rest: approve_prd(rest[0], rest[1]),
    "revise": lambda rest: revise(rest[0], rest[1], rest[2]),
}


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd not in CMDS:
        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    try:
        print(json.dumps(CMDS[cmd](rest), indent=2))
    except GateError as e:
        print(f"gate error: {e}", file=sys.stderr)
        return 3
    except IndexError:
        print(f"missing argument for '{cmd}' - see prd_flow.py with no args", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
