# command_center.py's QA teach session — the dashboard's "New test / Record"
# panel, the web equivalent of `orbit teach` (qa_teach.py). A CLI `teach` run
# is one blocking process (bring qa.services up -> foreground codegen ->
# stdin prompts -> write); the dashboard spreads the same three steps across
# THREE requests instead, since an HTTP handler must never block on either
# qa_env.up() (up to ready_timeout_seconds) or an operator driving a browser:
#   POST /qa-teach-start  -> validates like qa_teach.main(), backgrounds the
#                            slow bring-up + spawns codegen, returns at once.
#   GET  /qa-teach-status -> polled by the panel: starting -> recording ->
#                            recorded | failed.
#   POST /qa-teach-save   -> reuses qa_teach._write_flow/_update_flowmap
#                            verbatim, tears qa.services down, clears session.
#   POST /qa-teach-cancel -> kills the recorder, tears down, clears.
#
# Hermetic: qa_env.up/down are monkeypatched (no real services), and
# ORBIT_CODEGEN_CMD replaces the real `npx playwright codegen` call (same
# test hook tests/test_qa_teach.py uses) so no real browser is ever launched.
#
# Run: python3.11 -m pytest tests/test_command_center_teach.py -v   (from orbit/)
import importlib.util
import json
import re
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import yaml

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
    """)

SPEC_CONTENT = (
    "import { test } from '@playwright/test';\n"
    "test('recorded flow', async ({ page }) => {\n"
    "  await page.goto('http://example.test/app');\n"
    "});\n"
)


def _fake_codegen_cmd(tmp_path, content=SPEC_CONTENT, rc=0, write=True, sleep=0, name="codegen"):
    """A fake ORBIT_CODEGEN_CMD target — same substitution mechanism (and the
    same env-not-argv contract) as tests/test_qa_teach.py's fake_codegen:
    reads the output path off ORBIT_CODEGEN_OUTPUT and writes `content`
    there, optionally sleeping first (so a test can observe the `recording`
    state, or cancel before it ever finishes)."""
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

    # Hermetic: no real qa.services ever launched, and a spy so tests can
    # assert teardown actually happened.
    m._teach_up_calls = []
    m._teach_down_calls = []

    def fake_up(target, qa_cfg, log_dir):
        m._teach_up_calls.append((target, qa_cfg, log_dir))
        return []

    def fake_down(handles):
        m._teach_down_calls.append(handles)

    monkeypatch.setattr(m.qa_env, "up", fake_up)
    monkeypatch.setattr(m.qa_env, "down", fake_down)
    return m


def _wait_for_state(cc, states, timeout=5.0):
    """Poll do_qa_teach_status() (exactly what GET /qa-teach-status calls)
    until it reports one of `states` — the bring-up + recorder both run off
    a background thread, so a real (bounded) wait is unavoidable."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = cc.do_qa_teach_status()
        if last.get("state") in states:
            return last
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for state in {states} — last={last}")


# ===========================================================================
# Happy path: start -> recording -> recorded -> save writes a complete flow.
# ===========================================================================

def test_start_to_recorded_to_save_writes_complete_flow(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))

    start = cc.do_qa_teach_start({"name": "login-flow", "url": ""})
    assert start["ok"] is True

    st = _wait_for_state(cc, {"recorded", "failed"})
    assert st["state"] == "recorded", st
    assert st["active"] is True
    assert st["name"] == "login-flow"

    save = cc.do_qa_teach_save({
        "intent": "user can log in",
        "criteria": ["sees the dashboard", "sees their name in the header"],
    })
    assert save["ok"] is True, save
    assert save["flow"] == "login-flow"

    fd = cc.AP_HOME / "qa" / "flows" / "login-flow"
    assert fd.is_dir()

    intent_text = (fd / "intent.md").read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", intent_text, re.DOTALL)
    assert m, intent_text
    front = yaml.safe_load(m.group(1))
    assert front["name"] == "login-flow"
    assert front["entry_path"] == "http://example.test/app"
    assert front["tags"] == []
    body = m.group(2)
    assert "user can log in" in body
    assert "## Success criteria" in body
    assert "- sees the dashboard" in body
    assert "- sees their name in the header" in body

    assert (fd / "steps.spec.ts").read_text() == SPEC_CONTENT

    meta = json.loads((fd / "meta.json").read_text())
    assert set(meta.keys()) == {"created_at", "last_run_at", "last_verdict", "runs", "heals",
                                 "requires", "provides_auth"}
    assert meta["last_run_at"] is None and meta["last_verdict"] is None and meta["runs"] == 0
    # Test chaining (Task "prerequisites"): defaults when the save payload
    # names neither — see the dedicated section below for the non-default
    # path (prerequisites + provides_auth actually supplied).
    assert meta["requires"] == [] and meta["provides_auth"] is False

    flowmap = (cc.AP_HOME / "qa" / "FLOWMAP.md").read_text()
    assert "| login-flow |" in flowmap
    assert "user can log in" in flowmap
    assert "http://example.test/app" in flowmap

    # session cleared, services torn down (fake up() returned [] but down()
    # must still have been invoked — that's the teardown-happened spy)
    assert cc.do_qa_teach_status() == {"active": False}
    assert len(cc._teach_down_calls) >= 1
    assert len(cc._teach_up_calls) == 1


def test_url_falls_back_to_qa_base_url_when_not_given(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    start = cc.do_qa_teach_start({"name": "no-url-flow"})
    assert start["ok"] is True
    st = _wait_for_state(cc, {"recorded", "failed"})
    assert st["state"] == "recorded"
    save = cc.do_qa_teach_save({"intent": "intent line", "criteria": ["criterion one"]})
    assert save["ok"] is True
    front = yaml.safe_load(re.match(
        r"^---\n(.*?)\n---\n",
        (cc.AP_HOME / "qa" / "flows" / "no-url-flow" / "intent.md").read_text(),
        re.DOTALL).group(1))
    assert front["entry_path"] == "http://example.test/app"


# ===========================================================================
# /qa-teach-start validation — same order/shape as qa_teach.main().
# ===========================================================================

def test_start_rejects_duplicate_flow_name(cc):
    existing = cc.AP_HOME / "qa" / "flows" / "dup-flow"
    existing.mkdir(parents=True)
    result = cc.do_qa_teach_start({"name": "dup-flow"})
    assert result["ok"] is False
    assert "already exists" in result["msg"]
    assert cc.do_qa_teach_status() == {"active": False}


def test_start_rejects_bad_slug(cc):
    result = cc.do_qa_teach_start({"name": "Not_A_Slug!"})
    assert result["ok"] is False
    assert "slug" in result["msg"].lower()
    assert cc.do_qa_teach_status() == {"active": False}


def test_start_rejects_missing_qa_block(cc):
    (cc.AP_HOME / "config.yaml").write_text("repo: '.'\n")   # no qa: block
    result = cc.do_qa_teach_start({"name": "some-flow"})
    assert result["ok"] is False
    assert "qa" in result["msg"].lower()
    assert cc.do_qa_teach_status() == {"active": False}


def test_start_rejects_no_url_and_no_base_url(cc):
    (cc.AP_HOME / "config.yaml").write_text("qa:\n  ready_timeout_seconds: 90\n")
    result = cc.do_qa_teach_start({"name": "no-url-flow"})
    assert result["ok"] is False
    assert "url" in result["msg"].lower()


def test_start_rejects_when_a_session_is_already_active(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path, sleep=2))
    first = cc.do_qa_teach_start({"name": "first-flow"})
    assert first["ok"] is True

    second = cc.do_qa_teach_start({"name": "second-flow"})
    assert second["ok"] is False
    assert "already active" in second["msg"].lower() or "active" in second["msg"].lower()

    # Clean up the still-running session so the test doesn't leak a thread.
    cc.do_qa_teach_cancel()


# ===========================================================================
# Recorder failure -> status `failed`, services torn down.
# ===========================================================================

def test_codegen_nonzero_exit_yields_failed_and_tears_down(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path, rc=1, write=False))
    start = cc.do_qa_teach_start({"name": "broken-flow"})
    assert start["ok"] is True

    st = _wait_for_state(cc, {"failed"})
    assert st["state"] == "failed"
    assert st["error"]
    # Torn down by _qa_teach_bring_up's own post-wait teardown — already
    # done by the time this poll even ran, not triggered by the poll itself.
    assert len(cc._teach_down_calls) == 1

    # The (still-parked, failed) session refuses a save...
    save = cc.do_qa_teach_save({"intent": "x", "criteria": ["y"]})
    assert save["ok"] is False
    # ...cancel clears it so a fresh Record can start, WITHOUT double-downing
    # the already-torn-down handles (the torn_down claim-guard, fix for the
    # double-down Minor).
    cancel = cc.do_qa_teach_cancel()
    assert cancel["ok"] is True
    assert cc.do_qa_teach_status() == {"active": False}
    assert len(cc._teach_down_calls) == 1


def test_codegen_empty_output_yields_failed(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path, content="", rc=0))
    start = cc.do_qa_teach_start({"name": "empty-flow"})
    assert start["ok"] is True
    st = _wait_for_state(cc, {"failed"})
    assert st["state"] == "failed"
    assert "no output" in st["error"].lower() or "empty" in st["error"].lower() or st["error"]


# ===========================================================================
# /qa-teach-cancel — kills the recorder, tears down, clears.
# ===========================================================================

def test_cancel_kills_recorder_tears_down_and_clears(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path, sleep=10, write=False))
    start = cc.do_qa_teach_start({"name": "cancel-me"})
    assert start["ok"] is True

    st = _wait_for_state(cc, {"recording", "failed"})
    assert st["state"] == "recording", st   # still sleeping, not yet exited
    proc = cc._teach_session["proc"]
    assert proc is not None and proc.poll() is None   # genuinely still running

    result = cc.do_qa_teach_cancel()
    assert result["ok"] is True

    deadline = time.time() + 5
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.02)
    assert proc.poll() is not None   # actually killed, not just abandoned

    assert cc.do_qa_teach_status() == {"active": False}
    # Exactly one down() call: cancel nulls _teach_session BEFORE killing the
    # proc, so the bring-up thread's own proc.wait() (which only unblocks
    # once cancel's kill lands) always finds itself no longer the owner and
    # never reaches its own teardown call.
    assert len(cc._teach_down_calls) == 1
    assert not (cc.AP_HOME / "qa" / "flows" / "cancel-me").exists()


def test_cancel_with_no_active_session_reports_ok_false(cc):
    result = cc.do_qa_teach_cancel()
    assert result["ok"] is False


# ===========================================================================
# /qa-teach-save — requires a non-empty intent and >= 1 criterion.
# ===========================================================================

def test_save_requires_intent_and_criteria_session_stays_recorded(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    cc.do_qa_teach_start({"name": "needs-both"})
    _wait_for_state(cc, {"recorded", "failed"})
    assert cc.do_qa_teach_status()["state"] == "recorded"

    no_intent = cc.do_qa_teach_save({"intent": "", "criteria": ["a criterion"]})
    assert no_intent["ok"] is False
    assert cc.do_qa_teach_status()["state"] == "recorded"   # not consumed

    no_criteria = cc.do_qa_teach_save({"intent": "an intent", "criteria": []})
    assert no_criteria["ok"] is False
    assert cc.do_qa_teach_status()["state"] == "recorded"

    # A newline-joined string is accepted too, normalized into a list.
    ok = cc.do_qa_teach_save({"intent": "an intent", "criteria": "first\nsecond\n\n"})
    assert ok["ok"] is True
    intent_text = (cc.AP_HOME / "qa" / "flows" / "needs-both" / "intent.md").read_text()
    assert "- first" in intent_text and "- second" in intent_text

    assert cc.do_qa_teach_status() == {"active": False}


def test_save_with_no_active_session_reports_ok_false(cc):
    result = cc.do_qa_teach_save({"intent": "x", "criteria": ["y"]})
    assert result["ok"] is False


# ===========================================================================
# CRITICAL #1 — a _write_flow failure must never leave a half-written
# flow_dir behind (it's created via mkdir(parents=True), no exist_ok, so a
# leftover partial dir blocks every future retry/re-teach of that name with
# "already exists" forever).
# ===========================================================================

def test_write_flow_failure_cleans_up_partial_flow_dir_and_retry_succeeds(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    cc.do_qa_teach_start({"name": "half-written"})
    _wait_for_state(cc, {"recorded", "failed"})
    assert cc.do_qa_teach_status()["state"] == "recorded"

    real_write_flow = cc.qa_teach._write_flow

    def partial_then_boom(flow_dir, *a, **kw):
        # Simulate _write_flow getting partway through before failing: the
        # dir (and one file) exist on disk, then it raises.
        flow_dir.mkdir(parents=True)
        (flow_dir / "intent.md").write_text("partial, should be cleaned up")
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(cc.qa_teach, "_write_flow", partial_then_boom)

    fd = cc.AP_HOME / "qa" / "flows" / "half-written"
    save = cc.do_qa_teach_save({"intent": "an intent", "criteria": ["a criterion"]})
    assert save["ok"] is False
    assert "failed writing the flow" in save["msg"]
    assert not fd.exists()   # <- the orphaned-partial-dir bug: must be gone
    # Session is still parked in `recorded` — the operator can just retry.
    assert cc.do_qa_teach_status()["state"] == "recorded"

    monkeypatch.setattr(cc.qa_teach, "_write_flow", real_write_flow)
    retry = cc.do_qa_teach_save({"intent": "an intent", "criteria": ["a criterion"]})
    assert retry["ok"] is True, retry
    assert fd.is_dir()
    assert (fd / "steps.spec.ts").read_text() == SPEC_CONTENT
    assert cc.do_qa_teach_status() == {"active": False}


def test_write_flow_failure_then_cancel_does_not_block_a_fresh_start_same_name(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    cc.do_qa_teach_start({"name": "retry-name"})
    _wait_for_state(cc, {"recorded", "failed"})

    def boom(flow_dir, *a, **kw):
        flow_dir.mkdir(parents=True)
        raise OSError("simulated failure partway through")

    monkeypatch.setattr(cc.qa_teach, "_write_flow", boom)
    save = cc.do_qa_teach_save({"intent": "an intent", "criteria": ["a criterion"]})
    assert save["ok"] is False
    fd = cc.AP_HOME / "qa" / "flows" / "retry-name"
    assert not fd.exists()

    # Give up on this session entirely instead of retrying the save...
    cancel = cc.do_qa_teach_cancel()
    assert cancel["ok"] is True

    # ...a brand new /qa-teach-start for the SAME name must not bounce off
    # "already exists" — that's exactly what an un-cleaned-up flow_dir would do.
    restart = cc.do_qa_teach_start({"name": "retry-name"})
    assert restart["ok"] is True, restart
    cc.do_qa_teach_cancel()   # tidy up the still-running session


# ===========================================================================
# CRITICAL #2 — save/cancel race: cancel must refuse outright while a save
# is in progress (state `saving`), rather than rmtree-ing spec_dir or
# re-downing handles out from under _write_flow's own file copy.
# ===========================================================================

def test_cancel_refuses_while_a_save_is_in_progress(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    cc.do_qa_teach_start({"name": "racey-save"})
    _wait_for_state(cc, {"recorded", "failed"})
    assert cc.do_qa_teach_status()["state"] == "recorded"

    real_write_flow = cc.qa_teach._write_flow
    entered = threading.Event()

    def slow_write_flow(*a, **kw):
        entered.set()
        time.sleep(0.3)
        return real_write_flow(*a, **kw)

    monkeypatch.setattr(cc.qa_teach, "_write_flow", slow_write_flow)

    results = {}

    def do_save():
        results["save"] = cc.do_qa_teach_save({"intent": "x", "criteria": ["y"]})

    t = threading.Thread(target=do_save)
    t.start()
    assert entered.wait(timeout=5)   # save has claimed state="saving" and is mid-write
    assert cc.do_qa_teach_status()["state"] == "saving"

    cancel = cc.do_qa_teach_cancel()
    assert cancel["ok"] is False
    assert "progress" in cancel["msg"].lower()

    t.join(timeout=5)
    assert not t.is_alive()
    assert results["save"]["ok"] is True, results["save"]
    assert cc.do_qa_teach_status() == {"active": False}
    assert (cc.AP_HOME / "qa" / "flows" / "racey-save").is_dir()


# ===========================================================================
# IMPORTANT #3 — a malformed ORBIT_CODEGEN_CMD makes qa_teach.codegen_argv
# raise ValueError (shlex.split on an unbalanced quote), not OSError. The
# bring-up thread must catch that too (broadened `except Exception`) instead
# of dying silently and stranding the session in `starting` forever with
# qa.services left running.
# ===========================================================================

def test_malformed_codegen_cmd_value_error_does_not_strand_the_session(cc, monkeypatch):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", "echo 'unbalanced quote")   # shlex.split -> ValueError
    start = cc.do_qa_teach_start({"name": "bad-cmd-flow"})
    assert start["ok"] is True

    st = _wait_for_state(cc, {"failed"})   # not stuck in `starting`/`recording` forever
    assert st["state"] == "failed"
    assert st["error"]
    assert len(cc._teach_down_calls) == 1   # qa.services brought up then torn back down

    cancel = cc.do_qa_teach_cancel()
    assert cancel["ok"] is True
    assert cc.do_qa_teach_status() == {"active": False}


# ===========================================================================
# IMPORTANT #4 — atexit cleanup must also remove the scratch spec_dir (it
# used to kill the proc + down qa.services but leak the tmpdir).
# ===========================================================================

def test_atexit_cleanup_removes_spec_dir(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path, sleep=10, write=False))
    start = cc.do_qa_teach_start({"name": "atexit-flow"})
    assert start["ok"] is True

    st = _wait_for_state(cc, {"recording", "failed"})
    assert st["state"] == "recording", st
    spec_dir = Path(cc._teach_session["spec_dir"])
    assert spec_dir.is_dir()

    cc._teach_atexit_cleanup()
    assert not spec_dir.exists()
    assert len(cc._teach_down_calls) == 1

    # Best-effort process-exit hook — it doesn't null _teach_session (no one
    # is left to poll status after a real process exit), but the recorder it
    # killed unblocks the bring-up thread's own proc.wait(); that thread then
    # finds torn_down already claimed (by atexit) and settles into `failed`
    # without a second down() call.
    _wait_for_state(cc, {"failed"})
    assert len(cc._teach_down_calls) == 1


# ===========================================================================
# IMPORTANT #5 — GET /qa-teach-status must be a pure read: no proc.poll(),
# no qa_env.down(), no state mutation. The recording -> recorded|failed
# transition (and tearing qa.services down) is entirely _qa_teach_bring_up's
# job now, driven by blocking on the recorder's own exit, independent of
# whether anyone ever polls status at all.
# ===========================================================================

def test_status_never_calls_proc_poll(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path, sleep=1))
    cc.do_qa_teach_start({"name": "poll-check-flow"})
    st = _wait_for_state(cc, {"recording", "failed"})
    assert st["state"] == "recording", st
    proc = cc._teach_session["proc"]

    def boom(*a, **kw):
        raise AssertionError("do_qa_teach_status must never call proc.poll()")

    monkeypatch.setattr(proc, "poll", boom)

    for _ in range(5):   # several pure-read polls while still recording
        st = cc.do_qa_teach_status()
        assert st["state"] == "recording"
        time.sleep(0.05)

    cc.do_qa_teach_cancel()


def test_recorded_transition_and_teardown_happen_without_any_status_poll(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path, sleep=0.1))
    start = cc.do_qa_teach_start({"name": "no-poll-flow"})
    assert start["ok"] is True

    # Deliberately never call do_qa_teach_status while the recorder runs —
    # wait on the real subprocess directly instead, entirely out-of-band.
    deadline = time.time() + 5
    proc = None
    while proc is None and time.time() < deadline:
        sess = cc._teach_session
        proc = sess.get("proc") if sess else None
        time.sleep(0.01)
    assert proc is not None
    proc.wait(timeout=5)
    time.sleep(0.2)   # let the bring-up thread finish its own post-wait work

    # Torn down and transitioned already, before this test's first-ever
    # status call — proving neither depends on a status poll happening.
    assert len(cc._teach_down_calls) == 1
    assert cc.do_qa_teach_status()["state"] == "recorded"


def test_save_no_longer_calls_down_services_already_down_when_recorded(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    cc.do_qa_teach_start({"name": "down-once-flow"})
    _wait_for_state(cc, {"recorded", "failed"})
    assert cc.do_qa_teach_status()["state"] == "recorded"
    assert len(cc._teach_down_calls) == 1   # torn down once, by the bring-up thread

    save = cc.do_qa_teach_save({"intent": "x", "criteria": ["y"]})
    assert save["ok"] is True
    assert len(cc._teach_down_calls) == 1   # save adds no second down() call


def test_cancel_of_a_recorded_but_unsaved_session_does_not_double_down(cc, monkeypatch, tmp_path):
    # A session abandoned AFTER `recorded` (operator never clicked Save) —
    # qa.services are already down by then (the bring-up thread's own
    # teardown); cancel must still clean up the leftover spec_dir and clear
    # the session, but must not re-down the already-torn-down handles.
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    cc.do_qa_teach_start({"name": "abandoned-flow"})
    _wait_for_state(cc, {"recorded", "failed"})
    assert cc.do_qa_teach_status()["state"] == "recorded"
    assert len(cc._teach_down_calls) == 1
    spec_dir = Path(cc._teach_session["spec_dir"])
    assert spec_dir.is_dir()

    cancel = cc.do_qa_teach_cancel()
    assert cancel["ok"] is True
    assert len(cc._teach_down_calls) == 1   # no second down() call
    assert not spec_dir.exists()
    assert cc.do_qa_teach_status() == {"active": False}
    assert not (cc.AP_HOME / "qa" / "flows" / "abandoned-flow").exists()


# ===========================================================================
# /qa-teach-save — prerequisites + provides_auth (Task "prerequisites", test
# chaining). Threaded straight into qa_teach._write_flow's own params; see
# tests/test_qa_teach.py for _write_flow's own direct unit tests of the
# SLUG_RE validation and defaults.
# ===========================================================================

def test_save_with_requires_and_provides_auth_writes_them_into_meta(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    # The named prerequisite exists first, so no "not taught yet" note.
    (cc.AP_HOME / "qa" / "flows" / "login").mkdir(parents=True)

    cc.do_qa_teach_start({"name": "create-vertical"})
    _wait_for_state(cc, {"recorded", "failed"})
    save = cc.do_qa_teach_save({
        "intent": "create a vertical", "criteria": ["vertical appears in the list"],
        "requires": "login", "provides_auth": False,
    })
    assert save["ok"] is True, save

    meta = json.loads((cc.AP_HOME / "qa" / "flows" / "create-vertical" / "meta.json").read_text())
    assert meta["requires"] == ["login"]
    assert meta["provides_auth"] is False


def test_save_requires_accepts_a_list_too(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    (cc.AP_HOME / "qa" / "flows" / "login").mkdir(parents=True)
    (cc.AP_HOME / "qa" / "flows" / "add-to-cart").mkdir(parents=True)

    cc.do_qa_teach_start({"name": "checkout"})
    _wait_for_state(cc, {"recorded", "failed"})
    save = cc.do_qa_teach_save({
        "intent": "checkout", "criteria": ["order confirmed"],
        "requires": ["login", "add-to-cart"],
    })
    assert save["ok"] is True, save
    meta = json.loads((cc.AP_HOME / "qa" / "flows" / "checkout" / "meta.json").read_text())
    assert meta["requires"] == ["login", "add-to-cart"]


def test_save_provides_auth_true_writes_true(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    cc.do_qa_teach_start({"name": "login"})
    _wait_for_state(cc, {"recorded", "failed"})
    save = cc.do_qa_teach_save({
        "intent": "user can log in", "criteria": ["sees the dashboard"],
        "provides_auth": True,
    })
    assert save["ok"] is True, save
    meta = json.loads((cc.AP_HOME / "qa" / "flows" / "login" / "meta.json").read_text())
    assert meta["requires"] == []
    assert meta["provides_auth"] is True


def test_save_rejects_invalid_requires_slug_session_stays_recorded(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    cc.do_qa_teach_start({"name": "bad-requires-flow"})
    _wait_for_state(cc, {"recorded", "failed"})
    save = cc.do_qa_teach_save({
        "intent": "an intent", "criteria": ["a criterion"], "requires": "Not A Slug!",
    })
    assert save["ok"] is False
    assert "slug" in save["msg"].lower()
    # Not consumed — the operator can fix the form and resubmit.
    assert cc.do_qa_teach_status()["state"] == "recorded"
    assert not (cc.AP_HOME / "qa" / "flows" / "bad-requires-flow").exists()

    # Retry without the bad entry succeeds.
    retry = cc.do_qa_teach_save({"intent": "an intent", "criteria": ["a criterion"]})
    assert retry["ok"] is True, retry


def test_save_self_reference_gets_a_clearer_message_not_taught_yet(cc, monkeypatch, tmp_path):
    # A flow naming ITSELF in `requires` used to be lumped in with a
    # genuinely missing prerequisite ("not taught yet") — confusing, since
    # its own dir can never exist until AFTER this very save. It's not
    # blocked (qa_run.py's _resolve_chain already refuses the self-loop at
    # run time, loud exit 2) — just a clearer note, and no misleading
    # "not taught yet" phrasing for its own name.
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    cc.do_qa_teach_start({"name": "self-ref-flow"})
    _wait_for_state(cc, {"recorded", "failed"})
    save = cc.do_qa_teach_save({
        "intent": "an intent", "criteria": ["a criterion"], "requires": "self-ref-flow",
    })
    assert save["ok"] is True, save
    assert "require itself" in save["msg"].lower()
    assert "not taught yet" not in save["msg"].lower()
    meta = json.loads((cc.AP_HOME / "qa" / "flows" / "self-ref-flow" / "meta.json").read_text())
    assert meta["requires"] == ["self-ref-flow"]   # still recorded — just a warning


def test_save_warns_not_blocks_on_missing_prerequisite(cc, monkeypatch, tmp_path):
    # 'ghost-login' is SLUG_RE-valid but has no flow dir yet — warn, don't
    # block (the operator may be teaching a chain out of order).
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    cc.do_qa_teach_start({"name": "create-vertical"})
    _wait_for_state(cc, {"recorded", "failed"})
    save = cc.do_qa_teach_save({
        "intent": "create a vertical", "criteria": ["it appears"], "requires": "ghost-login",
    })
    assert save["ok"] is True, save
    assert "ghost-login" in save["msg"]
    meta = json.loads((cc.AP_HOME / "qa" / "flows" / "create-vertical" / "meta.json").read_text())
    assert meta["requires"] == ["ghost-login"]   # still recorded — just a warning


# ===========================================================================
# qa_flow_library surfaces requires/provides_auth (Task "prerequisites") —
# via the teach-session save path, following this file's own conventions
# (test_command_center_qa_view.py owns qa_flow_library's OTHER shape tests;
# this one is scoped here because it exercises the requires/provides_auth
# data this same module's do_qa_teach_save just wrote).
# ===========================================================================

def test_qa_flow_library_surfaces_requires_and_provides_auth(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBIT_CODEGEN_CMD", _fake_codegen_cmd(tmp_path))
    cc.do_qa_teach_start({"name": "login"})
    _wait_for_state(cc, {"recorded", "failed"})
    cc.do_qa_teach_save({"intent": "user can log in", "criteria": ["sees the dashboard"],
                         "provides_auth": True})

    cc.do_qa_teach_start({"name": "create-vertical"})
    _wait_for_state(cc, {"recorded", "failed"})
    cc.do_qa_teach_save({"intent": "create a vertical", "criteria": ["it appears"],
                         "requires": "login"})

    lib = cc.qa_flow_library()
    by_name = {f["name"]: f for f in lib["flows"]}
    assert by_name["login"]["provides_auth"] is True
    assert "requires" not in by_name["login"]           # omitted — it's the default ([])
    assert by_name["create-vertical"]["requires"] == ["login"]
    assert "provides_auth" not in by_name["create-vertical"]   # omitted — default (False)
