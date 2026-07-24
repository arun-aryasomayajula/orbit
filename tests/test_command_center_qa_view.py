# Task 23 — dashboard QA view: build_state's `qa_flows` key (the taught-flow
# library + recent run verdicts + pending-heal count) and POST /qa-action's
# `do_qa_action` (a validated, detached spawn of `qa_run.py --flow <name>`).
# Heal approve/reject stay refused here with a clear message — that UI is
# Phase 5.
#
# Run: python3.11 -m pytest tests/test_command_center_qa_view.py -v   (from orbit/)
import importlib.util
import json
import os
import sys
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


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "backlog.yaml").write_text("tasks:\n")
    monkeypatch.setenv("AP_HOME", str(ap))
    monkeypatch.setenv("AP_STATE", str(ap / "state"))
    return _mod("command_center")


def _write_flow(cc, name, intent="do the thing", entry_path="http://x/y",
                last_verdict="pass", last_run_at="2026-07-20T00:00:00Z", runs=3,
                flows_dir="qa/flows"):
    fd = cc.AP_HOME / flows_dir / name
    fd.mkdir(parents=True)
    (fd / "intent.md").write_text(
        "---\n"
        f"name: {name}\n"
        "created: '2026-07-01T00:00:00Z'\n"
        f"entry_path: {entry_path}\n"
        "tags: []\n"
        "---\n"
        "\n"
        f"{intent}\n"
        "\n"
        "## Success criteria\n"
        "- it works\n")
    meta = {"created_at": "2026-07-01T00:00:00Z", "last_run_at": last_run_at,
             "last_verdict": last_verdict, "runs": runs, "heals": []}
    (fd / "meta.json").write_text(json.dumps(meta))
    return fd


def _write_run(cc, filename, mtime_offset=0, **fields):
    d = cc.QA_DIR / "runs"
    d.mkdir(parents=True, exist_ok=True)
    body = {"flow": "login", "verdict": "pass", "failed_step": None,
            "screenshot": None, "summary": "ok", "healed_candidate": None}
    body.update(fields)
    p = d / filename
    p.write_text(json.dumps(body))
    now = time.time()
    os.utime(p, (now + mtime_offset, now + mtime_offset))
    return p


# ── build_state()'s qa_flows key ──────────────────────────────────────────────

def test_qa_flows_absent_without_a_flows_dir(cc):
    st = cc.build_state()
    assert "qa_flows" not in st


def test_qa_flows_absent_when_flows_dir_exists_but_is_empty(cc):
    (cc.AP_HOME / "qa" / "flows").mkdir(parents=True)
    st = cc.build_state()
    assert "qa_flows" not in st


def test_qa_flows_shape_with_two_flows_a_run_and_a_heal_candidate(cc):
    _write_flow(cc, "login", intent="log a user in", entry_path="http://x/login",
                last_verdict="pass", last_run_at="2026-07-20T00:00:00Z", runs=3)
    _write_flow(cc, "checkout", intent="complete checkout", entry_path="http://x/checkout",
                last_verdict="fail", last_run_at="2026-07-21T00:00:00Z", runs=1)
    _write_run(cc, "20260720T000000Z-login.json", mtime_offset=-100,
               flow="login", verdict="pass", summary="ok")
    _write_run(cc, "20260721T000000Z-checkout.json", mtime_offset=-50,
               flow="checkout", verdict="fail", failed_step=2,
               screenshot="flow-checkout/01.png", summary="broke")
    heal_dir = cc.QA_DIR / "heal"
    heal_dir.mkdir(parents=True)
    (heal_dir / "checkout--20260721T000000Z.json").write_text(json.dumps({"flow": "checkout"}))

    st = cc.build_state()
    qf = st["qa_flows"]

    assert [f["name"] for f in qf["flows"]] == ["checkout", "login"]  # sorted by name
    login = next(f for f in qf["flows"] if f["name"] == "login")
    assert login == {"name": "login", "intent_line": "log a user in",
                      "entry_path": "http://x/login", "last_verdict": "pass",
                      "last_run_at": "2026-07-20T00:00:00Z", "runs": 3}

    assert [r["flow"] for r in qf["recent_runs"]] == ["checkout", "login"]  # newest mtime first
    assert qf["recent_runs"][0]["file"] == "20260721T000000Z-checkout.json"
    assert qf["recent_runs"][0]["failed_step"] == 2

    assert qf["pending_heals"] == 1


def test_qa_flows_malformed_meta_json_yields_nulls_never_a_crash(cc):
    fd = cc.AP_HOME / "qa" / "flows" / "broken"
    fd.mkdir(parents=True)
    (fd / "intent.md").write_text("not even frontmatter\njust some text\n")
    (fd / "meta.json").write_text("{not valid json")
    st = cc.build_state()   # must not raise
    row = st["qa_flows"]["flows"][0]
    assert row == {"name": "broken", "intent_line": None, "entry_path": None,
                   "last_verdict": None, "last_run_at": None, "runs": None}


def test_qa_flows_missing_intent_and_meta_files_yield_nulls_never_a_crash(cc):
    fd = cc.AP_HOME / "qa" / "flows" / "bare"
    fd.mkdir(parents=True)   # no intent.md, no meta.json at all
    st = cc.build_state()
    row = st["qa_flows"]["flows"][0]
    assert row == {"name": "bare", "intent_line": None, "entry_path": None,
                   "last_verdict": None, "last_run_at": None, "runs": None}


def test_qa_flows_respects_configured_flows_dir(cc):
    (cc.AP_HOME / "config.yaml").write_text("qa:\n  flows_dir: custom/flows\n")
    _write_flow(cc, "login", flows_dir="custom/flows")
    st = cc.build_state()
    assert [f["name"] for f in st["qa_flows"]["flows"]] == ["login"]


def test_pending_heals_excludes_approved_and_rejected_subdirs(cc):
    _write_flow(cc, "login")
    heal = cc.QA_DIR / "heal"
    (heal / "approved").mkdir(parents=True)
    (heal / "rejected").mkdir(parents=True)
    (heal / "pending-1.json").write_text("{}")
    (heal / "pending-2.json").write_text("{}")
    (heal / "approved" / "old-1.json").write_text("{}")
    (heal / "rejected" / "old-2.json").write_text("{}")
    st = cc.build_state()
    assert st["qa_flows"]["pending_heals"] == 2


def test_recent_runs_capped_at_ten_newest_first(cc):
    _write_flow(cc, "login")
    for i in range(12):
        _write_run(cc, f"run-{i:02d}.json", mtime_offset=-(12 - i), summary=f"run {i}")
    st = cc.build_state()
    recent = st["qa_flows"]["recent_runs"]
    assert len(recent) == 10
    assert recent[0]["summary"] == "run 11"
    assert recent[-1]["summary"] == "run 2"


# ── POST /qa-action → do_qa_action ────────────────────────────────────────────

def test_do_qa_action_run_flow_spawns_detached_popen_with_right_argv(cc, monkeypatch):
    _write_flow(cc, "login")
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
    assert kw.get("start_new_session") is True
    assert (cc.LOGDIR / "qa-run-login.log").exists()


def test_do_qa_action_rejects_bad_flow_name_and_never_spawns(cc, monkeypatch):
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    result = cc.do_qa_action({"action": "run-flow", "flow": "Not A Slug!"})
    assert result["ok"] is False
    assert "slug" in result["msg"].lower()
    assert calls == []


def test_do_qa_action_rejects_unknown_flow_and_never_spawns(cc, monkeypatch):
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    result = cc.do_qa_action({"action": "run-flow", "flow": "ghost-flow"})
    assert result["ok"] is False
    assert "ghost-flow" in result["msg"]
    assert calls == []


def test_do_qa_action_rejects_unknown_action(cc, monkeypatch):
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    result = cc.do_qa_action({"action": "delete-everything", "flow": "login"})
    assert result["ok"] is False
    assert "unknown" in result["msg"].lower()
    assert calls == []


def test_do_qa_action_heal_actions_are_refused_as_phase_5(cc, monkeypatch):
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    for action in ("heal-approve", "heal-reject"):
        result = cc.do_qa_action({"action": action, "flow": "login"})
        assert result["ok"] is False
        assert "phase 5" in result["msg"].lower()
    assert calls == []
