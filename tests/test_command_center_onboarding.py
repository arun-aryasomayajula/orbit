# Per-project onboarding UI on the dashboard: run intake / build the
# knowledge base from the dashboard (POST /intake-run, /knowledge-run +
# their detached-Popen spawn, mirroring do_epic_action/do_qa_action), the
# confined live-log tail (GET /api/cmdlog), the confined knowledge-doc
# browser (GET /api/knowledge-doc), the extended config editor (gates +
# qa/plan/knowledge, validated by reusing engine/config.py's own
# `validate` subcommand instead of hand-rolling the schema), and
# build_state()'s new `onboarding` key.
#
# Run: python3.11 -m pytest tests/test_command_center_onboarding.py -v   (from orbit/)
import importlib.util
import json
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
    monkeypatch.setenv("AP_HOME", str(ap))
    monkeypatch.setenv("AP_STATE", str(ap / "state"))
    return _mod("command_center")


# ── POST /intake-run → do_intake_run ──────────────────────────────────────────

def test_do_intake_run_spawns_detached_popen_with_right_argv(cc, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append((argv, kw))

    monkeypatch.setattr(cc.subprocess, "Popen", FakePopen)
    msg = cc.do_intake_run()

    assert "Intake started" in msg
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv == [sys.executable, str(cc.ENGINE / "intake.py"), str(cc.REPO)]
    assert kw.get("start_new_session") is True
    assert kw.get("stdin") is cc.subprocess.DEVNULL
    assert (cc.LOGDIR / "intake.log").exists()


def test_do_intake_run_truncates_the_log_on_each_run(cc, monkeypatch):
    # Fixed filename shared across re-runs — "wb" (truncate), not "ab", so a
    # re-run's tail never mixes with a stale previous run's output.
    cc.LOGDIR.mkdir(parents=True, exist_ok=True)
    (cc.LOGDIR / "intake.log").write_text("stale output from a previous run\n" * 50)
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: None)
    cc.do_intake_run()
    assert (cc.LOGDIR / "intake.log").read_text() == ""


# ── POST /knowledge-run → do_knowledge_run ────────────────────────────────────

def test_do_knowledge_run_build_spawns_detached_popen_with_right_argv(cc, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append((argv, kw))

    monkeypatch.setattr(cc.subprocess, "Popen", FakePopen)
    msg = cc.do_knowledge_run("build")

    assert "knowledge base" in msg.lower()
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv == [sys.executable, str(cc.ENGINE / "knowledge.py"), str(cc.REPO), "build"]
    assert kw.get("start_new_session") is True
    assert (cc.LOGDIR / "knowledge.log").exists()


def test_do_knowledge_run_refresh_spawns_detached_popen_with_right_argv(cc, monkeypatch):
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    msg = cc.do_knowledge_run("refresh")

    assert "refresh" in msg.lower()
    assert calls[0][0] == [sys.executable, str(cc.ENGINE / "knowledge.py"), str(cc.REPO), "refresh"]


def test_do_knowledge_run_defaults_to_build_when_sub_blank(cc, monkeypatch):
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    cc.do_knowledge_run("")
    assert calls[0][0][-1] == "build"


def test_do_knowledge_run_rejects_bad_sub_and_never_spawns(cc, monkeypatch):
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    msg = cc.do_knowledge_run("delete-everything")

    assert "invalid" in msg.lower()
    assert calls == []
    assert not (cc.LOGDIR / "knowledge.log").exists()


# ── GET /api/cmdlog → read_cmdlog ─────────────────────────────────────────────

def test_read_cmdlog_returns_tail_for_allowlisted_slug(cc):
    cc.LOGDIR.mkdir(parents=True, exist_ok=True)
    (cc.LOGDIR / "intake.log").write_text("line1\nline2\nline3\n")
    assert cc.read_cmdlog("intake") == "line1\nline2\nline3"


def test_read_cmdlog_returns_empty_string_when_log_absent_never_500(cc):
    assert cc.read_cmdlog("intake") == ""
    assert cc.read_cmdlog("knowledge") == ""


def test_read_cmdlog_rejects_non_allowlisted_name(cc):
    # A log for some OTHER name (even one that exists on disk, even one
    # shaped like a traversal) must never be served through this route.
    cc.LOGDIR.mkdir(parents=True, exist_ok=True)
    (cc.LOGDIR / "orbit.log").write_text("the loop's own live log\n")
    assert cc.read_cmdlog("orbit") == ""
    assert cc.read_cmdlog("../../etc/passwd") == ""
    assert cc.read_cmdlog("") == ""


def test_cmdlog_allowlist_is_the_fixed_two_item_set(cc):
    assert cc._CMDLOG_ALLOW == {"intake", "knowledge"}


def test_read_cmdlog_tails_to_max_lines(cc):
    cc.LOGDIR.mkdir(parents=True, exist_ok=True)
    (cc.LOGDIR / "intake.log").write_text("\n".join(f"l{i}" for i in range(500)) + "\n")
    out = cc.read_cmdlog("intake").splitlines()
    assert len(out) == 400
    assert out[0] == "l100"
    assert out[-1] == "l499"


# ── GET /api/knowledge-doc → read_knowledge_doc ───────────────────────────────

def test_read_knowledge_doc_serves_allowlisted_doc(cc):
    kdir = cc.AP_HOME / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "INDEX.md").write_text("# Index\nsome content\n")
    assert cc.read_knowledge_doc("INDEX.md") == "# Index\nsome content\n"


def test_read_knowledge_doc_rejects_traversal(cc):
    kdir = cc.AP_HOME / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "INDEX.md").write_text("x")
    assert cc.read_knowledge_doc("../config.yaml") is None
    assert cc.read_knowledge_doc("../../etc/passwd") is None
    assert cc.read_knowledge_doc("sub/../../escape.md") is None


def test_read_knowledge_doc_rejects_absolute_path(cc):
    kdir = cc.AP_HOME / "knowledge"
    kdir.mkdir(parents=True)
    assert cc.read_knowledge_doc("/etc/passwd") is None


def test_read_knowledge_doc_rejects_unknown_name(cc):
    # Shape-valid but not actually present — must not fall through to a read.
    kdir = cc.AP_HOME / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "INDEX.md").write_text("x")
    assert cc.read_knowledge_doc("ghost.md") is None


def test_read_knowledge_doc_rejects_when_knowledge_dir_missing(cc):
    assert cc.read_knowledge_doc("INDEX.md") is None


def test_read_knowledge_doc_rejects_symlink_escape(cc, tmp_path):
    kdir = cc.AP_HOME / "knowledge"
    kdir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("secret content")
    (kdir / "escape.md").symlink_to(outside)
    # escape.md IS a shape-valid, currently-listed file (is_file() follows
    # the symlink) — the resolve()+startswith confinement check must still
    # catch it, the same way _qa_resolve does for QA evidence.
    assert cc.read_knowledge_doc("escape.md") is None


# ── extended config editing: gates + qa/plan/knowledge via config.py validate ─

def _base_gates():
    return {"gates": {"test": {"cmd": "echo hi"}}}


def test_do_save_config_accepts_valid_full_config_with_qa_plan_knowledge(cc):
    payload = {
        **_base_gates(),
        "qa": {"base_url": "http://localhost:3000"},
        "plan": {"enforce": "verifier"},
        "knowledge": {"refresh": "manual"},
    }
    msg = cc.do_save_config(json.dumps(payload))
    assert msg.startswith("✓ Saved")   # ✓
    saved = cc._load_cfg()
    assert saved["gates"] == payload["gates"]
    assert saved["qa"] == payload["qa"]
    assert saved["plan"] == payload["plan"]
    assert saved["knowledge"] == payload["knowledge"]


def test_do_save_config_rejects_bad_plan_enforce_surfacing_config_py_message(cc):
    # gates present so the ONLY config.py validate() failure is plan.enforce
    # — isolates the assertion to config.py's OWN error text (the point of
    # reusing validate() instead of re-deriving the schema here).
    payload = {**_base_gates(), "plan": {"enforce": "bogus"}}
    msg = cc.do_save_config(json.dumps(payload))
    assert msg.startswith("Rejected")
    assert 'plan.enforce must be "verifier" or "block"' in msg
    assert "plan" not in cc._load_cfg()   # rejected write never landed


def test_do_save_config_rejects_bad_qa_flows_dir_surfacing_config_py_message(cc):
    payload = {**_base_gates(), "qa": {"flows_dir": "/absolute/not/allowed"}}
    msg = cc.do_save_config(json.dumps(payload))
    assert msg.startswith("Rejected")
    assert "qa.flows_dir" in msg


def test_do_save_config_rejects_missing_gates_via_config_py(cc):
    # No _validate_config shape error (gates absent is fine at that shallow
    # layer) — config.py's validate() is what actually requires gates.
    msg = cc.do_save_config(json.dumps({"base_branch": "main"}))
    assert msg.startswith("Rejected")
    assert "gates" in msg.lower()
    assert not (cc.AP_HOME / "config.yaml").exists()   # never existed -> rollback removes it


def test_do_save_config_rolls_back_to_prior_bytes_on_validate_failure(cc):
    ok_msg = cc.do_save_config(json.dumps(_base_gates()))
    assert ok_msg.startswith("✓ Saved")
    before = (cc.AP_HOME / "config.yaml").read_text()

    bad_msg = cc.do_save_config(json.dumps({"plan": {"enforce": "bogus"}}))
    assert bad_msg.startswith("Rejected")
    assert (cc.AP_HOME / "config.yaml").read_text() == before


def test_do_save_config_rollback_restore_is_atomic_temp_then_rename(cc, monkeypatch):
    # The rollback restore must use the same temp+rename pattern as a normal
    # save — not a bare write_text — so a crash mid-restore can never
    # truncate config.yaml (the exact failure mode the rollback exists to
    # prevent in the first place).
    ok_msg = cc.do_save_config(json.dumps(_base_gates()))
    assert ok_msg.startswith("✓ Saved")

    real_replace = cc.Path.replace
    calls = []

    def spy_replace(self, target):
        if self.name == "config.yaml.tmp":
            calls.append((str(self), str(target)))
        return real_replace(self, target)

    monkeypatch.setattr(cc.Path, "replace", spy_replace)
    bad_msg = cc.do_save_config(json.dumps({"plan": {"enforce": "bogus"}}))
    assert bad_msg.startswith("Rejected")

    # One replace for the (invalid) merged write, one for the rollback
    # restore — both via config.yaml.tmp -> config.yaml, never a direct
    # write_text onto the live file.
    assert len(calls) == 2
    for src, dst in calls:
        assert src.endswith("config.yaml.tmp")
        assert dst.endswith("config.yaml")
    assert (cc.AP_HOME / "config.yaml.tmp").exists() is False


def test_do_save_config_still_rejects_shallow_shape_errors_before_touching_disk(cc):
    # _validate_config's cheap pre-filter (no config.py subprocess involved).
    msg = cc.do_save_config(json.dumps({"interval_seconds": -5}))
    assert msg.startswith("Rejected")
    assert not (cc.AP_HOME / "config.yaml").exists()


# ── build_state()'s `onboarding` key ──────────────────────────────────────────

def test_build_state_onboarding_is_empty_shape_without_any_onboarding_story(cc):
    st = cc.build_state()
    assert "qa_flows" not in st   # unrelated key stays absent, as before
    ob = st["onboarding"]
    assert ob == {
        "has_knowledge": False,
        "knowledge_docs": [],
        "intake_running": False,
        "knowledge_running": False,
        "gates_configured": False,
        "qa_configured": False,
        "plan_configured": False,
    }


def test_build_state_onboarding_reflects_knowledge_dir_and_config_blocks(cc):
    kdir = cc.AP_HOME / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "INDEX.md").write_text("# index\n")
    (kdir / "er.mmd").write_text("erDiagram\n")
    (cc.AP_HOME / "config.yaml").write_text(
        "gates:\n  test:\n    cmd: echo hi\n"
        "qa:\n  base_url: http://x\n"
        "plan:\n  enforce: verifier\n")

    st = cc.build_state()
    ob = st["onboarding"]
    assert ob["has_knowledge"] is True
    assert set(ob["knowledge_docs"]) == {"INDEX.md", "er.mmd"}
    assert ob["gates_configured"] is True
    assert ob["qa_configured"] is True
    assert ob["plan_configured"] is True


def test_build_state_onboarding_intake_running_true_before_completion_marker(cc):
    cc.LOGDIR.mkdir(parents=True, exist_ok=True)
    (cc.LOGDIR / "intake.log").write_text("ORBIT INTAKE\n  ✓ gate test: PASS\n")
    assert cc.build_state()["onboarding"]["intake_running"] is True


def test_build_state_onboarding_intake_running_false_after_completion_marker(cc):
    cc.LOGDIR.mkdir(parents=True, exist_ok=True)
    (cc.LOGDIR / "intake.log").write_text(
        "ORBIT INTAKE\n\n  proposals: 0 | merged: 0 | skipped: 0\n\n"
        "  next: triage the proposals on the dashboard (promote what's real), "
        "review .autopilot/tracks/, then `orbit run`.\n")
    assert cc.build_state()["onboarding"]["intake_running"] is False


def test_build_state_onboarding_knowledge_running_false_after_success_marker(cc):
    cc.LOGDIR.mkdir(parents=True, exist_ok=True)
    (cc.LOGDIR / "knowledge.log").write_text(
        "ORBIT KNOWLEDGE BUILD\n  ✓ knowledge build complete — 3 docs written to .../knowledge\n")
    assert cc.build_state()["onboarding"]["knowledge_running"] is False


def test_build_state_onboarding_knowledge_running_true_while_no_marker_and_fresh(cc):
    cc.LOGDIR.mkdir(parents=True, exist_ok=True)
    (cc.LOGDIR / "knowledge.log").write_text("ORBIT KNOWLEDGE BUILD\nsurveying...\n")
    assert cc.build_state()["onboarding"]["knowledge_running"] is True
