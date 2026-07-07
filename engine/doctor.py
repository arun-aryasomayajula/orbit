#!/usr/bin/env python3
"""ratchet doctor — validate a target repo's autopilot wiring WITHOUT running a cycle.
Read-only. Checks: config valid · claude bin · router resolves every category ·
referenced tracks exist · referenced skills resolve · dry-run routing per queued task.

Usage: doctor.py <target-repo>  [--all]
Env:   RATCHET_HOME (engine dir; defaults to this file's repo root)
"""
import sys, os, json, shutil, subprocess

TARGET = sys.argv[1] if len(sys.argv) > 1 else "."
TARGET = os.path.abspath(TARGET)
SHOW_ALL = "--all" in sys.argv
RATCHET_HOME = os.environ.get("RATCHET_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AP = os.path.join(TARGET, ".autopilot")
import yaml

def find(*rel):
    """Target override wins over engine default."""
    for base in (AP, RATCHET_HOME):
        p = os.path.join(base, *rel)
        if os.path.exists(p):
            return p
    return None

ok = True
def check(label, passed, detail=""):
    global ok
    ok = ok and passed
    print(f"  {'✓' if passed else '✗'} {label}" + (f"  {detail}" if detail else ""))

print(f"\nRATCHET DOCTOR — {TARGET}\n")

# 1. config
cfg_path = os.path.join(AP, "config.yaml")
check("config.yaml present", os.path.exists(cfg_path), cfg_path)
if not os.path.exists(cfg_path):
    sys.exit("\n  run `ratchet init` in the target first.")
try:
    subprocess.run(["python3", os.path.join(RATCHET_HOME, "engine", "config.py"), "validate", TARGET], check=True)
    cfg = yaml.safe_load(open(cfg_path)) or {}
except subprocess.CalledProcessError:
    ok = False; cfg = yaml.safe_load(open(cfg_path)) or {}

# 2. claude bin
env = cfg.get("env", {}) or {}
cb = env.get("CLAUDE_BIN", "auto")
claude = shutil.which("claude") if cb == "auto" else cb
check("claude binary resolves", bool(claude and (shutil.which(claude) or os.path.exists(claude))), claude or "NOT FOUND")

# 3. router — target's .autopilot/router.yaml overrides the engine default at router/router.yaml
router_path = find("router.yaml") or (
    os.path.join(RATCHET_HOME, "router", "router.yaml")
    if os.path.exists(os.path.join(RATCHET_HOME, "router", "router.yaml")) else None)
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
    print("  · (no queue yet — run the loop once or `ratchet backlog` to populate)")

print("\n" + ("  ALL CHECKS PASSED ✓" if ok else "  SOME CHECKS FAILED ✗ — fix the ✗ lines above"))
sys.exit(0 if ok else 1)
