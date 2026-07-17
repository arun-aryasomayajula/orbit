#!/usr/bin/env python3
"""intake.py — zero-day onboarding: survey a repo into a working Orbit profile.

`orbit intake <target>` runs AFTER `orbit init` and closes the cold-start gap
(init scaffolds an EMPTY backlog and placeholder tracks; a human had to fill
both by hand). Intake does it in two parts:

  1. DETERMINISTIC (no model): execute every configured gate and record
     pass/fail in state/intake/gates-report.txt. If NO gates are configured,
     inject the test-bootstrap proposal — "establish a smoke suite and wire it
     as the first gate" — because a loop without a gate has no definition of
     done and must not run.
  2. AGENT (one headless Claude Code call, /orbit-intake): survey the repo,
     replace the placeholder tracks with real repo facts, and write candidate
     tasks (each with evidence) to state/intake/proposals.json.

Then intake merges the proposals into backlog.yaml — ALWAYS as
`status: proposed` + `autopilot: human`. Intake output is leads for a human to
triage on the dashboard, never queued work. Re-running is idempotent (ids
already in the backlog are skipped).

Usage:  intake.py <target-repo> [--no-agent] [--dry-run] [--timeout SECONDS]
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

from backlog_append import append_tasks

ENGINE = Path(__file__).resolve().parent
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or ENGINE.parent)

GATE_TIMEOUT = 900          # per-gate cap; a hung gate must not hang intake
AGENT_TIMEOUT = 1800        # default cap for the survey call

BOOTSTRAP_TASK = {
    "id": "bootstrap-test-gate",
    "title": "Establish a smoke test suite and wire it in as the first gate",
    "category": "testing",
    "priority": "high",
    "context": ("This repo has NO gates configured, so Orbit has no definition of done "
                "and will not work tasks against it. Create a minimal smoke suite that "
                "exercises the core entry points, make it runnable with one command, and "
                "add that command as the `test` gate in .autopilot/config.yaml."),
    "evidence": "config.yaml gates: block is empty (orbit intake, deterministic check)",
    "acceptance_criteria": [
        "a one-command test run exists and passes locally (documented in the repo)",
        "the new tests fail when a core behaviour is deliberately broken",
        ".autopilot/config.yaml gates: runs that command",
    ],
}


def run_gates(repo: Path, cfg: dict, out_dir: Path) -> list[tuple[str, bool, str]]:
    """Execute each configured gate once; report (name, passed, tail-of-output)."""
    results = []
    for name, g in (cfg.get("gates") or {}).items():
        cmd, cwd = g.get("cmd", ""), repo / (g.get("cwd") or ".")
        try:
            # shell=True is deliberate: gate cmds are operator-authored shell lines
            # from the repo's own config.yaml (env prefixes, `--` args) — the same
            # trust level as the operator's shell, and how the checker runs them.
            p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                               text=True, timeout=GATE_TIMEOUT)
            tail = ((p.stdout or "") + (p.stderr or ""))[-2000:]
            results.append((name, p.returncode == 0, tail))
        except subprocess.TimeoutExpired:
            results.append((name, False, f"TIMEOUT after {GATE_TIMEOUT}s"))
        except OSError as e:
            results.append((name, False, str(e)))
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / "gates-report.txt"
    with report.open("w") as f:
        for name, okay, tail in results:
            f.write(f"== gate {name}: {'PASS' if okay else 'FAIL'} ==\n{tail}\n\n")
    return results


def run_survey_agent(target: Path, cfg: dict, timeout: int) -> bool:
    """One headless Claude Code call: fill tracks + write proposals.json."""
    claude = shutil.which("claude")
    if not claude:
        print("  ✗ claude not on PATH — skipping the survey agent (deterministic part only)")
        return False
    env = dict(os.environ)
    ap = target / ".autopilot"
    env.update(AP_HOME=str(ap), AP_STATE=str(ap / "state"), ORBIT_HOME=str(ORBIT_HOME))
    cmd = [claude, "--print", "/orbit-intake",
           "--permission-mode", cfg.get("permission_mode", "acceptEdits"),
           "--settings", str(ORBIT_HOME / "config" / "orbit.settings.json"),
           "--model", cfg.get("model", "claude-opus-4-8"),
           "--add-dir", str(ORBIT_HOME)]
    print(f"  · survey agent: {' '.join(shlex.quote(c) for c in cmd[:3])} … (≤{timeout}s)")
    try:
        p = subprocess.run(cmd, cwd=target, env=env, timeout=timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL)
        return p.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"  ✗ survey agent timed out after {timeout}s — merging whatever it wrote")
        return False


def load_proposals(state: Path) -> list[dict]:
    f = state / "intake" / "proposals.json"
    if not f.exists():
        return []
    try:
        doc = json.loads(f.read_text())
        return list(doc.get("proposals") or [])
    except (ValueError, AttributeError) as e:
        print(f"  ✗ {f} is not valid proposals JSON ({e}) — ignoring it")
        return []


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    target = Path(args[0]).resolve()
    ap = target / ".autopilot"
    cfg_path = ap / "config.yaml"
    if not cfg_path.exists():
        sys.exit(f"no {cfg_path} — run `orbit init {target}` first")
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    state = ap / "state"
    dry = "--dry-run" in argv
    no_agent = "--no-agent" in argv
    timeout = AGENT_TIMEOUT
    for i, a in enumerate(argv):
        if a == "--timeout" and i + 1 < len(argv):
            timeout = int(argv[i + 1])

    print(f"ORBIT INTAKE — {target}\n")

    # 1. deterministic: verify the gates, bootstrap if there are none
    proposals: list[dict] = []
    gates = run_gates(target, cfg, state / "intake")
    if not gates:
        print("  ✗ no gates configured — injecting the test-bootstrap proposal")
        proposals.append(dict(BOOTSTRAP_TASK))
    for name, okay, _ in gates:
        print(f"  {'✓' if okay else '✗'} gate {name}: {'PASS' if okay else 'FAIL (fix before running the loop)'}")

    # 2. agent survey (fills tracks in place; proposals land in proposals.json)
    if not no_agent:
        run_survey_agent(target, cfg, timeout)
        proposals.extend(load_proposals(state))
    else:
        print("  · --no-agent: skipping the survey (deterministic part only)")

    # 3. merge into the backlog — proposed/human ONLY; a human queues work
    added, skipped = append_tasks(ap / "backlog.yaml", proposals, "intake",
                                  "auto-ingested by orbit intake", dry_run=dry)
    print(f"\n  proposals: {len(proposals)} | merged: {len(added)} | skipped: {len(skipped)}")
    for tid in added:
        print(f"    + {tid}")
    for tid, why in skipped:
        print(f"    · skipped {tid}: {why}")
    if dry:
        print("  (--dry-run: backlog.yaml NOT modified)")
    print("\n  next: triage the proposals on the dashboard (promote what's real), "
          "review .autopilot/tracks/, then `orbit run`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
