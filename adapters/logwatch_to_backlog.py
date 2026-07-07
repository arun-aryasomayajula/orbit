#!/usr/bin/env python3
"""logwatch_to_backlog.py — turn production-log findings into proposed bug tasks.

Reads logwatch's deduped signature ledger (ops/logwatch/SEEN.md) and APPENDS a
`status: proposed`, `autopilot: human`, `source: logwatch` bug task to
backlog.yaml for each NEW signature. The human triages on the command-center
board (some signatures may already be fixed — these are leads, not verified
work) and promotes a real one by setting status/autopilot + acceptance_criteria.

Appends raw text (does NOT round-trip the YAML) so backlog.yaml's comments and
existing tasks are preserved. Idempotent: skips signatures already ingested.

Usage:  python3 logwatch_to_backlog.py            # append new bug tasks
        python3 logwatch_to_backlog.py --dry-run
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

import yaml

AP = Path(__file__).resolve().parent
REPO = AP.parent.parent
BACKLOG = AP / "backlog.yaml"
SEEN = REPO / "ops" / "logwatch" / "SEEN.md"


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:48].rstrip("-")


def parse_seen() -> list[dict]:
    """Parse the `| date | app | signature | count |` table in SEEN.md."""
    if not SEEN.exists():
        return []
    rows = []
    for ln in SEEN.read_text().splitlines():
        ln = ln.strip()
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) != 4 or cells[0] in ("date", "") or set(cells[0]) <= set("-"):
            continue  # header / separator
        date, app, sig, count = cells
        rows.append({"date": date, "app": app, "sig": sig, "count": count})
    return rows


def existing_ids() -> set[str]:
    try:
        return {str(t.get("id")) for t in (yaml.safe_load(BACKLOG.read_text()) or {}).get("tasks", [])}
    except Exception:
        return set()


def yq(s: str) -> str:
    """Quote a scalar for a YAML double-quoted string."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_task(row: dict, tid: str) -> str:
    ctx = (f"Auto-ingested from production logs (logwatch). App: {row['app']}, "
           f"count {row['count']}, first seen {row['date']}. Signature:\n"
           f"  {row['sig']}\n"
           f"See ops/logwatch/FINDINGS.md for the full analysis + code correlation. "
           f"NOTE: a lead, not verified-open — triage first (it may already be fixed).")
    return f"""
  - id: {tid}
    title: {yq('[LOG] ' + row['sig'][:80])}
    category: bug
    autopilot: human
    status: proposed
    priority: medium
    source: logwatch
    context: |
      {ctx.replace(chr(10), chr(10) + '      ')}
    acceptance_criteria:
      - "(triage from ops/logwatch/FINDINGS.md, then scope the fix + a regression test)"
"""


def main() -> int:
    dry = "--dry-run" in sys.argv
    rows = parse_seen()
    have = existing_ids()
    new_blocks, added = [], []
    seen_sigs = set()
    for r in rows:
        tid = "logwatch-" + slug(r["sig"])
        if tid in have or tid in seen_sigs:
            continue
        seen_sigs.add(tid)
        new_blocks.append(render_task(r, tid))
        added.append(tid)

    print(f"SEEN signatures: {len(rows)} | already in backlog: {len(rows)-len(added)} | new: {len(added)}")
    for tid in added:
        print(f"  + {tid}")
    if not added:
        print("nothing new to add."); return 0
    if dry:
        print("(--dry-run: backlog.yaml NOT modified)"); return 0
    with BACKLOG.open("a") as f:
        f.write("\n  # ── auto-ingested from production logs (logwatch) — triage before promoting ──")
        for b in new_blocks:
            f.write(b)
    print(f"appended {len(added)} proposed bug task(s) to {BACKLOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
