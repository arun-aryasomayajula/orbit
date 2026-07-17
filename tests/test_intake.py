# Intake's deterministic half: gates are executed and reported; a repo with NO
# gates gets the test-bootstrap proposal; proposals merge into backlog.yaml as
# proposed/human only, idempotently, and invalid proposals are rejected — a
# machine may suggest work, never queue it.
#
# Run: python3 -m pytest tests/test_intake.py -v   (from orbit/)
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

_spec = importlib.util.spec_from_file_location("backlog_append", ENGINE / "backlog_append.py")
ba = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ba)


def _target(tmp_path, gates="gates: {}\n"):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text('repo: "."\nbase_branch: "main"\n' + gates)
    (ap / "backlog.yaml").write_text("tasks: []\n")
    return tmp_path


def _intake(target, *flags):
    return subprocess.run([sys.executable, str(ENGINE / "intake.py"), str(target),
                           "--no-agent", *flags], capture_output=True, text=True,
                          env=dict(os.environ))


def _tasks(target):
    return (yaml.safe_load((target / ".autopilot" / "backlog.yaml").read_text()) or {}).get("tasks") or []


def test_no_gates_injects_bootstrap_proposal(tmp_path):
    t = _target(tmp_path)
    r = _intake(t)
    assert r.returncode == 0, r.stderr
    tasks = _tasks(t)
    assert [x["id"] for x in tasks] == ["bootstrap-test-gate"]
    assert tasks[0]["status"] == "proposed"          # never queued by a machine
    assert tasks[0]["autopilot"] == "human"
    assert tasks[0]["category"] == "testing"
    assert len(tasks[0]["acceptance_criteria"]) >= 2


def test_intake_is_idempotent(tmp_path):
    t = _target(tmp_path)
    _intake(t)
    r2 = _intake(t)
    assert "already in backlog" in r2.stdout
    assert len(_tasks(t)) == 1


def test_gates_are_executed_and_reported(tmp_path):
    t = _target(tmp_path, 'gates:\n  ok: {cmd: "true", cwd: "."}\n  broken: {cmd: "false", cwd: "."}\n')
    r = _intake(t)
    assert "gate ok: PASS" in r.stdout
    assert "gate broken: FAIL" in r.stdout
    report = (t / ".autopilot" / "state" / "intake" / "gates-report.txt").read_text()
    assert "== gate ok: PASS ==" in report
    assert _tasks(t) == []                            # gates exist → no bootstrap task


def test_dry_run_does_not_write(tmp_path):
    t = _target(tmp_path)
    _intake(t, "--dry-run")
    assert _tasks(t) == []


def test_append_rejects_invalid_proposals(tmp_path):
    bl = tmp_path / "backlog.yaml"
    bl.write_text("tasks: []\n")
    added, skipped = ba.append_tasks(bl, [
        {"id": "Bad Id!", "title": "x", "category": "bug"},
        {"id": "no-title", "title": "", "category": "bug"},
        {"id": "bad-cat", "title": "t", "category": "nonsense"},
        {"id": "good-one", "title": "a real lead", "category": "bug",
         "evidence": "src/x.py:12 TODO", "acceptance_criteria": ["a", "b"]},
    ], "intake", "test header")
    assert added == ["good-one"]
    assert len(skipped) == 3
    tasks = (yaml.safe_load(bl.read_text()) or {})["tasks"]
    assert tasks[0]["id"] == "good-one"
    assert "EVIDENCE:" in tasks[0]["context"]
    assert tasks[0]["source"] == "intake"


def test_append_never_honors_status_or_autopilot_from_proposal(tmp_path):
    bl = tmp_path / "backlog.yaml"
    bl.write_text("tasks: []\n")
    ba.append_tasks(bl, [{"id": "sneaky", "title": "t", "category": "bug",
                          "status": "queued", "autopilot": "allow"}], "intake", "h")
    t = (yaml.safe_load(bl.read_text()) or {})["tasks"][0]
    assert t["status"] == "proposed"
    assert t["autopilot"] == "human"
