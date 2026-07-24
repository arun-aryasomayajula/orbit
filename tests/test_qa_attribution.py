# Task 28 — QA merge attribution: mirrors logwatch's post-ship attribution
# (tests/test_regression_attribution.py) for uitest-* tasks. A NEW `uitest-*`
# task whose SEEN.md `date` falls within 7 days AFTER a merge marker's `at`
# gets the SAME "POSSIBLE REGRESSION" context lines injected (byte-identical
# wording to logwatch's, for operator consistency) — suspicion, never a
# verdict. Pre-existing/older findings don't; repos with no markers behave
# exactly as before.
#
# Run: python3 -m pytest tests/test_qa_attribution.py -v   (from orbit/)
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ADAPTER = ROOT / "adapters" / "qa_to_backlog.py"

MARKER = {"task_id": "feat-csv", "at": "2026-07-10T09:00:00Z", "sha": "abc123",
          "branch": "autopilot/task-feat-csv", "pr_url": "https://github.com/x/r/pull/4",
          "patch": "/state/diffs/cycle-x.patch"}


def target(tmp_path, seen_rows, markers=None):
    repo = tmp_path / "repo"
    ap = repo / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "backlog.yaml").write_text("tasks: []\n")
    (repo / "ops" / "qa").mkdir(parents=True)
    table = "| date | page | type | signature | severity |\n|---|---|---|---|---|\n"
    table += "".join(f"| {d} | {p} | {t} | {s} | {sev} |\n" for d, p, t, s, sev in seen_rows)
    (repo / "ops" / "qa" / "SEEN.md").write_text(table)
    if markers:
        (ap / "state" / "merge_markers.jsonl").write_text(
            "".join(json.dumps(m) + "\n" for m in markers))
    return repo


def run(repo):
    env = dict(os.environ, AP_HOME=str(repo / ".autopilot"),
               AP_STATE=str(repo / ".autopilot" / "state"), ORBIT_HOME=str(ROOT))
    return subprocess.run([sys.executable, str(ADAPTER)], cwd=repo,
                          capture_output=True, text=True, env=env)


def tasks(repo):
    return (yaml.safe_load((repo / ".autopilot" / "backlog.yaml").read_text()) or {}).get("tasks") or []


def test_finding_after_merge_is_attributed(tmp_path):
    repo = target(tmp_path, [("2026-07-12", "/checkout", "bug", "submit button does nothing", "high")],
                  markers=[MARKER])
    r = run(repo)
    assert r.returncode == 0, r.stderr
    t = tasks(repo)[0]
    assert "POSSIBLE REGRESSION" in t["context"]
    assert "feat-csv" in t["context"] and "pull/4" in t["context"]
    assert "revert patch: /state/diffs/cycle-x.patch" in t["context"]
    assert t["status"] == "proposed"           # attribution never queues anything


def test_finding_before_merge_is_not_attributed(tmp_path):
    repo = target(tmp_path, [("2026-07-08", "/checkout", "bug", "old finding", "medium")],
                  markers=[MARKER])
    run(repo)
    assert "POSSIBLE REGRESSION" not in tasks(repo)[0]["context"]


def test_finding_outside_window_is_not_attributed(tmp_path):
    repo = target(tmp_path, [("2026-07-25", "/checkout", "bug", "much later finding", "low")],
                  markers=[MARKER])
    run(repo)
    assert "POSSIBLE REGRESSION" not in tasks(repo)[0]["context"]


def test_no_markers_file_behaves_as_before(tmp_path):
    repo = target(tmp_path, [("2026-07-12", "/checkout", "ux", "some finding", "medium")])
    r = run(repo)
    assert r.returncode == 0, r.stderr
    assert len(tasks(repo)) == 1
    assert "POSSIBLE REGRESSION" not in tasks(repo)[0]["context"]


def test_existing_task_is_not_reattributed(tmp_path):
    # Idempotency: a signature already ingested (present by id) must never be
    # rewritten — attribution only ever lands on a NEW task's first append.
    repo = target(tmp_path, [("2026-07-12", "/checkout", "bug", "submit button does nothing", "high")],
                  markers=[MARKER])
    r1 = run(repo)
    assert r1.returncode == 0, r1.stderr
    before = tasks(repo)
    assert len(before) == 1
    r2 = run(repo)
    assert r2.returncode == 0, r2.stderr
    assert "nothing new to add" in r2.stdout
    after = tasks(repo)
    assert after == before
