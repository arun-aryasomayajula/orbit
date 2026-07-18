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
    cc.do_mark("t1", "rejected", "asserts on mock, not behaviour")
    led = json.loads((Path(os.environ["AP_STATE"]) / "ledger.json").read_text())
    e = led["entries"]["t1"]
    assert e["state"] == "rejected" and e["review_note"] == "asserts on mock, not behaviour"


def test_rollback_without_reason_is_refused(cc, monkeypatch):
    st = Path(os.environ["AP_STATE"])
    (st / "ledger.json").write_text(json.dumps({"entries": {"t1": {"sha": "abc123"}}}))
    msg = cc.do_rollback("t1", "")
    assert "reason is required" in msg


def test_ledger_reverted_records_note_without_touching_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_STATE", str(tmp_path))
    env = dict(os.environ, AP_STATE=str(tmp_path))
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
