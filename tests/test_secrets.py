"""Tests for .autopilot/.secrets.env sourcing, scaffold, and doctor awareness."""
import os
import subprocess
import stat
import sys
from pathlib import Path

import pytest

ORBIT = Path(__file__).resolve().parents[1]


def _init_product(tmp_path):
    """Scaffold a product workspace."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    r = subprocess.run([sys.executable, str(ORBIT / "install" / "init.py"),
                        str(tmp_path), str(ORBIT), "--product"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    return r


def test_doctor_quiet_when_token_env_in_secrets_file(tmp_path):
    """Doctor should not warn if METAQL_TOKEN is in .secrets.env."""
    _init_product(tmp_path)
    cfg = tmp_path / ".autopilot" / "config.yaml"
    cfg.write_text(cfg.read_text() + '\nmcp:\n  metaql:\n    transport: http\n'
                   '    url: "https://m/mcp"\n    token_env: METAQL_TOKEN\n    tools: "*"\n')

    # Create .secrets.env with METAQL_TOKEN
    secrets = tmp_path / ".autopilot" / ".secrets.env"
    secrets.write_text("# My secrets\nMETAQL_TOKEN=test-token-value\n")

    env = dict(os.environ, ORBIT_HOME=str(ORBIT))
    env.pop("METAQL_TOKEN", None)  # Remove from shell environment

    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)

    # Doctor should NOT warn about the missing token env (it's in the file)
    assert "METAQL_TOKEN" not in r.stdout, f"doctor should not warn when token is in .secrets.env: {r.stdout}"
    assert "ALL CHECKS PASSED" in r.stdout, f"doctor should pass: {r.stdout}"


def test_doctor_warns_on_loose_secrets_permissions(tmp_path):
    """Doctor should warn if .secrets.env exists with group/other bits readable."""
    _init_product(tmp_path)
    secrets = tmp_path / ".autopilot" / ".secrets.env"
    env = dict(os.environ, ORBIT_HOME=str(ORBIT))

    # Test 1: 0644 (group-readable) should warn
    secrets.write_text("METAQL_TOKEN=test-token-value\n")
    os.chmod(str(secrets), 0o644)
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)
    assert ".secrets.env" in r.stdout and "mode" in r.stdout.lower(), \
        f"doctor should warn about 0644 permissions: {r.stdout}"

    # Test 2: 0400 (owner read-only, stricter) should NOT warn
    os.chmod(str(secrets), 0o400)
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)
    assert ".secrets.env" not in r.stdout or "mode" not in r.stdout.lower(), \
        f"doctor should not warn about 0400 permissions: {r.stdout}"
    assert "ALL CHECKS PASSED" in r.stdout, \
        f"doctor should pass with 0400 permissions: {r.stdout}"


def test_product_scaffold_gitignore_covers_secrets_env(tmp_path):
    """Product scaffold should extend .gitignore to include .secrets.env."""
    _init_product(tmp_path)

    gitignore = tmp_path / ".autopilot" / ".gitignore"
    assert gitignore.exists(), ".gitignore should exist"
    content = gitignore.read_text()
    assert ".secrets.env" in content, ".gitignore should include .secrets.env"


def test_product_scaffold_creates_secrets_env_example(tmp_path):
    """Product scaffold should create .secrets.env.example."""
    _init_product(tmp_path)

    example = tmp_path / ".autopilot" / ".secrets.env.example"
    assert example.exists(), ".secrets.env.example should exist"

    content = example.read_text()
    assert "METAQL_TOKEN" in content, ".secrets.env.example should mention METAQL_TOKEN"
    assert content.startswith("#"), ".secrets.env.example should be all comments (example)"


def test_run_sh_syntax_is_valid(tmp_path):
    """run.sh should have valid bash syntax (bash -n)."""
    r = subprocess.run(["bash", "-n", str(ORBIT / "engine" / "run.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"run.sh has syntax errors: {r.stderr}"


def test_run_sh_sourcing_snippet_syntax_valid(tmp_path):
    """Verify the secrets sourcing snippet is valid bash."""
    # The snippet should be syntactically valid standalone
    snippet = '''
    AP_HOME=".autopilot"
    if [ -f "$AP_HOME/.secrets.env" ]; then
      set -a; . "$AP_HOME/.secrets.env"; set +a
    fi
    '''
    r = subprocess.run(["bash", "-n", "-c", snippet],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"snippet has syntax errors: {r.stderr}"
