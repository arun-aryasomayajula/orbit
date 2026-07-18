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
SEEN = REPO / "ops" / "logwatch" / "SEEN.md"
MARKERS = AP_STATE / "merge_markers.jsonl"    # written by the dashboard's merge action
LEDGER = AP_STATE / "ledger.json"
ATTRIBUTION_WINDOW_DAYS = 7                   # merge → first-seen gap that still reads as suspect


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
    """Merges that landed BEFORE this signature was first seen, within the
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
    ctx = (f"Auto-ingested from production logs (logwatch). App: {row['app']}, "
           f"count {row['count']}, first seen {row['date']}. Signature:\n"
           f"  {row['sig']}\n"
           f"See ops/logwatch/FINDINGS.md for the full analysis + code correlation. "
           f"NOTE: a lead, not verified-open — triage first (it may already be fixed)."
           + attribution_lines(suspects or []))
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
    markers = load_merge_markers()
    new_blocks, added = [], []
    seen_sigs = set()
    for r in rows:
        tid = "logwatch-" + slug(r["sig"])
        if tid in have or tid in seen_sigs:
            continue
        seen_sigs.add(tid)
        sus = suspects_for(r, markers)
        if sus:
            print(f"  ! {tid}: first seen after {len(sus)} recent merge(s) — attributed in context")
        new_blocks.append(render_task(r, tid, sus))
        added.append(tid)

    print(f"SEEN signatures: {len(rows)} | already in backlog: {len(rows)-len(added)} | new: {len(added)}")
    for tid in added:
        print(f"  + {tid}")
    if not added:
        print("nothing new to add."); return 0
    if dry:
        print("(--dry-run: backlog.yaml NOT modified)"); return 0
    append_raw_blocks(BACKLOG, "auto-ingested from production logs (logwatch) — triage before promoting",
                      new_blocks)
    print(f"appended {len(added)} proposed bug task(s) to {BACKLOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
