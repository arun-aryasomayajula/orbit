#!/bin/bash
# Ratchet — resilient outer loop wrapper (worktree-isolated, repo-agnostic).
#
# Runs Claude Code headless for ONE task (/ratchet-cycle) per iteration inside a
# DEDICATED git worktree on a fresh origin/<base_branch>. Never touches the target's
# main checkout or uncommitted work. Ships each task to its OWN branch
# (<branch_prefix>/task-<id>, never the base, never --force) for independent review.
#
# The ENGINE is generic. Everything project-specific comes from the TARGET repo's
# .autopilot/config.yaml (read via config.py). State lives in .autopilot/state/.
#
# Usage:  RATCHET_HOME=/path/to/ratchet ./run.sh <target-repo>
#   or set RATCHET_TARGET. install.sh writes a launchd/systemd unit that sets both.
set -uo pipefail

RATCHET_HOME="${RATCHET_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENGINE="$RATCHET_HOME/engine"
TARGET="${1:-${RATCHET_TARGET:-}}"
[ -n "$TARGET" ] || { echo "usage: run.sh <target-repo>  (or set RATCHET_TARGET)"; exit 2; }
TARGET="$(cd "$TARGET" && pwd)"

# Load the target's profile → RATCHET_* + AP_HOME/AP_STATE exports.
eval "$(python3 "$ENGINE/config.py" shellenv "$TARGET")" || { echo "config load failed"; exit 2; }

REPO="$RATCHET_REPO"
WT="${AP_WORKTREE:-${REPO}-ratchet-worktree}"
STATE="$AP_STATE"
LOGDIR="$STATE/logs"
DIFFDIR="$STATE/diffs"
STOP_FILE="$STATE/STOP"
SETTINGS="$RATCHET_HOME/config/ratchet.settings.json"
MODEL="${AP_MODEL:-$RATCHET_MODEL}"
PERM="${AP_PERM:-$RATCHET_PERM}"
INTERVAL="${AP_INTERVAL:-$RATCHET_INTERVAL}"
MAX_TASKS_PER_DAY="${AP_MAX_TASKS:-$RATCHET_MAX_TASKS}"
CYCLE_TIMEOUT="${AP_CYCLE_TIMEOUT:-$RATCHET_CYCLE_TIMEOUT}"
LIMIT_BACKOFF="${AP_LIMIT_BACKOFF:-1800}"
BASE_REF="origin/$RATCHET_BASE_BRANCH"
export AP_HOME AP_STATE RATCHET_HOME
export AP_BASE_BRANCH="$RATCHET_BASE_BRANCH"   # the cycle reads this to branch/diff against the right base

# Resolve the claude binary.
if [ "$RATCHET_CLAUDE_BIN" = "auto" ]; then
  CLAUDE_BIN="$(command -v claude || echo "$HOME/.local/bin/claude")"
else
  CLAUDE_BIN="$RATCHET_CLAUDE_BIN"
fi

mkdir -p "$LOGDIR" "$DIFFDIR" "$STATE/reviews"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }
log(){ echo "[$(ts)] $*" | tee -a "$LOGDIR/ratchet.log"; }
today(){ date '+%Y-%m-%d'; }
notify(){ python3 "$ENGINE/notify.py" "$1" "$2" >/dev/null 2>&1 || true; }

# --- preflight: are this repo's gate dependencies up? (config gates[].needs) ---
service_up() {
  case "$1" in
    postgres) pg_isready >/dev/null 2>&1 ;;
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
  git -C "$REPO" fetch origin --quiet 2>>"$LOGDIR/ratchet.log" || true
  if [ ! -d "$WT/.git" ] && ! git -C "$REPO" worktree list | grep -q "$WT"; then
    log "creating worktree at $WT"
    git -C "$REPO" worktree add --force "$WT" "$BASE_REF" >>"$LOGDIR/ratchet.log" 2>&1 || { log "FATAL: worktree add failed"; return 1; }
  fi
  git -C "$WT" checkout --detach "$BASE_REF" >>"$LOGDIR/ratchet.log" 2>&1 || true
  git -C "$WT" reset --hard "$BASE_REF" >>"$LOGDIR/ratchet.log" 2>&1
  git -C "$WT" clean -fd >>"$LOGDIR/ratchet.log" 2>&1
  git -C "$WT" for-each-ref --format='%(refname:short)' "refs/heads/$RATCHET_BRANCH_PREFIX/" 2>/dev/null \
    | while read -r b; do [ -n "$b" ] && git -C "$WT" branch -D "$b" >>"$LOGDIR/ratchet.log" 2>&1 || true; done
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

run_cycle() {
  local out="$1"; cd "$WT" || return 1
  local caff=""; command -v caffeinate >/dev/null 2>&1 && caff="caffeinate -i"
  $caff "$CLAUDE_BIN" --print "/ratchet-cycle" \
      --permission-mode "$PERM" --settings "$SETTINGS" --model "$MODEL" \
      --add-dir "$REPO" --add-dir "$RATCHET_HOME" \
      --output-format stream-json --verbose >"$out" 2>&1 &
  local cpid=$!
  ( sleep "$CYCLE_TIMEOUT"; kill -TERM "$cpid" 2>/dev/null; sleep 10; kill -KILL "$cpid" 2>/dev/null ) &
  local wpid=$!
  wait "$cpid"; local rc=$?
  kill "$wpid" 2>/dev/null || true
  return $rc
}

one_iteration() {
  gates_ready || { echo SKIP; return; }
  prepare_worktree || { echo FAIL; return; }
  python3 "$ENGINE/backlog_to_tasks.py" >>"$LOGDIR/ratchet.log" 2>&1 || log "WARN: backlog→queue refresh failed"
  if [ -f "$STATE/AUTO_PROMOTE" ] && [ "$(count_pickable)" = "0" ]; then
    log "auto-feed: promoting next safe task"
    python3 "$ENGINE/autopromote.py" >>"$LOGDIR/ratchet.log" 2>&1 || log "WARN: autopromote failed"
  fi
  local pickable; pickable="$(count_pickable)"
  if [ "${pickable:-0}" = "0" ]; then log "no pickable tasks — idling, no cycle spent."; echo EMPTY; return; fi

  local cyclelog="$LOGDIR/cycle-$(date '+%Y%m%dT%H%M%S').log"
  log "cycle start ($pickable pickable) → $cyclelog"
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
    branch="$RATCHET_BRANCH_PREFIX/task-${tid:-cycle-$(date '+%Y%m%dT%H%M%S')}"
    log "cycle committed $ahead change(s) ($sha) — pushing $branch"
    if ! git -C "$WT" push origin "HEAD:refs/heads/$branch" >>"$LOGDIR/ratchet.log" 2>&1; then
      branch="${branch}-$(date '+%Y%m%dT%H%M%S')"
      git -C "$WT" push origin "HEAD:refs/heads/$branch" >>"$LOGDIR/ratchet.log" 2>&1 || branch=""
    fi
    if [ -n "$branch" ]; then
      log "pushed origin/$branch ($sha) — review, then Merge/Reject"
      [ -n "$tid" ] && { python3 "$ENGINE/ledger.py" pushed "$tid" "origin/$branch" 2>/dev/null || true
        python3 "$ENGINE/review_packet.py" "$tid" "$WT" "$branch" "$RATCHET_BASE_BRANCH" >>"$LOGDIR/ratchet.log" 2>&1 || true; }
      notify "🔧 Ratchet shipped" "task ${tid:-?} → $branch"
    else
      log "WARN: push failed twice — patch kept: ${patch:-none}"
    fi
  else
    log "cycle produced no commit (no-op / escalation)."
    if [ -n "$tid" ] && ! python3 "$ENGINE/ledger.py" worked-ids 2>/dev/null | tr ' ' '\n' | grep -qx "$tid"; then
      python3 "$ENGINE/ledger.py" escalate "$tid" "wrapper: no commit (gate fail / too large / incomplete)" >>"$LOGDIR/ratchet.log" 2>&1 || true
    fi
  fi
  rm -f "$marker" 2>/dev/null || true

  if grep -qiE "usage limit|rate limit|session limit|429|quota" "$cyclelog" 2>/dev/null \
     && ! grep -q '"type":"result"' "$cyclelog" 2>/dev/null; then echo LIMIT; return; fi
  echo OK
}

log "==== ratchet up (pid $$) target=$TARGET model=$MODEL base=$RATCHET_BASE_BRANCH ===="
while true; do
  [ -f "$STOP_FILE" ] && { log "STOP present — idling."; sleep 60; continue; }
  count="$(cat "$STATE/.count-$(today)" 2>/dev/null || echo 0)"
  if [ "$count" -ge "$MAX_TASKS_PER_DAY" ]; then log "daily cap $MAX_TASKS_PER_DAY reached — idling."; sleep 600; continue; fi
  status="$(one_iteration)"
  case "$status" in
    OK)    echo $((count+1)) > "$STATE/.count-$(today)"; sleep "$INTERVAL" ;;
    FAIL)  echo $((count+1)) > "$STATE/.count-$(today)"; sleep 120 ;;
    EMPTY) sleep 600 ;;
    SKIP)  sleep 300 ;;
    LIMIT) log "usage limit — backing off ${LIMIT_BACKOFF}s."; sleep "$LIMIT_BACKOFF" ;;
    *)     sleep "$INTERVAL" ;;
  esac
done
