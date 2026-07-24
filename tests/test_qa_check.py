# qa_check.py contract tests.
#
# Section 1 ports EVERY contract from the retired tests/test_runtime_check.py
# unchanged (same fixtures, same assertions) against the new module's LEGACY
# path (no `qa:` block) — legacy parity must be byte-for-byte behavioural.
#
# Section 2+ cover the new QA path: verdict judgment (pass/fail/unable/
# missing/malformed), the category gate, MCP autogen (default + headless off
# + operator-supplied override), and stale-verdict cleanup.
#
# Run: python3 -m pytest tests/test_qa_check.py -v   (from orbit/)
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "engine" / "qa_check.py"


def target(tmp_path, extra_cfg="", category="frontend"):
    ap = tmp_path / "repo" / ".autopilot"
    (ap / "state" / "reviews").mkdir(parents=True)
    (ap / "config.yaml").write_text(
        'repo: "."\nbase_branch: "main"\ngates: {t: {cmd: "true"}}\n' + extra_cfg)
    (ap / "state" / "queue.json").write_text(json.dumps({"tasks": [
        {"id": "t1", "task": "x", "category": category}]}))
    return tmp_path / "repo"


def run(repo, agent_cmd=None, qa_agent_cmd=None, tid="t1"):
    env = dict(os.environ, AP_HOME=str(repo / ".autopilot"),
               AP_STATE=str(repo / ".autopilot" / "state"), ORBIT_HOME=str(ROOT))
    env.pop("ORBIT_RUNTIME_CMD", None)
    env.pop("ORBIT_QA_CMD", None)
    if agent_cmd:
        env["ORBIT_RUNTIME_CMD"] = agent_cmd
    if qa_agent_cmd:
        env["ORBIT_QA_CMD"] = qa_agent_cmd
    return subprocess.run([sys.executable, str(CHECK), tid, str(repo)],
                          capture_output=True, text=True, env=env)


# ===========================================================================
# Section 1 — ported legacy contract (tests/test_runtime_check.py, verbatim
# behaviour). Only the target script + a couple of unused knobs changed.
# ===========================================================================

RUNTIME_CFG = textwrap.dedent("""\
    runtime_check:
      categories:
        frontend: required
        feature: evidence
    """)


def legacy_agent(tmp_path, verdict):
    """Fake legacy agent: writes the verdict json (+ evidence md) and counts runs."""
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


def legacy_agent_ran(repo):
    f = repo / ".autopilot" / "state" / "reviews" / "agent-runs.txt"
    return f.exists()


def test_no_runtime_config_is_a_noop(tmp_path):
    repo = target(tmp_path)
    r = run(repo, legacy_agent(tmp_path, {"ran": True, "contradicts": True}))
    assert r.returncode == 0
    assert not legacy_agent_ran(repo)              # feature off → agent never invoked


def test_unlisted_category_is_skipped(tmp_path):
    repo = target(tmp_path, RUNTIME_CFG, category="documentation")
    r = run(repo, legacy_agent(tmp_path, {"ran": True, "contradicts": True}))
    assert r.returncode == 0
    assert not legacy_agent_ran(repo)


def test_required_contradiction_escalates(tmp_path):
    repo = target(tmp_path, RUNTIME_CFG)
    r = run(repo, legacy_agent(tmp_path, {"ran": True, "contradicts": True,
                                          "summary": "button never renders"}))
    assert r.returncode == 3
    assert "contradicts=True" in r.stdout


def test_evidence_mode_never_blocks(tmp_path):
    repo = target(tmp_path, RUNTIME_CFG, category="feature")
    r = run(repo, legacy_agent(tmp_path, {"ran": True, "contradicts": True}))
    assert r.returncode == 0


def test_unable_to_run_is_never_a_failure(tmp_path):
    repo = target(tmp_path, RUNTIME_CFG)
    r = run(repo, legacy_agent(tmp_path, {"ran": False, "contradicts": False,
                                          "summary": "dev server would not boot"}))
    assert r.returncode == 0


def test_missing_verdict_ships_with_a_warning(tmp_path):
    repo = target(tmp_path, RUNTIME_CFG)
    r = run(repo, f"{sys.executable} -c 'pass'")   # agent writes nothing
    assert r.returncode == 0
    assert "no verdict" in r.stdout


def test_runtime_evidence_lands_in_the_review_packet(tmp_path, monkeypatch):
    import importlib.util
    repo = target(tmp_path, RUNTIME_CFG)
    st = repo / ".autopilot" / "state"
    run(repo, legacy_agent(tmp_path, {"ran": True, "contradicts": False, "summary": "ok"}))
    # a ship this small has no worktree here — packet still renders the section
    monkeypatch.setenv("AP_STATE", str(st))
    spec = importlib.util.spec_from_file_location("review_packet",
                                                  ROOT / "engine" / "review_packet.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    out = rp.build("t1", str(repo), "autopilot/task-t1", "main")
    text = out.read_text()
    assert "## Runtime evidence" in text and "drove the flow" in text


# ===========================================================================
# Section 2 — QA path: verdict judgment + category gate.
# ===========================================================================

QA_CFG = textwrap.dedent("""\
    qa:
      gate_categories:
        frontend: required
        feature: evidence
    """)


def qa_agent(tmp_path, verdict, mcp_log=None, name="qa_agent"):
    """Fake QA agent: counts runs, optionally records the resolved
    QA_MCP_CONFIG env var (the ORBIT_QA_CMD override replaces argv wholesale,
    same as ORBIT_RUNTIME_CMD, so the resolved MCP path is only reliably
    observable via env — not argv — under override), and (if verdict is not
    None) writes the qa verdict json."""
    vf = tmp_path / f"{name}-verdict.json"
    vf.write_text(json.dumps(verdict))
    lines = [
        "import os, shutil",
        "from pathlib import Path",
        'tid = os.environ["TASK_ID"]',
        'rv = Path(os.environ["AP_STATE"]) / "reviews"',
        "rv.mkdir(parents=True, exist_ok=True)",
        'runs = rv / "qa-agent-runs.txt"',
        'runs.write_text((runs.read_text() if runs.exists() else "") + tid + "\\n")',
    ]
    if mcp_log is not None:
        lines.append(f'Path({str(mcp_log)!r}).write_text(os.environ.get("QA_MCP_CONFIG", ""))')
    lines.append(f'shutil.copy({str(vf)!r}, rv / ("task-" + tid + "-qa.json"))')
    script = tmp_path / f"{name}.py"
    script.write_text("\n".join(lines) + "\n")
    return f"{sys.executable} {script}"


def qa_agent_raw_verdict(tmp_path, raw_text, name="qa_agent_raw"):
    """Fake QA agent that writes literal (possibly malformed) text as the verdict."""
    script = tmp_path / f"{name}.py"
    script.write_text(textwrap.dedent(f"""\
        import os
        from pathlib import Path
        tid = os.environ["TASK_ID"]
        rv = Path(os.environ["AP_STATE"]) / "reviews"
        rv.mkdir(parents=True, exist_ok=True)
        (rv / ("task-" + tid + "-qa.json")).write_text({raw_text!r})
        """))
    return f"{sys.executable} {script}"


def qa_agent_ran(repo):
    f = repo / ".autopilot" / "state" / "reviews" / "qa-agent-runs.txt"
    return f.exists()


def test_qa_pass_verdict_ships(tmp_path):
    repo = target(tmp_path, QA_CFG)
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "pass", "contradicts": False, "summary": "all good"}))
    assert r.returncode == 0
    assert qa_agent_ran(repo)


def test_qa_fail_required_escalates(tmp_path):
    repo = target(tmp_path, QA_CFG)                 # t1 is category "frontend" → required
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True,
        "summary": "login button contradicted"}))
    assert r.returncode == 3
    assert "verdict=fail" in r.stdout


def test_qa_fail_evidence_never_blocks(tmp_path):
    repo = target(tmp_path, QA_CFG, category="feature")   # feature: evidence
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True, "summary": "seen broken"}))
    assert r.returncode == 0


def test_qa_unable_never_blocks(tmp_path):
    repo = target(tmp_path, QA_CFG)
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": False, "verdict": "unable", "contradicts": False,
        "summary": "the dev server never came up"}))
    assert r.returncode == 0


def test_qa_missing_verdict_ships_with_a_warning(tmp_path):
    repo = target(tmp_path, QA_CFG)
    r = run(repo, qa_agent_cmd=f"{sys.executable} -c 'pass'")   # agent writes nothing
    assert r.returncode == 0
    assert "no verdict" in r.stdout
    assert "WARN" in r.stdout


def test_qa_malformed_verdict_ships_with_a_warning(tmp_path):
    repo = target(tmp_path, QA_CFG)
    r = run(repo, qa_agent_cmd=qa_agent_raw_verdict(tmp_path, "{not valid json"))
    assert r.returncode == 0
    assert "WARN" in r.stdout


def test_qa_category_not_gated_skips_agent(tmp_path):
    repo = target(tmp_path, QA_CFG, category="documentation")   # not in gate_categories
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True, "summary": "would have escalated"}))
    assert r.returncode == 0
    assert not qa_agent_ran(repo)                    # never invoked


# ===========================================================================
# Section 3 — MCP autogen.
# ===========================================================================

def test_qa_mcp_autogen_default(tmp_path):
    repo = target(tmp_path, QA_CFG)
    mcp_log = tmp_path / "mcp.txt"
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "pass", "contradicts": False, "summary": "ok"}, mcp_log=mcp_log))
    assert r.returncode == 0

    generated = repo / ".autopilot" / "state" / "qa" / "mcp-run.json"
    assert generated.exists()
    cfg = json.loads(generated.read_text())
    args = cfg["mcpServers"]["playwright"]["args"]
    assert cfg["mcpServers"]["playwright"]["command"] == "npx"
    assert "--browser" in args and args[args.index("--browser") + 1] == "chromium"
    assert "--output-dir" in args
    out_dir = args[args.index("--output-dir") + 1]
    assert out_dir.endswith(str(Path("qa") / "task-t1"))
    assert "--headless" in args                     # default headless: true
    assert "--isolated" in args

    assert mcp_log.read_text() == str(generated)


def test_qa_mcp_autogen_headless_false(tmp_path):
    cfg = QA_CFG + "  headless: false\n"
    repo = target(tmp_path, cfg)
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "pass", "contradicts": False, "summary": "ok"}))
    assert r.returncode == 0

    generated = repo / ".autopilot" / "state" / "qa" / "mcp-run.json"
    args = json.loads(generated.read_text())["mcpServers"]["playwright"]["args"]
    assert "--headless" not in args


def test_qa_mcp_config_override_used(tmp_path):
    custom = tmp_path / "custom-mcp.json"
    custom.write_text(json.dumps({"mcpServers": {"playwright": {"command": "echo"}}}))
    cfg = QA_CFG + f"  mcp_config: {json.dumps(str(custom))}\n"
    repo = target(tmp_path, cfg)
    mcp_log = tmp_path / "mcp2.txt"
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "pass", "contradicts": False, "summary": "ok"}, mcp_log=mcp_log))
    assert r.returncode == 0

    generated = repo / ".autopilot" / "state" / "qa" / "mcp-run.json"
    assert not generated.exists()                    # nothing autogenerated

    assert mcp_log.read_text() == str(custom)


# ===========================================================================
# Section 4 — stale verdict cleanup.
# ===========================================================================

def test_qa_stale_verdict_removed_before_run(tmp_path):
    repo = target(tmp_path, QA_CFG)
    reviews = repo / ".autopilot" / "state" / "reviews"
    stale = reviews / "task-t1-qa.json"
    stale.write_text(json.dumps({"ran": True, "verdict": "fail", "contradicts": True,
                                 "summary": "stale from a prior run"}))
    r = run(repo, qa_agent_cmd=f"{sys.executable} -c 'pass'")   # agent writes nothing this run
    assert r.returncode == 0                          # missing-verdict path, not the stale fail
    assert not stale.exists()
