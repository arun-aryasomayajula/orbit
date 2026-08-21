import json
import sys
from pathlib import Path
ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import prd_flow  # noqa: E402

GOOD_INTAKE = {"run_id": "r1", "title": "t", "pm": "p", "goal": "g", "scope_in": ["a"],
               "scope_out": [], "data_targets": [], "code_targets": [], "pii_touch": False}

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
    import pytest
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
    with open(os.path.join(d, "04-prd.md"), "a") as f:
        f.write("\n## Review audit trail\n- none\n")
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
