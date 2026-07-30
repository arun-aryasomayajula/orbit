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
import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
CHECK = ENGINE / "qa_check.py"


def _load_module(name, path):
    """Fresh module load (not a cached `import`) — qa_check.py resolves its
    own AP_STATE/AP_HOME module-level globals from the environment at import
    time, so a plain `import qa_check` shared across test functions could
    silently reuse a stale first-import's globals. A unique spec name per
    call sidesteps that."""
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def target(tmp_path, extra_cfg="", category="frontend"):
    ap = tmp_path / "repo" / ".autopilot"
    (ap / "state" / "reviews").mkdir(parents=True)
    (ap / "config.yaml").write_text(
        'repo: "."\nbase_branch: "main"\ngates: {t: {cmd: "true"}}\n' + extra_cfg)
    (ap / "state" / "queue.json").write_text(json.dumps({"tasks": [
        {"id": "t1", "task": "x", "category": category}]}))
    return tmp_path / "repo"


def run(repo, agent_cmd=None, qa_agent_cmd=None, tid="t1", patch=None):
    env = dict(os.environ, AP_HOME=str(repo / ".autopilot"),
               AP_STATE=str(repo / ".autopilot" / "state"), ORBIT_HOME=str(ROOT),
               ORBIT_NO_NOTIFY="1")   # no real desktop/Slack notifications from the test suite
    env.pop("ORBIT_RUNTIME_CMD", None)
    env.pop("ORBIT_QA_CMD", None)
    if agent_cmd:
        env["ORBIT_RUNTIME_CMD"] = agent_cmd
    if qa_agent_cmd:
        env["ORBIT_QA_CMD"] = qa_agent_cmd
    argv = [sys.executable, str(CHECK), tid, str(repo)]
    if patch is not None:
        argv.append(patch)
    return subprocess.run(argv, capture_output=True, text=True, env=env)


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


def test_qa_invalid_verdict_string_ships_with_a_warning(tmp_path):
    # verdict must be exactly one of pass/fail/unable — an agent mistake
    # (e.g. "passed" instead of "pass") is a wiring/agent error, never an
    # observed contradiction, so it must ship (with a loud WARN), not block.
    repo = target(tmp_path, QA_CFG)
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "passed", "contradicts": False, "summary": "hmm"}))
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

    generated = repo / ".autopilot" / "state" / "qa" / "mcp-run-task-t1.json"
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

    generated = repo / ".autopilot" / "state" / "qa" / "mcp-run-task-t1.json"
    args = json.loads(generated.read_text())["mcpServers"]["playwright"]["args"]
    assert "--headless" not in args


def test_qa_mcp_autogen_precreates_output_dir(tmp_path):
    # `--output-dir` points at $AP_STATE/qa/task-<tid>/ — pre-create it here
    # rather than leaving it solely to the skill's own mkdir, which can
    # otherwise race the real @playwright/mcp server starting up and writing
    # into that same directory.
    repo = target(tmp_path, QA_CFG)
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "pass", "contradicts": False, "summary": "ok"}))
    assert r.returncode == 0
    out_dir = repo / ".autopilot" / "state" / "qa" / "task-t1"
    assert out_dir.is_dir()


def test_qa_mcp_config_override_used(tmp_path):
    custom = tmp_path / "custom-mcp.json"
    custom.write_text(json.dumps({"mcpServers": {"playwright": {"command": "echo"}}}))
    cfg = QA_CFG + f"  mcp_config: {json.dumps(str(custom))}\n"
    repo = target(tmp_path, cfg)
    mcp_log = tmp_path / "mcp2.txt"
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "pass", "contradicts": False, "summary": "ok"}, mcp_log=mcp_log))
    assert r.returncode == 0

    generated = repo / ".autopilot" / "state" / "qa" / "mcp-run-task-t1.json"
    assert not generated.exists()                    # nothing autogenerated

    assert mcp_log.read_text() == str(custom)


def test_build_mcp_config_task_and_flow_callers_never_collide(tmp_path, monkeypatch):
    # Phase 4 review finding: build_mcp_config used to write a FIXED
    # qa_dir/mcp-run.json regardless of caller, so the loop gate's qa_check
    # run and a dashboard-spawned qa_run.py --flow run (writing to the SAME
    # $AP_STATE/qa/) could clobber each other's MCP config concurrently.
    # Exercise qa_check.py's own task-scoped call site directly, and
    # qa_run.py's flow-scoped call site's exact args (see qa_run.py's
    # _flow_mcp_config — a one-line delegation to build_mcp_config; the
    # full subprocess path is already covered end-to-end by
    # test_qa_run.py's test_mcp_autogen_writes_flow_output_dir) — using the
    # SAME distinguisher ("dup") on both sides proves it's the task-/flow-
    # prefix baked into the filename, not just the distinguisher, that
    # keeps the two apart.
    ap_home = tmp_path / ".autopilot"
    ap_state = ap_home / "state"
    (ap_state / "qa").mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(ap_state))
    sys.path.insert(0, str(ENGINE))
    qc = _load_module("qa_check_dup_test", CHECK)

    qa_cfg: dict = {}
    task_path = qc._resolve_mcp_config("dup", qa_cfg)
    flow_path = qc.build_mcp_config(qa_cfg, ap_state / "qa", ap_state / "qa" / "flow-dup",
                                     "mcp-run-flow-dup.json")

    assert task_path != flow_path
    assert task_path.name == "mcp-run-task-dup.json"
    assert flow_path.name == "mcp-run-flow-dup.json"
    assert task_path.exists() and flow_path.exists()


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


def test_qa_path_removes_stale_cross_kind_runtime_verdict(tmp_path):
    # _unlink_stale_verdicts unlinks BOTH verdict kinds up front, whichever
    # path is about to run — a stale task-<id>-runtime.json left over from a
    # prior LEGACY run (config may have since gained a `qa:` block) must
    # never be mistaken for this QA-path run's own verdict, and must not
    # survive on disk as misleading leftover state either.
    repo = target(tmp_path, QA_CFG)
    reviews = repo / ".autopilot" / "state" / "reviews"
    stale_runtime = reviews / "task-t1-runtime.json"
    stale_runtime.write_text(json.dumps({"ran": True, "contradicts": True,
                                         "summary": "stale legacy verdict"}))
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "pass", "contradicts": False, "summary": "ok"}))
    assert r.returncode == 0
    assert not stale_runtime.exists()


# ===========================================================================
# Section 5 — dev⇄QA bounce protocol (Task 16): decide_fail_action on a
# fail/required verdict either bounces the SAME task back onto the queue
# (qa_bounces < qa.max_bounces → exit 4) or escalates with evidence attached
# at the cap (exit 3) — with a safe-fallback-to-escalate on any error.
# ===========================================================================

def bounce_target(tmp_path, max_bounces=2, qa_bounces=None, category="feature",
                  ledger_state="committed"):
    """A QA-gated target (config.yaml + queue.json via target()) PLUS the
    backlog.yaml + ledger.json the bounce protocol reads/writes.
    `ledger_state=None` → no ledger.json entry at all (tests the CRITICAL
    missing-entry WARN)."""
    cfg = textwrap.dedent(f"""\
        qa:
          max_bounces: {max_bounces}
          gate_categories:
            {category}: required
        """)
    repo = target(tmp_path, cfg, category=category)
    ap = repo / ".autopilot"
    lines = [
        "tasks:",
        "  - id: t1",
        '    title: "Fix the login button click handler"',
        "    status: queued",
        "    autopilot: allow",
        f"    category: {category}",
    ]
    if qa_bounces is not None:
        lines.append(f"    qa_bounces: {qa_bounces}")
    lines += [
        "    acceptance_criteria:",
        '      - "Clicking login navigates to the dashboard"',
        "",
    ]
    (ap / "backlog.yaml").write_text("\n".join(lines))
    if ledger_state is not None:
        (ap / "state" / "ledger.json").write_text(json.dumps(
            {"entries": {"t1": {"state": ledger_state, "title": "fix login"}}}))
    return repo


def _backlog_text(repo):
    return (repo / ".autopilot" / "backlog.yaml").read_text()


def _ledger_entries(repo):
    p = repo / ".autopilot" / "state" / "ledger.json"
    return json.loads(p.read_text())["entries"] if p.exists() else {}


def _queue_tasks(repo):
    p = repo / ".autopilot" / "state" / "queue.json"
    return json.loads(p.read_text()).get("tasks", []) if p.exists() else []


def _qa_findings_value(text):
    m = re.search(r'(?m)^\s*qa_findings:(.*)$', text)
    assert m, f"no qa_findings field in:\n{text}"
    return json.loads(m.group(1).strip())


def test_qa_fail_bounces_below_cap(tmp_path):
    repo = bounce_target(tmp_path, max_bounces=2, qa_bounces=None)   # absent → reads as 0
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True,
        "summary": "login button contradicted"}))
    assert r.returncode == 4, r.stdout + r.stderr

    text = _backlog_text(repo)
    assert re.search(r"(?m)^\s*qa_bounces: 1\s*$", text), text
    assert re.search(r"(?m)^\s*status: queued\s*$", text), text
    findings = _qa_findings_value(text)
    assert "login button contradicted" in findings
    # $AP_STATE-relative (not the wrapper-only .autopilot/state/... path) —
    # the re-picked maker's cwd is the WORKTREE, a sibling dir, where that
    # relative path doesn't exist, and it breaks under non-default AP_STATE.
    assert "$AP_STATE/qa/task-t1/findings.md" in findings
    assert "previous attempt patch: none kept" in findings   # no patch arg passed

    # qa-verdict is stamped, then clear immediately removes the whole entry —
    # clear wins; the ledger ends up with NO entry for a bounced task.
    assert _ledger_entries(repo) == {}

    tasks = _queue_tasks(repo)
    assert len(tasks) == 1 and tasks[0]["id"] == "t1"
    assert "QA FINDINGS" in tasks[0]["task"]
    assert "login button contradicted" in tasks[0]["task"]


def test_qa_fail_second_bounce_survives_existing_qa_findings_field(tmp_path):
    # Regression for the set_task_field regex-template bug (Phase 3 finding
    # #1): the FIRST bounce inserts a fresh qa_findings line (no existing
    # field to match → the insert branch, which never touches pat.sub at
    # all). Only the SECOND bounce has to SUBSTITUTE that now-existing line
    # — this is exactly the path that used to raise `re.error: bad escape
    # \u` the instant json.dumps's em-dash escape landed inside the
    # replacement TEMPLATE. Before the fix this crashed decide_fail_action,
    # which fell back to a plain escalate (exit 3) with NO evidence attached
    # (the crash-fallback bypasses _escalate_with_evidence) instead of the
    # correct exit 4 re-queue.
    repo = bounce_target(tmp_path, max_bounces=2, qa_bounces=None)   # absent → reads as 0
    agent_cmd = qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True,
        "summary": "login button contradicted"})

    r1 = run(repo, qa_agent_cmd=agent_cmd)
    assert r1.returncode == 4, r1.stdout + r1.stderr
    assert re.search(r"(?m)^\s*qa_bounces: 1\s*$", _backlog_text(repo))
    assert re.search(r"(?m)^\s*qa_findings:", _backlog_text(repo))   # field now exists

    r2 = run(repo, qa_agent_cmd=agent_cmd)                # SECOND bounce — substitute path
    assert r2.returncode == 4, r2.stdout + r2.stderr       # not 3 — no crash-to-escalate
    assert "re.error" not in (r2.stdout + r2.stderr)

    text = _backlog_text(repo)
    assert re.search(r"(?m)^\s*qa_bounces: 2\s*$", text), text
    assert len(re.findall(r"(?m)^\s*qa_findings:", text)) == 1   # substituted, not duplicated
    findings = _qa_findings_value(text)
    assert "login button contradicted" in findings


def test_qa_fail_bounce_records_patch_path(tmp_path):
    repo = bounce_target(tmp_path, max_bounces=2, qa_bounces=1)
    patch_file = tmp_path / "cycle.patch"
    patch_file.write_text("diff --git a/x b/x\n")
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True, "summary": "still broken"}),
        patch=str(patch_file))
    assert r.returncode == 4, r.stdout + r.stderr

    text = _backlog_text(repo)
    assert re.search(r"(?m)^\s*qa_bounces: 2\s*$", text), text
    findings = _qa_findings_value(text)
    assert f"previous attempt patch: {patch_file}" in findings


def test_qa_fail_bounces_tolerates_inline_comment_on_qa_bounces(tmp_path):
    # qa_bounces: 2  # note — a hand-annotated field. Before the
    # comment-tolerant get_task_field fix, the raw "2  # note" failed
    # int(), _read_qa_bounces silently defaulted to 0, and a task already
    # AT the cap bounced again instead of escalating.
    repo = bounce_target(tmp_path, max_bounces=2, qa_bounces="2  # note")
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True, "summary": "still broken"}))
    assert r.returncode == 3, r.stdout + r.stderr   # escalates (cap reached), NOT a bounce


def test_qa_fail_bounces_tolerates_hand_quoted_qa_bounces_at_cap(tmp_path):
    # qa_bounces: "2" — a hand-quoted scalar (get_task_field returns it raw,
    # quotes and all: `'"2"'`). Before stripping surrounding quotes,
    # int('"2"') raised ValueError, _read_qa_bounces silently defaulted to 0,
    # and a task already AT the cap bounced again instead of escalating.
    repo = bounce_target(tmp_path, max_bounces=2, qa_bounces='"2"')
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True, "summary": "still broken"}))
    assert r.returncode == 3, r.stdout + r.stderr   # escalates (cap reached), NOT a bounce


def test_qa_max_bounces_zero_escalates_immediately(tmp_path):
    # max_bounces: 0 is a legitimate "never bounce, always escalate" config —
    # `int(qa_cfg.get("max_bounces") or DEFAULT_MAX_BOUNCES)` treated 0 as
    # falsy and silently substituted the default (2), so a fresh task
    # (qa_bounces absent → 0) would bounce instead of escalating straight
    # away. `bounces < max_bounces` must be `0 < 0` (False) → escalate.
    repo = bounce_target(tmp_path, max_bounces=0, qa_bounces=None)
    qa_dir = repo / ".autopilot" / "state" / "qa" / "task-t1"
    qa_dir.mkdir(parents=True)
    (qa_dir / "findings.md").write_text("drove the login flow; button did nothing")
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True, "summary": "login dead"}))
    assert r.returncode == 3, r.stdout + r.stderr   # straight to escalation, never 4
    assert not re.search(r"(?m)^\s*qa_bounces: 1\s*$", _backlog_text(repo))  # not bounced
    e = _ledger_entries(repo)["t1"]
    assert e["qa"]["verdict"] == "fail"                 # escalated WITH evidence attached
    kinds = {a["kind"] for a in e.get("attachments", [])}
    assert "report" in kinds


def test_qa_fail_argv2_only_still_works_no_patch_arg(tmp_path):
    # run.sh's 3rd argv (patch) is optional — argv-2-only invocations (any
    # caller that predates this change) must still bounce cleanly.
    repo = bounce_target(tmp_path, max_bounces=2, qa_bounces=0)
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True, "summary": "x"}))   # patch=None
    assert r.returncode == 4, r.stdout + r.stderr


def test_qa_fail_escalates_at_cap_with_attachments(tmp_path):
    repo = bounce_target(tmp_path, max_bounces=2, qa_bounces=2)
    qa_dir = repo / ".autopilot" / "state" / "qa" / "task-t1"
    qa_dir.mkdir(parents=True)
    (qa_dir / "01-login.png").write_bytes(b"fake png")
    (qa_dir / "findings.md").write_text("drove the login flow; button did nothing")

    before_text = _backlog_text(repo)
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True, "summary": "login dead",
        "screenshots": ["task-t1/01-login.png"]}))
    assert r.returncode == 3, r.stdout + r.stderr

    assert _backlog_text(repo) == before_text     # NOT re-queued — cap reached, backlog untouched

    entries = _ledger_entries(repo)
    e = entries["t1"]
    assert e["qa"]["verdict"] == "fail" and e["qa"]["summary"] == "login dead"
    kinds = {a["kind"]: a["path"] for a in e.get("attachments", [])}
    # Relative to $AP_STATE/qa/ — NOT resolved/absolute — so command_center.py's
    # /qa-shot and /qa-report routes (which reject absolute `p`) can serve them.
    assert kinds["screenshot"] == "task-t1/01-login.png"
    assert kinds["report"] == "task-t1/findings.md"
    _QA_DASHBOARD_PATH_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,200}$")
    for p in kinds.values():
        assert _QA_DASHBOARD_PATH_RE.match(p), p
        assert not os.path.isabs(p), p


def test_qa_fail_at_cap_no_screenshots_or_report_attaches_nothing(tmp_path):
    repo = bounce_target(tmp_path, max_bounces=2, qa_bounces=2)   # no qa/task-t1/ dir at all
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True, "summary": "login dead"}))
    assert r.returncode == 3, r.stdout + r.stderr
    e = _ledger_entries(repo)["t1"]
    assert e["qa"]["verdict"] == "fail"
    assert not e.get("attachments")
    assert "WARN" in r.stdout


def test_qa_fail_at_cap_traversal_screenshot_rejected(tmp_path):
    repo = bounce_target(tmp_path, max_bounces=2, qa_bounces=2)
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True, "summary": "login dead",
        "screenshots": ["../../etc/x.png"]}))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "WARN" in r.stdout

    entries = _ledger_entries(repo)
    atts = entries.get("t1", {}).get("attachments", [])
    assert not any(a["kind"] == "screenshot" for a in atts)


def test_qa_fail_at_cap_absolute_screenshot_rejected(tmp_path):
    repo = bounce_target(tmp_path, max_bounces=2, qa_bounces=2)
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True, "summary": "login dead",
        "screenshots": ["/etc/passwd"]}))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "WARN" in r.stdout
    entries = _ledger_entries(repo)
    atts = entries.get("t1", {}).get("attachments", [])
    assert not any(a["kind"] == "screenshot" for a in atts)


def test_qa_fail_at_cap_missing_ledger_entry_warns_loudly(tmp_path):
    repo = bounce_target(tmp_path, max_bounces=2, qa_bounces=2, ledger_state=None)
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True, "summary": "login dead"}))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "WARN" in r.stdout and "no ledger entry" in r.stdout.lower()
    assert _ledger_entries(repo) == {}   # confirmed: still nothing recorded — no silent success


def test_qa_fail_bounce_path_exception_falls_back_to_escalate(tmp_path):
    # No backlog.yaml at all (unlike a normal target, which never creates one
    # either) — reading qa_bounces raises inside the bounce path. Must fail
    # SAFE (escalate), never half-bounce and never silently ship.
    cfg = textwrap.dedent("""\
        qa:
          max_bounces: 2
          gate_categories:
            feature: required
        """)
    repo = target(tmp_path, cfg, category="feature")
    r = run(repo, qa_agent_cmd=qa_agent(tmp_path, {
        "ran": True, "verdict": "fail", "contradicts": True, "summary": "x broke"}))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "WARN" in r.stdout
    assert "escalat" in r.stdout.lower()
