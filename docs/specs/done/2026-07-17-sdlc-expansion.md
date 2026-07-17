# SDLC expansion — intake, epics, PRs, signals, dashboard (shipped 2026-07-17)

Rationale record. The HOW lives in the code: `engine/intake.py`, `engine/epic_plan.py`,
`engine/backlog_append.py`, `run.sh raise_pr`, `backlog_to_tasks.run_source_adapters`,
and the Epics strip in `cc_shell.html`. This file keeps only the why and the invariants.

## Why

Orbit was repo-agnostic but covered only the build slice of the lifecycle: a human had
to hand-write the backlog (cold start), big work had nowhere to go (the loop refuses
design), review meant raw branches, and the shipped adapters were hard-coded to their
original host repo. This expansion added intake, a planning tier, wrapper-side PR
creation, and a portable signals contract — deliberately **excluding CI, auto-merge,
and release automation** (operator decision: merge and release stay fully manual).

## Invariants (what must stay true)

1. **Machines propose, humans dispose.** Every automated entry point (intake, epic
   decomposition, signal adapters) writes `status: proposed` + `autopilot: human` only
   — `backlog_append.append_tasks` enforces it and ignores status/autopilot from agent
   output. Queueing and merging are always human clicks.
2. **Epics never reach the loop.** `backlog_to_tasks` holds `category: epic`
   unconditionally — even `forced: true`. The children are the workable units, and
   decompose only runs on a spec a human approved.
3. **Provider actions are wrapper-only.** `gh pr create` runs in `run.sh`, never in an
   agent; the agent never holds credentials. PR mode is opt-in (`pull_requests`),
   degrade-to-log — a broken `gh` can never fail a cycle.
4. **backlog.yaml is never YAML-round-tripped by machines.** All programmatic edits go
   through `backlog_append.py` (block append / block-level field flip, comment-
   preserving, idempotent by id). It also owns the `tasks: []` → block-list opening.
5. **The epic stage lives in the task's own `status` field** (proposed → planning →
   spec_ready → approved → decomposing → decomposed); `state/epics.json` is a display
   cache, never truth. The spec ON DISK, not an agent exit code, is plan's success signal.
6. **A repo with no gates gets a test-bootstrap proposal before anything else** — a
   loop without a gate has no definition of done and must not run.
7. **Signal adapters run under an explicit contract** (env AP_HOME/AP_STATE/ORBIT_HOME,
   cwd = repo root, target-local `.autopilot/adapters/` wins over the engine's), and a
   configured source with no adapter warns loudly — a silently dead feed reads as "no
   findings" forever.

## Non-obvious fixes that rode along

- `run.sh` dispatched on the full captured stdout of `one_iteration`, but `log()` tees
  to stdout — `OK/EMPTY/SKIP/LIMIT` never matched, so the daily cap never counted and
  usage-limit backoff never fired. Dispatch is now on the last line only.
- `doctor.py` had a `RATCHET_HOME` NameError on the goldens fallback path (pre-rename
  leftover).
- The shipped adapters resolved paths relative to their own file inside the engine —
  broken for every target repo; now env-based per the contract.
- `command_center.py` embeds a stale fallback `SHELL`; `cc_shell.html` is the live
  dashboard (read per request). Only the file was updated — sync or delete the embedded
  copy if it ever starts serving.
