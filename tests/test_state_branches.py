# build_state must expose a "branches" list built from the reconciler, decorated
# with has_packet + pr_url. remote_branches/trunk_ancestry are monkeypatched so
# the test needs no git.
import importlib, sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"


def _cc(tmp_path, monkeypatch):
    ap_home = tmp_path / ".autopilot"
    state = ap_home / "state"
    (state / "reviews").mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(state))
    monkeypatch.setenv("ORBIT_BASE_BRANCH", "feature/trunk")
    monkeypatch.setenv("ORBIT_BB_REPO", "team/repo")
    (ap_home / "backlog.yaml").write_text("version: 1\ntasks: []\n")
    (state / "ledger.json").write_text(
        '{"entries": {"await": {"state": "pushed", "sha": "s1",'
        ' "remote_ref": "origin/autopilot/task-await"}}}')
    (state / "reviews" / "task-await.md").write_text("packet")
    sys.path.insert(0, str(ENGINE))
    sys.modules.pop("command_center", None)
    mod = importlib.import_module("command_center")
    importlib.reload(mod)
    sys.path.remove(str(ENGINE))
    return mod


def test_state_has_branches(tmp_path, monkeypatch):
    cc = _cc(tmp_path, monkeypatch)
    monkeypatch.setattr(cc, "remote_branches",
                        lambda: [("autopilot/task-await", "s1", 1_000_000)])
    monkeypatch.setattr(cc, "trunk_ancestry", lambda: set())
    st = cc.build_state()
    assert "branches" in st
    b = next(x for x in st["branches"] if x["branch"] == "autopilot/task-await")
    assert b["category"] == "awaiting"
    assert b["has_packet"] is True
    assert b["pr_url"] and "pull-requests/new" in b["pr_url"]
