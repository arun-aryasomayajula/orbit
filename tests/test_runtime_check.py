# Slice-3 contract (runtime verification): only an OBSERVED contradiction on a
# category the operator marked `required` blocks a ship (exit 3); everything
# else — no config, unlisted category, evidence mode, unable-to-run, missing
# verdict — ships (exit 0). Evidence lands in the review packet.
#
# Run: python3 -m pytest tests/test_runtime_check.py -v   (from orbit/)
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "engine" / "runtime_check.py"


def target(tmp_path, runtime_cfg=""):
    ap = tmp_path / "repo" / ".autopilot"
    (ap / "state" / "reviews").mkdir(parents=True)
    (ap / "config.yaml").write_text(
        'repo: "."\nbase_branch: "main"\ngates: {t: {cmd: "true"}}\n' + runtime_cfg)
    (ap / "state" / "queue.json").write_text(json.dumps({"tasks": [
        {"id": "t1", "task": "x", "category": "frontend"}]}))
    return tmp_path / "repo"


RUNTIME_CFG = textwrap.dedent("""\
    runtime_check:
      categories:
        frontend: required
        feature: evidence
    """)


def agent(tmp_path, verdict):
    """Fake runtime agent: writes the verdict json (+ evidence md) and counts runs."""
    vf = tmp_path / "verdict.json"
    vf.write_text(json.dumps(verdict))
    script = tmp_path / "agent.py"
    script.write_text(textwrap.dedent(f"""\
        import os, shutil
        from pathlib import Path
        tid = os.environ["TASK_ID"]
        rv = Path(os.environ["AP_STATE"]) / "reviews"
        rv.mkdir(parents=True, exist_ok=True)
        runs = rv / "agent-runs.txt"
        runs.write_text((runs.read_text() if runs.exists() else "") + tid + "\\n")
        shutil.copy({str(vf)!r}, rv / ("task-" + tid + "-runtime.json"))
        (rv / ("task-" + tid + "-runtime.md")).write_text("drove the flow; saw it render")
        """))
    return f"{sys.executable} {script}"


def run(repo, agent_cmd=None, tid="t1"):
    env = dict(os.environ, AP_HOME=str(repo / ".autopilot"),
               AP_STATE=str(repo / ".autopilot" / "state"), ORBIT_HOME=str(ROOT))
    env.pop("ORBIT_RUNTIME_CMD", None)
    if agent_cmd:
        env["ORBIT_RUNTIME_CMD"] = agent_cmd
    return subprocess.run([sys.executable, str(CHECK), tid, str(repo)],
                          capture_output=True, text=True, env=env)


def agent_ran(repo):
    f = repo / ".autopilot" / "state" / "reviews" / "agent-runs.txt"
    return f.exists()


def test_no_runtime_config_is_a_noop(tmp_path):
    repo = target(tmp_path)
    r = run(repo, agent(tmp_path, {"ran": True, "contradicts": True}))
    assert r.returncode == 0
    assert not agent_ran(repo)                    # feature off → agent never invoked


def test_unlisted_category_is_skipped(tmp_path):
    repo = target(tmp_path, RUNTIME_CFG)
    q = repo / ".autopilot" / "state" / "queue.json"
    q.write_text(json.dumps({"tasks": [{"id": "t1", "category": "documentation"}]}))
    r = run(repo, agent(tmp_path, {"ran": True, "contradicts": True}))
    assert r.returncode == 0
    assert not agent_ran(repo)


def test_required_contradiction_escalates(tmp_path):
    repo = target(tmp_path, RUNTIME_CFG)
    r = run(repo, agent(tmp_path, {"ran": True, "contradicts": True,
                                   "summary": "button never renders"}))
    assert r.returncode == 3
    assert "contradicts=True" in r.stdout


def test_evidence_mode_never_blocks(tmp_path):
    repo = target(tmp_path, RUNTIME_CFG)
    q = repo / ".autopilot" / "state" / "queue.json"
    q.write_text(json.dumps({"tasks": [{"id": "t1", "category": "feature"}]}))
    r = run(repo, agent(tmp_path, {"ran": True, "contradicts": True}))
    assert r.returncode == 0


def test_unable_to_run_is_never_a_failure(tmp_path):
    repo = target(tmp_path, RUNTIME_CFG)
    r = run(repo, agent(tmp_path, {"ran": False, "contradicts": False,
                                   "summary": "dev server would not boot"}))
    assert r.returncode == 0


def test_missing_verdict_ships_with_a_warning(tmp_path):
    repo = target(tmp_path, RUNTIME_CFG)
    r = run(repo, f"{sys.executable} -c 'pass'")   # agent writes nothing
    assert r.returncode == 0
    assert "no verdict" in r.stdout


def test_runtime_evidence_lands_in_the_review_packet(tmp_path, monkeypatch):
    repo = target(tmp_path, RUNTIME_CFG)
    st = repo / ".autopilot" / "state"
    run(repo, agent(tmp_path, {"ran": True, "contradicts": False, "summary": "ok"}))
    # a ship this small has no worktree here — packet still renders the section
    monkeypatch.setenv("AP_STATE", str(st))
    spec = importlib.util.spec_from_file_location("review_packet",
                                                  ROOT / "engine" / "review_packet.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    out = rp.build("t1", str(repo), "autopilot/task-t1", "main")
    text = out.read_text()
    assert "## Runtime evidence" in text and "drove the flow" in text
