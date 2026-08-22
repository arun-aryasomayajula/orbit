# The metric-watch adapter contract (see backlog_to_tasks.run_source_adapters
# and adapters/metric_watch_to_backlog.py): reads the profile's `watches:` list
# + $AP_STATE/watches.json, files ONE `status: proposed` task per watch past
# cadence (id `watch-<name>-<YYYYMMDD>` UTC), and always advances the watch's
# state - even when the dated id it would file already exists in the backlog.
#
# Run: python3 -m pytest tests/test_metric_watch.py -v   (from orbit/)
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ORBIT = Path(__file__).resolve().parent.parent
ADAPTER = ORBIT / "adapters" / "metric_watch_to_backlog.py"

WATCHES_CFG = (
    'repo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n'
    'sources: [backlog, metric_watch]\n'
    'watches:\n'
    '  - name: wow-signup\n'
    '    question: "What changed WoW in signup conversion?"\n'
    '    cadence_hours: 24\n'
    '    category: funnel\n'
)


def _target(tmp_path, cfg_text=WATCHES_CFG):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text(cfg_text)
    (ap / "backlog.yaml").write_text("tasks: []\n")
    return tmp_path


def _run(target, extra_env=None):
    env = dict(os.environ, AP_HOME=str(target / ".autopilot"),
              AP_STATE=str(target / ".autopilot" / "state"), ORBIT_HOME=str(ORBIT))
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, str(ADAPTER)], cwd=str(target),
                          capture_output=True, text=True, env=env)


def _backlog_tasks(target):
    doc = yaml.safe_load((target / ".autopilot" / "backlog.yaml").read_text()) or {}
    return doc.get("tasks") or []


def _state(target):
    p = target / ".autopilot" / "state" / "watches.json"
    return json.loads(p.read_text()) if p.exists() else {}


def test_first_run_files_a_proposed_task(tmp_path):
    t = _target(tmp_path)
    r = _run(t)
    assert r.returncode == 0, r.stderr

    tasks = _backlog_tasks(t)
    assert len(tasks) == 1
    task = tasks[0]
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    assert task["id"] == f"watch-wow-signup-{today}"
    assert task["status"] == "proposed"
    assert task["autopilot"] == "human"
    assert task["category"] == "funnel"
    assert task["title"] == "What changed WoW in signup conversion?"
    assert any("insight_lint" in a for a in task["acceptance_criteria"])

    state = _state(t)
    assert "wow-signup" in state


def test_second_run_within_cadence_files_nothing(tmp_path):
    t = _target(tmp_path)
    _run(t)
    before = _backlog_tasks(t)
    r = _run(t)
    assert r.returncode == 0, r.stderr
    after = _backlog_tasks(t)
    assert after == before
    assert len(after) == 1


def test_past_cadence_files_a_new_dated_id(tmp_path):
    """Freeze "now" across two runs a day apart (cadence_hours: 24) so the
    second fire computes a genuinely different dated id from the first."""
    t = _target(tmp_path)
    r1 = _run(t, {"ORBIT_METRIC_WATCH_NOW": "2026-01-01T00:00:00+00:00"})
    assert r1.returncode == 0, r1.stderr
    r2 = _run(t, {"ORBIT_METRIC_WATCH_NOW": "2026-01-02T01:00:00+00:00"})
    assert r2.returncode == 0, r2.stderr

    tasks = _backlog_tasks(t)
    assert len(tasks) == 2
    ids = {tk["id"] for tk in tasks}
    assert ids == {"watch-wow-signup-20260101", "watch-wow-signup-20260102"}

    state = _state(t)
    assert state["wow-signup"] == "2026-01-02T01:00:00+00:00"


def test_state_advances_even_when_id_already_exists(tmp_path):
    """Sub-daily cadence past due twice in one UTC day: the second attempt
    tries to file the SAME dated id (already in the backlog from the first
    attempt) - it is deduped, but the watch's last_filed clock must still
    advance."""
    cfg = ('repo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n'
          'sources: [backlog, metric_watch]\n'
          'watches:\n  - name: hourly-check\n    question: "Any spike?"\n'
          '    cadence_hours: 1\n')
    t = _target(tmp_path, cfg)
    r1 = _run(t, {"ORBIT_METRIC_WATCH_NOW": "2026-01-01T00:00:00+00:00"})
    assert r1.returncode == 0, r1.stderr
    tasks_before = _backlog_tasks(t)
    assert len(tasks_before) == 1

    # 2 hours later, same UTC day, past the 1h cadence - same dated id.
    r2 = _run(t, {"ORBIT_METRIC_WATCH_NOW": "2026-01-01T02:00:00+00:00"})
    assert r2.returncode == 0, r2.stderr
    tasks_after = _backlog_tasks(t)
    assert len(tasks_after) == 1        # deduped by id - no new task landed

    state = _state(t)
    assert state["hourly-check"] == "2026-01-01T02:00:00+00:00"   # clock still advanced


def test_corrupt_state_file_is_tolerated(tmp_path):
    t = _target(tmp_path)
    state_path = t / ".autopilot" / "state" / "watches.json"
    state_path.write_text("{not valid json")

    r = _run(t)
    assert r.returncode == 0, r.stderr
    assert "corrupt" in r.stdout.lower() or "unreadable" in r.stdout.lower() \
        or "corrupt" in r.stderr.lower() or "unreadable" in r.stderr.lower()
    tasks = _backlog_tasks(t)
    assert len(tasks) == 1              # started fresh - still files the watch


def test_missing_state_file_is_tolerated(tmp_path):
    t = _target(tmp_path)
    r = _run(t)
    assert r.returncode == 0, r.stderr
    assert len(_backlog_tasks(t)) == 1


def test_no_watches_configured_is_a_noop(tmp_path):
    t = _target(tmp_path, 'repo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n'
                          'sources: [backlog, metric_watch]\n')
    r = _run(t)
    assert r.returncode == 0, r.stderr
    assert _backlog_tasks(t) == []


def test_malformed_watch_entry_is_rejected_loudly_not_crashed(tmp_path):
    cfg = ('repo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n'
          'sources: [backlog, metric_watch]\n'
          'watches:\n  - name: "Not A Slug!"\n    question: "q?"\n')
    t = _target(tmp_path, cfg)
    r = _run(t)
    assert r.returncode == 0, r.stderr
    # Every WARN goes to stderr, so a caller can read stdout as the report and
    # stderr as the problems - the bad-cadence warning below already did.
    assert "WARN" in r.stderr
    assert "WARN" not in r.stdout
    assert _backlog_tasks(t) == []


def test_bad_cadence_skips_only_that_watch_others_still_process(tmp_path):
    """A non-coercible cadence_hours (e.g. a typo'd string) on one watch must
    never poison the run: earlier watches still file and their state still
    persists, later watches still get processed, and the process exits 0
    with a warning on stderr - never an unhandled crash mid-loop."""
    cfg = ('repo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n'
          'sources: [backlog, metric_watch]\n'
          'watches:\n'
          '  - name: first-watch\n    question: "q1?"\n'
          '  - name: bad-cadence\n    question: "q2?"\n    cadence_hours: "weekly"\n'
          '  - name: third-watch\n    question: "q3?"\n')
    t = _target(tmp_path, cfg)
    r = _run(t)
    assert r.returncode == 0, r.stderr
    assert "WARN" in r.stderr and "bad-cadence" in r.stderr

    tasks = _backlog_tasks(t)
    ids = {tk["id"] for tk in tasks}
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    assert f"watch-first-watch-{today}" in ids     # entry 1 still filed
    assert f"watch-third-watch-{today}" in ids     # entry 3 still processed
    assert not any(i.startswith("watch-bad-cadence-") for i in ids)

    state = _state(t)
    assert "first-watch" in state                  # entry 1's state persisted
    assert "third-watch" in state
    assert "bad-cadence" not in state               # the skipped entry never advances


def test_dry_run_does_not_touch_backlog_or_state(tmp_path):
    t = _target(tmp_path)
    r = subprocess.run([sys.executable, str(ADAPTER), "--dry-run"], cwd=str(t),
                       capture_output=True, text=True,
                       env=dict(os.environ, AP_HOME=str(t / ".autopilot"),
                               AP_STATE=str(t / ".autopilot" / "state"), ORBIT_HOME=str(ORBIT)))
    assert r.returncode == 0, r.stderr
    assert _backlog_tasks(t) == []
    assert not (t / ".autopilot" / "state" / "watches.json").exists()


def test_runs_via_run_source_adapters(tmp_path):
    """The full contract path: backlog_to_tasks.py folds `metric_watch` in
    exactly the way run_source_adapters documents (cwd + env)."""
    t = _target(tmp_path)
    env = dict(os.environ, AP_HOME=str(t / ".autopilot"),
              AP_STATE=str(t / ".autopilot" / "state"))
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "backlog_to_tasks.py"), "--dry-run"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "metric_watch" in r.stdout
