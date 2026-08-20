#!/usr/bin/env python3
"""Per-target MCP wiring from the profile's `mcp:` block (spec section 3).

The engine — not the agent — decides which MCP servers a cycle can reach and
which of their tools are allowed. Tokens never touch disk: generated configs
carry `${VAR}` references that Claude Code expands from the environment at
run time.

Subcommands (both print a path on stdout, or nothing when noted):
  build <target>     write $AP_STATE/mcp/mcp-config.json from config `mcp:`.
                     No mcp: block -> print nothing, exit 0.
  settings <target>  write $AP_STATE/mcp/settings.json = the engine's base
                     orbit.settings.json plus mcp__<server>__<tool> allows.
                     No mcp: block -> print the engine base settings path.
"""
import json, os, sys

ORBIT_HOME = os.environ.get("ORBIT_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_SETTINGS = os.path.join(ORBIT_HOME, "config", "orbit.settings.json")


def _load_mcp(target):
    import yaml
    cfg_path = os.path.join(target, ".autopilot", "config.yaml")
    if not os.path.exists(cfg_path):
        sys.exit(f"no config at {cfg_path}")
    raw = yaml.safe_load(open(cfg_path)) or {}
    return raw.get("mcp") or {}


def build_mcp_config(mcp_cfg: dict) -> dict:
    servers = {}
    for name, s in mcp_cfg.items():
        if s.get("transport") == "http":
            srv = {"type": "http", "url": s["url"]}
            if s.get("token_env"):
                srv["headers"] = {"Authorization": f"Bearer ${{{s['token_env']}}}"}
        else:  # stdio
            srv = {"command": s["command"], "args": list(s.get("args") or [])}
            if s.get("env_passthrough"):
                srv["env"] = {v: f"${{{v}}}" for v in s["env_passthrough"]}
        servers[name] = srv
    return {"mcpServers": servers}


def mcp_permission_allows(mcp_cfg: dict) -> list:
    allows = []
    for name, s in mcp_cfg.items():
        tools = s.get("tools", "*")
        if tools == "*":
            allows.append(f"mcp__{name}__*")
        else:
            allows += [f"mcp__{name}__{t}" for t in tools]
    return allows


def build_settings(mcp_cfg: dict, base: dict) -> dict:
    out = json.loads(json.dumps(base))          # deep copy, stdlib only
    perms = out.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    for entry in mcp_permission_allows(mcp_cfg):
        if entry not in allow:
            allow.append(entry)
    return out


def _out_dir(target):
    d = os.path.join(target, ".autopilot", "state", "mcp")
    os.makedirs(d, exist_ok=True)
    return d


def write_build(target: str) -> str:
    mcp_cfg = _load_mcp(target)
    if not mcp_cfg:
        return ""
    path = os.path.join(_out_dir(target), "mcp-config.json")
    with open(path, "w") as f:
        json.dump(build_mcp_config(mcp_cfg), f, indent=2)
    return path


def write_settings(target: str) -> str:
    mcp_cfg = _load_mcp(target)
    if not mcp_cfg:
        return BASE_SETTINGS
    base = json.load(open(BASE_SETTINGS))
    path = os.path.join(_out_dir(target), "settings.json")
    with open(path, "w") as f:
        json.dump(build_settings(mcp_cfg, base), f, indent=2)
    return path


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("build", "settings"):
        sys.exit(__doc__)
    fn = {"build": write_build, "settings": write_settings}[sys.argv[1]]
    p = fn(sys.argv[2])
    if p:
        print(p)
