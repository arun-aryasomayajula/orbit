# Task management from the dashboard: create / edit / delete backlog tasks via
# command_center handlers, with the SAME contract bar as the lint gate (known
# category, acceptance criteria required), comment-preserving block edits,
# refusal to delete worked history, and the force-with-reason mark flow driven
# by the lifecycle machine.
#
# Run: python3 -m pytest tests/test_task_crud.py -v   (from orbit/)
import importlib.util
import json
import os
import sys
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
    (ap / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates: {t: {cmd: "true"}}\n')
    monkeypatch.setenv("AP_HOME", str(ap))
    monkeypatch.setenv("AP_STATE", str(ap / "state"))
    return _mod("command_center")


def _backlog():
    return (Path(os.environ["AP_HOME"]) / "backlog.yaml").read_text()


def test_create_lands_as_proposed_with_full_contract(cc):
    msg = cc.do_task_new("fix-login", "Fix the login timeout on slow networks", "bug",
                         "high", "Users on 3G time out.\nSeen in prod logs.",
                         "login succeeds within 30s on throttled network\nno regression in auth tests")
    assert msg.startswith("Created fix-login")
    text = _backlog()
    assert "- id: fix-login" in text
    assert "status: proposed" in text and "autopilot: human" in text
    assert "source: dashboard" in text
    assert '"login succeeds within 30s on throttled network"' in text
    assert "Users on 3G time out." in text


def test_create_queue_now_is_the_human_triage_act(cc):
    cc.do_task_new("quick-fix", "A well understood quick fix", "bug", "medium",
                   "", "the bug is gone", queue_now=True)
    text = _backlog()
    assert "status: queued" in text and "autopilot: allow" in text


def test_create_enforces_the_lint_bar(cc):
    assert "acceptance criterion" in cc.do_task_new("no-acc", "Task without criteria", "bug")
    assert "Unknown category" in cc.do_task_new("bad-cat", "A task", "yolo", acc_raw="done")
    assert "lowercase slug" in cc.do_task_new("Bad Id!", "A task", "bug", acc_raw="done")
    assert "tasks:\n" == _backlog()          # nothing written on any refusal


def test_create_refuses_duplicate_and_worked_ids(cc):
    cc.do_task_new("t1", "First version of this task", "bug", acc_raw="done")
    assert "already exists" in cc.do_task_new("t1", "Duplicate", "bug", acc_raw="done")
    (Path(os.environ["AP_STATE"]) / "ledger.json").write_text(
        json.dumps({"entries": {"old-ship": {"state": "merged", "sha": "a"}}}))
    assert "already worked" in cc.do_task_new("old-ship", "Reuses a worked id", "bug", acc_raw="done")


def test_edit_updates_only_given_fields(cc):
    cc.do_task_new("t1", "Original title for the task", "bug", "medium",
                   "original context", "original criterion")
    msg = cc.do_task_edit("t1", title="Sharper title for the task", priority="high",
                          acc_raw="first criterion\nsecond criterion")
    assert "Updated t1" in msg
    text = _backlog()
    assert '"Sharper title for the task"' in text and "priority: high" in text
    assert '"first criterion"' in text and '"second criterion"' in text
    assert '"original criterion"' not in text
    assert "original context" in text        # untouched — empty field keeps value
    assert "category: bug" in text


def test_delete_removes_unworked_but_refuses_worked(cc):
    cc.do_task_new("gone", "Task to be deleted soon", "bug", acc_raw="done")
    cc.do_task_new("kept", "Task that was already worked", "bug", acc_raw="done")
    (Path(os.environ["AP_STATE"]) / "ledger.json").write_text(
        json.dumps({"entries": {"kept": {"state": "pushed", "sha": "a"}}}))
    assert "Deleted gone" in cc.do_task_delete("gone")
    assert "- id: gone" not in _backlog()
    assert "Won't delete kept" in cc.do_task_delete("kept")
    assert "- id: kept" in _backlog()


def test_mark_force_flows_through_with_reason_and_is_stamped(cc):
    st = Path(os.environ["AP_STATE"])
    (st / "ledger.json").write_text(json.dumps({"entries": {"t1": {"state": "in_progress"}}}))
    refused = cc.do_mark("t1", "merged", "")
    assert "illegal transition" in refused
    assert "requires a reason" in cc.do_mark("t1", "merged", "", force=True)
    ok = cc.do_mark("t1", "merged", "verified by hand on prod", force=True)
    assert "marked merged" in ok
    e = json.loads((st / "ledger.json").read_text())["entries"]["t1"]
    assert e["state"] == "merged" and e["forced"] is True
