# The feature-build status rename: "pushed" → "built". The old name collided
# with the ledger's `pushed` lifecycle state (different store, different
# machine). Legacy feature_builds.json rows written before the rename must
# normalize on read everywhere — including through the dedup guard, so a
# legacy-status build still blocks a redundant rebuild.
#
# Run: python3 -m pytest tests/test_feature_build_status.py -v   (from orbit/)
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, ENGINE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def ap(tmp_path, monkeypatch):
    home = tmp_path / ".autopilot"
    (home / "state").mkdir(parents=True)
    (home / "backlog.yaml").write_text("tasks: []\n")
    (home / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates: {t: {cmd: "true"}}\n')
    monkeypatch.setenv("AP_HOME", str(home))
    monkeypatch.setenv("AP_STATE", str(home / "state"))
    return home / "state"


def _write_registry(state, status, merged=False):
    (state / "feature_builds.json").write_text(json.dumps({"builds": [
        {"id": "feat-1", "title": "big feature", "branch": "feature/feat-1",
         "status": status, "merged": merged}]}))


def test_legacy_pushed_rows_normalize_to_built(ap):
    _write_registry(ap, "pushed")
    fb = _mod("feature_build")
    assert fb._load()[0]["status"] == "built"


def test_legacy_row_still_blocks_a_redundant_rebuild(ap):
    _write_registry(ap, "pushed", merged=False)
    fb = _mod("feature_build")
    msg = fb.cmd_start("feat-1", "big feature", "feature/feat-1")
    assert "already has a built branch" in msg
    assert "feature/feat-1" in msg


def test_command_center_loader_normalizes_too(ap):
    _write_registry(ap, "pushed")
    cc = _mod("command_center")
    assert cc._load_builds()[0]["status"] == "built"
