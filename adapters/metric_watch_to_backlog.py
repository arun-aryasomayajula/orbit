#!/usr/bin/env python3
"""metric_watch_to_backlog.py - cadence-based metric-watch signal adapter.

Turns a profile's `watches:` list (`.autopilot/config.yaml`, see
config/schema.yaml) into proposed backlog tasks on a per-watch cadence: each
watch names a standing question ("what's the WoW conversion delta?") that
should be re-asked into a fresh insights/ doc no more often than
`cadence_hours` (default 168 = weekly).

State (`$AP_STATE/watches.json`, `{name: last_filed_iso}`) tracks when each
watch last filed a task. Missing or corrupt state starts fresh (a stderr
warning, never a crash) - a watch with no recorded last_filed is due
immediately. Writes are atomic (tmp file + os.replace), and happen once per
watch that advances (not only at the end of the run), so a malformed later
entry can never cost an earlier, successfully processed watch its state.

Adapter contract (see backlog_to_tasks.run_source_adapters): appends
`status: proposed` tasks ONLY, via backlog_append - idempotent by id. A watch
past cadence always advances its state to "now", even when the id it would
file already exists in the backlog (append_tasks skips the duplicate); the
id is date-granular (`watch-<name>-<YYYYMMDD>`, UTC), so a sub-daily cadence
dedupes to at most one filed task per UTC day by design. Never queues work,
never mutates an existing task.

Test hook (no network, no model): ORBIT_METRIC_WATCH_NOW=<ISO8601> freezes
"now" so cadence-expiry and dated-id behaviour are deterministic to test.

Usage: python3 metric_watch_to_backlog.py [--dry-run]
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

# Adapter contract (see backlog_to_tasks.run_source_adapters): AP_HOME/AP_STATE/
# ORBIT_HOME are exported and cwd is the target repo root.
if not os.environ.get("AP_HOME"):
    sys.exit("AP_HOME unset - adapters run via backlog_to_tasks (or set AP_HOME=<repo>/.autopilot)")
AP_HOME = Path(os.environ["AP_HOME"])
AP_STATE = Path(os.environ.get("AP_STATE") or AP_HOME / "state")
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ORBIT_HOME / "engine"))
from backlog_append import append_tasks  # noqa: E402

BACKLOG = AP_HOME / "backlog.yaml"
STATE_FILE = AP_STATE / "watches.json"

DEFAULT_CADENCE_HOURS = 168
DEFAULT_CATEGORY = "insight"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
INSIGHT_LINT_CRITERION = "insights/ doc passes insight_lint (Answer + Basis, every number cited)"


# --- state -----------------------------------------------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError) as e:
        print(f"WARN: {STATE_FILE} unreadable/corrupt ({e}) - starting fresh", file=sys.stderr)
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, STATE_FILE)


def _now() -> datetime:
    override = os.environ.get("ORBIT_METRIC_WATCH_NOW")
    if override:
        dt = datetime.fromisoformat(override)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def is_due(name: str, state: dict, cadence_hours: int, now: datetime) -> bool:
    """A watch with no recorded last_filed is due immediately."""
    last = state.get(name)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return now - last_dt >= timedelta(hours=cadence_hours)


# --- proposal ----------------------------------------------------------------

def build_proposal(watch: dict, now: datetime, cadence: int) -> dict:
    name = watch["name"]
    category = watch.get("category") or DEFAULT_CATEGORY
    return {
        "id": f"watch-{name}-{now.strftime('%Y%m%d')}",
        "title": watch["question"],
        "category": category,
        "context": (f"Standing metric watch '{name}' (cadence: every {cadence}h) - "
                    "filed automatically when past cadence; see .autopilot/config.yaml "
                    "`watches:`."),
        "acceptance_criteria": [INSIGHT_LINT_CRITERION],
    }


def main() -> int:
    dry = "--dry-run" in sys.argv
    try:
        cfg = yaml.safe_load((AP_HOME / "config.yaml").read_text()) or {}
    except OSError:
        sys.exit("no config.yaml under AP_HOME")
    watches = cfg.get("watches") or []
    if not watches:
        print("metric_watch: no `watches:` configured - nothing to do")
        return 0

    state = load_state()
    now = _now()
    filed = deduped = 0
    for watch in watches:
        if not isinstance(watch, dict):
            print(f"WARN: malformed watch entry {watch!r} - skipping (see config/schema.yaml)",
                  file=sys.stderr)
            continue
        name = str(watch.get("name") or "").strip()
        question = str(watch.get("question") or "").strip()
        if not name or not NAME_RE.match(name) or not question:
            print(f"WARN: malformed watch entry {watch!r} - skipping (see config/schema.yaml)",
                  file=sys.stderr)
            continue
        # Single coercion site: a non-numeric cadence_hours (e.g. a typo'd
        # string) must warn-and-skip THIS watch, never raise mid-loop - an
        # uncaught exception here would abort the run before save_state and
        # cost every earlier, successfully processed watch its own state.
        try:
            cadence = int(watch.get("cadence_hours") or DEFAULT_CADENCE_HOURS)
        except (TypeError, ValueError):
            print(f"WARN: watch '{name}' has a non-numeric cadence_hours "
                  f"({watch.get('cadence_hours')!r}) - skipping (see config/schema.yaml)",
                  file=sys.stderr)
            continue
        if not is_due(name, state, cadence, now):
            continue
        proposal = build_proposal(watch, now, cadence)
        added, skipped = append_tasks(BACKLOG, [proposal], "metric_watch",
                                      f"auto-filed metric watch '{name}'", dry_run=dry)
        if added:
            print(f"  + watch '{name}' -> {proposal['id']} (proposed)" + (" (dry-run)" if dry else ""))
            filed += 1
        elif skipped:
            print(f"  - watch '{name}': {skipped[0][1]} - state still advances")
            deduped += 1
        # Idempotent-by-id, cadence-driven: the watch is past cadence, so its
        # clock advances to now regardless of whether the (possibly
        # same-day-duplicate) id actually landed in the backlog. Persisted
        # immediately (not batched to the end of the loop) so a later watch
        # being skipped - or any other error mid-loop - can never cost this
        # watch its advanced state.
        state[name] = now.isoformat()
        if not dry:
            save_state(state)

    print(f"metric_watch: {len(watches)} watch(es) configured | filed: {filed} | "
          f"deduped (already filed today): {deduped}")
    if dry:
        print("(--dry-run: backlog.yaml and watches.json NOT modified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
