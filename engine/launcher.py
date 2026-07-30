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

Reserved routes (never proxied — see the class-level docstring on Handler):
  GET  /_launcher/state    registry + active project + per-project flags
  POST /_launcher/import   {path} -> validate + register a git repo
  POST /_launcher/init     {path} -> scaffold .autopilot/ (install/init.py)
  POST /_launcher/open     {path} -> spawn/reuse a command_center.py child, activate it
  POST /_launcher/close    deselect the active project (back to the picker)
  POST /_launcher/remove   {path} -> unregister (stops its child if running)
  GET  /                   picker (no active project) or proxy (active project)
Everything else proxies to the active child; 503 JSON if none is active.
"""
from __future__ import annotations

import atexit
import hmac
import json
import os
import secrets
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


def registry_add(path: str) -> dict:
    """Register a project, deduped on its resolved absolute path. Idempotent."""
    p = str(Path(path).resolve())
    reg = _load_registry()
    for proj in reg["projects"]:
        if proj["path"] == p:
            return proj
    proj = {"name": Path(p).name, "path": p, "added_at": time.time(), "last_opened": None}
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


def _loop_running(path: str) -> bool:
    # Best-effort, never crash: a loop is considered "running" when its state
    # dir exists and no STOP kill-switch is present.
    try:
        state = Path(path) / ".autopilot" / "state"
        if not state.exists():
            return False
        return not (state / "STOP").exists()
    except Exception:
        return False


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


def do_close() -> dict:
    set_active(None)
    return {"ok": True}


def do_remove(path: str) -> dict:
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
        if path == "/_launcher/open":
            self._json(do_open((data.get("path") or "").strip())); return
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


atexit.register(lambda: [stop_child(p) for p in list(CHILDREN.keys())])


def main():
    print(f"Orbit launcher → http://127.0.0.1:{PORT}")
    print("Ctrl-C stops the launcher (and any child dashboards it spawned).")
    # atexit handlers don't run on a bare SIGTERM (kill, launchd-stop,
    # terminal-close) — translate it into a normal SystemExit so the
    # atexit.register'd child-reaper below actually fires and no child
    # dashboards are orphaned.
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
