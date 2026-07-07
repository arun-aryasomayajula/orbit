# Orbit architecture

## The boundary
One line splits everything: **engine (generic) vs profile (per-repo)**. The engine never names a
project; all project knowledge lives in the target's `.autopilot/`. metaql is just the first consumer.

## The cycle (one iteration of the loop)
`engine/run.sh` is the resilient outer loop (worktree-isolated, survives sleep/crash/usage-limit via
the service's KeepAlive + backoff). Each iteration:

1. **Preflight** — `config.py needs` lists the gate dependencies (postgres, docker, …); skip the cycle if any is down.
2. **Refresh queue** — `backlog_to_tasks.py` reads `.autopilot/backlog.yaml` (+ any opt-in adapters in `sources:`) → `state/queue.json`.
3. **Run one task** — `claude -p /orbit-cycle` (opus orchestrator) in a detached worktree at `origin/<base_branch>`:
   - pick the top workable task (ledger excludes worked ids);
   - **route** via `router.yaml`: category → maker `{agent, model, effort}`, `skill` (invoked + briefed), `tracks` (category ∪ path-triggered);
   - **build ⇄ check** (max 5): maker (builder/qa-writer/doc-writer) then checker (runs `config.gates`);
   - **verify-spec**: verifier judges diff-vs-intent; must CONFORM;
   - **atomic commit** on the detached HEAD; write a review packet.
4. **Push** — the wrapper (not the agent) pushes the commit to `origin/<branch_prefix>/task-<id>`, records it in the ledger, notifies.

## Model tiering (grounded in evidence, not vibes)
The router assigns opus to hard-judgment categories (bug/feature/refactor/security) and sonnet to
directed work (docs/deps/testing) where a smaller model is at parity — cheaper without quality loss.
`verification-before-completion` is mandatory on every task: execute the gate, show real output.

## Skills vs tracks (no duplication)
- **Skills** carry *general method* — invoked by the orchestrator (it holds the Skill tool; the
  restricted-tool makers don't), folded into the maker brief. e.g. bug→systematic-debugging,
  testing→test-driven-development, refactor→simplify, docs→write-docs, security→security-review.
- **Tracks** carry *repo-specific facts* — gotchas, invariants, pattern-file pointers no general
  skill knows. The engine ships generic templates; a target fills them in under `.autopilot/tracks/`.

## State
All runtime state lives in the target's `.autopilot/state/` (gitignored): `ledger.json` (worked
record), `queue.json` (per-cycle, read-only to the agent), `STATE.md` (cross-cycle lessons),
`reviews/` (packets), `diffs/` (backup patches), `logs/`, `NEEDS_YOU.md` (escalations). A repo thus
fully describes its own autopilot — clone it and the state comes along (or stays local, gitignored).

## Components
| path | role |
|------|------|
| `engine/run.sh` | outer loop wrapper |
| `engine/config.py` | reads `.autopilot/config.yaml`, emits loop env, answers gates/needs, validates |
| `engine/doctor.py` | read-only wiring validator + routing dry-run |
| `engine/ledger.py` | worked-task record |
| `engine/backlog_to_tasks.py` | backlog + adapters → queue.json |
| `engine/{review_packet,notify,autopromote,backlog_lint,command_center}.py` | packets, notifications, auto-feed, lint gate, dashboard |
| `agents/*.md` | builder, checker, verifier, qa-writer, doc-writer (generic) |
| `skills/orbit-cycle.md` | the orchestrator command |
| `router/router.yaml` | default routing (category → maker/model/skill/tracks) |
| `tracks/*.md` | generic track templates (+ `examples/` filled-in references) |
| `adapters/*_to_backlog.py` | opt-in task sources (foundry, logwatch, qa) |
| `install/` | init scaffolding + launchd/systemd service |
