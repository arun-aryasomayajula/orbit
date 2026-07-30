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
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
QA_RUN = ENGINE / "qa_run.py"

sys.path.insert(0, str(ENGINE))   # qa_teach.py does `from backlog_append import ...`
spec = importlib.util.spec_from_file_location("qa_teach", ENGINE / "qa_teach.py")
qa_teach = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa_teach)

# Direct (in-process) import of qa_run.py itself — used ONLY for white-box
# unit tests of _release_lock's ownership check (below); every other test in
# this file drives the module as a real subprocess (QA_RUN).
_qa_run_spec = importlib.util.spec_from_file_location("qa_run_direct", QA_RUN)
qa_run = importlib.util.module_from_spec(_qa_run_spec)
_qa_run_spec.loader.exec_module(qa_run)

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
               criteria=("sees the dashboard",), entry_path="http://example.test/app",
               requires=(), provides_auth=False):
    """Build a taught-flow fixture directly with qa_teach.py's own writer
    functions, so it matches Task 20's binding format byte-for-byte.
    `requires`/`provides_auth` (test chaining, Task "prerequisites") default
    to empty/False so every existing call site is unaffected."""
    ap = r / ".autopilot"
    flow_dir = ap / flows_sub / name
    spec_tmp = ap / "_tmp_steps.spec.ts"
    spec_tmp.parent.mkdir(parents=True, exist_ok=True)
    spec_tmp.write_text("// recorded steps\n")
    qa_teach._write_flow(flow_dir, name, entry_path, intent, list(criteria), spec_tmp,
                         requires=list(requires), provides_auth=provides_auth)
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
               mcp_log=None, attach_log=None, chain_log=None, name="fake_qa_run_agent",
               sleep_before_write=0.0):
    """`verdict` (dict or None) is folded straight into the run-verdict json
    (flow/UTC-ts filename resolved by the fake itself from FLOW_NAME/AP_STATE
    env vars) — None means "write nothing" (simulates a missing verdict).
    `heal_file`/`heal_content`, if given, are written under
    $AP_STATE/qa/heal/ BEFORE the run verdict (mirrors the skill's own
    heal-then-verdict write order). `attach_log`, if given, records the
    ORBIT_QA_ATTACH env var this invocation actually saw (empty string when
    unset) — same env-capture mechanism as `mcp_log`'s own QA_MCP_CONFIG
    dump, used to prove --attach's env-var contract from outside the
    process. `chain_log` (test chaining, Task "prerequisites") is the same
    mechanism again, for ORBIT_QA_CHAIN."""
    lines = [
        "import json, os, time",
        "from datetime import datetime, timezone",
        "from pathlib import Path",
        'flow = os.environ["FLOW_NAME"]',
        'ap_state = Path(os.environ["AP_STATE"])',
    ]
    if mcp_log is not None:
        lines.append(f'Path({str(mcp_log)!r}).write_text(os.environ.get("QA_MCP_CONFIG", ""))')
    if attach_log is not None:
        lines.append(f'Path({str(attach_log)!r}).write_text(os.environ.get("ORBIT_QA_ATTACH", ""))')
    if chain_log is not None:
        lines.append(f'Path({str(chain_log)!r}).write_text(os.environ.get("ORBIT_QA_CHAIN", ""))')
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


def test_bad_flows_dir_exit_2(tmp_path):
    # Finding 1 (Phase 4 review): qa_teach.py already rejects a malformed
    # qa.flows_dir (absolute / '..'-escaping) with exit 2 — qa_run.py used
    # to resolve `ap_home / flows_dir` raw, so `ap_home / "/etc"` silently
    # collapsed to Path("/etc") instead of being refused. Teach-parity: same
    # check (_bad_flows_dir_reason, imported from qa_teach), same exit code,
    # same message shape.
    r = repo(tmp_path, extra_cfg=textwrap.dedent("""\
        qa:
          base_url: "http://example.test/app"
          flows_dir: "/abs"
        """))
    result = run(r, "whatever")
    assert result.returncode == 2
    assert "flows_dir" in result.stdout + result.stderr


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

    generated = r / ".autopilot" / "state" / "qa" / "mcp-run-flow-login-flow.json"
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
    generated = r / ".autopilot" / "state" / "qa" / "mcp-run-flow-login-flow.json"
    assert not generated.exists()
    assert mcp_log.read_text() == "/tmp/custom-mcp.json"


# ===========================================================================
# --attach (persistent QA app session, Task "persistent QA app session"):
# --flow-only flag that sets ORBIT_QA_ATTACH=1 in the child env instead of
# launching/killing qa.services itself — see module docstring's "Attach
# mode" section. Rejected (exit 2) combined with --regression/--explore.
# ===========================================================================

def test_attach_flag_sets_orbit_qa_attach_env_in_child(tmp_path):
    r = repo(tmp_path)
    teach_flow(r)
    attach_log = tmp_path / "attach.log"
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "attached replay", "healed_candidate": None},
        attach_log=attach_log), extra_argv=("--attach",))
    assert result.returncode == 0, result.stdout + result.stderr
    assert attach_log.read_text() == "1"


def test_without_attach_flag_orbit_qa_attach_env_is_unset(tmp_path):
    r = repo(tmp_path)
    teach_flow(r)
    attach_log = tmp_path / "attach.log"
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "clean replay", "healed_candidate": None},
        attach_log=attach_log))
    assert result.returncode == 0, result.stdout + result.stderr
    assert attach_log.read_text() == ""


def test_attach_with_regression_rejected_exit_2(tmp_path):
    r = repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(QA_RUN), str(r), "--regression", "--attach"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--attach" in (result.stdout + result.stderr)


def test_attach_with_explore_rejected_exit_2(tmp_path):
    r = repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(QA_RUN), str(r), "--explore", "--attach"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--attach" in (result.stdout + result.stderr)


# ===========================================================================
# Test chaining / prerequisites (Task "prerequisites"): --flow resolves a
# flow's own `requires` (meta.json) into a topologically-ordered chain and
# exports it as ORBIT_QA_CHAIN before invoking the skill. --regression does
# NOT resolve chains — out of scope here, see qa_run.py's own docstring.
# ===========================================================================

def test_no_requires_yields_single_element_chain_byte_neutral(tmp_path):
    r = repo(tmp_path)
    teach_flow(r, name="login-flow")   # default requires=()
    chain_log = tmp_path / "chain.log"
    result = run(r, "login-flow", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}, chain_log=chain_log))
    assert result.returncode == 0, result.stdout + result.stderr
    assert chain_log.read_text() == "login-flow"


def test_requires_login_yields_two_element_chain_in_order(tmp_path):
    r = repo(tmp_path)
    teach_flow(r, name="login", provides_auth=True)
    teach_flow(r, name="create-vertical", requires=["login"])
    chain_log = tmp_path / "chain.log"
    result = run(r, "create-vertical", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}, chain_log=chain_log))
    assert result.returncode == 0, result.stdout + result.stderr
    assert chain_log.read_text() == "login,create-vertical"


def test_three_deep_chain_orders_correctly(tmp_path):
    r = repo(tmp_path)
    teach_flow(r, name="login")
    teach_flow(r, name="add-to-cart", requires=["login"])
    teach_flow(r, name="checkout", requires=["add-to-cart"])
    chain_log = tmp_path / "chain.log"
    result = run(r, "checkout", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}, chain_log=chain_log))
    assert result.returncode == 0, result.stdout + result.stderr
    assert chain_log.read_text() == "login,add-to-cart,checkout"


def test_requires_cycle_exit_2_no_run(tmp_path):
    r = repo(tmp_path)
    teach_flow(r, name="flow-a", requires=["flow-b"])
    teach_flow(r, name="flow-b", requires=["flow-a"])
    calls_log = tmp_path / "should-not-run.log"
    result = run(r, "flow-a", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}, mcp_log=calls_log))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cycle" in (result.stdout + result.stderr).lower()
    assert not calls_log.exists()   # the agent was never invoked


def test_missing_prerequisite_exit_2_names_it_no_run(tmp_path):
    r = repo(tmp_path)
    teach_flow(r, name="create-vertical", requires=["ghost-login"])
    calls_log = tmp_path / "should-not-run.log"
    result = run(r, "create-vertical", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}, mcp_log=calls_log))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "ghost-login" in (result.stdout + result.stderr)
    assert not calls_log.exists()


def test_verdict_still_recorded_for_target_only_in_a_chain(tmp_path):
    # The prerequisite's own meta.json/FLOWMAP row must be untouched by a
    # chained run — only the target's own verdict is located/recorded.
    r = repo(tmp_path)
    login_dir = teach_flow(r, name="login")
    target_dir = teach_flow(r, name="create-vertical", requires=["login"])
    result = run(r, "create-vertical", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}))
    assert result.returncode == 0, result.stdout + result.stderr
    assert meta(target_dir)["runs"] == 1
    assert meta(target_dir)["last_verdict"] == "pass"
    assert meta(login_dir)["runs"] == 0            # prerequisite's own meta untouched
    assert meta(login_dir)["last_verdict"] is None


def test_update_meta_preserves_requires_and_provides_auth_across_a_verdict_write(tmp_path):
    r = repo(tmp_path)
    teach_flow(r, name="login")
    flow_dir = teach_flow(r, name="create-vertical", requires=["login"], provides_auth=False)
    before = meta(flow_dir)
    assert before["requires"] == ["login"]
    assert before["provides_auth"] is False
    result = run(r, "create-vertical", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}))
    assert result.returncode == 0, result.stdout + result.stderr
    after = meta(flow_dir)
    assert after["requires"] == ["login"]          # preserved, not dropped
    assert after["provides_auth"] is False
    assert after["runs"] == 1                      # the verdict update itself did happen


def test_diamond_dependency_dedupes_shared_prerequisite(tmp_path):
    # a requires [b, c]; b and c both require login -> login must appear
    # exactly once, before both b and c, and a (the target) must be last.
    r = repo(tmp_path)
    teach_flow(r, name="login", provides_auth=True)
    teach_flow(r, name="b", requires=["login"])
    teach_flow(r, name="c", requires=["login"])
    teach_flow(r, name="a", requires=["b", "c"])
    chain_log = tmp_path / "chain.log"
    result = run(r, "a", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}, chain_log=chain_log))
    assert result.returncode == 0, result.stdout + result.stderr
    assert chain_log.read_text() == "login,b,c,a"


def test_self_loop_cycle_exit_2_no_run(tmp_path):
    r = repo(tmp_path)
    teach_flow(r, name="flow-a", requires=["flow-a"])
    calls_log = tmp_path / "should-not-run.log"
    result = run(r, "flow-a", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}, mcp_log=calls_log))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cycle" in (result.stdout + result.stderr).lower()
    assert not calls_log.exists()     # the agent was never invoked


def test_three_node_cycle_exit_2_no_run(tmp_path):
    r = repo(tmp_path)
    teach_flow(r, name="flow-a", requires=["flow-b"])
    teach_flow(r, name="flow-b", requires=["flow-c"])
    teach_flow(r, name="flow-c", requires=["flow-a"])
    calls_log = tmp_path / "should-not-run.log"
    result = run(r, "flow-a", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}, mcp_log=calls_log))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cycle" in (result.stdout + result.stderr).lower()
    assert not calls_log.exists()


def test_read_requires_drops_non_slug_entries_comma_and_traversal(tmp_path):
    # A hand-edited meta.json (not something _write_flow itself would ever
    # produce, since it SLUG_RE-validates `requires` up front) could sneak a
    # comma-bearing string into `requires` — which would corrupt
    # ORBIT_QA_CHAIN's comma-separated parsing — or a '../'-style string,
    # which would flow straight into `(flows_root / name).is_dir()`. Both
    # must be silently dropped from the resolved chain, not trusted.
    r = repo(tmp_path)
    teach_flow(r, name="good-prereq")
    target_dir = teach_flow(r, name="target", requires=["good-prereq"])
    meta_path = target_dir / "meta.json"
    m = json.loads(meta_path.read_text())
    m["requires"] = ["good-prereq", "bad,name", "../evil"]
    meta_path.write_text(json.dumps(m))

    chain_log = tmp_path / "chain.log"
    result = run(r, "target", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}, chain_log=chain_log))
    assert result.returncode == 0, result.stdout + result.stderr
    # Only the legit slug survived — the comma-bearing and traversal entries
    # were dropped, never even considered as a (missing) prerequisite.
    assert chain_log.read_text() == "good-prereq,target"


def _build_flow_stub(flows_root, name, requires=()):
    """A minimal flow fixture — just enough for _resolve_chain/_read_requires
    (a dir + meta.json with a `requires` list) — no intent.md/steps.spec.ts,
    unlike teach_flow's full qa_teach._write_flow. Used only for the deep-
    chain test below, where building 1500 fixtures through the full teach
    machinery would be needlessly slow; _resolve_chain never looks past
    meta.json and dir-existence either way."""
    d = flows_root / name
    d.mkdir(parents=True)
    meta = {"created_at": None, "last_run_at": None, "last_verdict": None,
             "runs": 0, "heals": [], "requires": list(requires), "provides_auth": False}
    (d / "meta.json").write_text(json.dumps(meta))
    return d


def test_deep_chain_resolves_without_recursion_error(tmp_path):
    # _resolve_chain used to be a Python-recursive post-order DFS, which
    # raises an uncaught RecursionError past ~995-deep chains (no guard in
    # main() -> unhandled traceback, exit 1, not the module's own loud-but-
    # clean exit 2 contract). 1500 flows, each requiring the previous one,
    # proves the now-iterative version handles a chain far past that depth
    # cleanly: exit 0, full chain resolved in order, no traceback at all.
    r = repo(tmp_path)
    flows_root = r / ".autopilot" / "qa" / "flows"
    flows_root.mkdir(parents=True)
    n = 1500
    _build_flow_stub(flows_root, "flow-0")
    for i in range(1, n):
        _build_flow_stub(flows_root, f"flow-{i}", requires=[f"flow-{i - 1}"])

    chain_log = tmp_path / "chain.log"
    result = run(r, f"flow-{n - 1}", run_cmd=fake_agent(tmp_path, {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}, chain_log=chain_log), timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout + result.stderr
    assert "RecursionError" not in out
    assert "Traceback" not in out
    chain = chain_log.read_text().split(",")
    assert len(chain) == n
    assert chain[0] == "flow-0"
    assert chain[-1] == f"flow-{n - 1}"


# ===========================================================================
# --regression (Task 27): cursor-triggered sweeps of the taught-flow library
# ===========================================================================

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_hours_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def qa_cfg_with_regression(on_merge=True, schedule_hours=24, max_flows_per_run=10,
                           timeout_seconds=None):
    lines = [
        "qa:",
        '  base_url: "http://example.test/app"',
        "  regression:",
        f"    on_merge: {str(bool(on_merge)).lower()}",
        f"    schedule_hours: {schedule_hours}",
        f"    max_flows_per_run: {max_flows_per_run}",
    ]
    if timeout_seconds is not None:
        lines.append(f"  timeout_seconds: {timeout_seconds}")
    return "\n".join(lines) + "\n"


def approve(flow_dir):
    """Marks a taught flow "approved" (replay-proven) — a bare steps.json is
    all _select_regression_flows checks for; its own shape is irrelevant to
    selection."""
    (flow_dir / "steps.json").write_text("[]")


def run_regression(repo_path, if_changed=False, run_cmd=None, extra_argv=(), timeout=30):
    env = dict(os.environ)
    env.pop("ORBIT_QA_RUN_CMD", None)
    if run_cmd:
        env["ORBIT_QA_RUN_CMD"] = run_cmd
    argv = [sys.executable, str(QA_RUN), str(repo_path), "--regression"]
    if if_changed:
        argv.append("--if-changed")
    argv += list(extra_argv)
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)


def fake_agent_by_flow(tmp_path, verdicts, name="fake_regression_agent", delay_seconds=0):
    """Per-invocation fake ORBIT_QA_RUN_CMD covering MULTIPLE flows in one
    sweep: `verdicts` maps flow name -> verdict dict, read back via FLOW_NAME
    at the fake's OWN runtime (mirrors fake_agent's env-driven flow lookup —
    this is the multi-flow analog, since one qa_run.py --regression process
    invokes the SAME override once per selected flow). A flow with no entry
    gets a default clean 'pass' — keeps selection/cap/pruning tests, which
    don't care about verdict content, terse. `delay_seconds` (applied to
    EVERY flow) simulates a slow flow — used to prove the sweep deadline
    (Task 27 fix #2) actually stops launching new flows past its budget."""
    default = {"verdict": "pass", "failed_step": None, "screenshot": None,
               "summary": "ok", "healed_candidate": None}
    script = tmp_path / f"{name}.py"
    script.write_text(textwrap.dedent(f"""\
        import json, os, time
        from datetime import datetime, timezone
        from pathlib import Path
        flow = os.environ["FLOW_NAME"]
        ap_state = Path(os.environ["AP_STATE"])
        verdicts = {verdicts!r}
        default = {default!r}
        if {delay_seconds!r}:
            time.sleep({delay_seconds!r})
        v = dict(verdicts.get(flow, default))
        v.setdefault("flow", flow)
        (ap_state / "qa" / "runs").mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%fZ")
        (ap_state / "qa" / "runs" / f"{{ts}}-{{flow}}.json").write_text(json.dumps(v))
        """))
    return f"{sys.executable} {script}"


def fake_agent_touch_probe(tmp_path, sleep_flow, sleep_seconds, probe_flow, report_path,
                           name="fake_touch_probe_agent"):
    """A fake agent for a 2-flow sweep: `sleep_flow` sleeps `sleep_seconds`
    before writing its (pass) verdict; `probe_flow` — run NEXT, after
    `sleep_flow` completes — reads $AP_STATE/qa/.qa-running's mtime and
    writes `time.time() - mtime` to `report_path` BEFORE writing its own
    verdict. A small reported age (well under `sleep_seconds`) proves
    qa_run.py's _touch_lock() refreshed the lock right after `sleep_flow`
    finished, rather than the lock sitting stale from whenever the sweep
    (and thus the lock) was first created."""
    script = tmp_path / f"{name}.py"
    script.write_text(textwrap.dedent(f"""\
        import json, os, time
        from datetime import datetime, timezone
        from pathlib import Path
        flow = os.environ["FLOW_NAME"]
        ap_state = Path(os.environ["AP_STATE"])
        if flow == {sleep_flow!r}:
            time.sleep({sleep_seconds!r})
        if flow == {probe_flow!r}:
            lock = ap_state / "qa" / {qa_run.LOCK_NAME!r}
            age = time.time() - lock.stat().st_mtime
            Path({str(report_path)!r}).write_text(str(age))
        v = {{"flow": flow, "verdict": "pass", "failed_step": None, "screenshot": None,
             "summary": "ok", "healed_candidate": None}}
        (ap_state / "qa" / "runs").mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%fZ")
        (ap_state / "qa" / "runs" / f"{{ts}}-{{flow}}.json").write_text(json.dumps(v))
        """))
    return f"{sys.executable} {script}"


def write_cursor(r, base_sha=None, markers_line_count=0, swept_at=None):
    cursor_path = r / ".autopilot" / "state" / "qa" / ".regression-cursor"
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(json.dumps({"base_sha": base_sha, "markers_line_count": markers_line_count,
                                        "swept_at": swept_at}))
    return cursor_path


def read_cursor(r):
    return json.loads((r / ".autopilot" / "state" / "qa" / ".regression-cursor").read_text())


def write_markers(r, n):
    p = r / ".autopilot" / "state" / "merge_markers.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(
        json.dumps({"at": "2026-01-01T00:00:00Z", "sha": "x", "task_id": str(i)}) + "\n"
        for i in range(n)))
    return p


def write_lock(r, age_seconds=0):
    lock_path = r / ".autopilot" / "state" / "qa" / ".qa-running"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 999999, "started_at": "2020-01-01T00:00:00Z"}))
    if age_seconds:
        t = time.time() - age_seconds
        os.utime(lock_path, (t, t))
    return lock_path


def make_run_files(r, n):
    runs_dir = r / ".autopilot" / "state" / "qa" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    base = time.time() - n
    for i in range(n):
        p = runs_dir / f"run-{i:04d}.json"
        p.write_text(json.dumps({"flow": "x", "verdict": "pass", "failed_step": None,
                                  "screenshot": None, "summary": "", "healed_candidate": None}))
        t = base + i
        os.utime(p, (t, t))
        paths.append(p)
    return paths


def init_git(r, base_branch="main"):
    """A minimal real git repo AT the target dir itself (config's default
    `repo: "."`), with a `refs/remotes/origin/<base_branch>` ref set locally
    via `update-ref` — no actual remote or fetch needed, `git rev-parse
    --verify origin/<base_branch>` resolves against this ref exactly like a
    real fetched one would (see engine/qa_run.py's `_current_base_sha`).
    Returns the sha."""
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "README.md"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r,
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "update-ref", f"refs/remotes/origin/{base_branch}", sha], cwd=r, check=True)
    return sha


def move_trunk(r, base_branch="main"):
    """A new commit + move the fake origin/<base_branch> ref onto it —
    simulates a merge landing without any real remote/fetch machinery."""
    (r / f"change-{time.time()}.txt").write_text("change\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "change"], cwd=r, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r,
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "update-ref", f"refs/remotes/origin/{base_branch}", sha], cwd=r, check=True)
    return sha


# --- selection / cap -------------------------------------------------------

def test_regression_only_sweeps_approved_flows_with_steps_json(tmp_path):
    r = repo(tmp_path, qa_cfg_with_regression())
    approved = teach_flow(r, name="approved-flow")
    approve(approved)
    unapproved = teach_flow(r, name="taught-only")
    agent = fake_agent_by_flow(tmp_path, {"approved-flow": {
        "verdict": "pass", "failed_step": None, "screenshot": None,
        "summary": "ok", "healed_candidate": None}})
    result = run_regression(r, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert meta(approved)["runs"] == 1
    assert meta(unapproved)["runs"] == 0     # never swept — no steps.json


def test_regression_cap_enforced_with_dropped_log_line(tmp_path):
    r = repo(tmp_path, qa_cfg_with_regression(max_flows_per_run=2))
    dirs = {}
    for name in ("flow-a", "flow-b", "flow-c"):
        d = teach_flow(r, name=name)
        approve(d)
        dirs[name] = d
    agent = fake_agent_by_flow(tmp_path, {})   # everything defaults to pass
    result = run_regression(r, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout + result.stderr
    assert "dropped" in out.lower()
    assert "flow-c" in out                      # sorted by name -> a, b swept, c dropped
    assert meta(dirs["flow-a"])["runs"] == 1
    assert meta(dirs["flow-b"])["runs"] == 1
    assert meta(dirs["flow-c"])["runs"] == 0


# --- chaining is out of scope for --regression (see qa_run.py's module
# docstring: "--regression's sweep does NOT resolve chains") -----------------

def fake_agent_by_flow_chain_probe(tmp_path, target_flow, report_path,
                                   name="fake_regression_chain_probe"):
    """Per-invocation fake ORBIT_QA_RUN_CMD for a regression sweep: when
    FLOW_NAME == target_flow, records ORBIT_QA_CHAIN's value to report_path
    (same env-capture mechanism as --flow's own chain_log fakes) before
    writing a clean 'pass' verdict for whichever flow it's invoked for."""
    script = tmp_path / f"{name}.py"
    script.write_text(textwrap.dedent(f"""\
        import json, os
        from datetime import datetime, timezone
        from pathlib import Path
        flow = os.environ["FLOW_NAME"]
        ap_state = Path(os.environ["AP_STATE"])
        if flow == {target_flow!r}:
            Path({str(report_path)!r}).write_text(os.environ.get("ORBIT_QA_CHAIN", ""))
        v = {{"flow": flow, "verdict": "pass", "failed_step": None, "screenshot": None,
             "summary": "ok", "healed_candidate": None}}
        (ap_state / "qa" / "runs").mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%fZ")
        (ap_state / "qa" / "runs" / f"{{ts}}-{{flow}}.json").write_text(json.dumps(v))
        """))
    return f"{sys.executable} {script}"


def test_regression_sweep_does_not_resolve_chain_byte_neutral(tmp_path):
    # A swept flow that HAS `requires` must still get the trivial single-
    # element chain (--regression never resolves prerequisite chains, unlike
    # --flow) — ORBIT_QA_CHAIN is just the flow's own name, byte-neutral with
    # a flow that has no `requires` at all.
    r = repo(tmp_path, qa_cfg_with_regression())
    teach_flow(r, name="login")   # never approved — proves it's not swept either
    checkout = teach_flow(r, name="checkout", requires=["login"])
    approve(checkout)
    report = tmp_path / "chain.log"
    agent = fake_agent_by_flow_chain_probe(tmp_path, "checkout", report)
    result = run_regression(r, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert report.read_text() == "checkout"
    assert meta(checkout)["runs"] == 1


# --- exit codes -------------------------------------------------------------

def test_regression_exit_3_when_any_flow_fails(tmp_path):
    r = repo(tmp_path, qa_cfg_with_regression())
    ok_dir = teach_flow(r, name="flow-ok")
    approve(ok_dir)
    bad_dir = teach_flow(r, name="flow-bad")
    approve(bad_dir)
    agent = fake_agent_by_flow(tmp_path, {
        "flow-ok": {"verdict": "pass", "failed_step": None, "screenshot": None,
                    "summary": "ok", "healed_candidate": None},
        "flow-bad": {"verdict": "fail", "failed_step": 1, "screenshot": "x.png",
                     "summary": "broke", "healed_candidate": None},
    })
    result = run_regression(r, run_cmd=agent)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "1 fail" in result.stdout
    assert meta(ok_dir)["last_verdict"] == "pass"
    assert meta(bad_dir)["last_verdict"] == "fail"


# --- locking ------------------------------------------------------------

def test_regression_fresh_lock_exits_0_without_sweeping(tmp_path):
    r = repo(tmp_path, qa_cfg_with_regression(timeout_seconds=5))
    flow_dir = teach_flow(r, name="approved-flow")
    approve(flow_dir)
    write_lock(r, age_seconds=0)
    agent = fake_agent_by_flow(tmp_path, {})
    result = run_regression(r, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already running" in (result.stdout + result.stderr).lower()
    assert meta(flow_dir)["runs"] == 0            # agent never invoked


def test_regression_stale_lock_is_replaced_and_sweep_runs(tmp_path):
    r = repo(tmp_path, qa_cfg_with_regression(timeout_seconds=5))
    flow_dir = teach_flow(r, name="approved-flow")
    approve(flow_dir)
    write_lock(r, age_seconds=9999)
    agent = fake_agent_by_flow(tmp_path, {})
    result = run_regression(r, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    out = (result.stdout + result.stderr).lower()
    assert "stale" in out and "replac" in out
    assert meta(flow_dir)["runs"] == 1             # sweep actually ran this time
    lock_path = r / ".autopilot" / "state" / "qa" / ".qa-running"
    assert not lock_path.exists()                  # released after


def test_regression_lock_removed_after_a_failing_sweep(tmp_path):
    r = repo(tmp_path, qa_cfg_with_regression())
    flow_dir = teach_flow(r, name="approved-flow")
    approve(flow_dir)
    agent = fake_agent_by_flow(tmp_path, {"approved-flow": {
        "verdict": "fail", "failed_step": 1, "screenshot": "x.png",
        "summary": "broke", "healed_candidate": None}})
    result = run_regression(r, run_cmd=agent)
    assert result.returncode == 3, result.stdout + result.stderr
    lock_path = r / ".autopilot" / "state" / "qa" / ".qa-running"
    assert not lock_path.exists()


# --- CRITICAL fix: lock staleness is a PER-FLOW window (proved by a
# per-flow touch), never the whole sweep's total budget; release is
# ownership-checked -----------------------------------------------------

def test_lock_widened_staleness_window_honors_a_lock_aged_past_bare_timeout(tmp_path):
    # A concurrent invocation must NOT mis-diagnose a legitimately still-
    # running multi-flow sweep as stale just because the lock is older than
    # ONE flow's own qa.timeout_seconds — the real window is timeout_seconds
    # + LOCK_STALE_MARGIN_SECONDS (60s). age=20 is > timeout_seconds(5) (the
    # OLD, too-narrow window this bug used) but well under 5+60=65 (the
    # fixed window) — must still read as fresh/busy, never replaced.
    r = repo(tmp_path, qa_cfg_with_regression(timeout_seconds=5))
    flow_dir = teach_flow(r, name="approved-flow")
    approve(flow_dir)
    write_lock(r, age_seconds=20)
    agent = fake_agent_by_flow(tmp_path, {})
    result = run_regression(r, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already running" in (result.stdout + result.stderr).lower()
    assert meta(flow_dir)["runs"] == 0                 # agent never invoked
    lock_path = r / ".autopilot" / "state" / "qa" / ".qa-running"
    assert lock_path.exists()                          # a busy-exit never touches the lock


def test_lock_is_touched_after_each_flow_proving_liveness_mid_sweep(tmp_path):
    # Direct proof of the liveness mechanism itself: flow-a sleeps, THEN
    # flow-b's own fake agent reads the lock's mtime and reports its age —
    # a small reported age proves qa_run.py's _touch_lock() refreshed the
    # lock right after flow-a finished, not that it merely hadn't gone stale
    # yet from whenever the sweep started.
    r = repo(tmp_path, qa_cfg_with_regression())
    flow_a = teach_flow(r, name="flow-a")
    approve(flow_a)
    flow_b = teach_flow(r, name="flow-b")
    approve(flow_b)
    report = tmp_path / "touch_age.txt"
    agent = fake_agent_touch_probe(tmp_path, sleep_flow="flow-a", sleep_seconds=2.0,
                                   probe_flow="flow-b", report_path=report)
    result = run_regression(r, run_cmd=agent, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    age = float(report.read_text().strip())
    assert age < 1.0, f"lock mtime was {age}s stale — _touch_lock did not run between flows"


def test_release_lock_leaves_a_foreign_pid_lock_in_place(tmp_path):
    # White-box unit test (direct import — see the module-level qa_run
    # import above): _release_lock must never unlink a lock file that no
    # longer records OUR pid — a different, still-running process may have
    # legitimately replaced it since we last touched it.
    lock_path = tmp_path / ".qa-running"
    lock_path.write_text(json.dumps({"pid": 999999, "started_at": "2020-01-01T00:00:00Z"}))
    qa_run._release_lock(lock_path)
    assert lock_path.exists()


def test_release_lock_removes_a_lock_recorded_as_our_own_pid(tmp_path):
    lock_path = tmp_path / ".qa-running"
    lock_path.write_text(json.dumps({"pid": os.getpid(), "started_at": "2020-01-01T00:00:00Z"}))
    qa_run._release_lock(lock_path)
    assert not lock_path.exists()


# --- Important fix: sweep-level deadline bounds the synchronous loop -------

def test_sweep_deadline_stops_launching_new_flows_and_logs_the_drop(tmp_path):
    # qa.timeout_seconds=1 -> deadline = 2 x 1 = 2s (from the CONFIG value,
    # independent of any --timeout override below — see _sweep's
    # `default_timeout` param). `--timeout 10` overrides each flow's OWN
    # per-agent budget generously so a 1.1s delay never trips an individual
    # flow's timeout — only the sweep-level deadline is under test here.
    # Three flows, each taking ~1.1s: flow-a and flow-b fit inside the 2s
    # deadline (elapsed checked BEFORE launching each), flow-c's launch-time
    # check (~2.2s elapsed) trips it — flow-c is dropped, logged, never
    # invoked.
    r = repo(tmp_path, qa_cfg_with_regression(timeout_seconds=1))
    dirs = {}
    for name in ("flow-a", "flow-b", "flow-c"):
        d = teach_flow(r, name=name)
        approve(d)
        dirs[name] = d
    agent = fake_agent_by_flow(tmp_path, {}, delay_seconds=1.1)
    result = run_regression(r, run_cmd=agent, extra_argv=("--timeout", "10"), timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout + result.stderr
    assert "deadline" in out.lower()
    assert "flow-c" in out
    assert meta(dirs["flow-a"])["runs"] == 1
    assert meta(dirs["flow-b"])["runs"] == 1
    assert meta(dirs["flow-c"])["runs"] == 0           # dropped, never launched


def test_sweep_deadline_truncated_sweep_still_writes_the_cursor(tmp_path):
    # A deadline-truncated sweep is still a COMPLETED sweep — --if-changed
    # must still rewrite the cursor for it (never left stale, which would
    # cause the next cycle to just re-trigger the exact same truncated sweep).
    # Same deadline-tripping shape as the test above (timeout_seconds=1 ->
    # deadline 2s, --timeout 10 keeps each flow's own budget generous).
    r = repo(tmp_path, qa_cfg_with_regression(timeout_seconds=1, schedule_hours=24))
    write_cursor(r, base_sha=None, markers_line_count=0, swept_at=_iso_hours_ago(1000))
    dirs = {}
    for name in ("flow-a", "flow-b", "flow-c"):
        d = teach_flow(r, name=name)
        approve(d)
        dirs[name] = d
    agent = fake_agent_by_flow(tmp_path, {}, delay_seconds=1.1)
    result = run_regression(r, if_changed=True, run_cmd=agent,
                            extra_argv=("--timeout", "10"), timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "deadline" in (result.stdout + result.stderr).lower()
    assert meta(dirs["flow-c"])["runs"] == 0           # dropped by the deadline
    cur = read_cursor(r)
    assert cur["swept_at"] != _iso_hours_ago(1000)     # cursor still rewritten


# --- pruning ---------------------------------------------------------------

def test_regression_prunes_runs_dir_to_newest_200(tmp_path):
    r = repo(tmp_path, qa_cfg_with_regression())
    files = make_run_files(r, 205)
    result = run_regression(r)
    assert result.returncode == 0, result.stdout + result.stderr
    runs_dir = r / ".autopilot" / "state" / "qa" / "runs"
    remaining = sorted(p.name for p in runs_dir.iterdir())
    assert len(remaining) == 200
    # the 5 oldest (run-0000..run-0004) must be gone; the newest must remain
    for i in range(5):
        assert f"run-{i:04d}.json" not in remaining
    assert "run-0204.json" in remaining
    assert "pruned" in (result.stdout + result.stderr).lower()


# --- no qa: block --------------------------------------------------------

def test_regression_no_qa_block_with_if_changed_is_silent_exit_0(tmp_path):
    r = repo(tmp_path, extra_cfg="")     # no qa: block at all
    result = run_regression(r, if_changed=True)
    assert result.returncode == 0
    assert result.stdout == "" and result.stderr == ""


def test_regression_no_qa_block_without_if_changed_is_loud_exit_2(tmp_path):
    # The human-click path ("sweep now", no --if-changed) keeps the SAME
    # loud manual-CLI posture as --flow — only the automated --if-changed
    # hook gets the silent fast exit (loop-safety ordering exception).
    r = repo(tmp_path, extra_cfg="")
    result = run_regression(r, if_changed=False)
    assert result.returncode == 2
    assert "qa:" in (result.stdout + result.stderr)


# --- --if-changed: cursor + cadence + on_merge ------------------------------

def test_if_changed_fresh_cursor_unchanged_cadence_off_fast_exits(tmp_path):
    r = repo(tmp_path, qa_cfg_with_regression(on_merge=True, schedule_hours=0))
    sha = init_git(r)
    write_cursor(r, base_sha=sha, markers_line_count=0, swept_at=_now())
    flow_dir = teach_flow(r, name="approved-flow")
    approve(flow_dir)
    agent = fake_agent_by_flow(tmp_path, {})
    result = run_regression(r, if_changed=True, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert meta(flow_dir)["runs"] == 0              # agent NOT invoked
    assert read_cursor(r)["base_sha"] == sha        # cursor untouched


def test_if_changed_trunk_sha_moved_sweeps_and_rewrites_cursor(tmp_path):
    r = repo(tmp_path, qa_cfg_with_regression(on_merge=True, schedule_hours=0))
    sha1 = init_git(r)
    write_cursor(r, base_sha=sha1, markers_line_count=0, swept_at=_now())
    flow_dir = teach_flow(r, name="approved-flow")
    approve(flow_dir)
    sha2 = move_trunk(r)
    assert sha2 != sha1
    agent = fake_agent_by_flow(tmp_path, {})
    result = run_regression(r, if_changed=True, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert meta(flow_dir)["runs"] == 1
    cur = read_cursor(r)
    assert cur["base_sha"] == sha2
    assert cur["swept_at"]


def test_if_changed_markers_line_count_grew_sweeps(tmp_path):
    r = repo(tmp_path, qa_cfg_with_regression(on_merge=True, schedule_hours=0))
    write_markers(r, 2)
    write_cursor(r, base_sha=None, markers_line_count=2, swept_at=_now())
    flow_dir = teach_flow(r, name="approved-flow")
    approve(flow_dir)
    write_markers(r, 3)                            # a new merge marker landed
    agent = fake_agent_by_flow(tmp_path, {})
    result = run_regression(r, if_changed=True, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert meta(flow_dir)["runs"] == 1
    assert read_cursor(r)["markers_line_count"] == 3


def test_if_changed_schedule_hours_elapsed_sweeps(tmp_path):
    r = repo(tmp_path, qa_cfg_with_regression(on_merge=True, schedule_hours=24))
    old = _iso_hours_ago(100)
    write_cursor(r, base_sha=None, markers_line_count=0, swept_at=old)
    flow_dir = teach_flow(r, name="approved-flow")
    approve(flow_dir)
    agent = fake_agent_by_flow(tmp_path, {})
    result = run_regression(r, if_changed=True, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert meta(flow_dir)["runs"] == 1
    assert read_cursor(r)["swept_at"] != old


def test_if_changed_schedule_hours_zero_cadence_never_fires(tmp_path):
    r = repo(tmp_path, qa_cfg_with_regression(on_merge=True, schedule_hours=0))
    old = _iso_hours_ago(100000)                    # absurdly old — would fire at ANY nonzero cadence
    write_cursor(r, base_sha=None, markers_line_count=0, swept_at=old)
    flow_dir = teach_flow(r, name="approved-flow")
    approve(flow_dir)
    agent = fake_agent_by_flow(tmp_path, {})
    result = run_regression(r, if_changed=True, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert meta(flow_dir)["runs"] == 0
    assert read_cursor(r)["swept_at"] == old         # untouched — never swept


def test_if_changed_on_merge_false_ignores_sha_move(tmp_path):
    r = repo(tmp_path, qa_cfg_with_regression(on_merge=False, schedule_hours=0))
    sha1 = init_git(r)
    write_cursor(r, base_sha=sha1, markers_line_count=0, swept_at=_now())
    flow_dir = teach_flow(r, name="approved-flow")
    approve(flow_dir)
    move_trunk(r)                                    # trunk moved, but on_merge is off
    agent = fake_agent_by_flow(tmp_path, {})
    result = run_regression(r, if_changed=True, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert meta(flow_dir)["runs"] == 0
    assert read_cursor(r)["base_sha"] == sha1        # untouched — sha move ignored


# ===========================================================================
# --explore (Task 29): opt-in bounded same-origin crawl via /orbit-qa-explore
# ===========================================================================

def qa_cfg_with_exploratory(enabled=True, max_pages=10, timeout_seconds=None):
    lines = [
        "qa:",
        '  base_url: "http://example.test/app"',
        "  exploratory:",
        f"    enabled: {str(bool(enabled)).lower()}",
        f"    max_pages: {max_pages}",
    ]
    if timeout_seconds is not None:
        lines.append(f"  timeout_seconds: {timeout_seconds}")
    return "\n".join(lines) + "\n"


def run_explore(repo_path, run_cmd=None, extra_argv=(), timeout=30):
    env = dict(os.environ)
    env.pop("ORBIT_QA_RUN_CMD", None)
    if run_cmd:
        env["ORBIT_QA_RUN_CMD"] = run_cmd
    argv = [sys.executable, str(QA_RUN), str(repo_path), "--explore"]
    argv += list(extra_argv)
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)


def fake_explore_agent(tmp_path, result=None, mcp_log=None, name="fake_explore_agent",
                        sleep_before_write=0.0):
    """`result` (dict or None) is written as $AP_STATE/qa/explore-<UTC-ts>.json
    — None means "write nothing" (simulates a missing result), mirroring
    fake_agent's own verdict=None convention."""
    lines = [
        "import json, os, time",
        "from datetime import datetime, timezone",
        "from pathlib import Path",
        'ap_state = Path(os.environ["AP_STATE"])',
    ]
    if mcp_log is not None:
        lines.append(f'Path({str(mcp_log)!r}).write_text(os.environ.get("QA_MCP_CONFIG", ""))')
    if sleep_before_write:
        lines.append(f"time.sleep({sleep_before_write})")
    if result is not None:
        lines += [
            '(ap_state / "qa").mkdir(parents=True, exist_ok=True)',
            'ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%fZ")',
            f'r = {result!r}',
            '(ap_state / "qa" / f"explore-{ts}.json").write_text(json.dumps(r))',
        ]
    script = tmp_path / f"{name}.py"
    script.write_text("\n".join(lines) + "\n")
    return f"{sys.executable} {script}"


# --- gating: opt-in, off by default -----------------------------------------

def test_explore_disabled_exit_2_agent_not_invoked(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory(enabled=False))
    agent = fake_explore_agent(tmp_path, {"pages_visited": 3, "new_rows": 1, "summary": "found stuff"})
    result = run_explore(r, run_cmd=agent)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "exploratory" in (result.stdout + result.stderr).lower()
    qa_dir = r / ".autopilot" / "state" / "qa"
    assert not (qa_dir.is_dir() and any(qa_dir.glob("explore-*.json")))   # agent never invoked


def test_explore_no_exploratory_block_exit_2(tmp_path):
    r = repo(tmp_path)     # default QA_CFG has no exploratory: block at all
    result = run_explore(r)
    assert result.returncode == 2, result.stdout + result.stderr


def test_explore_no_qa_block_exit_2(tmp_path):
    r = repo(tmp_path, extra_cfg="")    # no qa: block at all
    result = run_explore(r)
    assert result.returncode == 2, result.stdout + result.stderr


def test_explore_and_flow_mutually_exclusive_exit_2(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory())
    result = subprocess.run(
        [sys.executable, str(QA_RUN), str(r), "--explore", "--flow", "x"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr


def test_explore_and_regression_mutually_exclusive_exit_2(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory())
    result = subprocess.run(
        [sys.executable, str(QA_RUN), str(r), "--regression", "--explore"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr


def test_explore_rejects_if_changed_exit_2(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory())
    result = subprocess.run(
        [sys.executable, str(QA_RUN), str(r), "--explore", "--if-changed"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr


# --- happy path: counts echoed ----------------------------------------------

def test_explore_enabled_result_exit_0_echoes_counts(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory(enabled=True, max_pages=5))
    agent = fake_explore_agent(tmp_path, {
        "pages_visited": 4, "new_rows": 2, "summary": "found a dead link"})
    result = run_explore(r, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4" in result.stdout and "2" in result.stdout
    assert "found a dead link" in result.stdout


def test_explore_zero_new_rows_is_a_clean_exit_0(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory())
    agent = fake_explore_agent(tmp_path, {
        "pages_visited": 6, "new_rows": 0, "summary": "clean crawl, nothing new"})
    result = run_explore(r, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean crawl" in result.stdout


# --- missing / stale / malformed result -> exit 1 ---------------------------

def test_explore_missing_result_exit_1(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory())
    agent = fake_explore_agent(tmp_path, result=None)
    result = run_explore(r, run_cmd=agent)
    assert result.returncode == 1, result.stdout + result.stderr


def test_explore_stale_result_ignored_exit_1(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory())
    qa_dir = r / ".autopilot" / "state" / "qa"
    qa_dir.mkdir(parents=True)
    stale = qa_dir / "explore-2000-01-01T00-00-00Z.json"
    stale.write_text(json.dumps({"pages_visited": 9, "new_rows": 9, "summary": "ancient"}))
    old = time.time() - 3600
    os.utime(stale, (old, old))
    agent = fake_explore_agent(tmp_path, result=None)
    result = run_explore(r, run_cmd=agent)
    assert result.returncode == 1, result.stdout + result.stderr


def test_explore_malformed_negative_pages_exit_1(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory())
    agent = fake_explore_agent(tmp_path, {"pages_visited": -1, "new_rows": 0, "summary": "bad"})
    result = run_explore(r, run_cmd=agent)
    assert result.returncode == 1, result.stdout + result.stderr


def test_explore_malformed_non_string_summary_exit_1(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory())
    agent = fake_explore_agent(tmp_path, {"pages_visited": 1, "new_rows": 0, "summary": None})
    result = run_explore(r, run_cmd=agent)
    assert result.returncode == 1, result.stdout + result.stderr


# --- MCP filename distinct (extends the Phase-4 collision-test discipline) --

def test_explore_mcp_autogen_writes_explore_output_dir_and_distinct_filename(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory())
    mcp_log = tmp_path / "mcp-explore.txt"
    result = run_explore(r, run_cmd=fake_explore_agent(tmp_path, {
        "pages_visited": 2, "new_rows": 0, "summary": "clean"}, mcp_log=mcp_log))
    assert result.returncode == 0, result.stdout + result.stderr

    generated = r / ".autopilot" / "state" / "qa" / "mcp-run-explore.json"
    assert generated.exists()
    cfg = json.loads(generated.read_text())
    args = cfg["mcpServers"]["playwright"]["args"]
    out_dir = args[args.index("--output-dir") + 1]
    assert out_dir.endswith(str(Path("qa") / "explore"))
    assert Path(out_dir).is_dir()
    assert mcp_log.read_text() == str(generated)
    # distinct from --flow's own filename shape (mcp-run-flow-<name>.json) —
    # the Phase 4 review finding this whole naming discipline guards against.
    assert not (r / ".autopilot" / "state" / "qa" / "mcp-run-flow-explore.json").exists()


# --- shares Task 27's .qa-running lock with --regression --------------------

def test_explore_shares_qa_running_lock_fresh_lock_skips(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory(timeout_seconds=5))
    write_lock(r, age_seconds=0)
    agent = fake_explore_agent(tmp_path, {"pages_visited": 1, "new_rows": 0, "summary": "x"})
    result = run_explore(r, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already running" in (result.stdout + result.stderr).lower()
    qa_dir = r / ".autopilot" / "state" / "qa"
    assert not any(qa_dir.glob("explore-*.json"))    # agent never invoked


def test_explore_stale_lock_is_replaced_and_crawl_runs(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory(timeout_seconds=5))
    write_lock(r, age_seconds=9999)
    agent = fake_explore_agent(tmp_path, {"pages_visited": 3, "new_rows": 1, "summary": "ok"})
    result = run_explore(r, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    out = (result.stdout + result.stderr).lower()
    assert "stale" in out and "replac" in out
    lock_path = r / ".autopilot" / "state" / "qa" / ".qa-running"
    assert not lock_path.exists()                    # released after


def test_explore_lock_released_after_missing_result(tmp_path):
    r = repo(tmp_path, qa_cfg_with_exploratory())
    agent = fake_explore_agent(tmp_path, result=None)
    result = run_explore(r, run_cmd=agent)
    assert result.returncode == 1, result.stdout + result.stderr
    lock_path = r / ".autopilot" / "state" / "qa" / ".qa-running"
    assert not lock_path.exists()


# ===========================================================================
# --generate: AI-authored candidate tests via /orbit-qa-generate, staged for
# human review (never written straight into the committed flow library).
# ===========================================================================

def run_generate(repo_path, run_cmd=None, extra_argv=(), timeout=30):
    env = dict(os.environ)
    env.pop("ORBIT_QA_RUN_CMD", None)
    if run_cmd:
        env["ORBIT_QA_RUN_CMD"] = run_cmd
    argv = [sys.executable, str(QA_RUN), str(repo_path), "--generate"]
    argv += list(extra_argv)
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)


def fake_generate_agent(tmp_path, result=None, mcp_log=None, max_log=None, attach_log=None,
                         name="fake_generate_agent", sleep_before_write=0.0):
    """`result` (dict or None) is written as $AP_STATE/qa/generate-<UTC-ts>.json
    — None means "write nothing" (simulates a missing result), mirroring
    fake_explore_agent's own result=None convention. `max_log`/`attach_log`,
    if given, record the ORBIT_QA_GENERATE_MAX/ORBIT_QA_ATTACH env vars this
    invocation actually saw — same env-capture mechanism as `mcp_log`."""
    lines = [
        "import json, os, time",
        "from datetime import datetime, timezone",
        "from pathlib import Path",
        'ap_state = Path(os.environ["AP_STATE"])',
    ]
    if mcp_log is not None:
        lines.append(f'Path({str(mcp_log)!r}).write_text(os.environ.get("QA_MCP_CONFIG", ""))')
    if max_log is not None:
        lines.append(f'Path({str(max_log)!r}).write_text(os.environ.get("ORBIT_QA_GENERATE_MAX", ""))')
    if attach_log is not None:
        lines.append(f'Path({str(attach_log)!r}).write_text(os.environ.get("ORBIT_QA_ATTACH", ""))')
    if sleep_before_write:
        lines.append(f"time.sleep({sleep_before_write})")
    if result is not None:
        lines += [
            '(ap_state / "qa").mkdir(parents=True, exist_ok=True)',
            'ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%fZ")',
            f'r = {result!r}',
            '(ap_state / "qa" / f"generate-{ts}.json").write_text(json.dumps(r))',
        ]
    script = tmp_path / f"{name}.py"
    script.write_text("\n".join(lines) + "\n")
    return f"{sys.executable} {script}"


# --- gating / usage ----------------------------------------------------------

def test_generate_no_qa_block_exit_2(tmp_path):
    r = repo(tmp_path, extra_cfg="")   # no qa: block at all
    result = run_generate(r)
    assert result.returncode == 2, result.stdout + result.stderr


def test_generate_and_flow_mutually_exclusive_exit_2(tmp_path):
    r = repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(QA_RUN), str(r), "--generate", "--flow", "x"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr


def test_generate_and_regression_mutually_exclusive_exit_2(tmp_path):
    r = repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(QA_RUN), str(r), "--regression", "--generate"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr


def test_generate_and_explore_mutually_exclusive_exit_2(tmp_path):
    r = repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(QA_RUN), str(r), "--explore", "--generate"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr


def test_generate_rejects_if_changed_exit_2(tmp_path):
    r = repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(QA_RUN), str(r), "--generate", "--if-changed"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr


def test_max_flag_rejected_with_flow_exit_2(tmp_path):
    r = repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(QA_RUN), str(r), "--flow", "x", "--max", "3"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr


def test_generate_max_must_be_an_integer_exit_2(tmp_path):
    r = repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(QA_RUN), str(r), "--generate", "--max", "nope"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr


# --- happy path: count echoed -------------------------------------------------

def test_generate_enabled_result_exit_0_echoes_count(tmp_path):
    r = repo(tmp_path)
    agent = fake_generate_agent(tmp_path, {
        "candidates": ["checkout-flow", "signup-flow"], "skipped": [],
        "count": 2, "summary": "wrote 2 candidates"})
    result = run_generate(r, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 candidate test" in result.stdout
    assert "review & approve on the dashboard" in result.stdout
    assert "wrote 2 candidates" in result.stdout


def test_generate_zero_candidates_is_exit_1(tmp_path):
    # Manual-CLI posture: unlike --explore (a clean crawl finding nothing new
    # IS the happy path), a generate pass with nothing to propose leaves
    # nothing on the dashboard for the human to review — loud, exit 1.
    r = repo(tmp_path)
    agent = fake_generate_agent(tmp_path, {
        "candidates": [], "skipped": ["already-covered-flow"],
        "count": 0, "summary": "everything already has a flow"})
    result = run_generate(r, run_cmd=agent)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "everything already has a flow" in (result.stdout + result.stderr)


# --- missing / stale / malformed result -> exit 1 ----------------------------

def test_generate_missing_result_exit_1(tmp_path):
    r = repo(tmp_path)
    agent = fake_generate_agent(tmp_path, result=None)
    result = run_generate(r, run_cmd=agent)
    assert result.returncode == 1, result.stdout + result.stderr


def test_generate_stale_result_ignored_exit_1(tmp_path):
    r = repo(tmp_path)
    qa_dir = r / ".autopilot" / "state" / "qa"
    qa_dir.mkdir(parents=True)
    stale = qa_dir / "generate-2000-01-01T00-00-00Z.json"
    stale.write_text(json.dumps({"candidates": ["old"], "skipped": [], "count": 1, "summary": "ancient"}))
    old = time.time() - 3600
    os.utime(stale, (old, old))
    agent = fake_generate_agent(tmp_path, result=None)
    result = run_generate(r, run_cmd=agent)
    assert result.returncode == 1, result.stdout + result.stderr


def test_generate_malformed_count_mismatch_exit_1(tmp_path):
    r = repo(tmp_path)
    agent = fake_generate_agent(tmp_path, {
        "candidates": ["a"], "skipped": [], "count": 2, "summary": "mismatched"})
    result = run_generate(r, run_cmd=agent)
    assert result.returncode == 1, result.stdout + result.stderr


def test_generate_malformed_non_list_candidates_exit_1(tmp_path):
    r = repo(tmp_path)
    agent = fake_generate_agent(tmp_path, {
        "candidates": "not-a-list", "skipped": [], "count": 0, "summary": "bad shape"})
    result = run_generate(r, run_cmd=agent)
    assert result.returncode == 1, result.stdout + result.stderr


def test_generate_malformed_non_string_summary_exit_1(tmp_path):
    r = repo(tmp_path)
    agent = fake_generate_agent(tmp_path, {
        "candidates": [], "skipped": [], "count": 0, "summary": None})
    result = run_generate(r, run_cmd=agent)
    assert result.returncode == 1, result.stdout + result.stderr


# --- --max / --attach env plumbing -------------------------------------------

def test_generate_max_defaults_to_ten(tmp_path):
    r = repo(tmp_path)
    max_log = tmp_path / "max.txt"
    agent = fake_generate_agent(tmp_path, {
        "candidates": [], "skipped": [], "count": 0, "summary": "x"}, max_log=max_log)
    run_generate(r, run_cmd=agent)
    assert max_log.read_text() == "10"


def test_generate_max_flag_overrides_default(tmp_path):
    r = repo(tmp_path)
    max_log = tmp_path / "max.txt"
    agent = fake_generate_agent(tmp_path, {
        "candidates": [], "skipped": [], "count": 0, "summary": "x"}, max_log=max_log)
    run_generate(r, run_cmd=agent, extra_argv=["--max", "3"])
    assert max_log.read_text() == "3"


def test_generate_attach_unset_by_default(tmp_path):
    r = repo(tmp_path)
    attach_log = tmp_path / "attach.txt"
    agent = fake_generate_agent(tmp_path, {
        "candidates": [], "skipped": [], "count": 0, "summary": "x"}, attach_log=attach_log)
    run_generate(r, run_cmd=agent)
    assert attach_log.read_text() == ""


def test_generate_attach_flag_sets_env(tmp_path):
    r = repo(tmp_path)
    attach_log = tmp_path / "attach.txt"
    agent = fake_generate_agent(tmp_path, {
        "candidates": [], "skipped": [], "count": 0, "summary": "x"}, attach_log=attach_log)
    run_generate(r, run_cmd=agent, extra_argv=["--attach"])
    assert attach_log.read_text() == "1"


# --- MCP filename distinct (extends the Phase-4 collision-test discipline) --

def test_generate_mcp_autogen_writes_generate_output_dir_and_distinct_filename(tmp_path):
    r = repo(tmp_path)
    mcp_log = tmp_path / "mcp-generate.txt"
    result = run_generate(r, run_cmd=fake_generate_agent(tmp_path, {
        "candidates": [], "skipped": [], "count": 0, "summary": "clean"}, mcp_log=mcp_log))
    assert result.returncode == 1, result.stdout + result.stderr   # 0 candidates -> exit 1, MCP still generated

    generated = r / ".autopilot" / "state" / "qa" / "mcp-run-generate.json"
    assert generated.exists()
    cfg = json.loads(generated.read_text())
    args = cfg["mcpServers"]["playwright"]["args"]
    out_dir = args[args.index("--output-dir") + 1]
    assert out_dir.endswith(str(Path("qa") / "generate"))
    assert Path(out_dir).is_dir()
    assert mcp_log.read_text() == str(generated)
    assert not (r / ".autopilot" / "state" / "qa" / "mcp-run-explore.json").exists()


# --- shares Task 27's .qa-running lock with --regression/--explore ----------

def test_generate_shares_qa_running_lock_fresh_lock_skips(tmp_path):
    r = repo(tmp_path, QA_CFG + "  timeout_seconds: 5\n")
    write_lock(r, age_seconds=0)
    agent = fake_generate_agent(tmp_path, {
        "candidates": ["a"], "skipped": [], "count": 1, "summary": "x"})
    result = run_generate(r, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already running" in (result.stdout + result.stderr).lower()
    qa_dir = r / ".autopilot" / "state" / "qa"
    assert not any(qa_dir.glob("generate-*.json"))    # agent never invoked


def test_generate_stale_lock_is_replaced_and_run_proceeds(tmp_path):
    r = repo(tmp_path, QA_CFG + "  timeout_seconds: 5\n")
    write_lock(r, age_seconds=9999)
    agent = fake_generate_agent(tmp_path, {
        "candidates": ["a"], "skipped": [], "count": 1, "summary": "ok"})
    result = run_generate(r, run_cmd=agent)
    assert result.returncode == 0, result.stdout + result.stderr
    out = (result.stdout + result.stderr).lower()
    assert "stale" in out and "replac" in out
    lock_path = r / ".autopilot" / "state" / "qa" / ".qa-running"
    assert not lock_path.exists()                    # released after


def test_generate_lock_released_after_missing_result(tmp_path):
    r = repo(tmp_path)
    agent = fake_generate_agent(tmp_path, result=None)
    result = run_generate(r, run_cmd=agent)
    assert result.returncode == 1, result.stdout + result.stderr
    lock_path = r / ".autopilot" / "state" / "qa" / ".qa-running"
    assert not lock_path.exists()


# ── _sync_qa_command: keep the target's /orbit-qa-* slash command current ────
# The bug this guards against: a target onboarded before a QA skill existed
# (e.g. /orbit-qa-generate, added after the target was set up) never had the
# command installed, so `claude --print /orbit-qa-generate` invoked a
# non-existent command, the agent wrote no result, and the run failed
# silently. Every agent runner now re-syncs its own command from
# $ORBIT_HOME/skills before invoking claude — same fix as knowledge.py's
# _sync_knowledge_command. (White-box, in-process — the real runners only hit
# this on the live-claude path, which the ORBIT_QA_RUN_CMD test hook skips.)

def test_sync_qa_command_installs_missing_command(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    qa_run._sync_qa_command(target, "orbit-qa-generate.md")
    dest = target / ".claude" / "commands" / "orbit-qa-generate.md"
    assert dest.is_file()
    # byte-identical to the engine's own copy (the source of truth)
    src = qa_run.ORBIT_HOME / "skills" / "orbit-qa-generate.md"
    assert dest.read_text() == src.read_text()


def test_sync_qa_command_overwrites_stale_command(tmp_path):
    target = tmp_path / "repo"
    cmd_dir = target / ".claude" / "commands"
    cmd_dir.mkdir(parents=True)
    stale = cmd_dir / "orbit-qa-generate.md"
    stale.write_text("STALE — predates a feature")
    qa_run._sync_qa_command(target, "orbit-qa-generate.md")
    assert stale.read_text() != "STALE — predates a feature"
    assert "STALE" not in stale.read_text()


def test_sync_qa_command_never_raises_on_unwritable_target(tmp_path):
    # A permission error / read-only mount is a WARN, not a crash — the agent
    # still runs against whatever copy is already there.
    target = tmp_path / "repo"
    target.mkdir()
    (target / ".claude").write_text("not a dir")   # mkdir(parents) under a FILE errors
    qa_run._sync_qa_command(target, "orbit-qa-generate.md")   # must not raise
