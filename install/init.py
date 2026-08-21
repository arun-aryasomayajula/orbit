#!/usr/bin/env python3
"""Scaffold a target repo's .autopilot/ profile with auto-detected gates.
Called by install.sh. Usage: init.py <target-repo> <orbit-home>"""
import sys, os, shutil

ARGS = [a for a in sys.argv[1:] if a != "--product"]
PRODUCT = "--product" in sys.argv[1:]
if len(ARGS) != 2 or any(a.startswith("-") for a in ARGS):
    sys.exit("usage: init.py <target-repo> <orbit-home> [--product]")
TARGET = os.path.abspath(ARGS[0])
ORBIT_HOME = os.path.abspath(ARGS[1])
AP = os.path.join(TARGET, ".autopilot")

def detect_gates():
    """Best-effort stack detection → a starter gates{} block."""
    g = {}
    has = lambda *p: os.path.exists(os.path.join(TARGET, *p))
    # python
    if has("pytest.ini") or has("pyproject.toml") or has("setup.cfg") or has("tests"):
        cmd = "venv/bin/python -m pytest -q" if has("venv") else "python -m pytest -q"
        g["test"] = {"cmd": cmd, "cwd": ".", "needs": []}
    # node — front or root
    for sub in (".", "frontend", "web", "client"):
        pj = os.path.join(TARGET, sub, "package.json")
        if os.path.exists(pj):
            import json
            scripts = (json.load(open(pj)).get("scripts") or {})
            cwd = sub
            if "test" in scripts:
                g[f"{'frontend' if sub!='.' else 'js'}_test"] = {"cmd": "CI=true npm test -- --watchAll=false", "cwd": cwd, "needs": ["node"]}
            if "typecheck" in scripts:
                g[f"{'frontend' if sub!='.' else 'js'}_typecheck"] = {"cmd": "npm run typecheck", "cwd": cwd, "needs": ["node"]}
            elif "tsconfig.json" in os.listdir(os.path.join(TARGET, sub)):
                g[f"{'frontend' if sub!='.' else 'js'}_typecheck"] = {"cmd": "npx tsc --noEmit", "cwd": cwd, "needs": ["node"]}
            break
    # go / rust / make fallbacks
    if not g:
        if has("go.mod"): g["test"] = {"cmd": "go test ./...", "cwd": ".", "needs": []}
        elif has("Cargo.toml"): g["test"] = {"cmd": "cargo test", "cwd": ".", "needs": []}
        elif has("Makefile"): g["test"] = {"cmd": "make test", "cwd": ".", "needs": []}
    return g

def yaml_gates(g):
    if not g:
        return "  # NO gates auto-detected — you MUST add at least one (how the checker verifies your repo):\n  # test:\n  #   cmd: \"<your test command>\"\n  #   cwd: \".\"\n"
    out = []
    for name, spec in g.items():
        out.append(f"  {name}:")
        out.append(f"    cmd: {spec['cmd']!r}")
        out.append(f"    cwd: {spec['cwd']!r}")
        out.append(f"    needs: {spec.get('needs', [])}")
    return "\n".join(out) + "\n"

SECRET_GITIGNORE_ENTRIES = (".jira_token", ".slack_webhook", ".secrets.env")

def ensure_gitignore_entries(path, entries):
    """Write `entries` into the gitignore at `path`, appending only what is
    missing. Returns the entries it had to add (empty = already complete)."""
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write("# Orbit secrets - never commit\n" + "".join(f"{e}\n" for e in entries))
        return list(entries)
    body = open(path).read()
    have = {ln.strip() for ln in body.splitlines()}
    missing = [e for e in entries if e not in have]
    if missing:
        sep = "" if (not body or body.endswith("\n")) else "\n"
        with open(path, "a") as f:
            f.write(sep + "".join(f"{e}\n" for e in missing))
    return missing

def detect_base_branch():
    import subprocess
    try:
        r = subprocess.check_output(["git", "-C", TARGET, "symbolic-ref", "refs/remotes/origin/HEAD"], text=True).strip()
        return r.rsplit("/", 1)[-1]
    except Exception:
        return "main"

def main():
    if os.path.exists(os.path.join(AP, "config.yaml")):
        print(f"  · .autopilot/config.yaml already exists — leaving it untouched")
    else:
        os.makedirs(os.path.join(AP, "tracks"), exist_ok=True)
        os.makedirs(os.path.join(AP, "state"), exist_ok=True)
        base = detect_base_branch()
        if PRODUCT:
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
# Uncomment and fill in.
# token: export METAQL_TOKEN in the shell that runs `orbit run`. Service mode
# (launchd/systemd) does NOT inherit your shell — add the variable to the
# service environment (see `orbit install`) until Keychain delivery lands (M2).
# mcp:
#   metaql:
#     transport: http
#     url: "https://<your-metaql-mcp-host>/mcp"
#     token_env: METAQL_TOKEN
#     tools: [metaql_list_verticals, metaql_list_projects, metaql_list_use_cases,
#             metaql_list_explores, metaql_get_explore_fields, metaql_generate_query,
#             metaql_execute_query]

branch_prefix: "orbit-product"
commit_trailer: ""
"""
            open(os.path.join(AP, "config.yaml"), "w").write(cfg)
            print(f"  ✓ wrote .autopilot/config.yaml (base_branch={base}, product workspace, insight_lint gate)")
        else:
            gates = detect_gates()
            cfg = f"""# Orbit profile for this repo. Full field reference: {ORBIT_HOME}/config/schema.yaml
repo: "."
base_branch: "{base}"
model: "claude-opus-4-8"
permission_mode: "acceptEdits"
interval_seconds: 180
max_tasks_per_day: 12
cycle_timeout_seconds: 3600

spec: null                   # optional: path to a standing spec the loop rereads (e.g. docs/VISION.md)

# HOW the checker verifies THIS repo (auto-detected — REVIEW and fix these):
gates:
{yaml_gates(gates)}
workable_categories: [bug, feature, refactor, code_quality, testing, documentation, dependencies, developer_experience]
sources: [backlog]

# task-level planning: a planner subagent writes a justified per-task plan
# (approach, file-scope fence, standards) before the build loop; the verifier
# fails any off-plan diff. Remove this block to opt out.
plan:
  categories: [bug, feature, refactor, code_quality, testing]
  enforce: "verifier"

env:
  passthrough: []            # env var names to export into each cycle (values from your shell)

branch_prefix: "autopilot"
commit_trailer: "Co-Authored-By: Claude <noreply@anthropic.com>"
"""
            open(os.path.join(AP, "config.yaml"), "w").write(cfg)
            print(f"  ✓ wrote .autopilot/config.yaml (base_branch={base}, {len(gates)} gate(s) detected)")

    # seed an empty backlog + state scaffolding
    bl = os.path.join(AP, "backlog.yaml")
    if not os.path.exists(bl):
        if PRODUCT:
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
        else:
            open(bl, "w").write("# Orbit backlog — human-curated tasks.\n# Each: {id, title, category, priority, status: proposed|queued, autopilot: allow|human, acceptance_criteria: [...]}\ntasks: []\n")
        print("  ✓ wrote empty .autopilot/backlog.yaml")
    # state gitignore
    gi = os.path.join(AP, "state", ".gitignore")
    if not os.path.exists(gi):
        open(gi, "w").write("# Orbit runtime state — do not commit\n*\n!.gitignore\n")
    # profile-level gitignore: secrets that may live beside config.yaml (the
    # documented fallback locations for notify's webhook and the jira token,
    # plus .secrets.env). Idempotent on purpose: a workspace scaffolded before
    # .secrets.env existed already has this file, and skipping it would leave
    # that workspace one `git add -A` away from committing its tokens. Re-running
    # init.py must top up whatever is missing instead of leaving it alone.
    ensure_gitignore_entries(os.path.join(AP, ".gitignore"), SECRET_GITIGNORE_ENTRIES)
    # secrets example (product workspaces only) — template for manual secret config
    if PRODUCT:
        ex = os.path.join(AP, ".secrets.env.example")
        if not os.path.exists(ex):
            open(ex, "w").write(
                "# EXAMPLE ONLY - this file is never read. Copy it to\n"
                "# .autopilot/.secrets.env (0600, gitignored) and put the real values\n"
                "# there, or let the dashboard's Settings > Secrets panel write that\n"
                "# file for you. Editing THIS file has no effect.\n"
                "#\n"
                "# One KEY=VALUE per line, no quotes, no `export`. run.sh parses this\n"
                "# file (never executes it), so a value with shell metacharacters is an\n"
                "# inert literal string. Interactive runs and launchd services see the\n"
                "# same environment either way, and each cycle rereads the file.\n"
                "#\n"
                "# METAQL_TOKEN=<your-metaql-token-here>\n")
        # .gitkeep in each: git tracks files, not directories, so a fresh clone
        # of a committed workspace arrives with no research/ at all - and the
        # dashboard hides the whole PRD band (and its New PRD button) when
        # research/ is missing, by the absent-unless-configured rule.
        for d in ("knowledge", "insights", "research", "surveys"):
            os.makedirs(os.path.join(TARGET, d), exist_ok=True)
            keep = os.path.join(TARGET, d, ".gitkeep")
            if not os.path.exists(keep):
                open(keep, "w").write("")
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
        print("  ✓ scaffolded product workspace (knowledge/ insights/ research/ surveys/ CLAUDE.md)")
    # copy the engine's commands into the target's .claude/commands so the headless
    # calls resolve (`claude -p /orbit-cycle`, `/orbit-intake`, ...)
    cmd_dir = os.path.join(TARGET, ".claude", "commands")
    os.makedirs(cmd_dir, exist_ok=True)
    for s in os.listdir(os.path.join(ORBIT_HOME, "skills")):
        if s.endswith(".md"):
            shutil.copy(os.path.join(ORBIT_HOME, "skills", s), os.path.join(cmd_dir, s))
    # copy agents into the target's .claude/agents
    ag_dir = os.path.join(TARGET, ".claude", "agents")
    os.makedirs(ag_dir, exist_ok=True)
    for a in os.listdir(os.path.join(ORBIT_HOME, "agents")):
        if a.endswith(".md"):
            shutil.copy(os.path.join(ORBIT_HOME, "agents", a), os.path.join(ag_dir, a))
    print("  ✓ installed /orbit-cycle command + agents into .claude/")
    print(f"\n  next: review {AP}/config.yaml (esp. gates), then `orbit doctor` and `orbit install`")
    print(f"  once gates are confirmed: `orbit knowledge {TARGET} build` surveys the repo into .autopilot/knowledge/ (optional, briefed into every cycle)")

if __name__ == "__main__":
    main()
