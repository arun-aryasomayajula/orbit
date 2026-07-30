# AI test generation (engine/qa_run.py's `--generate`) — dashboard-side
# review/approve/reject for CANDIDATE flows staged under
# $AP_STATE/qa/generated/<slug>/ (never written straight into the committed
# flow library). Mirrors tests/test_command_center_qa_view.py's heal-
# candidate conventions exactly, one level up: a heal candidate corrects one
# file inside an EXISTING flow; a generate candidate IS a whole flow dir, so
# approve MOVES it wholesale into the library instead of rewriting in place.
#
# Run: python3.11 -m pytest tests/test_command_center_generate.py -v   (from orbit/)
import http.client
import http.server
import importlib.util
import json
import sys
import threading
import urllib.parse
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


def _configure_qa(cc):
    """A bare qa: block — enough for qa_flow_library() to surface qa_flows at
    all (see its own `qa_configured` check) even with zero taught flows and
    exploratory disabled. Real usage never needs this: engine/qa_run.py's
    --generate itself refuses to run without a qa: block, so candidates and
    a qa: block always co-occur outside a test fixture — this just supplies
    the precondition these unit tests would otherwise have to fake via a
    taught flow instead."""
    (cc.AP_HOME / "config.yaml").write_text('qa:\n  base_url: "http://x"\n')


def _write_candidate(cc, name, intent="do the candidate thing", entry_path="http://x/candidate",
                      criteria=("sees the success state",), requires=(), provides_auth=False,
                      steps=None, root=None):
    """Builds a full $AP_STATE/qa/generated/<name>/ candidate dir — the exact
    shape skills/orbit-qa-generate.md step 3 writes: intent.md (frontmatter +
    one-line intent + '## Success criteria'), steps.json, meta.json
    (including `generated: true`)."""
    cand_dir = (root or (cc.QA_DIR / "generated")) / name
    cand_dir.mkdir(parents=True)
    intent_md = (
        "---\n"
        f"name: {name}\n"
        "created: '2026-07-25T00:00:00Z'\n"
        f"entry_path: {entry_path}\n"
        "tags: []\n"
        "---\n"
        "\n"
        f"{intent}\n"
        "\n"
        "## Success criteria\n"
        + "".join(f"- {c}\n" for c in criteria)
    )
    (cand_dir / "intent.md").write_text(intent_md)
    if steps is None:
        steps = [{"action": "goto", "selector": "", "value": entry_path, "note": "start"}]
    (cand_dir / "steps.json").write_text(json.dumps(steps))
    meta = {"created_at": "2026-07-25T00:00:00Z", "last_run_at": None, "last_verdict": None,
            "runs": 0, "heals": [], "requires": list(requires), "provides_auth": provides_auth,
            "generated": True}
    (cand_dir / "meta.json").write_text(json.dumps(meta))
    return cand_dir


# ── build_state()'s qa_flows.generated key ───────────────────────────────────

def test_generated_absent_when_no_generated_dir_at_all(cc):
    _write_flow(cc, "login")
    st = cc.build_state()
    assert "generated" not in st["qa_flows"]


def test_generated_absent_when_generated_dir_exists_but_empty(cc):
    _write_flow(cc, "login")
    (cc.QA_DIR / "generated").mkdir(parents=True)
    st = cc.build_state()
    assert "generated" not in st["qa_flows"]


def test_generated_byte_neutral_full_state_without_candidates(cc):
    # The overarching claim: a target that has never run --generate sees an
    # EXACT match to the pre-existing qa_flows shape (heal/flows tests above
    # already prove those sub-shapes individually) plus only `qa_busy` (an
    # unconditionally-present field, same precedent as explore_enabled) —
    # no `generated` key leaks in with zero pending candidates.
    _write_flow(cc, "login")
    st = cc.build_state()
    assert set(st["qa_flows"].keys()) == {
        "flows", "recent_runs", "heals", "pending_heals", "explore_enabled", "base_url", "qa_busy"}


# ── qa_busy — the shared .qa-running lock, surfaced as a UI hint ────────────

def test_qa_busy_false_by_default(cc):
    _write_flow(cc, "login")
    st = cc.build_state()
    assert st["qa_flows"]["qa_busy"] is False


def test_qa_busy_true_when_lock_file_present(cc):
    _write_flow(cc, "login")
    lock = cc.QA_DIR / ".qa-running"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": 999999, "started_at": "2020-01-01T00:00:00Z"}))
    st = cc.build_state()
    assert st["qa_flows"]["qa_busy"] is True


def test_generated_present_with_full_candidate_shape(cc):
    _write_candidate(cc, "checkout-flow", intent="complete a checkout",
                      entry_path="http://x/checkout",
                      criteria=("sees order confirmation", "sees the receipt email note"),
                      requires=["login"])
    _write_flow(cc, "login")   # so requires refers to something real, cosmetically
    st = cc.build_state()
    gen = st["qa_flows"]["generated"]
    assert gen == [{"name": "checkout-flow", "intent": "complete a checkout",
                    "summary": "sees order confirmation; sees the receipt email note",
                    "requires": ["login"]}]


def test_generated_summary_falls_back_to_intent_when_no_criteria(cc):
    _configure_qa(cc)
    cand_dir = _write_candidate(cc, "bare-flow", intent="just look around", criteria=())
    # _write_candidate always appends criteria lines; strip them to leave a
    # heading with nothing under it (a candidate mid-write, or a hand-edited
    # one) — summary must still degrade gracefully to the one-line intent.
    text = (cand_dir / "intent.md").read_text()
    assert text.rstrip().endswith("## Success criteria")
    st = cc.build_state()
    gen = st["qa_flows"]["generated"]
    assert gen == [{"name": "bare-flow", "intent": "just look around",
                    "summary": "just look around", "requires": []}]


def test_generated_excludes_rejected_subdirectory(cc):
    _configure_qa(cc)
    _write_candidate(cc, "pending-one")
    rejected_dir = cc.QA_DIR / "generated" / "rejected"
    rejected_dir.mkdir(parents=True)
    _write_candidate(cc, "archived-one", root=rejected_dir)
    st = cc.build_state()
    names = [g["name"] for g in st["qa_flows"]["generated"]]
    assert names == ["pending-one"]
    assert "archived-one" not in names


def test_generated_excludes_non_slug_directory_names(cc):
    _configure_qa(cc)
    _write_candidate(cc, "legit-candidate")
    evil = cc.QA_DIR / "generated" / "x'onmouseover='1"
    evil.mkdir(parents=True)
    st = cc.build_state()
    names = [g["name"] for g in st["qa_flows"]["generated"]]
    assert names == ["legit-candidate"]


def test_generated_malformed_intent_yields_nulls_never_a_crash(cc):
    _configure_qa(cc)
    cand_dir = cc.QA_DIR / "generated" / "broken"
    cand_dir.mkdir(parents=True)
    (cand_dir / "intent.md").write_text("not even frontmatter\n")
    (cand_dir / "meta.json").write_text(json.dumps({"requires": ["a", "b"]}))
    st = cc.build_state()   # must not raise
    gen = st["qa_flows"]["generated"]
    assert gen == [{"name": "broken", "intent": None, "summary": None, "requires": ["a", "b"]}]


def test_generated_missing_meta_json_yields_empty_requires(cc):
    _configure_qa(cc)
    cand_dir = cc.QA_DIR / "generated" / "no-meta"
    cand_dir.mkdir(parents=True)
    (cand_dir / "intent.md").write_text(
        "---\nname: no-meta\ncreated: '2026-07-25T00:00:00Z'\nentry_path: http://x\ntags: []\n---\n"
        "\nsome intent\n\n## Success criteria\n- a thing\n")
    st = cc.build_state()
    gen = st["qa_flows"]["generated"]
    assert gen == [{"name": "no-meta", "intent": "some intent", "summary": "a thing", "requires": []}]


def test_generated_multiple_candidates_sorted_by_name(cc):
    _configure_qa(cc)
    _write_candidate(cc, "zeta-flow")
    _write_candidate(cc, "alpha-flow")
    st = cc.build_state()
    names = [g["name"] for g in st["qa_flows"]["generated"]]
    assert names == ["alpha-flow", "zeta-flow"]


# ── POST /qa-action run-generate ─────────────────────────────────────────────

def test_do_qa_action_run_generate_spawns_detached_popen_with_right_argv(cc, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append((argv, kw))

    monkeypatch.setattr(cc.subprocess, "Popen", FakePopen)
    result = cc.do_qa_action({"action": "run-generate"})

    assert result == {"ok": True, "msg": "test generation started"}
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv == [sys.executable, str(cc.ENGINE / "qa_run.py"), str(cc.REPO), "--generate"]
    assert "--attach" not in argv
    assert kw.get("start_new_session") is True
    assert (cc.LOGDIR / "qa-generate.log").exists()


def test_do_qa_action_run_generate_passes_attach_when_dev_session_running(cc, monkeypatch):
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    with cc._QA_SERVICES_LOCK:
        cc._QA_SERVICES["state"] = "running"

    result = cc.do_qa_action({"action": "run-generate"})

    assert result["ok"] is True
    assert "attached" in result["msg"].lower()
    argv, kw = calls[0]
    assert argv == [sys.executable, str(cc.ENGINE / "qa_run.py"), str(cc.REPO), "--generate", "--attach"]


# ── generate-approve ─────────────────────────────────────────────────────────

def test_do_generate_approve_moves_candidate_into_library_with_flowmap_and_requires(cc):
    _write_flow(cc, "login")
    cand_dir = _write_candidate(cc, "checkout-flow", intent="complete a checkout",
                                 entry_path="http://x/checkout",
                                 criteria=("sees order confirmation",),
                                 requires=["login"], provides_auth=False)

    result = cc.do_qa_action({"action": "generate-approve", "name": "checkout-flow"})
    assert result["ok"] is True

    assert not cand_dir.exists()
    flow_dir = cc.AP_HOME / "qa" / "flows" / "checkout-flow"
    assert flow_dir.is_dir()
    assert (flow_dir / "steps.json").is_file()
    meta = json.loads((flow_dir / "meta.json").read_text())
    assert meta["requires"] == ["login"]           # preserved untouched
    assert meta["provides_auth"] is False
    assert meta["generated"] is True                # left in place, harmless provenance
    assert meta["runs"] == 0

    flowmap = (cc.AP_HOME / "qa" / "FLOWMAP.md").read_text()
    row = [ln for ln in flowmap.splitlines() if ln.startswith("| checkout-flow |")][0]
    assert "complete a checkout" in row
    assert "http://x/checkout" in row
    assert row.rstrip().endswith("| never run |")


def test_do_generate_approve_refuses_when_flow_name_already_exists(cc):
    existing = _write_flow(cc, "checkout-flow")
    cand_dir = _write_candidate(cc, "checkout-flow")

    result = cc.do_qa_action({"action": "generate-approve", "name": "checkout-flow"})
    assert result["ok"] is False
    assert "already exists" in result["msg"]
    assert cand_dir.exists()          # untouched, not moved
    assert existing.exists()


def test_do_generate_approve_refuses_missing_steps_json(cc):
    cand_dir = _write_candidate(cc, "no-steps")
    (cand_dir / "steps.json").unlink()

    result = cc.do_qa_action({"action": "generate-approve", "name": "no-steps"})
    assert result["ok"] is False
    assert "steps.json" in result["msg"]
    assert cand_dir.exists()
    assert not (cc.AP_HOME / "qa" / "flows" / "no-steps").exists()


def test_do_generate_approve_refuses_empty_steps_json(cc):
    cand_dir = _write_candidate(cc, "empty-steps", steps=[])
    result = cc.do_qa_action({"action": "generate-approve", "name": "empty-steps"})
    assert result["ok"] is False
    assert cand_dir.exists()


def test_do_generate_approve_refuses_malformed_meta_json(cc):
    cand_dir = _write_candidate(cc, "bad-meta")
    (cand_dir / "meta.json").write_text("{not valid json")
    result = cc.do_qa_action({"action": "generate-approve", "name": "bad-meta"})
    assert result["ok"] is False
    assert cand_dir.exists()


def test_do_generate_approve_refuses_unreadable_intent(cc):
    cand_dir = _write_candidate(cc, "bad-intent")
    (cand_dir / "intent.md").write_text("not even frontmatter\n")
    result = cc.do_qa_action({"action": "generate-approve", "name": "bad-intent"})
    assert result["ok"] is False
    assert cand_dir.exists()


def test_do_generate_approve_refuses_unknown_candidate(cc):
    result = cc.do_qa_action({"action": "generate-approve", "name": "ghost-flow"})
    assert result["ok"] is False
    assert "ghost-flow" in result["msg"]


# ── generate-reject ───────────────────────────────────────────────────────────

def test_do_generate_reject_archives_candidate(cc):
    cand_dir = _write_candidate(cc, "checkout-flow")
    result = cc.do_qa_action({"action": "generate-reject", "name": "checkout-flow"})
    assert result["ok"] is True
    assert not cand_dir.exists()
    archived = cc.QA_DIR / "generated" / "rejected" / "checkout-flow"
    assert archived.is_dir()
    assert (archived / "intent.md").is_file()


def test_do_generate_reject_disambiguates_repeat_rejection_of_same_name(cc):
    _write_candidate(cc, "checkout-flow", intent="first draft")
    cc.do_qa_action({"action": "generate-reject", "name": "checkout-flow"})
    _write_candidate(cc, "checkout-flow", intent="second draft")   # a fresh regeneration, same slug
    result = cc.do_qa_action({"action": "generate-reject", "name": "checkout-flow"})
    assert result["ok"] is True

    rejected_root = cc.QA_DIR / "generated" / "rejected"
    entries = sorted(p.name for p in rejected_root.iterdir())
    assert len(entries) == 2
    assert "checkout-flow" in entries
    assert any(e.startswith("checkout-flow--") for e in entries)


def test_do_generate_reject_unknown_candidate_refused(cc):
    result = cc.do_qa_action({"action": "generate-reject", "name": "ghost-flow"})
    assert result["ok"] is False


# ── traversal / bad-slug / missing-candidate rejected, nothing moved ────────

@pytest.mark.parametrize("bad_name", [
    "../x", "/etc/passwd", "sub/dir", "UPPER-CASE", "", "rejected",
])
def test_generate_actions_reject_unsafe_or_malshaped_names(cc, bad_name):
    root = cc.QA_DIR / "generated"
    root.mkdir(parents=True)
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))

    approve = cc.do_qa_action({"action": "generate-approve", "name": bad_name})
    assert approve["ok"] is False

    reject = cc.do_qa_action({"action": "generate-reject", "name": bad_name})
    assert reject["ok"] is False

    after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    assert before == after   # nothing created, moved, or touched under generated/


# ── live HTTP wire: the /qa-action POST handler actually threads `name`
# through to do_qa_action ──────────────────────────────────────────────────
#
# Every test above calls cc.do_qa_action({...}) DIRECTLY, which bypasses
# do_POST's form-body → payload-dict mapping entirely. That mapping is its own
# failure surface: generate-approve/generate-reject dispatch on payload["name"],
# so if do_POST forgets to extract `name` from the POSTed body the whole gate
# is a silent no-op over the real dashboard while every direct-call test stays
# green (exactly the regression this test exists to catch). Same live-server
# harness convention as tests/test_launcher.py.

def _start_cc_server(cc):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), cc.Handler)
    port = srv.server_address[1]
    # Match do_POST's Host/Origin gate to the OS-assigned port (mirrors
    # test_launcher._start_launcher). PORT/_ALLOWED_HOSTS are import-time
    # module globals; the fixture module is a fresh import per test, so
    # patching them here can't leak into another test.
    cc.PORT = port
    cc._ALLOWED_HOSTS = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, port


def _post_qa_action(port, token, fields):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        body = urllib.parse.urlencode(fields)
        conn.request("POST", "/qa-action", body, {
            "Host": f"127.0.0.1:{port}",
            "Origin": f"http://127.0.0.1:{port}",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CC-Token": token,
        })
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode())
    finally:
        conn.close()


def test_qa_action_post_threads_name_to_generate_approve_over_the_wire(cc):
    _write_flow(cc, "login")
    cand_dir = _write_candidate(cc, "checkout-flow", requires=["login"])
    srv, t, port = _start_cc_server(cc)
    try:
        status, payload = _post_qa_action(
            port, cc._TOKEN, {"action": "generate-approve", "name": "checkout-flow"})
    finally:
        srv.shutdown(); srv.server_close(); t.join(timeout=5)

    assert status == 200
    assert payload["ok"] is True, payload           # would be a no-op if `name` weren't extracted
    assert not cand_dir.exists()
    assert (cc.AP_HOME / "qa" / "flows" / "checkout-flow" / "steps.json").is_file()


def test_qa_action_post_threads_name_to_generate_reject_over_the_wire(cc):
    cand_dir = _write_candidate(cc, "checkout-flow")
    srv, t, port = _start_cc_server(cc)
    try:
        status, payload = _post_qa_action(
            port, cc._TOKEN, {"action": "generate-reject", "name": "checkout-flow"})
    finally:
        srv.shutdown(); srv.server_close(); t.join(timeout=5)

    assert status == 200
    assert payload["ok"] is True, payload
    assert not cand_dir.exists()
    assert (cc.QA_DIR / "generated" / "rejected" / "checkout-flow").is_dir()
