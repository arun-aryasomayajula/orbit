# qa_run.py contract tests — `orbit qa <target> --flow <name>`: run ONE
# taught flow via the /orbit-qa-flow skill (Task 21) and record its verdict
# into the flow's library metadata (Task 20 format).
#
# Subprocess-driven (mirrors tests/test_qa_check.py's ORBIT_QA_CMD fakes):
# ORBIT_QA_RUN_CMD replaces the real `claude --print /orbit-qa-flow ...` call
# verbatim (own name, distinct from qa_check's ORBIT_QA_CMD, so fakes can
# tell the two hooks apart). A fake script writes the run verdict json (and,
# for a healed verdict, the heal candidate file) itself, the same way a real
# /orbit-qa-flow run would.
#
# The flow fixture is built with qa_teach.py's OWN writer functions
# (_write_flow / _update_flowmap) — this is deliberate: it guarantees the
# fixture matches Task 20's binding format exactly, not a hand-rolled
# approximation of it.
#
# Run: python3.11 -m pytest tests/test_qa_run.py -v   (from orbit/)
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
QA_RUN = ENGINE / "qa_run.py"

sys.path.insert(0, str(ENGINE))   # qa_teach.py does `from backlog_append import ...`
spec = importlib.util.spec_from_file_location("qa_teach", ENGINE / "qa_teach.py")
qa_teach = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa_teach)

QA_CFG = textwrap.dedent("""\
    qa:
      base_url: "http://example.test/app"
    """)


def repo(tmp_path, extra_cfg=QA_CFG):
    r = tmp_path / "repo"
    ap = r / ".autopilot"
    ap.mkdir(parents=True)
    (ap / "config.yaml").write_text(
        'repo: "."\nbase_branch: "main"\ngates: {t: {cmd: "true"}}\n' + extra_cfg)
    return r


def teach_flow(r, name="login-flow", flows_sub="qa/flows", intent="user can log in",
               criteria=("sees the dashboard",), entry_path="http://example.test/app"):
    """Build a taught-flow fixture directly with qa_teach.py's own writer
    functions, so it matches Task 20's binding format byte-for-byte."""
    ap = r / ".autopilot"
    flow_dir = ap / flows_sub / name
    spec_tmp = ap / "_tmp_steps.spec.ts"
    spec_tmp.parent.mkdir(parents=True, exist_ok=True)
    spec_tmp.write_text("// recorded steps\n")
    qa_teach._write_flow(flow_dir, name, entry_path, intent, list(criteria), spec_tmp)
    spec_tmp.unlink()
    flowmap = (ap / flows_sub).parent / "FLOWMAP.md"
    qa_teach._update_flowmap(flowmap, name, intent, entry_path, "never run")
    return flow_dir


def meta(flow_dir):
    return json.loads((flow_dir / "meta.json").read_text())


def flowmap_text(r, flows_sub="qa/flows"):
    return ((r / ".autopilot" / flows_sub).parent / "FLOWMAP.md").read_text()


def run(repo_path, flow_name, run_cmd=None, timeout_flag=None, extra_argv=(), timeout=30):
    env = dict(os.environ)
    env.pop("ORBIT_QA_RUN_CMD", None)
    if run_cmd:
        env["ORBIT_QA_RUN_CMD"] = run_cmd
    argv = [sys.executable, str(QA_RUN), str(repo_path), "--flow", flow_name]
    if timeout_flag is not None:
        argv += ["--timeout", str(timeout_flag)]
    argv += list(extra_argv)
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)


# ---------------------------------------------------------------------------
# Fake ORBIT_QA_RUN_CMD agents — each writes a run verdict (and, for a
# mcp_log request, records the resolved QA_MCP_CONFIG env var the same way
# test_qa_check.py's qa_agent() fixture does for ORBIT_QA_CMD).
# ---------------------------------------------------------------------------

def fake_agent(tmp_path, verdict=None, heal_file=None, heal_content=None,
               mcp_log=None, name="fake_qa_run_agent", sleep_before_write=0.0):
    """`verdict` (dict or None) is folded straight into the run-verdict json
    (flow/UTC-ts filename resolved by the fake itself from FLOW_NAME/AP_STATE
    env vars) — None means "write nothing" (simulates a missing verdict).
    `heal_file`/`heal_content`, if given, are written under
    $AP_STATE/qa/heal/ BEFORE the run verdict (mirrors the skill's own
    heal-then-verdict write order)."""
    lines = [
        "import json, os, time",
        "from datetime import datetime, timezone",
        "from pathlib import Path",
        'flow = os.environ["FLOW_NAME"]',
        'ap_state = Path(os.environ["AP_STATE"])',
    ]
    if mcp_log is not None:
        lines.append(f'Path({str(mcp_log)!r}).write_text(os.environ.get("QA_MCP_CONFIG", ""))')
    if heal_file is not None:
        lines += [
            '(ap_state / "qa" / "heal").mkdir(parents=True, exist_ok=True)',
            f'(ap_state / "qa" / "heal" / {heal_file!r}).write_text(json.dumps({heal_content!r}))',
        ]
    if sleep_before_write:
        lines.append(f"time.sleep({sleep_before_write})")
    if verdict is not None:
        lines += [
            '(ap_state / "qa" / "runs").mkdir(parents=True, exist_ok=True)',
            'ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%fZ")',
            f'v = {verdict!r}',
            'v.setdefault("flow", flow)',
            '(ap_state / "qa" / "runs" / f"{ts}-{flow}.json").write_text(json.dumps(v))',
        ]
    script = tmp_path / f"{name}.py"
    script.write_text("\n".join(lines) + "\n")
    return f"{sys.executable} {script}"


# ===========================================================================
# pass verdict
# ===========================================================================

def test_pass_verdict_exit_0_and_meta_updated(tmp_path):
    r = repo(tmp_path)
    flow_dir = teach_flow(r)
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "clean replay", "healed_candidate": None}))
    assert result.returncode == 0, result.stdout + result.stderr

    m = meta(flow_dir)
    assert m["runs"] == 1
    assert m["last_verdict"] == "pass"
    assert m["last_run_at"]

    fm = flowmap_text(r)
    assert "| login-flow |" in fm
    row = [ln for ln in fm.splitlines() if ln.startswith("| login-flow |")][0]
    assert row.rstrip().endswith("| pass |")
    assert "user can log in" in row          # other cells (intent, etc.) untouched
    assert "http://example.test/app" in row


def test_second_run_increments_runs_to_2(tmp_path):
    r = repo(tmp_path)
    flow_dir = teach_flow(r)
    agent = fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "clean replay", "healed_candidate": None})
    r1 = run(r, "login-flow", run_cmd=agent)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    r2 = run(r, "login-flow", run_cmd=agent)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert meta(flow_dir)["runs"] == 2


# ===========================================================================
# healed verdict
# ===========================================================================

def test_healed_verdict_exit_0_mentions_candidate(tmp_path):
    r = repo(tmp_path)
    flow_dir = teach_flow(r)
    heal_name = "login-flow--2026-07-24T00-00-00Z.json"
    result = run(r, "login-flow", run_cmd=fake_agent(
        tmp_path,
        verdict={"verdict": "healed", "failed_step": None, "screenshot": None,
                 "summary": "button label moved", "healed_candidate": heal_name},
        heal_file=heal_name, heal_content={"flow": "login-flow", "reason": "moved"}))
    assert result.returncode == 0, result.stdout + result.stderr
    assert heal_name in result.stdout
    assert "pending approval" in result.stdout.lower()
    assert meta(flow_dir)["last_verdict"] == "healed"
    fm = flowmap_text(r)
    row = [ln for ln in fm.splitlines() if ln.startswith("| login-flow |")][0]
    assert row.rstrip().endswith("| healed |")


def test_healed_with_dangling_candidate_is_malformed_exit_1(tmp_path):
    r = repo(tmp_path)
    flow_dir = teach_flow(r)
    before = meta(flow_dir)
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, verdict={
        "verdict": "healed", "failed_step": None, "screenshot": None,
        "summary": "button label moved", "healed_candidate": "does-not-exist.json"}))
    assert result.returncode == 1, result.stdout + result.stderr
    # malformed verdict never persists into the flow's metadata
    assert meta(flow_dir) == before


def test_healed_candidate_path_traversal_rejected_exit_1(tmp_path):
    # A traversal candidate that resolves to a REAL file just outside
    # qa/heal/ must still be rejected — existence alone is not sufficient,
    # the resolved path must actually be contained under qa/heal/.
    r = repo(tmp_path)
    flow_dir = teach_flow(r)
    before = meta(flow_dir)
    outside = r / ".autopilot" / "state" / "qa" / "evil.json"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(json.dumps({"not": "a real heal candidate"}))
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, verdict={
        "verdict": "healed", "failed_step": None, "screenshot": None,
        "summary": "drift", "healed_candidate": "../evil.json"}))
    assert result.returncode == 1, result.stdout + result.stderr
    assert meta(flow_dir) == before


def test_healed_candidate_absolute_path_rejected_exit_1(tmp_path):
    r = repo(tmp_path)
    flow_dir = teach_flow(r)
    before = meta(flow_dir)
    outside = tmp_path / "evil-abs.json"
    outside.write_text(json.dumps({"not": "a real heal candidate"}))
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, verdict={
        "verdict": "healed", "failed_step": None, "screenshot": None,
        "summary": "drift", "healed_candidate": str(outside)}))
    assert result.returncode == 1, result.stdout + result.stderr
    assert meta(flow_dir) == before


# ===========================================================================
# fail verdict
# ===========================================================================

def test_fail_with_failed_step_exit_3(tmp_path):
    r = repo(tmp_path)
    flow_dir = teach_flow(r)
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, {
        "verdict": "fail", "failed_step": 2, "screenshot": "flow-login-flow/03-x.png",
        "summary": "login button 404s", "healed_candidate": None}))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "SEEN.md" in result.stdout
    assert meta(flow_dir)["last_verdict"] == "fail"
    fm = flowmap_text(r)
    row = [ln for ln in fm.splitlines() if ln.startswith("| login-flow |")][0]
    assert row.rstrip().endswith("| fail |")


def test_fail_without_failed_step_is_malformed_exit_1(tmp_path):
    r = repo(tmp_path)
    flow_dir = teach_flow(r)
    before = meta(flow_dir)
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, {
        "verdict": "fail", "failed_step": None, "screenshot": "x.png",
        "summary": "broke somewhere", "healed_candidate": None}))
    assert result.returncode == 1, result.stdout + result.stderr
    assert meta(flow_dir) == before


def test_fail_without_screenshot_is_malformed_exit_1(tmp_path):
    # skills/orbit-qa-flow.md: "verdict: 'fail' REQUIRES failed_step set AND
    # a screenshot of the failure — never fail on a hunch."
    r = repo(tmp_path)
    flow_dir = teach_flow(r)
    before = meta(flow_dir)
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, {
        "verdict": "fail", "failed_step": 1, "screenshot": None,
        "summary": "broke somewhere", "healed_candidate": None}))
    assert result.returncode == 1, result.stdout + result.stderr
    assert meta(flow_dir) == before


# ===========================================================================
# unable verdict
# ===========================================================================

def test_unable_exit_1_echoes_summary(tmp_path):
    r = repo(tmp_path)
    flow_dir = teach_flow(r)
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, {
        "verdict": "unable", "failed_step": None, "screenshot": None,
        "summary": "dev server never came up", "healed_candidate": None}))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "dev server never came up" in result.stdout
    assert meta(flow_dir)["last_verdict"] == "unable"    # meta/FLOWMAP updated on unable too
    fm = flowmap_text(r)
    row = [ln for ln in fm.splitlines() if ln.startswith("| login-flow |")][0]
    assert row.rstrip().endswith("| unable |")


def test_unable_with_failed_step_is_malformed_exit_1(tmp_path):
    # skills/orbit-qa-flow.md: "verdict: 'unable' NEVER pairs with a
    # failed_step — the environment, not a step, is what gave out."
    r = repo(tmp_path)
    flow_dir = teach_flow(r)
    before = meta(flow_dir)
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, {
        "verdict": "unable", "failed_step": 1, "screenshot": None,
        "summary": "env trouble mid-step", "healed_candidate": None}))
    assert result.returncode == 1, result.stdout + result.stderr
    assert meta(flow_dir) == before


# ===========================================================================
# no verdict / stale verdict
# ===========================================================================

def test_no_verdict_file_exit_1(tmp_path):
    r = repo(tmp_path)
    teach_flow(r)
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, verdict=None))
    assert result.returncode == 1, result.stdout + result.stderr


def test_stale_preexisting_verdict_ignored_exit_1(tmp_path):
    r = repo(tmp_path)
    flow_dir = teach_flow(r)
    runs_dir = r / ".autopilot" / "state" / "qa" / "runs"
    runs_dir.mkdir(parents=True)
    stale = runs_dir / "2000-01-01T00-00-00Z-login-flow.json"
    stale.write_text(json.dumps({
        "flow": "login-flow", "verdict": "pass", "failed_step": None,
        "screenshot": None, "summary": "ancient stale verdict", "healed_candidate": None}))
    # backdate the mtime well before "now" so it reads as stale regardless of
    # how fast this test runs
    old = time.time() - 3600
    os.utime(stale, (old, old))

    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, verdict=None))
    assert result.returncode == 1, result.stdout + result.stderr
    assert meta(flow_dir)["runs"] == 0                   # stale file never counted as this run


def fake_agent_writes_sibling_flow_verdict(tmp_path, other_flow, name="sibling_flow_agent"):
    """Writes a FRESH (mtime >= this run's start) verdict for `other_flow`,
    never for the flow actually requested — used to prove the filename-
    suffix glob (`*-<flow>.json`) alone is never trusted for identity: flow
    "app" suffix-matches a file like "<ts>-my-app.json" just as much as
    "<ts>-app.json" would."""
    script = tmp_path / f"{name}.py"
    script.write_text(textwrap.dedent(f"""\
        import json, os
        from datetime import datetime, timezone
        from pathlib import Path
        ap_state = Path(os.environ["AP_STATE"])
        runs = ap_state / "qa" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%fZ")
        v = {{"flow": {other_flow!r}, "verdict": "pass", "failed_step": None,
             "screenshot": None, "summary": "unrelated flow", "healed_candidate": None}}
        (runs / f"{{ts}}-{other_flow}.json").write_text(json.dumps(v))
        """))
    return f"{sys.executable} {script}"


def test_locate_verdict_ignores_suffix_matching_sibling_flow(tmp_path):
    r = repo(tmp_path)
    teach_flow(r, name="app")
    result = run(r, "app", run_cmd=fake_agent_writes_sibling_flow_verdict(tmp_path, "my-app"))
    # "app"'s own agent never wrote an "app" verdict — only a fresher,
    # filename-suffix-matching "my-app" one — so this must still read as
    # no-verdict-for-app, not silently pass off "my-app"'s outcome.
    assert result.returncode == 1, result.stdout + result.stderr


# ===========================================================================
# usage / validation errors -> exit 2
# ===========================================================================

def test_unknown_flow_exit_2_lists_available(tmp_path):
    r = repo(tmp_path)
    teach_flow(r, name="known-flow")
    result = run(r, "no-such-flow")
    assert result.returncode == 2
    assert "known-flow" in result.stdout + result.stderr


def test_no_qa_block_exit_2(tmp_path):
    r = repo(tmp_path, extra_cfg="")   # no qa: block at all
    result = run(r, "whatever")
    assert result.returncode == 2


def test_bad_slug_exit_2(tmp_path):
    r = repo(tmp_path)
    result = run(r, "Not_A_Slug!")
    assert result.returncode == 2


# ===========================================================================
# MCP autogen + override
# ===========================================================================

def test_mcp_autogen_writes_flow_output_dir(tmp_path):
    r = repo(tmp_path)
    teach_flow(r)
    mcp_log = tmp_path / "mcp.txt"
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}, mcp_log=mcp_log))
    assert result.returncode == 0, result.stdout + result.stderr

    generated = r / ".autopilot" / "state" / "qa" / "mcp-run.json"
    assert generated.exists()
    cfg = json.loads(generated.read_text())
    args = cfg["mcpServers"]["playwright"]["args"]
    out_dir = args[args.index("--output-dir") + 1]
    assert out_dir.endswith(str(Path("qa") / "flow-login-flow"))
    assert Path(out_dir).is_dir()
    assert mcp_log.read_text() == str(generated)


def test_mcp_config_override_skips_autogen(tmp_path):
    r = repo(tmp_path, QA_CFG + "  mcp_config: \"/tmp/custom-mcp.json\"\n")
    teach_flow(r)
    mcp_log = tmp_path / "mcp2.txt"
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}, mcp_log=mcp_log))
    assert result.returncode == 0, result.stdout + result.stderr
    generated = r / ".autopilot" / "state" / "qa" / "mcp-run.json"
    assert not generated.exists()
    assert mcp_log.read_text() == "/tmp/custom-mcp.json"
