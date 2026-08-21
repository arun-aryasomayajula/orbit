import json, sys
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
