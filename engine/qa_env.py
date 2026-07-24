#!/usr/bin/env python3
"""qa_env.py — deterministic QA service launcher/teardown.

The engine-side twin of skills/orbit-qa-check.md's step-1 launch discipline
(background launch from `cwd`, env exported, log capture, ready-poll HTTP
200, kill everything you started) — but with no agent driving it. Where the
QA agent brings `qa.services` up itself (via its own Bash tool) as part of
driving a browser, `orbit teach` (Task 20) records a session with NO agent
present, so something deterministic has to do exactly that same up/down
dance CLI-side. This module is that something, shared by teach and any later
engine caller that needs a target's `qa.services` running.

Design choice (brief asks it be stated): module-level functions + a small
`ServiceHandle` dataclass, not a class — matches the rest of engine/ (see
config.py, qa_check.py, plan_gate.py: no service-object classes anywhere,
just functions over plain dicts/dataclasses). `QaEnvError` is the one
exception type this module raises; every teardown path is a plain function
that is documented to never raise.

`up`/`down`/`up_ctx` are pure stdlib and take an already-parsed `qa_cfg`
dict (the target's `qa:` config/schema.yaml block) — they never read a
target's config.yaml themselves. `main()`'s CLI convenience loader is the one
place that reads config.yaml off disk (mirroring config.py's target-relative
path convention: `<target>/.autopilot/config.yaml`), and it defers `import
yaml` to keep that a CLI-only dependency — `import qa_env` for library use
never requires PyYAML.

Contract:
    up(target, qa_cfg, log_dir) -> list[ServiceHandle]
        Launches qa_cfg['services'] IN ORDER: subprocess.Popen(shell=True)
        (launch strings are shell commands, e.g. "npm run dev"), cwd =
        target/service['cwd'] (default "."), env = os.environ + qa_cfg['env'],
        stdout+stderr -> log_dir/<name>.log. Each launched process gets its
        own process group (start_new_session=True) so teardown can kill the
        whole tree a shell command spawns (npm -> node, etc), not just the
        shell. After each launch: a `ready` URL polls (2s interval) until
        HTTP 200 or qa_cfg['ready_timeout_seconds'] (default 90) elapses; no
        `ready` -> a fixed 3s settle sleep. ANY failure (bad service shape,
        launch OSError, ready timeout) tears down EVERYTHING already started
        before raising QaEnvError naming the offending service — up() never
        leaves a partial set of services running behind a raised exception.
    down(handles)
        Terminates each handle (SIGTERM, 5s grace, SIGKILL), REVERSE order
        (last started, first killed). Never raises — every failure is
        logged to stderr and teardown continues to the next handle. Safe to
        call on an empty or partial list.
    up_ctx(target, qa_cfg, log_dir)
        Context manager: `with up_ctx(...) as handles:` — down() runs in a
        finally, so an exception raised inside the with-block (or any early
        return) can never leak a running service.

Not wired into bin/orbit — internal tool, CLI usage for manual/testing use:
    qa_env.py <target> up-check
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

READY_POLL_INTERVAL = 2      # seconds between ready-URL polls
DEFAULT_READY_TIMEOUT = 90   # qa.ready_timeout_seconds default (config/schema.yaml)
NO_READY_SLEEP = 3           # fixed settle sleep for a service with no `ready` URL
TERM_GRACE = 5               # seconds to wait after SIGTERM/SIGKILL before escalating/giving up


class QaEnvError(Exception):
    """A qa.services entry is malformed, or a service never became ready."""


@dataclass
class ServiceHandle:
    name: str
    proc: subprocess.Popen
    log_path: Path


# ---------------------------------------------------------------------------
# up
# ---------------------------------------------------------------------------

def _spawn(launch: str, cwd: Path, env: dict, log_path: Path) -> subprocess.Popen:
    """Launch one service. `launch` is passed to Popen as a raw string (not a
    list) — with shell=True a string is handed to `/bin/sh -c <string>`
    as-is, which is what matches the config's shell command strings (a list
    would instead be misparsed as [shell-command, extra-args...]).

    start_new_session=True makes this process the leader of a brand new
    process group — `launch` strings routinely fork further children (e.g.
    "npm run dev" -> npm -> the actual dev server), and killing only the
    directly-tracked pid can leave those grandchildren running. Teardown
    signals the whole group (see _send below), not just this pid.
    """
    log_fh = open(log_path, "ab")
    try:
        return subprocess.Popen(
            launch, shell=True, cwd=str(cwd), env=env,
            stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        # Popen() has already forked+exec'd by the time it returns (POSIX) —
        # the child owns its own dup'd copy of this fd, so closing our copy
        # in the parent here is safe and avoids leaking an open fd per service.
        log_fh.close()


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=READY_POLL_INTERVAL) as resp:
            return resp.status == 200
    except Exception:
        # Broad on purpose: "connection refused" (OSError), "not listening
        # yet" (URLError), a non-200 status (HTTPError, a URLError subclass),
        # or a malformed URL (ValueError) all just mean "not ready yet" here
        # — none of them may crash a ready-poll loop.
        return False


def _wait_ready(url: str, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _http_ok(url):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(READY_POLL_INTERVAL)


def up(target: str | Path, qa_cfg: dict, log_dir: str | Path) -> list[ServiceHandle]:
    """Bring qa_cfg['services'] up in order. See module docstring for the
    full contract. Raises QaEnvError (after tearing down anything already
    started) on any malformed service shape, launch failure, or ready
    timeout."""
    target = Path(target)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    services = qa_cfg.get("services") or []
    if not isinstance(services, list):
        raise QaEnvError(f"qa.services must be a list, got {type(services).__name__}")

    extra_env = qa_cfg.get("env") or {}
    if not isinstance(extra_env, dict):
        raise QaEnvError(f"qa.env must be an object/mapping, got {type(extra_env).__name__}")
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in extra_env.items()})

    raw_timeout = qa_cfg.get("ready_timeout_seconds")
    if raw_timeout is None:
        ready_timeout = DEFAULT_READY_TIMEOUT
    else:
        try:
            ready_timeout = int(raw_timeout)
        except (TypeError, ValueError):
            raise QaEnvError(
                f"qa.ready_timeout_seconds must be a number, got {raw_timeout!r}")

    # Everything from here on can have started a real process — one
    # try/except around the whole loop is the structural teardown guarantee:
    # ANY exception raised while bringing services up (the QaEnvErrors below,
    # today, or anything a future change might let through — e.g. a bug in
    # _http_ok's catch-all) tears down everything already started before
    # propagating. Safety net, not a substitute for raising QaEnvError at the
    # actual point of failure (still done below, for a clear message).
    handles: list[ServiceHandle] = []
    try:
        for svc in services:
            if not isinstance(svc, dict):
                raise QaEnvError(f"qa.services entry is not an object: {svc!r}")

            name = str(svc.get("name") or f"service{len(handles)}")
            launch = svc.get("launch")
            if not launch or not isinstance(launch, str):
                raise QaEnvError(f"service {name!r} has no 'launch' command")

            raw_cwd = svc.get("cwd") or "."
            if not isinstance(raw_cwd, str):
                raise QaEnvError(f"service {name!r} has a non-string 'cwd': {raw_cwd!r}")
            cwd = target / raw_cwd
            log_path = log_dir / f"{name}.log"
            try:
                proc = _spawn(launch, cwd, env, log_path)
            except OSError as e:
                raise QaEnvError(f"service {name!r} failed to launch: {e}") from e

            handles.append(ServiceHandle(name=name, proc=proc, log_path=log_path))

            ready = svc.get("ready")
            if ready:
                if not _wait_ready(str(ready), ready_timeout):
                    raise QaEnvError(
                        f"service {name!r} never became ready at {ready!r} "
                        f"within {ready_timeout}s")
            else:
                time.sleep(NO_READY_SLEEP)
    except Exception:
        down(handles)
        raise

    return handles


# ---------------------------------------------------------------------------
# down
# ---------------------------------------------------------------------------

def _send(proc: subprocess.Popen, sig: int) -> None:
    """Signal the WHOLE process group `proc` leads (see _spawn's
    start_new_session=True) so a shell command's children die with it.
    Falls back to signalling the pid directly if the group is already gone
    (race: process exited between poll() and here) or process groups aren't
    available for some reason — never raises past this function."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
        return
    except ProcessLookupError:
        return   # group (and process) already gone — nothing to signal
    except OSError:
        pass     # e.g. no such group for some other reason — fall back below
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        pass


def _kill_one(handle: ServiceHandle) -> None:
    proc = handle.proc
    if proc.poll() is not None:
        return   # already exited
    _send(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=TERM_GRACE)
        return
    except subprocess.TimeoutExpired:
        pass
    _send(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=TERM_GRACE)
    except subprocess.TimeoutExpired:
        pass   # leaked despite SIGKILL — nothing more to do without raising


def down(handles: list[ServiceHandle]) -> None:
    """Tear down every handle, LAST-started first, and never raise — this is
    the unconditional cleanup path called from every up() failure branch and
    from up_ctx()'s finally, so a bug in tearing down one service must never
    mask the real error (or stop the rest from being torn down too)."""
    for handle in reversed(list(handles or [])):
        try:
            _kill_one(handle)
        except Exception as e:
            print(f"WARN: qa_env: teardown of {handle.name!r} raised {e!r} — "
                  f"continuing to the next service", file=sys.stderr)


@contextlib.contextmanager
def up_ctx(target: str | Path, qa_cfg: dict, log_dir: str | Path):
    """`with up_ctx(target, qa_cfg, log_dir) as handles:` — down() always
    runs (finally), so an exception raised inside the with-block, an early
    return, or anything else can never leak a running service. (If up()
    itself raises, it has already torn down anything it started — this
    finally has nothing left to do in that case.)"""
    handles = up(target, qa_cfg, log_dir)
    try:
        yield handles
    finally:
        down(handles)


# ---------------------------------------------------------------------------
# CLI — manual/testing use only, not wired into bin/orbit.
# ---------------------------------------------------------------------------

def _load_qa_cfg(target: Path) -> dict:
    """CLI-only convenience loader: `<target>/.autopilot/config.yaml`'s
    `qa:` block, mirroring config.py's target-relative config path
    convention. Deferred `import yaml` so importing this module as a library
    (up/down/up_ctx, which take an already-parsed dict) never requires
    PyYAML."""
    import yaml
    cfg_path = target / ".autopilot" / "config.yaml"
    try:
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    except OSError as e:
        raise QaEnvError(f"could not read {cfg_path}: {e}") from e
    qa_cfg = raw.get("qa") if isinstance(raw, dict) else None
    if not isinstance(qa_cfg, dict):
        raise QaEnvError(f"no qa: block in {cfg_path}")
    return qa_cfg


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] != "up-check":
        print("usage: qa_env.py <target> up-check", file=sys.stderr)
        return 1
    target = Path(argv[0]).resolve()
    log_dir = Path(tempfile.mkdtemp(prefix="qa-env-"))
    try:
        qa_cfg = _load_qa_cfg(target)
        with up_ctx(target, qa_cfg, log_dir):
            print(f"READY — services up, logs in {log_dir}")
        return 0
    except QaEnvError as e:
        print(f"FAIL: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
