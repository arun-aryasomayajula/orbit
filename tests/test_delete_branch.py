# Guardrails: only autopilot/* deletable; bulk only touches server-classified
# merged/rejected; awaiting/orphan never deleted in bulk. git push is faked.
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


class _FakeGit:
    def __init__(self): self.deleted = []
    def __call__(self, *args, **kw):
        import types
        if args[:2] == ("push", "origin") and "--delete" in args:
            self.deleted.append(args[-1])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def test_refuses_non_autopilot(tmp_path, monkeypatch):
    cc = _cc(tmp_path, monkeypatch)
    fg = _FakeGit(); monkeypatch.setattr(cc, "_git", fg)
    msg = cc.do_delete_branch("main")
    assert "refused" in msg.lower()
    assert fg.deleted == []


def test_single_delete_autopilot(tmp_path, monkeypatch):
    cc = _cc(tmp_path, monkeypatch)
    fg = _FakeGit(); monkeypatch.setattr(cc, "_git", fg)
    msg = cc.do_delete_branch("autopilot/task-x")
    assert "deleted" in msg.lower()
    assert fg.deleted == ["autopilot/task-x"]


def test_bulk_merged_only_touches_merged(tmp_path, monkeypatch):
    cc = _cc(tmp_path, monkeypatch)
    fg = _FakeGit(); monkeypatch.setattr(cc, "_git", fg)
    monkeypatch.setattr(cc, "remote_branches", lambda: [
        ("autopilot/task-m", "sha_m", 1_000_000),   # merged (in ancestry)
        ("autopilot/task-a", "sha_a", 1_000_000),   # awaiting
    ])
    monkeypatch.setattr(cc, "trunk_ancestry", lambda: {"sha_m"})
    monkeypatch.setattr(cc, "load_ledger", lambda: {
        "a": {"state": "pushed", "sha": "sha_a", "remote_ref": "origin/autopilot/task-a"},
    })
    msg = cc.do_delete_branches_bulk("merged")
    assert fg.deleted == ["autopilot/task-m"]   # awaiting NOT deleted
    assert "1" in msg
