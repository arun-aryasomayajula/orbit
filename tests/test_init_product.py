import subprocess, sys
from pathlib import Path

ORBIT = Path(__file__).resolve().parents[1]

def _init_product(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    r = subprocess.run([sys.executable, str(ORBIT / "install" / "init.py"),
                        str(tmp_path), str(ORBIT), "--product"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    return r

def test_product_scaffold_creates_workspace_dirs(tmp_path):
    _init_product(tmp_path)
    for d in ("knowledge", "insights", "research", "surveys", ".autopilot"):
        assert (tmp_path / d).is_dir(), d
    assert (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / ".autopilot" / "backlog.yaml").exists()

def test_product_config_validates_and_carries_insight_gate(tmp_path):
    _init_product(tmp_path)
    cfg = (tmp_path / ".autopilot" / "config.yaml").read_text()
    assert "insight_lint" in cfg
    assert "workable_categories: [insight, documentation]" in cfg
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "config.py"),
                        "validate", str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout

def test_product_scaffold_installs_commands_and_agents(tmp_path):
    _init_product(tmp_path)
    assert (tmp_path / ".claude" / "commands" / "orbit-insight.md").exists()
    assert (tmp_path / ".claude" / "agents" / "analyst.md").exists()

def test_code_scaffold_is_unchanged_without_flag(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run([sys.executable, str(ORBIT / "install" / "init.py"),
                    str(tmp_path), str(ORBIT)], capture_output=True, text=True, check=True)
    cfg = (tmp_path / ".autopilot" / "config.yaml").read_text()
    assert "insight_lint" not in cfg
    assert not (tmp_path / "insights").exists()

def test_insight_is_a_safe_category(monkeypatch, tmp_path):
    monkeypatch.setenv("AP_HOME", str(tmp_path / ".autopilot"))
    sys.path.insert(0, str(ORBIT / "engine"))
    import backlog_to_tasks
    assert "insight" in backlog_to_tasks.SAFE_CATEGORIES

def test_init_rejects_unknown_flag(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    r = subprocess.run([sys.executable, str(ORBIT / "install" / "init.py"),
                        str(tmp_path), str(ORBIT), "--produkt"],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert not (tmp_path / ".autopilot" / "config.yaml").exists()

def test_insight_known_to_lint_and_dashboard():
    import importlib.util
    def _load(name):
        spec = importlib.util.spec_from_file_location(name, str(ORBIT / "engine" / f"{name}.py"))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
    import os
    os.environ.setdefault("AP_HOME", "/tmp/x")  # backlog_lint has no AP_HOME need; guard anyway
    bl = _load("backlog_lint")
    assert "insight" in bl.KNOWN_CATEGORIES

def test_doctor_warns_on_missing_mcp_token_env(tmp_path):
    _init_product(tmp_path)
    cfg = tmp_path / ".autopilot" / "config.yaml"
    cfg.write_text(cfg.read_text() + '\nmcp:\n  metaql:\n    transport: http\n'
                   '    url: "https://m/mcp"\n    token_env: METAQL_TOKEN\n    tools: "*"\n')
    import os
    env = dict(os.environ, ORBIT_HOME=str(ORBIT))
    env.pop("METAQL_TOKEN", None)
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)
    assert "METAQL_TOKEN" in r.stdout
