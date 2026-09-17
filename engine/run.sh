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
# --- slot identity ------------------------------------------------------------
# A SLOT is one independent worker of this loop: it claims its own task, runs it
# in its own worktree, and ships it on its own branch. ORBIT_SLOT names THIS
# process's lane; ORBIT_SLOTS says how many lanes the deployment is meant to be
# running (config `concurrency`, delivered by shellenv as ORBIT_CONCURRENCY).
# Both default so that an untouched environment is EXACTLY today's loop: one
# worker, slot 0. Nothing here starts other slots — the service manager does
# (`orbit install`); this script only ever is one of them.
SLOT="${ORBIT_SLOT:-0}"
SLOTS="${ORBIT_SLOTS:-${ORBIT_CONCURRENCY:-1}}"
# --- concurrency bound (extracted verbatim by tests/test_slot_loop.py) --------
# Clamps $SLOTS to 1..SLOTS_MAX.
# ONE contract, four enforcement points: config.py's CONCURRENCY_MAX (which
# `config.py validate` refuses out-of-range values against), the dashboard's
# Admin patch validator, the launcher's clamp, and this. Here it CLAMPS instead
# of exiting: ORBIT_SLOTS can be set by hand or by a service unit written
# before the bound existed, and a loop that refuses to boot is worse than one
# running the nearest legal slot count. Keep SLOTS_MAX in step with
# config.py's CONCURRENCY_MAX (tests/test_slot_loop.py asserts they agree).
SLOTS_MAX=32
# The WARN is deferred: log() needs $LOGDIR, which is not resolved yet.
SLOTS_WARN=""
case "$SLOTS" in
  ''|*[!0-9]*) SLOTS_WARN="slot count '$SLOTS' is not a number - clamped to 1"; SLOTS=1 ;;
esac
if [ "$SLOTS" -lt 1 ]; then
  SLOTS_WARN="slot count $SLOTS is below the 1..$SLOTS_MAX bound - clamped to 1"; SLOTS=1
elif [ "$SLOTS" -gt "$SLOTS_MAX" ]; then
  SLOTS_WARN="slot count $SLOTS is above the 1..$SLOTS_MAX bound - clamped to $SLOTS_MAX"; SLOTS="$SLOTS_MAX"
fi
# --- end concurrency bound ---
# Per-slot worktree. Two slots resetting ONE worktree to origin/<base> would
# stomp each other's checkout mid-cycle, so the path carries the slot number.
# AP_WORKTREE still wins (an explicit operator override, unchanged). Slot 0's
# default path does change (…-orbit-worktree → …-orbit-wt-0): accepted, because
# a worktree is disposable scratch — nothing outside this script names it, and
# prepare_worktree recreates whatever is missing on the next tick. The stale
# …-orbit-worktree directory is simply left alone for the operator to delete.
WT="${AP_WORKTREE:-${REPO}-orbit-wt-${SLOT}}"
STATE="$AP_STATE"
LOGDIR="$STATE/logs"
DIFFDIR="$STATE/diffs"
STOP_FILE="$STATE/STOP"
# Test/smoke seam only: run exactly ONE iteration then exit, instead of the
# infinite loop below. Off (unset/0) by default - a real deployment always
# leaves this unset. Exists so a concurrency smoke can drive a single slot
# tick without pre-arming STOP first, which would also block the very claim
# the smoke wants to observe.
ONE_SHOT="${ORBIT_ONE_SHOT:-0}"
SETTINGS="$ORBIT_HOME/config/orbit.settings.json"
MODEL="${AP_MODEL:-$ORBIT_MODEL}"
PERM="${AP_PERM:-$ORBIT_PERM}"
INTERVAL="${AP_INTERVAL:-$ORBIT_INTERVAL}"
MAX_TASKS_PER_DAY="${AP_MAX_TASKS:-$ORBIT_MAX_TASKS}"
CYCLE_TIMEOUT="${AP_CYCLE_TIMEOUT:-$ORBIT_CYCLE_TIMEOUT}"
LIMIT_BACKOFF="${AP_LIMIT_BACKOFF:-1800}"
# How long a slot naps after finding nothing it may claim. Slot 0 keeps today's
# ten minutes: it is the housekeeper, so its own next tick is the thing that
# regenerates and tops up the queue. A higher slot has no housekeeping of its
# own — work can only appear for it when slot 0 refills the queue — so a
# ten-minute nap leaves it sitting idle with claimable work already waiting. It
# polls faster instead; a tick that finds nothing costs one queue read.
EMPTY_BACKOFF=600; [ "$SLOT" = "0" ] || EMPTY_BACKOFF=60
BASE_REF="origin/$ORBIT_BASE_BRANCH"
# --- per-slot state filenames -------------------------------------------------
# These three marker filenames are LOAD-BEARING: the dashboard reads
# $STATE/.current-task-id, .cycle-candidate and .idle-reason by exact name.
# Slot 0 therefore keeps the LEGACY (unsuffixed) names byte-for-byte, so an
# existing single-slot deployment sees no rename at all; only the additional
# slots namespace theirs with a `.N` suffix (the dashboard's per-slot view
# knows to look for those). Same reason the pid file is <slot>.pid: the reaper
# looks it up by slot number (ledger.py `_slot_worker_matches`).
SFX=""; [ "$SLOT" = "0" ] || SFX=".$SLOT"
MARKER="$STATE/.current-task-id$SFX"
CAND_MARKER="$STATE/.cycle-candidate$SFX"
IDLE_MARKER="$STATE/.idle-reason$SFX"
WORKER_PID_FILE="$STATE/workers/$SLOT.pid"
# The LOOP's own file, for this process's whole lifetime (the worker file above
# exists only while a task is in flight). `loop-` prefixed so the per-cycle
# glob can tell the two apart by stem: <slot>.pid is a numeric stem, this one
# never is (command_center.workers_state, launcher._live_workers).
LOOP_PID_FILE="$STATE/workers/loop-$SLOT.pid"
export AP_HOME AP_STATE ORBIT_HOME
export AP_BASE_BRANCH="$ORBIT_BASE_BRANCH"   # the cycle reads this to branch/diff against the right base
# Children (the cycle, autopromote, anything the cycle shells out to) must see
# the SAME slot numbers this process is using, not re-derive them from config.
export ORBIT_SLOT="$SLOT" ORBIT_SLOTS="$SLOTS"

# Resolve the claude binary.
if [ "$ORBIT_CLAUDE_BIN" = "auto" ]; then
  CLAUDE_BIN="$(command -v claude || echo "$HOME/.local/bin/claude")"
else
  CLAUDE_BIN="$ORBIT_CLAUDE_BIN"
fi

mkdir -p "$LOGDIR" "$DIFFDIR" "$STATE/reviews" "$STATE/workers"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }
# Every slot appends to the SAME orbit.log, so each line says which slot wrote
# it. The timestamp stays first: the dashboard anchors its heartbeat/duration
# parsing on a leading "[<ts>]" (command_center.py _LOGTS), so the slot tag goes
# after it, never before.
log(){ echo "[$(ts)] [slot $SLOT] $*" | tee -a "$LOGDIR/orbit.log"; }
# Same line, FILE ONLY. one_iteration's stdout is a data channel (the status
# word the outer loop dispatches on) and so is claim_first's (the id it won), so
# any code whose stdout is read by a caller must log with this, never log().
logf(){ echo "[$(ts)] [slot $SLOT] $*" >> "$LOGDIR/orbit.log"; }
# Deferred from the slot-identity block above (log() only works once LOGDIR is
# resolved): say out loud that the requested slot count was not the one used.
[ -n "$SLOTS_WARN" ] && log "WARN: $SLOTS_WARN"
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
  # Anchored match: `grep -q "$WT"` would also match ANOTHER slot's worktree
  # (…-orbit-wt-1 contains …-orbit-wt-1x, and …-wt-1 is a prefix of …-wt-10), so
  # a slot could decide its own worktree already exists and never create it.
  if [ ! -d "$WT/.git" ] && ! git -C "$REPO" worktree list --porcelain | grep -qx "worktree $WT"; then
    log "creating worktree at $WT"
    git -C "$REPO" worktree add --force "$WT" "$BASE_REF" >>"$LOGDIR/orbit.log" 2>&1 || { log "FATAL: worktree add failed"; return 1; }
  fi
  git -C "$WT" checkout --detach "$BASE_REF" >>"$LOGDIR/orbit.log" 2>&1 || true
  git -C "$WT" reset --hard "$BASE_REF" >>"$LOGDIR/orbit.log" 2>&1
  git -C "$WT" clean -fd >>"$LOGDIR/orbit.log" 2>&1
  git -C "$WT" for-each-ref --format='%(refname:short)' "refs/heads/$ORBIT_BRANCH_PREFIX/" 2>/dev/null \
    | while read -r b; do [ -n "$b" ] && git -C "$WT" branch -D "$b" >>"$LOGDIR/orbit.log" 2>&1 || true; done
}

# --- worker liveness (extracted verbatim by tests/test_slot_loop.py) ---
# $STATE/workers/<slot>.pid is this slot's answer to "is task <tid> actually
# still in flight?" — and it is the ONLY answer the reaper trusts
# (ledger.py `_slot_worker_matches`). It cannot use the ledger entry's own
# worker_pid, because that is this LOOP's pid: alive across many tasks, so a
# reaper trusting it would never escalate anything while the loop runs. Hence:
# written the instant a claim is won (a claimed task with no pid file looks
# abandoned and gets escalated early) and removed the instant the task is done.
# A file left behind by a killed loop is not a hazard: the reaper believes one
# only while its pid is still alive, so a dead slot's file reads as "not live".
write_worker_pid() {
  local json
  json="$(python3 - "$$" "$1" <<'PYEOF'
import json, sys
from datetime import datetime, timezone
print(json.dumps({"pid": int(sys.argv[1]), "tid": sys.argv[2],
                  "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}))
PYEOF
)"
  if [ -n "$json" ]; then
    # tmp+rename: the reaper reads this file at arbitrary moments, and a torn
    # read parses as "not verified live" — i.e. it would escalate a task that
    # is running fine.
    mkdir -p "$(dirname "$WORKER_PID_FILE")" 2>/dev/null || true
    printf '%s\n' "$json" > "$WORKER_PID_FILE.tmp$$" \
      && mv -f "$WORKER_PID_FILE.tmp$$" "$WORKER_PID_FILE"
  else
    logf "WARN: could not write $WORKER_PID_FILE — the reaper may escalate task $1 early"
  fi
}
clear_worker_pid(){ rm -f "$WORKER_PID_FILE" 2>/dev/null || true; }
# Fold the RUNNING CYCLE's identity into the same file, the instant it exists
# ($1 = the cycle's pid). This is the only place an Abort can learn which
# process to kill: before it existed, command_center's Abort had nothing to
# aim at but the pid in this file - which is the LOOP's pid - so aborting one
# task killed the whole slot loop. `cycle_pgid` is written ONLY when the cycle
# really leads its own process group (see run_cycle's session wrapper),
# because an aborter that killpg'd a cycle sharing the loop's group would take
# the loop down exactly as before.
record_cycle_pid() {
  local json
  json="$(python3 - "$WORKER_PID_FILE" "$1" <<'PYEOF'
import json, os, sys, time
path, cpid = sys.argv[1], int(sys.argv[2])
rec = json.load(open(path))
if not isinstance(rec, dict) or not rec.get("pid"):
    raise SystemExit("worker pid file has no pid")
rec["cycle_pid"] = cpid
# The wrapper calls setsid() only after it is exec'd, so the group may not be
# its own yet the instant the shell hands us $!. Poll briefly (2s ceiling; a
# cycle runs for minutes) rather than record a pgid that is still the loop's.
for _ in range(40):
    try:
        if os.getpgid(cpid) == cpid:
            rec["cycle_pgid"] = cpid
            break
    except OSError:
        break          # already gone - nothing to abort, nothing to record
    time.sleep(0.05)
print(json.dumps(rec))
PYEOF
)"
  if [ -n "$json" ]; then
    printf '%s\n' "$json" > "$WORKER_PID_FILE.tmp$$" \
      && mv -f "$WORKER_PID_FILE.tmp$$" "$WORKER_PID_FILE"
  else
    logf "WARN: could not record cycle pid $1 in $WORKER_PID_FILE - Abort will have no cycle to aim at"
  fi
}
# --- end worker liveness ---

# --- loop liveness (extracted verbatim by tests/test_slot_loop.py) ---
# "Slot <N> of this workspace is OWNED" - written once at startup, removed by
# the EXIT trap, and true for the loop's WHOLE life, idle naps included. The
# per-task file above cannot answer that: it is absent between cycles, which
# is why the launcher used to fall back to guessing from orbit.log + STOP
# ("this workspace has run before and is not stopped, so it must be running").
# That guess is what let Start put a SECOND process on a paused-but-live slot.
# A file left behind by a hard-killed loop is not a hazard: every reader
# checks the recorded pid for liveness first, so a dead loop's file reads as
# an unowned slot.
write_loop_pid() {
  local json
  json="$(python3 - "$$" "$SLOT" <<'PYEOF'
import json, sys
from datetime import datetime, timezone
print(json.dumps({"pid": int(sys.argv[1]), "slot": sys.argv[2],
                  "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}))
PYEOF
)"
  if [ -n "$json" ]; then
    mkdir -p "$(dirname "$LOOP_PID_FILE")" 2>/dev/null || true
    # tmp+rename for the same reason as the worker file: readers poll it at
    # arbitrary moments and a torn read parses as "slot not owned".
    printf '%s\n' "$json" > "$LOOP_PID_FILE.tmp$$" \
      && mv -f "$LOOP_PID_FILE.tmp$$" "$LOOP_PID_FILE"
  else
    logf "WARN: could not write $LOOP_PID_FILE - the launcher may offer to start a second loop on slot $SLOT"
  fi
}
clear_loop_pid(){ rm -f "$LOOP_PID_FILE" 2>/dev/null || true; }
# --- end loop liveness ---

# Per-slot task markers + this slot's liveness, all dropped together: the task
# is no longer in flight, so nothing may still name it. Every path out of
# one_iteration that got as far as a claim ends here.
release_task(){ rm -f "$MARKER" "$CAND_MARKER" 2>/dev/null || true; clear_worker_pid; }

# The cycle's cost, parsed out of its stream-json log and handed to spend.py —
# the ONE writer of .spend-<date>. Its read-modify-write is lock-guarded, so two
# slots finishing in the same instant cannot lose one another's update, which the
# unlocked inline python this replaced could. spend.py prints the PER-CYCLE
# cost back, not the running total, so the caller's "cycle cost $X" line is
# unchanged. On-disk format unchanged too (a bare 4-decimal float).
record_spend() {
  local cost printed
  cost="$(python3 - "$1" <<'PYEOF'
import json, sys
cost = 0.0
try:
    for ln in open(sys.argv[1], errors="ignore"):
        ln = ln.strip()
        if ln.startswith("{") and '"type"' in ln and 'result' in ln:
            try: cost = float(json.loads(ln).get("total_cost_usd", 0) or 0)
            except Exception: pass
except Exception: pass
print(f"{cost:.4f}")
PYEOF
)"
  [ -n "$cost" ] || cost="0.0000"
  printed="$(python3 "$ENGINE/spend.py" add "$STATE" "$(today)" "$cost" 2>>"$LOGDIR/orbit.log")" || printed=""
  if [ -n "$printed" ]; then
    printf '%s\n' "$printed"
  else
    logf "WARN: spend.py did not record \$$cost for $1 — today's total is short by that much (see above)"
    printf '%s (unrecorded)\n' "$cost"
  fi
}
todays_spend(){ cat "$STATE/.spend-$(today)" 2>/dev/null || echo 0; }

# One atomic increment of .count-<date>, checked against the daily cap inside
# the same critical section (spend.py bump-count), so N slots can never push the
# count past the cap between one's check and another's write. Return 0 = counted,
# 1 = do not start another cycle today. Every non-zero exit maps to 1, not just
# the at-cap 3: on a lock timeout (4) or a bad argument (2) the count did NOT go
# up, and idling is the safe direction — the alternative is burning uncounted
# cycles past the cap.
count_cycle() {
  local new rc
  new="$(python3 "$ENGINE/spend.py" bump-count "$STATE" "$(today)" --cap "$MAX_TASKS_PER_DAY" 2>>"$LOGDIR/orbit.log")"
  rc=$?
  case "$rc" in
    0) return 0 ;;
    3) log "daily cap $MAX_TASKS_PER_DAY reached (count ${new:-?}) — idling."; return 1 ;;
    *) log "WARN: daily count not recorded (spend.py rc=$rc) — idling rather than risk an uncounted cycle."; return 1 ;;
  esac
}

# --- auto-feed refill threshold (extracted verbatim by tests/test_autopromote.py) ---
# Counts what the loop could actually PICK, so it must apply the same three
# filters as pickable_ids (worked · skipped · workable category, `forced`
# bypassing the last) - a count that includes tasks no slot may claim would make
# auto-feed decide the queue is topped up while every slot sits idle.
count_pickable() {
  python3 - "$STATE/queue.json" "$ENGINE/ledger.py" "$STATE/skips.txt" "${ORBIT_WORKABLE:-}" <<'PYEOF'
import json, os, subprocess, sys
qf, ledger, sk = sys.argv[1], sys.argv[2], sys.argv[3]
workable = set(c for c in sys.argv[4].split(",") if c) if len(sys.argv) > 4 else set()
q = json.load(open(qf)) if os.path.exists(qf) else {}
tasks = q.get("queue", q.get("tasks", q)) if isinstance(q, dict) else q
try: worked = set(subprocess.check_output(["python3", ledger, "worked-ids"], text=True).split())
except Exception: worked = set()
skips = set(l.strip() for l in open(sk)) if os.path.exists(sk) else set()
n, seen = 0, set()
for t in tasks:
    tid = str(t.get("id"))
    if tid in worked or tid in skips or tid in seen:
        continue
    if workable and str(t.get("category", "")) not in workable and not t.get("forced"):
        continue
    seen.add(tid)
    n += 1
print(n)
PYEOF
}

# ON iff auto-feed is enabled (AUTO_PROMOTE marker present) AND the queue has
# fewer than SLOTS pickable tasks - one task per slot, so no slot idles while
# there is safe work to promote. SLOTS is 1 unless config `concurrency` says
# otherwise, which makes this exactly the old "== 0" trigger for every
# single-slot deployment.
should_autofeed() {
  [ -f "$STATE/AUTO_PROMOTE" ] && [ "$(count_pickable)" -lt "$SLOTS" ]
}
# --- end auto-feed refill threshold ---

# --- claim-then-run candidate walk (extracted verbatim by tests/test_slot_loop.py) ---
# The WRAPPER, not the agent, decides which task this cycle works, and it does so
# by winning an exclusive ledger claim BEFORE spending a cycle. Two reasons:
#   1. Two slots reading the same queue would otherwise both hand the same task to
#      an agent, and the loser's entire cycle is wasted on a duplicate. The claim
#      is the arbiter (ledger.py claim --exclusive: exactly one winner, decided
#      under a real flock) and it costs milliseconds instead of an opus hour.
#   2. The id is then known BEFORE the cycle runs, which is what lets the log and
#      patch be named after the task, the dashboard name it immediately instead of
#      guessing, and the worker pid file say what this slot is actually working.

# Every pickable task, best candidate first. This is the wrapper's version of the
# pick the AGENT used to make for itself, so it has to apply all three of the
# clauses the skill applied, or the wrapper hands the agent work it will only
# refuse (a burnt cycle, then an escalation, every tick):
#   · not already worked (ledger) and not skipped (skips.txt);
#   · `category` in the config's workable_categories — EXCEPT when the task is
#     `forced`, which is precisely the operator saying "yes, this excluded
#     category, do it anyway";
#   · highest priority first (a stable sort, so queue order breaks ties).
# An EMPTY ORBIT_WORKABLE means no workable_categories key in config: filter
# nothing, because a deployment without the key must keep behaving as it does
# today rather than starve.
pickable_ids() {
  python3 - "$STATE/queue.json" "$ENGINE/ledger.py" "$STATE/skips.txt" "${ORBIT_WORKABLE:-}" <<'PYEOF'
import json, os, subprocess, sys
qf, ledger, sk = sys.argv[1], sys.argv[2], sys.argv[3]
workable = set(c for c in sys.argv[4].split(",") if c) if len(sys.argv) > 4 else set()
q = json.load(open(qf)) if os.path.exists(qf) else {}
tasks = q.get("queue", q.get("tasks", q)) if isinstance(q, dict) else q
try: worked = set(subprocess.check_output(["python3", ledger, "worked-ids"], text=True).split())
except Exception: worked = set()
skips = set(l.strip() for l in open(sk)) if os.path.exists(sk) else set()
RANK = {"high": 0, "medium": 1, "low": 2}
cands, rank, seen = [], {}, set()
for t in tasks:
    tid = str(t.get("id"))
    if tid in worked or tid in skips or tid in seen:
        continue
    if workable and str(t.get("category", "")) not in workable and not t.get("forced"):
        continue
    seen.add(tid)
    cands.append(tid)
    rank[tid] = RANK.get(str(t.get("priority", "medium")), RANK["medium"])
for tid in sorted(cands, key=lambda c: rank[c]):
    print(tid)
PYEOF
}

# The title to record on the ledger entry (it is what the dashboard shows). Queue
# entries carry the whole contract in `task` (title + WHY + acceptance criteria),
# so the title is its first line — the same line the agent used to claim with.
# Falls back to the id: claiming with an empty title would blank the title on an
# entry being reclaimed.
task_title() {
  python3 - "$STATE/queue.json" "$1" <<'PYEOF'
import json, os, sys
qf, want = sys.argv[1], sys.argv[2]
q = json.load(open(qf)) if os.path.exists(qf) else {}
tasks = q.get("queue", q.get("tasks", q)) if isinstance(q, dict) else q
title = ""
for t in tasks:
    if str(t.get("id")) == want:
        title = (t.get("title") or (t.get("task") or "").strip().split("\n")[0]).strip()
        break
print(title[:200] or want)
PYEOF
}

# Win ONE task out of the candidate list ($1, one id per line).
# stdout carries ONLY the id we won — the caller reads it with $( ), so a stray
# log line in here would be spliced into the task id (that is what logf is for).
# Exit: 0 won (id on stdout) · 1 every candidate was legitimately lost to another
# claimant · 2 the ledger lock was busy (transient: this tick does nothing, the
# next one retries) · 3 nothing was won and at least one claim FAILED unexpectedly
# — kept apart from 1 so a broken ledger reads as a broken ledger in the log
# instead of hiding behind "another slot has it".
claim_first() {
  local tid rc errored=0
  while IFS= read -r tid; do
    [ -n "$tid" ] || continue
    # </dev/null: the loop is reading the candidate list on stdin, and a child
    # that read from it would swallow the remaining candidates.
    python3 "$ENGINE/ledger.py" claim --exclusive "$tid" "$(task_title "$tid")" \
        --pid "$$" --slot "$SLOT" </dev/null >>"$LOGDIR/orbit.log" 2>&1
    rc=$?
    case "$rc" in
      0) printf '%s\n' "$tid"; return 0 ;;
      5) logf "task $tid already claimed elsewhere — trying the next candidate" ;;
      6) logf "ledger lock busy while claiming $tid — no claim this tick"; return 2 ;;
      *) logf "WARN: claiming $tid failed (rc=$rc, see above) — trying the next candidate"
         errored=1 ;;
    esac
  done <<< "$1"
  [ "$errored" = "1" ] && return 3
  return 1
}
# --- end claim-then-run candidate walk ---

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

# --- cycle session wrapper (extracted verbatim by tests/test_slot_loop.py) ---
# The cycle runs in its OWN session (hence its own process group), so a signal
# aimed at the cycle can never reach this loop - the C2 bug: Abort killed the
# loop's process group, taking the slot's wrapper down with the task.
#
# Mechanism, and why this one: `setsid(1)` does not exist on macOS (util-linux
# only), and bash's own job control (`set -m`) only creates per-job groups in
# an INTERACTIVE shell, which the loop never is. What is portable everywhere
# orbit already requires python3 is a two-line exec wrapper: it calls
# os.setsid() and then execvp()s the real command, so the process bash records
# as $! IS the cycle's session leader - exec replaces the process image, so no
# extra pid, no extra wait(), no orphan. EPERM from setsid() means the process
# is ALREADY a group leader (an interactive shell with job control started it),
# which is the isolation we wanted anyway, so it is ignored.
CYCLE_SESSION_PY='import os, sys
try:
    os.setsid()
except OSError:
    pass
os.execvp(sys.argv[1], sys.argv[1:])'
# --- end cycle session wrapper ---

run_cycle() {
  local out="$1"; cd "$WT" || return 1
  # The task is already claimed by the wrapper, so the cycle is TOLD which one to
  # work: /orbit-cycle <tid> skips its own pick-and-claim. The no-argument form
  # still exists for a manual `claude /orbit-cycle` run, hence the fallback.
  local prompt="/orbit-cycle"
  [ -n "${2:-}" ] && prompt="/orbit-cycle $2"
  local caff=""; command -v caffeinate >/dev/null 2>&1 && caff="caffeinate -i"
  local mcp_args=()
  [ -n "${MCP_CONFIG:-}" ] && mcp_args=(--mcp-config "$MCP_CONFIG" --strict-mcp-config)
  python3 -c "$CYCLE_SESSION_PY" $caff "$CLAUDE_BIN" --print "$prompt" \
      --permission-mode "$PERM" --settings "${CYCLE_SETTINGS:-$SETTINGS}" --model "$MODEL" \
      --add-dir "$REPO" --add-dir "$ORBIT_HOME" \
      "${mcp_args[@]:+"${mcp_args[@]}"}" \
      --output-format stream-json --verbose >"$out" 2>&1 </dev/null &
  local cpid=$!
  # Tell the pid file which process IS this cycle, before anything can want to
  # abort it (see record_cycle_pid).
  record_cycle_pid "$cpid"
  # Watchdog: hard-kill the cycle if it outruns CYCLE_TIMEOUT.
  # CRITICAL: run_cycle executes inside status="$(one_iteration)", whose stdout is a
  # pipe. If the watchdog (and its `sleep`) inherit that pipe, killing the subshell
  # orphans the `sleep`, which keeps the pipe's write end open — so the command
  # substitution never gets EOF and the whole loop wedges for the full CYCLE_TIMEOUT
  # (~1h). Redirect all of the watchdog's fds off the pipe, and kill its sleep child
  # on teardown so nothing is left holding it.
  # The kill targets the cycle's GROUP (`-$cpid`) first: now that the cycle is
  # its own session leader, the group is exactly the cycle tree - claude, the
  # caffeinate wrapper, every node child - and none of this loop. Killing only
  # $cpid would TERM the caffeinate wrapper and leave claude running (it does
  # not forward signals), so the watchdog would report a kill it did not make.
  # The bare-pid fallback covers the one case where setsid() was refused and
  # $cpid therefore leads no group of its own.
  ( sleep "$CYCLE_TIMEOUT"; { kill -TERM -"$cpid" || kill -TERM "$cpid"; } 2>/dev/null; sleep 10; { kill -KILL -"$cpid" || kill -KILL "$cpid"; } 2>/dev/null ) </dev/null >/dev/null 2>&1 &
  local wpid=$!
  wait "$cpid"; local rc=$?
  pkill -P "$wpid" 2>/dev/null || true   # kill the watchdog's sleep child first
  kill "$wpid" 2>/dev/null || true
  return $rc
}

one_iteration() {
  gates_ready || { echo SKIP; return; }
  # Secrets (spec decision 10): PM tokens live in $AP_HOME/.secrets.env (0600,
  # gitignored). Loaded HERE, per iteration, not once at startup: a token the
  # PM saves in the dashboard's Settings > Secrets panel must reach the NEXT
  # cycle, not wait for someone to restart a KeepAlive service.
  # PARSED line-by-line, never sourced/executed - a value containing shell
  # metacharacters (backticks, $(), ;, |, &, quotes, ...) lands in the
  # environment as an inert literal string instead of running with the loop's
  # privileges. The `case` guard drops interpreter/shell/loader names whose
  # values are code paths (PATH, PYTHONPATH, NODE_OPTIONS, LD_*, ...): the
  # dashboard's writer rejects those too (command_center.py
  # _SECRETS_NAME_DENY), so this is the same list enforced on both ends for
  # hand-edited files. Interactive runs and launchd services see the same
  # environment either way.
  # --- secrets parse loop (extracted verbatim by tests/test_secrets.py) ---
  if [ -f "$AP_HOME/.secrets.env" ]; then
    while IFS= read -r _sline || [ -n "$_sline" ]; do
      case "$_sline" in \#*|"") continue ;; esac
      case "${_sline%%=*}" in
        PATH|IFS|ENV|BASH_ENV|SHELL|HOME|PS4|CDPATH|PYTHONPATH|PYTHONSTARTUP|AP_HOME|AP_STATE|ORBIT_HOME|NODE_OPTIONS|NODE_PATH|PERL5LIB|RUBYOPT|GEM_PATH|CLASSPATH|LD_*|DYLD_*|BASH_*) continue ;;
      esac
      case "$_sline" in *=*) export "${_sline%%=*}=${_sline#*=}" ;; esac
    done < "$AP_HOME/.secrets.env"
  fi
  # --- end secrets parse loop ---
  prepare_worktree || { echo FAIL; return; }
  # --- housekeeping: SLOT 0 ONLY (part 1 of 2) --------------------------------
  # Everything in the two guarded blocks below is repo-WIDE work: one ledger, one
  # backlog, one queue, one knowledge base, one regression sweep. N slots each
  # doing it would mean N concurrent writers and N sweeps per tick for exactly one
  # tick's worth of work, so slot 0 is the housekeeper for the whole loop and the
  # other slots go straight to claiming. At the default concurrency there IS only
  # slot 0, so this is today's behaviour untouched.
  #
  # Reap entries stuck mid-cycle (claim-then-crash in_progress, commit-then-die
  # committed). It is no longer true that "no cycle is running at this point" —
  # another slot may well be mid-cycle — which is exactly why reap is
  # identity-aware now: an entry counts as live only while
  # $STATE/workers/<slot>.pid names a live pid AND that task (see the worker
  # liveness block above and ledger.py's `_slot_worker_matches`). The age bound
  # still guards the window between a claim and its pid file.
  if [ "$SLOT" = "0" ]; then
    local reaped
    reaped="$(python3 "$ENGINE/ledger.py" reap "$((CYCLE_TIMEOUT + 600))" 2>>"$LOGDIR/orbit.log" || true)"
    if [ -n "$reaped" ]; then
      log "reaper: escalated stalled task(s): $reaped"
      notify "🙋 Orbit needs you" "stalled mid-cycle task(s) escalated: $reaped — answer to re-queue" "$DASH"
    fi
    # Auto-close verified no-ops (tasks whose shipped commit is already an ancestor
    # of the base branch) BEFORE regenerating the queue, so they never re-inject or
    # re-escalate into the operator inbox.
    python3 "$ENGINE/autoclose.py" >>"$LOGDIR/orbit.log" 2>&1 || log "WARN: autoclose failed"
  fi
  # MCP seam (spec section 3): empty MCP_CONFIG = no mcp: block = flags omitted.
  # A FAILURE here (malformed mcp: block) must not silently run the cycle blind.
  if ! MCP_CONFIG="$(python3 "$ENGINE/mcp.py" build "$TARGET" 2>>"$LOGDIR/orbit.log")"; then
    log "ERROR: mcp.py build failed for $TARGET — see orbit.log; skipping iteration"
    echo SKIP; return
  fi
  if ! CYCLE_SETTINGS="$(python3 "$ENGINE/mcp.py" settings "$TARGET" 2>>"$LOGDIR/orbit.log")"; then
    log "ERROR: mcp.py settings failed for $TARGET — see orbit.log; skipping iteration"
    echo SKIP; return
  fi
  [ -n "$CYCLE_SETTINGS" ] || CYCLE_SETTINGS="$SETTINGS"
  # --- housekeeping: SLOT 0 ONLY (part 2 of 2) --------------------------------
  # Same reasoning as part 1; kept in the original order so the queue is
  # regenerated (and topped up) before anyone claims from it.
  if [ "$SLOT" = "0" ]; then
    python3 "$ENGINE/knowledge.py" "$TARGET" autorefresh --quiet >>"$LOGDIR/orbit.log" 2>&1 || log "WARN: knowledge autorefresh failed"
    # Regression sweep (Task 27): cursor-triggered replay of every approved taught
    # flow (steps.json-bearing). qa_run.py's own --regression --if-changed posture
    # is silent-exit-0 fast whenever there's nothing to do (no qa: block at all,
    # unchanged trunk/cadence, or a sweep already running) — this line can never
    # break the loop: any nonzero exit (a failed flow, a bad qa.flows_dir, a crash)
    # just logs a WARN and the iteration continues, exactly like every other
    # best-effort hook here. Runs inline; bounded by the sweep deadline (qa_run.py
    # stops LAUNCHING new flows past 2x qa.timeout_seconds of its own elapsed
    # time — a flow already running still gets its full per-flow timeout).
    python3 "$ENGINE/qa_run.py" "$TARGET" --regression --if-changed >>"$LOGDIR/orbit.log" 2>&1 || log "WARN: regression sweep failed"
    python3 "$ENGINE/backlog_to_tasks.py" >>"$LOGDIR/orbit.log" 2>&1 || log "WARN: backlog→queue refresh failed"
    if should_autofeed; then
      log "auto-feed: promoting next safe task"
      python3 "$ENGINE/autopromote.py" >>"$LOGDIR/orbit.log" 2>&1 || log "WARN: autopromote failed"
    fi
  fi
  # --- end housekeeping -------------------------------------------------------

  # Claim BEFORE spending a cycle (see the candidate walk above): the winner of an
  # exclusive ledger claim is this slot's task for this iteration, and nothing else
  # in the loop may work it. Losing every candidate is not a failure — it means the
  # other slots have the queue covered, so this tick idles like an empty queue.
  local candidates; candidates="$(pickable_ids 2>/dev/null)"
  if [ -z "$candidates" ]; then log "no pickable tasks — idling, no cycle spent."; echo EMPTY; return; fi
  local pickable; pickable="$(printf '%s\n' "$candidates" | grep -c '[^[:space:]]' || true)"
  local tid claim_rc
  tid="$(claim_first "$candidates")"; claim_rc=$?
  case "$claim_rc" in
    0) : ;;
    2) log "ledger lock busy — no claim this tick, retrying next tick."; echo EMPTY; return ;;
    3) log "ERROR: every claim attempt failed on $pickable candidate(s) — see orbit.log; no cycle spent."; echo EMPTY; return ;;
    *) log "all $pickable pickable task(s) are claimed elsewhere — idling, no cycle spent."; echo EMPTY; return ;;
  esac
  if [ -z "$tid" ]; then
    log "ERROR: claim reported a win with no task id — refusing to run a cycle blind."; echo EMPTY; return
  fi
  # This slot now owns $tid, so say so IMMEDIATELY, in both places that matter:
  # the pid file the reaper reads (so it does not escalate a task that is running)
  # and the marker the dashboard reads. The marker is authoritative from the start
  # now — the wrapper claimed, so there is nothing provisional left to confirm —
  # but .cycle-candidate keeps being written alongside it because the dashboard
  # still reads it as the fallback identity.
  write_worker_pid "$tid"
  printf '%s\n' "$tid" > "$MARKER"
  printf '%s\n' "$tid" > "$CAND_MARKER"

  # Record the resolved route on the ledger entry (portal route visibility).
  # Degrade-to-log: recording must never fail or delay the cycle.
  python3 "$ENGINE/route_record.py" "$tid" >>"$LOGDIR/orbit.log" 2>&1 || true

  # Log and patch are named after the TASK (plus a start stamp), not just the
  # clock: with several slots writing into one logs/ dir, "the newest cycle log"
  # is no longer a useful way to find a given task's run, and a task worked twice
  # keeps both attempts.
  local stamp; stamp="$(date '+%Y%m%dT%H%M%S')"
  local safe_tid; safe_tid="$(printf '%s' "$tid" | tr -c 'A-Za-z0-9._-' '_')"
  local cyclelog="$LOGDIR/cycle-$safe_tid-$stamp.log"
  log "cycle start (task $tid, $pickable pickable) → $cyclelog"
  set_idle_reason "cycle-running" 0
  run_cycle "$cyclelog" "$tid"
  log "cycle cost \$$(record_spend "$cyclelog") (today \$$(todays_spend), informational)"

  # $tid is the wrapper's own claim, so it is always set from here down; the
  # `[ -n "$tid" ]` guards below are left as-is (belt and braces, and a smaller
  # diff through the gate logic).
  local ahead; ahead="$(git -C "$WT" rev-list --count "$BASE_REF"..HEAD 2>/dev/null || echo 0)"
  local patch="$DIFFDIR/cycle-$safe_tid-$stamp.patch"
  { git -C "$WT" diff "$BASE_REF"...HEAD 2>/dev/null; git -C "$WT" diff 2>/dev/null; } > "$patch"
  [ -s "$patch" ] || rm -f "$patch"

  if [ "$ahead" -gt 0 ]; then
    local sha branch; sha="$(git -C "$WT" rev-parse --short HEAD 2>/dev/null || echo '?')"
    branch="$ORBIT_BRANCH_PREFIX/task-${tid:-cycle-$(date '+%Y%m%dT%H%M%S')}"
    # Scope-fence gate (opt-in `plan.enforce: "block"`): refuse to push a commit
    # that touches files outside the planner's plan_scope fence. Only exit code
    # 3 — plan file + fence + enforce mode all present/parseable AND the commit
    # strays outside it — blocks the ship; every other outcome (feature off,
    # missing/unparseable plan, in-scope) ships.
    if [ -n "$tid" ]; then
      python3 "$ENGINE/plan_gate.py" "$tid" "$WT" >>"$LOGDIR/orbit.log" 2>&1
      if [ "$?" = "3" ]; then
        log "plan gate: commit touches files outside the plan's scope — escalating instead of shipping (patch kept: ${patch:-none})"
        python3 "$ENGINE/ledger.py" escalate "$tid" "plan gate: commit touches files outside the plan's scope (see reviews/task-$tid-plan-gate.txt)" >>"$LOGDIR/orbit.log" 2>&1 \
          || log "WARN: ledger escalate not recorded for $tid (lifecycle gate or write error — see orbit.log)"
        notify "🛑 Orbit plan gate" "task $tid: commit touches files outside the plan's scope — escalated, nothing pushed" "$DASH"
        release_task
        echo OK; return
      fi
    fi
    # QA gate (engine/qa_check.py): a browser-driving QA agent (opt-in `qa:`
    # config) when present, else the legacy `runtime_check:` behaviour byte-
    # for-byte — either way, observe the change in the RUNNING product before
    # pushing. Exit 3 escalates (contradiction, or a QA `fail` on a `required`
    # category at the bounce cap); QA-only exit 4 bounces the SAME task back
    # to the queue with findings folded in instead. Every other outcome
    # (skipped, unable-to-run, agent error) ships.
    if [ -n "$tid" ]; then
      python3 "$ENGINE/qa_check.py" "$tid" "$WT" "$patch" >>"$LOGDIR/orbit.log" 2>&1
      local qa_rc="$?"
      if [ "$qa_rc" = "3" ]; then
        log "QA gate: contract contradicted or bounce cap reached — escalating instead of shipping (patch kept: ${patch:-none})"
        python3 "$ENGINE/ledger.py" escalate "$tid" "QA gate: contract contradicted or bounce cap reached — see reviews/task-$tid-runtime.md" >>"$LOGDIR/orbit.log" 2>&1 \
          || log "WARN: ledger escalate not recorded for $tid (lifecycle gate or write error — see orbit.log)"
        notify "🛑 Orbit QA gate" "task $tid: contract contradicted or bounce cap reached — escalated, nothing pushed" "$DASH"
        release_task
        echo OK; return
      elif [ "$qa_rc" = "4" ]; then
        # dev⇄QA bounce: qa_check.py already re-filed the SAME task (status
        # queued, findings folded in) and cleared its ledger entry — nothing
        # to push, nothing to escalate, and qa_check.py already notified.
        log "QA bounced task $tid back to the queue"
        release_task
        echo OK; return
      fi
    fi
    log "cycle committed $ahead change(s) ($sha) — pushing $branch"
    if ! git -C "$WT" push origin "HEAD:refs/heads/$branch" >>"$LOGDIR/orbit.log" 2>&1; then
      branch="${branch}-$(date '+%Y%m%dT%H%M%S')"
      git -C "$WT" push origin "HEAD:refs/heads/$branch" >>"$LOGDIR/orbit.log" 2>&1 || branch=""
    fi
    if [ -n "$branch" ]; then
      log "pushed origin/$branch ($sha) — review, then Merge/Reject"
      [ -n "$tid" ] && { python3 "$ENGINE/ledger.py" pushed "$tid" "origin/$branch" >>"$LOGDIR/orbit.log" 2>&1 \
          || log "WARN: ledger pushed not recorded for $tid (lifecycle gate or write error — see orbit.log)"
        python3 "$ENGINE/review_packet.py" "$tid" "$WT" "$branch" "$ORBIT_BASE_BRANCH" >>"$LOGDIR/orbit.log" 2>&1 || true; }
      raise_pr "$tid" "$branch"
      notify "🔧 Orbit shipped" "task ${tid:-?} → $branch — review packet is ready; merge or reject" "$DASH"
    else
      log "WARN: push failed twice — patch kept: ${patch:-none}"
      # A committed-but-unpushed ship is otherwise invisible (no gate surfaces
      # 'committed') — escalate so the operator sees it now, not via the reaper.
      if [ -n "$tid" ]; then
        python3 "$ENGINE/ledger.py" escalate "$tid" "wrapper: push failed twice — commit exists only in the worktree; patch kept: ${patch:-none}" >>"$LOGDIR/orbit.log" 2>&1 \
          || log "WARN: ledger escalate not recorded for $tid (lifecycle gate or write error — see orbit.log)"
        notify "🙋 Orbit needs you" "task $tid: push failed twice — the ship never reached origin (patch kept)" "$DASH"
      fi
    fi
  else
    log "cycle produced no commit (no-op / escalation)."
    # The safety net for a cycle that produced nothing: put the task on a human
    # gate instead of leaving it mid-flight. Keyed on the ledger STATE, because
    # the old guard ("this id is not in worked-ids") can no longer ever be true —
    # the WRAPPER's own claim is what put it there. That made this net dead, and
    # every no-op cycle — a watchdog kill, an agent that refused the task, a run
    # cut short by a usage limit — left the task in_progress and invisible until
    # the reaper noticed an hour later. States that mean "already handled":
    # committed/pushed (shipped), merged/rejected (reviewed), escalated (already
    # on the gate), queued (something re-filed it deliberately).
    local lstate; lstate="$(python3 "$ENGINE/ledger.py" state "$tid" 2>/dev/null || true)"
    case "$lstate" in
      committed|pushed|merged|rejected|escalated|queued) : ;;
      *)
        python3 "$ENGINE/ledger.py" escalate "$tid" "wrapper: no commit (gate fail / too large / incomplete)" >>"$LOGDIR/orbit.log" 2>&1 \
          || log "WARN: ledger escalate not recorded for $tid (lifecycle gate or write error — see orbit.log)" ;;
    esac
    # An escalated cycle is a stalled human gate — say so where the human is.
    if [ -n "$tid" ] && [ "$(python3 "$ENGINE/ledger.py" state "$tid" 2>/dev/null)" = "escalated" ]; then
      notify "🙋 Orbit needs you" "task $tid escalated — answer it to unblock the loop" "$DASH"
    fi
  fi
  release_task

  if grep -qiE "usage limit|rate limit|session limit|429|quota" "$cyclelog" 2>/dev/null \
     && ! grep -q '"type":"result"' "$cyclelog" 2>/dev/null; then echo LIMIT; return; fi
  echo OK
}

# Marker the dashboard reads to explain WHY the loop is idle (backoff / idle reason + when it resumes).
# Format: "<reason> <resume-epoch>". Defined BEFORE the loop so the STOP and daily-cap branches can set
# it too — otherwise a paused/capped loop keeps a stale marker and the dashboard shows "between-cycles"
# (or a false "next pick ~Ns") while the loop is actually STOPPED and picking nothing.
set_idle_reason(){ echo "$1 $(( $(date +%s) + ${2:-0} ))" > "$IDLE_MARKER"; }
# Backstop for the worker pid file: release_task drops it on every path through
# one_iteration, and this catches the ones the shell itself takes (an unbound
# variable, a `set -u` abort, any exit that is not the while-true loop).
# DELIBERATELY not trapping TERM/INT: bash runs a trap only after the current
# FOREGROUND command finishes, so trapping them would make `kill`/launchd stop
# wait out an in-progress `sleep 600` (or a whole cycle) instead of ending the
# process at once, the way it does today. And a leaked pid file is not dangerous:
# the reaper only believes one whose pid is still ALIVE (ledger.py
# `_slot_worker_matches`), so a dead slot's file reads as "not live" and its task
# gets escalated exactly as it should.
trap 'clear_worker_pid; clear_loop_pid' EXIT
log "==== orbit up (pid $$) slot $SLOT of $SLOTS target=$TARGET model=$MODEL base=$ORBIT_BASE_BRANCH ===="
# Claim the slot for this process's whole lifetime (see the loop liveness
# block): from here on, anything asking "is slot $SLOT taken?" gets a
# definitive answer instead of a heuristic.
write_loop_pid
# Said once per start, because nothing else will ever mention it again: the
# pre-slot worktree is now orphaned and no loop will touch it, so an operator
# reading the log knows both that it is safe to delete and that it is theirs to
# delete. (Deleting it here would be this script destroying a directory it no
# longer manages — and one that may hold the only copy of an unpushed commit.)
if [ "$WT" != "${REPO}-orbit-worktree" ] && [ -d "${REPO}-orbit-worktree" ]; then
  log "note: ${REPO}-orbit-worktree is the pre-slot worktree and is no longer used — remove it with 'git -C $REPO worktree remove --force ${REPO}-orbit-worktree' once you have checked it holds nothing you want"
fi
if [ "$ONE_SHOT" = "1" ]; then
  # STOP is honoured here too. It is the workspace kill switch - "claim
  # nothing here" - and a mode that claimed anyway would be the single path in
  # the engine that walks past it. A smoke that wants its iteration to happen
  # disarms STOP explicitly (rm -f "$AP_STATE/STOP"), which is deliberate and
  # visible, instead of relying on a flag that silently overrides it.
  if [ -f "$STOP_FILE" ]; then
    log "STOP present - ORBIT_ONE_SHOT claims nothing (remove $STOP_FILE first)."
    set_idle_reason "stopped" 60
    exit 0
  fi
  # The DAILY CAP is still bypassed on purpose - that paces an unattended
  # deployment, it is not a kill switch, and a one-shot run wants THIS
  # iteration. Bookkeeping (count_cycle) still runs on OK/FAIL, matching the
  # loop's own accounting, so a smoke's spend/count files come out exactly as
  # if the normal loop had ticked once.
  status="$(one_iteration)"; status="${status##*$'\n'}"
  case "$status" in OK|FAIL) count_cycle >/dev/null 2>&1 || true ;; esac
  log "ORBIT_ONE_SHOT set - exiting after one iteration (status=$status)."
  exit 0
fi
while true; do
  # STOP is checked HERE and nowhere else: it stops new CLAIMS, never an in-flight
  # cycle (killing a cycle mid-commit is how tasks end up in limbo).
  [ -f "$STOP_FILE" ] && { log "STOP present — idling."; set_idle_reason "stopped" 60; sleep 60; continue; }
  # Daily cap, read through spend.py (the same lock-guarded accessor that writes
  # it) so a slot can never read a half-written count. Unparseable/unavailable
  # counts as 0, exactly like the plain `cat` this replaced.
  count="$(python3 "$ENGINE/spend.py" count "$STATE" "$(today)" 2>>"$LOGDIR/orbit.log" || echo 0)"
  case "$count" in ''|*[!0-9]*) count=0 ;; esac
  if [ "$count" -ge "$MAX_TASKS_PER_DAY" ]; then log "daily cap $MAX_TASKS_PER_DAY reached — idling."; set_idle_reason "daily-cap" 600; sleep 600; continue; fi
  # one_iteration's stdout carries BOTH the tee'd log lines and the final status
  # word — dispatch on the LAST line only. (Matching the whole capture meant
  # OK/EMPTY/SKIP/LIMIT never matched once anything was logged: the daily count
  # never incremented and usage-limit backoff never fired.)
  status="$(one_iteration)"; status="${status##*$'\n'}"
  case "$status" in
    # A spent cycle is counted the same as before (OK and FAIL both count, EMPTY/
    # SKIP/LIMIT do not) — only the write is now atomic and cap-checked in one
    # critical section, so N slots finishing together cannot walk past the cap.
    OK)    if count_cycle; then set_idle_reason "between-cycles" "$INTERVAL"; sleep "$INTERVAL"
           else set_idle_reason "daily-cap" 600; sleep 600; fi ;;
    FAIL)  if count_cycle; then set_idle_reason "recovering" 120; sleep 120
           else set_idle_reason "daily-cap" 600; sleep 600; fi ;;
    EMPTY) set_idle_reason "queue-empty" "$EMPTY_BACKOFF"; sleep "$EMPTY_BACKOFF" ;;
    SKIP)  set_idle_reason "waiting-for-services" 300; sleep 300 ;;
    LIMIT) log "usage limit — backing off ${LIMIT_BACKOFF}s."; set_idle_reason "usage-limit" "$LIMIT_BACKOFF"; sleep "$LIMIT_BACKOFF" ;;
    *)     set_idle_reason "between-cycles" "$INTERVAL"; sleep "$INTERVAL" ;;
  esac
done
