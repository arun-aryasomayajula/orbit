#!/usr/bin/env python3
"""orbit doctor — validate a target repo's autopilot wiring WITHOUT running a cycle.
Read-only. Checks: config valid · claude bin · router resolves every category ·
referenced tracks exist · referenced skills resolve · dry-run routing per queued task.

Usage: doctor.py <target-repo>  [--all]
Env:   ORBIT_HOME (engine dir; defaults to this file's repo root)
"""
import sys, os, json, shutil, subprocess, re, platform

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import secrets_env

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

def _secrets_env(target):
    """Parse .autopilot/.secrets.env as KEY=VALUE lines. Thin wrapper over the
    shared parser (engine/secrets_env.py) so doctor, the dashboard's secrets
    panel, run.sh's loop and prd_flow's stage children all agree on the format."""
    return secrets_env.read(target)


def _var_set(name, secrets, check_environ=True):
    """True if `name` is set to a NON-BLANK value in the environment (when
    `check_environ`) or in `secrets` (the parsed .secrets.env mapping).
    Mirrors publish.py's own _auth(), which requires
    `(env.get(v) or "").strip()` truthy - a `key present but blank` line in
    .secrets.env, or an exported-but-empty shell variable, passes a bare
    membership test but still fails publish.py at run time. Advisory doctor
    output must agree with the thing it is advising about, or it tells an
    operator everything is fine right up until the first publish attempt."""
    if check_environ and (os.environ.get(name) or "").strip():
        return True
    return bool((secrets.get(name) or "").strip())

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

# 2d. publish (optional): advisory only — missing ATLASSIAN_* secrets is the
# top setup failure, but it doesn't fail doctor itself since a workspace may be
# mid-setup or only publishes occasionally.
pub_cfg = cfg.get("publish") if isinstance(cfg.get("publish"), dict) else {}
secrets = _secrets_env(TARGET)
if pub_cfg:
    has_confluence = isinstance(pub_cfg.get("confluence"), dict)
    has_jira = isinstance(pub_cfg.get("jira"), dict)
    if has_confluence or has_jira:
        email_set = _var_set("ATLASSIAN_EMAIL", secrets)
        token_set = _var_set("ATLASSIAN_API_TOKEN", secrets)
        if not (email_set and token_set):
            missing = []
            if not email_set: missing.append("ATLASSIAN_EMAIL")
            if not token_set: missing.append("ATLASSIAN_API_TOKEN")
            print(f"  · publish: {' and '.join(missing)} missing — set them in "
                  f"{os.path.join(AP, '.secrets.env')} (0600) or the environment")

# 2e. mcp servers (optional): advisory only — a missing token_env doesn't
# fail doctor, since the server may simply not be reached by any queued
# task yet, but it's the top setup failure a PM will hit once one is.
mcp_cfg = cfg.get("mcp") if isinstance(cfg.get("mcp"), dict) else {}
for name, s in mcp_cfg.items():
    te = isinstance(s, dict) and s.get("token_env")
    if te and te not in os.environ and te not in secrets:
        print(f"  · mcp.{name}: token_env {te} is not set in this shell — interactive cycles "
              f"will reach {name} unauthenticated; a service-mode loop needs it in the "
              f"service environment too")

# Check .secrets.env permissions (if it exists) and informational note for
# Atlassian secrets without publish config
secrets_path = os.path.join(AP, ".secrets.env")
if os.path.exists(secrets_path):
    st = os.stat(secrets_path)
    mode = st.st_mode & 0o777
    if mode & 0o077:
        print(f"  · .secrets.env exists with mode {oct(mode)} — group/other bits are readable; should be 0600 or stricter for security")
    if not pub_cfg:
        email_set = _var_set("ATLASSIAN_EMAIL", secrets, check_environ=False)
        token_set = _var_set("ATLASSIAN_API_TOKEN", secrets, check_environ=False)
        if email_set or token_set:
            print(f"  · Atlassian secrets are set but publish: unconfigured — if you plan to publish to Confluence/Jira, add a publish: block (see config/schema.yaml)")

# 2f. installed service slot count vs concurrency (advisory, best-effort;
# never fatal - launchctl/systemctl may be slow, absent, or the daemon may
# not be reachable in this shell). Silent when NO unit for this target is
# installed at all: most workspaces run interactively or via the launcher
# and were never `orbit install`ed, so "0 installed" is not a problem to
# report - it is the common case.
def _service_slug(path):
    """The service slug for a target repo. Must stay byte-identical to
    install_service.sh and engine.launcher._slug: 'orbit-' + the basename
    with every non-alphanumeric character replaced by a dash, trailing
    dashes stripped."""
    name = re.sub(r"[^a-zA-Z0-9]", "-", os.path.basename(path.rstrip(os.sep)))
    return "orbit-" + name.rstrip("-")

def _unit_owner_on_disk(name, is_darwin):
    """Best-effort: the ORBIT_TARGET baked into the on-disk unit file for a
    launchctl label (macOS - file at ~/Library/LaunchAgents/<name>.plist) or
    a systemd unit name (Linux - file at ~/.config/systemd/user/<name>).
    Returns None when the file is missing or unreadable - callers then fall
    back to the label/unit-name match alone, since a daemon-reported unit's
    file may have since moved or been deleted. Guards against the same
    naming collision install_service.sh guards against: a DIFFERENT
    target's slug can produce a byte-identical label/unit name to one of
    THIS target's derived `-slotN` units."""
    try:
        if is_darwin:
            path = os.path.expanduser(f"~/Library/LaunchAgents/{name}.plist")
            if not os.path.exists(path):
                return None
            import plistlib
            with open(path, "rb") as f:
                data = plistlib.load(f)
            return (data.get("EnvironmentVariables") or {}).get("ORBIT_TARGET")
        else:
            path = os.path.expanduser(f"~/.config/systemd/user/{name}")
            if not os.path.exists(path):
                return None
            for line in open(path):
                if line.startswith("Environment=ORBIT_TARGET="):
                    return line.rstrip("\n").split("=", 2)[2]
            return None
    except (OSError, ValueError):
        return None

def _installed_slot_labels(slug):
    """Best-effort: the set of installed unit labels/names for THIS target's
    slug (slot 0's legacy name plus any `-slotN`), via `launchctl list`
    (macOS) or `systemctl --user list-units` (Linux). Returns None on any
    failure - including an unrecognized platform - which callers treat the
    same as "can't tell", never as "zero installed". A label/unit name that
    matches by prefix but whose on-disk file's ORBIT_TARGET names a
    DIFFERENT target (the naming-collision case install_service.sh also
    guards against) is excluded - when the file can't be read at all, the
    prefix match is kept as-is (best effort, not a false negative)."""
    try:
        if platform.system() == "Darwin":
            r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return None
            labels = set()
            for line in r.stdout.splitlines():
                cols = line.split()
                if not cols:
                    continue
                label = cols[-1]
                if label == f"com.orbit.{slug}" or label.startswith(f"com.orbit.{slug}-slot"):
                    owner = _unit_owner_on_disk(label, is_darwin=True)
                    if owner is not None and owner != TARGET:
                        continue
                    labels.add(label)
            return labels
        elif platform.system() == "Linux":
            r = subprocess.run(["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend"],
                                capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return None
            labels = set()
            for line in r.stdout.splitlines():
                cols = line.split()
                if not cols:
                    continue
                unit = cols[0]
                if unit == f"{slug}.service" or (unit.startswith(f"{slug}-slot") and unit.endswith(".service")):
                    owner = _unit_owner_on_disk(unit, is_darwin=False)
                    if owner is not None and owner != TARGET:
                        continue
                    labels.add(unit)
            return labels
        return None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None

_installed = _installed_slot_labels(_service_slug(TARGET))
if _installed:
    _want = cfg.get("concurrency", 1)
    if not isinstance(_want, int) or isinstance(_want, bool) or _want <= 0:
        _want = 1
    if len(_installed) != _want:
        print(f"  · service: {len(_installed)} installed unit(s) for this target, concurrency wants {_want} "
              f"- re-run `orbit install {TARGET}` to reconcile")

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
_orbit_sk = os.path.join(ORBIT_HOME, "skills")
ORBIT_SKILLS = {f[:-3] for f in os.listdir(_orbit_sk) if f.endswith(".md")} if os.path.isdir(_orbit_sk) else set()
def skill_ok(s):
    if not s: return True
    if s.startswith("superpowers:"): return s.split(":",1)[1] in sp_skills
    return s in disk or s in BUILTIN or s in ORBIT_SKILLS
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

# 5e. QA runtime environment (optional — presence of a `qa:` config block is
# the switch; feature unused → zero behavior change, same convention as 5c/5d).
# Read-only: doctor never launches Playwright MCP or the target app — it only
# confirms the environment CAN run qa_check.py once Task 14 lands. Playwright
# MCP itself is fetched by `npx` at QA-run time (never an engine dependency),
# so node+npx resolving on PATH is the actual precondition; whether Playwright
# is already cached is advisory only (`npx playwright install` is an operator
# act, not something doctor can or should do).
qa_cfg = cfg.get("qa") if isinstance(cfg.get("qa"), dict) else None
if qa_cfg is not None:
    node = shutil.which("node")
    npx = shutil.which("npx")
    check("qa: node on PATH", bool(node), node or "NOT FOUND — install Node.js (qa: requires it)")
    check("qa: npx on PATH", bool(npx), npx or "NOT FOUND — install Node.js (bundles npx; qa: requires it)")
    if npx:
        # Bounded + non-interactive: doctor must stay fast and read-only. An
        # uncached Playwright can make npx try to resolve/download it from the
        # registry (slow, or a hang on a flaky/offline network) — a short
        # timeout turns that into the same advisory line as any other miss,
        # never a doctor hang.
        try:
            pw_ok = subprocess.run([npx, "playwright", "--version"], capture_output=True,
                                    stdin=subprocess.DEVNULL, timeout=10).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            pw_ok = False
        if not pw_ok:
            print("  · qa: `npx playwright --version` failed — it will be fetched on first QA run "
                  "(or install ahead of time: npx playwright install)")
    if cfg.get("runtime_check"):
        print("  · qa: supersedes runtime_check: as the post-commit gate")

# 5f. product workspace (optional - presence of research/ or the insight_lint
# gate is the switch). The PRD skills all open by reading
# .autopilot/tracks/prd-grounding.md, so a product workspace missing it runs
# every PRD stage ungrounded - and the failure is silent: the skill just does
# not find its lane rules. Advisory only, never a check(): a workspace can be
# mid-setup, and doctor's exit code is about wiring that is BROKEN, not
# incomplete.
_gates = cfg.get("gates") if isinstance(cfg.get("gates"), dict) else {}
if os.path.isdir(os.path.join(TARGET, "research")) or "insight_lint" in _gates:
    if not os.path.exists(os.path.join(AP, "tracks", "prd-grounding.md")):
        print("  · product workspace has no .autopilot/tracks/prd-grounding.md - "
              f"the PRD skills read it for their lane rules; re-run `orbit init {TARGET} --product`")

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
