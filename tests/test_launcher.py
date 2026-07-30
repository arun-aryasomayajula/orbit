# launcher.py — standalone multi-project front door: registry CRUD, import
# validation, non-interactive init scaffolding, and the proxy's security gate
# (Host/Origin/token checked BEFORE any request ever reaches a child).
#
# Run: python3.11 -m pytest tests/test_launcher.py -v   (from orbit/)
import http.server
import importlib.util
import json
import subprocess
import sys
import threading
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


def test_launcher_state_loop_running_reflects_stop_file(launcher, tmp_path):
    repo = tmp_path / "looprepo"
    repo.mkdir()
    ap_state = repo / ".autopilot" / "state"
    ap_state.mkdir(parents=True)
    launcher.registry_add(str(repo))
    assert launcher.launcher_state()["projects"][0]["loop_running"] is True
    (ap_state / "STOP").write_text("stopped\n")
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
