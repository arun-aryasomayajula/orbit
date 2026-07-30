#!/usr/bin/env python3
"""qa_to_backlog.py — turn gstack QA / design-review findings into backlog tasks.

Reads the deduped QA findings ledger (ops/qa/SEEN.md) and APPENDS a
`status: proposed`, `source: ui-test` task to backlog.yaml for each NEW finding,
so UI bugs + UX feedback flow into the same agentic discovery pipeline as
foundry (maturity) and logwatch (prod errors). The operator triages on the
command-center board (filter chip "🧪 UI-test") and promotes the real ones.

SEEN.md table columns:  | date | page | type | signature | severity |
  type     = bug | ux | feature
  severity = critical | high | medium | low

Mapping:
  type=bug      → category bug,     autopilot review-only  (loop can DRAFT a fix)
  type=ux/feature → category feature, autopilot human       (needs design judgment)
  severity → priority (critical/high→high, medium→medium, low→low)

Appends raw text (does NOT round-trip the YAML) so comments + existing tasks are
preserved. Idempotent: skips signatures already ingested (keyed by id).

Usage:  python3 qa_to_backlog.py            # append new tasks
        python3 qa_to_backlog.py --dry-run
"""
from __future__ import annotations
import json
import os
import re
import sys
from datetime import datetime
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

AP_STATE = Path(os.environ.get("AP_STATE") or AP_HOME / "state")
REPO = AP_HOME.parent
BACKLOG = AP_HOME / "backlog.yaml"
SEEN = REPO / "ops" / "qa" / "SEEN.md"
MARKERS = AP_STATE / "merge_markers.jsonl"    # written by the dashboard's merge action
ATTRIBUTION_WINDOW_DAYS = 7                   # merge → first-seen gap that still reads as suspect

_SEV2PRI = {"critical": "high", "high": "high", "medium": "medium", "low": "low"}


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:48].rstrip("-")


def parse_seen() -> list[dict]:
    """Parse the `| date | page | type | signature | severity |` table."""
    if not SEEN.exists():
        return []
    rows = []
    for ln in SEEN.read_text().splitlines():
        ln = ln.strip()
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) != 5 or cells[0] in ("date", "") or set(cells[0]) <= set("-"):
            continue  # header / separator
        rows.append({"date": cells[0], "page": cells[1], "type": cells[2].lower(),
                     "sig": cells[3], "severity": cells[4].lower()})
    return rows


def existing_ids() -> set[str]:
    try:
        return {str(t.get("id")) for t in (yaml.safe_load(BACKLOG.read_text()) or {}).get("tasks", [])}
    except Exception:
        return set()


def yq(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def load_merge_markers() -> list[dict]:
    """Ships the operator merged (dashboard writes one line per merge)."""
    if not MARKERS.exists():
        return []
    out = []
    for ln in MARKERS.read_text().splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def suspects_for(row: dict, markers: list[dict]) -> list[dict]:
    """Merges that landed BEFORE this finding was first seen, within the
    attribution window — suspicion with evidence, never a verdict."""
    try:
        seen = datetime.strptime(row["date"][:10], "%Y-%m-%d")
    except (ValueError, KeyError):
        return []
    hits = []
    for m in markers:
        try:
            merged = datetime.strptime((m.get("at") or "")[:10], "%Y-%m-%d")
        except ValueError:
            continue
        if merged <= seen and (seen - merged).days <= ATTRIBUTION_WINDOW_DAYS:
            hits.append((merged, m))
    return [m for _, m in sorted(hits, key=lambda x: x[0], reverse=True)[:3]]


def attribution_lines(suspects: list[dict]) -> str:
    if not suspects:
        return ""
    lines = ["POSSIBLE REGRESSION — merged shortly before this was first seen "
             "(suspicion, not a verdict; triage against each diff):"]
    for m in suspects:
        bits = [f"task {m.get('task_id')}", f"merged {(m.get('at') or '')[:10]}"]
        if m.get("branch"):
            bits.append(m["branch"])
        if m.get("pr_url"):
            bits.append(m["pr_url"])
        if m.get("patch"):
            bits.append(f"revert patch: {m['patch']}")
        lines.append("  - " + ", ".join(bits))
    return "\n" + "\n".join(lines)


def render_task(row: dict, tid: str, suspects: list[dict] | None = None) -> str:
    is_bug = row["type"] == "bug"
    category = "bug" if is_bug else "feature"
    autopilot = "review-only" if is_bug else "human"
    priority = _SEV2PRI.get(row["severity"], "medium")
    tag = {"bug": "[UI BUG]", "ux": "[UX]", "feature": "[UI FEAT]"}.get(row["type"], "[UI]")
    ctx = (f"Auto-ingested from gstack QA/design review (browse). Page: {row['page']}, "
           f"severity {row['severity']}, first seen {row['date']}. Finding:\n"
           f"  {row['sig']}\n"
           f"See ops/qa/FINDINGS.md for the full analysis + screenshots. "
           f"NOTE: a lead — triage before promoting."
           + attribution_lines(suspects or []))
    return f"""
  - id: {tid}
    title: {yq(f'{tag} ' + row['sig'][:80])}
    category: {category}
    autopilot: {autopilot}
    status: proposed
    priority: {priority}
    source: ui-test
    context: |
      {ctx.replace(chr(10), chr(10) + '      ')}
    acceptance_criteria:
      - "(triage from ops/qa/FINDINGS.md, then scope the fix/feature + a test)"
"""


def main() -> int:
    dry = "--dry-run" in sys.argv
    rows = parse_seen()
    have = existing_ids()
    markers = load_merge_markers()
    blocks, added, seen = [], [], set()
    for r in rows:
        tid = "uitest-" + slug(r["sig"])
        if tid in have or tid in seen:
            continue
        seen.add(tid)
        sus = suspects_for(r, markers)
        if sus:
            print(f"  ! {tid}: first seen after {len(sus)} recent merge(s) — attributed in context")
        blocks.append(render_task(r, tid, sus))
        added.append(tid)

    print(f"QA findings: {len(rows)} | already in backlog: {len(rows)-len(added)} | new: {len(added)}")
    for tid in added:
        print(f"  + {tid}")
    if not added:
        print("nothing new to add."); return 0
    if dry:
        print("(--dry-run: backlog.yaml NOT modified)"); return 0
    append_raw_blocks(BACKLOG, "auto-ingested from gstack QA / design review (browse) — triage before promoting",
                      blocks)
    print(f"appended {len(added)} proposed UI task(s) to {BACKLOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
