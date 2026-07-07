#!/usr/bin/env python3
"""Read a target repo's .autopilot/config.yaml, apply defaults, and answer queries.
Keeps run.sh clean: `eval "$(config.py shellenv <target>)"` exports the loop's vars.

Subcommands:
  shellenv <target>   emit `export RATCHET_*=...` lines for run.sh to eval
  gates <target>      print each gate as `name<TAB>cwd<TAB>cmd` (one per line)
  needs <target>      print the union of all gates' `needs` (one per line)
  validate <target>   exit 0 if config is well-formed, else print errors + exit 1
"""
import sys, os, shlex

DEFAULTS = {
    "repo": ".", "base_branch": "main", "model": "claude-opus-4-8",
    "permission_mode": "acceptEdits", "interval_seconds": 180,
    "max_tasks_per_day": 12, "cycle_timeout_seconds": 3600, "spec": None,
    "sources": ["backlog"], "branch_prefix": "autopilot",
    "commit_trailer": "Co-Authored-By: Claude <noreply@anthropic.com>",
}

def load(target):
    import yaml
    cfg_path = os.path.join(target, ".autopilot", "config.yaml")
    if not os.path.exists(cfg_path):
        sys.exit(f"no config at {cfg_path} — run `ratchet init` in the target repo first")
    cfg = yaml.safe_load(open(cfg_path)) or {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in cfg.items() if v is not None})
    # resolve repo to absolute (relative to the target dir)
    repo = out["repo"]
    out["repo"] = os.path.abspath(os.path.join(target, repo)) if not os.path.isabs(repo) else repo
    return out, cfg

def sh(v):
    return shlex.quote(str(v))

def shellenv(target):
    cfg, raw = load(target)
    ap_home = os.path.join(os.path.abspath(target), ".autopilot")
    lines = [
        f"export RATCHET_REPO={sh(cfg['repo'])}",
        f"export RATCHET_BASE_BRANCH={sh(cfg['base_branch'])}",
        f"export RATCHET_MODEL={sh(cfg['model'])}",
        f"export RATCHET_PERM={sh(cfg['permission_mode'])}",
        f"export RATCHET_INTERVAL={sh(cfg['interval_seconds'])}",
        f"export RATCHET_MAX_TASKS={sh(cfg['max_tasks_per_day'])}",
        f"export RATCHET_CYCLE_TIMEOUT={sh(cfg['cycle_timeout_seconds'])}",
        f"export RATCHET_BRANCH_PREFIX={sh(cfg['branch_prefix'])}",
        f"export AP_HOME={sh(ap_home)}",
        f"export AP_STATE={sh(os.path.join(ap_home, 'state'))}",
    ]
    env = raw.get("env", {}) or {}
    cb = env.get("CLAUDE_BIN", "auto")
    lines.append(f"export RATCHET_CLAUDE_BIN={sh(cb)}")
    if env.get("extra_ca_certs"):
        lines.append(f"export NODE_EXTRA_CA_CERTS={sh(env['extra_ca_certs'])}")
    print("\n".join(lines))

def gates(target):
    cfg, _ = load(target)
    for name, g in (cfg.get("gates") or {}).items():
        print(f"{name}\t{g.get('cwd', '.')}\t{g.get('cmd', '')}")

def needs(target):
    cfg, _ = load(target)
    seen = []
    for g in (cfg.get("gates") or {}).values():
        for n in (g.get("needs") or []):
            if n not in seen:
                seen.append(n)
    print("\n".join(seen))

def validate(target):
    cfg, raw = load(target)
    errs = []
    if not cfg.get("gates"):
        errs.append("no `gates:` defined — the checker has nothing to run (this is how it verifies YOUR repo)")
    if not os.path.isdir(cfg["repo"]):
        errs.append(f"repo path does not exist: {cfg['repo']}")
    if cfg.get("spec") and not os.path.exists(os.path.join(cfg["repo"], cfg["spec"])):
        errs.append(f"spec file not found: {cfg['spec']}")
    for name, g in (cfg.get("gates") or {}).items():
        if not g.get("cmd"):
            errs.append(f"gate '{name}' has no cmd")
    if errs:
        print("\n".join("  ✗ " + e for e in errs)); sys.exit(1)
    print("  ✓ config valid")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd, target = sys.argv[1], sys.argv[2]
    {"shellenv": shellenv, "gates": gates, "needs": needs, "validate": validate}[cmd](target)
