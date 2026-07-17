# Dashboard side of the planning tier: /epic-action only moves along legal
# stage edges, approve flips the stage synchronously (human act, no model),
# non-epics are refused, and build_state carries the epics strip + keeps epics
# out of the Deep-space board.
#
# Run: python3 -m pytest tests/test_epic_dashboard.py -v   (from orbit/)
import importlib
import json
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"

BACKLOG = """tasks:
  - id: plain-bug
    title: "an ordinary proposed bug"
    category: bug
    autopilot: human
    status: proposed
    priority: medium
    acceptance_criteria: ["a", "b"]
  - id: epic-x
    title: "rework the pipeline"
    category: epic
    autopilot: human
    status: spec_ready
    priority: high
    acceptance_criteria: ["streams not batches", "no regressions"]
  - id: epic-x-1-child
    title: "first slice"
    category: feature
    autopilot: human
    status: proposed
    epic: epic-x
    acceptance_criteria: ["c", "d"]
"""


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    ap_home = tmp_path / ".autopilot"
    ap_state = ap_home / "state"
    ap_state.mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(ap_state))
    (ap_home / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n')
    (ap_home / "backlog.yaml").write_text(BACKLOG)
    (ap_home / "specs").mkdir()
    (ap_home / "specs" / "epic-x.md").write_text("# spec\n" + "x" * 300)
    (ap_state / "queue.json").write_text(json.dumps({"version": 2, "tasks": []}))

    sys.path.insert(0, str(ENGINE))
    sys.modules.pop("command_center", None)
    mod = importlib.import_module("command_center")
    importlib.reload(mod)
    yield mod
    sys.path.remove(str(ENGINE))


def test_epic_action_rejects_non_epics_and_bad_stages(cc):
    assert "not an epic" in cc.do_epic_action("plain-bug", "plan")
    assert "cannot decompose" in cc.do_epic_action("epic-x", "decompose")  # stage is spec_ready
    assert "unknown epic action" in cc.do_epic_action("epic-x", "merge")


def test_epic_action_approve_flips_stage_synchronously(cc):
    msg = cc.do_epic_action("epic-x", "approve")
    assert "approved" in msg.lower()
    import yaml
    tasks = {t["id"]: t for t in yaml.safe_load((Path(cc.AP_HOME) / "backlog.yaml").read_text())["tasks"]}
    assert tasks["epic-x"]["status"] == "approved"


def test_build_state_splits_epics_from_board(cc):
    s = cc.build_state()
    assert [e["id"] for e in s["epics"]] == ["epic-x"]
    e = s["epics"][0]
    assert e["stage"] == "spec_ready"
    assert e["has_spec"] is True
    assert e["children"] == {"total": 1, "proposed": 1, "queued": 0, "shipped": 0, "merged": 0}
    board_ids = [t["id"] for t in s["board"]]
    assert "epic-x" not in board_ids                      # epics live in the strip
    assert "plain-bug" in board_ids and "epic-x-1-child" in board_ids
    child = next(t for t in s["board"] if t["id"] == "epic-x-1-child")
    assert child["epic"] == "epic-x"                      # linkage surfaces on the card
