# Auto-close reconcile: a task whose recorded commit is an ancestor of the base
# branch has shipped, so it must be dropped from the backlog and flipped to
# 'merged' in the ledger — never re-escalated. Tasks with no ship, a non-ancestor
# ship, or a human 'rejected' verdict must be left untouched.
#
# Run: python3 -m pytest tests/test_autoclose.py -v   (from orbit/)
import importlib.util
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"
_spec = importlib.util.spec_from_file_location("autoclose", ENGINE / "autoclose.py")
autoclose = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autoclose)

NOW = "2026-07-13T00:00:00Z"


def _tasks(*ids):
    return [{"id": i, "title": i, "status": "queued"} for i in ids]


def test_closes_task_whose_sha_is_ancestor():
    tasks = _tasks("shipped", "open")
    entries = {"shipped": {"state": "escalated", "sha": "abc1234"}}
    # only 'abc1234' is an ancestor of base
    closed, kept, out = autoclose.reconcile(tasks, entries, lambda t: t == "abc1234", NOW)
    assert closed == ["shipped"]
    assert [t["id"] for t in kept] == ["open"]           # shipped removed from backlog
    assert out["shipped"]["state"] == "merged"           # ledger reflects truth
    assert out["shipped"]["auto_closed"] is True
    assert out["shipped"]["sha"] == "abc1234"            # sha preserved


def test_keeps_task_with_no_ledger_entry():
    tasks = _tasks("neverworked")
    closed, kept, out = autoclose.reconcile(tasks, {}, lambda t: True, NOW)
    assert closed == []
    assert [t["id"] for t in kept] == ["neverworked"]


def test_keeps_task_whose_sha_is_not_ancestor():
    tasks = _tasks("inflight")
    entries = {"inflight": {"state": "pushed", "sha": "deadbeef"}}
    closed, kept, out = autoclose.reconcile(tasks, entries, lambda t: False, NOW)
    assert closed == []
    assert [t["id"] for t in kept] == ["inflight"]
    assert out["inflight"]["state"] == "pushed"          # untouched


def test_never_autocloses_a_rejected_ship():
    tasks = _tasks("rejected_one")
    entries = {"rejected_one": {"state": "rejected", "sha": "abc1234"}}
    # even though the sha is an ancestor, a human rejection is never auto-closed
    closed, kept, out = autoclose.reconcile(tasks, entries, lambda t: True, NOW)
    assert closed == []
    assert [t["id"] for t in kept] == ["rejected_one"]
    assert out["rejected_one"]["state"] == "rejected"


def test_falls_back_to_remote_ref_when_no_sha():
    tasks = _tasks("byref")
    entries = {"byref": {"state": "pushed", "remote_ref": "origin/autopilot/task-byref"}}
    seen = {}
    def anc(t):
        seen["target"] = t
        return True
    closed, kept, out = autoclose.reconcile(tasks, entries, anc, NOW)
    assert closed == ["byref"]
    assert seen["target"] == "origin/autopilot/task-byref"   # ref used as the ancestry target
