# The planning tier's guarantees: an epic is a container the loop can never
# pick (even forced); its stage machine only moves along legal edges; approve
# is purely human (no model call); decompose merges children as proposed/human
# with the epic linkage stamped by the WRAPPER, not trusted from the agent.
#
# Run: python3 -m pytest tests/test_epic_plan.py -v   (from orbit/)
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

_spec = importlib.util.spec_from_file_location("epic_plan", ENGINE / "epic_plan.py")
epic_plan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(epic_plan)

_spec2 = importlib.util.spec_from_file_location("backlog_append", ENGINE / "backlog_append.py")
ba = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(ba)

EPIC_BLOCK = """tasks:
  - id: big-one
    title: "rework the widget pipeline"
    category: epic
    autopilot: human
    status: proposed
    priority: high
    acceptance_criteria:
      - "widgets stream instead of batch"
      - "no regression in widget tests"
"""


def _target(tmp_path, backlog=EPIC_BLOCK):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n')
    (ap / "backlog.yaml").write_text(backlog)
    return tmp_path


def test_transitions_only_move_along_legal_edges():
    assert epic_plan.allowed("plan", "proposed")
    assert epic_plan.allowed("plan", "spec_ready")       # replan is allowed
    assert not epic_plan.allowed("plan", "approved")     # approved spec is frozen
    assert epic_plan.allowed("approve", "spec_ready")
    assert not epic_plan.allowed("approve", "proposed")  # nothing to approve yet
    assert not epic_plan.allowed("approve", "decomposed")
    assert epic_plan.allowed("decompose", "approved")
    assert not epic_plan.allowed("decompose", "spec_ready")  # approval gate is mandatory


def test_approve_flips_stage_without_a_model(tmp_path):
    t = _target(tmp_path)
    ep = epic_plan.Epic(t)
    ep.spec_path("big-one").parent.mkdir(parents=True)
    ep.spec_path("big-one").write_text("# spec\n" + "x" * 300)
    ba.set_task_field(ep.backlog, "big-one", "status", "spec_ready")
    assert epic_plan.do_approve(ep, "big-one") == 0
    assert ep.task("big-one")["status"] == "approved"
    reg = json.loads((t / ".autopilot" / "state" / "epics.json").read_text())
    assert reg["epics"]["big-one"]["stage"] == "approved"


def test_decompose_merges_children_with_wrapper_stamped_linkage(tmp_path, monkeypatch):
    t = _target(tmp_path)
    ep = epic_plan.Epic(t)
    ba.set_task_field(ep.backlog, "big-one", "status", "approved")
    ep.spec_path("big-one").parent.mkdir(parents=True)
    ep.spec_path("big-one").write_text("# spec\n" + "x" * 300)

    def fake_agent(command, tid, timeout):
        f = ep.state / "epics" / f"{tid}-children.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"proposals": [
            {"id": "big-one-1-stream", "title": "stream widgets", "category": "feature",
             "epic": "SPOOFED", "acceptance_criteria": ["a", "b"]},
            {"id": "big-one-2-tests", "title": "pin streaming", "category": "testing",
             "acceptance_criteria": ["c", "d"]},
        ]}))
        return True

    monkeypatch.setattr(epic_plan.Epic, "run_agent",
                        lambda self, command, tid, timeout: fake_agent(command, tid, timeout))
    ep2 = epic_plan.Epic(t)
    assert epic_plan.do_decompose(ep2, "big-one", 10) == 0
    tasks = {x["id"]: x for x in (yaml.safe_load(ep2.backlog.read_text()))["tasks"]}
    assert tasks["big-one"]["status"] == "decomposed"
    for cid in ("big-one-1-stream", "big-one-2-tests"):
        assert tasks[cid]["epic"] == "big-one"          # wrapper stamped, spoof ignored
        assert tasks[cid]["status"] == "proposed"
        assert tasks[cid]["autopilot"] == "human"


def test_decompose_with_no_children_falls_back_to_approved(tmp_path, monkeypatch):
    t = _target(tmp_path)
    ep = epic_plan.Epic(t)
    ba.set_task_field(ep.backlog, "big-one", "status", "approved")
    monkeypatch.setattr(epic_plan.Epic, "run_agent", lambda self, c, i, to: True)
    assert epic_plan.do_decompose(ep, "big-one", 10) == 1
    assert ep.task("big-one")["status"] == "approved"


def test_loop_never_emits_an_epic_even_forced(tmp_path):
    forced = EPIC_BLOCK.replace("status: proposed", "status: queued") \
                       .replace("autopilot: human", "autopilot: allow") + "    forced: true\n"
    t = _target(tmp_path, forced)
    env = dict(os.environ, AP_HOME=str(t / ".autopilot"), AP_STATE=str(t / ".autopilot" / "state"))
    r = subprocess.run([sys.executable, str(ENGINE / "backlog_to_tasks.py")],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    q = json.loads((t / ".autopilot" / "state" / "queue.json").read_text())
    assert q["tasks"] == []                              # held, not emitted
    assert "epic" in r.stdout


def test_set_task_field_flips_only_the_target_block(tmp_path):
    bl = tmp_path / "backlog.yaml"
    bl.write_text("""# keep this comment
tasks:
  - id: one
    status: proposed   # inline comment on another task
  - id: two
    status: proposed
""")
    assert ba.set_task_field(bl, "two", "status", "queued")
    doc = yaml.safe_load(bl.read_text())
    assert {t["id"]: t["status"] for t in doc["tasks"]} == {"one": "proposed", "two": "queued"}
    assert "# keep this comment" in bl.read_text()
    assert not ba.set_task_field(bl, "missing", "status", "queued")
