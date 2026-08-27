import sys
from pathlib import Path

import pytest
ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import prd_evals  # noqa: E402

GOOD_RESEARCH = {"run_id": "r1",
    "sources": [{"id": "S1", "kind": "analytics", "ref": "q", "date": "2026-08-21", "summary": "x"}],
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
            "## Acceptance criteria\n- recall >= 95% on the validation set\n\n## Open questions\n- q\n\n## Basis\n- S1: analytics q\n")

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

def test_research_accepts_figma_kind():
    good = dict(GOOD_RESEARCH, sources=[{"id": "S1", "kind": "figma",
                "ref": "https://figma.com/file/abc/node-id=1:2", "date": "2026-08-22", "summary": "pilot screen frame"}])
    assert prd_evals.check_research(good) == []

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


# --- total on shape: a wrong shape is a finding, never an exception --------
# The input is whatever an agent wrote to disk. A raise here kills the stage
# process; an error string fails the stage's eval and leaves a readable reason.

@pytest.mark.parametrize("artifact", [None, [], ["a", "b"], "a string", 7])
def test_every_checker_survives_a_non_object_artifact(artifact):
    assert prd_evals.check_intake(artifact)
    assert prd_evals.check_research(artifact)
    assert prd_evals.check_synthesis(artifact, GOOD_RESEARCH)


@pytest.mark.parametrize("research", [None, [], "a string", {"sources": "S1"},
                                      {"sources": [None]}, {"sources": [{"id": None}]}])
def test_checkers_survive_a_malformed_research_reference(research):
    # research is the SECOND argument to synthesis/prd checks - a malformed
    # upstream artifact must not take the downstream stage's eval down with it.
    prd_evals.check_synthesis({"run_id": "r1", "requirements": []}, research)
    prd_evals.check_prd_md(GOOD_PRD, research)
    assert prd_evals.check_research(research)


@pytest.mark.parametrize("srcs", [[None], [["S1"]], ["S1"], [{"id": None}], [{"id": 7}]])
def test_research_reports_bad_source_entries_without_raising(srcs):
    errs = prd_evals.check_research(dict(GOOD_RESEARCH, sources=srcs))
    assert errs and all(isinstance(e, str) for e in errs)


@pytest.mark.parametrize("reqs", [[None], ["R1"], [{"id": None}], [{"id": "R1", "source_ids": "S1"}]])
def test_synthesis_reports_bad_requirement_entries_without_raising(reqs):
    errs = prd_evals.check_synthesis({"run_id": "r1", "requirements": reqs}, GOOD_RESEARCH)
    assert errs and all(isinstance(e, str) for e in errs)


def test_research_reports_bad_finding_entries_without_raising():
    for findings in ([None], ["c"], [{"claim": "c", "source_ids": "S1"}], "not a list"):
        errs = prd_evals.check_research(dict(GOOD_RESEARCH, findings=findings))
        assert errs and all(isinstance(e, str) for e in errs), findings
