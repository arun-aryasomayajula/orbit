#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# refresh_sources.sh — top up the task pipeline from all sources.
#
# Run PERIODICALLY (e.g. weekly), NOT per-cycle. The per-cycle loop already
# re-runs the cheap converter (backlog_to_tasks.py) every cycle; THIS script is
# for the expensive/occasional sources:
#
#   1. foundry score --tasks   (maturity/coding tasks)   — EXPENSIVE LLM run,
#      opt-in via AP_REFRESH_FOUNDRY=1, refreshes .foundry/tasks.json.
#   2. logwatch_to_backlog.py  (prod-log bug signatures → proposed bug tasks)
#   3. backlog_to_tasks.py     (rebuild queue.json = foundry + backlog allow)
#
# /backlog-research (web/competitor/user research → proposed tasks) is run
# separately/by hand inside Claude — it's interactive, not part of this cron.
#
# Schedule (launchd) example — run Mondays 09:00:
#   cp com.arun.metaql-autopilot.plist → a refresh plist with
#   ProgramArguments = [/bin/bash, <abs>/refresh_sources.sh], StartCalendarInterval
#   {Weekday:1, Hour:9, Minute:0}, and EnvironmentVariables AP_REFRESH_FOUNDRY=1.
#   Then: launchctl load ~/Library/LaunchAgents/<that>.plist
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

AP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$AP/../.." && pwd)"
LOG="$AP/logs/refresh.log"
mkdir -p "$AP/logs"

# Match run.sh's environment so foundry + python resolve (Zscaler cert, PATH).
export NODE_EXTRA_CA_CERTS="/Users/arunaryasomayajula/.certs/zscaler-root-ca.pem"
export PATH="/Users/arunaryasomayajula/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }
log(){ echo "[$(ts)] $*" | tee -a "$LOG"; }

log "==== refresh_sources start (foundry=${AP_REFRESH_FOUNDRY:-0}) ===="

# 1. Foundry maturity tasks — expensive, opt-in. Regenerates .foundry/tasks.json,
#    then folds the new tasks INTO backlog.yaml (safe → allow/queued, else human/proposed).
if [ "${AP_REFRESH_FOUNDRY:-0}" = "1" ]; then
  if command -v foundry >/dev/null 2>&1; then
    log "running 'foundry score --tasks' (this is the costly step)…"
    ( cd "$REPO" && foundry score --tasks --no-publish ) >>"$LOG" 2>&1 \
      && log "foundry score --tasks done" \
      || log "WARN: foundry score --tasks failed (see $LOG)"
    log "folding foundry maturity tasks → backlog…"
    python3 "$AP/foundry_to_backlog.py" >>"$LOG" 2>&1 && log "foundry fold done" \
      || log "WARN: foundry fold failed"
  else
    log "WARN: foundry not on PATH — skipping maturity refresh"
  fi
else
  log "skipping foundry (set AP_REFRESH_FOUNDRY=1 to refresh maturity tasks — costly)"
fi

# 2. Production-log bugs → proposed tasks.
log "ingesting logwatch signatures → backlog…"
python3 "$AP/logwatch_to_backlog.py" >>"$LOG" 2>&1 && log "logwatch ingest done" \
  || log "WARN: logwatch ingest failed"

# 3. UI bugs + UX feedback (gstack QA/design-review via /browse). The cycle is
#    opt-in (needs a live target + browser); the feeder always runs so any new
#    findings in ops/qa/SEEN.md get filed.
QA="$REPO/ops/qa"
if [ "${AP_REFRESH_QA:-0}" = "1" ]; then
  if command -v claude >/dev/null 2>&1; then
    log "running '/qa-cycle' over ${AP_QA_URL:-http://localhost:3000}…"
    ( cd "$REPO" && claude -p "/qa-cycle ${AP_QA_URL:-}" ) >>"$LOG" 2>&1 \
      && log "qa-cycle done" || log "WARN: qa-cycle failed (target down / browser?)"
  else
    log "WARN: claude not on PATH — skipping qa-cycle"
  fi
else
  log "skipping qa-cycle (set AP_REFRESH_QA=1 + AP_QA_URL to run the UI sweep)"
fi
log "ingesting QA findings → backlog…"
python3 "$QA/qa_to_backlog.py" >>"$LOG" 2>&1 && log "QA ingest done" \
  || log "WARN: QA ingest failed"

# 4. Rebuild the loop queue from all sources.
log "rebuilding queue.json…"
python3 "$AP/backlog_to_tasks.py" >>"$LOG" 2>&1 && log "queue rebuilt" \
  || log "WARN: queue rebuild failed"

log "==== refresh_sources done ===="
