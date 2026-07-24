# Task 23 — dashboard QA view: build_state's `qa_flows` key (the taught-flow
# library + recent run verdicts + pending heal candidates) and POST
# /qa-action's `do_qa_action` (a validated, detached spawn of `qa_run.py
# --flow <name>`, plus Task 26's heal-approve/heal-reject — the human gate
# that closes the self-healing loop, mirroring do_calibration_action).
#
# Run: python3.11 -m pytest tests/test_command_center_qa_view.py -v   (from orbit/)
import importlib.util
import json
import os
import sys
import time
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


def _write_flow(cc, name, intent="do the thing", entry_path="http://x/y",
                last_verdict="pass", last_run_at="2026-07-20T00:00:00Z", runs=3,
                flows_dir="qa/flows"):
    fd = cc.AP_HOME / flows_dir / name
    fd.mkdir(parents=True)
    (fd / "intent.md").write_text(
        "---\n"
        f"name: {name}\n"
        "created: '2026-07-01T00:00:00Z'\n"
        f"entry_path: {entry_path}\n"
        "tags: []\n"
        "---\n"
        "\n"
        f"{intent}\n"
        "\n"
        "## Success criteria\n"
        "- it works\n")
    meta = {"created_at": "2026-07-01T00:00:00Z", "last_run_at": last_run_at,
             "last_verdict": last_verdict, "runs": runs, "heals": []}
    (fd / "meta.json").write_text(json.dumps(meta))
    return fd


def _write_run(cc, filename, mtime_offset=0, **fields):
    d = cc.QA_DIR / "runs"
    d.mkdir(parents=True, exist_ok=True)
    body = {"flow": "login", "verdict": "pass", "failed_step": None,
            "screenshot": None, "summary": "ok", "healed_candidate": None}
    body.update(fields)
    p = d / filename
    p.write_text(json.dumps(body))
    now = time.time()
    os.utime(p, (now + mtime_offset, now + mtime_offset))
    return p


def _write_heal(cc, filename, **fields):
    """A pending heal candidate — $AP_STATE/qa/heal/<flow>--<ts>.json shape
    (skills/orbit-qa-flow.md), directly under heal/ (not approved/rejected)."""
    d = cc.QA_DIR / "heal"
    d.mkdir(parents=True, exist_ok=True)
    body = {"flow": "login", "reason": "button relabeled", "step_diff": "old -> new",
            "new_steps": [{"action": "click", "selector": "#go", "value": "", "note": "renamed"}],
            "screenshot": "flow-login/01.png", "mined_at": "2026-07-21T00:00:00Z"}
    body.update(fields)
    p = d / filename
    p.write_text(json.dumps(body))
    return p


# ── build_state()'s qa_flows key ──────────────────────────────────────────────

def test_qa_flows_absent_without_a_flows_dir(cc):
    st = cc.build_state()
    assert "qa_flows" not in st


def test_qa_flows_absent_when_flows_dir_exists_but_is_empty(cc):
    (cc.AP_HOME / "qa" / "flows").mkdir(parents=True)
    st = cc.build_state()
    assert "qa_flows" not in st


def test_qa_flows_shape_with_two_flows_a_run_and_a_heal_candidate(cc):
    _write_flow(cc, "login", intent="log a user in", entry_path="http://x/login",
                last_verdict="pass", last_run_at="2026-07-20T00:00:00Z", runs=3)
    _write_flow(cc, "checkout", intent="complete checkout", entry_path="http://x/checkout",
                last_verdict="fail", last_run_at="2026-07-21T00:00:00Z", runs=1)
    _write_run(cc, "20260720T000000Z-login.json", mtime_offset=-100,
               flow="login", verdict="pass", summary="ok")
    _write_run(cc, "20260721T000000Z-checkout.json", mtime_offset=-50,
               flow="checkout", verdict="fail", failed_step=2,
               screenshot="flow-checkout/01.png", summary="broke")
    heal_dir = cc.QA_DIR / "heal"
    heal_dir.mkdir(parents=True)
    (heal_dir / "checkout--20260721T000000Z.json").write_text(json.dumps({"flow": "checkout"}))

    st = cc.build_state()
    qf = st["qa_flows"]

    assert [f["name"] for f in qf["flows"]] == ["checkout", "login"]  # sorted by name
    login = next(f for f in qf["flows"] if f["name"] == "login")
    assert login == {"name": "login", "intent_line": "log a user in",
                      "entry_path": "http://x/login", "last_verdict": "pass",
                      "last_run_at": "2026-07-20T00:00:00Z", "runs": 3}

    assert [r["flow"] for r in qf["recent_runs"]] == ["checkout", "login"]  # newest mtime first
    assert qf["recent_runs"][0]["file"] == "20260721T000000Z-checkout.json"
    assert qf["recent_runs"][0]["failed_step"] == 2

    assert qf["pending_heals"] == 1
    assert qf["heals"] == [{"file": "checkout--20260721T000000Z.json", "flow": "checkout",
                            "reason": None, "step_diff": None, "screenshot": None, "mined_at": None}]


def test_qa_flows_malformed_meta_json_yields_nulls_never_a_crash(cc):
    fd = cc.AP_HOME / "qa" / "flows" / "broken"
    fd.mkdir(parents=True)
    (fd / "intent.md").write_text("not even frontmatter\njust some text\n")
    (fd / "meta.json").write_text("{not valid json")
    st = cc.build_state()   # must not raise
    row = st["qa_flows"]["flows"][0]
    assert row == {"name": "broken", "intent_line": None, "entry_path": None,
                   "last_verdict": None, "last_run_at": None, "runs": None}


def test_qa_flows_missing_intent_and_meta_files_yield_nulls_never_a_crash(cc):
    fd = cc.AP_HOME / "qa" / "flows" / "bare"
    fd.mkdir(parents=True)   # no intent.md, no meta.json at all
    st = cc.build_state()
    row = st["qa_flows"]["flows"][0]
    assert row == {"name": "bare", "intent_line": None, "entry_path": None,
                   "last_verdict": None, "last_run_at": None, "runs": None}


def test_qa_flows_respects_configured_flows_dir(cc):
    (cc.AP_HOME / "config.yaml").write_text("qa:\n  flows_dir: custom/flows\n")
    _write_flow(cc, "login", flows_dir="custom/flows")
    st = cc.build_state()
    assert [f["name"] for f in st["qa_flows"]["flows"]] == ["login"]


# ── explore_enabled (Task 29) — the dashboard's Explore-button gate ──────────

def test_qa_flows_explore_enabled_false_by_default(cc):
    # No config.yaml at all in the fixture -> _load_cfg returns {} -> the
    # gate reads as disabled, same opt-in-off-by-default posture as
    # qa_run.py's own --explore check.
    _write_flow(cc, "login")
    st = cc.build_state()
    assert st["qa_flows"]["explore_enabled"] is False


def test_qa_flows_explore_enabled_false_when_config_says_false(cc):
    (cc.AP_HOME / "config.yaml").write_text(
        "qa:\n  exploratory:\n    enabled: false\n    max_pages: 10\n")
    _write_flow(cc, "login")
    st = cc.build_state()
    assert st["qa_flows"]["explore_enabled"] is False


def test_qa_flows_explore_enabled_true_when_config_says_true(cc):
    (cc.AP_HOME / "config.yaml").write_text(
        "qa:\n  exploratory:\n    enabled: true\n    max_pages: 10\n")
    _write_flow(cc, "login")
    st = cc.build_state()
    assert st["qa_flows"]["explore_enabled"] is True


def test_qa_exploratory_enabled_helper_matches_state_flag(cc):
    (cc.AP_HOME / "config.yaml").write_text("qa:\n  exploratory:\n    enabled: true\n")
    assert cc._qa_exploratory_enabled() is True


# ── explore_enabled decoupled from taught flows (Phase 5 review) ────────────
# explore_enabled lives INSIDE qa_flows, which used to be None whenever there
# were zero taught (SLUG_RE-valid) flows — so enabling exploratory with no
# flows taught yet hid the whole QA view, Explore button included. qa_flows
# must now be present (with an empty `flows` list) whenever EITHER flows
# exist OR exploratory is enabled; still absent when neither holds.

def test_qa_flows_present_with_empty_flows_when_exploratory_enabled_no_flows_dir(cc):
    # No qa/flows dir at all, exploratory enabled -> qa_flows still present.
    (cc.AP_HOME / "config.yaml").write_text(
        "qa:\n  exploratory:\n    enabled: true\n    max_pages: 10\n")
    st = cc.build_state()
    assert "qa_flows" in st
    assert st["qa_flows"]["flows"] == []
    assert st["qa_flows"]["explore_enabled"] is True


def test_qa_flows_present_with_empty_flows_when_exploratory_enabled_empty_flows_dir(cc):
    # qa/flows dir exists but is empty, exploratory enabled -> still present.
    (cc.AP_HOME / "qa" / "flows").mkdir(parents=True)
    (cc.AP_HOME / "config.yaml").write_text(
        "qa:\n  exploratory:\n    enabled: true\n    max_pages: 10\n")
    st = cc.build_state()
    assert "qa_flows" in st
    assert st["qa_flows"]["flows"] == []
    assert st["qa_flows"]["explore_enabled"] is True


def test_qa_flows_absent_when_no_flows_and_exploratory_disabled(cc):
    # Neither flows nor exploratory -> unchanged, still fully absent.
    st = cc.build_state()
    assert "qa_flows" not in st


def test_qa_flows_root_falls_back_to_default_on_escaping_flows_dir(cc):
    # Finding 1 (Phase 4 review): _qa_flows_root used to resolve
    # `AP_HOME / flows_dir` raw — a malformed/escaping flows_dir (leading
    # '/', '..' segments) could point outside .autopilot entirely (the same
    # shape qa_teach.py's `orbit teach` already refuses with exit 2). A poll
    # loop (build_state every ~3s) must never error on a bad config value,
    # so this is a SILENT fallback to the default instead.
    (cc.AP_HOME / "config.yaml").write_text('qa:\n  flows_dir: "../x"\n')
    # A flow planted at what "../x" would actually resolve to, OUTSIDE
    # .autopilot — the fixture that proves no escape: if _qa_flows_root ever
    # honored the bad value, this flow would show up in qa_flows.
    escape_dir = cc.AP_HOME.parent / "x" / "planted-outside"
    escape_dir.mkdir(parents=True)

    assert cc._qa_flows_root() == cc.AP_HOME / cc.DEFAULT_QA_FLOWS_DIR
    st = cc.build_state()
    assert "qa_flows" not in st   # default flows dir doesn't exist -> omitted, planted flow never seen


def test_qa_flows_excludes_non_slug_flow_dir_names(cc):
    # Finding 2 (Phase 4 review): qa_flow_library used to list EVERY
    # directory under flows_root — cc_shell.html's
    # onclick="runFlow('${esc(f.name)}')" isn't quote-safe (esc() only
    # neuters &/</>, not '/"), so a crafted committed flow-dir name could
    # execute JS in the operator dashboard. Non-slug names are unrunnable
    # anyway (do_qa_action/qa_run.py both refuse them), so the listing
    # itself is now SLUG_RE-gated.
    _write_flow(cc, "legit-flow")
    evil_name = "x'onmouseover='1"
    (cc.AP_HOME / "qa" / "flows" / evil_name).mkdir(parents=True)

    st = cc.build_state()
    names = [f["name"] for f in st["qa_flows"]["flows"]]
    assert names == ["legit-flow"]
    assert evil_name not in names


def test_qa_flows_omitted_when_only_non_slug_dirs_exist(cc):
    evil_name = "x'onmouseover='1"
    (cc.AP_HOME / "qa" / "flows" / evil_name).mkdir(parents=True)
    st = cc.build_state()
    assert "qa_flows" not in st   # zero SLUG_RE-valid flows -> same as zero flows at all


def test_pending_heals_excludes_approved_and_rejected_subdirs(cc):
    _write_flow(cc, "login")
    heal = cc.QA_DIR / "heal"
    (heal / "approved").mkdir(parents=True)
    (heal / "rejected").mkdir(parents=True)
    (heal / "pending-1--20260721T000000Z.json").write_text("{}")
    (heal / "pending-2--20260721T000001Z.json").write_text("{}")
    (heal / "approved" / "old-1--20260721T000000Z.json").write_text("{}")
    (heal / "rejected" / "old-2--20260721T000000Z.json").write_text("{}")
    st = cc.build_state()
    assert st["qa_flows"]["pending_heals"] == 2
    assert len(st["qa_flows"]["heals"]) == 2
    assert {h["file"] for h in st["qa_flows"]["heals"]} == {
        "pending-1--20260721T000000Z.json", "pending-2--20260721T000001Z.json"}


def test_pending_heals_excludes_non_conforming_filenames(cc):
    # Finding 1(b): the heal listing is gated to _HEAL_FILE_RE — the same
    # regex heal-approve/heal-reject already require via _heal_candidate_path
    # — not just `.is_file()`. A name like `x&#39;evil.json` is creatable on
    # disk (e.g. by a misbehaving agent writing candidates directly) and,
    # unfiltered, would previously reach cc_shell.html's onclick attributes
    # unescaped-enough to smuggle a quote via HTML entity decoding (see
    # escJsAttr). `.DS_Store` is ordinary Finder litter. Neither is a
    # _HEAL_FILE_RE match, so neither should appear in `heals` or count
    # toward `pending_heals` — while a conforming candidate alongside them
    # still is. This also closes the unclearable-junk-card bug: previously
    # such a file was LISTED but heal-approve/heal-reject both refused it
    # (wrong shape), so Reject always errored and the card could never be
    # cleared; now it's simply never listed.
    # Note: the cc_shell.html escJsAttr fix (escaping & first) isn't
    # harness-testable from here — this Python test suite never renders or
    # parses the dashboard's HTML/JS. This server-side listing filter is the
    # primary gate; escJsAttr is defense-in-depth on top of it.
    _write_flow(cc, "login")
    heal = cc.QA_DIR / "heal"
    heal.mkdir(parents=True)
    _write_heal(cc, "login--20260722T000000Z.json", flow="login")
    (heal / "x&#39;evil.json").write_text("{}")
    (heal / ".DS_Store").write_text("")

    st = cc.build_state()
    qf = st["qa_flows"]
    assert qf["pending_heals"] == 1
    assert [h["file"] for h in qf["heals"]] == ["login--20260722T000000Z.json"]


def test_qa_flows_heals_list_full_shape_and_malformed_entries(cc):
    # Task 26: build_state's `heals` list carries every field a pending
    # candidate needs for the dashboard's heal strip EXCEPT new_steps (too
    # big — the approve action re-reads the file for that). A candidate that
    # isn't valid JSON (or isn't a JSON object) still needs to reach the
    # operator so they can reject it — it's surfaced as {file, malformed}.
    _write_flow(cc, "login")
    heal = cc.QA_DIR / "heal"
    heal.mkdir(parents=True)
    _write_heal(cc, "login--20260722T000000Z.json", flow="login", reason="moved button",
                step_diff="old -> new", screenshot="flow-login/01.png",
                mined_at="2026-07-22T00:00:00Z")
    (heal / "bad--20260722T000001Z.json").write_text("{not valid json")

    st = cc.build_state()
    qf = st["qa_flows"]
    assert qf["pending_heals"] == 2 == len(qf["heals"])

    good = next(h for h in qf["heals"] if h.get("file") == "login--20260722T000000Z.json")
    assert good == {"file": "login--20260722T000000Z.json", "flow": "login",
                    "reason": "moved button", "step_diff": "old -> new",
                    "screenshot": "flow-login/01.png", "mined_at": "2026-07-22T00:00:00Z"}
    assert "new_steps" not in good

    bad = next(h for h in qf["heals"] if h.get("file") == "bad--20260722T000001Z.json")
    assert bad == {"file": "bad--20260722T000001Z.json", "malformed": True}


def test_recent_runs_capped_at_ten_newest_first(cc):
    _write_flow(cc, "login")
    for i in range(12):
        _write_run(cc, f"run-{i:02d}.json", mtime_offset=-(12 - i), summary=f"run {i}")
    st = cc.build_state()
    recent = st["qa_flows"]["recent_runs"]
    assert len(recent) == 10
    assert recent[0]["summary"] == "run 11"
    assert recent[-1]["summary"] == "run 2"


# ── POST /qa-action → do_qa_action ────────────────────────────────────────────

def test_do_qa_action_run_flow_spawns_detached_popen_with_right_argv(cc, monkeypatch):
    _write_flow(cc, "login")
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append((argv, kw))

    monkeypatch.setattr(cc.subprocess, "Popen", FakePopen)
    result = cc.do_qa_action({"action": "run-flow", "flow": "login"})

    assert result == {"ok": True, "msg": "flow login started"}
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv == [sys.executable, str(cc.ENGINE / "qa_run.py"), str(cc.REPO), "--flow", "login"]
    assert kw.get("start_new_session") is True
    assert (cc.LOGDIR / "qa-run-login.log").exists()


def test_do_qa_action_rejects_bad_flow_name_and_never_spawns(cc, monkeypatch):
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    result = cc.do_qa_action({"action": "run-flow", "flow": "Not A Slug!"})
    assert result["ok"] is False
    assert "slug" in result["msg"].lower()
    assert calls == []


def test_do_qa_action_rejects_unknown_flow_and_never_spawns(cc, monkeypatch):
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    result = cc.do_qa_action({"action": "run-flow", "flow": "ghost-flow"})
    assert result["ok"] is False
    assert "ghost-flow" in result["msg"]
    assert calls == []


def test_do_qa_action_run_regression_spawns_detached_popen_with_right_argv(cc, monkeypatch):
    # Task 27: a human "sweep flows now" click — no flow param, and no
    # --if-changed (that gating is the run.sh loop hook's own concern; a
    # human click always means "now").
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append((argv, kw))

    monkeypatch.setattr(cc.subprocess, "Popen", FakePopen)
    result = cc.do_qa_action({"action": "run-regression"})

    assert result == {"ok": True, "msg": "regression sweep started"}
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv == [sys.executable, str(cc.ENGINE / "qa_run.py"), str(cc.REPO), "--regression"]
    assert "--if-changed" not in argv
    assert kw.get("start_new_session") is True
    assert (cc.LOGDIR / "qa-regression.log").exists()


def test_do_qa_action_run_explore_spawns_detached_popen_when_enabled(cc, monkeypatch):
    (cc.AP_HOME / "config.yaml").write_text("qa:\n  exploratory:\n    enabled: true\n    max_pages: 10\n")
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append((argv, kw))

    monkeypatch.setattr(cc.subprocess, "Popen", FakePopen)
    result = cc.do_qa_action({"action": "run-explore"})

    assert result == {"ok": True, "msg": "exploratory crawl started"}
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv == [sys.executable, str(cc.ENGINE / "qa_run.py"), str(cc.REPO), "--explore"]
    assert kw.get("start_new_session") is True
    assert (cc.LOGDIR / "qa-explore.log").exists()


def test_do_qa_action_run_explore_refused_and_never_spawns_when_disabled(cc, monkeypatch):
    # No config.yaml at all -> _qa_exploratory_enabled() reads False, same
    # opt-in-off-by-default posture as qa_run.py's own --explore gate.
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    result = cc.do_qa_action({"action": "run-explore"})

    assert result["ok"] is False
    assert "disabled" in result["msg"].lower()
    assert calls == []
    assert not (cc.LOGDIR / "qa-explore.log").exists()


def test_do_qa_action_run_explore_refused_when_explicitly_false(cc, monkeypatch):
    (cc.AP_HOME / "config.yaml").write_text("qa:\n  exploratory:\n    enabled: false\n")
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    result = cc.do_qa_action({"action": "run-explore"})

    assert result["ok"] is False
    assert calls == []


def test_do_qa_action_rejects_unknown_action(cc, monkeypatch):
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    result = cc.do_qa_action({"action": "delete-everything", "flow": "login"})
    assert result["ok"] is False
    assert "unknown" in result["msg"].lower()
    assert calls == []


# ── heal-approve / heal-reject (Task 26) — mirrors do_calibration_action ─────

def test_do_heal_approve_happy_path_writes_steps_and_meta_and_moves_file(cc):
    fd = _write_flow(cc, "login")   # runs=3, per _write_flow's default
    new_steps = [{"action": "click", "selector": "#new-btn", "value": "", "note": "renamed button"}]
    _write_heal(cc, "login--20260722T000000Z.json", flow="login",
                reason="button renamed", new_steps=new_steps)

    result = cc.do_qa_action({"action": "heal-approve", "file": "login--20260722T000000Z.json"})
    assert result["ok"] is True

    assert json.loads((fd / "steps.json").read_text()) == new_steps
    meta = json.loads((fd / "meta.json").read_text())
    assert len(meta["heals"]) == 1
    assert meta["heals"][0]["reason"] == "button renamed"
    assert meta["heals"][0]["candidate"] == "login--20260722T000000Z.json"
    assert "at" in meta["heals"][0]
    assert meta["runs"] == 3   # runs/last_* untouched by the heal append

    assert not (cc.QA_DIR / "heal" / "login--20260722T000000Z.json").exists()
    assert (cc.QA_DIR / "heal" / "approved" / "login--20260722T000000Z.json").exists()

    # second approve of the same (now-moved) file -> error, nothing double-applied
    result2 = cc.do_qa_action({"action": "heal-approve", "file": "login--20260722T000000Z.json"})
    assert result2["ok"] is False
    meta2 = json.loads((fd / "meta.json").read_text())
    assert len(meta2["heals"]) == 1   # unchanged


def test_do_heal_approve_rejects_unknown_flow_and_touches_nothing(cc):
    _write_heal(cc, "ghost--20260722T000000Z.json", flow="ghost-flow")
    result = cc.do_qa_action({"action": "heal-approve", "file": "ghost--20260722T000000Z.json"})
    assert result["ok"] is False
    assert "ghost-flow" in result["msg"]
    assert (cc.QA_DIR / "heal" / "ghost--20260722T000000Z.json").exists()   # untouched, not moved


def test_do_heal_approve_rejects_empty_new_steps(cc):
    _write_flow(cc, "login")
    _write_heal(cc, "login--20260722T000000Z.json", flow="login", new_steps=[])
    result = cc.do_qa_action({"action": "heal-approve", "file": "login--20260722T000000Z.json"})
    assert result["ok"] is False
    assert not (cc.QA_DIR / "heal" / "approved" / "login--20260722T000000Z.json").exists()


def test_do_heal_approve_rejects_bad_action_in_new_steps(cc):
    fd = _write_flow(cc, "login")
    (fd / "steps.json").write_text("[]")   # pre-existing — proves it's untouched on failure
    _write_heal(cc, "login--20260722T000000Z.json", flow="login",
                new_steps=[{"action": "detonate", "selector": "#x"}])
    result = cc.do_qa_action({"action": "heal-approve", "file": "login--20260722T000000Z.json"})
    assert result["ok"] is False
    assert (fd / "steps.json").read_text() == "[]"


def test_do_heal_approve_refuses_malformed_candidate_json(cc):
    _write_flow(cc, "login")
    p = cc.QA_DIR / "heal" / "login--20260722T000000Z.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not valid json")
    result = cc.do_qa_action({"action": "heal-approve", "file": "login--20260722T000000Z.json"})
    assert result["ok"] is False
    assert p.exists()   # untouched, not moved — approve of malformed is refused


def test_do_heal_reject_requires_reason_and_leaves_file_untouched(cc):
    p = _write_heal(cc, "login--20260722T000000Z.json")
    result = cc.do_qa_action({"action": "heal-reject",
                              "file": "login--20260722T000000Z.json", "reason": ""})
    assert result["ok"] is False
    assert p.exists()


def test_do_heal_reject_stamps_reason_and_moves_to_rejected(cc):
    p = _write_heal(cc, "login--20260722T000000Z.json", flow="login")
    result = cc.do_qa_action({"action": "heal-reject", "file": "login--20260722T000000Z.json",
                              "reason": "not a real drift, revert"})
    assert result["ok"] is True
    assert not p.exists()
    archived = json.loads((cc.QA_DIR / "heal" / "rejected" / "login--20260722T000000Z.json").read_text())
    assert archived["rejected_reason"] == "not a real drift, revert"
    assert archived["flow"] == "login"   # original content preserved
    assert "rejected_at" in archived


def test_do_heal_reject_allows_malformed_candidate_json(cc):
    # Reject-of-malformed is allowed — it's the operator's only way to clear
    # junk out of the pending strip (unlike approve, which refuses it).
    p = cc.QA_DIR / "heal" / "login--20260722T000000Z.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not valid json")
    result = cc.do_qa_action({"action": "heal-reject",
                              "file": "login--20260722T000000Z.json", "reason": "junk"})
    assert result["ok"] is True
    assert not p.exists()
    archived = json.loads((cc.QA_DIR / "heal" / "rejected" / "login--20260722T000000Z.json").read_text())
    assert archived["rejected_reason"] == "junk"
    assert "rejected_at" in archived


@pytest.mark.parametrize("bad_file", [
    "../x.json", "/etc/passwd.json", "sub/dir--20260722T000000Z.json",
    "no-timestamp-suffix.json", "UPPER--20260722T000000Z.json",
])
def test_heal_actions_reject_unsafe_or_malshaped_filenames(cc, bad_file):
    d = cc.QA_DIR / "heal"
    d.mkdir(parents=True)
    before = sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())

    approve = cc.do_qa_action({"action": "heal-approve", "file": bad_file})
    assert approve["ok"] is False

    reject = cc.do_qa_action({"action": "heal-reject", "file": bad_file, "reason": "x"})
    assert reject["ok"] is False

    after = sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())
    assert before == after   # nothing created, moved, or touched under heal/
