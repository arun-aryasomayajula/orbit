# Track: async-backend
Loaded when: the task changes backend code on async paths — `async def`,
dispatcher/lane_runners/executors, `backend/routes/`, caches, outbound calls.
CLAUDE.md's "Coding Guardrails" (async/event-loop, timeouts) already apply —
this track holds what CLAUDE.md does NOT.

## Hard rules (repo-specific — violations have shipped bugs or burned cycles)
- `get_singleflight_lock` returns a SHARED `asyncio.Lock` — callers must not
  assume a per-key/per-caller lock; check the keying before reusing it.
- The backend has NO hot reload and runs on port 8006 (CLAUDE.md's 8002 is
  stale). After Python edits it must be restarted to take effect; kill via
  `lsof -ti :8006` — NEVER `pkill python` (kills unrelated processes).
- Mixed-engine dispatch (`MixedEngineFunnelDispatcher`): lanes run in parallel
  with per-lane timeouts and failure isolation — one lane's error must never
  fail the other. The Trino lane is cached (`trino_funnel_cache`); Druid is
  never cached. Preserve both properties in any change.
- The Druid executor's own timeout must be ≥ the caller's per-lane budget —
  mismatched timeouts produce confusing partial failures.
- 502 error envelope from Druid is `{error, message, upstream_status}` —
  callers parse it with the shared `extractErrorMessage` shape; keep it stable.

## Playbook (distilled patterns that fit this repo)
- Stay fully sync or fully async within one call path — mixing hides blocking.
- Prefer `gather(..., return_exceptions=True)` when partial failure is
  tolerable (lane isolation); otherwise let the failure propagate loudly.
- On timeout of a created task: cancel it AND await it (CancelledError
  cleanup), remembering `wait_for` never kills a `to_thread` worker.
- Pattern files: `backend/funnelhub/dispatcher.py`, `backend/funnelhub/lane_runners.py`,
  `backend/trino_executor.py`, `backend/services/druid_sql_executor.py`.

## Learned here (cycle-appended, newest last — one line each, with date)
- 2026-07: (seed) the shared-singleflight-lock gotcha above was caught by a
  builder mid-task — verify lock granularity whenever caching is involved.
