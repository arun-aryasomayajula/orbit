# Task 1: engine/locks.py - the shared flock() critical-section helper.
#
# Every scenario here needs REAL, separate OS processes (an in-process
# thread test would never exercise flock()'s actual cross-process
# semantics, and would give the stale/live-owner tests nothing meaningful
# to distinguish). Processes are spawned with the "fork" context
# deliberately: it duplicates the already-imported `locks` module into the
# child instead of needing pytest's own test module to be re-importable by
# name under "spawn" - simpler and more robust for a hermetic test.
#
# Run: python3 -m pytest tests/test_locks.py -v   (from orbit/)
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

import locks  # noqa: E402

CTX = mp.get_context("fork")


# ── basic acquire/release + no lost updates ─────────────────────────────────

def _increment_n_times(lock_path, counter_path, n):
    for _ in range(n):
        with locks.hold(lock_path, timeout=10):
            current = int(Path(counter_path).read_text())
            time.sleep(0.001)  # widen the race window a lost-update bug needs
            Path(counter_path).write_text(str(current + 1))


def test_concurrent_increments_lose_nothing(tmp_path):
    lock_path = str(tmp_path / "counter.lock")
    counter_path = str(tmp_path / "counter")
    Path(counter_path).write_text("0")

    procs = [CTX.Process(target=_increment_n_times, args=(lock_path, counter_path, 25))
             for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)

    assert all(p.exitcode == 0 for p in procs)
    assert int(Path(counter_path).read_text()) == 50


# ── stale (dead-pid) lock, naturally released ───────────────────────────────

def _hold_and_die(lock_path, pid_file):
    """Acquires the real flock, records our own pid, then exits immediately
    without releasing anything explicitly - the kernel tears down our fd
    table on exit, which releases the flock on its own. This is the common
    case (a crashed or killed holder), and needs NO special "breaking" logic
    at all - hold() must simply notice the flock is free again."""
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(fd, json.dumps({"pid": os.getpid()}).encode())
    Path(pid_file).write_text(str(os.getpid()))
    os._exit(0)


def test_stale_dead_pid_lock_is_acquired_promptly(tmp_path):
    lock_path = str(tmp_path / "stale.lock")
    pid_file = tmp_path / "pid"

    p = CTX.Process(target=_hold_and_die, args=(lock_path, str(pid_file)))
    p.start()
    p.join(10)
    assert p.exitcode == 0

    dead_pid = int(pid_file.read_text())
    assert not locks._pid_alive(dead_pid)  # sanity: really dead, not reused

    start = time.monotonic()
    with locks.hold(lock_path, timeout=5, break_stale=True):
        pass
    assert time.monotonic() - start < 4


# ── stale (dead-pid) lock that flock() genuinely cannot reacquire ──────────
#
# This is the scenario break_stale actually exists for (see locks.py's
# module docstring): a lock fd leaked into a still-live process that never
# itself calls hold() again - NOT via subprocess/exec (PEP 446 makes fds
# close-on-exec by default, so a plain subprocess.Popen call doesn't hand
# a child the lock fd), but via a bare os.fork() with no following exec
# (multiprocessing's "fork" start method, or a service double-forking to
# daemonize) - means the flock is real and still held even though the
# RECORDED owner pid is dead. Reproducing the fork-leak itself turns out
# to be platform-dependent (macOS's flock release timing across fork
# differs from Linux's documented "released only when every duplicate fd
# is closed" semantics - confirmed by hand while writing this test), so
# this drives the same critical branch directly and deterministically:
# one real, live process holds the flock the whole time, but lies about
# the owner pid it stamps into the lock file, using a pid we affirmatively
# KNOW is dead (a just-exited subprocess's). That isolates exactly the
# logic under test - "flock() fails AND the recorded pid is dead" - from
# the mechanism that produces it in production.

def _hold_under_a_dead_pid(lock_path, dead_pid, started_evt, hold_seconds):
    import fcntl
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(fd, json.dumps({"pid": dead_pid}).encode())
    started_evt.set()
    time.sleep(hold_seconds)
    os._exit(0)


def _guaranteed_dead_pid() -> int:
    """A pid that has definitely already exited - not a made-up number that
    might collide with something real and alive on the test machine."""
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_stale_lock_with_unbreakable_flock_needs_break_stale(tmp_path):
    lock_path = str(tmp_path / "leaked.lock")
    dead_pid = _guaranteed_dead_pid()
    assert not locks._pid_alive(dead_pid)

    started = CTX.Event()
    p = CTX.Process(target=_hold_under_a_dead_pid,
                     args=(lock_path, dead_pid, started, 5))
    p.start()
    try:
        assert started.wait(5)

        # A real, live process holds the flock the whole time, so without
        # break_stale this must behave exactly like a genuinely live owner.
        with pytest.raises(locks.LockTimeout):
            with locks.hold(lock_path, timeout=0.5, break_stale=False):
                pass

        # With break_stale, hold() must force the break (the recorded pid
        # is dead) rather than wait out the holder's full 5s sleep.
        start = time.monotonic()
        with locks.hold(lock_path, timeout=3, break_stale=True):
            pass
        assert time.monotonic() - start < 3
    finally:
        p.join(10)


# ── live owner ───────────────────────────────────────────────────────────

def _hold_for(lock_path, seconds, started_evt):
    with locks.hold(lock_path, timeout=10):
        started_evt.set()
        time.sleep(seconds)


def test_live_owner_lock_raises_timeout(tmp_path):
    lock_path = str(tmp_path / "live.lock")
    started = CTX.Event()
    p = CTX.Process(target=_hold_for, args=(lock_path, 3, started))
    p.start()
    try:
        assert started.wait(5)
        with pytest.raises(locks.LockTimeout):
            with locks.hold(lock_path, timeout=0.3):
                pass
    finally:
        p.join(10)


# ── reentrancy ───────────────────────────────────────────────────────────

def test_reentrant_hold_raises_immediately(tmp_path):
    """A nested hold() on the SAME path in the SAME process must fail fast
    (ReentrantLock), not poll until timeout against its own earlier
    acquisition - flock() cannot tell "same process, different fd" apart
    from a genuinely unrelated contender."""
    lock_path = str(tmp_path / "reentrant.lock")
    start = time.monotonic()
    with locks.hold(lock_path, timeout=10):
        with pytest.raises(locks.ReentrantLock):
            with locks.hold(lock_path, timeout=10):
                pass
    assert time.monotonic() - start < 1

    # Released cleanly afterwards - a subsequent, non-nested hold still
    # works (the bookkeeping doesn't leak past the failed nested attempt).
    with locks.hold(lock_path, timeout=1):
        pass


# ── stranded contender (adversarial-review regression) ──────────────────
#
# A contender that opened its fd BEFORE another process broke a stale lock
# must notice the break and reacquire promptly - it must not remain wedged
# reading a mismatched (post-break) owner off a stale fd that no longer
# corresponds to what's actually at `path`. This is the exact bug an
# adversarial review caught: a pre-existing contender pinned to the
# orphaned inode timed out at its full budget while the lock was actually
# free within milliseconds of the break.

def _contend_without_breaking(lock_path, timeout, result_queue):
    start = time.monotonic()
    try:
        with locks.hold(lock_path, timeout=timeout, break_stale=False):
            pass
        result_queue.put(("ok", time.monotonic() - start))
    except locks.LockTimeout:
        result_queue.put(("timeout", time.monotonic() - start))


def _contend_and_break(lock_path, timeout, hold_seconds, ready_evt):
    with locks.hold(lock_path, timeout=timeout, break_stale=True):
        ready_evt.set()
        time.sleep(hold_seconds)


def test_stranded_contender_recovers_after_someone_elses_break(tmp_path):
    lock_path = str(tmp_path / "contended.lock")
    dead_pid = _guaranteed_dead_pid()

    # D: genuinely, unbreakably holds the ORIGINAL inode under a
    # falsified dead pid for the whole test.
    started_d = CTX.Event()
    d = CTX.Process(target=_hold_under_a_dead_pid,
                     args=(lock_path, dead_pid, started_d, 10))
    d.start()

    manager = CTX.Manager()
    result_q = manager.Queue()
    c = None
    b = None
    try:
        assert started_d.wait(5)

        # C: a pre-existing contender - starts polling while D still holds
        # the original inode, and (break_stale=False) never itself
        # attempts to break the lock, so any recovery it manages must
        # come from noticing SOMEONE ELSE'S break, not from doing its own.
        c = CTX.Process(target=_contend_without_breaking,
                         args=(lock_path, 8, result_q))
        c.start()
        time.sleep(0.3)  # let C open its fd and take at least one failed poll

        # B: the breaker - starts after C, forces the stale lock open, and
        # releases quickly.
        ready_b = CTX.Event()
        b = CTX.Process(target=_contend_and_break, args=(lock_path, 5, 0.1, ready_b))
        b_start = time.monotonic()
        b.start()
        assert ready_b.wait(5)
        b.join(10)
        assert b.exitcode == 0
        assert time.monotonic() - b_start < 1  # the breaker itself isn't stuck

        kind, c_elapsed = result_q.get(timeout=10)
        c.join(10)
        assert c.exitcode == 0
        assert kind == "ok", (
            f"pre-existing contender should recover, got {kind!r} after "
            f"{c_elapsed:.2f}s"
        )
        assert c_elapsed < 2  # promptly, not stranded until its own 8s budget
    finally:
        for proc in (d, c, b):
            if proc is not None and proc.is_alive():
                proc.terminate()
                proc.join(5)
