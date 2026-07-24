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

**Task-level planning (optional, step 2.8)** — when the target's config carries a
`plan:` block, a **planner** subagent runs before the maker, for every task whose
category is in `plan.categories` (absent = every workable category), writing a
justified per-task plan — approach, a `plan_scope` file-scope fence, applicable
standards — to `reviews/task-<id>-plan.md`; the maker/checker/verifier are all
briefed with it, and the maker is told to touch only the fenced `edit`/`create`
paths. The verify-spec step (3.5) always judges a diff outside that fence as
NONCONFORMING once a plan exists; `plan.enforce: "block"` additionally has the
wrapper run `engine/plan_gate.py` — a deterministic scope-fence gate — right
before push (ahead of the QA gate), refusing to ship (escalating instead) any
commit touching a file outside the plan's scope.

**The QA gate (optional, between commit and push)** — right after the cycle
commits, `engine/qa_check.py` decides whether to ship, bounce, or escalate. No
`qa:` block in the target's config → the LEGACY path, byte-for-byte the retired
`runtime_check.py`'s behaviour (`runtime_check:` config, `/orbit-runtime-check`
skill). A `qa:` block → the QA path supersedes it: a browser-driving
`/orbit-qa-check` agent launches (or attaches to) the product via a per-run
Playwright MCP server, drives the flow the acceptance criteria name, and judges
it against `qa.gate_categories`. Exit codes:

| exit | meaning |
|------|---------|
| 0 | ship — pass, unable-to-run, the check is off/not-configured for this category, or an agent/wiring error (never blocks) |
| 3 | escalate — legacy: an observed contradiction on a `required` category; QA: a `fail` verdict on `required` at the bounce cap (or any unexpected error inside the bounce path itself, as a safe fallback) |
| 4 | QA bounce (QA path only) — `fail` on `required`, still under `qa.max_bounces`: the SAME task is re-queued (`status: queued`) with the QA findings folded into its contract (`qa_findings`) for the next pick |

The dev⇄QA loop still respects machines-propose/humans-dispose: a bounce only
ever re-files a task ALREADY on the queue — no new task is created and no human
click is skipped for it. Unrelated bugs the QA agent notices along the way never
move that task's verdict; they're appended to `ops/qa/SEEN.md` and flow through
the ordinary discovery pipeline instead (the `qa` signal adapter turns them into
`status: proposed` tasks for a human to triage), same as any other
adapter-sourced finding.

**The flow library (teach mode, optional, decoupled from the cycle)** — where the QA
gate above proves ONE task's acceptance criteria, `orbit teach` builds a standing,
committed flow map: a human records a browser flow once (Playwright `codegen`), states
its intent + success criteria, and Orbit writes `intent.md` + `steps.spec.ts` +
`meta.json` under `.autopilot/<qa.flows_dir>/<name>/` (indexed in one `FLOWMAP.md`) for a
human to `git add`/commit — the library is human-owned, never machine-authored.
`orbit qa <target> --flow <name>` then replays it independently, any time, via the same
`/orbit-qa-flow` skill and Playwright MCP wiring the QA gate uses, grading the run
against the flow's own intent rather than a task's criteria. It's the supervised-
learning loop in miniature: the human demonstrates once, the machine imitates it
thereafter, and drift never edits the demonstration in place — a UI change that still
honors the intent mines a **heal candidate** under `$AP_STATE/qa/heal/`, gated on human
approval (the dashboard surfaces the pending count; the approval action itself lands
next phase), while a genuine break grades **fail** and feeds `ops/qa/SEEN.md` into the
same triage pipeline any other signal adapter uses. `steps.json` — the normalized,
healable step list — is written by the replay agent itself, only on the first fully
clean pass; `teach` never writes it.

## Model tiering (grounded in evidence, not vibes)
The router assigns opus to hard-judgment categories (bug/feature/refactor/security) and sonnet to
directed work (docs/deps/testing) where a smaller model is at parity — cheaper without quality loss.
`verification-before-completion` is mandatory on every task: execute the gate, show real output.

## Four reference types (no duplication)
- **Skills** carry *general method* — invoked by the orchestrator (it holds the Skill tool; the
  restricted-tool makers don't), folded into the maker brief. e.g. bug→systematic-debugging,
  testing→test-driven-development, refactor→simplify, docs→write-docs, security→security-review.
- **Tracks** carry *repo-specific facts* — gotchas, invariants, pattern-file pointers no general
  skill knows. The engine ships generic templates; a target fills them in under `.autopilot/tracks/`.
- **Goldens** carry *graded exemplars* of good output — what "good" looks like for a category,
  mined from this repo's own ledger (`goldens/LEARNED.md`) alongside the engine's calibration.
- **Knowledge** carries *mapped project structure* — the endpoints→tables map, ER diagram, flows,
  and conventions `orbit knowledge build` surveys once and keeps fresh (`engine/knowledge.py`),
  committed under `.autopilot/knowledge/`. Loaded at cycle step 2.7 (only what a task
  implicates, not the whole base) whenever `knowledge/INDEX.md` exists — zero behaviour change
  otherwise. Staleness is tracked via merge markers, not file mtimes: `orbit knowledge status`
  maps merges landed after the manifest's `generated_at` to the files they touched, and those
  files to the docs the manifest says they feed, so a maker is warned before trusting a doc a
  later merge invalidated. Human-reviewed like code, but code wins — a contradiction is noted,
  not silently trusted.

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
