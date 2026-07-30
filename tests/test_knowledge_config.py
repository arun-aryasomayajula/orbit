# The `knowledge:` config block is opt-in (absent = feature off, no consumer
# yet). This covers only the config layer: schema validation of the block's
# three fields via `config.py validate`. Mirrors tests/test_pull_requests.py.
#
# Run: python3.11 -m pytest tests/test_knowledge_config.py -v   (from orbit/)
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


def test_validate_accepts_full_knowledge_block(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'knowledge:\n'
        '  refresh: "auto"\n'
        '  stale_after_merges: 3\n'
        '  timeout_seconds: 1800\n'
    ))))
    assert r.returncode == 0, r.stdout + r.stderr


def test_validate_absent_knowledge_block_passes(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path)))
    assert r.returncode == 0, r.stdout + r.stderr


def test_validate_rejects_bogus_refresh_value(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'knowledge:\n'
        '  refresh: "bogus"\n'
    ))))
    assert r.returncode == 1
    assert "knowledge.refresh" in r.stdout


def test_validate_rejects_negative_stale_after_merges(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'knowledge:\n'
        '  stale_after_merges: -1\n'
    ))))
    assert r.returncode == 1
    assert "knowledge.stale_after_merges" in r.stdout


def test_validate_rejects_non_int_stale_after_merges(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'knowledge:\n'
        '  stale_after_merges: "three"\n'
    ))))
    assert r.returncode == 1
    assert "knowledge.stale_after_merges" in r.stdout


def test_validate_rejects_non_positive_timeout_seconds(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'knowledge:\n'
        '  timeout_seconds: 0\n'
    ))))
    assert r.returncode == 1
    assert "knowledge.timeout_seconds" in r.stdout


def test_validate_rejects_non_dict_knowledge(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, 'knowledge: "yes"\n')))
    assert r.returncode == 1
    assert "knowledge" in r.stdout
