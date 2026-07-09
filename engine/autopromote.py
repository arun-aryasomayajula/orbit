#!/usr/bin/env python3
"""autopromote.py — auto-feed the loop one task at a time.

Called by run.sh (only when AUTO_PROMOTE flag is present AND the loop queue is
empty). Promotes the single highest-priority `proposed` task in a conservative
set of categories to `status: queued` + `autopilot: allow`, then rebuilds the
queue. One task per call, so the loop drains the backlog steadily instead of
flooding. Skips anything already worked (ledger) or skipped.

Deliberately conservative: auto-promotes only low-risk, well-contained work
(testing/documentation/dependencies/bug/feature). It does NOT auto-promote
`code_quality`/`refactor` (which can include risky core rewrites) or any
human-only category — promote those by hand from the dashboard.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parent


def _ap_home() -> Path:
    # AP_HOME = <target-repo>/.autopilot (holds backlog.yaml, config.yaml, ...).
    home = os.environ.get("AP_HOME")
    if not home:
        sys.exit("AP_HOME unset — run inside a orbit target repo (set AP_HOME=<repo>/.autopilot)")
    return Path(home)


def _ap_state() -> Path:
    # AP_STATE = runtime state (ledger.json, skips.txt): env AP_STATE, else $AP_HOME/state.
    st = os.environ.get("AP_STATE")
    return Path(st) if st else _ap_home() / "state"


AP_HOME = _ap_home()
AP_STATE = _ap_state()
BACKLOG = AP_HOME / "backlog.yaml"
LEDGER = AP_STATE / "ledger.json"
SKIPS = AP_STATE / "skips.txt"
CONVERTER = ENGINE / "backlog_to_tasks.py"

AUTOPROMOTE_CATEGORIES = {"testing", "documentation", "dependencies", "bug"}
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _worked() -> set[str]:
    try:
        return set(json.loads(LEDGER.read_text()).get("entries", {}).keys())
    except Exception:
        return set()


def _skips() -> set[str]:
    if not SKIPS.exists():
        return set()
    return {ln.strip() for ln in SKIPS.read_text().splitlines() if ln.strip() and not ln.startswith("#")}


def main() -> int:
    backlog = (yaml.safe_load(BACKLOG.read_text()) or {}).get("tasks", []) or []
    worked, skips = _worked(), _skips()
    cands = [t for t in backlog
             if t.get("status") == "proposed"
             and t.get("category") in AUTOPROMOTE_CATEGORIES
             and t.get("autopilot") != "human"   # operator-gated tasks are never auto-fed
             and str(t.get("id")) not in worked
             and str(t.get("id")) not in skips]
    if not cands:
        print("autopromote: no eligible proposed task — nothing to feed.")
        return 0
    cands.sort(key=lambda t: PRIORITY_RANK.get(t.get("priority", "medium"), 1))
    pick = str(cands[0]["id"])

    # Block-level edit (preserve comments): set status queued + autopilot allow.
    # Indent-tolerant: real backlogs use both `- id:` at column 0 (block style)
    # and `  - id:` nested under `tasks:` — match either, and flip the key lines
    # at whatever indent the block uses.
    text = BACKLOG.read_text()
    parts = re.split(r"(?m)(?=^\s*- id: )", text)
    edited = False
    for i, blk in enumerate(parts):
        m = re.match(r"^\s*- id: (\S+)", blk)
        if m and m.group(1) == pick:
            blk, n_status = re.subn(r"(?m)^(\s*status: ).*$", r"\1queued", blk, count=1)
            blk = re.sub(r"(?m)^(\s*autopilot: ).*$", r"\1allow", blk, count=1)
            parts[i] = blk
            edited = n_status == 1
            break
    if not edited:
        print(f"autopromote: could not rewrite block for '{pick}' — backlog left unchanged.")
        return 1
    BACKLOG.write_text("".join(parts))
    subprocess.run([sys.executable, str(CONVERTER)], cwd=str(ENGINE), capture_output=True)
    print(f"autopromote: promoted '{pick}' ({cands[0].get('category')}, "
          f"p{cands[0].get('priority','medium')}) → queued+allow.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
