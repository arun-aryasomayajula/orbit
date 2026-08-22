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
into a still-live process that never itself calls hold() again. The vector
is NOT `subprocess`/exec (PEP 446 makes every fd Python opens
close-on-exec by default, so a plain `subprocess.Popen` call, even without
`close_fds`, does not hand a child the lock fd) - the real vector is a bare
`os.fork()` with NO following exec: `close-on-exec` only fires at exec()
time, so a fork()'d child (e.g. `multiprocessing`'s "fork" start method, or
a service daemonizing itself via a double-fork) inherits the parent's
lock fd wholesale and can outlive the parent while never touching the lock
API again. The kernel has no way to know the "real" owner already exited -
the open file description, and the flock on it, stays alive as long as ANY
process still has that fd open. This is precisely why the docstring rule
below says NEVER hold this lock across a claude invocation: a `claude`
subprocess launched *from inside* a `with hold(...)` block could itself
fork internally in a way that ends up holding a copy of the fd for the
rest of a cycle's runtime, wedging every other contender for the full
`timeout` window on every subsequent attempt - the process that "holds" it
(per the pid recorded in the lock file) is already dead, but the lock
itself is not going anywhere on its own.

`break_stale=True` (the default) copes with that: when `flock()` fails and
the pid recorded in the lock file is dead, `hold()` unlinks the lock path
and re-creates it fresh, so new contenders race onto a brand-new inode
instead of queueing forever behind a leaked fd on the old one. This is a
deliberate, disclosed trade-off, not a free lunch: forcing a break while
the true, live holder of the leaked fd might still be mutating whatever
the lock protects is inherently unsafe in the general case - it is only
ever reached when the "never hold across a claude invocation" rule has
already been broken elsewhere. `hold()` narrows the race as far as a
single flock+file API reasonably allows: every acquisition (stale-break or
not) is verified against the inode currently sitting at `path` before the
critical section starts (`_same_file`, below), the owner pid is re-read
immediately before a break actually happens (narrowing, not closing, the
window between "diagnosed dead" and "acted on it"), and - the specific bug
an adversarial review caught - a contender whose own fd was opened BEFORE
someone else's break drops that now-orphaned fd and reopens the current
file the moment it notices the mismatch, rather than continuing to poll a
file nobody else can even see any more (see "stranded contender" below).
None of this makes the underlying leaked-fd scenario safe in general; it
only makes sure autopilot itself is never the reason a corpse's lock
outlives it forever, and never the reason an innocent bystander contender
gets left holding a dead reference.

Reentrancy: `hold()` is NOT reentrant, and cannot be made safely reentrant
with plain flock() - a nested `with hold(p): ... with hold(p): ...` in the
SAME process opens a SECOND, independent fd to `p` and tries to flock it,
which is indistinguishable to the kernel from a totally unrelated process
contending for the same lock (flock has no notion of "same process,
different fd"). Left unguarded this self-deadlocks (or, worse, with
break_stale it could try to break a lock the process itself still holds).
`hold()` tracks paths currently held per-pid (module-level, cleared on
release) and raises `ReentrantLock` immediately - before ever touching the
filesystem - on a same-process re-entry, so the bug surfaces as a loud,
instant exception instead of a 30-second hang. Per-pid (not a bare set) so
a fork()'d child - which inherits the parent's Python memory, including
whatever this module's bookkeeping said at fork time - never mistakes the
parent's still-recorded holds for its own; the fork gets a fresh pid, so
`os.getpid()`-keyed lookups correctly see nothing already held.

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


class ReentrantLock(Exception):
    """Raised by hold() when this same process already holds the lock at
    `path` - see the module docstring's "Reentrancy" section. Fail fast
    instead of self-deadlocking against your own earlier acquisition."""


# path -> {paths currently held by that pid}, so a fork()'d child (which
# inherits this dict's CONTENTS but gets its own fresh pid) never confuses
# a parent's still-open hold with something it itself is holding.
_HELD_PATHS: dict = {}


def _held_paths() -> set:
    return _HELD_PATHS.setdefault(os.getpid(), set())


def pid_alive(pid) -> bool:
    """Best-effort liveness check for a pid recorded in a lock file (or any
    other worker-identity record - ledger.py's reap also consumes this).
    A negative/zero/unparseable pid is never "alive" (never something to
    wait out). EPERM means the pid exists but is owned by someone else -
    still alive, just not ours to signal."""
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


_pid_alive = pid_alive  # back-compat alias - prefer the public name above


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

    Raises `ReentrantLock` immediately (before touching the filesystem) if
    this same process is already inside a `hold()` on this exact path - see
    the module docstring's "Reentrancy" section.

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
    held = _held_paths()
    if path in held:
        raise ReentrantLock(
            f"{path!r} is already held by this process (pid {os.getpid()}) - "
            "nested locks.hold() calls on the same path self-deadlock under "
            "flock(); restructure the caller so the inner critical section "
            "reuses the outer one's hold instead of re-acquiring it"
        )
    held.add(path)

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    deadline = time.monotonic() + timeout
    fd = None
    try:
        while True:
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    f"could not acquire lock {path!r} within {timeout}s"
                )

            if fd is None:
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise

                if not _same_file(fd, path):
                    # `path` has already moved on to a different inode
                    # since we opened this fd (another contender broke or
                    # replaced it). Our flock() result says NOTHING about
                    # the file actually at `path` now - in particular,
                    # reading "the owner" off `path` at this point would
                    # describe someone else's lock entirely, not whatever
                    # our own stale fd is contending with. Drop it; the
                    # sleep+retry below reopens the CURRENT file next
                    # iteration instead of us polling (or breaking based
                    # on) a file nobody else can even see any more - this
                    # is the "stranded contender" bug an adversarial
                    # review caught: a pre-existing contender timing out
                    # while the real lock was actually free.
                    os.close(fd)
                    fd = None
                elif break_stale:
                    owner = _read_owner_pid(path)
                    if owner is not None and not pid_alive(owner):
                        # Re-read immediately before acting: narrows (does
                        # not close - see module docstring) the window
                        # between "diagnosed dead" and "broke the lock" in
                        # which the file could have changed again.
                        if _read_owner_pid(path) == owner:
                            try:
                                os.unlink(path)
                            except OSError:
                                pass
                            os.close(fd)
                            fd = None
                            continue  # our own action just changed things

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
                continue
            break

        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, (json.dumps({"pid": os.getpid()}) + "\n").encode(), 0)
        except OSError:
            pass  # best-effort - failing to stamp ownership never blocks
        yield
    finally:
        held.discard(path)
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
