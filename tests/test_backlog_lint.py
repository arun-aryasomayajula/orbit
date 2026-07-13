# The contract gate: a task with no acceptance_criteria must HARD-fail (held from
# the queue) — the verifier has nothing to grade and the maker has no contract.
# Criteria present → no hard failure. lint_ok waives the hard gate.
#
# Run: python3 -m pytest tests/test_backlog_lint.py -v   (from orbit/)
import importlib.util
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"
_spec = importlib.util.spec_from_file_location("backlog_lint", ENGINE / "backlog_lint.py")
backlog_lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backlog_lint)


def _task(**kw):
    base = {"id": "t1", "title": "a reasonably long task title here",
            "category": "feature", "context": "x" * 60}
    base.update(kw)
    return base


def test_no_criteria_hard_fails():
    hard, soft = backlog_lint.lint_task(_task(acceptance_criteria=[]))
    assert any("no acceptance criteria" in h for h in hard)


def test_no_criteria_hard_even_with_long_context():
    # regression: long context used to downgrade this to a soft warning
    hard, _ = backlog_lint.lint_task(_task(acceptance_criteria=[], context="y" * 500))
    assert any("no acceptance criteria" in h for h in hard)


def test_criteria_present_no_hard():
    hard, soft = backlog_lint.lint_task(_task(acceptance_criteria=["does X", "test covers X"]))
    assert not hard


def test_single_criterion_is_soft_not_hard():
    hard, soft = backlog_lint.lint_task(_task(acceptance_criteria=["only one"]))
    assert not hard
    assert any("single acceptance criterion" in s for s in soft)


def test_lint_ok_waives_missing_criteria():
    hard, _ = backlog_lint.lint_task(_task(acceptance_criteria=[], lint_ok=True))
    assert hard == []
