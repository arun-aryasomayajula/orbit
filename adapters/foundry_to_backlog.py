#!/usr/bin/env python3
"""foundry_to_backlog.py — fold foundry's maturity tasks into backlog.yaml.

`foundry score --tasks` regenerates .foundry/tasks.json with coding-maturity
tasks (testing, docs, deps, code_quality, …). This feeder turns each into a
backlog.yaml task so backlog.yaml stays the SINGLE source of truth (foundry,
logwatch and /backlog-research all feed INTO it; backlog_to_tasks.py then builds
the loop queue from backlog alone).

EVERYTHING lands as `status: proposed` — the same intake state as logwatch /
/backlog-research — so NOTHING auto-enters the loop without a human flipping it to
`status: queued`. The `autopilot` gate is PRE-SET by category as a hint:
  * SAFE category  → autopilot: allow   (one-field flip to queued = loop-pickable)
  * other category → autopilot: human   (stays board-only even if you queue it)
This is deliberate: foundry's `code_quality` bucket mixes safe lint config with
risky core-module refactors, so a human eyeballs each before it can ship.

Idempotent + comment-preserving: appends raw text for NEW tasks only, keyed by a
content slug (foundry renumbers ids every run, so we never key off its numeric id).

Usage:  python3 foundry_to_backlog.py            # append new maturity tasks
        python3 foundry_to_backlog.py --dry-run
"""
from __future__ import annotations
import json
import os
import re
import sys
from pathlib import Path

import yaml

# Adapter contract (see backlog_to_tasks.run_source_adapters): AP_HOME/AP_STATE/
# ORBIT_HOME are exported and cwd is the target repo root.
if not os.environ.get("AP_HOME"):
    sys.exit("AP_HOME unset — adapters run via backlog_to_tasks (or set AP_HOME=<repo>/.autopilot)")
AP_HOME = Path(os.environ["AP_HOME"])
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ORBIT_HOME / "engine"))
from backlog_append import append_raw_blocks

REPO = AP_HOME.parent
BACKLOG = AP_HOME / "backlog.yaml"
FOUNDRY = REPO / ".foundry" / "tasks.json"

# Categories the loop may auto-ship — must match backlog_to_tasks.SAFE_CATEGORIES.
SAFE_CATEGORIES = {
    "testing", "documentation", "dependencies", "code_quality", "developer_experience",
}


def slug(s: str) -> str:
    """Stable kebab id fragment from free text (foundry's numeric id is volatile)."""
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:56].rstrip("-")


def split_task(task: str) -> tuple[str, str]:
    """foundry packs '<problem> -- <recommendation>' into one string. Split it;
    the problem becomes the title, the recommendation the WHY + acceptance."""
    parts = re.split(r"\s+--\s+", task, maxsplit=1)
    problem = parts[0].strip()
    rec = parts[1].strip() if len(parts) > 1 else ""
    return problem, rec


def existing_ids() -> set[str]:
    try:
        return {str(t.get("id")) for t in (yaml.safe_load(BACKLOG.read_text()) or {}).get("tasks", [])}
    except Exception:
        return set()


def yq(s: str) -> str:
    """Quote a scalar for a YAML double-quoted string."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_task(t: dict, tid: str) -> str:
    """Render one foundry task as a backlog.yaml block (gated by category)."""
    problem, rec = split_task(t.get("task", ""))
    cat = (t.get("category") or "code_quality").strip() or "code_quality"
    safe = cat in SAFE_CATEGORIES
    autopilot = "allow" if safe else "human"   # gate HINT (pre-set by category)
    status = "proposed"                          # intake: human promotes to `queued`
    priority = t.get("priority", "medium")
    ref = t.get("ref", "")

    ctx = (f"Auto-folded from `foundry score --tasks` (coding-maturity scorecard).\n"
           f"Problem: {problem}\n")
    if rec:
        ctx += f"Recommended fix: {rec}\n"
    if ref:
        ctx += f"Scorecard: {ref}"
    ctx = ctx.rstrip("\n")

    acc = rec if rec else problem
    block = f"""
  - id: {tid}
    title: {yq('[MAT] ' + problem[:80])}
    category: {cat}
    autopilot: {autopilot}
    status: {status}
    priority: {priority}
    source: maturity-score
    context: |
      {ctx.replace(chr(10), chr(10) + '      ')}
    acceptance_criteria:
      - {yq(acc)}
      - "add/extend a regression test where the change is testable"
"""
    return block, safe


def main() -> int:
    dry = "--dry-run" in sys.argv
    if not FOUNDRY.exists():
        print(f"no {FOUNDRY} — run `foundry score --tasks` first."); return 1
    tasks = json.loads(FOUNDRY.read_text()).get("tasks", [])
    have = existing_ids()

    blocks, added, loopable = [], [], 0
    seen = set()
    for t in tasks:
        problem, _ = split_task(t.get("task", ""))
        if not problem:
            continue
        tid = "mat-" + slug(problem)
        if not tid or tid in have or tid in seen:
            continue
        seen.add(tid)
        block, safe = render_task(t, tid)
        blocks.append(block)
        added.append((tid, safe))
        loopable += 1 if safe else 0

    print(f"foundry tasks: {len(tasks)} | already in backlog: {len(tasks)-len(added)} | new: {len(added)}")
    print(f"  all land as status:proposed → {loopable} pre-gated autopilot:allow "
          f"(flip to queued = loop-pickable) | {len(added)-loopable} autopilot:human (board-only)")
    for tid, safe in added:
        print(f"  + {tid}  [{'allow' if safe else 'human'}]")
    if not added:
        print("nothing new to add."); return 0
    if dry:
        print("(--dry-run: backlog.yaml NOT modified)"); return 0
    append_raw_blocks(BACKLOG, "auto-folded from `foundry score --tasks` (maturity scorecard)", blocks)
    print(f"appended {len(added)} maturity task(s) to {BACKLOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
