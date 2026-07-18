# Slice-4 contract (post-ship attribution): the dashboard's merge action drops
# a merge marker; a log signature FIRST SEEN after a recent merge gets that
# ship (id, branch, PR, revert patch) attributed in its proposed task's context
# — as suspicion, never a verdict. Pre-existing/older signatures don't; repos
# with no markers behave exactly as before.
#
# Run: python3 -m pytest tests/test_regression_attribution.py -v   (from orbit/)
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ADAPTER = ROOT / "adapters" / "logwatch_to_backlog.py"
sys.path.insert(0, str(ROOT / "engine"))


def target(tmp_path, seen_rows, markers=None):
    repo = tmp_path / "repo"
    ap = repo / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "backlog.yaml").write_text("tasks: []\n")
    (repo / "ops" / "logwatch").mkdir(parents=True)
    table = "| date | app | signature | count |\n|---|---|---|---|\n"
    table += "".join(f"| {d} | {a} | {s} | {c} |\n" for d, a, s, c in seen_rows)
    (repo / "ops" / "logwatch" / "SEEN.md").write_text(table)
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


MARKER = {"task_id": "feat-csv", "at": "2026-07-10T09:00:00Z", "sha": "abc123",
          "branch": "autopilot/task-feat-csv", "pr_url": "https://github.com/x/r/pull/4",
          "patch": "/state/diffs/cycle-x.patch"}


def test_signature_after_merge_is_attributed(tmp_path):
    repo = target(tmp_path, [("2026-07-12", "api", "NullPointer in CsvExport", "9")],
                  markers=[MARKER])
    r = run(repo)
    assert r.returncode == 0, r.stderr
    t = tasks(repo)[0]
    assert "POSSIBLE REGRESSION" in t["context"]
    assert "feat-csv" in t["context"] and "pull/4" in t["context"]
    assert "revert patch: /state/diffs/cycle-x.patch" in t["context"]
    assert t["status"] == "proposed"           # attribution never queues anything


def test_signature_before_merge_is_not_attributed(tmp_path):
    repo = target(tmp_path, [("2026-07-08", "api", "old error", "2")], markers=[MARKER])
    run(repo)
    assert "POSSIBLE REGRESSION" not in tasks(repo)[0]["context"]


def test_signature_outside_window_is_not_attributed(tmp_path):
    repo = target(tmp_path, [("2026-07-25", "api", "much later error", "2")], markers=[MARKER])
    run(repo)
    assert "POSSIBLE REGRESSION" not in tasks(repo)[0]["context"]


def test_no_markers_file_behaves_as_before(tmp_path):
    repo = target(tmp_path, [("2026-07-12", "api", "some error", "3")])
    r = run(repo)
    assert r.returncode == 0, r.stderr
    assert len(tasks(repo)) == 1
    assert "POSSIBLE REGRESSION" not in tasks(repo)[0]["context"]


def test_merge_action_writes_the_marker(tmp_path, monkeypatch):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "backlog.yaml").write_text("tasks: []\n")
    (ap / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates: {t: {cmd: "true"}}\n')
    (ap / "state" / "ledger.json").write_text(json.dumps({"entries": {"t1": {
        "state": "pushed", "sha": "abc123", "branch": "autopilot/task-t1",
        "pr_url": "https://github.com/x/r/pull/8", "patch": "/d/p.patch"}}}))
    monkeypatch.setenv("AP_HOME", str(ap))
    monkeypatch.setenv("AP_STATE", str(ap / "state"))
    spec = importlib.util.spec_from_file_location("command_center",
                                                  ROOT / "engine" / "command_center.py")
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    cc.do_mark("t1", "merged", "")
    lines = (ap / "state" / "merge_markers.jsonl").read_text().splitlines()
    m = json.loads(lines[0])
    assert m["task_id"] == "t1" and m["sha"] == "abc123"
    assert m["pr_url"].endswith("/pull/8") and m["patch"] == "/d/p.patch"
