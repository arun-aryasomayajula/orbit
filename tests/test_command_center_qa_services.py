# command_center.py's persistent QA "Dev session" — bring `qa.services` up
# ONCE and keep them warm, so `orbit qa --flow`/`orbit teach` stop rebooting
# the frontend/backend on every single run (see engine/qa_env.py's
# restart_one and skills/orbit-qa-flow.md's `$ORBIT_QA_ATTACH` clause):
#   POST /qa-services-start   -> validates qa.services is configured,
#                                backgrounds qa_env.up(), returns at once.
#   POST /qa-services-stop    -> qa_env.down()s the stored handles, resets.
#   POST /qa-services-restart -> {service} restarts ONE via qa_env.restart_one
#                                (updating the stored handles); no `service`
#                                restarts everything (down then up).
#   build_state()'s `qa_services` key drives the dashboard panel and is what
#   do_qa_action's run-flow / _qa_teach_bring_up's attach wiring both read.
#
# Hermetic: qa_env.up/down/restart_one are monkeypatched (no real services;
# see tests/test_qa_env.py for the real-subprocess coverage of qa_env itself).
#
# Run: python3.11 -m pytest tests/test_command_center_qa_services.py -v   (from orbit/)
import importlib.util
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, ENGINE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


QA_CFG = textwrap.dedent("""\
    qa:
      base_url: "http://example.test/app"
      services:
        - name: web
          launch: "true"
          ready: "http://127.0.0.1:9999/never-answers"
        - name: api
          launch: "true"
    """)

QA_CFG_NO_SERVICES = textwrap.dedent("""\
    qa:
      base_url: "http://example.test/app"
    """)

SPEC_CONTENT = (
    "import { test } from '@playwright/test';\n"
    "test('recorded flow', async ({ page }) => {\n"
    "  await page.goto('http://example.test/app');\n"
    "});\n"
)


def _fake_codegen_cmd(tmp_path, content=SPEC_CONTENT, rc=0, write=True, sleep=0, name="codegen"):
    """Same substitution mechanism (and env-not-argv contract) as
    tests/test_command_center_teach.py's own fake codegen: reads the output
    path off ORBIT_CODEGEN_OUTPUT and writes `content` there."""
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import os, sys, time\n"
        f"time.sleep({sleep})\n"
        "out = os.environ.get('ORBIT_CODEGEN_OUTPUT')\n"
        f"if out and {write!r}:\n"
        f"    open(out, 'w').write({content!r})\n"
        f"sys.exit({rc})\n"
    )
    return f"{sys.executable} {script}"


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "backlog.yaml").write_text("tasks:\n")
    (ap / "config.yaml").write_text(QA_CFG)
    monkeypatch.setenv("AP_HOME", str(ap))
    monkeypatch.setenv("AP_STATE", str(ap / "state"))
    monkeypatch.delenv("ORBIT_CODEGEN_CMD", raising=False)

    m = _mod("command_center")

    # Hermetic: no real qa.services ever launched. Spies so tests can assert
    # exactly what was called, with what arguments, and how many times.
    m._svc_up_calls = []
    m._svc_down_calls = []
    m._svc_restart_calls = []
    m._up_delay = 0.0
    m._restart_delay = 0.0

    def fake_up(target, qa_cfg, log_dir):
        m._svc_up_calls.append((target, qa_cfg, log_dir))
        if m._up_delay:
            time.sleep(m._up_delay)
        return ["handle-web", "handle-api"]

    def fake_down(handles):
        m._svc_down_calls.append(handles)

    def fake_restart_one(target, qa_cfg, name, handles, log_dir):
        m._svc_restart_calls.append((target, qa_cfg, name, handles, log_dir))
        if m._restart_delay:
            time.sleep(m._restart_delay)
        return [f"handle-{name}-v2" if h == f"handle-{name}" else h for h in handles]

    monkeypatch.setattr(m.qa_env, "up", fake_up)
    monkeypatch.setattr(m.qa_env, "down", fake_down)
    monkeypatch.setattr(m.qa_env, "restart_one", fake_restart_one)
    yield m
    # This module instance's atexit hooks were registered at import time and
    # only ever fire at the WHOLE pytest process's real interpreter exit —
    # long after monkeypatch has reverted qa_env.up/down back to the real
    # functions. By then this test's fake sentinel "handles" (plain strings,
    # not ServiceHandle objects) would otherwise be handed to the REAL
    # down(), which is harmless (it only WARNs) but noisy. Clear module
    # state so there's nothing left for either atexit hook to tear down.
    m._QA_SERVICES["handles"] = None
    m._teach_session = None
    # Hand the workspace qa.services mutex back if a test left the session
    # holding it: the flock lives on an fd that would otherwise stay open for
    # the rest of the pytest process.
    m._qa_services_release_ports()


def _wait_for_qa_state(cc, states, timeout=5.0):
    """Poll the module global directly (fast) until _QA_SERVICES reaches one
    of `states` — the bring-up runs off a background thread, so a real
    (bounded) wait is unavoidable."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = cc._QA_SERVICES["state"]
        if last in states:
            return last
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for qa_services state in {states} — last={last}")


def _wait_for_build_state_qa_services(cc, states, timeout=5.0):
    """Same wait, but through build_state() itself — proves the dashboard's
    own poll route (not just the internal dict) surfaces the transition."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        qs = cc.build_state().get("qa_services")
        last = qs
        if qs and qs.get("state") in states:
            return qs
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for qa_services state in {states} — last={last}")


def _wait_for_teach_state(cc, states, timeout=5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = cc.do_qa_teach_status()
        if last.get("state") in states:
            return last
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for teach state in {states} — last={last}")


# ===========================================================================
# /qa-services-start — background bring-up, state reaches `running`.
# ===========================================================================

def test_start_brings_services_up_and_build_state_reaches_running(cc):
    result = cc.do_qa_services_start()
    assert result["ok"] is True

    qs = _wait_for_build_state_qa_services(cc, {"running", "error"})
    assert qs["state"] == "running", qs
    assert qs["services"] == [
        {"name": "web", "ready": "http://127.0.0.1:9999/never-answers"},
        {"name": "api", "ready": None},
    ]
    assert qs["error"] is None
    assert len(cc._svc_up_calls) == 1


def test_start_rejects_when_no_qa_services_configured(cc):
    (cc.AP_HOME / "config.yaml").write_text(QA_CFG_NO_SERVICES)
    result = cc.do_qa_services_start()
    assert result["ok"] is False
    assert "services" in result["msg"].lower()
    assert cc._svc_up_calls == []


def test_start_when_already_running_is_a_noop_ok(cc):
    cc.do_qa_services_start()
    _wait_for_qa_state(cc, {"running"})
    result = cc.do_qa_services_start()
    assert result["ok"] is True
    assert "already" in result["msg"].lower()
    assert len(cc._svc_up_calls) == 1   # no second bring-up


def test_start_when_already_starting_is_a_noop_ok(cc):
    cc._up_delay = 0.5   # keep the bring-up thread mid-flight for the assert below
    first = cc.do_qa_services_start()
    assert first["ok"] is True
    assert cc._QA_SERVICES["state"] == "starting"

    second = cc.do_qa_services_start()
    assert second["ok"] is True
    assert "already" in second["msg"].lower()

    _wait_for_qa_state(cc, {"running"})
    assert len(cc._svc_up_calls) == 1


def test_start_failure_sets_error_state(cc):
    def boom(target, qa_cfg, log_dir):
        raise cc.qa_env.QaEnvError("service 'web' never became ready")
    cc.qa_env.up = boom   # override the fixture's fake_up for this one test

    result = cc.do_qa_services_start()
    assert result["ok"] is True   # start itself just backgrounds the attempt
    state = _wait_for_qa_state(cc, {"error"})
    assert state == "error"
    assert "never became ready" in cc._QA_SERVICES["error"]


# ===========================================================================
# /qa-services-stop — tears down, resets to `stopped`.
# ===========================================================================

def test_stop_tears_down_and_resets_state(cc):
    cc.do_qa_services_start()
    _wait_for_qa_state(cc, {"running"})
    handles_at_running = cc._QA_SERVICES["handles"]

    result = cc.do_qa_services_stop()
    assert result["ok"] is True
    assert cc._QA_SERVICES["state"] == "stopped"
    assert cc._QA_SERVICES["handles"] is None
    assert cc._svc_down_calls == [handles_at_running]
    assert cc.build_state()["qa_services"]["state"] == "stopped"


def test_stop_with_nothing_running_is_still_ok_and_never_downs(cc):
    result = cc.do_qa_services_stop()
    assert result["ok"] is True
    assert cc._svc_down_calls == []


# ===========================================================================
# /qa-services-restart — one named service via qa_env.restart_one, or all.
# ===========================================================================

def test_restart_named_service_calls_restart_one_with_right_name(cc):
    cc.do_qa_services_start()
    _wait_for_qa_state(cc, {"running"})
    handles_before = cc._QA_SERVICES["handles"]

    result = cc.do_qa_services_restart({"service": "web"})
    assert result["ok"] is True
    assert len(cc._svc_restart_calls) == 1
    target, qa_cfg, name, handles_arg, log_dir = cc._svc_restart_calls[0]
    assert name == "web"
    assert handles_arg == handles_before
    assert cc._QA_SERVICES["state"] == "running"
    assert cc._QA_SERVICES["handles"] == ["handle-web-v2", "handle-api"]


def test_restart_unknown_service_rejected(cc):
    cc.do_qa_services_start()
    _wait_for_qa_state(cc, {"running"})

    result = cc.do_qa_services_restart({"service": "ghost"})
    assert result["ok"] is False
    assert "ghost" in result["msg"]
    assert cc._svc_restart_calls == []
    assert cc._QA_SERVICES["state"] == "running"   # untouched


def test_restart_requires_running(cc):
    result = cc.do_qa_services_restart({"service": "web"})
    assert result["ok"] is False
    assert "running" in result["msg"].lower()
    assert cc._svc_restart_calls == []


def test_restart_all_with_no_service_downs_then_ups(cc):
    cc.do_qa_services_start()
    _wait_for_qa_state(cc, {"running"})
    up_calls_before = len(cc._svc_up_calls)

    result = cc.do_qa_services_restart({})
    assert result["ok"] is True
    assert cc._svc_restart_calls == []   # restart_one is not used for "all"
    assert len(cc._svc_down_calls) == 1
    assert len(cc._svc_up_calls) == up_calls_before + 1
    assert cc._QA_SERVICES["state"] == "running"


# ===========================================================================
# CRITICAL (review fix): a failed named-service restart must NOT orphan the
# OTHER still-running services by discarding `handles` to None. restart_one's
# contract is that on failure only the named service is dead (already killed
# before the fresh spawn attempt) — every other handle is untouched and
# still alive; the survivors must stay tracked so a later Stop/atexit still
# tears them down.
# ===========================================================================

def test_restart_named_service_failure_keeps_other_handles_alive_no_orphan(cc):
    cc.do_qa_services_start()
    _wait_for_qa_state(cc, {"running"})
    handles_before = cc._QA_SERVICES["handles"]
    assert handles_before == ["handle-web", "handle-api"]

    def boom(target, qa_cfg, name, handles, log_dir):
        cc._svc_restart_calls.append((target, qa_cfg, name, handles, log_dir))
        raise cc.qa_env.QaEnvError(f"service {name!r} never became ready")
    cc.qa_env.restart_one = boom

    result = cc.do_qa_services_restart({"service": "web"})
    assert result["ok"] is False
    assert "restart failed" in result["msg"].lower()
    assert cc._QA_SERVICES["state"] == "error"

    # The surviving 'api' handle must NOT be discarded to None — discarding
    # it would make a later Stop/atexit tear down ZERO services, orphaning
    # the still-alive 'api' process holding its port.
    assert cc._QA_SERVICES["handles"] is not None
    assert cc._QA_SERVICES["handles"] == handles_before

    # A subsequent Stop must still down() the survivors, not skip them.
    stop_result = cc.do_qa_services_stop()
    assert stop_result["ok"] is True
    assert cc._svc_down_calls == [handles_before]
    assert cc._QA_SERVICES["state"] == "stopped"
    assert cc._QA_SERVICES["handles"] is None


def test_restart_all_failure_clears_handles_since_old_set_already_torn_down(cc):
    # Contrast case: restart-ALL (no `service`) already down()'d the old set
    # before attempting up() again, and up() itself tears down anything it
    # partially started before raising — nothing survives to track, so
    # `handles` correctly goes to None here (unlike the named-service case
    # above).
    cc.do_qa_services_start()
    _wait_for_qa_state(cc, {"running"})

    def boom_up(target, qa_cfg, log_dir):
        cc._svc_up_calls.append((target, qa_cfg, log_dir))
        raise cc.qa_env.QaEnvError("boom")
    cc.qa_env.up = boom_up

    result = cc.do_qa_services_restart({})
    assert result["ok"] is False
    assert cc._QA_SERVICES["state"] == "error"
    assert cc._QA_SERVICES["handles"] is None


# ===========================================================================
# Lock-race safety: /qa-services-stop racing a mid-flight /qa-services-start
# or /qa-services-restart. Both must end up safe — no orphaned processes,
# and a final state that matches the Stop the operator actually asked for.
# ===========================================================================

def test_stop_races_mid_flight_start_thread_tears_down_what_it_started(cc):
    cc._up_delay = 0.3
    start_result = cc.do_qa_services_start()
    assert start_result["ok"] is True
    assert cc._QA_SERVICES["state"] == "starting"

    # Stop wins the race: it runs (and returns) while the bring-up thread is
    # still sleeping inside fake_up, well before it ever reaches _QA_SERVICES.
    stop_result = cc.do_qa_services_stop()
    assert stop_result["ok"] is True
    assert cc._QA_SERVICES["state"] == "stopped"
    assert cc._QA_SERVICES["handles"] is None
    # Nothing to down() yet at this instant — the bring-up thread hasn't
    # produced any handles.
    assert cc._svc_down_calls == []

    # The bring-up thread finishes shortly after and notices state is no
    # longer 'starting' — it must tear down what IT just started rather
    # than resurrecting the stopped session or leaking the new processes.
    deadline = time.time() + 5.0
    while time.time() < deadline and not cc._svc_down_calls:
        time.sleep(0.02)
    assert cc._svc_down_calls == [["handle-web", "handle-api"]]
    assert cc._QA_SERVICES["state"] == "stopped"
    assert cc._QA_SERVICES["handles"] is None


def test_stop_races_mid_flight_restart_tears_down_the_fresh_handles(cc):
    cc.do_qa_services_start()
    _wait_for_qa_state(cc, {"running"})

    cc._restart_delay = 0.3
    result_box = {}

    def do_restart():
        result_box["result"] = cc.do_qa_services_restart({"service": "web"})

    t = threading.Thread(target=do_restart, daemon=True)
    t.start()
    # Give the restart call time to read the pre-call handles and enter the
    # (delayed) qa_env.restart_one before Stop runs.
    time.sleep(0.05)

    stop_result = cc.do_qa_services_stop()
    assert stop_result["ok"] is True
    assert cc._QA_SERVICES["state"] == "stopped"
    assert cc._QA_SERVICES["handles"] is None
    # Stop tore down whatever was running at that moment.
    assert cc._svc_down_calls == [["handle-web", "handle-api"]]

    t.join(timeout=5.0)
    assert not t.is_alive()
    assert result_box["result"]["ok"] is True
    assert "stopped while restarting" in result_box["result"]["msg"].lower()

    # restart_one's own fresh handles must ALSO be torn down (not
    # resurrected, not leaked) — the existing `stopped_meanwhile` guard.
    assert cc._svc_down_calls == [["handle-web", "handle-api"], ["handle-web-v2", "handle-api"]]
    assert cc._QA_SERVICES["state"] == "stopped"
    assert cc._QA_SERVICES["handles"] is None


# ===========================================================================
# build_state()'s `qa_services` key — present with a qa.services block,
# omitted (byte-identical /api/state) without one.
# ===========================================================================

def test_build_state_qa_services_present_when_configured(cc):
    st = cc.build_state()
    assert "qa_services" in st
    assert st["qa_services"] == {
        "state": "stopped",
        "services": [
            {"name": "web", "ready": "http://127.0.0.1:9999/never-answers"},
            {"name": "api", "ready": None},
        ],
        "error": None,
    }


def test_build_state_qa_services_omitted_when_no_services_configured(cc):
    (cc.AP_HOME / "config.yaml").write_text(QA_CFG_NO_SERVICES)
    st = cc.build_state()
    assert "qa_services" not in st


def test_build_state_qa_services_omitted_when_no_qa_block_at_all(cc):
    (cc.AP_HOME / "config.yaml").write_text("repo: '.'\n")
    st = cc.build_state()
    assert "qa_services" not in st


# ===========================================================================
# do_qa_action's run-flow — attaches (--attach) when the dev session is
# running, behaves exactly as before (no --attach) when it's stopped.
# ===========================================================================

def test_run_flow_does_not_pass_attach_when_stopped(cc, monkeypatch):
    (cc.AP_HOME / "qa" / "flows" / "login").mkdir(parents=True)
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append((argv, kw))

    monkeypatch.setattr(cc.subprocess, "Popen", FakePopen)
    result = cc.do_qa_action({"action": "run-flow", "flow": "login"})

    assert result == {"ok": True, "msg": "flow login started"}
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv == [sys.executable, str(cc.ENGINE / "qa_run.py"), str(cc.REPO), "--flow", "login"]
    assert "--attach" not in argv


def test_run_flow_passes_attach_when_dev_session_running(cc, monkeypatch):
    (cc.AP_HOME / "qa" / "flows" / "login").mkdir(parents=True)
    cc.do_qa_services_start()
    _wait_for_qa_state(cc, {"running"})

    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append((argv, kw))

    monkeypatch.setattr(cc.subprocess, "Popen", FakePopen)
    result = cc.do_qa_action({"action": "run-flow", "flow": "login"})

    assert result["ok"] is True
    assert "attached" in result["msg"].lower()
    argv, kw = calls[0]
    assert argv == [sys.executable, str(cc.ENGINE / "qa_run.py"), str(cc.REPO),
                     "--flow", "login", "--attach"]


# ===========================================================================
# _qa_teach_bring_up — skips qa_env.up() (and every teardown down() call)
# when the persistent dev session is running; behaves exactly as before
# (ups/downs its own) when it's stopped.
# ===========================================================================

def test_teach_skips_qa_env_up_when_dev_session_running_and_still_records(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    cc.do_qa_services_start()
    _wait_for_qa_state(cc, {"running"})
    up_calls_before = len(cc._svc_up_calls)
    down_calls_before = len(cc._svc_down_calls)

    start = cc.do_qa_teach_start({"name": "attached-flow"})
    assert start["ok"] is True

    st = _wait_for_teach_state(cc, {"recorded", "failed"})
    assert st["state"] == "recorded", st
    assert cc._teach_session["attached"] is True
    assert cc._teach_session["handles"] == []

    # Teach never called qa_env.up() itself, and never tore the persistent
    # session's own services down either.
    assert len(cc._svc_up_calls) == up_calls_before
    assert len(cc._svc_down_calls) == down_calls_before

    save = cc.do_qa_teach_save({"intent": "an intent", "criteria": ["a criterion"]})
    assert save["ok"] is True, save
    assert (cc.AP_HOME / "qa" / "flows" / "attached-flow").is_dir()

    # Saving doesn't touch the persistent session's services either.
    assert len(cc._svc_down_calls) == down_calls_before
    # The persistent dev session is still running — teach never stopped it.
    assert cc.build_state()["qa_services"]["state"] == "running"


def test_teach_cancel_while_attached_does_not_down_persistent_services(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path, sleep=10, write=False))
    cc.do_qa_services_start()
    _wait_for_qa_state(cc, {"running"})
    down_calls_before = len(cc._svc_down_calls)

    cc.do_qa_teach_start({"name": "attached-cancel-flow"})
    st = _wait_for_teach_state(cc, {"recording", "failed"})
    assert st["state"] == "recording", st
    assert cc._teach_session["attached"] is True

    cancel = cc.do_qa_teach_cancel()
    assert cancel["ok"] is True
    assert len(cc._svc_down_calls) == down_calls_before   # persistent services untouched
    assert cc.build_state()["qa_services"]["state"] == "running"


def test_teach_still_brings_its_own_services_up_when_dev_session_stopped(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    # _QA_SERVICES starts 'stopped' by default — never started in this test.
    assert cc._QA_SERVICES["state"] == "stopped"

    start = cc.do_qa_teach_start({"name": "own-services-flow"})
    assert start["ok"] is True
    st = _wait_for_teach_state(cc, {"recorded", "failed"})
    assert st["state"] == "recorded", st
    assert cc._teach_session["attached"] is False
    assert len(cc._svc_up_calls) == 1     # teach's own bring-up
    assert len(cc._svc_down_calls) == 1   # torn back down once recording finished

    save = cc.do_qa_teach_save({"intent": "an intent", "criteria": ["a criterion"]})
    assert save["ok"] is True, save
    assert (cc.AP_HOME / "qa" / "flows" / "own-services-flow").is_dir()
    # Never touched the (never-started) persistent session's own bookkeeping.
    assert cc._QA_SERVICES["state"] == "stopped"


# ── F4: the Dev session is one consumer of THE workspace qa.services mutex ───
# The ports are a single shared resource: this session, the loop's own QA gate
# (qa_check.py) and a qa_run --regression/--explore/--generate run can each
# bind them, and before this only qa_check took a lock at all. The session
# holds the mutex from Start until Stop, because that is exactly how long its
# services stay bound.

def _hold_services_lock(cc):
    """Hold the mutex from a LIVE child process, standing in for the loop's
    QA gate holding it across its claude invocation. Live matters: locks.hold
    breaks a lock whose recorded pid is dead."""
    code = textwrap.dedent("""\
        import sys, time
        sys.path.insert(0, sys.argv[1])
        import locks
        with locks.hold(sys.argv[2], timeout=10):
            print("held", flush=True)
            time.sleep(30)
        """)
    lock = cc.qa_check.services_lock_path(cc.AP_STATE)
    lock.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", code, str(ENGINE), str(lock)],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_dev_session_start_reports_busy_while_another_consumer_holds_the_ports(cc):
    cc.QA_SERVICES_LOCK_WAIT = 0.2      # no reason to make the test wait
    holder = _hold_services_lock(cc)
    try:
        result = cc.do_qa_services_start()
        assert result["ok"] is False
        assert "ports are busy" in result["msg"]
        assert cc._QA_SERVICES["state"] == "stopped"   # never promised to start
        assert cc._svc_up_calls == []                  # nothing was launched
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def _probe_lock_from_a_child(cc, timeout=0.4):
    """"busy" or "free", asked from ANOTHER PROCESS - which is the only honest
    way to ask. locks.hold's reentrancy guard is keyed by pid, so a contender
    inside this same process would raise ReentrantLock rather than contend,
    and the cross-process exclusion is the thing under test."""
    code = textwrap.dedent("""\
        import sys
        sys.path.insert(0, sys.argv[1])
        import locks
        try:
            with locks.hold(sys.argv[2], timeout=float(sys.argv[3]), break_stale=False):
                print("free")
        except locks.LockTimeout:
            print("busy")
        """)
    lock = cc.qa_check.services_lock_path(cc.AP_STATE)
    r = subprocess.run([sys.executable, "-c", code, str(ENGINE), str(lock), str(timeout)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_dev_session_holds_the_ports_until_stop(cc):
    assert cc.do_qa_services_start()["ok"] is True
    assert _wait_for_qa_state(cc, {"running"}) == "running"
    assert _probe_lock_from_a_child(cc) == "busy"       # a running session owns it
    assert cc.do_qa_services_stop()["ok"] is True
    assert _probe_lock_from_a_child(cc, timeout=2) == "free"   # ...and hands it back


def test_dev_session_hands_the_ports_back_when_bring_up_fails(cc, monkeypatch):
    """A failed Start binds nothing, so holding the mutex in `error` would lock
    every other consumer out until someone remembered to click Stop."""
    def boom(target, qa_cfg, log_dir):
        raise RuntimeError("port already in use")

    monkeypatch.setattr(cc.qa_env, "up", boom)
    assert cc.do_qa_services_start()["ok"] is True
    assert _wait_for_qa_state(cc, {"error"}) == "error"
    assert _probe_lock_from_a_child(cc, timeout=2) == "free"
