import json
import sys
from pathlib import Path

import pytest
ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import prd_flow  # noqa: E402

GOOD_INTAKE = {"run_id": "r1", "title": "t", "pm": "p", "goal": "g", "scope_in": ["a"],
               "scope_out": [], "data_targets": [], "code_targets": [], "pii_touch": False}


# --- stage skills can actually write their artifact ------------------------

def _skill_tools(stage):
    text = (ORBIT / "skills" / f"{prd_flow.SKILLS[stage]}.md").read_text()
    line = [ln for ln in text.splitlines() if ln.startswith("allowed-tools:")]
    assert len(line) == 1, f"{prd_flow.SKILLS[stage]}.md has no single allowed-tools line"
    return [t.strip() for t in line[0].split(":", 1)[1].split(",")]


@pytest.mark.parametrize("stage", prd_flow.STAGES)
def test_every_stage_skill_may_write_its_artifact(stage):
    """Each stage's whole job is writing one file, and record_stage refuses a
    stage whose artifact is missing. Without Write/Edit in allowed-tools the
    skill cannot produce it and every stage dies at the gate."""
    tools = _skill_tools(stage)
    assert "Write" in tools and "Edit" in tools, tools


def test_research_skill_reaches_mcp_and_holds_no_shell():
    """Research grounds against real systems, so it needs the mcp tools (the
    generated per-workspace settings allowlist is the real gate on WHICH ones).
    It reads code_targets with Read/Grep/Glob, so Bash bought it nothing but an
    injection surface on text an upstream artifact supplied."""
    tools = _skill_tools("research")
    assert "mcp__*" in tools
    assert "Bash" not in tools


@pytest.mark.parametrize("stage", [s for s in prd_flow.STAGES if s != "research"])
def test_non_research_stage_skills_hold_no_shell(stage):
    assert "Bash" not in _skill_tools(stage)

def _ws(tmp_path):
    (tmp_path / "research").mkdir()
    return tmp_path

def test_new_creates_marker_and_dir(tmp_path):
    ws = _ws(tmp_path)
    prd_flow.new(str(ws), "r1", "My PRD")
    m = prd_flow.load_marker(str(ws), "r1")
    assert m["status"] == "intake" and m["title"] == "My PRD"

def test_new_stores_brief(tmp_path):
    ws = _ws(tmp_path)
    prd_flow.new(str(ws), "r1", "My PRD", brief="context for the intake skill")
    m = prd_flow.load_marker(str(ws), "r1")
    assert m["brief"] == "context for the intake skill"
    prd_flow.new(str(ws), "r2", "No Brief")
    assert prd_flow.load_marker(str(ws), "r2")["brief"] == ""

def test_record_intake_stamps_and_gates(tmp_path):
    ws = _ws(tmp_path); prd_flow.new(str(ws), "r1", "T")
    (ws / "research" / "r1" / "01-intake.json").write_text(json.dumps(GOOD_INTAKE))
    m = prd_flow.record_stage(str(ws), "r1", "intake")
    assert m["stages"]["intake"]["evals"]["ok"] is True
    assert m["stages"]["intake"]["sha"] and m["stages"]["intake"]["prev_sha"] is None
    assert m["status"] == "awaiting-scope-approval"

def test_research_refused_before_scope_approval(tmp_path):
    ws = _ws(tmp_path); prd_flow.new(str(ws), "r1", "T")
    (ws / "research" / "r1" / "01-intake.json").write_text(json.dumps(GOOD_INTAKE))
    prd_flow.record_stage(str(ws), "r1", "intake")
    with pytest.raises(prd_flow.GateError):
        prd_flow.record_stage(str(ws), "r1", "research")
    prd_flow.approve_scope(str(ws), "r1")
    assert prd_flow.load_marker(str(ws), "r1")["status"] == "research"

def test_stamp_chain_and_tamper_detection(tmp_path):
    ws = _ws(tmp_path); prd_flow.new(str(ws), "r1", "T")
    f = ws / "research" / "r1" / "01-intake.json"
    f.write_text(json.dumps(GOOD_INTAKE))
    prd_flow.record_stage(str(ws), "r1", "intake")
    f.write_text(json.dumps(dict(GOOD_INTAKE, goal="forged")))
    assert any("intake" in e for e in prd_flow.verify(str(ws), "r1"))

def test_failed_evals_still_stamp_but_block_advance(tmp_path):
    ws = _ws(tmp_path); prd_flow.new(str(ws), "r1", "T")
    bad = dict(GOOD_INTAKE, scope_in=[])
    (ws / "research" / "r1" / "01-intake.json").write_text(json.dumps(bad))
    m = prd_flow.record_stage(str(ws), "r1", "intake")
    assert m["stages"]["intake"]["evals"]["ok"] is False
    assert m["status"] == "intake"   # not advanced

def test_revise_cascades(tmp_path):
    ws = _ws(tmp_path); prd_flow.new(str(ws), "r1", "T")
    (ws / "research" / "r1" / "01-intake.json").write_text(json.dumps(GOOD_INTAKE))
    prd_flow.record_stage(str(ws), "r1", "intake"); prd_flow.approve_scope(str(ws), "r1")
    (ws / "research" / "r1" / "02-research.json").write_text(json.dumps(
        {"run_id": "r1", "sources": [{"id": "S1", "kind": "doc", "ref": "x", "date": "d", "summary": "s"}],
         "findings": [], "coverage_gaps": []}))
    prd_flow.record_stage(str(ws), "r1", "research")
    prd_flow.revise(str(ws), "r1", "research")
    m = prd_flow.load_marker(str(ws), "r1")
    assert "research" not in m["stages"] and m["status"] == "research"
    assert "intake" in m["stages"]


# --- run_stage: headless invocation, ORBIT_PRD_CMD test seam, --chain ---

# A fake "claude" that reads PRD_STAGE/PRD_RUN from its env (exported by
# run_stage regardless of the ORBIT_PRD_CMD override) and writes a valid
# artifact for that stage into research/<run>/ (cwd is the target, per
# run_stage's contract). Mirrors what the real /orbit-prd-<skill> slash
# commands are expected to produce (contracts.md).
FAKE_PRD_SCRIPT = r'''
import json, os, sys

stage = os.environ["PRD_STAGE"]
run = os.environ["PRD_RUN"]
d = os.path.join("research", run)
os.makedirs(d, exist_ok=True)

if stage == "intake":
    data = {"run_id": run, "title": "t", "pm": "p", "goal": "g", "scope_in": ["a"],
            "scope_out": [], "data_targets": [], "code_targets": [], "pii_touch": False}
    with open(os.path.join(d, "01-intake.json"), "w") as f:
        json.dump(data, f)
elif stage == "research":
    data = {"run_id": run,
            "sources": [{"id": "S1", "kind": "doc", "ref": "x", "date": "2026-01-01", "summary": "s"}],
            "findings": [{"claim": "c1", "source_ids": ["S1"]}],
            "coverage_gaps": []}
    with open(os.path.join(d, "02-research.json"), "w") as f:
        json.dump(data, f)
elif stage == "synthesis":
    data = {"run_id": run,
            "requirements": [{"id": "R1", "text": "must do X", "source_ids": ["S1"]}],
            "gaps": [], "open_questions": []}
    with open(os.path.join(d, "03-synthesis.json"), "w") as f:
        json.dump(data, f)
elif stage == "draft":
    md = ("## Problem\np\n"
          "## Goals\ng\n"
          "## Non-goals\nn\n"
          "## Requirements\nR1 (S1)\n"
          "## Acceptance criteria\n- responds in < 200ms (S1)\n"
          "## Open questions\nnone\n"
          "## Basis\nS1\n")
    with open(os.path.join(d, "04-prd.md"), "w") as f:
        f.write(md)
elif stage == "review":
    with open(os.path.join(d, "05-review.json"), "w") as f:
        json.dump({"verdicts": [], "ok": True}, f)
sys.exit(0)
'''

# Same as above, but intake writes a Tier-A-eval-failing artifact (empty
# scope_in), to prove run_stage stops instead of advancing on a bad stamp.
FAKE_PRD_SCRIPT_BAD_INTAKE = r'''
import json, os, sys

stage = os.environ["PRD_STAGE"]
run = os.environ["PRD_RUN"]
d = os.path.join("research", run)
os.makedirs(d, exist_ok=True)

data = {"run_id": run, "title": "t", "pm": "p", "goal": "g", "scope_in": [],
        "scope_out": [], "data_targets": [], "code_targets": [], "pii_touch": False}
with open(os.path.join(d, "01-intake.json"), "w") as f:
    json.dump(data, f)
sys.exit(0)
'''


def _write_fake(tmp_path, name, script):
    p = tmp_path / name
    p.write_text(script)
    return p


def test_run_stage_intake_records(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    fake = _write_fake(tmp_path, "fake_intake.py", FAKE_PRD_SCRIPT)
    monkeypatch.setenv("ORBIT_PRD_CMD", f"{sys.executable} {fake}")
    prd_flow.new(str(ws), "r1", "T")
    m = prd_flow.run_stage(str(ws), "r1", "intake")
    assert m["stages"]["intake"]["evals"]["ok"] is True
    assert m["status"] == "awaiting-scope-approval"


def test_run_stage_chain_drives_to_awaiting_signoff(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    fake = _write_fake(tmp_path, "fake_chain.py", FAKE_PRD_SCRIPT)
    monkeypatch.setenv("ORBIT_PRD_CMD", f"{sys.executable} {fake}")
    prd_flow.new(str(ws), "r1", "T")
    prd_flow.run_stage(str(ws), "r1", "intake")
    prd_flow.approve_scope(str(ws), "r1")
    m = prd_flow.run_stage(str(ws), "r1", "research", chain=True)
    assert m["status"] == "awaiting-signoff"
    for stage in ("research", "synthesis", "draft", "review"):
        assert m["stages"][stage]["evals"]["ok"] is True


def test_run_stage_eval_failure_stops_chain(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    fake = _write_fake(tmp_path, "fake_bad.py", FAKE_PRD_SCRIPT_BAD_INTAKE)
    monkeypatch.setenv("ORBIT_PRD_CMD", f"{sys.executable} {fake}")
    prd_flow.new(str(ws), "r1", "T")
    m = prd_flow.run_stage(str(ws), "r1", "intake", chain=True)
    assert m["stages"]["intake"]["evals"]["ok"] is False
    assert m["status"] == "intake"  # not advanced, chain stopped here


def test_verify_clean_on_full_done_run(tmp_path, monkeypatch):
    # Regression: the review stage must never mutate 04-prd.md (or any
    # other already-stamped artifact) after the fact - stamped artifacts
    # are immutable. A full chain through to "done" must leave verify()
    # reporting no tamper at all.
    ws = _ws(tmp_path)
    fake = _write_fake(tmp_path, "fake_full.py", FAKE_PRD_SCRIPT)
    monkeypatch.setenv("ORBIT_PRD_CMD", f"{sys.executable} {fake}")
    prd_flow.new(str(ws), "r1", "T")
    prd_flow.run_stage(str(ws), "r1", "intake")
    prd_flow.approve_scope(str(ws), "r1")
    m = prd_flow.run_stage(str(ws), "r1", "research", chain=True)
    assert m["status"] == "awaiting-signoff"
    m = prd_flow.approve_prd(str(ws), "r1")
    assert m["status"] == "done"
    assert prd_flow.verify(str(ws), "r1") == []


# --- secrets reach the stage child (.autopilot/.secrets.env) ---------------

# A fake "claude" that reports what METAQL_TOKEN it was given, by writing it
# into the intake artifact's `pm` field (which keeps the artifact eval-valid).
FAKE_PRD_SCRIPT_ECHO_TOKEN = r'''
import json, os, sys

run = os.environ["PRD_RUN"]
d = os.path.join("research", run)
os.makedirs(d, exist_ok=True)
with open(os.path.join(d, "01-intake.json"), "w") as f:
    json.dump({"run_id": run, "title": "t", "pm": os.environ.get("METAQL_TOKEN", "<unset>"),
               "goal": "g", "scope_in": ["a"], "scope_out": [], "data_targets": [],
               "code_targets": [], "pii_touch": False}, f)
sys.exit(0)
'''


def test_stage_child_sees_secrets_env_tokens(tmp_path, monkeypatch):
    """An MCP server configured with token_env sends `Bearer ${VAR}`, which
    Claude Code expands from the STAGE CHILD's environment. run.sh exports
    .secrets.env for cycles, but a stage launched from the dashboard inherits
    the dashboard's environment - so _invoke_claude has to merge the file in
    itself, or every grounded research stage runs unauthenticated."""
    ws = _ws(tmp_path)
    ap = ws / ".autopilot"
    ap.mkdir()
    (ap / ".secrets.env").write_text("# tokens\nMETAQL_TOKEN=tok123\n")
    fake = _write_fake(tmp_path, "fake_token.py", FAKE_PRD_SCRIPT_ECHO_TOKEN)
    monkeypatch.setenv("ORBIT_PRD_CMD", f"{sys.executable} {fake}")
    monkeypatch.delenv("METAQL_TOKEN", raising=False)
    prd_flow.new(str(ws), "r1", "T")
    prd_flow.run_stage(str(ws), "r1", "intake")
    artifact = json.loads((ws / "research" / "r1" / "01-intake.json").read_text())
    assert artifact["pm"] == "tok123"


def test_secrets_env_does_not_override_the_calls_own_variables(tmp_path, monkeypatch):
    """.secrets.env sits between the inherited environment and the PRD_*/AP_*
    names this call owns - a stray PRD_STAGE in the file must not redirect the
    stage."""
    ws = _ws(tmp_path)
    ap = ws / ".autopilot"
    ap.mkdir()
    (ap / ".secrets.env").write_text("PRD_STAGE=review\nMETAQL_TOKEN=tok123\n")
    fake = _write_fake(tmp_path, "fake_token2.py", FAKE_PRD_SCRIPT_ECHO_TOKEN)
    monkeypatch.setenv("ORBIT_PRD_CMD", f"{sys.executable} {fake}")
    prd_flow.new(str(ws), "r1", "T")
    m = prd_flow.run_stage(str(ws), "r1", "intake")
    assert m["stages"]["intake"]["evals"]["ok"] is True


# --- no silent stage death: a malformed artifact fails, never raises -------

def _at_research(ws):
    prd_flow.new(str(ws), "r1", "T")
    (ws / "research" / "r1" / "01-intake.json").write_text(json.dumps(GOOD_INTAKE))
    prd_flow.record_stage(str(ws), "r1", "intake")
    prd_flow.approve_scope(str(ws), "r1")


@pytest.mark.parametrize("body", [
    json.dumps(["a", "b"]),                                  # a list of strings
    json.dumps({"run_id": "r1", "sources": [None]}),          # a null in sources
    json.dumps({"run_id": "r1", "sources": [{"id": None}]}),  # a non-string id
    json.dumps({"run_id": "r1", "sources": "S1"}),            # sources not a list
    "not json at all",                                        # unparseable
])
def test_malformed_artifact_fails_the_eval_instead_of_raising(tmp_path, body):
    """The artifact is whatever an agent wrote. A shape nobody anticipated must
    land as `evals.ok: false` with a readable error on the card - a raise here
    kills the stage process and the run just stops with nothing to show."""
    ws = _ws(tmp_path)
    _at_research(ws)
    (ws / "research" / "r1" / "02-research.json").write_text(body)
    m = prd_flow.record_stage(str(ws), "r1", "research")
    stamp = m["stages"]["research"]
    assert stamp["evals"]["ok"] is False
    assert stamp["evals"]["errors"], "a failed eval must say why"
    assert m["status"] == "research"   # held at the stage, not advanced


def test_unparseable_artifact_records_eval_crashed(tmp_path):
    ws = _ws(tmp_path)
    prd_flow.new(str(ws), "r1", "T")
    (ws / "research" / "r1" / "01-intake.json").write_text("{ this is not json")
    m = prd_flow.record_stage(str(ws), "r1", "intake")
    errs = m["stages"]["intake"]["evals"]["errors"]
    assert any("eval crashed" in e for e in errs), errs


def test_missing_mcp_config_surfaces_as_gate_error(tmp_path, monkeypatch):
    """mcp.py is a CLI first: with no config it calls sys.exit(), and SystemExit
    is not an Exception - unwrapped it would terminate the stage process. It has
    to arrive as a GateError the caller can report."""
    ws = _ws(tmp_path)
    prd_flow.new(str(ws), "r1", "T")
    monkeypatch.delenv("ORBIT_PRD_CMD", raising=False)
    monkeypatch.setattr(prd_flow.shutil, "which", lambda _name: "/usr/bin/false")
    assert not (ws / ".autopilot" / "config.yaml").exists()
    with pytest.raises(prd_flow.GateError) as e:
        prd_flow.run_stage(str(ws), "r1", "intake")
    assert "mcp" in str(e.value).lower()


# --- run id shape is the controller's, not the dashboard's -----------------

@pytest.mark.parametrize("bad", ["O'Brien", "../escape", "has space", "Upper", "", "-leading"])
def test_new_rejects_invalid_run_ids(tmp_path, bad):
    ws = _ws(tmp_path)
    with pytest.raises(prd_flow.GateError):
        prd_flow.new(str(ws), bad, "T")


def test_new_accepts_the_documented_shape(tmp_path):
    ws = _ws(tmp_path)
    assert prd_flow.new(str(ws), "checkout-redesign-2", "T")["run_id"] == "checkout-redesign-2"


# --- pii_touch scope gate --------------------------------------------------

PII_INTAKE = dict(GOOD_INTAKE, pii_touch=True)


def _at_scope_gate(ws, intake):
    prd_flow.new(str(ws), "r1", "T")
    (ws / "research" / "r1" / "01-intake.json").write_text(json.dumps(intake))
    return prd_flow.record_stage(str(ws), "r1", "intake")


def test_pii_run_records_the_flag_on_the_marker(tmp_path):
    ws = _ws(tmp_path)
    assert _at_scope_gate(ws, PII_INTAKE)["pii_touch"] is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _at_scope_gate(_ws(plain), GOOD_INTAKE)["pii_touch"] is False


def test_pii_run_refuses_scope_approval_without_acknowledgement(tmp_path):
    ws = _ws(tmp_path)
    _at_scope_gate(ws, PII_INTAKE)
    with pytest.raises(prd_flow.GateError) as e:
        prd_flow.approve_scope(str(ws), "r1")
    assert "PII acknowledgement" in str(e.value)
    # Refused means unchanged: still at the gate, still unapproved.
    m = prd_flow.load_marker(str(ws), "r1")
    assert m["status"] == "awaiting-scope-approval" and m["approvals"]["scope"] is None

    m = prd_flow.approve_scope(str(ws), "r1", ack_pii=True)
    assert m["status"] == "research"
    assert m["approvals"]["scope"]["ack_pii"] is True


def test_non_pii_run_needs_no_acknowledgement(tmp_path):
    ws = _ws(tmp_path)
    _at_scope_gate(ws, GOOD_INTAKE)
    m = prd_flow.approve_scope(str(ws), "r1")
    assert m["status"] == "research"
    assert m["approvals"]["scope"]["ack_pii"] is False


def test_revising_intake_clears_a_stale_pii_flag(tmp_path):
    ws = _ws(tmp_path)
    _at_scope_gate(ws, PII_INTAKE)
    prd_flow.revise(str(ws), "r1", "intake")
    (ws / "research" / "r1" / "01-intake.json").write_text(json.dumps(GOOD_INTAKE))
    assert prd_flow.record_stage(str(ws), "r1", "intake")["pii_touch"] is False
    assert prd_flow.approve_scope(str(ws), "r1")["status"] == "research"
