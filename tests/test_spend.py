# Task 1: engine/spend.py - atomic accounting for `.spend-<date>` /
# `.count-<date>`, keeping the exact on-disk formats bin/orbit and
# command_center.py already read (see engine/spend.py's own docstring).
#
# Run: python3 -m pytest tests/test_spend.py -v   (from orbit/)
import multiprocessing as mp
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

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
