# Verifies BASE_BRANCH resolution precedence: env override → target config.yaml
# → loud 'main' fallback. Guards against the silent-main regression.
import importlib, sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"


def _import_cc(tmp_path, monkeypatch, *, env=None, config_base=None):
    ap_home = tmp_path / ".autopilot"
    (ap_home / "state").mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(ap_home / "state"))
    for var in ("ORBIT_BASE_BRANCH", "AP_BASE_BRANCH"):
        monkeypatch.delenv(var, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    if config_base is not None:
        (ap_home / "config.yaml").write_text(f"base_branch: {config_base}\n")
    sys.path.insert(0, str(ENGINE))
    sys.modules.pop("command_center", None)
    mod = importlib.import_module("command_center")
    importlib.reload(mod)
    sys.path.remove(str(ENGINE))
    return mod


def test_env_override_wins(tmp_path, monkeypatch):
    mod = _import_cc(tmp_path, monkeypatch,
                     env={"ORBIT_BASE_BRANCH": "feature/x"}, config_base="main")
    assert mod._resolve_base_branch() == "feature/x"


def test_config_used_when_no_env(tmp_path, monkeypatch):
    mod = _import_cc(tmp_path, monkeypatch, config_base="feature/funnelhub-auto-loop")
    assert mod._resolve_base_branch() == "feature/funnelhub-auto-loop"


def test_loud_fallback_when_nothing_set(tmp_path, monkeypatch, capsys):
    mod = _import_cc(tmp_path, monkeypatch)
    assert mod._resolve_base_branch() == "main"
    assert "base_branch unresolved" in capsys.readouterr().err
