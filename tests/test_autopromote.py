# engine/autopromote.py - the AUTO_PROMOTE flag's one-task-per-call feeder.
#
# The finding this pins: backlog_append writes EVERY machine-filed proposal as
# `autopilot: human`, so before this the unattended product path did not exist -
# a metric watch's weekly insight task sat on the board until someone clicked
# Promote, every week, for work they had already asked to happen on a cadence.
#
# Eligibility is now category + provenance (autopromote._eligible): an
# unattended signal adapter's task is auto-feedable; a hand-written one is not,
# including the dashboard's own New-task form, which also stamps a `source:`
# field (`dashboard`) and so must NOT be waved through by mere presence of it.
#
# Run: python3 -m pytest tests/test_autopromote.py -v   (from orbit/)
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ORBIT = Path(__file__).resolve().parent.parent
SCRIPT = ORBIT / "engine" / "autopromote.py"


def _task(tid, category, *, autopilot="human", source=None, priority="medium"):
    lines = [f"  - id: {tid}",
             '    title: "a task with a long enough title"',
             f"    category: {category}",
             f"    autopilot: {autopilot}",
             "    status: proposed",
             f"    priority: {priority}"]
    if source is not None:
        lines.append(f"    source: {source}")
    lines.append('    acceptance_criteria:')
    lines.append('      - "does the thing"')
    return "\n".join(lines) + "\n"


@pytest.fixture()
def target(tmp_path):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n')
    return tmp_path


def _write_backlog(target, *blocks):
    (target / ".autopilot" / "backlog.yaml").write_text("tasks:\n" + "".join(blocks))


def _run(target):
    ap = target / ".autopilot"
    env = dict(os.environ, AP_HOME=str(ap), AP_STATE=str(ap / "state"),
               ORBIT_HOME=str(ORBIT))
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=str(target),
                          capture_output=True, text=True, env=env)


def _tasks(target):
    doc = yaml.safe_load((target / ".autopilot" / "backlog.yaml").read_text()) or {}
    return {t["id"]: t for t in (doc.get("tasks") or [])}


# --- the unattended product path -------------------------------------------

def test_watch_filed_insight_task_is_promoted(target):
    """A metric_watch-filed task: `autopilot: human` (backlog_append writes
    nothing else) plus an unattended source. AUTO_PROMOTE covers it."""
    _write_backlog(target, _task("watch-wow-signup-20260101", "insight",
                                 source="metric_watch"))
    r = _run(target)
    assert r.returncode == 0, r.stderr
    t = _tasks(target)["watch-wow-signup-20260101"]
    assert t["status"] == "queued"
    assert t["autopilot"] == "allow"


@pytest.mark.parametrize("category", ["insight", "funnel", "hypothesis"])
def test_every_product_category_is_promotable(target, category):
    _write_backlog(target, _task(f"watch-x-{category}", category, source="metric_watch"))
    assert _run(target).returncode == 0
    assert _tasks(target)[f"watch-x-{category}"]["status"] == "queued"


# --- hand-written work stays the operator's call ----------------------------

def test_hand_written_human_task_is_never_promoted(target):
    """No source field at all - a task typed straight into backlog.yaml."""
    _write_backlog(target, _task("hand-written-insight", "insight"))
    r = _run(target)
    assert r.returncode == 0, r.stderr
    t = _tasks(target)["hand-written-insight"]
    assert t["status"] == "proposed"
    assert t["autopilot"] == "human"
    assert "no eligible" in r.stdout


def test_dashboard_filed_task_is_never_promoted(target):
    """The dashboard's New-task form stamps `source: dashboard`. Presence of a
    source field must not be what makes a task eligible - the operator who
    typed it can queue it in the same click."""
    _write_backlog(target, _task("typed-in-the-ui", "insight", source="dashboard"))
    r = _run(target)
    assert r.returncode == 0, r.stderr
    assert _tasks(target)["typed-in-the-ui"]["status"] == "proposed"
    assert "no eligible" in r.stdout


def test_escalation_answer_source_is_never_promoted(target):
    _write_backlog(target, _task("answered-escalation", "bug", source="escalation-answer"))
    assert _run(target).returncode == 0
    assert _tasks(target)["answered-escalation"]["status"] == "proposed"


# --- the pre-existing contract still holds ---------------------------------

def test_ungated_chore_task_still_promotes(target):
    """The original arm: `autopilot` is anything but human, so the operator
    already un-gated it. No source field needed."""
    _write_backlog(target, _task("add-missing-tests", "testing", autopilot="allow"))
    assert _run(target).returncode == 0
    assert _tasks(target)["add-missing-tests"]["status"] == "queued"


def test_risky_category_is_never_promoted(target):
    _write_backlog(target, _task("rewrite-the-core", "refactor", autopilot="allow"),
                   _task("watch-refactor-thing", "refactor", source="metric_watch"))
    r = _run(target)
    assert r.returncode == 0, r.stderr
    assert all(t["status"] == "proposed" for t in _tasks(target).values())


def test_one_task_per_call_highest_priority_first(target):
    _write_backlog(target,
                   _task("watch-low", "insight", source="metric_watch", priority="low"),
                   _task("watch-high", "insight", source="metric_watch", priority="high"))
    assert _run(target).returncode == 0
    tasks = _tasks(target)
    assert tasks["watch-high"]["status"] == "queued"
    assert tasks["watch-low"]["status"] == "proposed"      # one per call


def test_worked_and_skipped_ids_are_excluded(target):
    state = target / ".autopilot" / "state"
    (state / "ledger.json").write_text(json.dumps({"entries": {"watch-worked": {}}}))
    (state / "skips.txt").write_text("watch-skipped\n")
    _write_backlog(target,
                   _task("watch-worked", "insight", source="metric_watch"),
                   _task("watch-skipped", "insight", source="metric_watch"))
    r = _run(target)
    assert r.returncode == 0, r.stderr
    assert all(t["status"] == "proposed" for t in _tasks(target).values())
    assert "no eligible" in r.stdout
