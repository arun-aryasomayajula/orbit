# Task 1: engine/spend.py - atomic accounting for `.spend-<date>` /
# `.count-<date>`, keeping the exact on-disk formats bin/orbit and
# command_center.py already read (see engine/spend.py's own docstring).
#
# Run: python3 -m pytest tests/test_spend.py -v   (from orbit/)
import multiprocessing as mp
import subprocess
import sys
import time
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

import locks  # noqa: E402
import spend  # noqa: E402

CTX = mp.get_context("fork")


def _cli(*args):
    return subprocess.run([sys.executable, str(ENGINE / "spend.py"), *args],
                           capture_output=True, text=True)


# ── add ──────────────────────────────────────────────────────────────────

def test_add_writes_4dp_float_string(tmp_path):
    r = _cli("add", str(tmp_path), "2026-08-22", "1.5")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".spend-2026-08-22").read_text() == "1.5000"
    assert r.stdout.strip() == "1.5000"


def test_add_accumulates_across_calls(tmp_path):
    _cli("add", str(tmp_path), "2026-08-22", "1.5")
    r = _cli("add", str(tmp_path), "2026-08-22", "0.25")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".spend-2026-08-22").read_text() == "1.7500"


def test_add_starts_from_zero_when_absent(tmp_path):
    total = spend.add(tmp_path, "2026-08-22", 0.3)
    assert total == pytest.approx(0.3)


def test_add_cli_prints_per_cycle_cost_not_running_total(tmp_path):
    """run.sh's caller logs THIS printed value as "cycle cost" - it must
    never drift into the accumulated total (an adversarial-review catch:
    the original CLI printed the post-add total instead)."""
    r1 = _cli("add", str(tmp_path), "2026-08-22", "1.50")
    assert r1.returncode == 0, r1.stderr
    assert r1.stdout.strip() == "1.5000"

    r2 = _cli("add", str(tmp_path), "2026-08-22", "0.25")
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.strip() == "0.2500"  # per-cycle, not "1.7500"

    # The running total is untouched on disk either way.
    assert (tmp_path / ".spend-2026-08-22").read_text() == "1.7500"


def test_add_rejects_nan(tmp_path):
    r = _cli("add", str(tmp_path), "2026-08-22", "nan")
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (tmp_path / ".spend-2026-08-22").exists()


def test_add_rejects_infinity(tmp_path):
    r = _cli("add", str(tmp_path), "2026-08-22", "inf")
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (tmp_path / ".spend-2026-08-22").exists()


# ── count ────────────────────────────────────────────────────────────────

def test_count_zero_when_absent(tmp_path):
    r = _cli("count", str(tmp_path), "2026-08-22")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0"


def test_count_reads_existing_file(tmp_path):
    (tmp_path / ".count-2026-08-22").write_text("7")
    r = _cli("count", str(tmp_path), "2026-08-22")
    assert r.returncode == 0
    assert r.stdout.strip() == "7"


# ── bump-count ───────────────────────────────────────────────────────────

def test_bump_count_increments_under_cap(tmp_path):
    r = _cli("bump-count", str(tmp_path), "2026-08-22", "--cap", "5")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "1"
    assert (tmp_path / ".count-2026-08-22").read_text() == "1"


def test_bump_count_refuses_exactly_at_cap_without_incrementing(tmp_path):
    (tmp_path / ".count-2026-08-22").write_text("3")
    r = _cli("bump-count", str(tmp_path), "2026-08-22", "--cap", "3")
    assert r.returncode == 3
    assert r.stdout.strip() == "3"
    assert (tmp_path / ".count-2026-08-22").read_text() == "3"  # unchanged


def test_bump_count_allows_one_below_cap(tmp_path):
    (tmp_path / ".count-2026-08-22").write_text("2")
    r = _cli("bump-count", str(tmp_path), "2026-08-22", "--cap", "3")
    assert r.returncode == 0
    assert (tmp_path / ".count-2026-08-22").read_text() == "3"


# ── concurrency ──────────────────────────────────────────────────────────

def _add_n_times(state_dir, date, n, cost):
    for _ in range(n):
        spend.add(state_dir, date, cost)


def test_concurrent_add_loses_nothing(tmp_path):
    date = "2026-08-22"
    procs = [CTX.Process(target=_add_n_times, args=(str(tmp_path), date, 25, 0.01))
             for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)

    assert all(p.exitcode == 0 for p in procs)
    total = float((tmp_path / f".spend-{date}").read_text())
    assert total == pytest.approx(0.5, abs=1e-6)  # 2 procs * 25 * 0.01


def _bump_worker(state_dir, date, cap, results, idx):
    bumped, _ = spend.bump_count(state_dir, date, cap)
    results[idx] = bumped


def test_bump_count_cap_boundary_under_concurrency(tmp_path):
    """The defining guarantee: spawn 2N processes racing bump-count at cap
    N - exactly N must succeed, no matter the interleaving."""
    date = "2026-08-22"
    cap = 10
    n_procs = 2 * cap

    manager = CTX.Manager()
    results = manager.list([None] * n_procs)
    procs = [CTX.Process(target=_bump_worker, args=(str(tmp_path), date, cap, results, i))
              for i in range(n_procs)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)

    assert all(p.exitcode == 0 for p in procs)
    succeeded = sum(1 for r in results if r)
    assert succeeded == cap
    assert int((tmp_path / f".count-{date}").read_text()) == cap


# ── lock contention surfaces as exit 4, not a traceback ─────────────────

def _hold_spend_lock_for(lock_path, seconds, ready_evt):
    with locks.hold(lock_path, timeout=10):
        ready_evt.set()
        time.sleep(seconds)


def test_cli_exits_4_on_lock_timeout(tmp_path, monkeypatch):
    """A LockTimeout from spend.py's own lock must never escape main() as
    an uncaught traceback (exit 1, colliding with the CLI's own exit-1
    space) - it gets one stderr line and a distinct exit code (4) so a
    caller can tell "someone else is mid-write" apart from a cap refusal
    (3) or a bad argument (2)."""
    monkeypatch.setattr(spend, "DEFAULT_TIMEOUT", 0.2)
    lock_path = str(tmp_path / ".spend.lock")  # matches spend._lock_path()

    started = CTX.Event()
    p = CTX.Process(target=_hold_spend_lock_for, args=(lock_path, 2, started))
    p.start()
    try:
        assert started.wait(5)
        rc = spend.main(["add", str(tmp_path), "2026-08-22", "1.0"])
        assert rc == 4
    finally:
        p.join(5)
