# The `plan:` config block is opt-in (absent = feature off; no consumer yet —
# the planner agent + cycle wiring land in later tasks). This covers only the
# config layer: schema validation of the block's three fields via
# `config.py validate`, plus the init scaffold turning it ON by default for
# newly-created target configs. Mirrors tests/test_knowledge_config.py.
#
# Run: python3.11 -m pytest tests/test_plan_config.py -v   (from orbit/)
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE = REPO_ROOT / "engine"
INIT_PY = REPO_ROOT / "install" / "init.py"


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


def test_validate_accepts_full_plan_block(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'plan:\n'
        '  categories: [bug, feature, refactor, code_quality, testing]\n'
        '  enforce: "verifier"\n'
        '  model: "sonnet"\n'
    ))))
    assert r.returncode == 0, r.stdout + r.stderr


def test_validate_absent_plan_block_passes(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path)))
    assert r.returncode == 0, r.stdout + r.stderr


def test_validate_rejects_bogus_enforce_value(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'plan:\n'
        '  enforce: "bogus"\n'
    ))))
    assert r.returncode == 1
    assert "plan.enforce" in r.stdout


def test_validate_rejects_non_list_categories(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'plan:\n'
        '  categories: "bug"\n'
    ))))
    assert r.returncode == 1
    assert "plan.categories" in r.stdout


def test_validate_rejects_empty_string_category(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'plan:\n'
        '  categories: [""]\n'
    ))))
    assert r.returncode == 1
    assert "plan.categories" in r.stdout


def test_validate_rejects_non_dict_plan(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, 'plan: "yes"\n')))
    assert r.returncode == 1
    assert "plan" in r.stdout


def test_validate_rejects_non_string_model(tmp_path):
    r = _run("config.py", "validate", str(_cfg_repo(tmp_path, (
        'plan:\n'
        '  model: 7\n'
    ))))
    assert r.returncode == 1
    assert "plan.model" in r.stdout


def test_init_scaffolds_active_plan_block(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    r = subprocess.run([sys.executable, str(INIT_PY), str(target), str(REPO_ROOT)],
                        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    cfg_path = target / ".autopilot" / "config.yaml"
    assert cfg_path.exists()
    cfg = yaml.safe_load(cfg_path.read_text())
    assert cfg is not None
    assert cfg.get("plan") == {
        "categories": ["bug", "feature", "refactor", "code_quality", "testing"],
        "enforce": "verifier",
    }
    # scaffolded config must itself pass validate (gates block is a comment-only
    # placeholder when nothing is auto-detected in an empty repo, so validate
    # is expected to fail on missing gates — but not on the plan block).
    v = _run("config.py", "validate", str(target))
    assert "plan" not in (v.stdout or "")
