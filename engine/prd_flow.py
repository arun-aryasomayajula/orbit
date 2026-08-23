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

run_stage() (Task 4) does the actual work: one headless `claude` call per
stage (MCP wiring via engine/mcp.py, same idiom as intake.py/qa_check.py),
then record_stage() on what it wrote. `ORBIT_PRD_CMD` (shlex-split) swaps
the whole command out for a test double; `chain=True` keeps driving
successive stages while each one's eval passes and the marker hasn't hit a
gate (awaiting-*) or "done".

CLI:
  prd_flow.py new <target> <run> <title> [--brief <text>] [--pm <name>]
  prd_flow.py status <target> <run>
  prd_flow.py record <target> <run> <stage>
  prd_flow.py approve-scope <target> <run> [--ack-pii]
  prd_flow.py approve-prd <target> <run>
  prd_flow.py revise <target> <run> <stage>
  prd_flow.py run <target> <run> <stage> [--chain]
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp
import prd_evals
import secrets_env

ORBIT_HOME = os.environ.get("ORBIT_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_TIMEOUT = 3600
SKILLS = {
    "intake": "orbit-prd-intake",
    "research": "orbit-prd-research",
    "synthesis": "orbit-prd-synthesis",
    "draft": "orbit-prd-writer",
    "review": "orbit-prd-review",
}


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
# A run id becomes a DIRECTORY name under research/ and a token in a slash
# command, so the controller - not just the dashboard that happens to call it -
# owns the shape. Anything with a slash, a dot, a quote or a space is refused
# here, which also keeps `research/<run>/` inside research/. The dashboard
# imports this rather than keeping its own copy.
RUN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


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


def _default_pm(target):
    """Who is running this PRD, when nobody said. The workspace's git identity
    is the closest thing to the PM's name that is already on the machine; the
    login name is the last resort. Never empty - `pm` is a required intake
    field, and an empty one fails intake's eval for a reason no PM can act on."""
    try:
        r = subprocess.run(["git", "-C", target, "config", "user.name"],
                           capture_output=True, text=True, timeout=10)
        name = (r.stdout or "").strip()
        if r.returncode == 0 and name:
            return name
    except (OSError, subprocess.SubprocessError):
        pass
    return os.environ.get("USER") or "unknown"


def new(target, run_id, title, brief="", pm=""):
    """Create research/<run>/ and its marker at status 'intake'.

    `pm` is the run's owner. Nothing else carries the PM's identity into a run,
    so intake had no source for the `pm` field its own contract requires and
    every real run stalled with an empty one. Left blank, it resolves from the
    workspace's git identity (see _default_pm) - the intake skill copies this
    value verbatim rather than inventing a name."""
    if not RUN_RE.match(run_id or ""):
        raise GateError(
            f"invalid run id {run_id!r} - lowercase letters, digits and hyphens only, "
            "must start alphanumeric")
    os.makedirs(_run_dir(target, run_id), exist_ok=True)
    marker = {
        "run_id": run_id,
        "title": title,
        "brief": brief,
        "pm": (pm or "").strip() or _default_pm(target),
        "created": _now(),
        "status": "intake",
        "pii_touch": False,   # set from 01-intake.json when intake is recorded
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


def write_marker(target, run, marker):
    """The marker's atomic write, under a public name.

    engine/publish.py owns the marker's `published` sub-block (M5 ledger
    ruling) and does its own locked read-modify-write of that one key. It
    needs THIS writer - a second hand-rolled atomic write would be a second
    definition of how this file is written - and reaching into a private for
    it would hide that dependency from anyone changing the write mechanics."""
    _write_marker(target, run, marker)


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


def _eval_verdict(target, run, stage, raw):
    """The stamp's `evals` block for `stage`. An artifact that won't parse, or
    a shape no checker anticipated, must FAIL the eval - never kill the stage
    process. A crash here is recorded as an error string so the run stays put
    with a readable reason on the card instead of dying silently mid-stage."""
    try:
        errs = _run_evals(target, run, stage, raw)
    except Exception as e:  # noqa: BLE001 - the whole point is that nothing escapes
        return {"ok": False, "errors": [f"eval crashed: {e!r}"]}
    return {"ok": not errs, "errors": errs}


def _intake_pii_touch(raw):
    """intake's `pii_touch`, read off the artifact bytes. A malformed artifact
    reads as False - its eval is failing anyway, so the run cannot reach the
    scope gate this flag guards."""
    try:
        return bool(json.loads(raw).get("pii_touch"))
    except (ValueError, AttributeError, TypeError):
        return False


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
    if stage == "intake":
        # Lift intake's pii_touch onto the marker so the scope gate (and the
        # dashboard card) can see it without re-reading the artifact. Recorded
        # on every intake stamp, so a revise that clears the flag clears it here.
        marker["pii_touch"] = _intake_pii_touch(raw)
    verdict = _eval_verdict(target, run, stage, raw)
    ok = verdict["ok"]
    marker["stages"][stage] = {
        "sha": sha,
        "prev_sha": prev_sha,
        "stamped_at": _now(),
        "evals": verdict,
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


def approve_scope(target, run, ack_pii=False):
    """Approve the scope gate. On a run whose intake set pii_touch, approval
    needs `ack_pii` on top of the status check: the grounding contract defers
    PII compliance to a human, and a one-click approve is not that human
    saying yes to PII. The dashboard asks a second, explicit question."""
    marker = load_marker(target, run)
    if marker["status"] != "awaiting-scope-approval":
        raise GateError(
            f"cannot approve scope: status is '{marker['status']}', "
            "expected 'awaiting-scope-approval'")
    if marker.get("pii_touch") and not ack_pii:
        raise GateError("pii_touch run: scope approval requires explicit PII acknowledgement")
    marker["approvals"]["scope"] = {"at": _now(), "ack_pii": bool(marker.get("pii_touch"))}
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


def _load_prd_cfg(target):
    """Read the `prd:` block from the target's .autopilot/config.yaml (model,
    timeout_seconds). No config file or no `prd:` block -> {} (defaults
    apply). Mirrors engine/mcp.py's _load_mcp idiom."""
    cfg_path = os.path.join(target, ".autopilot", "config.yaml")
    if not os.path.exists(cfg_path):
        return {}
    import yaml
    raw = yaml.safe_load(open(cfg_path)) or {}
    return raw.get("prd") or {}


def _mcp_wire(target):
    """(settings_path, build_path) for the stage's headless call.

    mcp.py is a CLI first: on a missing/broken config it calls sys.exit(), which
    raises SystemExit - and SystemExit is not an Exception, so it would sail
    straight through every `except Exception` and terminate the process that
    called us. From the dashboard that is a stage that dies with nothing on the
    card. Convert it (and anything else that escapes) into a GateError the
    caller already knows how to report. KeyboardInterrupt stays fatal."""
    try:
        return mcp.write_settings(target), mcp.write_build(target)
    except KeyboardInterrupt:
        raise
    except BaseException as e:  # noqa: BLE001 - SystemExit is the whole reason
        raise GateError(f"mcp wiring failed for {target}: {e}") from e


def _build_cmd(target, run, stage, prd_cfg):
    claude = shutil.which("claude")
    if not claude:
        raise GateError("claude not found on PATH")
    model = prd_cfg.get("model") or DEFAULT_MODEL
    settings_path, build_path = _mcp_wire(target)
    cmd = [claude, "--print", f"/{SKILLS[stage]} {run}",
           "--permission-mode", "acceptEdits",
           "--settings", settings_path,
           "--model", model]
    if build_path:
        cmd += ["--mcp-config", build_path, "--strict-mcp-config"]
    return cmd


def _invoke_claude(target, run, stage):
    """Run the headless claude call for `stage` (or its ORBIT_PRD_CMD test
    double). Raises GateError on nonzero exit or timeout; the exception
    carries the tail of what the process printed. Does NOT touch the
    marker - the caller records what got written to disk."""
    prd_cfg = _load_prd_cfg(target)
    timeout = prd_cfg.get("timeout_seconds") or DEFAULT_TIMEOUT
    override = os.environ.get("ORBIT_PRD_CMD")
    cmd = shlex.split(override) if override else _build_cmd(target, run, stage, prd_cfg)
    ap = os.path.join(target, ".autopilot")
    # Stage children need the PM's tokens: an MCP server configured with
    # token_env sends `Authorization: Bearer ${VAR}`, and Claude Code expands
    # ${VAR} from THIS child's environment. The dashboard writes those tokens
    # to .autopilot/.secrets.env; run.sh's loop exports them for cycles, but a
    # stage launched from the dashboard (do_prd_action -> Popen) inherits the
    # server's environment, not the loop's - so read them here. Precedence:
    # inherited env < .secrets.env < the PRD_*/AP_* names this call owns.
    # Never log these values.
    env = dict(os.environ)
    env.update(secrets_env.read(target))
    env.update(PRD_STAGE=stage, PRD_RUN=run, PRD_TARGET=target,
               AP_HOME=ap, AP_STATE=os.path.join(ap, "state"), ORBIT_HOME=ORBIT_HOME)
    try:
        p = subprocess.run(cmd, cwd=target, env=env, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, text=True)
    except subprocess.TimeoutExpired:
        raise GateError(f"stage '{stage}' timed out after {timeout}s")
    except OSError as e:
        raise GateError(f"stage '{stage}' failed to start: {e}")
    if p.returncode != 0:
        tail = (p.stdout or "")[-2000:]
        raise GateError(f"stage '{stage}' exited {p.returncode}:\n{tail}")


def _run_one_stage(target, run, stage):
    if stage not in STAGES:
        raise GateError(f"unknown stage '{stage}' (one of: {', '.join(STAGES)})")
    marker = load_marker(target, run)
    if marker["status"] != stage:
        raise GateError(
            f"cannot run '{stage}': current status is '{marker['status']}'")
    _invoke_claude(target, run, stage)
    return record_stage(target, run, stage)


def run_stage(target, run, stage, chain=False):
    """Invoke the headless claude call that drives `stage` forward, then
    record_stage() its artifact. With chain=False, runs exactly one stage
    and returns whatever record_stage returns (evals.ok may be false;
    status won't have advanced past `stage` in that case).

    With chain=True, keeps going: after a stage's eval passes, if the
    marker's new status names another stage (not a gate like
    "awaiting-scope-approval"/"awaiting-signoff", and not "done"), that
    stage is run next in the same call. Stops the moment a stage's eval
    fails (evals.ok: false - status doesn't advance) or the marker lands on
    a gate/"done"."""
    while True:
        marker = _run_one_stage(target, run, stage)
        stamp = marker["stages"].get(stage) or {}
        if not chain or not stamp.get("evals", {}).get("ok"):
            return marker
        status = marker["status"]
        if status not in STAGES:
            return marker  # hit a gate (awaiting-*) or reached "done"
        stage = status


def _cli_new(rest):
    target, run, title = rest[0], rest[1], rest[2]
    brief = rest[rest.index("--brief") + 1] if "--brief" in rest else ""
    pm = rest[rest.index("--pm") + 1] if "--pm" in rest else ""
    return new(target, run, title, brief=brief, pm=pm)


def _cli_run(rest):
    target, run, stage = rest[0], rest[1], rest[2]
    return run_stage(target, run, stage, chain="--chain" in rest[3:])


CMDS = {
    "new": _cli_new,
    "status": lambda rest: load_marker(rest[0], rest[1]),
    "record": lambda rest: record_stage(rest[0], rest[1], rest[2]),
    "approve-scope": lambda rest: approve_scope(rest[0], rest[1],
                                                ack_pii="--ack-pii" in rest[2:]),
    "approve-prd": lambda rest: approve_prd(rest[0], rest[1]),
    "revise": lambda rest: revise(rest[0], rest[1], rest[2]),
    "run": _cli_run,
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
