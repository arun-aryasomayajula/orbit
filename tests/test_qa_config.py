# The `qa:` config block is opt-in (absent = feature off; qa_check.py itself
# lands in Task 14 — this covers only the config layer: schema validation via
# `config.py validate`, the QA permission-settings file (a drift-locked copy of
# config/orbit.settings.json with the Playwright MCP tools allowlisted), and
# doctor's read-only environment checks (node/npx on PATH). Mirrors
# tests/test_plan_config.py for the validate half and the Task 5/11 doctor
# sections of tests/test_knowledge.py for the doctor half.
#
# Run: python3.11 -m pytest tests/test_qa_config.py -v   (from orbit/)
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE = REPO_ROOT / "engine"
DOCTOR = ENGINE / "doctor.py"


def _cfg_repo(tmp_path, extra=""):
    ap = tmp_path / ".autopilot"
    ap.mkdir()
    (ap / "config.yaml").write_text(
        'repo: "."\nbase_branch: "main"\ngates:\n  test: {cmd: "true", cwd: "."}\n' + extra
    )
    return tmp_path


def _run(script, *args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, str(ENGINE / script), *args],
                          capture_output=True, text=True, env=e)


# === config.py validate() — schema/shape checks =============================

def test_validate_accepts_full_qa_block(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  services:\n'
        '    - name: api\n'
        '      launch: "npm run dev:api"\n'
        '      cwd: "server"\n'
        '      ready: "http://localhost:4000/health"\n'
        '    - name: web\n'
        '      launch: "npm run dev"\n'
        '      ready: "http://localhost:3000"\n'
        '  base_url: "http://localhost:3000"\n'
        '  env: {PORT: "3000"}\n'
        '  ready_timeout_seconds: 90\n'
        '  timeout_seconds: 1200\n'
        '  model: "sonnet"\n'
        '  mcp_config: null\n'
        '  browser: "chromium"\n'
        '  headless: true\n'
        '  max_bounces: 2\n'
        '  gate_categories:\n'
        '    frontend: required\n'
        '    feature: required\n'
        '    bug: evidence\n'
    ))))
    assert r.returncode == 0, r.stdout + r.stderr


def test_validate_absent_qa_block_passes(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path)))
    assert r.returncode == 0, r.stdout + r.stderr


def test_validate_rejects_non_dict_qa(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, 'qa: "yes"\n')))
    assert r.returncode == 1
    assert "qa" in r.stdout


def test_validate_rejects_services_not_a_list(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  services: "api"\n'
    ))))
    assert r.returncode == 1
    assert "qa.services" in r.stdout


def test_validate_rejects_service_missing_launch(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  services:\n'
        '    - name: api\n'
        '      cwd: "server"\n'
    ))))
    assert r.returncode == 1
    assert "qa.services[0].launch" in r.stdout


def test_validate_rejects_bogus_gate_category_value(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  gate_categories:\n'
        '    bug: sometimes\n'
    ))))
    assert r.returncode == 1
    assert "qa.gate_categories" in r.stdout


def test_validate_rejects_negative_max_bounces(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  max_bounces: -1\n'
    ))))
    assert r.returncode == 1
    assert "qa.max_bounces" in r.stdout


def test_validate_rejects_non_bool_headless(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  headless: "yes"\n'
    ))))
    assert r.returncode == 1
    assert "qa.headless" in r.stdout


def test_validate_rejects_non_positive_ready_timeout_seconds(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  ready_timeout_seconds: 0\n'
    ))))
    assert r.returncode == 1
    assert "qa.ready_timeout_seconds" in r.stdout


def test_validate_rejects_non_string_base_url(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  base_url: 7\n'
    ))))
    assert r.returncode == 1
    assert "qa.base_url" in r.stdout


def test_validate_accepts_null_mcp_config(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  mcp_config: null\n'
    ))))
    assert r.returncode == 0, r.stdout + r.stderr


def test_validate_rejects_non_string_mcp_config(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  mcp_config: 7\n'
    ))))
    assert r.returncode == 1
    assert "qa.mcp_config" in r.stdout


def test_validate_rejects_non_string_env_value(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  env: {PORT: 3000}\n'
    ))))
    assert r.returncode == 1
    assert "qa.env" in r.stdout


def test_validate_accepts_default_flows_dir(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  flows_dir: "qa/flows"\n'
    ))))
    assert r.returncode == 0, r.stdout + r.stderr


def test_validate_accepts_custom_flows_dir(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  flows_dir: "browser-flows"\n'
    ))))
    assert r.returncode == 0, r.stdout + r.stderr


def test_validate_rejects_absolute_flows_dir(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  flows_dir: "/abs"\n'
    ))))
    assert r.returncode == 1
    assert "qa.flows_dir" in r.stdout


def test_validate_rejects_flows_dir_with_dotdot(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  flows_dir: "../x"\n'
    ))))
    assert r.returncode == 1
    assert "qa.flows_dir" in r.stdout


def test_validate_rejects_empty_flows_dir(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'qa:\n'
        '  flows_dir: ""\n'
    ))))
    assert r.returncode == 1
    assert "qa.flows_dir" in r.stdout


# === QA permission-settings file — drift-locked to config/orbit.settings.json

def test_qa_settings_file_exists_and_is_valid_json():
    qa_path = REPO_ROOT / "config" / "orbit.qa.settings.json"
    assert qa_path.exists(), qa_path
    json.loads(qa_path.read_text())  # must parse


def test_qa_settings_differs_from_dev_settings_only_by_playwright_allow():
    dev = json.loads((REPO_ROOT / "config" / "orbit.settings.json").read_text())
    qa = json.loads((REPO_ROOT / "config" / "orbit.qa.settings.json").read_text())

    dev_allow = dev["permissions"]["allow"]
    qa_allow = qa["permissions"]["allow"]
    assert qa_allow == dev_allow + ["mcp__playwright__*"], (
        "qa settings' permissions.allow must be the dev settings' allow list "
        "plus exactly one appended entry: mcp__playwright__*"
    )

    dev_perms_rest = {k: v for k, v in dev["permissions"].items() if k != "allow"}
    qa_perms_rest = {k: v for k, v in qa["permissions"].items() if k != "allow"}
    assert dev_perms_rest == qa_perms_rest, "ask/deny (and any other permissions keys) must be identical"

    dev_rest = {k: v for k, v in dev.items() if k != "permissions"}
    qa_rest = {k: v for k, v in qa.items() if k != "permissions"}
    assert dev_rest == qa_rest, "top-level keys outside permissions must be identical"


# === doctor.py — read-only QA environment checks ============================

def _target(tmp_path):
    ap = tmp_path / ".autopilot"
    ap.mkdir(parents=True)
    (ap / "config.yaml").write_text(
        'repo: "."\nbase_branch: "main"\ngates:\n  test: {cmd: "true", cwd: "."}\n')
    return tmp_path


def _configure_qa(t, extra="", also_runtime_check=False):
    lines = ['repo: "."\n', 'base_branch: "main"\n', 'gates:\n', '  test: {cmd: "true", cwd: "."}\n']
    if also_runtime_check:
        lines += ['runtime_check:\n', '  launch: "npm run dev"\n']
    lines += ['qa:\n', '  base_url: "http://localhost:3000"\n']
    if extra:
        lines.append(extra)
    (t / ".autopilot" / "config.yaml").write_text("".join(lines))


def _fake_bin(tmp_path, playwright_ok=True, name="fakebin"):
    """A tmp bin dir with executable `node` + `npx` stubs — `npx playwright
    --version` exits 0 or nonzero per `playwright_ok`, everything else is a no-op."""
    bindir = tmp_path / name
    bindir.mkdir()
    node = bindir / "node"
    node.write_text("#!/bin/sh\necho fake-node\n")
    node.chmod(0o755)
    npx = bindir / "npx"
    npx.write_text("#!/bin/sh\nexit {}\n".format(0 if playwright_ok else 1))
    npx.chmod(0o755)
    return bindir


def _run_doctor(target, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run([sys.executable, str(DOCTOR), str(target)],
                          capture_output=True, text=True, env=e)


def _check_lines(stdout, mark):
    return [ln.strip() for ln in stdout.splitlines() if ln.strip().startswith(mark)]


def test_doctor_qa_env_ok_with_node_and_npx_on_path(tmp_path):
    t = _target(tmp_path)
    _configure_qa(t)
    bindir = _fake_bin(tmp_path, playwright_ok=True)
    r = _run_doctor(t, env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
    oks = _check_lines(r.stdout, "✓")
    assert any(ln.startswith("✓ qa: node on PATH") for ln in oks), r.stdout
    assert any(ln.startswith("✓ qa: npx on PATH") for ln in oks), r.stdout
    assert not any(ln.startswith("✗ qa") for ln in _check_lines(r.stdout, "✗")), r.stdout
    # playwright resolves cleanly in this fixture -> no advisory line
    assert not any("playwright" in ln for ln in _check_lines(r.stdout, "·"))


def test_doctor_qa_env_missing_node_and_npx_shows_fail_lines(tmp_path):
    t = _target(tmp_path)
    _configure_qa(t)
    r = _run_doctor(t, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
    fails = _check_lines(r.stdout, "✗")
    assert any(ln.startswith("✗ qa: node on PATH") and "node" in ln for ln in fails), r.stdout
    assert any(ln.startswith("✗ qa: npx on PATH") and "npx" in ln for ln in fails), r.stdout


def test_doctor_qa_playwright_missing_shows_advisory_line(tmp_path):
    t = _target(tmp_path)
    _configure_qa(t)
    bindir = _fake_bin(tmp_path, playwright_ok=False)
    r = _run_doctor(t, env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
    assert any("playwright" in ln for ln in _check_lines(r.stdout, "·")), r.stdout
    # advisory only — never a hard failure
    assert not any(ln.startswith("✗ qa") for ln in _check_lines(r.stdout, "✗")), r.stdout


def test_doctor_qa_and_runtime_check_both_present_shows_supersede_note(tmp_path):
    t = _target(tmp_path)
    _configure_qa(t, also_runtime_check=True)
    bindir = _fake_bin(tmp_path, playwright_ok=True)
    r = _run_doctor(t, env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
    assert any("supersedes runtime_check" in ln for ln in _check_lines(r.stdout, "·")), r.stdout


def test_doctor_no_qa_block_prints_nothing_new(tmp_path):
    # feature unused (no `qa:` block) -> zero behavior change: doctor emits no
    # qa ✓/✗/informational line. (Test name avoids the literal word "qa" in a
    # way that would collide with tmp_path substrings, mirroring the note in
    # tests/test_knowledge.py above test_doctor_feature_unused_prints_nothing_new.)
    t = _target(tmp_path)
    r = _run_doctor(t)
    all_lines = _check_lines(r.stdout, "✓") + _check_lines(r.stdout, "✗") + _check_lines(r.stdout, "·")
    assert not any(ln.lower().startswith("✓ qa") or ln.lower().startswith("✗ qa")
                   or "qa:" in ln for ln in all_lines), r.stdout
