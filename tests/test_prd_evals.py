import sys
from pathlib import Path
ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import prd_evals  # noqa: E402

GOOD_RESEARCH = {"run_id": "r1",
    "sources": [{"id": "S1", "kind": "metaql", "ref": "q", "date": "2026-08-21", "summary": "x"}],
    "findings": [{"claim": "c", "source_ids": ["S1"]}], "coverage_gaps": []}

def test_intake_requires_nonempty_scope_and_bool_pii():
    bad = {"run_id": "r1", "title": "t", "pm": "p", "goal": "g",
           "scope_in": [], "scope_out": [], "data_targets": [], "code_targets": [], "pii_touch": "yes"}
    errs = prd_evals.check_intake(bad)
    assert any("scope_in" in e for e in errs) and any("pii_touch" in e for e in errs)

def test_research_findings_must_cite_resolving_sources():
    bad = dict(GOOD_RESEARCH, findings=[{"claim": "c", "source_ids": ["S9"]}])
    assert any("S9" in e for e in prd_evals.check_research(bad))
    assert prd_evals.check_research(GOOD_RESEARCH) == []

def test_synthesis_requirements_resolve_against_research():
    good = {"run_id": "r1", "requirements": [{"id": "R1", "text": "x", "source_ids": ["S1"]}],
            "gaps": [], "open_questions": []}
    assert prd_evals.check_synthesis(good, GOOD_RESEARCH) == []
    bad = dict(good, requirements=[{"id": "R1", "text": "x", "source_ids": ["S2"]}])
    assert any("S2" in e for e in prd_evals.check_synthesis(bad, GOOD_RESEARCH))

GOOD_PRD = ("# T\n\n## Problem\np (S1)\n\n## Goals\ng\n\n## Non-goals\nn\n\n## Requirements\n- R1 (S1)\n\n"
            "## Acceptance criteria\n- recall >= 95% on the validation set\n\n## Open questions\n- q\n\n## Basis\n- S1: metaql q\n")

def test_prd_md_headings_citations_testable_criteria():
    assert prd_evals.check_prd_md(GOOD_PRD, GOOD_RESEARCH) == []
    assert any("S7" in e for e in prd_evals.check_prd_md(GOOD_PRD.replace("(S1)", "(S7)"), GOOD_RESEARCH))
    woolly = GOOD_PRD.replace("- recall >= 95% on the validation set", "- works well for users")
    assert any("testable" in e.lower() for e in prd_evals.check_prd_md(woolly, GOOD_RESEARCH))
    assert any("Non-goals" in e for e in prd_evals.check_prd_md(GOOD_PRD.replace("## Non-goals\nn\n\n", ""), GOOD_RESEARCH))
    assert any("TODO" in e for e in prd_evals.check_prd_md(GOOD_PRD + "\nTODO x\n", GOOD_RESEARCH))

def test_research_findings_require_nonempty_claim():
    bad = dict(GOOD_RESEARCH, findings=[{"claim": "", "source_ids": ["S1"]}])
    assert any("claim" in e for e in prd_evals.check_research(bad))
    bad = dict(GOOD_RESEARCH, findings=[{"source_ids": ["S1"]}])
    assert any("claim" in e for e in prd_evals.check_research(bad))

def test_research_and_synthesis_require_run_id():
    bad_research = dict(GOOD_RESEARCH, run_id="")
    assert any("run_id" in e for e in prd_evals.check_research(bad_research))
    bad_synthesis = {"requirements": [{"id": "R1", "text": "x", "source_ids": ["S1"]}], "gaps": [], "open_questions": []}
    assert any("run_id" in e for e in prd_evals.check_synthesis(bad_synthesis, GOOD_RESEARCH))

def test_research_source_kind_must_be_known():
    bad = dict(GOOD_RESEARCH, sources=[{"id": "S1", "kind": "unknown_kind", "ref": "q", "date": "2026-08-21", "summary": "x"}])
    assert any("kind" in e for e in prd_evals.check_research(bad))

def test_research_coverage_gaps_must_be_list():
    bad = dict(GOOD_RESEARCH, coverage_gaps="not a list")
    assert any("coverage_gaps" in e for e in prd_evals.check_research(bad))

def test_synthesis_gaps_and_open_questions_must_be_lists():
    good = {"run_id": "r1", "requirements": [{"id": "R1", "text": "x", "source_ids": ["S1"]}], "gaps": [], "open_questions": []}
    assert prd_evals.check_synthesis(good, GOOD_RESEARCH) == []
    bad = dict(good, gaps="not a list")
    assert any("gaps" in e for e in prd_evals.check_synthesis(bad, GOOD_RESEARCH))
    bad = dict(good, open_questions="not a list")
    assert any("open_questions" in e for e in prd_evals.check_synthesis(bad, GOOD_RESEARCH))
