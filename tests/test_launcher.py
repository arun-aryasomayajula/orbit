# launcher.py — standalone multi-project front door: registry CRUD, import
# validation, non-interactive init scaffolding, workspace creation, the loop
# process manager (per-project run.sh slots), and the proxy's security gate
# (Host/Origin/token checked BEFORE any request ever reaches a child).
#
# Run: python3.11 -m pytest tests/test_launcher.py -v   (from orbit/)
import http.server
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, ENGINE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def launcher(tmp_path, monkeypatch):
    # Hermetic registry (env override read at import time, per launcher.py's
    # own convention) + a fixed shared token so tests can assert on it.
    registry_path = tmp_path / "home" / ".orbit" / "projects.json"
    monkeypatch.setenv("ORBIT_REGISTRY", str(registry_path))
    monkeypatch.setenv("CC_TOKEN", "test-shared-token-xyz")
    return _mod("launcher")


# ── tiny HTTP server helpers (launcher's own server + a fake "child") ────────
class _RunningServer:
    def __init__(self, server, thread):
        self.server = server
        self.thread = thread

    @property
    def port(self):
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _start(handler_cls):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return _RunningServer(srv, t)


def _start_launcher(mod):
    # Bind to an OS-assigned port, then patch the module's own allowed-hosts
    # set to match — avoids picking a port then racing to rebind it.
    rs = _start(mod.Handler)
    mod.PORT = rs.port
    mod._ALLOWED_HOSTS = {f"127.0.0.1:{rs.port}", f"localhost:{rs.port}", f"[::1]:{rs.port}"}
    return rs


_STUB_HITS = []  # appended to by StubHandler; each proxy test clears it


class StubHandler(http.server.BaseHTTPRequestHandler):
    """Fake command_center.py child: echoes back what it received so the
    test can assert the proxy rewrote Host/Origin/token."""

    def _reply(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        info = {"method": self.command, "path": self.path,
                "host": self.headers.get("Host"), "origin": self.headers.get("Origin"),
                "referer": self.headers.get("Referer"),
                "token": self.headers.get("X-CC-Token")}
        _STUB_HITS.append(info)
        body = json.dumps(info).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Mirrors the real child (command_center.py): every route sends its
        # OWN Cache-Control (default "no-store, must-revalidate", except
        # /vendor/* which opts into a long max-age) — see
        # test_proxy_passes_through_child_cache_control_header below.
        cache = "public, max-age=86400" if self.path.startswith("/vendor/") else "no-store, must-revalidate"
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply()

    def do_POST(self):
        self._reply()

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture()
def proxied(launcher, tmp_path):
    _STUB_HITS.clear()
    stub = _start(StubHandler)
    lch = _start_launcher(launcher)
    fake_child_path = str(tmp_path / "child-project")
    launcher.register_child(fake_child_path, stub.port)
    launcher.set_active(fake_child_path)
    yield launcher, lch, stub
    lch.close()
    stub.close()


# ── registry: add/list/remove round-trips, dedupe, atomic write ─────────────
def test_registry_add_and_list_round_trip(launcher, tmp_path):
    p1 = tmp_path / "p1"
    p1.mkdir()
    proj = launcher.registry_add(str(p1))
    assert proj["path"] == str(p1.resolve())
    assert proj["name"] == "p1"
    assert proj["added_at"] is not None
    assert proj["last_opened"] is None
    listed = launcher.registry_list()
    assert len(listed) == 1
    assert listed[0]["path"] == str(p1.resolve())


def test_registry_add_dedupes_on_same_absolute_path(launcher, tmp_path):
    p1 = tmp_path / "p1"
    p1.mkdir()
    first = launcher.registry_add(str(p1))
    second = launcher.registry_add(str(p1))
    assert first == second
    assert len(launcher.registry_list()) == 1


def test_registry_remove_round_trip(launcher, tmp_path):
    p1 = tmp_path / "p1"
    p1.mkdir()
    launcher.registry_add(str(p1))
    assert launcher.registry_remove(str(p1)) is True
    assert launcher.registry_list() == []
    assert launcher.registry_remove(str(p1)) is False  # already gone


def test_registry_write_is_atomic_temp_then_rename(launcher, tmp_path, monkeypatch):
    p1 = tmp_path / "p1"
    p1.mkdir()
    real_replace = launcher.os.replace
    calls = []

    def spy_replace(src, dst):
        assert Path(src).exists()
        assert Path(src).parent == Path(dst).parent
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(launcher.os, "replace", spy_replace)
    launcher.registry_add(str(p1))
    assert len(calls) == 1
    leftovers = list(launcher.REGISTRY.parent.glob(".projects-*"))
    assert leftovers == []


# ── import validation ────────────────────────────────────────────────────────
def test_do_import_rejects_nonexistent_path(launcher, tmp_path):
    ghost = tmp_path / "does-not-exist"
    result = launcher.do_import(str(ghost))
    assert result["ok"] is False
    assert "not exist" in result["msg"]
    assert launcher.registry_list() == []


def test_do_import_rejects_non_git_dir(launcher, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    result = launcher.do_import(str(d))
    assert result["ok"] is False
    assert "git" in result["msg"].lower()
    assert launcher.registry_list() == []


def test_do_import_accepts_git_repo_without_autopilot(launcher, tmp_path):
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    result = launcher.do_import(str(repo))
    assert result["ok"] is True
    assert result["has_autopilot"] is False
    assert len(launcher.registry_list()) == 1


def test_do_import_detects_existing_autopilot(launcher, tmp_path):
    repo = tmp_path / "gitrepo2"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".autopilot").mkdir()
    result = launcher.do_import(str(repo))
    assert result["ok"] is True
    assert result["has_autopilot"] is True


# ── open / close / remove ────────────────────────────────────────────────────
def test_do_open_rejects_unregistered_path(launcher, tmp_path):
    repo = tmp_path / "notreg"
    repo.mkdir()
    result = launcher.do_open(str(repo))
    assert result["ok"] is False
    assert "not registered" in result["msg"]


def test_do_open_rejects_without_autopilot(launcher, tmp_path):
    repo = tmp_path / "noap"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    launcher.registry_add(str(repo))
    result = launcher.do_open(str(repo))
    assert result["ok"] is False
    assert "autopilot" in result["msg"].lower()


def test_do_open_success_sets_active_and_touches_registry(launcher, tmp_path, monkeypatch):
    repo = tmp_path / "openok"
    repo.mkdir()
    (repo / ".autopilot").mkdir()
    launcher.registry_add(str(repo))
    monkeypatch.setattr(launcher, "spawn_child", lambda p: (True, "opened"))
    result = launcher.do_open(str(repo))
    assert result["ok"] is True
    assert launcher.ACTIVE["path"] == str(repo.resolve())
    assert launcher.registry_get(str(repo))["last_opened"] is not None


def test_do_close_deselects_active(launcher, tmp_path):
    launcher.set_active(str(tmp_path))
    result = launcher.do_close()
    assert result == {"ok": True}
    assert launcher.ACTIVE["path"] is None


def test_do_remove_unregisters_and_stops_child(launcher, tmp_path):
    repo = tmp_path / "rm"
    repo.mkdir()
    launcher.registry_add(str(repo))
    stopped = []

    class FakeProc:
        def terminate(self):
            stopped.append(True)

        def wait(self, timeout=None):
            return 0

    launcher.register_child(str(repo), 12345, proc=FakeProc())
    launcher.set_active(str(repo))
    result = launcher.do_remove(str(repo))
    assert result["ok"] is True
    assert launcher.registry_list() == []
    assert stopped == [True]
    assert launcher.ACTIVE["path"] is None
    assert str(repo.resolve()) not in launcher.CHILDREN


def test_spawn_child_passes_launchd_label_for_this_project(launcher, tmp_path, monkeypatch):
    # The multi-project restart bug: without ORBIT_LAUNCHD_LABEL every
    # dashboard child fell back to command_center.py's default
    # `com.orbit.autopilot`, so its loop-loaded light and Restart button both
    # pointed at whichever project happened to own that label.
    repo = tmp_path / "label repo.v2"
    repo.mkdir()
    (repo / ".autopilot").mkdir()
    launcher.registry_add(str(repo))
    captured = {}

    class FakeProc:
        def poll(self):
            return None

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(launcher.subprocess, "Popen",
                        lambda argv, env=None, cwd=None, **kw: (captured.update(env=env), FakeProc())[1])
    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    ok, _ = launcher.spawn_child(str(repo))
    assert ok is True
    # install_service.sh's slug: every non-alphanumeric char becomes a dash,
    # trailing dashes stripped.
    assert captured["env"]["ORBIT_LAUNCHD_LABEL"] == "com.orbit.orbit-label-repo-v2"
    assert launcher.launchd_label(str(repo)) == "com.orbit.orbit-label-repo-v2"


def test_launchd_label_matches_install_service_slug(launcher, tmp_path):
    for basename, expected in [("orbit", "com.orbit.orbit-orbit"),
                               ("cdp-analytics", "com.orbit.orbit-cdp-analytics"),
                               ("My Repo!!", "com.orbit.orbit-My-Repo"),
                               ("dots.in.name", "com.orbit.orbit-dots-in-name")]:
        assert launcher.launchd_label(str(tmp_path / basename)) == expected


def test_spawn_child_passes_expected_env_and_reuses_live_child(launcher, tmp_path, monkeypatch):
    repo = tmp_path / "spawnrepo"
    repo.mkdir()
    (repo / ".autopilot").mkdir()
    launcher.registry_add(str(repo))
    captured = {}

    class FakeProc:
        def poll(self):
            return None  # still running

    def fake_popen(argv, env=None, cwd=None, **kwargs):
        captured["argv"] = argv
        captured["env"] = env
        captured["cwd"] = cwd
        return FakeProc()

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda *a, **k: FakeResp())

    ok, msg = launcher.spawn_child(str(repo))
    assert ok is True
    assert captured["argv"] == [sys.executable, str(launcher.COMMAND_CENTER)]
    env = captured["env"]
    assert env["AP_HOME"] == str(repo.resolve() / ".autopilot")
    assert env["AP_STATE"] == str(repo.resolve() / ".autopilot" / "state")
    assert env["ORBIT_REPO"] == str(repo.resolve())
    assert env["ORBIT_HOME"] == str(launcher.ORBIT_HOME)
    assert env["CC_TOKEN"] == launcher._TOKEN
    assert "PORT" in env

    # re-open reuses the live child — Popen must not be called a second time
    ok2, msg2 = launcher.spawn_child(str(repo))
    assert ok2 is True
    assert msg2 == "already running"


# ── init (non-interactive scaffold) ──────────────────────────────────────────
def test_do_init_scaffolds_autopilot_for_registered_repo(launcher, tmp_path):
    repo = tmp_path / "initrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    launcher.registry_add(str(repo))
    result = launcher.do_init(str(repo))
    assert result["ok"] is True, result["msg"]
    assert (repo / ".autopilot" / "config.yaml").exists()
    assert (repo / ".autopilot" / "backlog.yaml").exists()


def test_do_init_rejects_unregistered_path(launcher, tmp_path):
    repo = tmp_path / "unreg"
    repo.mkdir()
    result = launcher.do_init(str(repo))
    assert result["ok"] is False
    assert "not registered" in result["msg"]


def test_do_init_rejects_when_autopilot_already_present(launcher, tmp_path):
    repo = tmp_path / "already"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".autopilot").mkdir()
    launcher.registry_add(str(repo))
    result = launcher.do_init(str(repo))
    assert result["ok"] is False
    assert ".autopilot" in result["msg"]


def test_do_init_invokes_init_script_with_expected_argv(launcher, tmp_path, monkeypatch):
    repo = tmp_path / "spyrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    launcher.registry_add(str(repo))
    calls = []

    class FakeCompleted:
        returncode = 0
        stdout = "scaffolded"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return FakeCompleted()

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    result = launcher.do_init(str(repo))
    assert result["ok"] is True
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == sys.executable
    assert argv[1] == str(launcher.INIT_SCRIPT)
    assert argv[2] == str(repo.resolve())
    assert argv[3] == str(launcher.ORBIT_HOME)


# ── /_launcher/state shape (direct + HTTP) ───────────────────────────────────
def test_launcher_state_function_shape(launcher, tmp_path):
    repo = tmp_path / "p"
    repo.mkdir()
    launcher.registry_add(str(repo))
    state = launcher.launcher_state()
    assert state["active"] is None
    assert state["active_name"] is None
    assert isinstance(state["projects"], list)
    assert state["projects"][0]["path"] == str(repo.resolve())
    assert state["projects"][0]["has_autopilot"] is False
    assert state["projects"][0]["loop_running"] is False


def test_launcher_state_loop_running_ignores_orbit_log_and_stop(launcher, tmp_path):
    # F3: the orbit.log-plus-no-STOP heuristic is GONE. A workspace with a log
    # (proof a loop ran here at some point) and no live pid file is not
    # running, and arming STOP does not change that either way - STOP says
    # "claim nothing", never "the process is gone".
    repo = tmp_path / "looprepo"
    repo.mkdir()
    ap_state = repo / ".autopilot" / "state"
    (ap_state / "logs").mkdir(parents=True)
    (ap_state / "logs" / "orbit.log").write_text("[2026-08-22 10:00:00] [slot 0] tick\n")
    launcher.registry_add(str(repo))
    assert launcher.launcher_state()["projects"][0]["loop_running"] is False
    (ap_state / "STOP").write_text("stopped\n")
    assert launcher.launcher_state()["projects"][0]["loop_running"] is False


def test_launcher_state_loop_running_reads_the_loop_pid_file(launcher, tmp_path):
    # The signal that REPLACED the heuristic: run.sh's lifetime
    # loop-<slot>.pid. Live pid = that slot is owned, cycle or no cycle, STOP
    # armed or not.
    repo = tmp_path / "looppid"
    repo.mkdir()
    workers = repo / ".autopilot" / "state" / "workers"
    workers.mkdir(parents=True)
    (workers / "loop-0.pid").write_text(json.dumps(
        {"pid": os.getpid(), "slot": "0", "started": "2026-08-22T00:00:00Z"}))
    launcher.registry_add(str(repo))
    assert launcher.launcher_state()["projects"][0]["loop_running"] is True
    (repo / ".autopilot" / "state" / "STOP").write_text("paused\n")
    assert launcher._loop_running(str(repo)) is True      # paused, still owned
    (workers / "loop-0.pid").write_text(json.dumps({"pid": 999999999, "slot": "0"}))
    assert launcher._loop_running(str(repo)) is False     # dead pid = unowned


def test_launcher_state_loop_not_running_for_a_never_started_workspace(launcher, tmp_path):
    # A freshly scaffolded workspace has a state dir and no STOP file, which
    # the old heuristic reported as "running" - a badge that lied about every
    # brand-new project.
    repo = tmp_path / "fresh"
    (repo / ".autopilot" / "state").mkdir(parents=True)
    launcher.registry_add(str(repo))
    assert launcher.launcher_state()["projects"][0]["loop_running"] is False


def test_launcher_state_route_over_http(launcher, tmp_path):
    repo = tmp_path / "proj1"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    launcher.registry_add(str(repo))
    lch = _start_launcher(launcher)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{lch.port}/_launcher/state", timeout=5) as resp:
            data = json.loads(resp.read())
        assert data["active"] is None
        assert len(data["projects"]) == 1
        assert data["projects"][0]["path"] == str(repo.resolve())
    finally:
        lch.close()


def test_root_serves_picker_when_no_active_project(launcher):
    lch = _start_launcher(launcher)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{lch.port}/", timeout=5) as resp:
            body = resp.read().decode()
        assert "__TOKEN__" not in body
        assert launcher._TOKEN in body
        assert "Orbit" in body
    finally:
        lch.close()


def test_root_serves_picker_when_active_child_is_dead(launcher, tmp_path):
    # A crashed child used to make every GET / a 503 blob with no way back
    # to the picker (ACTIVE stayed pointed at a dead child forever). GET /
    # must fall back to the picker instead, and self-heal ACTIVE.
    class DeadProc:
        def poll(self):
            return 1  # already exited

    repo = tmp_path / "deadchild"
    repo.mkdir()
    launcher.register_child(str(repo), 1, proc=DeadProc())
    launcher.set_active(str(repo))
    lch = _start_launcher(launcher)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{lch.port}/", timeout=5) as resp:
            code = resp.status
            body = resp.read().decode()
        assert code == 200
        assert launcher._TOKEN in body
        assert "Orbit" in body
        assert launcher.ACTIVE["path"] is None   # self-healed
    finally:
        lch.close()


# ── proxy mechanics ───────────────────────────────────────────────────────────
def test_proxy_503_when_no_active_project(launcher):
    lch = _start_launcher(launcher)
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"http://127.0.0.1:{lch.port}/api/state", timeout=5)
        assert ei.value.code == 503
        body = json.loads(ei.value.read())
        assert body["ok"] is False
    finally:
        lch.close()


def test_proxy_get_rewrites_host_and_origin_and_injects_token(proxied):
    mod, lch, stub = proxied
    with urllib.request.urlopen(f"http://127.0.0.1:{lch.port}/api/state", timeout=5) as resp:
        data = json.loads(resp.read())
    assert data["method"] == "GET"
    assert data["path"] == "/api/state"
    assert data["host"] == f"127.0.0.1:{stub.port}"
    assert data["origin"] == f"http://127.0.0.1:{stub.port}"
    assert data["token"] == mod._TOKEN
    assert len(_STUB_HITS) == 1


def test_proxy_passes_through_child_cache_control_header(proxied):
    # The bug: _send used to hardcode "no-store, must-revalidate" on every
    # proxied response, silently dropping whatever Cache-Control the child
    # actually sent — so command_center.py's /vendor/* route opting into
    # `public, max-age=86400` (for the 435 KB vendored cytoscape.js) was lost
    # behind the launcher, forcing a refetch on every load. The launcher must
    # forward the CHILD's own header value instead of substituting its own.
    mod, lch, stub = proxied
    with urllib.request.urlopen(f"http://127.0.0.1:{lch.port}/vendor/cytoscape.min.js", timeout=5) as resp:
        resp.read()
        assert resp.headers.get("Cache-Control") == "public, max-age=86400"


def test_proxy_still_defaults_to_no_store_when_child_says_no_store(proxied):
    # Every OTHER route (the child's own default) must still come through
    # as no-store — this isn't "cache everything proxied", it's "trust the
    # child's own Cache-Control instead of overriding it".
    mod, lch, stub = proxied
    with urllib.request.urlopen(f"http://127.0.0.1:{lch.port}/api/state", timeout=5) as resp:
        resp.read()
        assert resp.headers.get("Cache-Control") == "no-store, must-revalidate"


def test_proxy_post_with_valid_origin_and_token_reaches_child_rewritten(proxied):
    mod, lch, stub = proxied
    req = urllib.request.Request(
        f"http://127.0.0.1:{lch.port}/promote", data=b"id=task-1", method="POST",
        headers={"Origin": f"http://127.0.0.1:{lch.port}", "X-CC-Token": mod._TOKEN,
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    assert data["method"] == "POST"
    assert data["path"] == "/promote"
    assert data["host"] == f"127.0.0.1:{stub.port}"
    assert data["origin"] == f"http://127.0.0.1:{stub.port}"
    assert data["token"] == mod._TOKEN
    assert len(_STUB_HITS) == 1


def test_proxy_post_rewrites_browser_referer_to_child_netloc(proxied):
    # The real bug: a browser always sends "Referer: http://127.0.0.1:<port>/"
    # for a same-origin POST — here that's the LAUNCHER's own port, not the
    # child's. Left unrewritten, the child's own _origin_ok() would reject it
    # (it checks Referer against its own ephemeral-port allowed-hosts), 403ing
    # every state-changing action once a project is open. The proxy must
    # rewrite Referer to the child's own netloc, same as it does for Origin.
    mod, lch, stub = proxied
    browser_referer = f"http://127.0.0.1:{lch.port}/"
    req = urllib.request.Request(
        f"http://127.0.0.1:{lch.port}/promote", data=b"id=task-1", method="POST",
        headers={"Origin": f"http://127.0.0.1:{lch.port}", "X-CC-Token": mod._TOKEN,
                 "Referer": browser_referer,
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    assert data["referer"] == f"http://127.0.0.1:{stub.port}/"
    assert data["referer"] != browser_referer
    assert len(_STUB_HITS) == 1


def test_proxy_rejects_post_with_wrong_token_before_proxying(proxied):
    mod, lch, stub = proxied
    req = urllib.request.Request(
        f"http://127.0.0.1:{lch.port}/promote", data=b"id=task-1", method="POST",
        headers={"Origin": f"http://127.0.0.1:{lch.port}", "X-CC-Token": "wrong-token",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 403
    assert _STUB_HITS == []  # never reached the child


def test_proxy_rejects_post_with_bad_origin_before_proxying(proxied):
    mod, lch, stub = proxied
    req = urllib.request.Request(
        f"http://127.0.0.1:{lch.port}/promote", data=b"id=task-1", method="POST",
        headers={"Origin": "http://evil.example.com", "X-CC-Token": mod._TOKEN,
                 "Content-Type": "application/x-www-form-urlencoded"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 403
    assert _STUB_HITS == []  # never reached the child


def test_proxy_rejects_wrong_host_header(proxied):
    mod, lch, stub = proxied
    # http.client sends Host matching the connection target; simulate a
    # DNS-rebound name by connecting via a raw socket with a spoofed Host.
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", lch.port, timeout=5)
    try:
        conn.putrequest("GET", "/api/state", skip_host=True)
        conn.putheader("Host", "evil.example.com")
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 403
        resp.read()
    finally:
        conn.close()
    assert _STUB_HITS == []


# ── loop process manager (LOOPS, loop-start / loop-stop, _loop_running) ──────
def _wait_for(cond, timeout=8.0):
    """Poll `cond` until it's true (processes and their side effects are
    asynchronous - never assert on a spawn without waiting for it)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _workspace(tmp_path, name="ws", concurrency=None):
    """A minimally initialized workspace: .autopilot/state + a config.yaml."""
    repo = tmp_path / name
    (repo / ".autopilot" / "state").mkdir(parents=True)
    cfg = 'repo: "."\nbase_branch: "main"\n'
    if concurrency is not None:
        cfg += f"concurrency: {concurrency}\n"
    else:
        cfg += "# concurrency: 4\n"   # the commented-out default init.py writes
    (repo / ".autopilot" / "config.yaml").write_text(cfg)
    return repo


def _stray_loop(repo, name="fake-loop.sh"):
    """A live process whose COMMAND LINE names this workspace, so
    launcher._ps_names_the_loop vouches for it - i.e. a stand-in for a
    launchd/hand-started loop the launcher did not spawn.

    Why a script inside the workspace rather than `sh -c "sleep 60 # <path>"`:
    /bin/sh exec-optimizes a single simple command, so the comment (and with
    it the path) vanishes from the process's argv and ps shows a bare
    `sleep 60`."""
    script = repo.resolve() / ".autopilot" / "state" / name
    script.write_text("#!/bin/sh\nsleep 60\n")
    script.chmod(0o755)
    return subprocess.Popen(["/bin/sh", str(script)], start_new_session=True)


def _kill_stray(proc):
    if proc.poll() is None:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    proc.wait(timeout=5)


@pytest.fixture()
def loops(launcher, tmp_path, monkeypatch):
    """launcher module with a zero grace period (loop-stop's real 2s window is
    a courtesy to a live loop, not something to pay for in tests), an
    ORBIT_LOOP_CMD stand-in for engine/run.sh, and a teardown that never
    leaves a spawned slot behind."""
    launcher.LOOP_STOP_GRACE = 0.0
    envdir = tmp_path / "loopenv"
    envdir.mkdir()
    script = tmp_path / "fake-run.sh"
    # Records the slot env it was handed (so the spawn contract is assertable),
    # writes the same $STATE/workers/<slot>.pid record run.sh's
    # write_worker_pid writes (pid = the loop's own $$ - that is what makes a
    # loop visible to a launcher that didn't start it), says one line on stdout
    # (so the loop-<slot>.out redirection is assertable too), then sleeps until
    # it is signalled. Deliberately does NOT clean the pid file up on exit:
    # run.sh's trap does, but a loop that dies hard doesn't, and the launcher
    # has to be right in the harsher case.
    script.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$ORBIT_SLOT|$ORBIT_SLOTS|$ORBIT_LAUNCHD_LABEL|$1" > "$ORBIT_TEST_ENVDIR/slot-$ORBIT_SLOT"\n'
        'W="$1/.autopilot/state/workers"; mkdir -p "$W"\n'
        'printf \'{"pid": %s, "tid": "t-fake-%s", "started": "now"}\\n\' "$$" "$ORBIT_SLOT" > "$W/$ORBIT_SLOT.pid"\n'
        'echo "fake loop slot $ORBIT_SLOT up"\n'
        "sleep 60\n")
    script.chmod(0o755)
    monkeypatch.setenv("ORBIT_TEST_ENVDIR", str(envdir))
    monkeypatch.setenv("ORBIT_LOOP_CMD", f"/bin/sh {script}")
    yield launcher, envdir
    for path in list(launcher.LOOPS.keys()):
        for proc in list((launcher.LOOPS.get(path) or {}).values()):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        launcher.LOOPS.pop(path, None)


def test_loop_start_spawns_one_process_per_configured_slot(loops, tmp_path):
    launcher, envdir = loops
    repo = _workspace(tmp_path, "twoslot", concurrency=2)
    launcher.registry_add(str(repo))
    result = launcher.do_loop_start(str(repo))
    assert result["ok"] is True, result["msg"]
    assert result["slots"] == 2
    assert result["started"] == [0, 1]
    procs = launcher.LOOPS[str(repo.resolve())]
    assert sorted(procs) == [0, 1]
    assert all(p.poll() is None for p in procs.values())
    # each slot got its own identity, the shared slot count, this project's
    # service label, and the target path as argv[1]
    assert _wait_for(lambda: len(list(envdir.iterdir())) == 2), list(envdir.iterdir())
    for slot in (0, 1):
        slot_env, slots, label, target = (envdir / f"slot-{slot}").read_text().strip().split("|")
        assert slot_env == str(slot)
        assert slots == "2"
        assert label == "com.orbit.orbit-twoslot"
        assert target == str(repo.resolve())
    # stdout/stderr landed in the per-slot log
    logs = repo / ".autopilot" / "state" / "logs"
    assert _wait_for(lambda: (logs / "loop-0.out").exists() and (logs / "loop-1.out").exists())
    assert _wait_for(lambda: "fake loop slot 0 up" in (logs / "loop-0.out").read_text())


def test_loop_start_defaults_to_one_slot_without_configured_concurrency(loops, tmp_path):
    launcher, envdir = loops
    repo = _workspace(tmp_path, "oneslot")   # only a commented-out concurrency
    launcher.registry_add(str(repo))
    result = launcher.do_loop_start(str(repo))
    assert result["ok"] is True
    assert result["slots"] == 1
    assert launcher._concurrency(str(repo)) == 1
    assert sorted(launcher.LOOPS[str(repo.resolve())]) == [0]


def test_loop_start_disarms_the_stop_file(loops, tmp_path):
    launcher, envdir = loops
    repo = _workspace(tmp_path, "armed")
    stop = repo / ".autopilot" / "state" / "STOP"
    stop.write_text("stopped\n")
    launcher.registry_add(str(repo))
    assert launcher.do_loop_start(str(repo))["ok"] is True
    assert not stop.exists()   # a start with STOP still armed would just idle


def test_loop_start_is_idempotent_for_live_slots(loops, tmp_path):
    launcher, envdir = loops
    repo = _workspace(tmp_path, "again")
    launcher.registry_add(str(repo))
    launcher.do_loop_start(str(repo))
    first = launcher.LOOPS[str(repo.resolve())][0].pid
    second = launcher.do_loop_start(str(repo))
    assert second["started"] == []   # nothing new spawned
    assert launcher.LOOPS[str(repo.resolve())][0].pid == first


def test_loop_start_never_double_starts_a_slot_a_foreign_loop_owns(loops, tmp_path):
    # THE cross-restart case: after a launcher restart LOOPS is empty while
    # the loops it started keep running (the no-kill-on-exit ruling), so a
    # Start click must not put a second process on a slot that is already
    # working - two loops on one slot share a worktree, a pid file and (at
    # slot 0) the legacy marker names, and would stomp each other mid-cycle.
    launcher, envdir = loops
    repo = _workspace(tmp_path, "restarted", concurrency=2)
    launcher.registry_add(str(repo))
    workers = repo / ".autopilot" / "state" / "workers"
    workers.mkdir(parents=True)
    foreign = subprocess.Popen(["/bin/sh", "-c", "sleep 60"], start_new_session=True)
    try:
        # slot 1 is live under a pid this launcher does not track
        (workers / "1.pid").write_text(json.dumps({"pid": foreign.pid, "tid": "t-9", "started": "now"}))
        result = launcher.do_loop_start(str(repo))
        assert result["ok"] is True, result["msg"]
        assert result["started"] == [0]        # only the genuinely free slot
        assert result["skipped"] == [1]
        assert "1" in result["msg"] and "outside this launcher" in result["msg"]
        assert sorted(launcher.LOOPS[str(repo.resolve())]) == [0]
        assert _wait_for(lambda: [f.name for f in envdir.iterdir()] == ["slot-0"]), list(envdir.iterdir())

        # and with every slot accounted for, a second Start is a no-op
        again = launcher.do_loop_start(str(repo))
        assert again["ok"] is True
        assert again["started"] == []
        assert "already running" in again["msg"]
        assert sorted(launcher.LOOPS[str(repo.resolve())]) == [0]
    finally:
        os.killpg(os.getpgid(foreign.pid), signal.SIGKILL)
        foreign.wait(timeout=5)


def test_loop_start_rejects_unregistered_and_uninitialized(loops, tmp_path):
    launcher, envdir = loops
    ghost = tmp_path / "ghost"
    ghost.mkdir()
    assert launcher.do_loop_start(str(ghost))["ok"] is False
    bare = tmp_path / "bare"
    bare.mkdir()
    launcher.registry_add(str(bare))
    res = launcher.do_loop_start(str(bare))
    assert res["ok"] is False
    assert "autopilot" in res["msg"].lower()
    assert launcher.LOOPS == {}


def test_loop_stop_terms_the_slots_and_leaves_stop_armed(loops, tmp_path):
    launcher, envdir = loops
    repo = _workspace(tmp_path, "stopme", concurrency=2)
    launcher.registry_add(str(repo))
    launcher.do_loop_start(str(repo))
    procs = list(launcher.LOOPS[str(repo.resolve())].values())
    assert _wait_for(lambda: len(list(envdir.iterdir())) == 2)

    result = launcher.do_loop_stop(str(repo))
    assert result["ok"] is True
    assert len(result["signalled"]) == 2
    for proc in procs:
        assert _wait_for(lambda p=proc: p.poll() is not None), "slot survived SIGTERM"
    # the ledger ruling: STOP stays on disk so launchd's KeepAlive restart
    # comes back up idle instead of silently resuming
    assert (repo / ".autopilot" / "state" / "STOP").exists()
    assert launcher.LOOPS == {}


def test_loop_stop_without_a_running_loop_still_arms_stop(loops, tmp_path):
    launcher, envdir = loops
    repo = _workspace(tmp_path, "neverstarted")
    launcher.registry_add(str(repo))
    result = launcher.do_loop_stop(str(repo))
    assert result["ok"] is True
    assert result["signalled"] == []
    assert (repo / ".autopilot" / "state" / "STOP").exists()


def test_loop_stop_signals_untracked_loops_from_worker_pid_files(loops, tmp_path):
    # A loop started by launchd (or a previous launcher process) isn't in
    # LOOPS at all - its $STATE/workers/<slot>.pid is the only allowlist
    # loop-stop consults.
    launcher, envdir = loops
    repo = _workspace(tmp_path, "untracked")
    launcher.registry_add(str(repo))
    workers = repo / ".autopilot" / "state" / "workers"
    workers.mkdir(parents=True)
    # The command line names the workspace, so `ps` vouches for it (F9's
    # identity check - its own tests are below).
    stray = _stray_loop(repo)
    try:
        (workers / "0.pid").write_text(json.dumps({"pid": stray.pid, "tid": "t-1", "started": "now"}))
        result = launcher.do_loop_stop(str(repo))
        assert result["signalled"] == [stray.pid]
        assert _wait_for(lambda: stray.poll() is not None)
    finally:
        if stray.poll() is None:
            os.killpg(os.getpgid(stray.pid), signal.SIGKILL)
            stray.wait(timeout=5)


def test_loop_stop_only_blasts_a_process_group_the_loop_actually_leads(loops, tmp_path, monkeypatch):
    # A loop pid from a workers file is not ours; if it is its own group's
    # leader (our spawns, launchd, systemd) the group goes down with it so no
    # in-flight cycle is orphaned. If it is NOT, the group belongs to whatever
    # shell backgrounded it and must never be blasted - TERM the pid alone.
    launcher, envdir = loops
    repo = _workspace(tmp_path, "grouprules")
    launcher.registry_add(str(repo))
    workers = repo / ".autopilot" / "state" / "workers"
    workers.mkdir(parents=True)
    (workers / "0.pid").write_text(json.dumps({"pid": 4242, "tid": "t-1"}))
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: True)
    # 4242 is a made-up pid, so `ps` cannot vouch for it (F9's identity check
    # has its own tests below) - this test is about the leader guard only.
    monkeypatch.setattr(launcher, "_ps_names_the_loop", lambda pid, path: True)

    for pgid, expect_group_kill in [(4242, True), (1, False)]:
        killed, blasted = [], []
        monkeypatch.setattr(launcher.os, "getpgid", lambda pid, _g=pgid: _g)
        monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: killed.append((pid, sig)))
        monkeypatch.setattr(launcher.os, "killpg", lambda pg, sig: blasted.append((pg, sig)))
        result = launcher.do_loop_stop(str(repo))
        assert result["signalled"] == [4242]
        if expect_group_kill:
            assert blasted == [(4242, signal.SIGTERM)] and killed == []
        else:
            assert killed == [(4242, signal.SIGTERM)] and blasted == []


def test_stop_then_start_works_and_leaves_no_zombie_slot(loops, tmp_path):
    # The bug this pins (found by a live run, not by a unit test): a slot the
    # launcher TERMs is its own child, so unreaped it lingers as <defunct> -
    # and a zombie still answers kill(pid, 0). With its pid file still on disk
    # (a loop that died hard leaves one), the launcher would read the project
    # as running forever AND refuse to restart the slot, i.e. Stop would be a
    # one-way door.
    launcher, envdir = loops
    repo = _workspace(tmp_path, "cycleback")
    launcher.registry_add(str(repo))
    assert launcher.do_loop_start(str(repo))["started"] == [0]
    pidfile = repo / ".autopilot" / "state" / "workers" / "0.pid"
    assert _wait_for(lambda: pidfile.exists())

    launcher.do_loop_stop(str(repo))
    assert pidfile.exists()                        # nothing cleaned it up
    assert launcher.LOOPS == {}                    # reaped, not zombified
    assert launcher._live_workers(str(repo)) == []  # the dead pid is not "live"
    assert launcher._loop_running(str(repo)) is False

    # ...and Start works again, on the same slot
    again = launcher.do_loop_start(str(repo))
    assert again["started"] == [0], again["msg"]
    assert again["skipped"] == []
    assert launcher._loop_running(str(repo)) is True


def test_loop_stop_ignores_worker_files_it_cannot_correlate(loops, tmp_path):
    # A pid file is only trusted to name a live loop while its identity holds
    # up: it must name a task AND have been touched inside 3x cycle_timeout.
    # A leaked file (SIGKILLed loop) ages out, and once it does its pid may
    # well have been recycled onto an unrelated process - never TERM that.
    launcher, envdir = loops
    repo = _workspace(tmp_path, "stalefiles", concurrency=1)
    cfg = repo / ".autopilot" / "config.yaml"
    cfg.write_text(cfg.read_text() + "cycle_timeout_seconds: 60\n")
    launcher.registry_add(str(repo))
    workers = repo / ".autopilot" / "state" / "workers"
    workers.mkdir(parents=True)
    # Names the workspace so the `ps` identity check (F9) passes - what this
    # test is about is the tid + freshness correlation on top of it.
    stray = _stray_loop(repo)
    try:
        # old file (mtime way past 3x60s) + a file with no task id
        old = workers / "0.pid"
        old.write_text(json.dumps({"pid": stray.pid, "tid": "t-1", "started": "old"}))
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        untitled = workers / "1.pid"
        untitled.write_text(json.dumps({"pid": stray.pid, "tid": "", "started": "now"}))

        result = launcher.do_loop_stop(str(repo))
        assert result["ok"] is True
        assert result["signalled"] == []
        assert sorted(result["ignored"]) == ["0.pid", "1.pid"]
        assert "0.pid" in result["msg"] and "1.pid" in result["msg"]
        assert stray.poll() is None       # never signalled
        assert (repo / ".autopilot" / "state" / "STOP").exists()   # STOP still armed

        # freshen one of them and it becomes signallable again
        untitled.write_text(json.dumps({"pid": stray.pid, "tid": "t-2", "started": "now"}))
        result = launcher.do_loop_stop(str(repo))
        assert result["signalled"] == [stray.pid]
        assert result["ignored"] == ["0.pid"]
        assert _wait_for(lambda: stray.poll() is not None)
    finally:
        if stray.poll() is None:
            os.killpg(os.getpgid(stray.pid), signal.SIGKILL)
            stray.wait(timeout=5)


def test_loop_start_and_stop_serialize_their_stop_file_section(loops, tmp_path):
    # start clears STOP and stop writes it; both do that under the launcher's
    # lock, so the file can never end up cleared by a start that lost the race
    # with a stop (slots coming up already stopped, or worse).
    launcher, envdir = loops
    repo = _workspace(tmp_path, "stoprace")
    launcher.registry_add(str(repo))
    stop = repo / ".autopilot" / "state" / "STOP"
    barrier = threading.Barrier(3)
    errors = []

    def start():
        barrier.wait()
        try:
            launcher.do_loop_start(str(repo))
        except Exception as e:   # pragma: no cover - a raise here IS the failure
            errors.append(e)

    def halt():
        barrier.wait()
        try:
            launcher.do_loop_stop(str(repo))
        except Exception as e:   # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=start), threading.Thread(target=halt)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join(timeout=20)
    assert errors == []
    # The invariant that matters: the launcher never ends up with STOP armed
    # AND slots it believes are running (a start whose STOP-clear was undone
    # would leave exactly that - live slots that immediately idle, reported as
    # running). Either the stop won (STOP armed, nothing tracked) or the start
    # did (no STOP; whatever it spawned may still be tracked).
    tracked = launcher._tracked_loop_pids(str(repo))
    if stop.exists():
        assert stop.read_text().startswith("stopped by the launcher")
        assert tracked == [], "STOP is armed but the launcher still tracks live slots"


def test_loop_running_is_true_while_a_slot_lives_and_false_after_stop(loops, tmp_path):
    launcher, envdir = loops
    repo = _workspace(tmp_path, "liveness")
    launcher.registry_add(str(repo))
    assert launcher.do_loop_start(str(repo))["ok"] is True
    assert launcher._loop_running(str(repo)) is True
    assert launcher.launcher_state()["projects"][0]["loop_running"] is True
    assert launcher.launcher_state()["projects"][0]["loop_tracked"] == 1

    launcher.do_loop_stop(str(repo))
    assert _wait_for(lambda: launcher._loop_running(str(repo)) is False)
    state = launcher.launcher_state()["projects"][0]
    assert state["loop_running"] is False
    assert state["loop_tracked"] == 0


def test_loop_running_trusts_a_live_worker_pid_over_the_stop_file(loops, tmp_path):
    # A mid-cycle worker pid file is still a running loop even with STOP armed
    # (STOP stops new claims, not the process). A dead pid is not.
    launcher, envdir = loops
    repo = _workspace(tmp_path, "midcycle")
    state = repo / ".autopilot" / "state"
    (state / "workers").mkdir()
    (state / "STOP").write_text("stopped\n")
    (state / "workers" / "0.pid").write_text(json.dumps({"pid": os.getpid(), "tid": "t-1"}))
    assert launcher._loop_running(str(repo)) is True
    (state / "workers" / "0.pid").write_text(json.dumps({"pid": 999999999, "tid": "t-1"}))
    assert launcher._loop_running(str(repo)) is False


def test_loop_running_tolerates_a_torn_worker_pid_file(loops, tmp_path):
    launcher, envdir = loops
    repo = _workspace(tmp_path, "torn")
    state = repo / ".autopilot" / "state"
    (state / "workers").mkdir()
    (state / "workers" / "0.pid").write_text('{"pid": 1')   # half-written
    (state / "workers" / "loop-0.pid").write_text('{"pid": 1')
    (state / "STOP").write_text("stopped\n")
    assert launcher._loop_running(str(repo)) is False   # no crash, unowned


# ── F3: the lifetime loop pid file (idle-slot ownership) ─────────────────────

def test_loop_start_refuses_a_slot_an_idle_foreign_loop_owns(loops, tmp_path):
    # THE pause-then-Start double start. A paused loop holds no claim, so it
    # has no <slot>.pid at all - the launcher used to see a free slot and put
    # a SECOND process on the same worktree. loop-<slot>.pid is what makes an
    # idle-but-alive slot visible.
    launcher, envdir = loops
    repo = _workspace(tmp_path, "idleowned", concurrency=2)
    launcher.registry_add(str(repo))
    workers = repo / ".autopilot" / "state" / "workers"
    workers.mkdir(parents=True)
    idle = _stray_loop(repo)
    try:
        # slot 1 owned, idle: a loop pid file and deliberately NO worker file
        (workers / "loop-1.pid").write_text(json.dumps(
            {"pid": idle.pid, "slot": "1", "started": "now"}))
        assert list(workers.glob("[0-9]*.pid")) == []
        result = launcher.do_loop_start(str(repo))
        assert result["ok"] is True, result["msg"]
        assert result["started"] == [0]
        assert result["skipped"] == [1]
        assert launcher._loop_running(str(repo)) is True
    finally:
        os.killpg(os.getpgid(idle.pid), signal.SIGKILL)
        idle.wait(timeout=5)


def test_loop_start_proceeds_past_a_stale_dead_loop_pid_file(loops, tmp_path):
    # A hard-killed loop leaves its file behind. A dead pid owns nothing, so
    # Start must take the slot rather than refuse it forever.
    launcher, envdir = loops
    repo = _workspace(tmp_path, "staleloop")
    launcher.registry_add(str(repo))
    workers = repo / ".autopilot" / "state" / "workers"
    workers.mkdir(parents=True)
    (workers / "loop-0.pid").write_text(json.dumps({"pid": 999999999, "slot": "0"}))
    assert launcher._loop_running(str(repo)) is False
    result = launcher.do_loop_start(str(repo))
    assert result["started"] == [0], result["msg"]
    assert result["skipped"] == []


def test_loop_stop_reaches_an_idle_foreign_loop(loops, tmp_path):
    # The other half of F3: Stop can now find and TERM an idle foreign loop,
    # which used to be invisible here (no claim, so no worker pid file) and
    # left running with only STOP armed.
    launcher, envdir = loops
    repo = _workspace(tmp_path, "stopidle")
    launcher.registry_add(str(repo))
    workers = repo / ".autopilot" / "state" / "workers"
    workers.mkdir(parents=True)
    idle = _stray_loop(repo)
    try:
        (workers / "loop-0.pid").write_text(json.dumps(
            {"pid": idle.pid, "slot": "0", "started": "now"}))
        result = launcher.do_loop_stop(str(repo))
        assert result["signalled"] == [idle.pid], result["msg"]
        assert _wait_for(lambda: idle.poll() is not None)
    finally:
        if idle.poll() is None:
            os.killpg(os.getpgid(idle.pid), signal.SIGKILL)
            idle.wait(timeout=5)


def test_loop_stop_refuses_a_pid_ps_says_is_not_our_loop(loops, tmp_path):
    # F9's recycled-pid guard: the pid in the file is alive and fresh, but the
    # process it names has nothing to do with this workspace. Signalling it
    # would TERM an innocent process.
    launcher, envdir = loops
    repo = _workspace(tmp_path, "recycled")
    launcher.registry_add(str(repo))
    workers = repo / ".autopilot" / "state" / "workers"
    workers.mkdir(parents=True)
    innocent = subprocess.Popen(["/bin/sh", "-c", "sleep 60"], start_new_session=True)
    try:
        (workers / "loop-0.pid").write_text(json.dumps({"pid": innocent.pid, "slot": "0"}))
        (workers / "1.pid").write_text(json.dumps(
            {"pid": innocent.pid, "tid": "t-1", "started": "now"}))
        result = launcher.do_loop_stop(str(repo))
        assert result["signalled"] == []
        # Both files are named, not just the first: an operator chasing a loop
        # that will not die needs to see every file that was refused.
        assert sorted(result["ignored"]) == ["1.pid", "loop-0.pid"]
        assert "not a loop of this workspace" in result["msg"]
        assert innocent.poll() is None
        assert (repo / ".autopilot" / "state" / "STOP").exists()
    finally:
        os.killpg(os.getpgid(innocent.pid), signal.SIGKILL)
        innocent.wait(timeout=5)


def test_loop_stop_also_kills_the_cycle_the_loop_had_in_flight(loops, tmp_path):
    """The cycle runs in its OWN session now (run.sh's session wrapper, so that
    aborting one task cannot kill its loop), so killing the loop no longer
    takes the cycle with it. Stop has to signal it explicitly or it leaves a
    claude cycle burning tokens against a worktree nobody owns."""
    launcher, envdir = loops
    repo = _workspace(tmp_path, "stopcycle")
    launcher.registry_add(str(repo))
    workers = repo / ".autopilot" / "state" / "workers"
    workers.mkdir(parents=True)
    loop = _stray_loop(repo, name="fake-loop.sh")
    cycle = _stray_loop(repo, name="fake-cycle.sh")     # its own session leader
    try:
        (workers / "0.pid").write_text(json.dumps(
            {"pid": loop.pid, "tid": "t-1", "started": "now",
             "cycle_pid": cycle.pid, "cycle_pgid": cycle.pid}))
        result = launcher.do_loop_stop(str(repo))
        assert result["signalled"] == [loop.pid], result["msg"]
        assert result["cycles_signalled"] == [cycle.pid], result["msg"]
        assert "in-flight cycle" in result["msg"]
        assert _wait_for(lambda: loop.poll() is not None)
        assert _wait_for(lambda: cycle.poll() is not None)
    finally:
        _kill_stray(loop)
        _kill_stray(cycle)


def test_loop_stop_leaves_a_cycle_alone_when_no_group_was_recorded(loops, tmp_path, monkeypatch):
    """No cycle_pgid means run.sh could not confirm the cycle leads its own
    group, so killpg would blast the LOOP's group - the very thing F2 fixed. A
    plain TERM on the cycle pid is all that is safe."""
    launcher, envdir = loops
    repo = _workspace(tmp_path, "nogroupcycle")
    launcher.registry_add(str(repo))
    workers = repo / ".autopilot" / "state" / "workers"
    workers.mkdir(parents=True)
    (workers / "0.pid").write_text(json.dumps(
        {"pid": 4242, "tid": "t-1", "started": "now", "cycle_pid": 777001}))
    signals = []
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(launcher, "_ps_names_the_loop", lambda pid, path: True)
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: signals.append(("kill", pid, sig)))
    monkeypatch.setattr(launcher.os, "killpg", lambda pg, sig: signals.append(("killpg", pg, sig)))
    monkeypatch.setattr(launcher.os, "getpgid", lambda pid: 1)   # the loop leads no group
    result = launcher.do_loop_stop(str(repo))
    assert ("kill", 777001, signal.SIGTERM) in signals    # the cycle, by pid
    assert ("killpg", 777001, signal.SIGTERM) not in signals
    assert result["cycles_signalled"] == [777001]


def test_ps_names_the_loop_accepts_run_sh_and_the_target_path(loops, tmp_path):
    launcher, envdir = loops
    repo = _workspace(tmp_path, "psprobe")
    named = _stray_loop(repo)
    other = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], start_new_session=True)
    try:
        assert launcher._ps_names_the_loop(named.pid, str(repo)) is True
        assert launcher._ps_names_the_loop(other.pid, str(repo)) is False
        assert launcher._ps_names_the_loop(999999999, str(repo)) is False   # fails closed
    finally:
        for p in (named, other):
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            p.wait(timeout=5)


def test_workers_dir_loop_files_are_not_counted_as_cycle_workers(loops, tmp_path):
    # loop-<slot>.pid lives in the same directory as the per-cycle <slot>.pid
    # files. Reading one as a worker would put a permanent phantom "working"
    # row on the dashboard's strip and make the launcher's worker signal true
    # forever.
    launcher, envdir = loops
    repo = _workspace(tmp_path, "stems")
    workers = repo / ".autopilot" / "state" / "workers"
    workers.mkdir(parents=True)
    (workers / "loop-0.pid").write_text(json.dumps({"pid": os.getpid(), "slot": "0"}))
    assert launcher._live_workers(str(repo)) == []
    assert [r["slot"] for r in launcher._live_loops(str(repo))] == [0]


def test_loop_routes_over_http_require_the_token(launcher, tmp_path):
    repo = _workspace(tmp_path, "httpstop")
    launcher.registry_add(str(repo))
    lch = _start_launcher(launcher)
    try:
        body = json.dumps({"path": str(repo)}).encode()
        for route in ("/_launcher/loop-start", "/_launcher/loop-stop"):
            req = urllib.request.Request(f"http://127.0.0.1:{lch.port}{route}", data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "X-CC-Token": "wrong-token"})
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(req, timeout=5)
            assert ei.value.code == 403
        assert not (repo / ".autopilot" / "state" / "STOP").exists()
        # with the real token the route runs (stop is the side-effect-cheap one)
        req = urllib.request.Request(f"http://127.0.0.1:{lch.port}/_launcher/loop-stop", data=body,
                                     method="POST", headers={"Content-Type": "application/json",
                                                             "X-CC-Token": launcher._TOKEN})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        assert data["ok"] is True
        assert (repo / ".autopilot" / "state" / "STOP").exists()
    finally:
        lch.close()


# ── /_launcher/create (new workspace) ────────────────────────────────────────
def test_create_scaffolds_git_init_plus_autopilot_and_registers(launcher, tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(launcher, "spawn_child", lambda p: (spawned.append(p), (True, "opened"))[1])
    target = tmp_path / "brand-new"
    result = launcher.do_create(str(target), name="Brand New", product=False)
    assert result["ok"] is True, result["msg"]
    assert (target / ".git").exists()
    assert (target / ".autopilot" / "config.yaml").exists()
    assert (target / ".autopilot" / "backlog.yaml").exists()
    assert (target / ".claude" / "commands").is_dir()
    # registered under the operator's name, with the dashboard child brought up
    assert result["project"]["path"] == str(target.resolve())
    assert result["project"]["name"] == "Brand New"
    assert launcher.registry_get(str(target))["name"] == "Brand New"
    assert result["opened"] is True
    assert spawned == [str(target.resolve())]


def test_create_product_workspace_passes_the_product_flag(launcher, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "spawn_child", lambda p: (True, "opened"))
    target = tmp_path / "pm-space"
    result = launcher.do_create(str(target), name="", product=True)
    assert result["ok"] is True, result["msg"]
    assert result["project"]["name"] == "pm-space"   # defaults to the basename
    # --product-only scaffolding (init.py): the PM directories and the
    # grounding track it copies out of the engine
    assert (target / "insights").is_dir()
    assert (target / "surveys").is_dir()
    assert (target / ".autopilot" / "tracks" / "prd-grounding.md").exists()
    assert "insight_lint" in (target / ".autopilot" / "config.yaml").read_text()


def test_create_refuses_an_existing_non_empty_directory(launcher, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "spawn_child", lambda p: (True, "opened"))
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "README.md").write_text("someone's repo\n")
    result = launcher.do_create(str(target), name="", product=False)
    assert result["ok"] is False
    assert "not empty" in result["msg"]
    assert not (target / ".autopilot").exists()
    assert launcher.registry_list() == []


def test_create_accepts_an_existing_empty_directory(launcher, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "spawn_child", lambda p: (True, "opened"))
    target = tmp_path / "empty-but-there"
    target.mkdir()
    result = launcher.do_create(str(target), name="", product=False)
    assert result["ok"] is True, result["msg"]
    assert (target / ".autopilot" / "config.yaml").exists()


def test_create_refuses_a_broken_symlink_target(launcher, tmp_path, monkeypatch):
    # exists() is False for a dangling symlink, so without an explicit check
    # this reached mkdir and died with a bare FileExistsError.
    monkeypatch.setattr(launcher, "spawn_child", lambda p: (True, "opened"))
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "nothing-here")
    result = launcher.do_create(str(link), name="", product=False)
    assert result["ok"] is False
    assert "symlink" in result["msg"]
    assert launcher.registry_list() == []


def test_create_non_empty_refusal_names_the_recovery_path(launcher, tmp_path, monkeypatch):
    # create is not transactional: a half-built directory (git init done, init
    # died) must not leave the operator with a dead-end refusal.
    monkeypatch.setattr(launcher, "spawn_child", lambda p: (True, "opened"))
    half = tmp_path / "half-built"
    half.mkdir()
    (half / ".git").mkdir()
    msg = launcher.do_create(str(half), name="", product=False)["msg"]
    assert "Import" in msg
    assert "delete it and create again" in msg


def test_create_route_rejects_a_wrong_token(launcher, tmp_path):
    lch = _start_launcher(launcher)
    try:
        target = tmp_path / "never-made"
        body = json.dumps({"path": str(target), "name": "nope", "product": False}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{lch.port}/_launcher/create", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-CC-Token": "wrong-token"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 403
        assert not target.exists()          # nothing scaffolded
        assert launcher.registry_list() == []
    finally:
        lch.close()


def test_create_rejects_a_relative_or_missing_path(launcher):
    assert launcher.do_create("", name="", product=False)["ok"] is False
    res = launcher.do_create("relative/workspace", name="", product=False)
    assert res["ok"] is False
    assert "absolute" in res["msg"]


def test_create_survives_a_failing_dashboard_spawn(launcher, tmp_path, monkeypatch):
    # The scaffold is on disk and registered - a dashboard that didn't come up
    # is a reopen away, not a failed create.
    monkeypatch.setattr(launcher, "spawn_child", lambda p: (False, "dashboard did not respond within 5s"))
    target = tmp_path / "no-dash"
    result = launcher.do_create(str(target), name="", product=False)
    assert result["ok"] is True
    assert result["opened"] is False
    assert "did not respond" in result["msg"]
    assert launcher.registry_get(str(target)) is not None


def test_create_route_over_http(launcher, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "spawn_child", lambda p: (True, "opened"))
    lch = _start_launcher(launcher)
    try:
        target = tmp_path / "over-http"
        body = json.dumps({"path": str(target), "name": "Over HTTP", "product": False}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{lch.port}/_launcher/create", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-CC-Token": launcher._TOKEN})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        assert data["ok"] is True, data["msg"]
        assert data["project"]["name"] == "Over HTTP"
        assert (target / ".autopilot" / "config.yaml").exists()
    finally:
        lch.close()


def test_both_shells_wire_the_loop_routes(launcher):
    # Loop control has to be reachable from wherever the operator is: the
    # picker's tab strip (launcher_shell) AND the dashboard's project switcher
    # (cc_shell), which is the only launcher chrome visible once a project is
    # open and the launcher is proxying / to that child.
    picker = (ENGINE / "launcher_shell.html").read_text()
    dash = (ENGINE / "cc_shell.html").read_text()
    for shell, name in [(picker, "launcher_shell.html"), (dash, "cc_shell.html")]:
        assert "/_launcher/loop-start" in shell, f"{name} has no way to start a loop"
        assert "/_launcher/loop-stop" in shell, f"{name} has no way to stop a loop"
        assert "loop_running" in shell, f"{name} shows no running state"
    assert "/_launcher/create" in picker


def test_concurrency_reads_the_config_and_defaults_to_one(launcher, tmp_path):
    repo = _workspace(tmp_path, "cfg", concurrency=3)
    assert launcher._concurrency(str(repo)) == 3
    # a commented-out key is not a value
    commented = _workspace(tmp_path, "commented")
    assert launcher._concurrency(str(commented)) == 1
    # garbage and absent configs both fall back to today's single loop
    (repo / ".autopilot" / "config.yaml").write_text("concurrency: banana\n")
    assert launcher._concurrency(str(repo)) == 1
    assert launcher._concurrency(str(tmp_path / "nope")) == 1
    # a real number out of range is clamped, not dropped
    (repo / ".autopilot" / "config.yaml").write_text("concurrency: 99\n")
    assert launcher._concurrency(str(repo)) == 32
    (repo / ".autopilot" / "config.yaml").write_text("concurrency: 0\n")
    assert launcher._concurrency(str(repo)) == 1
