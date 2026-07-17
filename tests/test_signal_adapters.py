# The signals contract: `sources:` names resolve to <name>_to_backlog.py with
# the TARGET's .autopilot/adapters/ winning over the engine's; adapters run with
# cwd = the target repo root and AP_HOME/AP_STATE/ORBIT_HOME exported; a
# configured source with no adapter is reported loudly, never skipped silently.
#
# Run: python3 -m pytest tests/test_signal_adapters.py -v   (from orbit/)
import json
import os
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"

FAKE_ADAPTER = """\
import json, os, sys
from pathlib import Path
Path(os.environ["AP_STATE"], "adapter-ran.json").write_text(json.dumps({
    "cwd": os.getcwd(),
    "AP_HOME": os.environ.get("AP_HOME", ""),
    "ORBIT_HOME": os.environ.get("ORBIT_HOME", ""),
    "which": %r,
}))
"""


def _target(tmp_path, sources):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text(
        f'repo: "."\nbase_branch: "main"\nsources: {sources}\ngates:\n  t: {{cmd: "true"}}\n')
    (ap / "backlog.yaml").write_text("tasks: []\n")
    return tmp_path


def _convert(target):
    env = dict(os.environ, AP_HOME=str(target / ".autopilot"),
               AP_STATE=str(target / ".autopilot" / "state"))
    return subprocess.run([sys.executable, str(ENGINE / "backlog_to_tasks.py")],
                          capture_output=True, text=True, env=env)


def test_target_local_adapter_runs_with_contract_env_and_cwd(tmp_path):
    t = _target(tmp_path, "[backlog, mysignal]")
    ad = t / ".autopilot" / "adapters"
    ad.mkdir()
    (ad / "mysignal_to_backlog.py").write_text(FAKE_ADAPTER % "local")
    r = _convert(t)
    assert r.returncode == 0, r.stderr
    ran = json.loads((t / ".autopilot" / "state" / "adapter-ran.json").read_text())
    assert ran["which"] == "local"
    assert Path(ran["cwd"]).resolve() == t.resolve()      # cwd = target repo root
    assert Path(ran["AP_HOME"]).resolve() == (t / ".autopilot").resolve()
    assert ran["ORBIT_HOME"]                               # engine path exported


def test_target_local_adapter_wins_over_engine(tmp_path, monkeypatch):
    t = _target(tmp_path, "[backlog, dupsignal]")
    ad = t / ".autopilot" / "adapters"
    ad.mkdir()
    (ad / "dupsignal_to_backlog.py").write_text(FAKE_ADAPTER % "local")
    fake_engine_home = tmp_path / "fake-orbit"
    (fake_engine_home / "adapters").mkdir(parents=True)
    (fake_engine_home / "adapters" / "dupsignal_to_backlog.py").write_text(FAKE_ADAPTER % "engine")
    # point ORBIT_HOME at the fake engine so both locations offer the adapter
    env = dict(os.environ, AP_HOME=str(t / ".autopilot"),
               AP_STATE=str(t / ".autopilot" / "state"), ORBIT_HOME=str(fake_engine_home))
    r = subprocess.run([sys.executable, str(ENGINE / "backlog_to_tasks.py")],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    ran = json.loads((t / ".autopilot" / "state" / "adapter-ran.json").read_text())
    assert ran["which"] == "local"                         # target overrides engine


def test_missing_adapter_is_reported_loudly(tmp_path):
    t = _target(tmp_path, "[backlog, ghostsignal]")
    r = _convert(t)
    assert r.returncode == 0
    assert "WARN" in r.stdout and "ghostsignal" in r.stdout
