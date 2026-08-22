#!/usr/bin/env python3
"""locks.py - the ONE shared flock-based critical-section helper for
autopilot. Every new lock in the codebase (ledger claims, backlog.yaml
writers, spend/count accounting, QA services serialization) goes through
`hold()` here instead of hand-rolling another ad-hoc lock file - see
qa_run.py's `_acquire_lock`/`_touch_lock`/`_release_lock` for the
create-exclusively-plus-mtime-staleness scheme this deliberately does NOT
repeat: that scheme's "fresh vs stale" call is a guess based on file age,
which is exactly wrong for a lock that's legitimately still held (a slow
critical section looks identical to a crashed one until the timeout
passes). `hold()` instead uses a REAL OS advisory lock (`flock(2)` via
`fcntl`), so a normal process death - however it happens, a clean exit or
a SIGKILL - releases the lock immediately and unconditionally: the kernel
tears down the holder's file descriptor table on exit, and flock is scoped
to the open file description behind that fd. There is no window where a
dead holder's lock silently keeps blocking everyone else, and no need to
guess an age threshold.

Why `break_stale` still exists despite that: it exists for exactly one
failure mode flock's own semantics cannot save you from - a lock fd LEAKED
across a fork+exec (e.g. `subprocess.Popen` without `close_fds`, or any
libc that doesn't set `O_CLOEXEC`) into a child that outlives the process
that acquired the lock. The child never touches the lock again, has no
idea it is holding anything, and the kernel has no way to know the
"real" owner already exited - the open file description, and the flock on
it, stays alive as long as ANY process still has that fd open. This is
precisely why the docstring rule below says NEVER hold this lock across a
claude invocation: `claude` subprocesses are exactly the kind of
long-lived child that can inherit a leaked fd and then sit on it for the
rest of a cycle's runtime, wedging every other contender for the full
`timeout` window, forever, on every subsequent attempt - the process that
"holds" it (per the pid recorded in the lock file) is already dead, but
the lock itself is not going anywhere on its own.

`break_stale=True` (the default) copes with that: when `flock()` fails and
the pid recorded in the lock file is dead, `hold()` unlinks the lock path
and re-creates it fresh, so new contenders race onto a brand-new inode
instead of queueing forever behind a leaked fd on the old one. This is a
deliberate, disclosed trade-off, not a free lunch: forcing a break while
the true, live holder of the leaked fd might still be mutating whatever
the lock protects is inherently unsafe in the general case - it is only
ever reached when the "never hold across a claude invocation" rule has
already been broken elsewhere. `hold()` narrows the race as far as a
single flock+file API reasonably allows (see `_same_file` below - every
acquisition, stale-break or not, is verified against the inode currently
sitting at `path` before the critical section starts, so a concurrent
break-and-recreate by another contender is caught rather than silently
producing two simultaneous "holders"), but it cannot make the underlying
scenario safe; it can only make sure autopilot itself is never the reason
a corpse's lock outlives it forever.

Usage:
    import locks
    with locks.hold(state_dir / ".spend.lock", timeout=30):
        ...critical section...

NEVER hold this across a claude invocation - see above. Lock files live
next to whatever they guard, or under $AP_STATE if there is no more
specific home for them.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import time


class LockTimeout(Exception):
    """Raised by hold() when `timeout` seconds passed without acquiring the
    lock, and the current holder was never diagnosed as stale-and-breakable
    (or break_stale=False) - i.e. some other, live process really does hold
    it the whole time."""


def _pid_alive(pid) -> bool:
    """Best-effort liveness check for a pid recorded in a lock file. A
    negative/zero/unparseable pid is never "alive" (never something to wait
    out). EPERM means the pid exists but is owned by someone else - still
    alive, just not ours to signal."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_owner_pid(path):
    """The pid recorded in the lock file at `path`, or None if the file is
    missing, empty, or not the {"pid": ...} json this module writes (e.g. a
    lock someone just created via O_CREAT but hasn't written to yet - never
    treated as stale, since its owner is mid-acquisition, not dead)."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("pid")
    except (OSError, ValueError, AttributeError):
        return None


def _same_file(fd, path) -> bool:
    """True if the open fd still refers to the inode currently sitting at
    `path` on disk - false once anyone has unlinked+recreated `path` out
    from under it (a stale-break race, see module docstring)."""
    try:
        return os.fstat(fd).st_ino == os.stat(path).st_ino
    except OSError:
        return False


@contextlib.contextmanager
def hold(path, timeout: float = 30, break_stale: bool = True,
         poll_interval: float = 0.05):
    """Exclusive critical section guarded by the lock file at `path`.

    Blocks (polling every `poll_interval` seconds) until the flock is
    acquired or `timeout` seconds pass, whichever comes first. On success,
    stamps the lock file with our own pid (json: {"pid": <int>}) and yields;
    the flock is released (never unlinked - see below) on the way out,
    success or exception alike.

    `break_stale`: while contended, if the pid recorded in the lock file is
    no longer alive, force a fresh inode at `path` so new contenders never
    queue forever behind a leaked fd (see module docstring for exactly when
    this does and doesn't help). Exceeding `timeout` without ever being able
    to acquire - because a live process genuinely holds it, or breaking is
    disabled/never diagnosed the holder as dead - raises LockTimeout.

    Release deliberately does NOT unlink `path`: deleting a lock file out
    from under a concurrent opener that already has an fd to it would let
    that opener's flock silently succeed against an orphaned inode no
    future opener will ever see, defeating the lock for everyone after it.
    The file is reused (truncated and re-stamped) by every future holder
    instead - the same posture as a plain, permanent lock file.
    """
    path = os.fspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    deadline = time.monotonic() + timeout
    fd = None
    try:
        while True:
            if fd is None:
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                broke = False
                if break_stale:
                    owner = _read_owner_pid(path)
                    if owner is not None and not _pid_alive(owner):
                        # Dead owner, still can't get the flock: almost
                        # certainly a leaked fd (see module docstring).
                        # Force a fresh inode so contenders stop queueing
                        # behind it.
                        try:
                            if _same_file(fd, path):
                                os.unlink(path)
                            broke = True
                        except OSError:
                            pass
                        os.close(fd)
                        fd = None
                if broke:
                    continue  # reopen the fresh file immediately, no sleep
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"could not acquire lock {path!r} within {timeout}s"
                    )
                time.sleep(poll_interval)
                continue

            # Acquired the flock - but make sure it's still the file at
            # `path` (a concurrent stale-break could have swapped it out
            # from under us between our open() and this flock()).
            if not _same_file(fd, path):
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
                fd = None
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"could not acquire lock {path!r} within {timeout}s"
                    )
                continue
            break

        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, (json.dumps({"pid": os.getpid()}) + "\n").encode(), 0)
        except OSError:
            pass  # best-effort - failing to stamp ownership never blocks
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
