# Orbit Product M1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship milestone M1 of the product-teams design: a config-driven MCP seam, an `orbit init --product` workspace template, and the `orbit-insight` workflow (grounded ad-hoc MetaQL analysis) with its deterministic lint gate.

**Architecture:** A new stdlib module `engine/mcp.py` turns a profile `mcp:` block into a per-target `--mcp-config` file and a merged permission-settings file; `run.sh` passes both to the cycle. The product workspace is a variant template in `install/init.py`. The insight workflow is pure declarative extension: a skill, an agent, a router category, and one lint script used as the workspace's gate.

**Tech Stack:** Python 3 stdlib + PyYAML only (no new dependencies in M1), Bash, pytest.

**Spec:** `docs/superpowers/specs/2026-08-20-orbit-product-teams-design.md` (sections 2, 3, 5, 13).

## Global Constraints

- Engine code is Python 3 stdlib + PyYAML only. No new pip dependencies in M1.
- MCP tokens never land on disk or in the repo. Generated configs reference env vars as `${VAR}` literals, which Claude Code expands at run time.
- Tests are hermetic in the existing style: pytest, no conftest, subprocess or importlib against temp dirs, no network, no `claude` calls.
- Run the suite from the orbit repo root: `python3 -m pytest tests/ -q` (or `uv run --with pytest --with pyyaml -- pytest tests/ -q` when no system pytest).
- Commit messages: conventional-commit style matching the repo history. Do NOT add a `Co-Authored-By` or `Generated with` trailer.
- The engine never names a specific project. Everything repo-specific stays in the target's `.autopilot/`.
- gstack is a design reference only; credit it in comments where a design is borrowed. Never invoke or vendor gstack code.

## File Structure

| File | Responsibility |
|---|---|
| `engine/mcp.py` (create) | Profile `mcp:` block to per-target mcp-config JSON + merged settings JSON. CLI: `build`, `settings`. |
| `engine/config.py` (modify) | Validate the `mcp:` block in `validate`. |
| `engine/run.sh` (modify) | Resolve MCP config + settings per iteration; pass `--mcp-config`/`--settings` to the cycle. |
| `config/schema.yaml` (modify) | Document the `mcp:` block and the product profile fields. |
| `engine/insight_lint.py` (create) | Tier-A deterministic lint over `insights/*.md`: structure + citation integrity. Used as the workspace gate. |
| `install/init.py` (modify) | `--product` flag: product workspace template (folders, config, CLAUDE.md, backlog seed). |
| `install.sh` + `bin/orbit` (modify) | Pass `--product` through `orbit init`. |
| `skills/orbit-insight.md` (create) | The insight methodology skill (grounding contract, MetaQL discovery sequence, output format). |
| `agents/analyst.md` (create) | The insight maker subagent. |
| `router/router.yaml` (modify) | `insight` category routing. |
| `engine/backlog_to_tasks.py` (modify) | Add `insight` to `SAFE_CATEGORIES`. |
| `tests/test_mcp.py`, `tests/test_insight_lint.py`, `tests/test_init_product.py` (create) | Coverage for the three new units. |

---

### Task 1: `engine/mcp.py` — MCP config generation

**Files:**
- Create: `engine/mcp.py`
- Test: `tests/test_mcp.py`

**Interfaces:**
- Produces: `build_mcp_config(mcp_cfg: dict) -> dict` (pure), `write_build(target: str) -> str` (returns written path or "" when no `mcp:` block). CLI: `python3 engine/mcp.py build <target>` prints the path or nothing.
- Consumes: the target's `.autopilot/config.yaml` raw `mcp:` mapping.

The profile shape this task implements (documented in Task 3):

```yaml
mcp:
  metaql:
    transport: http                  # http | stdio
    url: "https://metaql.example/mcp"
    token_env: METAQL_TOKEN          # optional; becomes an Authorization header with ${METAQL_TOKEN}
    tools: [metaql_list_use_cases, metaql_execute_query]   # allowlist, or "*"
  figma:
    transport: stdio
    command: npx
    args: ["-y", "figma-mcp"]
    env_passthrough: [FIGMA_TOKEN]   # exported into the server process as ${VAR} references
    tools: "*"
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp.py
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
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "mcp.py"), "build", str(t)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    path = r.stdout.strip()
    assert path.endswith("mcp-config.json")
    data = json.loads(Path(path).read_text())
    assert "metaql" in data["mcpServers"]


def test_cli_build_no_mcp_block_prints_nothing(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG)
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "mcp.py"), "build", str(t)],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout.strip() == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_mcp.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcp'`.

- [ ] **Step 3: Write the implementation**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_mcp.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/mcp.py tests/test_mcp.py
git commit -m "feat(mcp): generate per-target MCP config from the profile mcp: block"
```

---

### Task 2: `engine/mcp.py` — merged permission settings

**Files:**
- Modify: `engine/mcp.py` (already contains `build_settings`/`write_settings` from Task 1)
- Test: `tests/test_mcp.py` (extend)

**Interfaces:**
- Produces: CLI `python3 engine/mcp.py settings <target>` prints the settings path (base path when no `mcp:` block). `build_settings(mcp_cfg, base) -> dict` and `mcp_permission_allows(mcp_cfg) -> list` are the pure units.
- Consumes: `config/orbit.settings.json` as the base.

This task pins the behavior with tests (the implementation landed in Task 1's module; the settings path was untested).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp.py`:

```python
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
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "mcp.py"), "settings", str(t)],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout.strip() == str(ORBIT / "config" / "orbit.settings.json")


def test_cli_settings_with_mcp_writes_merged_file(tmp_path):
    t = _mk_target(tmp_path, BASE_CFG +
                   'mcp:\n  metaql:\n    transport: http\n    url: "https://m/mcp"\n    tools: [metaql_execute_query]\n')
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "mcp.py"), "settings", str(t)],
                       capture_output=True, text=True)
    path = r.stdout.strip()
    assert path.endswith("settings.json")
    data = json.loads(Path(path).read_text())
    assert "mcp__metaql__metaql_execute_query" in data["permissions"]["allow"]
    # the base file's own protections must survive the merge
    assert "Bash(git push:*)" in data["permissions"]["deny"]
```

- [ ] **Step 2: Run tests, fix until green**

Run: `python3 -m pytest tests/test_mcp.py -v`
Expected: all PASS (Task 1's implementation should already satisfy these; fix any mismatch in `mcp.py`, not in the tests).

- [ ] **Step 3: Commit**

```bash
git add engine/mcp.py tests/test_mcp.py
git commit -m "feat(mcp): merge mcp tool allowlist into per-target settings"
```

---

### Task 3: Validate the `mcp:` block + document it

**Files:**
- Modify: `engine/config.py` (extend `validate`, after the `qa` block around line 183)
- Modify: `config/schema.yaml` (append documentation)
- Test: `tests/test_mcp.py` (extend)

**Interfaces:**
- Consumes: the raw config mapping already loaded in `config.py:validate`.
- Produces: `python3 engine/config.py validate <target>` fails with a message for each malformed `mcp:` field.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp.py`:

```python
def _validate(target):
    return subprocess.run([sys.executable, str(ORBIT / "engine" / "config.py"),
                           "validate", str(target)], capture_output=True, text=True)


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_mcp.py -k validate -v`
Expected: the two reject-tests FAIL (validate currently exits 0, ignoring `mcp:`).

- [ ] **Step 3: Implement validation**

In `engine/config.py`, insert before the final `if errs:` in `validate` (after the `qa` block):

```python
    if "mcp" in raw and raw["mcp"] is not None:
        mc = raw["mcp"]
        if not isinstance(mc, dict):
            errs.append(f"mcp must be a mapping of server-name to config, got {mc!r}")
        else:
            for name, s in mc.items():
                pre = f"mcp.{name}"
                if not isinstance(s, dict):
                    errs.append(f"{pre} must be a mapping, got {s!r}"); continue
                tr = s.get("transport")
                if tr not in ("http", "stdio"):
                    errs.append(f"{pre}.transport must be \"http\" or \"stdio\", got {tr!r}")
                if tr == "http" and (not isinstance(s.get("url"), str) or not s.get("url")):
                    errs.append(f"{pre}.url is required for http transport")
                if tr == "stdio" and (not isinstance(s.get("command"), str) or not s.get("command")):
                    errs.append(f"{pre}.command is required for stdio transport")
                tools = s.get("tools", "*")
                if tools != "*" and (not isinstance(tools, list)
                                     or not all(isinstance(t, str) and t for t in tools)):
                    errs.append(f"{pre}.tools must be \"*\" or a list of non-empty strings, got {tools!r}")
                for fld in ("token_env",):
                    if fld in s and (not isinstance(s[fld], str) or not s[fld]):
                        errs.append(f"{pre}.{fld} must be a non-empty string, got {s.get(fld)!r}")
                for fld in ("args", "env_passthrough"):
                    if fld in s and (not isinstance(s[fld], list)
                                     or not all(isinstance(x, str) for x in s[fld])):
                        errs.append(f"{pre}.{fld} must be a list of strings, got {s.get(fld)!r}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_mcp.py -v`
Expected: all PASS.

- [ ] **Step 5: Document in `config/schema.yaml`**

Append a commented block (matching the file's existing annotated style):

```yaml
# mcp:                               # OPTIONAL — MCP servers the cycle may reach (spec section 3).
#   metaql:                          # server name → mcp__metaql__* tool prefix
#     transport: http                # http | stdio
#     url: "https://<host>/mcp"      # http only
#     token_env: METAQL_TOKEN        # optional; sent as `Authorization: Bearer ${METAQL_TOKEN}` —
#                                    # the ${VAR} is expanded by Claude Code at run time; the
#                                    # token itself never lands on disk or in the repo
#     tools: [metaql_execute_query]  # allowlist merged into the cycle's settings; "*" = all
#   figma:
#     transport: stdio
#     command: npx
#     args: ["-y", "figma-mcp"]
#     env_passthrough: [FIGMA_TOKEN] # env var names forwarded to the server process as ${VAR}
#     tools: "*"
```

- [ ] **Step 6: Full suite + commit**

Run: `python3 -m pytest tests/ -q` — expected: all green.

```bash
git add engine/config.py config/schema.yaml tests/test_mcp.py
git commit -m "feat(config): validate and document the mcp: profile block"
```

---

### Task 4: Wire MCP into the loop (`run.sh`)

**Files:**
- Modify: `engine/run.sh` (near line 31 `SETTINGS=...`, inside `one_iteration` around line 198, and `run_cycle` at lines 174-180)

**Interfaces:**
- Consumes: `engine/mcp.py build|settings <target>` from Tasks 1-2.
- Produces: the cycle command gains `--mcp-config <path> --strict-mcp-config` when the profile declares `mcp:`, and `--settings` points at the merged settings file.

No unit test exists for run.sh (the suite replaces the whole claude call via env seams); the check here is `bash -n` plus a foreground smoke in Task 9.

- [ ] **Step 1: Resolve per-iteration**

At the top of `one_iteration()` (after `gates_ready || ...`), add:

```bash
  # MCP seam (spec section 3): the engine decides which MCP servers and tools a
  # cycle can reach. Empty MCP_CONFIG = no mcp: block = flags omitted entirely.
  MCP_CONFIG="$(python3 "$ENGINE/mcp.py" build "$TARGET" 2>>"$LOGDIR/orbit.log" || true)"
  CYCLE_SETTINGS="$(python3 "$ENGINE/mcp.py" settings "$TARGET" 2>>"$LOGDIR/orbit.log" || echo "$SETTINGS")"
  [ -n "$CYCLE_SETTINGS" ] || CYCLE_SETTINGS="$SETTINGS"
```

- [ ] **Step 2: Pass the flags in `run_cycle`**

Replace the claude invocation lines 177-180 with:

```bash
  local mcp_args=()
  [ -n "${MCP_CONFIG:-}" ] && mcp_args=(--mcp-config "$MCP_CONFIG" --strict-mcp-config)
  $caff "$CLAUDE_BIN" --print "/orbit-cycle" \
      --permission-mode "$PERM" --settings "${CYCLE_SETTINGS:-$SETTINGS}" --model "$MODEL" \
      --add-dir "$REPO" --add-dir "$ORBIT_HOME" \
      "${mcp_args[@]}" \
      --output-format stream-json --verbose >"$out" 2>&1 </dev/null &
```

- [ ] **Step 3: Syntax check**

Run: `bash -n engine/run.sh`
Expected: no output, exit 0.

- [ ] **Step 4: Full suite (guard against collateral)**

Run: `python3 -m pytest tests/ -q` — expected: all green.

- [ ] **Step 5: Commit**

```bash
git add engine/run.sh
git commit -m "feat(loop): pass profile-declared MCP config and merged settings to the cycle"
```

---

### Task 5: `engine/insight_lint.py` — the insight gate

**Files:**
- Create: `engine/insight_lint.py`
- Test: `tests/test_insight_lint.py`

**Interfaces:**
- Produces: `lint_file(path: Path) -> list[str]` (error strings, empty = clean) and CLI `python3 engine/insight_lint.py <repo>` exiting 0 (clean or no `insights/` dir) / 1 (violations printed).
- Consumed by: the product workspace's `gates:` block (Task 6) and later PRD-flow evals (M2).

Lint rules (the mechanical half of the spec's grounding contract):

1. File starts with a `# ` title.
2. Contains `## Answer` and `## Basis` sections.
3. `## Basis` contains at least one source line matching `^- S\d+[ :—-]` (a typed source).
4. Every inline citation `(S<n>)` in the body resolves to an `S<n>` defined under Basis.
5. No `TBD` or `TODO` anywhere.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_insight_lint.py
import subprocess, sys
from pathlib import Path

ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import insight_lint  # noqa: E402

GOOD = """# Funnel drop at plan-select

## Answer
Conversion from view-plans to pay fell 4.2% week over week (S1).

## Basis
- S1: metaql_execute_query explore=premium_funnel, 2026-08-18, rows=14
"""

def _run_cli(repo):
    return subprocess.run([sys.executable, str(ORBIT / "engine" / "insight_lint.py"), str(repo)],
                          capture_output=True, text=True)

def test_good_insight_passes(tmp_path):
    d = tmp_path / "insights"; d.mkdir()
    (d / "001-funnel.md").write_text(GOOD)
    assert insight_lint.lint_file(d / "001-funnel.md") == []
    assert _run_cli(tmp_path).returncode == 0

def test_missing_basis_section_fails(tmp_path):
    d = tmp_path / "insights"; d.mkdir()
    (d / "x.md").write_text("# T\n\n## Answer\nUp 5% (S1).\n")
    errs = insight_lint.lint_file(d / "x.md")
    assert any("Basis" in e for e in errs)
    assert _run_cli(tmp_path).returncode == 1

def test_dangling_citation_fails(tmp_path):
    d = tmp_path / "insights"; d.mkdir()
    (d / "x.md").write_text(GOOD.replace("(S1)", "(S9)"))
    errs = insight_lint.lint_file(d / "x.md")
    assert any("S9" in e for e in errs)

def test_todo_marker_fails(tmp_path):
    d = tmp_path / "insights"; d.mkdir()
    (d / "x.md").write_text(GOOD + "\nTODO: check cohort\n")
    assert any("TODO" in e for e in insight_lint.lint_file(d / "x.md"))

def test_repo_without_insights_dir_is_clean(tmp_path):
    assert _run_cli(tmp_path).returncode == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_insight_lint.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'insight_lint'`.

- [ ] **Step 3: Write the implementation**

```python
#!/usr/bin/env python3
"""Tier-A deterministic lint for insight docs (spec sections 4 and 5): structure
plus citation integrity, the mechanical half of the grounding contract. Used as
the product workspace's gate, so a cycle cannot ship an ungrounded insight.
Exit 0 = clean (or no insights/ directory yet); exit 1 = violations printed.
Design reference: dwh-suite's prd_evals.py Tier-A checks."""
import re, sys
from pathlib import Path

CITE = re.compile(r"\(S(\d+)\)")
SOURCE = re.compile(r"^- S(\d+)[ :—-]", re.M)


def lint_file(path: Path) -> list:
    errs = []
    text = path.read_text()
    rel = path.name
    if not text.startswith("# "):
        errs.append(f"{rel}: must start with a '# ' title")
    if "## Answer" not in text:
        errs.append(f"{rel}: missing '## Answer' section")
    if "## Basis" not in text:
        errs.append(f"{rel}: missing '## Basis' section")
        return errs
    basis = text.split("## Basis", 1)[1]
    defined = set(SOURCE.findall(basis))
    if not defined:
        errs.append(f"{rel}: Basis has no typed source lines ('- S1: ...')")
    for n in set(CITE.findall(text)):
        if n not in defined:
            errs.append(f"{rel}: citation (S{n}) has no matching source in Basis")
    for marker in ("TBD", "TODO"):
        if marker in text:
            errs.append(f"{rel}: contains '{marker}' — abstain-over-guess means gaps go in Basis, not markers")
    return errs


def main(repo: str) -> int:
    d = Path(repo) / "insights"
    if not d.is_dir():
        return 0
    errs = []
    for f in sorted(d.glob("*.md")):
        errs += lint_file(f)
    for e in errs:
        print(f"  ✗ {e}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_insight_lint.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/insight_lint.py tests/test_insight_lint.py
git commit -m "feat(insight): deterministic grounding lint for insight docs"
```

---

### Task 6: Product workspace template (`orbit init --product`)

**Files:**
- Modify: `install/init.py` (arg parsing at lines 6-7, new template branch in `main`)
- Modify: `install.sh` (line 11-17: accept and forward `--product`)
- Modify: `bin/orbit` (line 34: forward extra args to install.sh)
- Test: `tests/test_init_product.py`

**Interfaces:**
- Produces: `orbit init <target> --product` scaffolds the workspace of spec section 2. `init.py` argv becomes `<target> <orbit-home> [--product]`.
- Consumes: `engine/insight_lint.py` (Task 5) as the gate command, `engine/config.py validate` must accept the generated config.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_init_product.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_init_product.py -v`
Expected: first three FAIL (unknown flag / missing dirs); the fourth PASSES (guards the existing path). Note: the commands/agents assertion also depends on Task 7's files; run this task and Task 7 as one commit series if executing out of order.

- [ ] **Step 3: Implement the flag and template**

In `install/init.py`, replace lines 6-7 with:

```python
ARGS = [a for a in sys.argv[1:] if a != "--product"]
PRODUCT = "--product" in sys.argv[1:]
TARGET = os.path.abspath(ARGS[0])
ORBIT_HOME = os.path.abspath(ARGS[1])
```

In `main()`, branch the template. Keep the existing code-repo template untouched; when `PRODUCT` is true write instead:

```python
        cfg = f"""# Orbit PRODUCT workspace profile. Field reference: {ORBIT_HOME}/config/schema.yaml
repo: "."
base_branch: "{base}"
model: "claude-opus-4-8"
permission_mode: "acceptEdits"
interval_seconds: 300
max_tasks_per_day: 8
cycle_timeout_seconds: 3600

spec: null

# The workspace gate: no cycle ships an insight that fails the grounding lint.
gates:
  insight_lint:
    cmd: 'python3 {ORBIT_HOME}/engine/insight_lint.py .'
    cwd: "."
    needs: []

workable_categories: [insight, documentation]
sources: [backlog]

# MCP servers this workspace's cycles may reach (see schema.yaml `mcp:` docs).
# Uncomment and fill in; the token stays in your shell env, never in this file.
# mcp:
#   metaql:
#     transport: http
#     url: "https://<your-metaql-mcp-host>/mcp"
#     token_env: METAQL_TOKEN
#     tools: [metaql_list_verticals, metaql_list_projects, metaql_list_use_cases,
#             metaql_list_explores, metaql_get_explore_fields, metaql_generate_query,
#             metaql_execute_query]

env:
  passthrough: [METAQL_TOKEN]

branch_prefix: "orbit-product"
commit_trailer: ""
"""
```

And after the config write, still under `PRODUCT`, scaffold the workspace and seed files:

```python
    if PRODUCT:
        for d in ("knowledge", "insights", "research", "surveys"):
            os.makedirs(os.path.join(TARGET, d), exist_ok=True)
        cm = os.path.join(TARGET, "CLAUDE.md")
        if not os.path.exists(cm):
            open(cm, "w").write(
                "# <Product name> workspace\n\n"
                "This is an Orbit product workspace (PM analysis, insights, research).\n"
                "State facts, not interpretations. Do not plant hypotheses here.\n\n"
                "## Data sources\n- MetaQL MCP (see .autopilot/config.yaml `mcp:`)\n\n"
                "## Layout\n- knowledge/: screenshots, schema notes, error mappings\n"
                "- insights/: grounded analysis outputs (gated by insight_lint)\n"
                "- research/: PRD runs (M2)\n- surveys/: questionnaires and responses (M3)\n")
```

Also seed the backlog with a commented example insight task (replace the generic backlog seed under `PRODUCT`):

```python
        open(bl, "w").write(
            "# Orbit backlog — product workspace.\n"
            "# Example insight task (uncomment, edit, set status: queued to run):\n"
            "# tasks:\n"
            "# - id: 1\n"
            "#   title: \"Where is the biggest week-over-week funnel drop?\"\n"
            "#   category: insight\n"
            "#   status: queued\n"
            "#   autopilot: allow\n"
            "#   acceptance_criteria:\n"
            "#     - \"insights/ doc passes insight_lint (Answer + Basis, every number cited)\"\n"
            "tasks: []\n")
```

In `install.sh`, change lines 10-17 to accept the flag and forward it:

```bash
TARGET="${1:-}"
FLAG="${2:-}"
[ -n "$TARGET" ] || { echo "usage: ./install.sh <target-repo> [--product]"; exit 2; }
...
python3 "$ORBIT_HOME/install/init.py" "$TARGET" "$ORBIT_HOME" $FLAG
```

In `bin/orbit`, change the init dispatch (line 34) to forward remaining args:

```bash
  init)    shift 2 2>/dev/null || true; "$ORBIT_HOME/install.sh" "$target" "$@" ;;
```

(Match the dispatcher's actual arg convention when editing; the intent is `orbit init <target> --product` reaching init.py.)

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_init_product.py -v`
Expected: the commands/agents test still FAILS (files arrive in Task 7); the other three PASS.

- [ ] **Step 5: Commit**

```bash
git add install/init.py install.sh bin/orbit tests/test_init_product.py
git commit -m "feat(init): orbit init --product scaffolds a PM workspace"
```

---

### Task 7: The insight workflow — skill, agent, router, queue category

**Files:**
- Create: `skills/orbit-insight.md`
- Create: `agents/analyst.md`
- Modify: `router/router.yaml` (add `insight` under `categories:` and `by_category`)
- Modify: `engine/backlog_to_tasks.py:55-57` (SAFE_CATEGORIES)
- Test: `tests/test_init_product.py` (the remaining failing assertion) + `tests/test_signal_adapters.py`-style queue test appended to `tests/test_init_product.py`

**Interfaces:**
- Consumes: router schema (`categories.<name>` with policy/maker/skill/tracks/discipline), agent frontmatter (`name/description/tools/model`), skill frontmatter (`description/argument-hint/allowed-tools/model`).
- Produces: category `insight` is auto-pickable (`policy: allow`, in `SAFE_CATEGORIES`), routed to the `analyst` maker briefed by the `orbit-insight` skill.

- [ ] **Step 1: Write the failing queue test**

Append to `tests/test_init_product.py`:

```python
def test_insight_is_a_safe_category():
    sys.path.insert(0, str(ORBIT / "engine"))
    import backlog_to_tasks
    assert "insight" in backlog_to_tasks.SAFE_CATEGORIES
```

Run: `python3 -m pytest tests/test_init_product.py -v` — expected: this test and the commands/agents test FAIL.

- [ ] **Step 2: Add the category to the queue emitter**

In `engine/backlog_to_tasks.py`, change `SAFE_CATEGORIES` to:

```python
SAFE_CATEGORIES = {
    "testing", "documentation", "dependencies", "code_quality", "developer_experience",
    "insight",   # product workspace analysis (spec section 5): read-only data pulls into a gated doc
}
```

- [ ] **Step 3: Write `agents/analyst.md`**

```markdown
---
name: analyst
description: Product data analyst — answers one scoped data question with grounded evidence, writing a single insight doc. Uses MetaQL MCP tools when the workspace declares them.
model: opus
---

You are the **Orbit analyst**. One insight task per invocation: answer the
question in your brief with real data, write ONE doc under `insights/`, and
report. You never edit code, never mutate remote systems, never guess a number.

Operating rules:
- **No fact without a tool call.** Every number in your doc comes from a query
  you ran this session, cited as a typed source (S1, S2, ...) in `## Basis`.
- **Abstain over guess.** A question the data cannot answer becomes a named gap
  in `## Basis`, never an estimate presented as fact.
- **MetaQL discovery order** (when metaql tools are available): list verticals →
  projects → use cases → explores → get_explore_fields → generate/execute query.
  Never hand-write SQL against guessed columns.
- **Scope queries**: aggregate in the query (GROUP BY, LIMIT); never pull raw
  rows to summarize by eye.
- Quantify impact (INR or affected-user count) whenever the data supports it.
- Output file: `insights/<task-id>-<slug>.md` with `# title`, `## Answer`
  (finding first, then evidence), `## Basis` (one `- S<n>: <tool> <params>
  <date>` line per source). The workspace gate lints this shape — a doc that
  fails `insight_lint` does not ship.

Report format (one line): `ANALYST: <insight file> — <one-sentence finding>`.
```

- [ ] **Step 4: Write `skills/orbit-insight.md`**

```markdown
---
description: Methodology for one grounded product-data insight — question to evidence-backed doc via MetaQL discovery, under the grounding contract (no fact without a source, abstain over guess). Invoked by the orchestrator to brief the analyst maker.
argument-hint: "(no args — the task contract arrives via the cycle brief)"
allowed-tools: Read, Grep, Glob
model: opus
---

You are briefing an insight task (category `insight`). Distill the following
method into the maker brief.

## Method
1. **Phase 0 — load context silently**: `CLAUDE.md`, `knowledge/` (data-source
   notes, query gotchas), the two most recent `insights/*.md`. Do not narrate.
2. **Restate the question** as a testable claim with a cohort and a time window.
   If the task lacks either, choose the most conservative reading and say so in
   the doc.
3. **Discover before querying**: verticals → projects → use cases → explores →
   fields. Only then generate and execute the query. Wrong-column guessing is
   the top failure mode; the knowledge folder's gotchas exist to prevent it.
4. **Triangulate**: one source is a clue, two are a finding. Where a second
   source exists (another explore, a knowledge-folder export), cross-check the
   headline number.
5. **Quantify impact** in INR or affected users when the data supports it.
6. **Write** `insights/<task-id>-<slug>.md`: `# title`, `## Answer`, `## Basis`
   with typed sources. Every inline number cites `(S<n>)`. Gaps are recorded in
   Basis, never papered over.
7. **Definition of done**: `python3 $ORBIT_HOME/engine/insight_lint.py .` exits
   0 on the worktree, and the doc answers the task's acceptance criteria.

Design references: the PREMIUM PM-agent methodology (funnel thinking,
triangulation, INR impact) and dwh-suite's grounding contract.
```

- [ ] **Step 5: Add the router category**

In `router/router.yaml`, add under `categories:` (after `developer_experience`):

```yaml
  insight:
    policy: allow
    maker: { agent: analyst, model: opus, effort: high }
    skill: orbit-insight            # methodology brief (skills/orbit-insight.md)
    tracks: []
    discipline: no fact without a tool call; abstain over guess; doc must pass insight_lint
```

And under `goldens.by_category`, add:

```yaml
    insight: always                # no dedicated card yet — verify-before-done applies
```

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/test_init_product.py tests/test_mcp.py -v`
Expected: all PASS (init copies every `skills/*.md` and `agents/*.md`, so the Task 6 assertion now passes too).

- [ ] **Step 7: Full suite + commit**

Run: `python3 -m pytest tests/ -q` — expected: all green.

```bash
git add skills/orbit-insight.md agents/analyst.md router/router.yaml engine/backlog_to_tasks.py tests/test_init_product.py
git commit -m "feat(insight): analyst agent, orbit-insight skill, and router category"
```

---

### Task 8: Doctor awareness of the product profile

**Files:**
- Modify: `engine/doctor.py` (add an mcp section check)
- Test: extend `tests/test_init_product.py`

**Interfaces:**
- Consumes: `engine/mcp.py._load_mcp` shape and the profile.
- Produces: `orbit doctor <target>` warns when an `mcp:` server's `token_env` names an env var that is unset in the current shell (the top setup failure a PM will hit).

- [ ] **Step 1: Write the failing test**

```python
def test_doctor_warns_on_missing_mcp_token_env(tmp_path, monkeypatch):
    _init_product(tmp_path)
    cfg = tmp_path / ".autopilot" / "config.yaml"
    cfg.write_text(cfg.read_text() + '\nmcp:\n  metaql:\n    transport: http\n'
                   '    url: "https://m/mcp"\n    token_env: METAQL_TOKEN\n    tools: "*"\n')
    monkeypatch.delenv("METAQL_TOKEN", raising=False)
    env = dict(**__import__("os").environ, ORBIT_HOME=str(ORBIT))
    env.pop("METAQL_TOKEN", None)
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)
    assert "METAQL_TOKEN" in r.stdout
```

Run: `python3 -m pytest tests/test_init_product.py::test_doctor_warns_on_missing_mcp_token_env -v` — expected: FAIL.

- [ ] **Step 2: Implement**

Read `engine/doctor.py` first and follow its existing section style. Add, in its checks over the loaded raw config:

```python
    mcp_cfg = raw.get("mcp") or {}
    for name, s in mcp_cfg.items():
        te = isinstance(s, dict) and s.get("token_env")
        if te and not os.environ.get(te):
            warn(f"mcp.{name}: token_env {te} is not set in this shell — "
                 f"cycles will reach {name} unauthenticated or fail")
```

(Use doctor.py's actual warn/print helper; if none exists, print in its established `  ✗ / ·` style and do not change the exit code for a warning.)

- [ ] **Step 3: Run tests, then the full suite**

Run: `python3 -m pytest tests/test_init_product.py -v && python3 -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add engine/doctor.py tests/test_init_product.py
git commit -m "feat(doctor): warn when an mcp token_env is unset"
```

---

### Task 9: End-to-end smoke and docs

**Files:**
- Modify: `README.md` (short product-workspace subsection pointing at the spec)
- No new code.

- [ ] **Step 1: Scaffold a real workspace**

```bash
mkdir -p /tmp/orbit-smoke/premium-workspace && cd /tmp/orbit-smoke/premium-workspace && git init -q
~/master/orbit/install.sh . --product
```

Expected: scaffold messages, doctor runs, config validates. Inspect `.autopilot/config.yaml`, `CLAUDE.md`, the four folders.

- [ ] **Step 2: Verify the gate and the MCP plumbing on the smoke repo**

```bash
python3 ~/master/orbit/engine/insight_lint.py .              # exit 0 (no insights yet)
printf '# T\n\n## Answer\nUp 5%% (S1).\n\n## Basis\n- S1: metaql_execute_query x\n' > insights/t.md
python3 ~/master/orbit/engine/insight_lint.py . && echo CLEAN  # CLEAN
sed -i '' 's/(S1)/(S9)/' insights/t.md
python3 ~/master/orbit/engine/insight_lint.py .; echo "exit=$?" # violation printed, exit=1
```

Then uncomment the `mcp:` block in `.autopilot/config.yaml`, and:

```bash
python3 ~/master/orbit/engine/mcp.py build .    # prints .autopilot/state/mcp/mcp-config.json
python3 ~/master/orbit/engine/mcp.py settings . # prints merged settings path
python3 ~/master/orbit/engine/config.py validate .
```

- [ ] **Step 3: README subsection**

Add a short "Product workspaces" subsection to `README.md` (follow the write-docs principle: point at the spec, don't mirror it):

```markdown
## Product workspaces

`orbit init <dir> --product` scaffolds a PM workspace: the same loop, gates,
and dashboard, but the work items are grounded data insights instead of code
(category `insight`, gated by `engine/insight_lint.py`), and cycles may reach
MCP servers the profile declares under `mcp:` (see `config/schema.yaml`).
Design: `docs/superpowers/specs/2026-08-20-orbit-product-teams-design.md`.
```

- [ ] **Step 4: Full suite one last time + commit**

Run: `python3 -m pytest tests/ -q` — expected: all green (723+ passing).

```bash
git add README.md
git commit -m "docs: product workspaces (M1) pointer"
```

---

## Self-review notes

- Spec coverage for M1: section 3 (Tasks 1-4, 8), section 2 workspace template (Task 6), section 5's `orbit-insight` slice (Tasks 5, 7), section 13's testing style (every task). Sections 4, 6, 7, 9, 10, 11, 12 are M2+ by design and intentionally absent here.
- Type consistency: `mcp.build_mcp_config(dict) -> dict`, `mcp.mcp_permission_allows(dict) -> list`, `mcp.write_build(str) -> str`, `insight_lint.lint_file(Path) -> list` are used with those exact names in every task that references them.
- Known judgment calls an executor must respect: `--strict-mcp-config` is passed so a PM's global `~/.claude.json` MCP servers never leak into an unattended cycle; `commit_trailer: ""` in the product template because PM workspaces publish docs, not code; `insight` joins `SAFE_CATEGORIES` because it is read-only analysis whose output is gated by lint plus checker plus verifier.
- Line numbers cited (run.sh 174-180/198, config.py 183, init.py 6-7, backlog_to_tasks.py 55-57, install.sh 10-17, bin/orbit 34) are anchors from 2026-08-20; re-locate by content if the files have moved.
