# Orbit architecture

## The boundary
One line splits everything: **engine (generic) vs profile (per-repo)**. The engine never names a
project; all project knowledge lives in the target's `.autopilot/`. metaql is just the first consumer.

## The second boundary: machines propose, humans dispose
Every way work ENTERS Orbit (intake survey, signal adapters, epic decomposition) may only
produce `status: proposed` + `autopilot: human` tasks — promotion to `queued` is always a
human act (dashboard/CLI). Every way work LEAVES Orbit (branch push, PR creation) stops at
a review artifact — merging is always a human act. The lifecycle phases hang off this rule:

- **intake** (`engine/intake.py` + `/orbit-intake`) — zero-day onboarding: verify gates
  deterministically (no gates → a test-bootstrap proposal, because a loop without a gate
  has no definition of done), agent-fill the tracks, propose an evidence-backed backlog.
- **planning** (`engine/epic_plan.py` + `/orbit-plan`, `/orbit-decompose`) — `category: epic`
  tasks are containers the loop can never pick, even forced. Pipeline:
  plan → spec (`.autopilot/specs/<id>.md`) → **human approves** → decompose → proposed
  children (`epic: <id>`). Stage lives in the task's own `status` field.
- **build** — the cycle (below), unchanged: one task, one commit, one review branch.
- **review** — review packet always; with `pull_requests: "github"` the WRAPPER also runs
  `gh pr create` (agent never holds provider credentials; merge stays manual).
- **operate** — signal adapters fold external evidence (logs, QA runs, scorecards) back
  into proposals each cycle (contract: SETUP.md "signal adapters").

CI and release are deliberately out of scope: nothing here deploys anywhere.

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
The ledger's `state` field moves through an explicit machine — `engine/lifecycle.py`, the single
source of truth for legal transitions (in_progress → committed → pushed → merged|rejected, with
escalated off-ramps; `reverted` is an annotation on merged, never a state). `ledger.py` gates every
state-writing verb through it: illegal transitions exit 3 writing nothing; `--force` overrides and
stamps `forced: true`; `ledger.py can <id> <event>` is the side-effect-free pre-check (used by the
dashboard's rollback before it runs `git revert`). autoclose consults the same machine. Mid-cycle
states can't rot: `ledger.py reap` (run by the wrapper each iteration) escalates entries stuck at
in_progress/committed past the cycle timeout, and a twice-failed push escalates immediately. The
full transition diagram + rationale live in the README's Architecture section.

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
| `engine/ledger.py` | worked-task record (incl. wrapper-opened PR URLs) |
| `engine/lifecycle.py` | the task lifecycle state machine — legal-transition source of truth |
| `engine/backlog_to_tasks.py` | backlog + signal adapters → queue.json |
| `engine/backlog_append.py` | the ONE way engine code adds/edits backlog tasks (comment-preserving) |
| `engine/intake.py` | zero-day onboarding: gate verification + survey → proposed backlog |
| `engine/epic_plan.py` | planning tier: epic → spec → human approval → child tasks |
| `engine/{review_packet,notify,autopromote,backlog_lint,command_center}.py` | packets, notifications, auto-feed, lint gate, dashboard |
| `agents/*.md` | builder, checker, verifier, qa-writer, doc-writer (generic) |
| `skills/orbit-cycle.md` | the orchestrator command |
| `router/router.yaml` | default routing (category → maker/model/skill/tracks) |
| `tracks/*.md` | generic track templates (+ `examples/` filled-in references) |
| `adapters/*_to_backlog.py` | opt-in task sources (foundry, logwatch, qa) |
| `install/` | init scaffolding + launchd/systemd service |
