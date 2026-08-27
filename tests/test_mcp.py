import json, os, subprocess, sys
from pathlib import Path

ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import mcp  # noqa: E402


def test_build_http_server_with_token_env():
    cfg = {"analytics": {"transport": "http", "url": "https://m.example/mcp",
                      "token_env": "ANALYTICS_TOKEN", "tools": ["analytics_execute_query"]}}
    out = mcp.build_mcp_config(cfg)
    srv = out["mcpServers"]["analytics"]
    assert srv["type"] == "http"
    assert srv["url"] == "https://m.example/mcp"
    # token must be an env reference, never a value
    assert srv["headers"]["Authorization"] == "Bearer ${ANALYTICS_TOKEN}"


def test_build_http_server_without_token_has_no_headers():
    cfg = {"analytics": {"transport": "http", "url": "https://m.example/mcp", "tools": "*"}}
    srv = mcp.build_mcp_config(cfg)["mcpServers"]["analytics"]
    assert "headers" not in srv


def test_build_stdio_server():
    cfg = {"figma": {"transport": "stdio", "command": "npx",
                     "args": ["-y", "figma-mcp"], "env_passthrough": ["FIGMA_TOKEN"],
                     "tools": "*"}}
    srv = mcp.build_mcp_config(cfg)["mcpServers"]["figma"]
    assert srv["command"] == "npx"
    assert srv["args"] == ["-y", "figma-mcp"]
    assert srv["env"] == {"FIGMA_TOKEN": "${FIGMA_TOKEN}"}
    assert "type" not in srv       # stdio is the claude-code default


def _mk_target(tmp_path, cfg_text):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text(cfg_text)
    return tmp_path


BASE_CFG = 'repo: "."\ngates:\n  t: {cmd: "true", cwd: "."}\n'


def test_cli_build_writes_file_and_prints_path(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'mcp:\n  analytics:\n    transport: http\n    url: "https://m/mcp"\n    tools: "*"\n')
    env = {k: v for k, v in os.environ.items() if k != "ORBIT_HOME"}
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "mcp.py"), "build", str(t)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    path = r.stdout.strip()
    assert path.endswith("mcp-config.json")
    data = json.loads(Path(path).read_text())
    assert "analytics" in data["mcpServers"]


def test_cli_build_no_mcp_block_prints_nothing(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG)
    env = {k: v for k, v in os.environ.items() if k != "ORBIT_HOME"}
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "mcp.py"), "build", str(t)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_permission_allows_tool_list_and_wildcard():
    cfg = {"analytics": {"transport": "http", "url": "u", "tools": ["a", "b"]},
           "figma": {"transport": "stdio", "command": "npx", "tools": "*"}}
    allows = mcp.mcp_permission_allows(cfg)
    assert "mcp__analytics__a" in allows and "mcp__analytics__b" in allows
    assert "mcp__figma__*" in allows
    assert "mcp__analytics__*" not in allows   # a tool list must NOT widen to the whole server


def test_permission_allows_omitted_tools_grants_nothing():
    cfg = {"analytics": {"transport": "http", "url": "u"}}
    assert mcp.mcp_permission_allows(cfg) == []


def test_build_settings_preserves_base_deny():
    base = {"permissions": {"allow": ["Read"], "deny": ["Bash(git push:*)"]}}
    out = mcp.build_settings({"m": {"transport": "http", "url": "u", "tools": ["q"]}}, base)
    assert "Bash(git push:*)" in out["permissions"]["deny"]
    assert "mcp__m__q" in out["permissions"]["allow"]
    assert "Read" in out["permissions"]["allow"]
    assert base["permissions"]["allow"] == ["Read"]   # base object untouched


def test_cli_settings_no_mcp_prints_base_path(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG)
    env = {k: v for k, v in os.environ.items() if k != "ORBIT_HOME"}
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "mcp.py"), "settings", str(t)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert r.stdout.strip() == str(ORBIT / "config" / "orbit.settings.json")


def test_validate_accepts_good_publish_block(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'publish:\n  confluence:\n    base_url: "https://wiki.example.com"\n'
                   '    space: "PROD"\n    parent_id: 42\n'
                   '  jira:\n    base_url: "https://jira.example.com"\n'
                   '    project: "ORB"\n    issue_type: "Task"\n')
    r = _validate(t)
    assert r.returncode == 0, r.stdout


def test_validate_rejects_non_dict_publish_block(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG + 'publish: "invalid"\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "publish must be a mapping" in r.stdout


def test_validate_rejects_missing_confluence_base_url(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'publish:\n  confluence:\n    space: "PROD"\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "publish.confluence.base_url" in r.stdout


def test_validate_rejects_empty_jira_project(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'publish:\n  jira:\n    base_url: "https://jira.example.com"\n'
                   '    project: ""\n    issue_type: "Task"\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "publish.jira.project" in r.stdout


def test_validate_accepts_confluence_without_parent_id(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'publish:\n  confluence:\n    base_url: "https://wiki.example.com"\n'
                   '    space: "PROD"\n')
    r = _validate(t)
    assert r.returncode == 0, r.stdout


def test_validate_rejects_invalid_confluence_parent_id(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'publish:\n  confluence:\n    base_url: "https://wiki.example.com"\n'
                   '    space: "PROD"\n    parent_id: {}\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "publish.confluence.parent_id" in r.stdout


def test_validate_rejects_empty_confluence_parent_id_string(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'publish:\n  confluence:\n    base_url: "https://wiki.example.com"\n'
                   '    space: "PROD"\n    parent_id: ""\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "publish.confluence.parent_id" in r.stdout


def test_validate_rejects_http_to_non_loopback_confluence(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'publish:\n  confluence:\n    base_url: "http://wiki.example.com"\n'
                   '    space: "PROD"\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "publish.confluence.base_url" in r.stdout and "loopback" in r.stdout


def test_validate_rejects_http_to_non_loopback_jira(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'publish:\n  jira:\n    base_url: "http://jira.example.com"\n'
                   '    project: "ORB"\n    issue_type: "Task"\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "publish.jira.base_url" in r.stdout and "loopback" in r.stdout


def test_validate_accepts_http_loopback_confluence(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'publish:\n  confluence:\n    base_url: "http://127.0.0.1:8090"\n'
                   '    space: "PROD"\n')
    r = _validate(t)
    assert r.returncode == 0, r.stdout


def test_validate_accepts_http_localhost_confluence(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'publish:\n  confluence:\n    base_url: "http://localhost:8090"\n'
                   '    space: "PROD"\n')
    r = _validate(t)
    assert r.returncode == 0, r.stdout


def test_cli_settings_with_mcp_writes_merged_file(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'mcp:\n  analytics:\n    transport: http\n    url: "https://m/mcp"\n    tools: [analytics_execute_query]\n')
    env = {k: v for k, v in os.environ.items() if k != "ORBIT_HOME"}
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "mcp.py"), "settings", str(t)],
                       capture_output=True, text=True, env=env)
    path = r.stdout.strip()
    assert path.endswith("settings.json")
    data = json.loads(Path(path).read_text())
    assert "mcp__analytics__analytics_execute_query" in data["permissions"]["allow"]
    # the base file's own protections must survive the merge
    assert "Bash(git push:*)" in data["permissions"]["deny"]


def _validate(target):
    env = {k: v for k, v in os.environ.items() if k != "ORBIT_HOME"}
    return subprocess.run([sys.executable, str(ORBIT / "engine" / "config.py"),
                           "validate", str(target)], capture_output=True, text=True, env=env)


def test_validate_rejects_http_server_without_url(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG + 'mcp:\n  m:\n    transport: http\n    tools: "*"\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "mcp.m" in r.stdout and "url" in r.stdout


def test_validate_rejects_bad_transport_and_bad_tools(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'mcp:\n  m:\n    transport: websocket\n    tools: 42\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "transport" in r.stdout and "tools" in r.stdout


def test_validate_rejects_missing_tools(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG + 'mcp:\n  m:\n    transport: http\n    url: "https://m/mcp"\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "mcp.m" in r.stdout and "tools" in r.stdout


def test_validate_accepts_good_mcp_block(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'mcp:\n  analytics:\n    transport: http\n    url: "https://m/mcp"\n'
                   '    token_env: ANALYTICS_TOKEN\n    tools: [a]\n'
                   '  figma:\n    transport: stdio\n    command: npx\n    args: ["-y", "x"]\n'
                   '    env_passthrough: [FIGMA_TOKEN]\n    tools: "*"\n')
    r = _validate(t)
    assert r.returncode == 0, r.stdout


def test_validate_accepts_good_prd_block(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG + 'prd:\n  model: "sonnet"\n  timeout_seconds: 1800\n')
    r = _validate(t)
    assert r.returncode == 0, r.stdout


def test_validate_rejects_bad_prd_block(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG + 'prd:\n  model: ""\n  timeout_seconds: -1\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "prd.model" in r.stdout and "prd.timeout_seconds" in r.stdout


def test_validate_accepts_good_watches_block(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'watches:\n  - name: wow-signup\n    question: "How did signup change WoW?"\n'
                   '    cadence_hours: 24\n    category: funnel\n'
                   '  - name: min-fields\n    question: "Any regressions?"\n')
    r = _validate(t)
    assert r.returncode == 0, r.stdout


def test_validate_rejects_bad_watch_name(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'watches:\n  - name: "Not A Slug!"\n    question: "q?"\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "watches[0].name" in r.stdout


def test_validate_rejects_missing_watch_question(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG + 'watches:\n  - name: foo\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "watches[0].question" in r.stdout


def test_validate_rejects_bad_watch_cadence_and_category(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'watches:\n  - name: foo\n    question: "q?"\n    cadence_hours: -5\n'
                   '    category: bogus\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "watches[0].cadence_hours" in r.stdout and "watches[0].category" in r.stdout


def test_validate_rejects_watches_not_a_list(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG + 'watches:\n  name: foo\n')
    r = _validate(t)
    assert r.returncode == 1
    assert "watches must be a list" in r.stdout
