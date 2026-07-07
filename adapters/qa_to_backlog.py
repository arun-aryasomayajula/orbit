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
import re
import sys
from pathlib import Path

import yaml

AP = Path(__file__).resolve().parent
REPO = AP.parent.parent
BACKLOG = REPO / "ops" / "autopilot" / "backlog.yaml"
SEEN = AP / "SEEN.md"

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


def render_task(row: dict, tid: str) -> str:
    is_bug = row["type"] == "bug"
    category = "bug" if is_bug else "feature"
    autopilot = "review-only" if is_bug else "human"
    priority = _SEV2PRI.get(row["severity"], "medium")
    tag = {"bug": "[UI BUG]", "ux": "[UX]", "feature": "[UI FEAT]"}.get(row["type"], "[UI]")
    ctx = (f"Auto-ingested from gstack QA/design review (browse). Page: {row['page']}, "
           f"severity {row['severity']}, first seen {row['date']}. Finding:\n"
           f"  {row['sig']}\n"
           f"See ops/qa/FINDINGS.md for the full analysis + screenshots. "
           f"NOTE: a lead — triage before promoting.")
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
    blocks, added, seen = [], [], set()
    for r in rows:
        tid = "uitest-" + slug(r["sig"])
        if tid in have or tid in seen:
            continue
        seen.add(tid)
        blocks.append(render_task(r, tid))
        added.append(tid)

    print(f"QA findings: {len(rows)} | already in backlog: {len(rows)-len(added)} | new: {len(added)}")
    for tid in added:
        print(f"  + {tid}")
    if not added:
        print("nothing new to add."); return 0
    if dry:
        print("(--dry-run: backlog.yaml NOT modified)"); return 0
    with BACKLOG.open("a") as f:
        f.write("\n  # ── auto-ingested from gstack QA / design review (browse) — triage before promoting ──")
        for b in blocks:
            f.write(b)
    print(f"appended {len(added)} proposed UI task(s) to {BACKLOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
