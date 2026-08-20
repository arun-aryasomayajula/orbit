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
