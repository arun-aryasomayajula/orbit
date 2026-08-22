#!/usr/bin/env python3
"""launcher.py — standalone multi-project front door for Orbit.

Zero external deps (stdlib http.server). Sits in front of command_center.py:
owns a project registry (~/.orbit/projects.json), lets an operator import a
repo, scaffold it (install/init.py), and open its own command_center.py child
bound to an ephemeral port. Everything that isn't a launcher-reserved route
is transparently proxied to the ACTIVE child, re-issuing the request with its
own Host/Origin/token so the child's own CSRF defense stays intact end to end.

Run:    python3 $ORBIT_HOME/engine/launcher.py    # http://127.0.0.1:8787
        PORT=9000 python3 $ORBIT_HOME/engine/launcher.py

It is also the loop process manager: each project's run.sh slots are started
and stopped from here (see LOOPS), so an operator never has to touch launchd
to pause or resume a workspace.

Reserved routes (never proxied — see the class-level docstring on Handler):
  GET  /_launcher/state       registry + active project + per-project flags
  POST /_launcher/import      {path} -> validate + register a git repo
  POST /_launcher/init        {path} -> scaffold .autopilot/ (install/init.py)
  POST /_launcher/create      {path, name, product} -> mkdir + git init + init.py + register
  POST /_launcher/open        {path} -> spawn/reuse a command_center.py child, activate it
  POST /_launcher/loop-start  {path} -> spawn slots 0..concurrency-1 of engine/run.sh
  POST /_launcher/loop-stop   {path} -> arm the STOP file, then TERM this project's slots
  POST /_launcher/close       deselect the active project (back to the picker)
  POST /_launcher/remove      {path} -> unregister (stops its child if running)
  GET  /                      picker (no active project) or proxy (active project)
Everything else proxies to the active child; 503 JSON if none is active.
"""
from __future__ import annotations

import atexit
import hmac
import json
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ── path model ───────────────────────────────────────────────────────────────
ENGINE = Path(__file__).resolve().parent               # this repo's engine dir
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or ENGINE.parent)   # orbit repo root
COMMAND_CENTER = ENGINE / "command_center.py"
RUN_SCRIPT = ENGINE / "run.sh"
INIT_SCRIPT = ORBIT_HOME / "install" / "init.py"
SHELL_FILE = ENGINE / "launcher_shell.html"

PORT = int(os.environ.get("PORT", "8787"))

# Registry location: ORBIT_REGISTRY pins the exact json file (tests use this
# for hermetic runs); else ORBIT_LAUNCHER_HOME/.orbit/projects.json; else
# ~/.orbit/projects.json. Resolved once at import time, same convention as
# command_center.py's AP_HOME/AP_STATE globals.
_registry_override = os.environ.get("ORBIT_REGISTRY")
if _registry_override:
    REGISTRY = Path(_registry_override)
else:
    _launcher_home = os.environ.get("ORBIT_LAUNCHER_HOME") or os.environ.get("HOME") or str(Path.home())
    REGISTRY = Path(_launcher_home) / ".orbit" / "projects.json"

# Shared token: one per launcher process lifetime, handed to every child as
# CC_TOKEN and substituted into the picker shell as __TOKEN__. CC_TOKEN env
# wins if set (mirrors command_center.py's own _TOKEN resolution — lets an
# operator/test pin a deterministic token).
_TOKEN = os.environ.get("CC_TOKEN") or secrets.token_urlsafe(18)
_ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}

# path(str, resolved) -> {"proc": Popen|None, "port": int}. proc is None for
# test-injected fake children (register_child), never for real spawns.
CHILDREN: dict[str, dict] = {}
ACTIVE: dict[str, str | None] = {"path": None}

# path(str, resolved) -> {slot(int): Popen}. One entry per engine/run.sh slot
# process THIS launcher started for that project (slots 0..concurrency-1).
# Loops started by launchd/systemd (or by a previous launcher process) are not
# in here at all - they are discovered through $STATE/workers/*.pid instead,
# which is why every read of this table is only half of the liveness answer
# (see _loop_running) and every write of it is best-effort bookkeeping.
LOOPS: dict[str, dict[int, subprocess.Popen]] = {}

# How long loop-stop waits between arming the STOP file and signalling the
# slot processes. STOP is only read at the top of a slot's iteration, so this
# is not "wait for a graceful exit" (that can take an interval_seconds nap) -
# it is a courtesy window for a slot that is about to loop around anyway.
# Env-tunable so tests don't pay for it (a garbage value must never keep the
# launcher from booting, so it falls back to the default).
try:
    LOOP_STOP_GRACE = max(0.0, float(os.environ.get("ORBIT_LOOP_STOP_GRACE") or 2))
except ValueError:
    LOOP_STOP_GRACE = 2.0

# Coarse lock guarding all reads/writes of CHILDREN and ACTIVE. RLock so a
# function that already holds it (e.g. spawn_child) can call another that
# also takes it (e.g. _child_alive) from the same thread without deadlocking.
# Kept simple on purpose: contention is negligible (a handful of short dict
# ops per request), and the alternative — a KeyError racing a concurrent
# /_launcher/remove mid-proxy — is worse than a little serialization.
_LOCK = threading.RLock()

_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host"}

# Bare opener for the child hop: no redirect-following, no proxy env vars
# honored (ProxyHandler({}) disables env-based proxying) — the outbound call
# is pinned straight to the child's own loopback port, never redirected
# elsewhere or routed through an ambient HTTP(S)_PROXY.
_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ── registry (atomic read/write) ─────────────────────────────────────────────
def _load_registry() -> dict:
    try:
        data = json.loads(REGISTRY.read_text())
        if isinstance(data, dict) and isinstance(data.get("projects"), list):
            return data
    except Exception:
        pass
    return {"projects": []}


def _save_registry(reg: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(REGISTRY.parent), prefix=".projects-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(reg, f, indent=2)
            f.write("\n")
        os.replace(tmp, REGISTRY)   # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def registry_list() -> list:
    return _load_registry()["projects"]


def registry_add(path: str, name: str | None = None) -> dict:
    """Register a project, deduped on its resolved absolute path. Idempotent.
    `name` overrides the display name (default: the directory's basename) -
    used by do_create, where the operator names the workspace themselves."""
    p = str(Path(path).resolve())
    reg = _load_registry()
    for proj in reg["projects"]:
        if proj["path"] == p:
            return proj
    proj = {"name": (name or "").strip() or Path(p).name, "path": p,
            "added_at": time.time(), "last_opened": None}
    reg["projects"].append(proj)
    _save_registry(reg)
    return proj


def registry_remove(path: str) -> bool:
    p = str(Path(path).resolve())
    reg = _load_registry()
    before = len(reg["projects"])
    reg["projects"] = [proj for proj in reg["projects"] if proj["path"] != p]
    if len(reg["projects"]) == before:
        return False
    _save_registry(reg)
    return True


def registry_touch(path: str) -> None:
    p = str(Path(path).resolve())
    reg = _load_registry()
    for proj in reg["projects"]:
        if proj["path"] == p:
            proj["last_opened"] = time.time()
            _save_registry(reg)
            return


def registry_get(path: str) -> dict | None:
    p = str(Path(path).resolve())
    return next((proj for proj in registry_list() if proj["path"] == p), None)


# ── project inspection ───────────────────────────────────────────────────────
def has_autopilot(path: str) -> bool:
    try:
        return (Path(path) / ".autopilot").exists()
    except Exception:
        return False


def _state_dir(path: str) -> Path:
    return Path(path) / ".autopilot" / "state"


def _pid_alive(pid) -> bool:
    """Local copy of locks.pid_alive (same semantics: EPERM means alive but
    not ours). Duplicated rather than imported because launcher.py is the one
    engine file that must run standalone - stdlib only, no engine imports, so
    it still boots from a half-installed tree."""
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


def _worker_pids(path: str) -> list[int]:
    """Live pids recorded in $STATE/workers/*.pid (run.sh's write_worker_pid
    writes the SLOT LOOP's own pid there, so these name loop processes - not
    just in-flight cycles). This is how loops the launcher didn't start
    (launchd, systemd, a previous launcher) become visible to it."""
    out = []
    wd = _state_dir(path) / "workers"
    try:
        files = sorted(wd.glob("*.pid")) if wd.is_dir() else []
    except OSError:
        return out
    for f in files:
        try:
            info = json.loads(f.read_text())
        except (OSError, ValueError):
            continue   # torn tmp+rename read - same tolerance as command_center's workers_state
        if isinstance(info, dict) and _pid_alive(info.get("pid")):
            out.append(int(info["pid"]))
    return out


def _tracked_loop_pids(path: str) -> list[int]:
    """Pids of this launcher's own live slot processes for `path`, reaping the
    dead entries out of LOOPS on the way past."""
    p = str(Path(path).resolve())
    alive = []
    with _LOCK:
        table = LOOPS.get(p) or {}
        for slot, proc in list(table.items()):
            if proc.poll() is None:
                alive.append(proc.pid)
            else:
                del table[slot]
        if not table:
            LOOPS.pop(p, None)
    return alive


def _loop_running(path: str) -> bool:
    """Is this project's loop running? Three signals, cheapest first:
      1. a slot process THIS launcher started is still alive (LOOPS);
      2. $STATE/workers/*.pid names a live pid (a loop started by launchd, by
         a previous launcher, or by hand - the launcher is not the only way in);
      3. legacy fallback - a loop has run here before (logs/orbit.log) and no
         STOP file is armed. Kept because the pid files only exist while a slot
         HOLDS a claim: a service-managed loop napping between cycles has none,
         and before Task 4 the workers dir didn't exist at all. It is a
         heuristic, so it comes last - and it is gated on orbit.log because
         "state dir exists and no STOP" alone reported every freshly scaffolded
         workspace, which has never run anything, as running."""
    try:
        if _tracked_loop_pids(path):
            return True
        if _worker_pids(path):
            return True
        state = _state_dir(path)
        if not (state / "logs" / "orbit.log").exists():
            return False
        return not (state / "STOP").exists()
    except Exception:
        return False


def _slug(path: str) -> str:
    """The service slug for a target repo, byte-identical to
    install_service.sh: `orbit-` + the basename with every non-alphanumeric
    character replaced by a dash (tr -c, one dash per character - not
    squeezed), trailing dashes stripped."""
    name = re.sub(r"[^a-zA-Z0-9]", "-", Path(path).name)
    return "orbit-" + name.rstrip("-")


def launchd_label(path: str) -> str:
    """The launchd/systemd label for a target repo's loop. The dashboard reads
    it as ORBIT_LAUNCHD_LABEL to answer "is my loop loaded?" and to restart
    the right service - without it every child of a multi-project launcher
    inherits the default `com.orbit.autopilot` and restarts (or reports on)
    some OTHER project's service."""
    return f"com.orbit.{_slug(path)}"


def _concurrency(path: str) -> int:
    """The project's configured `concurrency` (how many slots its loop runs),
    default 1. Read straight from its .autopilot/config.yaml: engine/config.py
    would be the authority, but importing it drags in PyYAML, and the launcher
    is stdlib-only by contract. So try PyYAML if the interpreter happens to
    have it, else scan for a top-level `concurrency:` line (a commented-out or
    indented one is deliberately not a match). Any garbage means 1 - the
    default that reproduces today's single-loop behavior - and a real number
    is clamped to 1..32 rather than dropped, so a fat-fingered 99 starts a
    lot of slots instead of silently starting one."""
    cfg = Path(path) / ".autopilot" / "config.yaml"
    try:
        text = cfg.read_text()
    except OSError:
        return 1
    raw = None
    try:
        import yaml   # noqa: PLC0415 - optional, see above
        loaded = yaml.safe_load(text) or {}
        if isinstance(loaded, dict):
            raw = loaded.get("concurrency")
    except Exception:
        raw = None
    if raw is None:
        m = re.search(r"^concurrency:\s*([0-9]+)\s*$", text, re.MULTILINE)
        raw = m.group(1) if m else None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return min(max(n, 1), 32)


def _loop_argv(path: str) -> list[str]:
    """argv for one slot process. ORBIT_LOOP_CMD replaces the command (shlex
    split, target path still appended) so tests can spawn a tiny sleeper
    instead of a real Claude-driven loop."""
    override = os.environ.get("ORBIT_LOOP_CMD")
    if override:
        return shlex.split(override) + [path]
    return ["bash", str(RUN_SCRIPT), path]


def _is_git_repo(path: Path) -> bool:
    if (path / ".git").exists():
        return True
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "--git-dir"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


# ── launcher actions ─────────────────────────────────────────────────────────
def do_import(path: str) -> dict:
    path = (path or "").strip()
    if not path:
        return {"ok": False, "msg": "path is required", "has_autopilot": False}
    p = Path(path).expanduser()
    if not p.exists() or not p.is_dir():
        return {"ok": False, "msg": f"'{path}' does not exist or is not a directory", "has_autopilot": False}
    if not _is_git_repo(p):
        return {"ok": False, "msg": f"'{path}' is not a git repository", "has_autopilot": False}
    proj = registry_add(str(p.resolve()))
    ha = has_autopilot(proj["path"])
    return {"ok": True, "msg": f"Imported {proj['name']}.", "has_autopilot": ha}


def do_init(path: str) -> dict:
    proj = registry_get(path)
    if not proj:
        return {"ok": False, "msg": "project not registered — import it first"}
    p = proj["path"]
    if has_autopilot(p):
        return {"ok": False, "msg": "project already has .autopilot/ — nothing to initialize"}
    try:
        r = subprocess.run([sys.executable, str(INIT_SCRIPT), p, str(ORBIT_HOME)],
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "init timed out after 120s"}
    except Exception as e:
        return {"ok": False, "msg": f"failed to run init: {e}"}
    out = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
    if r.returncode != 0:
        return {"ok": False, "msg": f"init failed (rc={r.returncode}): {out[-2000:]}"}
    return {"ok": True, "msg": out[-2000:] or f"Initialized {proj['name']}."}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _child_alive(path: str) -> bool:
    with _LOCK:
        ch = CHILDREN.get(path)
        if not ch:
            return False
        proc = ch.get("proc")
        if proc is not None and proc.poll() is not None:
            del CHILDREN[path]
            return False
        return True


def register_child(path: str, port: int, proc=None) -> None:
    """Test/injection hook: wire a (possibly fake) child into the child table
    without actually spawning command_center.py."""
    with _LOCK:
        CHILDREN[str(Path(path).resolve())] = {"proc": proc, "port": port}


def set_active(path: str | None) -> None:
    with _LOCK:
        ACTIVE["path"] = str(Path(path).resolve()) if path else None


def stop_child(path: str) -> None:
    with _LOCK:
        ch = CHILDREN.pop(path, None)
    if not ch:
        return
    proc = ch.get("proc")
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass


def spawn_child(path: str) -> tuple[bool, str]:
    p = str(Path(path).resolve())
    if _child_alive(p):
        return True, "already running"
    port = _free_port()
    env = dict(os.environ)
    env.update({
        "AP_HOME": str(Path(p) / ".autopilot"),
        "AP_STATE": str(Path(p) / ".autopilot" / "state"),
        "ORBIT_REPO": p,
        "ORBIT_HOME": str(ORBIT_HOME),
        "CC_TOKEN": _TOKEN,
        "PORT": str(port),
        # Which service this dashboard's loop actually is. Without it every
        # child of a multi-project launcher fell back to command_center.py's
        # default `com.orbit.autopilot`, so its "loop loaded" light and its
        # Restart button both pointed at whatever project happened to own
        # that label - the restart-loop bug.
        "ORBIT_LAUNCHD_LABEL": launchd_label(p),
    })
    try:
        proc = subprocess.Popen([sys.executable, str(COMMAND_CENTER)], env=env, cwd=p,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return False, f"failed to spawn dashboard: {e}"
    with _LOCK:
        CHILDREN[p] = {"proc": proc, "port": port}
    deadline = time.time() + 5
    while time.time() < deadline:
        if proc.poll() is not None:
            with _LOCK:
                CHILDREN.pop(p, None)
            return False, f"dashboard child exited early (rc={proc.returncode})"
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=0.5)
            return True, "opened"
        except Exception:
            time.sleep(0.2)
    return False, "dashboard did not respond within 5s"


def do_open(path: str) -> dict:
    proj = registry_get(path)
    if not proj:
        return {"ok": False, "msg": "project not registered — import it first"}
    p = proj["path"]
    if not has_autopilot(p):
        return {"ok": False, "msg": "project has no .autopilot/ — initialize it first"}
    ok, msg = spawn_child(p)
    if not ok:
        return {"ok": False, "msg": msg}
    set_active(p)
    registry_touch(p)
    return {"ok": True, "msg": msg}


def do_loop_start(path: str) -> dict:
    """Start this project's loop: one detached engine/run.sh process per slot
    (0..concurrency-1). Disarms the STOP file first - a start with STOP still
    on disk would spawn slots that immediately idle."""
    proj = registry_get(path)
    if not proj:
        return {"ok": False, "msg": "project not registered - import it first"}
    p = proj["path"]
    if not has_autopilot(p):
        return {"ok": False, "msg": "project has no .autopilot/ - initialize it first"}
    state = _state_dir(p)
    logs = state / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "msg": f"could not create {logs}: {e}"}
    try:
        (state / "STOP").unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        return {"ok": False, "msg": f"could not clear the STOP file: {e}"}
    slots = _concurrency(p)
    label = launchd_label(p)
    _tracked_loop_pids(p)   # reap dead entries so a restart refills their slots
    started, kept, failed = [], [], []
    with _LOCK:
        table = LOOPS.setdefault(p, {})
        for slot in range(slots):
            if slot in table:
                kept.append(slot)   # already ours and alive (just reaped above)
                continue
            env = dict(os.environ)
            env.update({
                "ORBIT_HOME": str(ORBIT_HOME),
                "ORBIT_SLOT": str(slot),
                "ORBIT_SLOTS": str(slots),
                "ORBIT_LAUNCHD_LABEL": label,
            })
            try:
                # Append, never truncate: two starts of the same slot across a
                # launcher restart must not eat the first one's tail.
                log = open(logs / f"loop-{slot}.out", "ab")
            except OSError as e:
                failed.append(f"slot {slot}: {e}")
                continue
            try:
                # start_new_session: each slot gets its OWN session/process
                # group, so (a) Ctrl-C in the launcher's terminal does not
                # reach it - loops outlive the launcher by design (see the
                # atexit note) - and (b) loop-stop can signal exactly one
                # slot's group without touching the launcher or its siblings.
                proc = subprocess.Popen(_loop_argv(p), env=env, cwd=p, start_new_session=True,
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            except Exception as e:
                failed.append(f"slot {slot}: {e}")
                continue
            finally:
                log.close()   # the child holds its own dup of the fd
            table[slot] = proc
            started.append(slot)
        if not table:
            LOOPS.pop(p, None)
    if failed and not started:
        return {"ok": False, "msg": "could not start the loop - " + "; ".join(failed)}
    bits = [f"started {len(started)} slot(s) of {slots}"]
    if kept:
        bits.append(f"{len(kept)} already running")
    if failed:
        bits.append("failed: " + "; ".join(failed))
    return {"ok": True, "msg": f"{proj['name']}: " + ", ".join(bits),
            "slots": slots, "started": started}


def do_loop_stop(path: str) -> dict:
    """Stop this project's loop: arm the STOP file, wait out a short grace
    window, then SIGTERM the slot processes.

    STOP is deliberately LEFT ARMED afterwards (ledger ruling): a
    service-managed loop is restarted within seconds by launchd KeepAlive /
    systemd Restart, and a STOP-less restart would silently resume the work
    the operator just stopped. Armed, the restarted loop comes back up and
    idles. Resuming is therefore always an explicit loop-start (which clears
    the file), never an accident."""
    proj = registry_get(path)
    if not proj:
        return {"ok": False, "msg": "project not registered - import it first"}
    p = proj["path"]
    state = _state_dir(p)
    try:
        state.mkdir(parents=True, exist_ok=True)
        (state / "STOP").write_text(f"stopped by the launcher at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except OSError as e:
        return {"ok": False, "msg": f"could not write the STOP file: {e}"}
    if LOOP_STOP_GRACE > 0:
        time.sleep(LOOP_STOP_GRACE)
    tracked = _tracked_loop_pids(p)
    # Untracked loops (launchd, systemd, a previous launcher) are only ever
    # signalled when THIS project's own workers/*.pid names them - the pid
    # files are the whole allowlist. Nothing here ever pgreps the machine.
    extra = [pid for pid in _worker_pids(p) if pid not in tracked]
    signalled, errors = [], []
    for pid in tracked + extra:
        try:
            # Signal the GROUP when the loop leads its own one, so the cycle it
            # has in flight goes with it instead of being orphaned (same
            # discipline as command_center.do_killtask). Our own spawns always
            # qualify - start_new_session makes each slot a session leader -
            # and so does a launchd/systemd job. A loop that is NOT its
            # group's leader was backgrounded from some other process's shell,
            # whose group is not ours to blast: TERM that pid alone.
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
            signalled.append(pid)
        except ProcessLookupError:
            pass   # already gone - STOP stays armed either way
        except OSError as e:
            errors.append(f"pid {pid}: {e}")
    with _LOCK:
        LOOPS.pop(p, None)
    msg = f"{proj['name']}: STOP armed"
    msg += f", TERMed {len(signalled)} loop process(es)" if signalled else ", no live loop process found"
    if errors:
        msg += " (" + "; ".join(errors) + ")"
    return {"ok": True, "msg": msg, "signalled": signalled}


def do_create(path: str, name: str = "", product: bool = False) -> dict:
    """Create a brand-new workspace at `path`: mkdir -p, `git init`,
    install/init.py (--product for a PM workspace), register it, and bring up
    its dashboard child. Refuses to touch a directory that already has
    anything in it - scaffolding into someone's existing repo is what
    /_launcher/import + /_launcher/init are for."""
    path = (path or "").strip()
    if not path:
        return {"ok": False, "msg": "path is required"}
    p = Path(path).expanduser()
    if not p.is_absolute():
        return {"ok": False, "msg": "path must be absolute"}
    if p.exists():
        if not p.is_dir():
            return {"ok": False, "msg": f"'{p}' exists and is not a directory"}
        try:
            nonempty = any(p.iterdir())
        except OSError as e:
            return {"ok": False, "msg": f"could not read '{p}': {e}"}
        if nonempty:
            return {"ok": False, "msg": f"'{p}' already exists and is not empty - import it instead"}
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "msg": f"could not create '{p}': {e}"}
    p = p.resolve()
    if not _is_git_repo(p):
        try:
            r = subprocess.run(["git", "init", "-q", str(p)], capture_output=True, text=True, timeout=60)
        except Exception as e:
            return {"ok": False, "msg": f"git init failed: {e}"}
        if r.returncode != 0:
            return {"ok": False, "msg": f"git init failed: {(r.stderr or r.stdout or '').strip()[-500:]}"}
    argv = [sys.executable, str(INIT_SCRIPT), str(p), str(ORBIT_HOME)]
    if product:
        argv.append("--product")
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "init timed out after 120s"}
    except Exception as e:
        return {"ok": False, "msg": f"failed to run init: {e}"}
    if r.returncode != 0:
        out = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
        return {"ok": False, "msg": f"init failed (rc={r.returncode}): {out[-2000:]}"}
    proj = registry_add(str(p), name=name)
    # A workspace with no dashboard is a workspace an operator can't see, so
    # bring the child up now - but a spawn failure is not a create failure:
    # the scaffold is on disk and registered, and /_launcher/open retries.
    ok, spawn_msg = spawn_child(str(p))
    return {"ok": True, "project": proj, "opened": ok,
            "msg": f"Created {proj['name']} at {p}." + ("" if ok else f" Dashboard not up yet: {spawn_msg}")}


def do_close() -> dict:
    set_active(None)
    return {"ok": True}


def do_remove(path: str) -> dict:
    # Unregisters and drops the DASHBOARD child only. A running loop is left
    # running on purpose, same ruling as the atexit reaper below: loop
    # processes are long-lived work and are only ever stopped explicitly
    # (loop-stop, or the workspace's own STOP file).
    p = str(Path(path).resolve())
    stop_child(p)
    removed = registry_remove(p)
    with _LOCK:
        if ACTIVE["path"] == p:
            ACTIVE["path"] = None
    return {"ok": removed}


def launcher_state() -> dict:
    reg = _load_registry()
    projects = []
    for proj in reg["projects"]:
        path = proj["path"]
        ha = has_autopilot(path)
        projects.append({
            "name": proj["name"],
            "path": path,
            "has_autopilot": ha,
            "loop_running": _loop_running(path) if ha else False,
            # Configured slot count and how many of them are this launcher's
            # own live processes - the tab strip badge reads "2/2" from these.
            # slots is 0 for an un-initialized project (no config to read).
            "loop_slots": _concurrency(path) if ha else 0,
            "loop_tracked": len(_tracked_loop_pids(path)) if ha else 0,
        })
    with _LOCK:
        active = ACTIVE["path"]
    active_name = None
    if active:
        m = next((x for x in reg["projects"] if x["path"] == active), None)
        active_name = m["name"] if m else Path(active).name
    return {"active": active, "active_name": active_name, "projects": projects}


# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str, code=200, cache: str | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Default stays no-store (the launcher's own shell/JSON routes must
        # never be cached) — but a proxied response can override this to
        # pass the CHILD's own Cache-Control through unchanged (see _proxy;
        # e.g. command_center.py's /vendor/* opts into a long max-age, which
        # this hardcoding used to silently drop, forcing a refetch of the
        # 435 KB vendored lib on every load behind the launcher).
        self.send_header("Cache-Control", cache or "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code=200):
        self._send(json.dumps(obj).encode(), "application/json", code)

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in _ALLOWED_HOSTS

    def _origin_ok(self) -> bool:
        for h in ("Origin", "Referer"):
            v = self.headers.get(h)
            if v and urllib.parse.urlparse(v).netloc not in _ALLOWED_HOSTS:
                return False
        return True

    def _token_ok(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-CC-Token", ""), _TOKEN)

    def _serve_shell(self):
        if not SHELL_FILE.exists():
            self._send(b"engine/launcher_shell.html is missing - restore it and reload",
                       "text/plain", 500)
            return
        body = SHELL_FILE.read_text().replace("__TOKEN__", _TOKEN)
        self._send(body.encode(), "text/html; charset=utf-8")

    def _proxy(self):
        # Security-critical path — see the module docstring / build contract:
        # FIRST the launcher's own POST defense (Origin + shared token), THEN
        # (only once that passes) does an inactive-project 503 or a real
        # network call to the child ever happen.
        method = self.command
        if method == "POST" and (not self._origin_ok() or not self._token_ok()):
            self._json({"ok": False, "msg": "forbidden origin/token"}, 403)
            return
        # Raw-socket netloc injection guard: a request-line target that isn't
        # a proper origin-form path (e.g. an absolute-URI "http://evil/x" or
        # a "//host/x" schema-relative form sent straight over a raw socket)
        # must never be spliced into the outbound child URL below.
        if not self.path.startswith("/"):
            self._json({"ok": False, "msg": "bad request path"}, 400)
            return
        with _LOCK:
            active = ACTIVE["path"]
            alive = active is not None and _child_alive(active)
            if not alive:
                # Self-heal: a dead active child shouldn't keep GET / stuck
                # proxying into a 503 forever — clear it so the picker comes
                # back on the next request (do_GET's "/" branch relies on
                # this, but any route benefits from it).
                if active is not None:
                    ACTIVE["path"] = None
            else:
                port = CHILDREN[active]["port"]
        if not alive:
            self._json({"ok": False, "msg": "no active project — open one from the picker"}, 503)
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            self._json({"ok": False, "msg": "malformed Content-Length"}, 400)
            return
        body = self.rfile.read(length) if length else None
        url = f"http://127.0.0.1:{port}{self.path}"
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP}
        headers["Host"] = f"127.0.0.1:{port}"
        headers["Origin"] = f"http://127.0.0.1:{port}"
        # Rewrite Referer to the child's own netloc too — the browser sends
        # "Referer: http://127.0.0.1:<launcher-port>/", but the child's own
        # _origin_ok() checks Referer against ITS allowed-hosts (its own
        # ephemeral port). Left unrewritten, every proxied POST would 403 at
        # the child once a project is open.
        headers["Referer"] = f"http://127.0.0.1:{port}/"
        headers["X-CC-Token"] = _TOKEN
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with _PROXY_OPENER.open(req, timeout=30) as resp:
                out = resp.read()
                ctype = resp.headers.get("Content-Type", "application/octet-stream")
                cache = resp.headers.get("Cache-Control")
                code = resp.status
        except urllib.error.HTTPError as e:
            out = e.read()
            ctype = (e.headers.get("Content-Type") if e.headers else None) or "text/plain"
            cache = (e.headers.get("Cache-Control") if e.headers else None)
            code = e.code
        except Exception as e:
            self._json({"ok": False, "msg": f"child unreachable: {e}"}, 502)
            return
        self._send(out, ctype, code, cache=cache)

    def _handle_launcher_post(self, path: str, data: dict):
        if path == "/_launcher/import":
            self._json(do_import((data.get("path") or "").strip())); return
        if path == "/_launcher/init":
            self._json(do_init((data.get("path") or "").strip())); return
        if path == "/_launcher/create":
            self._json(do_create((data.get("path") or "").strip(),
                                 (data.get("name") or "").strip(),
                                 bool(data.get("product")))); return
        if path == "/_launcher/open":
            self._json(do_open((data.get("path") or "").strip())); return
        if path == "/_launcher/loop-start":
            self._json(do_loop_start((data.get("path") or "").strip())); return
        if path == "/_launcher/loop-stop":
            self._json(do_loop_stop((data.get("path") or "").strip())); return
        if path == "/_launcher/close":
            self._json(do_close()); return
        if path == "/_launcher/remove":
            self._json(do_remove((data.get("path") or "").strip())); return
        self._json({"ok": False, "msg": "unknown launcher route"}, 404)

    def do_GET(self):
        if not self._host_ok():
            self._send(b"forbidden host", "text/plain", 403); return
        path = urllib.parse.urlparse(self.path).path
        if path == "/_launcher/state":
            self._json(launcher_state()); return
        if path.startswith("/_launcher/"):
            # Reserved namespace is never proxied — even for routes this
            # handler doesn't otherwise recognize (matches the module
            # docstring's "Reserved routes ... never proxied" contract).
            self._json({"ok": False, "msg": "not found"}, 404); return
        if path == "/":
            with _LOCK:
                active = ACTIVE["path"]
                alive = active is not None and _child_alive(active)
                picker = not alive
                if picker and active is not None:
                    # Self-heal here too, not just in _proxy's 503 branch —
                    # a dead active child must not keep GET / stuck behind
                    # a stale ACTIVE pointer with no way back to the picker.
                    ACTIVE["path"] = None
            if picker:
                self._serve_shell()
            else:
                self._proxy()
            return
        self._proxy()

    def do_POST(self):
        if not self._host_ok():
            self._send(b"forbidden host", "text/plain", 403); return
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/_launcher/"):
            if not self._origin_ok() or not self._token_ok():
                self._json({"ok": False, "msg": "forbidden host/origin/token"}, 403); return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):
                self._json({"ok": False, "msg": "malformed Content-Length"}, 400); return
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode()) if raw else {}
            except Exception:
                data = {}
            self._handle_launcher_post(path, data)
            return
        self._proxy()

    def log_message(self, *a):  # quiet
        pass


# Dashboard children die with the launcher: they are pure UI in front of a
# state dir, and a dashboard with no launcher to proxy to is unreachable.
# LOOPS is deliberately NOT touched here (ledger ruling): a slot loop is
# long-lived work, mid-cycle when the launcher happens to exit, and stopping
# it is always an explicit act (loop-stop / the STOP file). The cost is that a
# launcher restart can find loops it no longer tracks - which is exactly the
# case _loop_running's workers/*.pid signal and loop-stop's untracked-pid pass
# exist to handle.
atexit.register(lambda: [stop_child(p) for p in list(CHILDREN.keys())])


def main():
    print(f"Orbit launcher → http://127.0.0.1:{PORT}")
    print("Ctrl-C stops the launcher (and any child dashboards it spawned).")
    print("Loops keep running - stop them from a tab (or with the workspace's STOP file).")
    # atexit handlers don't run on a bare SIGTERM (kill, launchd-stop,
    # terminal-close) — translate it into a normal SystemExit so the
    # atexit.register'd child-reaper below actually fires and no child
    # dashboards are orphaned.
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
