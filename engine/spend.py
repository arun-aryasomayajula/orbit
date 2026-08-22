#!/usr/bin/env python3
"""spend.py - atomic accounting for autopilot's two per-day counters,
replacing run.sh's own inline `record_spend`/`.count-<date>` shell+python
snippets (see run.sh's `record_spend`, ~line 92, and the `.count-<date>`
read/increment around the daily-cap check, ~line 382) with one lock-guarded
implementation both the wrapper and any concurrent slot loop (M4) can call
without racing each other.

On-disk formats are UNCHANGED on purpose - bin/orbit and
command_center.py's spend/count readers (`float(...)`/`int(...)` on
stripped file text) keep working untouched:
    <state_dir>/.spend-<date>   a bare float string, e.g. "1.2340"
                                 (4 decimal places, matching run.sh's
                                 historical `f"{prev+cost:.4f}"`)
    <state_dir>/.count-<date>   a bare int string, e.g. "3"

Why locks.hold() and not just "write is one syscall so it's probably fine":
`add` and `bump-count` are read-modify-write - two cycles finishing at the
same instant (or N slot-loop workers under M4) can both read the same
"before" value and each write their own "+1", silently losing one update.
`count` also takes the lock: a plain read racing a concurrent writer's
truncate+write could otherwise observe a torn/partial file.

CLI:
    spend.py add <state_dir> <date> <cost>
        Adds float(<cost>) to .spend-<date> (created at 0 if absent).
        Prints the new total. Exit 0.
    spend.py count <state_dir> <date>
        Prints .count-<date> as an int; 0 if absent or unparseable.
        Exit 0.
    spend.py bump-count <state_dir> <date> --cap N
        One critical section: iff the current count is < N, writes
        count+1 and exits 0 (prints the new count); otherwise writes
        NOTHING and exits 3 (prints the unchanged count) - the cap is
        never exceeded even under concurrent callers, because the check
        and the increment never straddle a lock release.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import locks

DEFAULT_TIMEOUT = 30


def _spend_path(state_dir: Path, date: str) -> Path:
    return state_dir / f".spend-{date}"


def _count_path(state_dir: Path, date: str) -> Path:
    return state_dir / f".count-{date}"


def _lock_path(state_dir: Path, name: str) -> Path:
    return state_dir / f".{name}.lock"


def _write_atomic(path: Path, text: str) -> None:
    """tmp-write + os.replace so a reader never observes a truncated file
    mid-write (belt-and-suspenders alongside the lock - readers outside
    this module, like bin/orbit's plain `cat`, don't take the lock)."""
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def _read_float(path: Path) -> float:
    if not path.exists():
        return 0.0
    try:
        return float(path.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def _read_int(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return int(path.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def add(state_dir, date: str, cost: float) -> float:
    """Atomically add `cost` to today's (well, `date`'s) spend total.
    Returns the new total."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _spend_path(state_dir, date)
    with locks.hold(_lock_path(state_dir, "spend"), timeout=DEFAULT_TIMEOUT):
        total = _read_float(path) + float(cost)
        _write_atomic(path, f"{total:.4f}")
    return total


def count(state_dir, date: str) -> int:
    """Today's (well, `date`'s) task count - 0 if the file is absent."""
    state_dir = Path(state_dir)
    path = _count_path(state_dir, date)
    with locks.hold(_lock_path(state_dir, "count"), timeout=DEFAULT_TIMEOUT):
        return _read_int(path)


def bump_count(state_dir, date: str, cap: int):
    """One critical section: (current < cap) ? increment-and-True :
    leave-unchanged-and-False. Returns (bumped: bool, count: int) where
    `count` is the value AFTER the call either way."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _count_path(state_dir, date)
    with locks.hold(_lock_path(state_dir, "count"), timeout=DEFAULT_TIMEOUT):
        current = _read_int(path)
        if current >= cap:
            return False, current
        new = current + 1
        _write_atomic(path, str(new))
        return True, new


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="spend.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("state_dir")
    p_add.add_argument("date")
    p_add.add_argument("cost", type=float)

    p_count = sub.add_parser("count")
    p_count.add_argument("state_dir")
    p_count.add_argument("date")

    p_bump = sub.add_parser("bump-count")
    p_bump.add_argument("state_dir")
    p_bump.add_argument("date")
    p_bump.add_argument("--cap", type=int, required=True)

    args = parser.parse_args(argv)

    if args.cmd == "add":
        total = add(args.state_dir, args.date, args.cost)
        print(f"{total:.4f}")
        return 0

    if args.cmd == "count":
        print(count(args.state_dir, args.date))
        return 0

    if args.cmd == "bump-count":
        bumped, new_count = bump_count(args.state_dir, args.date, args.cap)
        print(new_count)
        return 0 if bumped else 3

    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
