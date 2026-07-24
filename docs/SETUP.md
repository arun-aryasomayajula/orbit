# Orbit setup — full profile reference

## 1. Install the engine (once per machine)
```bash
git clone <orbit-repo> ~/orbit
# ensure `claude` is on PATH and `python3 -m pip install pyyaml`
```

## 2. Onboard a target repo
```bash
cd <your-project>
~/orbit/install.sh .
```
This scaffolds `.autopilot/`, auto-detects your gates, installs the `/orbit-cycle` command + agents
into `.claude/`, links the `orbit` convenience script, runs `doctor`, and offers to install the service.

## 3. Review the profile (the two things auto-detection can't nail)
Open `.autopilot/config.yaml`:

- **`gates:`** — the most important block. This is how the checker verifies YOUR repo. Auto-detection
  gives a starting point; confirm the commands actually pass locally and add missing ones
  (typecheck, lint). `needs:` lists services that must be up (postgres, docker, redis, …).
- **`base_branch`** — the loop resets its worktree to `origin/<base_branch>` each cycle and forks
  per-task branches from it. It is never pushed to directly.

Other fields (all optional, sensible defaults): `model`, `permission_mode`, `interval_seconds`,
`max_tasks_per_day`, `cycle_timeout_seconds`, `spec` (a standing doc the loop rereads),
`workable_categories`, `sources`, `env.passthrough`, `branch_prefix`, `commit_trailer`,
`pull_requests` (`"github"` = the wrapper opens a PR per ship via `gh`; merge stays manual).
Full field docs: `<orbit>/config/schema.yaml`.

## 3.5. Intake (optional — recommended on an unfamiliar repo)
```bash
orbit intake .        # flags: --no-agent (deterministic only), --dry-run, --timeout N
```
Closes the cold-start gap: executes every gate and reports pass/fail
(`state/intake/gates-report.txt`), injects a test-bootstrap proposal when NO gates exist,
then one headless survey call fills the placeholder tracks with real repo facts and writes
evidence-backed candidate tasks. Everything lands `status: proposed` + `autopilot: human` —
intake produces leads for you to triage on the dashboard, never queued work.

## 4. Fill in tracks (optional but high-value)
`.autopilot/tracks/` seeds from the engine's generic templates. Replace the placeholder lines with
YOUR repo's real gotchas and pattern-file pointers (see `<orbit>/tracks/TEMPLATE.md` and the
filled-in `<orbit>/tracks/examples/`). Tracks are what make the maker write code that fits your repo.

## 5. Add tasks
`.autopilot/backlog.yaml` — each task:
```yaml
tasks:
  - id: fix-null-grain-crash
    title: "int(None) crashes the lane on an empty stage"
    category: bug            # bug|feature|refactor|code_quality|testing|documentation|dependencies|developer_experience|security|...
    priority: high           # high|medium|low
    status: queued           # queued (pickable) | proposed (needs promotion) | done
    autopilot: allow         # allow (auto) | human (never auto-ship) | review-only
    acceptance_criteria:
      - "empty stage returns 0, not a crash"
      - "regression test fails before, passes after"
```
Only `workable_categories` with `status: queued` and `autopilot: allow` are auto-picked. Others need
`forced: true` or a human.

### Epics (work too big for one commit)
Give a big task `category: epic` — the loop can never pick one, even forced. It moves
through a human-gated planning pipeline instead:

```bash
orbit epic . plan <id>        # headless planner → .autopilot/specs/<id>.md, stage → spec_ready
# read the spec, then:
orbit epic . approve <id>     # human act — freezes the spec, stage → approved
orbit epic . decompose <id>   # headless decomposer → child tasks (proposed, epic: <id>)
```
The dashboard's Epics strip (Deep space) drives the same pipeline with buttons. Children
are ordinary backlog tasks — queue them one by one, in spec order.

## 6. Run
```bash
orbit doctor .      # validate everything, dry-run routing (read-only)
orbit run .         # one loop, foreground — watch it work a task
orbit install .     # background service (launchd/systemd)
orbit pause . / resume .   # kill switch
```

## 6.5. Project knowledge base (optional)
```bash
orbit knowledge . build      # one headless survey → .autopilot/knowledge/
orbit knowledge . status     # exit 0 fresh, 4 stale — deterministic, no agent call
orbit knowledge . refresh    # re-survey just the stale docs
```
`build` runs one headless survey of the repo (evidence-backed from code — never a live
DB) and writes `.autopilot/knowledge/`, briefed into every cycle (step 2.7) once
`INDEX.md` exists:

- **`INDEX.md`** — one-paragraph project summary, domain glossary, links to every other doc (≤80 lines)
- **`endpoints.md`** — one section per router/controller: method+path → handler → tables read/written
- **`er.mmd`** — a mermaid `erDiagram`, entities and relations inferred from migrations/models/SQL
- **`mindmap.mmd`** — a mermaid `mindmap` of domains → modules → key files
- **`flows/`** — one doc per significant business flow (login, checkout, ...), endpoint → handler → tables
- **`conventions.md`** — naming/layering/error-handling/testing patterns, each tagged STANDARD or NON-STANDARD
- **`manifest.json`** — the completion signal + staleness map (`files:` maps source paths to the docs they feed)

Workflow: `orbit knowledge . build`, review the git diff like any other change, commit it.
`status` reads merge markers since the manifest's `generated_at` and reports which docs a
later merge made stale; `refresh` re-runs the survey scoped to just those. Config
(`.autopilot/config.yaml`, all optional):
```yaml
# --- project knowledge base (optional) ----------------------------------------
# Generated by `orbit knowledge build` (LLM survey of the repo — no live DB),
# written to .autopilot/knowledge/ (committed, reviewed like code) and briefed
# into every cycle when present (presence of knowledge/INDEX.md is the switch;
# this block only tunes refresh behavior).
# knowledge:
#   refresh: "manual"            # "manual" | "auto" (loop refreshes between cycles when stale)
#   stale_after_merges: 3        # auto: refresh once >=N merged ships touch mapped files
#   timeout_seconds: 1800        # cap on the headless survey call
```
Trust rule: the knowledge base is human-reviewed like code, but **code wins over
knowledge** — a maker who finds the two disagree trusts the code and notes the
discrepancy.

## 6.6. Task-level planning (optional)
Before the build loop, a **planner** subagent writes a justified per-task plan —
approach, a `plan_scope` file-scope fence, applicable standards — to
`$AP_STATE/reviews/task-<id>-plan.md` (cycle step 2.8); the maker, checker, and
verifier are all briefed with it. When a plan exists it also shows up in that
task's review packet, verbatim, in a `## The plan (scope fence + approach)`
section between the contract and the commit. Config (`.autopilot/config.yaml`,
all optional):
```yaml
# --- task-level planning (optional) -------------------------------------------
# Before the build loop, a planner subagent writes a justified per-task plan
# (approach, file-scope fence, applicable standards) into the review packet;
# the maker is briefed with it and the verifier fails any off-plan diff.
# plan:
#   categories: [bug, feature, refactor, code_quality, testing]  # which tasks get planned
#   enforce: "verifier"        # "verifier" (LLM judges the fence) | "block" (wrapper refuses
#                              #  to push off-plan commits — exit 3 → escalate)
#   model: "sonnet"            # planner model (router defaults.planner overrides)
```
`model` only takes effect if a target router override omits `defaults.planner` — the
engine router already defines it, so that wins by default.

`categories` absent means every workable category gets a plan. The `enforce` ladder:
**`verifier`** (default) — the spec-conformance verifier (step 3.5) judges any diff
outside the plan's file scope as NONCONFORMING, one more review signal that feeds
back to the maker like any other verifier finding; **`block`** — additionally has the
wrapper run `engine/plan_gate.py`, a deterministic backstop right before the push
that refuses to ship (escalating instead) any commit touching a file outside the
plan's `edit`/`create`/`allow_incidental` scope, or inside `forbid`. Amendment
protocol: if the maker hits a genuinely out-of-plan file, the planner is
re-dispatched once with that reason and appends a new `## Amendment <n>` section —
a complete new file-scope fence, the last one in the file always wins — to the
SAME plan file rather than editing what's already there; a second out-of-plan stop
on the same task escalates.

## 6.7. QA runtime (optional)
A browser-driving **QA agent** proves each ship in the RUNNING product, not just the
diff — and, unlike the legacy runtime check, can bounce a failing task back for
another attempt instead of escalating it straight to a human. When the target's
config carries a `qa:` block it supersedes `runtime_check:` as the post-commit gate
(`engine/qa_check.py`, dispatched by `run.sh` right after the cycle commits and
before it pushes). Config (`.autopilot/config.yaml`, all optional):
```yaml
# --- QA runtime (optional): a browser-driving QA agent proves ships, replays taught
# flows, and files failures back onto the task. Requires node+npx in the target env
# (Playwright MCP is fetched via npx — never an engine dependency). When present,
# this supersedes runtime_check: as the post-commit gate.
# qa:
#   services:                          # full-stack launch recipe, in start order
#     - name: api
#       launch: "npm run dev:api"      # command; run from `cwd` inside the worktree
#       cwd: "server"                  # optional, default "."
#       ready: "http://localhost:4000/health"   # optional; polled until HTTP 200
#     - name: web
#       launch: "npm run dev"
#       ready: "http://localhost:3000"
#   base_url: "http://localhost:3000"  # where the browser starts (required with services)
#   env: {PORT: "3000"}                # exported into service launches (dedicated QA ports)
#   ready_timeout_seconds: 90
#   timeout_seconds: 1200              # whole QA-agent budget per invocation
#   model: "sonnet"                    # QA runs are frequent — cheaper than the maker
#   mcp_config: null                   # path to your own mcp json; null → engine auto-generates
#   browser: "chromium"
#   headless: true
#   max_bounces: 2                     # QA→dev re-files before escalating to a human
#   gate_categories:                   # replaces runtime_check.categories when qa: present
#     frontend: required               # required = QA fail blocks the ship (bounce/escalate)
#     feature: required
#     bug: evidence                    # evidence only, never blocks
```
Post-commit, the `/orbit-qa-check <task-id>` agent (`skills/orbit-qa-check.md`)
launches (or attaches to) the product per `qa.services`/`base_url`, drives the
SPECIFIC flow the task's acceptance criteria describe through a per-run Playwright
MCP server (screenshots land under `state/qa/task-<id>/`), and writes a
`pass`/`fail`/`unable` verdict judged against `qa.gate_categories`.

**The bounce loop:** a `fail` verdict on a `required` category re-files the SAME
task back onto the queue (`status: queued`) with the QA agent's findings folded
into its contract (`qa_findings`) and the findings file cited, and bumps its
`qa_bounces` counter — the next pick sees exactly what QA observed and is
expected to fix it, up to `qa.max_bounces` (default 2) attempts. Once the cap is
reached, the cycle escalates instead, attaching the QA verdict plus a screenshot
and the findings report to the ledger entry so a human sees the evidence on the
dashboard. `unable` (the environment, not the code, blocked the check) never
bounces or blocks, and neither does a `fail` on an `evidence`-only category.

**Requirements:** `node`/`npx` on the machine running the loop — Playwright MCP
(`@playwright/mcp`) is fetched by `npx` at QA-run time, never an engine
dependency. `orbit doctor` checks `node`/`npx` are on PATH when `qa:` is
configured (fails the run otherwise) and advises — without failing — if
`npx playwright --version` isn't already cached; it also flags when both `qa:`
and `runtime_check:` are configured (`qa:` wins).

**Legacy note:** no `qa:` block in your config → `qa_check.py` runs byte-for-byte
the same `runtime_check:` behaviour as before (same config keys, same
`/orbit-runtime-check` skill, same `reviews/task-<id>-runtime.json` verdict) —
nothing changes until you add one.

## 6.8. Teach mode (optional): the flow library
Where the QA gate above proves ONE task's acceptance criteria, **teach mode** builds a
standing library of browser flows a human records once and the QA agent replays forever
after — decoupled from any single task's cycle.

```bash
orbit teach <target> <flow-name> [--url URL]   # target URL = --url > qa.base_url; neither -> exit 2
```
Brings `qa.services` up, then launches `npx playwright codegen` against the target URL
in the foreground — you drive the real recording browser yourself and close its window
when the flow is complete. It then prompts on stdin for a one-line intent and one-or-more
success criteria; nothing is written until both are given (an empty intent or zero
criteria is exit 1, no flow left behind). On success it writes the flow dir and prints a
`git add` hint — **you** review the new flow dir's git diff like any other change, then
commit it; the engine never runs git on the target.

Each taught flow lives at `.autopilot/<qa.flows_dir>/<name>/` (default flows_dir
`qa/flows`):
- **`intent.md`** — frontmatter (name/created/entry_path/tags) + your one-line intent +
  a `## Success criteria` list — the contract the replay agent grades against
- **`steps.spec.ts`** — the raw `codegen` recording, byte-for-byte — ground truth
- **`meta.json`** — `created_at`/`last_run_at`/`last_verdict`/`runs`/`heals`, updated on
  every replay
- **`steps.json`** — the normalized, healable step list — **not** written by `teach`;
  it appears after the first fully clean replay (no drift, intent achieved)

One directory up from `flows_dir` (default `.autopilot/qa/FLOWMAP.md`), a `FLOWMAP.md`
tracks one row per flow (name/intent/entry path/last verdict). Each `orbit qa --flow`
run updates that flow's own last-verdict cell in place, never duplicating the row (a
flow name already taught is refused by `orbit teach` for now — re-teaching lands a
later phase).

**Replaying a flow:**
```bash
orbit qa <target> --flow <name> [--timeout N]
```
Runs the `/orbit-qa-flow` skill headless against the named flow and records a verdict
into its `meta.json` + `FLOWMAP.md` row. Exit codes:

| exit | meaning |
|------|---------|
| 0 | pass, or healed (UI drifted but the intent still holds) |
| 3 | fail — an app bug |
| 1 | unable (the environment prevented the attempt), no verdict was written, or the verdict was malformed |
| 2 | usage/validation error (bad flow-name slug, no `qa:` block, or an unknown flow name) |

The replay agent tells drift from breakage: if it needed one OBVIOUSLY-equivalent path to
still satisfy every success criterion (a renamed button, a moved-but-recognizable
control), the verdict is **healed** — a corrected-steps candidate is written to
`$AP_STATE/qa/heal/`, **pending approval** (the dashboard shows the pending count;
the approval action itself lands next phase) — the flow dir's own steps are never
touched by a heal. If the intent genuinely can't be achieved, the verdict is **fail** —
one row is appended to `ops/qa/SEEN.md`, which the `qa` signal adapter turns into a
`status: proposed` task for a human to triage, the same discovery pipeline any other
adapter-sourced finding flows through.

On a flow's first fully-clean replay (no drift, every success criterion met), the
agent also writes `steps.json` into the flow dir, alongside its own `meta.json`/
`FLOWMAP.md` updates. Treat that the same as any other flow-dir change: review the
new `steps.json` against `steps.spec.ts` and `git add`/commit it once you're happy —
`orbit qa --flow` never commits on your behalf, replay or teach alike.

## Opt-in task sources (signal adapters)
Beyond the native `backlog.yaml`, every extra name in `sources:` runs a matching
`<name>_to_backlog.py` each cycle — resolved from YOUR repo's `.autopilot/adapters/` first,
then the engine's `adapters/`. The contract: the adapter runs with `AP_HOME`/`AP_STATE`/
`ORBIT_HOME` exported and cwd = the repo root; it appends `status: proposed` tasks to
backlog.yaml itself, idempotently (use `engine/backlog_append.py`), and never queues work
or touches existing tasks. Shipped examples to copy from: `foundry` (maturity tasks),
`logwatch` (prod log findings), `qa` (UI-test findings) — each carries coupling to its
specific tool. This is the operate-phase feedback loop: production signal → proposed task
→ human triage → fix ships.

`jira` is the shipped intake-side source: label a ticket `orbit-ready` and the adapter
exports it, has an enrichment agent survey the code and draft a gated task contract
(evidence + gradable acceptance criteria), and imports it — or comments specific
questions back on the ticket when it's too thin to build (a human's reply retriggers
enrichment). Ships, merges, and rejections are commented back on the ticket, all
engine-side. Configure the `jira:` block (see `config/schema.yaml`), add `jira` to
`sources:`, and put credentials in `state/.jira_token` — agents never see the token.
Exception to "never queues work": with `jira.auto_queue: true` a gate-passing import
lands queued — the ready-label was the human queue act. Merge stays human always.
