# qa_env.py contract tests — the deterministic (no-agent) twin of the QA
# agent's launch discipline (skills/orbit-qa-check.md step 1): bring
# qa.services up in order, ready-poll, and guarantee teardown on every
# failure path. Task 20 (`orbit teach`) launches services CLI-side via this
# module with no agent present, so these tests exercise real subprocesses
# (tiny stdlib http.server instances on free ports) rather than mocking
# Popen — the leak-proofing this module exists for is exactly what mocking
# would hide.
#
# Run: python3.11 -m pytest tests/test_qa_env.py -v   (from orbit/)
import importlib.util
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
sys.path.insert(0, str(ENGINE))

_spec = importlib.util.spec_from_file_location("qa_env", ENGINE / "qa_env.py")
qa_env = importlib.util.module_from_spec(_spec)
sys.modules["qa_env"] = qa_env   # dataclass's typing resolution needs the module registered
_spec.loader.exec_module(qa_env)


def free_port() -> int:
    """Grab a free port and immediately release it — good enough for a test
    suite (no other process on this box is racing us for it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_server_cmd(port: int) -> str:
    return f"{sys.executable} -m http.server {port} --bind 127.0.0.1"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_until(fn, timeout=3.0, interval=0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return fn()


# ===========================================================================
# up() brings a real service up; down() kills it.
# ===========================================================================

def test_up_starts_service_ready_poll_succeeds_and_down_kills_it(tmp_path):
    port = free_port()
    qa_cfg = {"services": [
        {"name": "web", "launch": http_server_cmd(port), "cwd": ".",
         "ready": f"http://127.0.0.1:{port}/"},
    ]}
    log_dir = tmp_path / "logs"

    handles = qa_env.up(tmp_path, qa_cfg, log_dir)
    try:
        assert [h.name for h in handles] == ["web"]
        h = handles[0]
        assert h.proc.poll() is None            # alive
        assert alive(h.proc.pid)
        assert h.log_path == log_dir / "web.log"
        assert h.log_path.exists()               # log file captured
        pid = h.proc.pid
    finally:
        qa_env.down(handles)

    assert wait_until(lambda: not alive(pid)), "service still alive after down()"


def test_down_is_reverse_order():
    order = []

    class FakeProc:
        def __init__(self, name):
            self.name = name
            self.pid = 999999   # never a real pid — poll() short-circuits before any signal
            self._polled = False

        def poll(self):
            order.append(self.name)
            return 0   # already exited — _kill_one returns immediately, no signal sent

    handles = [qa_env.ServiceHandle(name=n, proc=FakeProc(n), log_path=Path("/dev/null"))
               for n in ("a", "b", "c")]
    qa_env.down(handles)
    assert order == ["c", "b", "a"]


def test_teardown_kills_forked_grandchild_not_just_launch_pid(tmp_path, monkeypatch):
    """Mechanism proof for the process-group teardown (start_new_session +
    os.killpg in _send): `python -m http.server` is exec-replaced by
    /bin/sh, so its tracked pid IS the real server (pid == pgid) — every
    other test in this file would still pass even with start_new_session/
    killpg deleted, signalling only proc.pid. Real `launch` strings (e.g.
    `npm run dev`) routinely fork a grandchild the shell never exec-replaces
    into. `bash -c 'sleep 30 & echo $! > pidfile; wait'` reproduces that
    shape: bash (our tracked pid) backgrounds `sleep 30` as a genuinely
    separate, un-exec'd process and captures its pid to a file. If down()
    only signalled the tracked pid, the sleep grandchild would keep running
    for its full 30s after teardown — this asserts it doesn't.

    RED-tested manually: temporarily patching _send to `proc.send_signal(sig)`
    (bypassing os.killpg entirely, i.e. simulating start_new_session/killpg
    having been deleted) makes this exact test fail (grandchild still alive
    after down()) while every other test in this file keeps passing — see
    the task-19-report.md fix-pass entry for the transcript.
    """
    monkeypatch.setattr(qa_env, "NO_READY_SLEEP", 0.2)   # keep the test itself fast
    pidfile = tmp_path / "grandchild.pid"
    qa_cfg = {"services": [
        # Single-quote the inner bash script: this whole `launch` string
        # itself goes through ONE shell parse already (up()'s own
        # Popen(shell=True) invokes /bin/sh -c launch) — double-quoting it
        # here would let $! expand in the OUTER shell (which has no
        # background jobs, so it'd expand to "") before bash ever saw it.
        {"name": "forker",
         "launch": f"bash -c 'sleep 30 & echo $! > {pidfile}; wait'"},
    ]}
    log_dir = tmp_path / "logs"

    handles = qa_env.up(tmp_path, qa_cfg, log_dir)
    try:
        pid_written = lambda: pidfile.exists() and pidfile.read_text().strip() != ""
        assert wait_until(pid_written, timeout=2), "grandchild never wrote its pid"
        grandchild_pid = int(pidfile.read_text().strip())
        assert grandchild_pid != handles[0].proc.pid   # genuinely a DIFFERENT process
        assert alive(grandchild_pid), "grandchild not even running yet"
    finally:
        qa_env.down(handles)

    assert wait_until(lambda: not alive(grandchild_pid)), (
        "grandchild leaked past down() — process-group teardown isn't working")


# ===========================================================================
# Ready timeout tears down EVERYTHING already started, naming the culprit.
# ===========================================================================

def test_ready_timeout_tears_down_earlier_services(tmp_path, monkeypatch):
    # Shrink the poll interval so a ~1s ready_timeout_seconds override still
    # gets at least one real poll without the test taking the full 2s/attempt.
    monkeypatch.setattr(qa_env, "READY_POLL_INTERVAL", 0.2)

    ok_port = free_port()
    dead_port = free_port()   # freed immediately after; nothing will ever listen here
    qa_cfg = {
        "ready_timeout_seconds": 1,
        "services": [
            {"name": "ok", "launch": http_server_cmd(ok_port),
             "ready": f"http://127.0.0.1:{ok_port}/"},
            {"name": "dead", "launch": "true",
             "ready": f"http://127.0.0.1:{dead_port}/"},
        ],
    }
    log_dir = tmp_path / "logs"

    with pytest.raises(qa_env.QaEnvError, match="dead"):
        qa_env.up(tmp_path, qa_cfg, log_dir)

    # The earlier "ok" service must have been torn down too — its port
    # should no longer answer.
    def ok_port_dead():
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{ok_port}/", timeout=1)
            return False
        except (urllib.error.URLError, OSError):
            return True

    assert wait_until(ok_port_dead), "earlier-started service was not torn down"


# ===========================================================================
# No `ready` URL -> fixed settle sleep, no ready-poll.
# ===========================================================================

def test_no_ready_url_uses_fixed_sleep(tmp_path, monkeypatch):
    monkeypatch.setattr(qa_env, "NO_READY_SLEEP", 0.2)   # keep the test itself fast
    qa_cfg = {"services": [{"name": "solo", "launch": "true"}]}
    log_dir = tmp_path / "logs"

    t0 = time.monotonic()
    handles = qa_env.up(tmp_path, qa_cfg, log_dir)
    elapsed = time.monotonic() - t0
    try:
        assert elapsed >= 0.2
        assert [h.name for h in handles] == ["solo"]
    finally:
        qa_env.down(handles)


# ===========================================================================
# Defensive shape validation.
# ===========================================================================

def test_missing_launch_raises(tmp_path):
    qa_cfg = {"services": [{"name": "no-launch"}]}
    with pytest.raises(qa_env.QaEnvError, match="launch"):
        qa_env.up(tmp_path, qa_cfg, tmp_path / "logs")


def test_non_string_cwd_raises(tmp_path):
    qa_cfg = {"services": [{"name": "bad-cwd", "launch": "true", "cwd": 42}]}
    with pytest.raises(qa_env.QaEnvError, match="cwd"):
        qa_env.up(tmp_path, qa_cfg, tmp_path / "logs")


def test_non_dict_service_entry_raises(tmp_path):
    qa_cfg = {"services": ["not-a-dict"]}
    with pytest.raises(qa_env.QaEnvError):
        qa_env.up(tmp_path, qa_cfg, tmp_path / "logs")


def test_services_not_a_list_raises(tmp_path):
    qa_cfg = {"services": "nope"}
    with pytest.raises(qa_env.QaEnvError, match="list"):
        qa_env.up(tmp_path, qa_cfg, tmp_path / "logs")


def test_no_services_is_a_noop(tmp_path):
    assert qa_env.up(tmp_path, {}, tmp_path / "logs") == []


def test_non_numeric_ready_timeout_raises(tmp_path):
    qa_cfg = {"ready_timeout_seconds": "soon",
              "services": [{"name": "x", "launch": "true"}]}
    with pytest.raises(qa_env.QaEnvError, match="ready_timeout_seconds"):
        qa_env.up(tmp_path, qa_cfg, tmp_path / "logs")


def test_non_dict_env_raises(tmp_path):
    qa_cfg = {"env": ["not", "a", "dict"],
              "services": [{"name": "x", "launch": "true"}]}
    with pytest.raises(qa_env.QaEnvError, match="qa.env"):
        qa_env.up(tmp_path, qa_cfg, tmp_path / "logs")


# ===========================================================================
# up_ctx() tears down on exception inside the with-block.
# ===========================================================================

def test_up_ctx_tears_down_on_exception(tmp_path):
    port = free_port()
    qa_cfg = {"services": [
        {"name": "svc", "launch": http_server_cmd(port),
         "ready": f"http://127.0.0.1:{port}/"},
    ]}
    log_dir = tmp_path / "logs"
    pid = None

    with pytest.raises(RuntimeError, match="boom"):
        with qa_env.up_ctx(tmp_path, qa_cfg, log_dir) as handles:
            pid = handles[0].proc.pid
            assert handles[0].proc.poll() is None
            raise RuntimeError("boom")

    assert wait_until(lambda: not alive(pid)), "service leaked past up_ctx exception"


def test_up_ctx_tears_down_on_clean_exit(tmp_path):
    port = free_port()
    qa_cfg = {"services": [
        {"name": "svc", "launch": http_server_cmd(port),
         "ready": f"http://127.0.0.1:{port}/"},
    ]}
    log_dir = tmp_path / "logs"

    with qa_env.up_ctx(tmp_path, qa_cfg, log_dir) as handles:
        pid = handles[0].proc.pid
        assert alive(pid)

    assert wait_until(lambda: not alive(pid)), "service leaked past clean up_ctx exit"


# ===========================================================================
# restart_one() — per-service restart for a long-lived services session
# (command_center.py's persistent "Dev session"), as opposed to up()/down()'s
# whole-set bring-up/teardown.
# ===========================================================================

def test_restart_one_replaces_named_service_leaves_other_untouched(tmp_path):
    port_a = free_port()
    port_b = free_port()
    qa_cfg = {"services": [
        {"name": "web", "launch": http_server_cmd(port_a), "ready": f"http://127.0.0.1:{port_a}/"},
        {"name": "api", "launch": http_server_cmd(port_b), "ready": f"http://127.0.0.1:{port_b}/"},
    ]}
    log_dir = tmp_path / "logs"
    handles = qa_env.up(tmp_path, qa_cfg, log_dir)
    try:
        assert [h.name for h in handles] == ["web", "api"]
        web_pid_before = handles[0].proc.pid
        api_pid_before = handles[1].proc.pid
        assert alive(web_pid_before) and alive(api_pid_before)

        new_handles = qa_env.restart_one(tmp_path, qa_cfg, "web", handles, log_dir)

        # A brand new list, same length, same order, only "web" replaced.
        assert new_handles is not handles
        assert [h.name for h in new_handles] == ["web", "api"]
        web = new_handles[0]
        api = new_handles[1]

        # web's process changed (killed + fresh spawn)...
        assert web.proc.pid != web_pid_before
        assert web.proc.poll() is None
        assert wait_until(lambda: not alive(web_pid_before)), "old 'web' process not killed"
        # ...web is genuinely ready again on the same port.
        with urllib.request.urlopen(f"http://127.0.0.1:{port_a}/", timeout=1) as resp:
            assert resp.status == 200

        # ...while api's handle (and process) is the EXACT same object/pid —
        # restart_one() must never touch a handle it wasn't asked to restart.
        assert api is handles[1]
        assert api.proc.pid == api_pid_before
        assert alive(api_pid_before)

        handles = new_handles
    finally:
        qa_env.down(handles)


def test_restart_one_unnamed_service_fallback_name_uses_current_position_not_stale_handles_index(tmp_path):
    # Regression (review fix): restart_one() must derive an unnamed
    # service's fallback name (f"service{i}") from ITS OWN position in the
    # CURRENT qa.services list (re-read fresh every call), never from the
    # position of the (possibly stale, built-at-Start) handles-list entry
    # it's replacing — those two indices can disagree if qa.services was
    # edited while running. Constructed to isolate the two: at Start,
    # position 0 is EXPLICITLY named "service1" (legal, if perverse — it
    # deliberately collides with the fallback pattern for a DIFFERENT
    # index); by restart time that entry is gone from config and a
    # genuinely unnamed service now legitimately sits at position 1 (whose
    # own correct fallback name is "service1" too).
    port_keepalive = free_port()
    port_old = free_port()
    port_new = free_port()

    started_cfg = {"services": [
        {"name": "service1", "launch": http_server_cmd(port_old), "ready": f"http://127.0.0.1:{port_old}/"},
        {"name": "keepalive", "launch": http_server_cmd(port_keepalive), "ready": f"http://127.0.0.1:{port_keepalive}/"},
    ]}
    log_dir = tmp_path / "logs"
    handles = qa_env.up(tmp_path, started_cfg, log_dir)
    try:
        assert [h.name for h in handles] == ["service1", "keepalive"]

        # Config edited while running: the old explicit "service1" entry is
        # gone; a genuinely unnamed service now legitimately occupies
        # position 1.
        edited_cfg = {"services": [
            {"name": "keepalive", "launch": http_server_cmd(port_keepalive),
             "ready": f"http://127.0.0.1:{port_keepalive}/"},
            {"launch": http_server_cmd(port_new), "ready": f"http://127.0.0.1:{port_new}/"},
        ]}

        new_handles = qa_env.restart_one(tmp_path, edited_cfg, "service1", handles, log_dir)

        # The freshly spawned handle's name must match ITS current position
        # in edited_cfg (index 1 -> "service1"), not the stale handles-list
        # index of the entry it's replacing (index 0 -> would wrongly come
        # out "service0").
        assert [h.name for h in new_handles] == ["service1", "keepalive"]
        restarted = new_handles[0]
        assert restarted.name == "service1"
        # ...and it's genuinely the NEW (unnamed) entry's launch command,
        # not the old, now-removed "service1" entry's — proving `svc`
        # itself was resolved to the right config entry too.
        with urllib.request.urlopen(f"http://127.0.0.1:{port_new}/", timeout=1) as resp:
            assert resp.status == 200

        handles = new_handles
    finally:
        qa_env.down(handles)


def test_restart_one_unknown_name_raises(tmp_path):
    port = free_port()
    qa_cfg = {"services": [
        {"name": "web", "launch": http_server_cmd(port), "ready": f"http://127.0.0.1:{port}/"},
    ]}
    log_dir = tmp_path / "logs"
    handles = qa_env.up(tmp_path, qa_cfg, log_dir)
    try:
        with pytest.raises(qa_env.QaEnvError, match="no-such-service"):
            qa_env.restart_one(tmp_path, qa_cfg, "no-such-service", handles, log_dir)
        # Untouched: still the same, still alive.
        assert handles[0].proc.poll() is None
    finally:
        qa_env.down(handles)


def test_restart_one_service_removed_from_config_raises(tmp_path):
    port = free_port()
    qa_cfg = {"services": [
        {"name": "web", "launch": http_server_cmd(port), "ready": f"http://127.0.0.1:{port}/"},
    ]}
    log_dir = tmp_path / "logs"
    handles = qa_env.up(tmp_path, qa_cfg, log_dir)
    try:
        # The handle exists (it's running), but the config no longer
        # mentions it by that name — restart_one must refuse, not guess.
        changed_cfg = {"services": [
            {"name": "other", "launch": "true"},
        ]}
        with pytest.raises(qa_env.QaEnvError, match="web"):
            qa_env.restart_one(tmp_path, changed_cfg, "web", handles, log_dir)
        assert handles[0].proc.poll() is None   # untouched
    finally:
        qa_env.down(handles)


def test_restart_one_ready_timeout_raises_and_kills_the_fresh_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(qa_env, "READY_POLL_INTERVAL", 0.2)
    port = free_port()
    dead_port = free_port()   # freed immediately; nothing will ever listen here
    qa_cfg = {
        "ready_timeout_seconds": 1,
        "services": [
            {"name": "flaky", "launch": http_server_cmd(port), "ready": f"http://127.0.0.1:{port}/"},
        ],
    }
    log_dir = tmp_path / "logs"
    handles = qa_env.up(tmp_path, qa_cfg, log_dir)
    try:
        # Point the SAME named service at a launch command that will never
        # answer its ready URL — restart_one must raise, having already
        # killed the fresh attempt it just spawned.
        broken_cfg = {
            "ready_timeout_seconds": 1,
            "services": [
                {"name": "flaky", "launch": "true", "ready": f"http://127.0.0.1:{dead_port}/"},
            ],
        }
        with pytest.raises(qa_env.QaEnvError, match="flaky"):
            qa_env.restart_one(tmp_path, broken_cfg, "flaky", handles, log_dir)
    finally:
        qa_env.down(handles)
