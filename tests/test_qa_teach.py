# qa_teach.py contract tests — `orbit teach`: record a browser flow by
# demonstration into a committed flow library (Phase 4, Task 20).
#
# Subprocess-driven (mirrors tests/test_qa_check.py's ORBIT_QA_CMD fakes):
# ORBIT_CODEGEN_CMD replaces the real `npx playwright codegen` call verbatim
# (same substitution mechanism as ORBIT_QA_CMD/ORBIT_RUNTIME_CMD) — a fake
# script reads ORBIT_CODEGEN_OUTPUT (and ORBIT_CODEGEN_URL) from the env
# (never argv, since an override replaces argv wholesale) and writes the
# recorded spec file itself. Interactive prompts (intent + criteria) are
# fed via piped stdin.
#
# Run: python3.11 -m pytest tests/test_qa_teach.py -v   (from orbit/)
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
TEACH = ENGINE / "qa_teach.py"

SPEC_CONTENT = (
    "import { test } from '@playwright/test';\n"
    "test('recorded flow', async ({ page }) => {\n"
    "  await page.goto('http://example.test/app');\n"
    "});\n"
)

QA_CFG = textwrap.dedent("""\
    qa:
      base_url: "http://example.test/app"
    """)


def repo(tmp_path, extra_cfg=""):
    r = tmp_path / "repo"
    ap = r / ".autopilot"
    ap.mkdir(parents=True)
    (ap / "config.yaml").write_text(
        'repo: "."\nbase_branch: "main"\ngates: {t: {cmd: "true"}}\n' + extra_cfg)
    return r


def fake_codegen(tmp_path, content=SPEC_CONTENT, rc=0, write=True, name="codegen"):
    """A fake ORBIT_CODEGEN_CMD target: reads the output path off
    ORBIT_CODEGEN_OUTPUT (never argv — an override replaces argv wholesale)
    and writes `content` there (unless write=False, simulating a recorder
    that produced nothing), then exits `rc`."""
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import os, sys\n"
        "out = os.environ.get('ORBIT_CODEGEN_OUTPUT')\n"
        f"if out and {write!r}:\n"
        f"    open(out, 'w').write({content!r})\n"
        f"sys.exit({rc})\n"
    )
    return f"{sys.executable} {script}"


def run(repo_path, name, codegen_cmd=None, stdin_text="", url=None, timeout=30):
    env = dict(os.environ)
    env.pop("ORBIT_CODEGEN_CMD", None)
    if codegen_cmd:
        env["ORBIT_CODEGEN_CMD"] = codegen_cmd
    argv = [sys.executable, str(TEACH), str(repo_path), name]
    if url:
        argv += ["--url", url]
    return subprocess.run(argv, input=stdin_text, capture_output=True, text=True,
                          env=env, timeout=timeout)


def flows_dir(r, sub="qa/flows"):
    return r / ".autopilot" / sub


def flowmap_path(r, sub="qa"):
    return r / ".autopilot" / sub / "FLOWMAP.md"


# ===========================================================================
# Happy path — a complete, binding-format-compliant flow dir.
# ===========================================================================

def test_happy_path_writes_complete_flow(tmp_path):
    r = repo(tmp_path, QA_CFG)
    result = run(r, "login-flow", codegen_cmd=fake_codegen(tmp_path),
                 stdin_text="user can log in\nsees the dashboard\nsees their name in the header\n")
    assert result.returncode == 0, result.stdout + result.stderr

    fd = flows_dir(r) / "login-flow"
    assert fd.is_dir()

    intent_text = (fd / "intent.md").read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", intent_text, re.DOTALL)
    assert m, intent_text
    front = yaml.safe_load(m.group(1))
    assert front["name"] == "login-flow"
    assert front["entry_path"] == "http://example.test/app"
    assert "created" in front and front["created"]
    assert front["tags"] == []

    body = m.group(2)
    assert "user can log in" in body
    assert "## Success criteria" in body
    assert "- sees the dashboard" in body
    assert "- sees their name in the header" in body

    spec = (fd / "steps.spec.ts").read_text()
    assert spec == SPEC_CONTENT

    # steps.json is a Task-22 artifact — teach must never write it.
    assert not (fd / "steps.json").exists()

    meta = json.loads((fd / "meta.json").read_text())
    assert set(meta.keys()) == {"created_at", "last_run_at", "last_verdict", "runs", "heals"}
    assert meta["created_at"]
    assert meta["last_run_at"] is None
    assert meta["last_verdict"] is None
    assert meta["runs"] == 0
    assert meta["heals"] == []

    flowmap = flowmap_path(r).read_text()
    assert "| login-flow |" in flowmap
    assert "user can log in" in flowmap
    assert "http://example.test/app" in flowmap

    assert "git add" in result.stdout
    assert ".autopilot/qa/flows/login-flow" in result.stdout


def test_url_flag_overrides_base_url(tmp_path):
    r = repo(tmp_path, QA_CFG)
    result = run(r, "override-flow", codegen_cmd=fake_codegen(tmp_path),
                 stdin_text="intent line\ncriterion one\n",
                 url="http://override.test/start")
    assert result.returncode == 0, result.stdout + result.stderr
    intent_text = (flows_dir(r) / "override-flow" / "intent.md").read_text()
    front = yaml.safe_load(re.match(r"^---\n(.*?)\n---\n", intent_text, re.DOTALL).group(1))
    assert front["entry_path"] == "http://override.test/start"


# ===========================================================================
# Second flow appends a second FLOWMAP row without clobbering the first.
# ===========================================================================

def test_second_flow_appends_second_flowmap_row(tmp_path):
    r = repo(tmp_path, QA_CFG)
    r1 = run(r, "flow-one", codegen_cmd=fake_codegen(tmp_path, name="c1"),
             stdin_text="first flow intent\ncriterion a\n")
    assert r1.returncode == 0, r1.stdout + r1.stderr
    r2 = run(r, "flow-two", codegen_cmd=fake_codegen(tmp_path, name="c2"),
             stdin_text="second flow intent\ncriterion b\n")
    assert r2.returncode == 0, r2.stdout + r2.stderr

    flowmap = flowmap_path(r).read_text()
    assert flowmap.count("| flow-one |") == 1
    assert flowmap.count("| flow-two |") == 1
    assert "first flow intent" in flowmap
    assert "second flow intent" in flowmap
    assert (flows_dir(r) / "flow-one" / "meta.json").exists()
    assert (flows_dir(r) / "flow-two" / "meta.json").exists()


# ===========================================================================
# Existing flow name -> exit 2, dir untouched.
# ===========================================================================

def test_existing_flow_name_refused_dir_untouched(tmp_path):
    r = repo(tmp_path, QA_CFG)
    first = run(r, "dup-flow", codegen_cmd=fake_codegen(tmp_path, name="c1"),
                stdin_text="original intent\noriginal criterion\n")
    assert first.returncode == 0, first.stdout + first.stderr
    before = (flows_dir(r) / "dup-flow" / "meta.json").read_text()
    before_intent = (flows_dir(r) / "dup-flow" / "intent.md").read_text()

    second = run(r, "dup-flow", codegen_cmd=fake_codegen(tmp_path, name="c2"),
                 stdin_text="new intent\nnew criterion\n")
    assert second.returncode == 2

    after = (flows_dir(r) / "dup-flow" / "meta.json").read_text()
    after_intent = (flows_dir(r) / "dup-flow" / "intent.md").read_text()
    assert after == before
    assert after_intent == before_intent
    # only one row in FLOWMAP for dup-flow
    assert flowmap_path(r).read_text().count("| dup-flow |") == 1


# ===========================================================================
# Argument / config-shape errors -> exit 2, no flow dir ever created.
# ===========================================================================

def test_bad_slug_rejected(tmp_path):
    r = repo(tmp_path, QA_CFG)
    result = run(r, "Not_A_Slug!", codegen_cmd=fake_codegen(tmp_path))
    assert result.returncode == 2
    assert not (flows_dir(r) / "Not_A_Slug!").exists()


def test_no_qa_block_rejected(tmp_path):
    r = repo(tmp_path)  # no qa: block at all
    result = run(r, "some-flow", codegen_cmd=fake_codegen(tmp_path))
    assert result.returncode == 2
    assert "qa" in result.stderr.lower()
    assert not flows_dir(r).exists()


def test_no_url_and_no_base_url_exits_2(tmp_path):
    r = repo(tmp_path, "qa:\n  ready_timeout_seconds: 90\n")   # qa: present, no base_url
    result = run(r, "no-url-flow", codegen_cmd=fake_codegen(tmp_path))
    assert result.returncode == 2
    assert not (flows_dir(r) / "no-url-flow").exists()


# ===========================================================================
# Recorder failure paths -> exit 1, no flow dir left behind.
# ===========================================================================

def test_codegen_nonzero_exit_leaves_no_flow_dir(tmp_path):
    r = repo(tmp_path, QA_CFG)
    result = run(r, "broken-flow",
                 codegen_cmd=fake_codegen(tmp_path, rc=1, write=False),
                 stdin_text="intent\ncriterion\n")
    assert result.returncode == 1
    assert not (flows_dir(r) / "broken-flow").exists()
    assert not flowmap_path(r).exists()


def test_codegen_empty_output_leaves_no_flow_dir(tmp_path):
    r = repo(tmp_path, QA_CFG)
    result = run(r, "empty-flow",
                 codegen_cmd=fake_codegen(tmp_path, content="", rc=0),
                 stdin_text="intent\ncriterion\n")
    assert result.returncode == 1
    assert not (flows_dir(r) / "empty-flow").exists()


def test_codegen_no_output_at_all_leaves_no_flow_dir(tmp_path):
    r = repo(tmp_path, QA_CFG)
    result = run(r, "no-output-flow",
                 codegen_cmd=fake_codegen(tmp_path, write=False, rc=0),
                 stdin_text="intent\ncriterion\n")
    assert result.returncode == 1
    assert not (flows_dir(r) / "no-output-flow").exists()


# ===========================================================================
# Empty intent / no criteria on stdin -> clean error, no flow dir.
# ===========================================================================

def test_empty_intent_rejected_no_flow_dir(tmp_path):
    r = repo(tmp_path, QA_CFG)
    result = run(r, "empty-intent-flow", codegen_cmd=fake_codegen(tmp_path),
                 stdin_text="\ncriterion one\n")   # blank first line = empty intent
    assert result.returncode in (1, 2)
    assert not (flows_dir(r) / "empty-intent-flow").exists()


def test_intent_only_eof_before_any_criteria_rejected_no_flow_dir(tmp_path):
    r = repo(tmp_path, QA_CFG)
    result = run(r, "no-criteria-flow", codegen_cmd=fake_codegen(tmp_path),
                 stdin_text="an intent with nothing after it\n")   # EOF right after
    assert result.returncode in (1, 2)
    assert not (flows_dir(r) / "no-criteria-flow").exists()


def test_completely_empty_stdin_is_eof_safe(tmp_path):
    r = repo(tmp_path, QA_CFG)
    result = run(r, "eof-flow", codegen_cmd=fake_codegen(tmp_path), stdin_text="")
    assert result.returncode in (1, 2)
    assert not (flows_dir(r) / "eof-flow").exists()


# ===========================================================================
# flows_dir config key is honored (custom relative path).
# ===========================================================================

def test_custom_flows_dir_is_honored(tmp_path):
    cfg = 'qa:\n  base_url: "http://example.test/app"\n  flows_dir: "browser-flows"\n'
    r = repo(tmp_path, cfg)
    result = run(r, "custom-dir-flow", codegen_cmd=fake_codegen(tmp_path),
                 stdin_text="intent\ncriterion\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (r / ".autopilot" / "browser-flows" / "custom-dir-flow" / "meta.json").exists()
    assert (r / ".autopilot" / "FLOWMAP.md").exists()
    assert not (r / ".autopilot" / "qa" / "flows").exists()
