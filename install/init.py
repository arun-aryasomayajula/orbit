#!/usr/bin/env python3
"""Scaffold a target repo's .autopilot/ profile with auto-detected gates.
Called by install.sh. Usage: init.py <target-repo> <ratchet-home>"""
import sys, os, shutil

TARGET = os.path.abspath(sys.argv[1])
RATCHET_HOME = os.path.abspath(sys.argv[2])
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
        gates = detect_gates()
        base = detect_base_branch()
        cfg = f"""# Ratchet profile for this repo. Full field reference: {RATCHET_HOME}/config/schema.yaml
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
        open(bl, "w").write("# Ratchet backlog — human-curated tasks.\n# Each: {id, title, category, priority, status: proposed|queued, autopilot: allow|human, acceptance_criteria: [...]}\ntasks: []\n")
        print("  ✓ wrote empty .autopilot/backlog.yaml")
    # state gitignore
    gi = os.path.join(AP, "state", ".gitignore")
    if not os.path.exists(gi):
        open(gi, "w").write("# Ratchet runtime state — do not commit\n*\n!.gitignore\n")
    # copy the cycle command into the target's .claude/commands so `claude -p /ratchet-cycle` resolves
    cmd_dir = os.path.join(TARGET, ".claude", "commands")
    os.makedirs(cmd_dir, exist_ok=True)
    shutil.copy(os.path.join(RATCHET_HOME, "skills", "ratchet-cycle.md"), os.path.join(cmd_dir, "ratchet-cycle.md"))
    # copy agents into the target's .claude/agents
    ag_dir = os.path.join(TARGET, ".claude", "agents")
    os.makedirs(ag_dir, exist_ok=True)
    for a in os.listdir(os.path.join(RATCHET_HOME, "agents")):
        if a.endswith(".md"):
            shutil.copy(os.path.join(RATCHET_HOME, "agents", a), os.path.join(ag_dir, a))
    print("  ✓ installed /ratchet-cycle command + agents into .claude/")
    print(f"\n  next: review {AP}/config.yaml (esp. gates), then `ratchet doctor` and `ratchet install`")

if __name__ == "__main__":
    main()
