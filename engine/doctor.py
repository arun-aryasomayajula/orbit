#!/usr/bin/env python3
"""orbit doctor — validate a target repo's autopilot wiring WITHOUT running a cycle.
Read-only. Checks: config valid · claude bin · router resolves every category ·
referenced tracks exist · referenced skills resolve · dry-run routing per queued task.

Usage: doctor.py <target-repo>  [--all]
Env:   ORBIT_HOME (engine dir; defaults to this file's repo root)
"""
import sys, os, json, shutil, subprocess

TARGET = sys.argv[1] if len(sys.argv) > 1 else "."
TARGET = os.path.abspath(TARGET)
SHOW_ALL = "--all" in sys.argv
ORBIT_HOME = os.environ.get("ORBIT_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AP = os.path.join(TARGET, ".autopilot")
import yaml

def find(*rel):
    """Target override wins over engine default."""
    for base in (AP, ORBIT_HOME):
        p = os.path.join(base, *rel)
        if os.path.exists(p):
            return p
    return None

ok = True
def check(label, passed, detail=""):
    global ok
    ok = ok and passed
    print(f"  {'✓' if passed else '✗'} {label}" + (f"  {detail}" if detail else ""))

print(f"\nORBIT DOCTOR — {TARGET}\n")

# 1. config
cfg_path = os.path.join(AP, "config.yaml")
check("config.yaml present", os.path.exists(cfg_path), cfg_path)
if not os.path.exists(cfg_path):
    sys.exit("\n  run `orbit init` in the target first.")
try:
    subprocess.run(["python3", os.path.join(ORBIT_HOME, "engine", "config.py"), "validate", TARGET], check=True)
    cfg = yaml.safe_load(open(cfg_path)) or {}
except subprocess.CalledProcessError:
    ok = False; cfg = yaml.safe_load(open(cfg_path)) or {}

# 2. claude bin
env = cfg.get("env", {}) or {}
cb = env.get("CLAUDE_BIN", "auto")
claude = shutil.which("claude") if cb == "auto" else cb
check("claude binary resolves", bool(claude and (shutil.which(claude) or os.path.exists(claude))), claude or "NOT FOUND")

# 2b. pull requests (wrapper-only, opt-in): gh must resolve AND be authenticated
if cfg.get("pull_requests") == "github":
    gh = shutil.which("gh")
    if not gh:
        check("pull_requests=github: gh on PATH", False, "gh NOT FOUND — install GitHub CLI or set pull_requests: off")
    else:
        authed = subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0
        check("pull_requests=github: gh authenticated", authed, "" if authed else "`gh auth status` failed — run `gh auth login`")

# 2c. jira source (optional): config block ↔ sources wiring + token present
jcfg = cfg.get("jira")
sources = cfg.get("sources") or []
if jcfg:
    check("jira: block has base_url + project", bool(jcfg.get("base_url") and jcfg.get("project")),
          "" if jcfg.get("base_url") and jcfg.get("project") else "both are required (see config/schema.yaml)")
    check("jira: listed in sources", "jira" in sources,
          "" if "jira" in sources else "add `jira` to sources: or the adapter never runs")
    tok = next((p for p in (os.path.join(AP, "state", ".jira_token"), os.path.join(AP, ".jira_token"))
                if os.path.exists(p)), None)
    check("jira: token file present", bool(tok),
          tok or f"put credentials in {os.path.join(AP, 'state', '.jira_token')} (basic: email:api_token · bearer: PAT)")
elif "jira" in sources:
    check("jira in sources has a jira: config block", False, "add a jira: block (see config/schema.yaml)")

# 3. router — target's .autopilot/router.yaml overrides the engine default at router/router.yaml
router_path = find("router.yaml") or (
    os.path.join(ORBIT_HOME, "router", "router.yaml")
    if os.path.exists(os.path.join(ORBIT_HOME, "router", "router.yaml")) else None)
check("router.yaml resolves", bool(router_path), router_path or "missing")
router = yaml.safe_load(open(router_path)) if router_path else {"categories": {}, "path_tracks": {}, "skills": {}}
cats = router.get("categories", {})

# 4. tracks referenced exist (target override or engine template)
missing_tracks = []
for spec in cats.values():
    if isinstance(spec, dict):
        for t in (spec.get("tracks") or []):
            if not find("tracks", f"{t}.md"):
                missing_tracks.append(t)
for t in (router.get("path_tracks") or {}):
    if not find("tracks", f"{t}.md"):
        missing_tracks.append(t)
check("every referenced track file exists", not missing_tracks, "" if not missing_tracks else "MISSING: " + ", ".join(sorted(set(missing_tracks))))

# 5. skills resolve (on disk in ~/.claude/skills, in superpowers plugin cache, or built-in)
disk = set(os.listdir(os.path.expanduser("~/.claude/skills"))) if os.path.isdir(os.path.expanduser("~/.claude/skills")) else set()
sp = os.path.expanduser("~/.claude/plugins/cache/claude-plugins-official/superpowers")
sp_skills = set()
if os.path.isdir(sp):
    for v in os.listdir(sp):
        d = os.path.join(sp, v, "skills")
        if os.path.isdir(d): sp_skills |= set(os.listdir(d))
BUILTIN = {"simplify", "security-review", "code-review", "verify", "write-docs"}
def skill_ok(s):
    if not s: return True
    if s.startswith("superpowers:"): return s.split(":",1)[1] in sp_skills
    return s in disk or s in BUILTIN
used = {v.get("skill") for v in cats.values() if isinstance(v, dict) and v.get("skill")}
bad = sorted(s for s in used if not skill_ok(s))
check("every referenced skill resolves", not bad, "" if not bad else "UNRESOLVED: " + ", ".join(bad))

# 5b. golden calibration resolves (graded exemplars — a third reference type)
g = router.get("goldens") or {}
gfile = g.get("file", "goldens/CALIBRATION.md")
gpath = find(gfile) or (os.path.join(ORBIT_HOME, gfile) if os.path.exists(os.path.join(ORBIT_HOME, gfile)) else None)
check("golden calibration resolves", bool(gpath), gpath or f"missing ({gfile})")

# 5c. project knowledge base (optional — presence of knowledge/ is the switch).
# Read-only: doctor never runs the survey. knowledge.py already does the real
# (containment-checked) validation on build/refresh — this just re-confirms
# the manifest still parses and its docs still exist, in doctor's own style.
kdir = os.path.join(AP, "knowledge")
kb_cfg = cfg.get("knowledge") if isinstance(cfg.get("knowledge"), dict) else {}
if not os.path.isdir(kdir):
    if kb_cfg.get("refresh") == "auto":
        check("knowledge.refresh: auto but no knowledge base — run `orbit knowledge <target> build`", False)
    # else: feature unused — print nothing (zero behavior change)
else:
    manifest_path = os.path.join(kdir, "manifest.json")
    problem = None
    docs = []
    if not os.path.exists(manifest_path):
        problem = f"manifest.json missing under {kdir}/"
    else:
        try:
            manifest = json.load(open(manifest_path))
        except (OSError, ValueError) as e:
            manifest = None
            problem = f"manifest.json is not valid JSON ({e})"
        if manifest is not None:
            if not isinstance(manifest, dict):
                problem = "manifest.json must be a JSON object"
            else:
                docs = manifest.get("docs")
                if not isinstance(docs, list) or not docs:
                    problem = "manifest.json 'docs' must be a non-empty list"
                    docs = []
                else:
                    missing = [d for d in docs if not isinstance(d, str) or not os.path.exists(os.path.join(kdir, d))]
                    if missing:
                        problem = "docs missing on disk: " + ", ".join(missing)
    check("knowledge base", problem is None, problem or f"{len(docs)} doc(s)")

    staleness_path = os.path.join(AP, "state", "knowledge-staleness.json")
    if os.path.exists(staleness_path):
        try:
            staleness = json.load(open(staleness_path))
            stale = staleness.get("stale_docs") if isinstance(staleness, dict) else None
        except (OSError, ValueError):
            stale = None
        if isinstance(stale, list) and stale:
            print(f"  · knowledge: {len(stale)} doc(s) stale — orbit knowledge <target> refresh")

# 5d. task-level planning (optional — presence of a `plan:` config block is the
# switch). Read-only surfacing: engine/config.py's `validate` subcommand already
# checks enforce/categories/model *shapes* (run in section 1 above); this section
# additionally (a) confirms the planner agent file actually resolves somewhere
# Claude Code will load it from, and (b) checks every plan.categories entry names
# a category the router actually knows about — a membership check config.py has
# no way to make (it never reads router.yaml). Category source: `cats` (the
# router.yaml `categories:` keys loaded in section 3 above) — the same source
# section 6 below already uses to validate queue task categories against, so
# plan.categories is held to the identical bar doctor already applies elsewhere.
plan_cfg = cfg.get("plan") if isinstance(cfg.get("plan"), dict) else None
if plan_cfg:
    engine_planner = os.path.join(ORBIT_HOME, "agents", "planner.md")
    target_agents_dir = os.path.join(TARGET, ".claude", "agents")
    target_planner = os.path.join(target_agents_dir, "planner.md")
    if os.path.exists(target_planner):
        check("plan: agents/planner.md resolves", True, f"target: {target_planner}")
    elif os.path.exists(engine_planner):
        detail = f"engine: {engine_planner}"
        if os.path.isdir(target_agents_dir):
            detail += " (not yet synced into target .claude/agents/ — run `orbit sync`)"
        check("plan: agents/planner.md resolves", True, detail)
    else:
        check("plan: agents/planner.md resolves", False,
              f"missing — checked target ({target_planner}) and engine ({engine_planner})")

    enforce = plan_cfg.get("enforce", "verifier")
    check('plan.enforce is "verifier" or "block"', enforce in ("verifier", "block"),
          "" if enforce in ("verifier", "block")
          else f"got {enforce!r} (config validate also flags this)")

    plan_cats = plan_cfg.get("categories")
    if isinstance(plan_cats, list) and all(isinstance(c, str) for c in plan_cats):
        unknown = sorted({c for c in plan_cats if c not in cats})
        check("plan.categories are known router categories", not unknown,
              "" if not unknown else "UNKNOWN: " + ", ".join(unknown))

# 6. dry-run routing over the queue (if present)
qf = os.path.join(AP, "state", "queue.json")
if os.path.exists(qf):
    q = json.load(open(qf)); tasks = q.get("queue", q.get("tasks", q)) if isinstance(q, dict) else q
    q_cats = {str(t.get("category","")) for t in tasks if t.get("category")}
    uncovered = [c for c in q_cats if c not in cats]
    check(f"router covers all {len(q_cats)} queue categories", not uncovered, "" if not uncovered else "UNCOVERED: " + str(uncovered))
    if SHOW_ALL:
        print("\n  DRY-RUN (one per category):")
        seen = set()
        for t in tasks:
            c = str(t.get("category",""))
            if c in seen or c not in cats: continue
            seen.add(c); m = cats[c].get("maker", {})
            print(f"    {c:22} → {m.get('agent','—'):9} @ {m.get('model','—'):7} · skill={cats[c].get('skill') or '—'} · tracks={','.join(cats[c].get('tracks') or []) or '—'}")
else:
    print("  · (no queue yet — run the loop once or `orbit backlog` to populate)")

print("\n" + ("  ALL CHECKS PASSED ✓" if ok else "  SOME CHECKS FAILED ✗ — fix the ✗ lines above"))
sys.exit(0 if ok else 1)
