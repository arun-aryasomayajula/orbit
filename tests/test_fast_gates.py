# Slice-2 contracts (fast human gates): a reject/revert REQUIRES a reason (it
# is the calibration miner's ground truth); the review packet opens with a
# decide-fast header (scope, sensitive-surface flag, look-here-first, undo);
# the digest surfaces every stalled human gate; ledger `reverted` records the
# why without disturbing state-based categorization.
#
# Run: python3 -m pytest tests/test_fast_gates.py -v   (from orbit/)
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))


def _mod(name, monkeypatch=None):
    spec = importlib.util.spec_from_file_location(name, ENGINE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "backlog.yaml").write_text("tasks: []\n")
    (ap / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates: {t: {cmd: "true"}}\n')
    monkeypatch.setenv("AP_HOME", str(ap))
    monkeypatch.setenv("AP_STATE", str(ap / "state"))
    return _mod("command_center")


def test_reject_without_reason_is_refused(cc):
    msg = cc.do_mark("t1", "rejected", "")
    assert "reason is required" in msg
    assert not (Path(os.environ["AP_STATE"]) / "ledger.json").exists()


def test_reject_with_reason_lands_in_ledger(cc):
    st = Path(os.environ["AP_STATE"])
    (st / "ledger.json").write_text(json.dumps(          # a real shipped entry —
        {"entries": {"t1": {"state": "pushed", "sha": "abc123"}}}))  # rejecting nothing is now illegal
    cc.do_mark("t1", "rejected", "asserts on mock, not behaviour")
    e = json.loads((st / "ledger.json").read_text())["entries"]["t1"]
    assert e["state"] == "rejected" and e["review_note"] == "asserts on mock, not behaviour"


def test_mark_on_never_worked_task_is_refused(cc):
    msg = cc.do_mark("ghost", "merged", "")
    assert "illegal transition" in msg
    assert not (Path(os.environ["AP_STATE"]) / "ledger.json").exists()
    assert not (Path(os.environ["AP_STATE"]) / "merge_markers.jsonl").exists()


def test_rollback_without_reason_is_refused(cc, monkeypatch):
    st = Path(os.environ["AP_STATE"])
    (st / "ledger.json").write_text(json.dumps({"entries": {"t1": {"sha": "abc123"}}}))
    msg = cc.do_rollback("t1", "")
    assert "reason is required" in msg


def test_ledger_reverted_records_note_without_touching_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_STATE", str(tmp_path))
    env = dict(os.environ, AP_STATE=str(tmp_path))
    (tmp_path / "ledger.json").write_text(json.dumps(    # shipped, then merged —
        {"entries": {"t1": {"state": "pushed", "sha": "abc123"}}}))
    subprocess.run([sys.executable, str(ENGINE / "ledger.py"), "mark", "t1", "merged"],
                   env=env, check=True, capture_output=True)
    subprocess.run([sys.executable, str(ENGINE / "ledger.py"), "reverted", "t1", "broke prod search"],
                   env=env, check=True, capture_output=True)
    e = json.loads((tmp_path / "ledger.json").read_text())["entries"]["t1"]
    assert e["state"] == "merged"                      # categorization unchanged
    assert e["revert_note"] == "broke prod search" and e["reverted_at"]


def _ship_repo(tmp_path):
    """A tiny git repo with one commit touching a sensitive + a normal file."""
    repo = tmp_path / "wt"
    repo.mkdir()
    def g(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q")
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (repo / "app.py").write_text("x = 1\n" * 30)
    (repo / "auth_middleware.py").write_text("guard = True\n")
    g("add", "."); g("commit", "-qm", "feat: add auth guard")
    return repo


def test_review_packet_decision_header(tmp_path, monkeypatch):
    st = tmp_path / "state"
    st.mkdir()
    monkeypatch.setenv("AP_STATE", str(st))
    (st / "queue.json").write_text(json.dumps({"tasks": [
        {"id": "t1", "task": "add guard", "category": "feature", "priority": "high"}]}))
    (st / "ledger.json").write_text(json.dumps({"entries": {"t1": {"patch": "/p/x.patch"}}}))
    repo = _ship_repo(tmp_path)
    rp = _mod("review_packet")
    out = rp.build("t1", str(repo), "autopilot/task-t1", "main")
    text = out.read_text()
    assert "## Decide fast" in text
    assert "2 file(s)" in text and "category `feature`" in text
    assert "⚠" in text and "auth_middleware.py" in text     # sensitive surface flagged
    assert "Look here first" in text and "app.py" in text   # biggest hunk named
    assert "git revert" in text and "/p/x.patch" in text    # undo is one copy-paste away


def _plan_repo_setup(tmp_path):
    """A state dir with a queue/ledger for task t1 + a reviews/ dir ready for a
    planner-written plan file, plus a tiny shipped repo — the common fixture
    for the review-packet plan-section tests below."""
    st = tmp_path / "state"
    (st / "reviews").mkdir(parents=True)
    (st / "queue.json").write_text(json.dumps({"tasks": [
        {"id": "t1", "task": "add guard", "category": "feature", "priority": "high"}]}))
    (st / "ledger.json").write_text(json.dumps({"entries": {"t1": {"patch": "/p/x.patch"}}}))
    return st, _ship_repo(tmp_path)


def test_review_packet_includes_plan_section_when_present(tmp_path, monkeypatch):
    st, repo = _plan_repo_setup(tmp_path)
    monkeypatch.setenv("AP_STATE", str(st))
    plan_text = ("# Plan — task t1\ngenerated: 2026-07-22T00:00:00Z · category: feature\n\n"
                 "## Approach (justified)\n1. add the guard — because app.py:1 has none\n")
    (st / "reviews" / "task-t1-plan.md").write_text(plan_text)

    rp = _mod("review_packet")
    out = rp.build("t1", str(repo), "autopilot/task-t1", "main")
    text = out.read_text()

    assert "## The plan (scope fence + approach)" in text
    assert "## Approach (justified)" in text          # plan body reproduced verbatim
    assert "add the guard — because app.py:1 has none" in text
    assert "- [ ] Diff stayed inside the plan's file scope (see plan section)" in text
    # placement: between the contract section and the commit section
    assert (text.index("## The contract") < text.index("## The plan")
            < text.index("## The commit"))


def test_review_packet_includes_plan_gate_violations_when_present(tmp_path, monkeypatch):
    st, repo = _plan_repo_setup(tmp_path)
    monkeypatch.setenv("AP_STATE", str(st))
    (st / "reviews" / "task-t1-plan.md").write_text(
        "# Plan — task t1\n\n## Approach (justified)\n1. add the guard\n")
    (st / "reviews" / "task-t1-plan-gate.txt").write_text("secret.py\tout-of-scope\n")

    rp = _mod("review_packet")
    out = rp.build("t1", str(repo), "autopilot/task-t1", "main")
    text = out.read_text()

    assert "plan gate:" in text
    assert "secret.py" in text and "out-of-scope" in text
    # the gate content lives inside the plan section, not before it
    assert text.index("## The plan") < text.index("plan gate:") < text.index("## The commit")


def test_review_packet_omits_plan_section_when_absent(tmp_path, monkeypatch):
    st, repo = _plan_repo_setup(tmp_path)
    monkeypatch.setenv("AP_STATE", str(st))

    rp = _mod("review_packet")
    out = rp.build("t1", str(repo), "autopilot/task-t1", "main")
    text = out.read_text()

    assert "## The plan" not in text
    assert "plan gate:" not in text
    assert "Diff stayed inside the plan's file scope" not in text
    # byte-identical shape to today: contract section is immediately followed
    # by the commit section, nothing spliced in between
    between = text[text.index("## The contract"):text.index("## The commit")]
    assert "## The plan" not in between


def test_digest_surfaces_every_stalled_gate(tmp_path):
    ap = tmp_path / "repo" / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates: {t: {cmd: "true"}}\n')
    st = ap / "state"
    (st / "ledger.json").write_text(json.dumps({"entries": {
        "ship-1": {"state": "pushed", "updated_at": "2026-07-01T00:00:00Z",
                   "pr_url": "https://github.com/x/r/pull/3"},
        "esc-1": {"state": "escalated", "updated_at": "2026-07-10T00:00:00Z",
                  "reason": "needs schema decision"}}}))
    (st / "queue.json").write_text(json.dumps({"tasks": [{"id": "q1"}, {"id": "ship-1"}]}))
    (st / "jira_map.json").write_text(json.dumps({"CDP-9": {"state": "needs_info"}}))
    dg = _mod("digest")
    title, body, dash = dg.build(tmp_path / "repo")
    assert "1 ship(s) awaiting" in body and "pull/3" in body
    assert "1 escalation(s)" in body and "needs schema decision" in body
    assert "CDP-9" in body
    assert "1 pickable / 2 emitted" in body                 # ship-1 is worked → not pickable
    assert dash.startswith("http")


def test_digest_all_clear(tmp_path):
    ap = tmp_path / "repo" / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates: {t: {cmd: "true"}}\n')
    dg = _mod("digest")
    _, body, _ = dg.build(tmp_path / "repo")
    assert "no human gate is blocking" in body
