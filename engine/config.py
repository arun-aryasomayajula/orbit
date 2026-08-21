#!/usr/bin/env python3
"""Read a target repo's .autopilot/config.yaml, apply defaults, and answer queries.
Keeps run.sh clean: `eval "$(config.py shellenv <target>)"` exports the loop's vars.

Subcommands:
  shellenv <target>   emit `export ORBIT_*=...` lines for run.sh to eval
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
    "pull_requests": "off",
    "dashboard_url": "http://127.0.0.1:8787",
}

def load(target):
    import yaml
    cfg_path = os.path.join(target, ".autopilot", "config.yaml")
    if not os.path.exists(cfg_path):
        sys.exit(f"no config at {cfg_path} — run `orbit init` in the target repo first")
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
        f"export ORBIT_REPO={sh(cfg['repo'])}",
        f"export ORBIT_BASE_BRANCH={sh(cfg['base_branch'])}",
        f"export ORBIT_MODEL={sh(cfg['model'])}",
        f"export ORBIT_PERM={sh(cfg['permission_mode'])}",
        f"export ORBIT_INTERVAL={sh(cfg['interval_seconds'])}",
        f"export ORBIT_MAX_TASKS={sh(cfg['max_tasks_per_day'])}",
        f"export ORBIT_CYCLE_TIMEOUT={sh(cfg['cycle_timeout_seconds'])}",
        f"export ORBIT_BRANCH_PREFIX={sh(cfg['branch_prefix'])}",
        f"export ORBIT_PULL_REQUESTS={sh(cfg['pull_requests'])}",
        f"export ORBIT_DASHBOARD_URL={sh(cfg['dashboard_url'])}",
        f"export AP_HOME={sh(ap_home)}",
        f"export AP_STATE={sh(os.path.join(ap_home, 'state'))}",
    ]
    env = raw.get("env", {}) or {}
    cb = env.get("CLAUDE_BIN", "auto")
    lines.append(f"export ORBIT_CLAUDE_BIN={sh(cb)}")
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
    if cfg.get("pull_requests") not in ("off", "github"):
        errs.append(f"pull_requests must be \"off\" or \"github\", got {cfg.get('pull_requests')!r}")
    if "knowledge" in raw and raw["knowledge"] is not None:
        kb = raw["knowledge"]
        if not isinstance(kb, dict):
            errs.append(f"knowledge must be a mapping, got {kb!r}")
        else:
            if "refresh" in kb and kb["refresh"] not in ("manual", "auto"):
                errs.append(f"knowledge.refresh must be \"manual\" or \"auto\", got {kb.get('refresh')!r}")
            for field in ("stale_after_merges", "timeout_seconds"):
                if field in kb and (not isinstance(kb[field], int) or isinstance(kb[field], bool) or kb[field] <= 0):
                    errs.append(f"knowledge.{field} must be a positive integer, got {kb.get(field)!r}")
    if "plan" in raw and raw["plan"] is not None:
        pl = raw["plan"]
        if not isinstance(pl, dict):
            errs.append(f"plan must be a mapping, got {pl!r}")
        else:
            if "enforce" in pl and pl["enforce"] not in ("verifier", "block"):
                errs.append(f"plan.enforce must be \"verifier\" or \"block\", got {pl.get('enforce')!r}")
            if "categories" in pl:
                cats = pl["categories"]
                if not isinstance(cats, list) or not all(isinstance(c, str) and c for c in cats):
                    errs.append(f"plan.categories must be a list of non-empty strings, got {cats!r}")
            if "model" in pl and (not isinstance(pl["model"], str) or not pl["model"]):
                errs.append(f"plan.model must be a non-empty string, got {pl.get('model')!r}")
    if "qa" in raw and raw["qa"] is not None:
        qa = raw["qa"]
        if not isinstance(qa, dict):
            errs.append(f"qa must be a mapping, got {qa!r}")
        else:
            if "services" in qa:
                svcs = qa["services"]
                if not isinstance(svcs, list):
                    errs.append(f"qa.services must be a list, got {svcs!r}")
                else:
                    for i, svc in enumerate(svcs):
                        if not isinstance(svc, dict):
                            errs.append(f"qa.services[{i}] must be a mapping, got {svc!r}")
                            continue
                        if not isinstance(svc.get("launch"), str) or not svc.get("launch"):
                            errs.append(f"qa.services[{i}].launch must be a non-empty string, got {svc.get('launch')!r}")
                        for field in ("name", "cwd", "ready"):
                            if field in svc and not isinstance(svc[field], str):
                                errs.append(f"qa.services[{i}].{field} must be a string, got {svc[field]!r}")
            for field in ("base_url", "model", "browser"):
                if field in qa and (not isinstance(qa[field], str) or not qa[field]):
                    errs.append(f"qa.{field} must be a non-empty string, got {qa.get(field)!r}")
            if "flows_dir" in qa:
                fd = qa["flows_dir"]
                if (not isinstance(fd, str) or not fd.strip() or fd.startswith("/")
                        or ".." in fd.split("/")):
                    errs.append(
                        f"qa.flows_dir must be a non-empty relative path with no leading "
                        f"'/' and no '..' segments, got {fd!r}")
            if "mcp_config" in qa and qa["mcp_config"] is not None and not isinstance(qa["mcp_config"], str):
                errs.append(f"qa.mcp_config must be a string or null, got {qa.get('mcp_config')!r}")
            for field in ("ready_timeout_seconds", "timeout_seconds"):
                if field in qa and (not isinstance(qa[field], int) or isinstance(qa[field], bool) or qa[field] <= 0):
                    errs.append(f"qa.{field} must be a positive integer, got {qa.get(field)!r}")
            if "headless" in qa and not isinstance(qa["headless"], bool):
                errs.append(f"qa.headless must be a boolean, got {qa.get('headless')!r}")
            if "max_bounces" in qa and (not isinstance(qa["max_bounces"], int) or isinstance(qa["max_bounces"], bool) or qa["max_bounces"] < 0):
                errs.append(f"qa.max_bounces must be a non-negative integer, got {qa.get('max_bounces')!r}")
            if "gate_categories" in qa:
                gc = qa["gate_categories"]
                if not isinstance(gc, dict):
                    errs.append(f"qa.gate_categories must be a mapping, got {gc!r}")
                else:
                    for k, v in gc.items():
                        if not isinstance(k, str) or not k or v not in ("required", "evidence"):
                            errs.append(f"qa.gate_categories entries must map non-empty strings to \"required\" or \"evidence\", got {k!r}: {v!r}")
            if "env" in qa:
                envv = qa["env"]
                if not isinstance(envv, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in envv.items()):
                    errs.append(f"qa.env must be a mapping of string to string, got {envv!r}")
            if "regression" in qa:
                reg = qa["regression"]
                if not isinstance(reg, dict):
                    errs.append(f"qa.regression must be a mapping, got {reg!r}")
                else:
                    if "on_merge" in reg and not isinstance(reg["on_merge"], bool):
                        errs.append(f"qa.regression.on_merge must be a boolean, got {reg.get('on_merge')!r}")
                    if "schedule_hours" in reg and (not isinstance(reg["schedule_hours"], int) or isinstance(reg["schedule_hours"], bool) or reg["schedule_hours"] < 0):
                        errs.append(f"qa.regression.schedule_hours must be a non-negative integer, got {reg.get('schedule_hours')!r}")
                    if "max_flows_per_run" in reg and (not isinstance(reg["max_flows_per_run"], int) or isinstance(reg["max_flows_per_run"], bool) or reg["max_flows_per_run"] <= 0):
                        errs.append(f"qa.regression.max_flows_per_run must be a positive integer, got {reg.get('max_flows_per_run')!r}")
            if "exploratory" in qa:
                exp = qa["exploratory"]
                if not isinstance(exp, dict):
                    errs.append(f"qa.exploratory must be a mapping, got {exp!r}")
                else:
                    if "enabled" in exp and not isinstance(exp["enabled"], bool):
                        errs.append(f"qa.exploratory.enabled must be a boolean, got {exp.get('enabled')!r}")
                    if "max_pages" in exp and (not isinstance(exp["max_pages"], int) or isinstance(exp["max_pages"], bool) or exp["max_pages"] <= 0):
                        errs.append(f"qa.exploratory.max_pages must be a positive integer, got {exp.get('max_pages')!r}")
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
                if "tools" not in s:
                    errs.append(f'{pre}.tools is required — use "*" to allow every tool on this server')
                else:
                    tools = s.get("tools")
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
    if errs:
        print("\n".join("  ✗ " + e for e in errs)); sys.exit(1)
    print("  ✓ config valid")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd, target = sys.argv[1], sys.argv[2]
    {"shellenv": shellenv, "gates": gates, "needs": needs, "validate": validate}[cmd](target)
