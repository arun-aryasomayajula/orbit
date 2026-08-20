import json, os, subprocess, sys
from pathlib import Path

ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import mcp  # noqa: E402


def test_build_http_server_with_token_env():
    cfg = {"metaql": {"transport": "http", "url": "https://m.example/mcp",
                      "token_env": "METAQL_TOKEN", "tools": ["metaql_execute_query"]}}
    out = mcp.build_mcp_config(cfg)
    srv = out["mcpServers"]["metaql"]
    assert srv["type"] == "http"
    assert srv["url"] == "https://m.example/mcp"
    # token must be an env reference, never a value
    assert srv["headers"]["Authorization"] == "Bearer ${METAQL_TOKEN}"


def test_build_http_server_without_token_has_no_headers():
    cfg = {"metaql": {"transport": "http", "url": "https://m.example/mcp", "tools": "*"}}
    srv = mcp.build_mcp_config(cfg)["mcpServers"]["metaql"]
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
                   'mcp:\n  metaql:\n    transport: http\n    url: "https://m/mcp"\n    tools: "*"\n')
    env = {k: v for k, v in os.environ.items() if k != "ORBIT_HOME"}
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "mcp.py"), "build", str(t)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    path = r.stdout.strip()
    assert path.endswith("mcp-config.json")
    data = json.loads(Path(path).read_text())
    assert "metaql" in data["mcpServers"]


def test_cli_build_no_mcp_block_prints_nothing(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG)
    env = {k: v for k, v in os.environ.items() if k != "ORBIT_HOME"}
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "mcp.py"), "build", str(t)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_permission_allows_tool_list_and_wildcard():
    cfg = {"metaql": {"transport": "http", "url": "u", "tools": ["a", "b"]},
           "figma": {"transport": "stdio", "command": "npx", "tools": "*"}}
    allows = mcp.mcp_permission_allows(cfg)
    assert "mcp__metaql__a" in allows and "mcp__metaql__b" in allows
    assert "mcp__figma__*" in allows
    assert "mcp__metaql__*" not in allows   # a tool list must NOT widen to the whole server


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


def test_cli_settings_with_mcp_writes_merged_file(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'mcp:\n  metaql:\n    transport: http\n    url: "https://m/mcp"\n    tools: [metaql_execute_query]\n')
    env = {k: v for k, v in os.environ.items() if k != "ORBIT_HOME"}
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "mcp.py"), "settings", str(t)],
                       capture_output=True, text=True, env=env)
    path = r.stdout.strip()
    assert path.endswith("settings.json")
    data = json.loads(Path(path).read_text())
    assert "mcp__metaql__metaql_execute_query" in data["permissions"]["allow"]
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


def test_validate_accepts_good_mcp_block(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'mcp:\n  metaql:\n    transport: http\n    url: "https://m/mcp"\n'
                   '    token_env: METAQL_TOKEN\n    tools: [a]\n'
                   '  figma:\n    transport: stdio\n    command: npx\n    args: ["-y", "x"]\n'
                   '    env_passthrough: [FIGMA_TOKEN]\n    tools: "*"\n')
    r = _validate(t)
    assert r.returncode == 0, r.stdout
