import subprocess, sys
from pathlib import Path

ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import insight_lint  # noqa: E402

GOOD = """# Funnel drop at plan-select

## Answer
Conversion from view-plans to pay fell 4.2% week over week (S1).

## Basis
- S1: metaql_execute_query explore=premium_funnel, 2026-08-18, rows=14
"""

def _run_cli(repo):
    return subprocess.run([sys.executable, str(ORBIT / "engine" / "insight_lint.py"), str(repo)],
                          capture_output=True, text=True)

def test_good_insight_passes(tmp_path):
    d = tmp_path / "insights"; d.mkdir()
    (d / "001-funnel.md").write_text(GOOD)
    assert insight_lint.lint_file(d / "001-funnel.md") == []
    assert _run_cli(tmp_path).returncode == 0

def test_missing_basis_section_fails(tmp_path):
    d = tmp_path / "insights"; d.mkdir()
    (d / "x.md").write_text("# T\n\n## Answer\nUp 5% (S1).\n")
    errs = insight_lint.lint_file(d / "x.md")
    assert any("Basis" in e for e in errs)
    assert _run_cli(tmp_path).returncode == 1

def test_dangling_citation_fails(tmp_path):
    d = tmp_path / "insights"; d.mkdir()
    (d / "x.md").write_text(GOOD.replace("(S1)", "(S9)"))
    errs = insight_lint.lint_file(d / "x.md")
    assert any("S9" in e for e in errs)

def test_todo_marker_fails(tmp_path):
    d = tmp_path / "insights"; d.mkdir()
    (d / "x.md").write_text(GOOD + "\nTODO: check cohort\n")
    assert any("TODO" in e for e in insight_lint.lint_file(d / "x.md"))

def test_repo_without_insights_dir_is_clean(tmp_path):
    assert _run_cli(tmp_path).returncode == 0

# Survey-specific tests

GOOD_SURVEY = """# User preferences on payment methods

## Answer
Users prefer mobile wallets for speed, but still value card backups (S1, S2).

## Basis
- S1: respondent A, 2026-08-15
- S2: respondent B, 2026-08-15

## Coverage gaps
None identified; sample representative of target users.

> Wallets are faster
> Cards should stay as backup
> Mobile-first is the way
"""

SURVEY_NO_QUOTES = """# User preferences on payment methods

## Answer
Users prefer mobile wallets for speed.

## Basis
- S1: respondent A, 2026-08-15
"""

SURVEY_WITH_QUOTES = """# User preferences on payment methods

## Answer
Users prefer mobile wallets for speed.

## Basis
- S1: respondent A, 2026-08-15

> Wallets are faster
> Cards should stay as backup
> Mobile-first is the way
"""

SURVEY_WITH_COVERAGE_GAPS = """# User preferences on payment methods

## Answer
Users prefer mobile wallets for speed.

## Basis
- S1: respondent A, 2026-08-15

## Coverage gaps
Limited to power users; needs validation with casual users.
"""

def test_good_survey_with_quotes_passes(tmp_path):
    d = tmp_path / "surveys" / "001"; d.mkdir(parents=True)
    (d / "report.md").write_text(GOOD_SURVEY)
    errs = insight_lint.lint_file(d / "report.md", survey=True)
    assert errs == []
    assert _run_cli(tmp_path).returncode == 0

def test_survey_without_quotes_and_without_coverage_gaps_fails(tmp_path):
    d = tmp_path / "surveys" / "001"; d.mkdir(parents=True)
    (d / "report.md").write_text(SURVEY_NO_QUOTES)
    errs = insight_lint.lint_file(d / "report.md", survey=True)
    assert any("quote" in e.lower() or "coverage" in e.lower() for e in errs)

def test_survey_with_quotes_no_coverage_gaps_passes(tmp_path):
    d = tmp_path / "surveys" / "001"; d.mkdir(parents=True)
    (d / "report.md").write_text(SURVEY_WITH_QUOTES)
    errs = insight_lint.lint_file(d / "report.md", survey=True)
    assert errs == []

def test_survey_without_quotes_but_with_coverage_gaps_passes(tmp_path):
    d = tmp_path / "surveys" / "001"; d.mkdir(parents=True)
    (d / "report.md").write_text(SURVEY_WITH_COVERAGE_GAPS)
    errs = insight_lint.lint_file(d / "report.md", survey=True)
    assert errs == []

def test_survey_via_cli_passes(tmp_path):
    d = tmp_path / "surveys" / "001"; d.mkdir(parents=True)
    (d / "report.md").write_text(GOOD_SURVEY)
    assert _run_cli(tmp_path).returncode == 0

def test_survey_via_cli_fails(tmp_path):
    d = tmp_path / "surveys" / "001"; d.mkdir(parents=True)
    (d / "report.md").write_text(SURVEY_NO_QUOTES)
    assert _run_cli(tmp_path).returncode == 1

def test_repo_with_surveys_only_no_insights_runs(tmp_path):
    d = tmp_path / "surveys" / "001"; d.mkdir(parents=True)
    (d / "report.md").write_text(GOOD_SURVEY)
    assert _run_cli(tmp_path).returncode == 0

def test_existing_insight_behavior_unchanged(tmp_path):
    d = tmp_path / "insights"; d.mkdir()
    (d / "001-funnel.md").write_text(GOOD)
    assert insight_lint.lint_file(d / "001-funnel.md") == []
    assert insight_lint.lint_file(d / "001-funnel.md", survey=False) == []
