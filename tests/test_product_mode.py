"""Product mode (docs/PM-UX-RETHINK.md): explicit mode marker, the
server-owned decisions list, server-injected __MODE__, mode-authored rail and
views, gated keyboard/persisted navigation, and the no-native-dialogs rule.
Engineer targets keep a byte-identical /api/state shape (absent-unless-true)."""
import importlib
import re
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"
SHELL = ENGINE / "cc_shell.html"
LAUNCHER = ENGINE / "launcher_shell.html"


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    ap_home = tmp_path / ".autopilot"
    ap_state = ap_home / "state"
    ap_state.mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(ap_state))
    (ap_home / "config.yaml").write_text(
        'repo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n'
    )
    sys.path.insert(0, str(ENGINE))
    sys.modules.pop("command_center", None)
    mod = importlib.import_module("command_center")
    importlib.reload(mod)
    yield mod
    if str(ENGINE) in sys.path:
        sys.path.remove(str(ENGINE))


def _set_cfg(cc, text):
    (Path(cc.AP_HOME) / "config.yaml").write_text(text)


# ── is_product_workspace: marker first, heuristics as legacy fallback ──────

def test_product_mode_absent_on_engineer_workspace(cc):
    assert cc.is_product_workspace() is False
    st = cc.build_state()
    assert "product_mode" not in st
    assert "decisions" not in st


def test_mode_marker_product_wins_without_any_scaffold(cc):
    _set_cfg(cc, 'mode: product\nrepo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n')
    assert cc.is_product_workspace() is True
    st = cc.build_state()
    assert st.get("product_mode") is True
    assert st.get("decisions") == []


def test_mode_marker_engineering_wins_over_heuristics(cc):
    (cc.REPO / "insights").mkdir(exist_ok=True)
    _set_cfg(cc, 'mode: engineering\nrepo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n')
    assert cc.is_product_workspace() is False


def test_heuristics_survive_for_pre_marker_workspaces(cc):
    (cc.REPO / "insights").mkdir(exist_ok=True)
    assert cc.is_product_workspace() is True
    _set_cfg(cc, 'repo: "."\nbase_branch: "main"\ngates:\n  insight_lint: {cmd: "true"}\n')
    for d in (cc.REPO / "insights",):
        d.rmdir()
    assert cc.is_product_workspace() is True


# ── decisions: ONE server-owned list, every gate kind present ──────────────

def test_decisions_state_unions_every_gate_kind(cc):
    escalated = [{"id": "e1", "title": "Esc", "recommendation": "do X"}]
    done = [{"id": "d1", "title": "Ship", "merged": False, "rejected": False},
            {"id": "d2", "title": "Merged", "merged": True, "rejected": False}]
    prd = [{"run_id": "p1", "title": "PRD", "next_actions": ["approve-scope"]},
           {"run_id": "p2", "title": "Running", "next_actions": ["run-chain"]}]
    calibration = [{"id": "c1", "title": "Lesson"}]
    ds = cc.decisions_state(escalated, done, prd, calibration)
    assert [(d["source"], d["id"]) for d in ds] == [
        ("escalation", "e1"), ("review", "d1"), ("prd", "p1"), ("calibration", "c1")]
    assert [d["urgency"] for d in ds] == [0, 1, 1, 2]
    assert all({"id", "source", "kind", "title", "detail", "urgency"} <= set(d) for d in ds)


# ── shell: mode injected at serve time, rail authored per mode ─────────────

def test_shell_is_source_owned_per_mode():
    shell = SHELL.read_text()
    assert '__MODE__' in shell                       # server-injected mode
    assert "applyProductChrome" not in shell         # the rename layer is gone
    assert "data-eng=" not in shell and "data-pm=" not in shell
    for token in ("NAV_PM", "NAV_ENG", "renderNav", "'Today'", "'Decisions'",
                  "'Work'", "'Library'", "'Setup'"):
        assert token in shell, token
    for vid in ("view-work", "view-library"):
        assert f'id="{vid}"' in shell, vid


def test_shell_gates_keyboard_and_persisted_navigation():
    shell = SHELL.read_text()
    assert "function keyViews()" in shell
    assert "function viewAllowed(" in shell
    # digits map to the active mode's list, never a fixed 1-8 table
    assert "go(kv[i-1])" in shell
    # persisted restore is validated against the active mode
    assert "PM_VIEWS.includes(v)" in shell
    # diagnostics is an explicit disclosure, not a hidden route
    assert "diagGroup" in shell and "toggleDiag" in shell


def test_shell_has_no_native_dialogs():
    shell = SHELL.read_text()
    assert not re.search(r"(?<![a-zA-Z.])confirm\(", shell.replace("ccDlgConfirm", "").replace("ccConfirm", ""))
    assert not re.search(r"(?<![a-zA-Z.])prompt\(", shell)
    assert not re.search(r"(?<![a-zA-Z.])alert\(", shell)
    # Orbit dialog has real dialog semantics
    assert 'role="dialog"' in shell and 'aria-modal="true"' in shell
    assert "ccConfirm" in shell and "ccReason" in shell


def test_shell_renders_decisions_from_server_list():
    shell = SHELL.read_text()
    assert "S.decisions" in shell
    assert "decisionCard" in shell
    assert "renderDawn" in shell and "renderWork" in shell and "renderLibrary" in shell


def test_shell_accessibility_hooks():
    shell = SHELL.read_text()
    assert 'role="status"' in shell                  # toast announces
    assert 'id="a11yAnnounce"' in shell              # decision-count live region
    assert "pagehead" in shell                       # visible page headings
    assert "min-height:44px" in shell                # primary touch targets


# ── launcher: name-first create ────────────────────────────────────────────

def test_launcher_shell_is_name_first():
    html = LAUNCHER.read_text()
    assert 'id="kindProduct"' in html
    assert "checked" in html[html.find("kindProduct"): html.find("kindProduct") + 80]
    assert "Advanced" in html                        # path lives behind a disclosure
    assert "createPath" in html
    assert "name (or set a path" in html.lower() or "Give the workspace a name" in html


def test_launcher_derives_workspace_path_from_name(tmp_path, monkeypatch):
    sys.path.insert(0, str(ENGINE))
    try:
        import launcher
        monkeypatch.setattr(launcher.Path, "home", staticmethod(lambda: tmp_path))
        p = launcher._derive_workspace_path("Checkout Revamp!")
        assert p == tmp_path / "orbit-workspaces" / "checkout-revamp"
        # collision auto-suffixes instead of bouncing
        p.mkdir(parents=True)
        (p / "x").write_text("")
        assert launcher._derive_workspace_path("Checkout Revamp!") == \
            tmp_path / "orbit-workspaces" / "checkout-revamp-2"
        assert launcher._derive_workspace_path("!!!") is None
    finally:
        sys.path.remove(str(ENGINE))


def test_malformed_ledger_entries_never_crash_state(cc):
    """A hand-edited ledger with entries as a list (or any non-dict) must read
    as empty — build_state serving /api/state is the whole dashboard."""
    import json as _json
    cc.LEDGER.parent.mkdir(parents=True, exist_ok=True)
    for bad in ('{"entries": []}', '{"entries": "x"}', '{"entries": null}'):
        cc.LEDGER.write_text(bad)
        assert cc.load_ledger() == {}
        st = cc.build_state()   # must not raise
        assert st["counts"]["done"] == 0


def test_land_ship_merges_local_origin_branch(cc, tmp_path):
    """Local-origin workspaces have no git host: /land-ship must actually
    merge the ship branch into base, push base to the local origin, and
    record `merged` — the document goes live in the workspace."""
    import subprocess
    repo = Path(cc.REPO)

    def g(*a, cwd=repo):
        r = subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)
        assert r.returncode == 0, (a, r.stderr)
        return r.stdout.strip()

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "root")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    g("remote", "add", "origin", str(bare))
    g("push", "-q", "-u", "origin", "main")
    # ship branch with the artifact, pushed to origin only (not local refs)
    g("checkout", "-q", "-b", "orbit-product/task-t1")
    (repo / "insights").mkdir(exist_ok=True)
    (repo / "insights" / "t1.md").write_text("# doc\n")
    g("add", "insights/t1.md")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "insight: t1")
    g("push", "-q", "origin", "orbit-product/task-t1")
    g("checkout", "-q", "main")
    g("branch", "-D", "orbit-product/task-t1")
    assert not (repo / "insights" / "t1.md").exists()   # not on main yet
    import json as _json
    cc.LEDGER.parent.mkdir(parents=True, exist_ok=True)
    cc.LEDGER.write_text(_json.dumps({"entries": {"t1": {
        "state": "pushed", "remote_ref": "origin/orbit-product/task-t1"}}}))

    assert cc.is_local_origin() is True
    msg = cc.do_land_ship("t1")
    assert "Landed" in msg, msg
    assert (repo / "insights" / "t1.md").exists()          # doc live on main
    assert cc.load_ledger()["t1"]["state"] == "merged"     # recorded
    # base pushed back to origin so future worktrees include the landed work
    assert "insights/t1.md" in g("ls-tree", "-r", "--name-only", "origin/main")
