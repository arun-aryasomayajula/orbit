# Pure-function tests for branch_reconcile: category assignment, merge detection,
# orphan/current-ref logic, age. No git, no network — inputs are injected.
import importlib, sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"


def _cc(tmp_path, monkeypatch):
    ap_home = tmp_path / ".autopilot"
    (ap_home / "state").mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(ap_home / "state"))
    monkeypatch.setenv("ORBIT_BASE_BRANCH", "feature/trunk")
    sys.path.insert(0, str(ENGINE))
    sys.modules.pop("command_center", None)
    mod = importlib.import_module("command_center")
    importlib.reload(mod)
    sys.path.remove(str(ENGINE))
    return mod


def test_categories(tmp_path, monkeypatch):
    cc = _cc(tmp_path, monkeypatch)
    NOW = 1_000_000
    DAY = 86400
    branches = [
        ("autopilot/task-await", "sha_await", NOW - 2 * DAY),   # pushed, not merged
        ("autopilot/task-done",  "sha_done",  NOW - 5 * DAY),   # tip in ancestry
        ("autopilot/task-rej",   "sha_rej",   NOW - 1 * DAY),   # rejected in ledger
        ("autopilot/task-orph",  "sha_orph",  NOW - 9 * DAY),   # no ledger entry
        ("autopilot/task-dup-20260101T010101", "sha_dup", NOW), # dup: ledger points elsewhere
    ]
    ancestry = {"sha_done"}
    ledger = {
        "task-await": {"state": "pushed",   "sha": "sha_await", "remote_ref": "origin/autopilot/task-await"},
        "task-done":  {"state": "pushed",   "sha": "sha_done",  "remote_ref": "origin/autopilot/task-done"},
        "task-rej":   {"state": "rejected", "sha": "sha_rej",   "remote_ref": "origin/autopilot/task-rej"},
        "task-dup":   {"state": "pushed",   "sha": "sha_new",   "remote_ref": "origin/autopilot/task-dup"},
    }
    rows = {r["branch"]: r for r in cc.branch_reconcile(branches, ancestry, ledger, NOW)}

    assert rows["autopilot/task-await"]["category"] == "awaiting"
    assert rows["autopilot/task-await"]["age_days"] == 2

    assert rows["autopilot/task-done"]["category"] == "merged"    # ancestry beats pushed state
    assert rows["autopilot/task-done"]["merged"] is True

    assert rows["autopilot/task-rej"]["category"] == "rejected"

    assert rows["autopilot/task-orph"]["category"] == "orphan"
    assert rows["autopilot/task-orph"]["is_orphan"] is True

    dup = rows["autopilot/task-dup-20260101T010101"]
    assert dup["task_id"] == "dup"           # timestamp suffix stripped
    assert dup["is_current_ref"] is False     # ledger's current ref is the clean branch
    assert dup["category"] == "orphan"        # superseded branch → orphan bucket


def test_marked_merged_without_ancestry(tmp_path, monkeypatch):
    cc = _cc(tmp_path, monkeypatch)
    branches = [("autopilot/task-m", "sha_m", 1_000_000)]
    ledger = {"task-m": {"state": "merged", "sha": "sha_m", "remote_ref": "origin/autopilot/task-m"}}
    rows = cc.branch_reconcile(branches, set(), ledger, 1_000_000)
    assert rows[0]["category"] == "merged"
    assert rows[0]["merged"] is True
