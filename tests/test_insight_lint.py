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
