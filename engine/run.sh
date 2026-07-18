#!/bin/bash
# Orbit — resilient outer loop wrapper (worktree-isolated, repo-agnostic).
#
# Runs Claude Code headless for ONE task (/orbit-cycle) per iteration inside a
# DEDICATED git worktree on a fresh origin/<base_branch>. Never touches the target's
# main checkout or uncommitted work. Ships each task to its OWN branch
# (<branch_prefix>/task-<id>, never the base, never --force) for independent review.
#
# The ENGINE is generic. Everything project-specific comes from the TARGET repo's
# .autopilot/config.yaml (read via config.py). State lives in .autopilot/state/.
#
# Usage:  ORBIT_HOME=/path/to/orbit ./run.sh <target-repo>
#   or set ORBIT_TARGET. install.sh writes a launchd/systemd unit that sets both.
set -uo pipefail

ORBIT_HOME="${ORBIT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENGINE="$ORBIT_HOME/engine"
TARGET="${1:-${ORBIT_TARGET:-}}"
[ -n "$TARGET" ] || { echo "usage: run.sh <target-repo>  (or set ORBIT_TARGET)"; exit 2; }
TARGET="$(cd "$TARGET" && pwd)"

# Load the target's profile → ORBIT_* + AP_HOME/AP_STATE exports.
eval "$(python3 "$ENGINE/config.py" shellenv "$TARGET")" || { echo "config load failed"; exit 2; }

REPO="$ORBIT_REPO"
WT="${AP_WORKTREE:-${REPO}-orbit-worktree}"
STATE="$AP_STATE"
LOGDIR="$STATE/logs"
DIFFDIR="$STATE/diffs"
STOP_FILE="$STATE/STOP"
SETTINGS="$ORBIT_HOME/config/orbit.settings.json"
MODEL="${AP_MODEL:-$ORBIT_MODEL}"
PERM="${AP_PERM:-$ORBIT_PERM}"
INTERVAL="${AP_INTERVAL:-$ORBIT_INTERVAL}"
MAX_TASKS_PER_DAY="${AP_MAX_TASKS:-$ORBIT_MAX_TASKS}"
CYCLE_TIMEOUT="${AP_CYCLE_TIMEOUT:-$ORBIT_CYCLE_TIMEOUT}"
LIMIT_BACKOFF="${AP_LIMIT_BACKOFF:-1800}"
BASE_REF="origin/$ORBIT_BASE_BRANCH"
export AP_HOME AP_STATE ORBIT_HOME
export AP_BASE_BRANCH="$ORBIT_BASE_BRANCH"   # the cycle reads this to branch/diff against the right base

# Resolve the claude binary.
if [ "$ORBIT_CLAUDE_BIN" = "auto" ]; then
  CLAUDE_BIN="$(command -v claude || echo "$HOME/.local/bin/claude")"
else
  CLAUDE_BIN="$ORBIT_CLAUDE_BIN"
fi

mkdir -p "$LOGDIR" "$DIFFDIR" "$STATE/reviews"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }
log(){ echo "[$(ts)] $*" | tee -a "$LOGDIR/orbit.log"; }
today(){ date '+%Y-%m-%d'; }
DASH="${ORBIT_DASHBOARD_URL:-http://127.0.0.1:8787}"
notify(){ python3 "$ENGINE/notify.py" "$1" "$2" "${3:-}" >/dev/null 2>&1 || true; }

# --- preflight: are this repo's gate dependencies up? (config gates[].needs) ---
service_up() {
  case "$1" in
    # pg_isready is often keg-only (not on launchd PATH) — fall back to a TCP probe
    postgres) if command -v pg_isready >/dev/null 2>&1; then pg_isready >/dev/null 2>&1
              else (exec 3<>"/dev/tcp/127.0.0.1/${PGPORT:-5432}") 2>/dev/null; fi ;;
    mysql)    mysqladmin ping >/dev/null 2>&1 ;;
    redis)    redis-cli ping >/dev/null 2>&1 ;;
    docker)   docker info >/dev/null 2>&1 ;;
    node)     command -v node >/dev/null 2>&1 ;;
    python)   command -v python3 >/dev/null 2>&1 ;;
    *)        eval "$1" >/dev/null 2>&1 ;;   # arbitrary shell test
  esac
}
gates_ready() {
  local n rc=0
  while IFS= read -r n; do
    [ -z "$n" ] && continue
    if ! service_up "$n"; then log "gate dep '$n' not ready — skipping cycle"; rc=1; fi
  done < <(python3 "$ENGINE/config.py" needs "$TARGET")
  return $rc
}

prepare_worktree() {
  git -C "$REPO" fetch origin --quiet 2>>"$LOGDIR/orbit.log" || true
  if [ ! -d "$WT/.git" ] && ! git -C "$REPO" worktree list | grep -q "$WT"; then
    log "creating worktree at $WT"
    git -C "$REPO" worktree add --force "$WT" "$BASE_REF" >>"$LOGDIR/orbit.log" 2>&1 || { log "FATAL: worktree add failed"; return 1; }
  fi
  git -C "$WT" checkout --detach "$BASE_REF" >>"$LOGDIR/orbit.log" 2>&1 || true
  git -C "$WT" reset --hard "$BASE_REF" >>"$LOGDIR/orbit.log" 2>&1
  git -C "$WT" clean -fd >>"$LOGDIR/orbit.log" 2>&1
  git -C "$WT" for-each-ref --format='%(refname:short)' "refs/heads/$ORBIT_BRANCH_PREFIX/" 2>/dev/null \
    | while read -r b; do [ -n "$b" ] && git -C "$WT" branch -D "$b" >>"$LOGDIR/orbit.log" 2>&1 || true; done
}

record_spend() {
  python3 - "$1" "$STATE/.spend-$(today)" <<'PYEOF'
import json, sys, os
log, sf = sys.argv[1], sys.argv[2]
cost = 0.0
try:
    for ln in open(log, errors="ignore"):
        ln = ln.strip()
        if ln.startswith("{") and '"type"' in ln and 'result' in ln:
            try: cost = float(json.loads(ln).get("total_cost_usd", 0) or 0)
            except Exception: pass
except Exception: pass
prev = 0.0
if os.path.exists(sf):
    try: prev = float(open(sf).read().strip() or 0)
    except Exception: pass
open(sf, "w").write(f"{prev+cost:.4f}")
print(f"{cost:.4f}")
PYEOF
}
todays_spend(){ cat "$STATE/.spend-$(today)" 2>/dev/null || echo 0; }

count_pickable() {
  python3 - "$STATE/queue.json" "$ENGINE/ledger.py" "$STATE/skips.txt" <<'PYEOF'
import json, os, subprocess, sys
qf, ledger, sk = sys.argv[1], sys.argv[2], sys.argv[3]
q = json.load(open(qf)) if os.path.exists(qf) else {}
tasks = q.get("queue", q.get("tasks", q)) if isinstance(q, dict) else q
try: worked = set(subprocess.check_output(["python3", ledger, "worked-ids"], text=True).split())
except Exception: worked = set()
skips = set(l.strip() for l in open(sk)) if os.path.exists(sk) else set()
print(sum(1 for t in tasks if str(t.get("id")) not in worked and str(t.get("id")) not in skips))
PYEOF
}

# The id the cycle will MOST LIKELY claim — first pickable task in queue order,
# same worked/skipped filter as count_pickable. Written as a provisional marker
# before the cycle so the dashboard names the task in seconds instead of showing
# "claiming a task…" for minutes while the agent reads STATE/backlog and vets it.
# The agent's own ledger-claim overwrites .current-task-id with the authoritative id.
next_pickable_id() {
  python3 - "$STATE/queue.json" "$ENGINE/ledger.py" "$STATE/skips.txt" <<'PYEOF'
import json, os, subprocess, sys
qf, ledger, sk = sys.argv[1], sys.argv[2], sys.argv[3]
q = json.load(open(qf)) if os.path.exists(qf) else {}
tasks = q.get("queue", q.get("tasks", q)) if isinstance(q, dict) else q
try: worked = set(subprocess.check_output(["python3", ledger, "worked-ids"], text=True).split())
except Exception: worked = set()
skips = set(l.strip() for l in open(sk)) if os.path.exists(sk) else set()
for t in tasks:
    tid = str(t.get("id"))
    if tid not in worked and tid not in skips:
        print(tid); break
PYEOF
}

# --- optional PR raising (config `pull_requests: "github"`) -----------------
# Wrapper-only: the agent never sees a provider credential. Opt-in and
# degrade-to-log — a missing/unauthenticated `gh` or a failed create must never
# fail the cycle (the branch is already pushed). Merging stays 100% manual.
raise_pr() {
  local tid="$1" branch="$2"
  [ "${ORBIT_PULL_REQUESTS:-off}" = "github" ] || return 0
  command -v gh >/dev/null 2>&1 || { log "WARN: pull_requests=github but gh not on PATH — skipping PR create"; return 0; }
  local subject url body="$STATE/reviews/task-${tid}.md"
  subject="$(git -C "$WT" log -1 --format=%s 2>/dev/null || echo "orbit: $branch")"
  if [ -n "$tid" ] && [ -f "$body" ]; then
    url="$(cd "$WT" && gh pr create --head "$branch" --base "$ORBIT_BASE_BRANCH" \
           --title "$subject" --body-file "$body" 2>>"$LOGDIR/orbit.log")" || url=""
  else
    url="$(cd "$WT" && gh pr create --head "$branch" --base "$ORBIT_BASE_BRANCH" \
           --title "$subject" --body "Automated Orbit ship — review packet under .autopilot/state/reviews/." 2>>"$LOGDIR/orbit.log")" || url=""
  fi
  if [ -n "$url" ]; then
    log "opened PR $url (merge stays manual)"
    [ -n "$tid" ] && { python3 "$ENGINE/ledger.py" pr "$tid" "$url" >>"$LOGDIR/orbit.log" 2>&1 || true; }
    notify "🔀 Orbit PR" "task ${tid:-?} → $url" "$url"
  else
    log "WARN: gh pr create failed for $branch (auth? PR already open?) — branch is pushed; open one manually"
  fi
}

run_cycle() {
  local out="$1"; cd "$WT" || return 1
  local caff=""; command -v caffeinate >/dev/null 2>&1 && caff="caffeinate -i"
  $caff "$CLAUDE_BIN" --print "/orbit-cycle" \
      --permission-mode "$PERM" --settings "$SETTINGS" --model "$MODEL" \
      --add-dir "$REPO" --add-dir "$ORBIT_HOME" \
      --output-format stream-json --verbose >"$out" 2>&1 </dev/null &
  local cpid=$!
  # Watchdog: hard-kill the cycle if it outruns CYCLE_TIMEOUT.
  # CRITICAL: run_cycle executes inside status="$(one_iteration)", whose stdout is a
  # pipe. If the watchdog (and its `sleep`) inherit that pipe, killing the subshell
  # orphans the `sleep`, which keeps the pipe's write end open — so the command
  # substitution never gets EOF and the whole loop wedges for the full CYCLE_TIMEOUT
  # (~1h). Redirect all of the watchdog's fds off the pipe, and kill its sleep child
  # on teardown so nothing is left holding it.
  ( sleep "$CYCLE_TIMEOUT"; kill -TERM "$cpid" 2>/dev/null; sleep 10; kill -KILL "$cpid" 2>/dev/null ) </dev/null >/dev/null 2>&1 &
  local wpid=$!
  wait "$cpid"; local rc=$?
  pkill -P "$wpid" 2>/dev/null || true   # kill the watchdog's sleep child first
  kill "$wpid" 2>/dev/null || true
  return $rc
}

one_iteration() {
  gates_ready || { echo SKIP; return; }
  prepare_worktree || { echo FAIL; return; }
  # Auto-close verified no-ops (tasks whose shipped commit is already an ancestor
  # of the base branch) BEFORE regenerating the queue, so they never re-inject or
  # re-escalate into the operator inbox.
  python3 "$ENGINE/autoclose.py" >>"$LOGDIR/orbit.log" 2>&1 || log "WARN: autoclose failed"
  python3 "$ENGINE/backlog_to_tasks.py" >>"$LOGDIR/orbit.log" 2>&1 || log "WARN: backlog→queue refresh failed"
  if [ -f "$STATE/AUTO_PROMOTE" ] && [ "$(count_pickable)" = "0" ]; then
    log "auto-feed: promoting next safe task"
    python3 "$ENGINE/autopromote.py" >>"$LOGDIR/orbit.log" 2>&1 || log "WARN: autopromote failed"
  fi
  local pickable; pickable="$(count_pickable)"
  if [ "${pickable:-0}" = "0" ]; then log "no pickable tasks — idling, no cycle spent."; echo EMPTY; return; fi

  local cyclelog="$LOGDIR/cycle-$(date '+%Y%m%dT%H%M%S').log"
  log "cycle start ($pickable pickable) → $cyclelog"
  # Name the task on the dashboard immediately (see next_pickable_id). Provisional
  # until the agent's claim overwrites .current-task-id; kept in a SEPARATE file so
  # the post-cycle push/escalation below only ever reads the authoritative marker.
  local cand; cand="$(next_pickable_id 2>/dev/null)"
  [ -n "$cand" ] && echo "$cand" > "$STATE/.cycle-candidate"
  set_idle_reason "cycle-running" 0
  run_cycle "$cyclelog"
  log "cycle cost \$$(record_spend "$cyclelog") (today \$$(todays_spend), informational)"

  local marker="$STATE/.current-task-id" tid=""
  [ -f "$marker" ] && tid="$(tr -d '[:space:]' < "$marker" 2>/dev/null)"
  local ahead; ahead="$(git -C "$WT" rev-list --count "$BASE_REF"..HEAD 2>/dev/null || echo 0)"
  local patch="$DIFFDIR/cycle-$(date '+%Y%m%dT%H%M%S').patch"
  { git -C "$WT" diff "$BASE_REF"...HEAD 2>/dev/null; git -C "$WT" diff 2>/dev/null; } > "$patch"
  [ -s "$patch" ] || rm -f "$patch"

  if [ "$ahead" -gt 0 ]; then
    local sha branch; sha="$(git -C "$WT" rev-parse --short HEAD 2>/dev/null || echo '?')"
    branch="$ORBIT_BRANCH_PREFIX/task-${tid:-cycle-$(date '+%Y%m%dT%H%M%S')}"
    log "cycle committed $ahead change(s) ($sha) — pushing $branch"
    if ! git -C "$WT" push origin "HEAD:refs/heads/$branch" >>"$LOGDIR/orbit.log" 2>&1; then
      branch="${branch}-$(date '+%Y%m%dT%H%M%S')"
      git -C "$WT" push origin "HEAD:refs/heads/$branch" >>"$LOGDIR/orbit.log" 2>&1 || branch=""
    fi
    if [ -n "$branch" ]; then
      log "pushed origin/$branch ($sha) — review, then Merge/Reject"
      [ -n "$tid" ] && { python3 "$ENGINE/ledger.py" pushed "$tid" "origin/$branch" 2>/dev/null || true
        python3 "$ENGINE/review_packet.py" "$tid" "$WT" "$branch" "$ORBIT_BASE_BRANCH" >>"$LOGDIR/orbit.log" 2>&1 || true; }
      raise_pr "$tid" "$branch"
      notify "🔧 Orbit shipped" "task ${tid:-?} → $branch — review packet is ready; merge or reject" "$DASH"
    else
      log "WARN: push failed twice — patch kept: ${patch:-none}"
    fi
  else
    log "cycle produced no commit (no-op / escalation)."
    if [ -n "$tid" ] && ! python3 "$ENGINE/ledger.py" worked-ids 2>/dev/null | tr ' ' '\n' | grep -qx "$tid"; then
      python3 "$ENGINE/ledger.py" escalate "$tid" "wrapper: no commit (gate fail / too large / incomplete)" >>"$LOGDIR/orbit.log" 2>&1 || true
    fi
    # An escalated cycle is a stalled human gate — say so where the human is.
    if [ -n "$tid" ] && [ "$(python3 "$ENGINE/ledger.py" state "$tid" 2>/dev/null)" = "escalated" ]; then
      notify "🙋 Orbit needs you" "task $tid escalated — answer it to unblock the loop" "$DASH"
    fi
  fi
  rm -f "$marker" "$STATE/.cycle-candidate" 2>/dev/null || true

  if grep -qiE "usage limit|rate limit|session limit|429|quota" "$cyclelog" 2>/dev/null \
     && ! grep -q '"type":"result"' "$cyclelog" 2>/dev/null; then echo LIMIT; return; fi
  echo OK
}

# Marker the dashboard reads to explain WHY the loop is idle (backoff / idle reason + when it resumes).
# Format: "<reason> <resume-epoch>". Defined BEFORE the loop so the STOP and daily-cap branches can set
# it too — otherwise a paused/capped loop keeps a stale marker and the dashboard shows "between-cycles"
# (or a false "next pick ~Ns") while the loop is actually STOPPED and picking nothing.
set_idle_reason(){ echo "$1 $(( $(date +%s) + ${2:-0} ))" > "$STATE/.idle-reason"; }
log "==== orbit up (pid $$) target=$TARGET model=$MODEL base=$ORBIT_BASE_BRANCH ===="
while true; do
  [ -f "$STOP_FILE" ] && { log "STOP present — idling."; set_idle_reason "stopped" 60; sleep 60; continue; }
  count="$(cat "$STATE/.count-$(today)" 2>/dev/null || echo 0)"
  if [ "$count" -ge "$MAX_TASKS_PER_DAY" ]; then log "daily cap $MAX_TASKS_PER_DAY reached — idling."; set_idle_reason "daily-cap" 600; sleep 600; continue; fi
  # one_iteration's stdout carries BOTH the tee'd log lines and the final status
  # word — dispatch on the LAST line only. (Matching the whole capture meant
  # OK/EMPTY/SKIP/LIMIT never matched once anything was logged: the daily count
  # never incremented and usage-limit backoff never fired.)
  status="$(one_iteration)"; status="${status##*$'\n'}"
  case "$status" in
    OK)    echo $((count+1)) > "$STATE/.count-$(today)"; set_idle_reason "between-cycles" "$INTERVAL"; sleep "$INTERVAL" ;;
    FAIL)  echo $((count+1)) > "$STATE/.count-$(today)"; set_idle_reason "recovering" 120; sleep 120 ;;
    EMPTY) set_idle_reason "queue-empty" 600; sleep 600 ;;
    SKIP)  set_idle_reason "waiting-for-services" 300; sleep 300 ;;
    LIMIT) log "usage limit — backing off ${LIMIT_BACKOFF}s."; set_idle_reason "usage-limit" "$LIMIT_BACKOFF"; sleep "$LIMIT_BACKOFF" ;;
    *)     set_idle_reason "between-cycles" "$INTERVAL"; sleep "$INTERVAL" ;;
  esac
done
