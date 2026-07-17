# PR raising is wrapper-only and opt-in: config `pull_requests` must default to
# "off", reject unknown values at validate time, flow into the loop env as
# ORBIT_PULL_REQUESTS, and the ledger must carry the opened PR's URL as metadata
# without disturbing the entry's lifecycle state.
#
# Run: python3 -m pytest tests/test_pull_requests.py -v   (from orbit/)
import json
import os
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"


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


def test_shellenv_defaults_pull_requests_off(tmp_path):
    r = _run("config.py", "shellenv", str(_cfg_repo(tmp_path)))
    assert r.returncode == 0
    assert "export ORBIT_PULL_REQUESTS=off" in r.stdout


def test_shellenv_exports_github_mode(tmp_path):
    r = _run("config.py", "shellenv", str(_cfg_repo(tmp_path, 'pull_requests: "github"\n')))
    assert r.returncode == 0
    assert "export ORBIT_PULL_REQUESTS=github" in r.stdout


def test_validate_rejects_unknown_pull_requests_value(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, 'pull_requests: "gitlab"\n')))
    assert r.returncode == 1
    assert "pull_requests" in r.stdout


def test_validate_accepts_github(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, 'pull_requests: "github"\n')))
    assert r.returncode == 0


def test_ledger_pr_records_url_without_touching_state(tmp_path):
    env = {"AP_STATE": str(tmp_path)}
    _run("ledger.py", "pushed", "t1", "origin/autopilot/task-t1", env=env)
    _run("ledger.py", "pr", "t1", "https://github.com/o/r/pull/7", env=env)
    entry = json.loads((tmp_path / "ledger.json").read_text())["entries"]["t1"]
    assert entry["pr_url"] == "https://github.com/o/r/pull/7"
    assert entry["state"] == "pushed"          # lifecycle untouched — PR is metadata
    show = _run("ledger.py", "show", env=env)
    assert "https://github.com/o/r/pull/7" in show.stdout
