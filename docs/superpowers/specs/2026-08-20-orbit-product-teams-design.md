# Orbit for Product Teams - Design

Date: 2026-08-20
Status: approved design, pre-implementation
Owner: Arun Aryasomayajula

## What this is

Orbit today is an autonomous harness for one developer working on one code repo.
This design extends it to product managers: PRD authoring, data analysis via MetaQL, survey and customer research, and design workflows, all running through the same loop, gates, ledger, and dashboard.

Three references shaped it:

- **dwh-suite `feat/prd-flow-v2-grounding`** (Ravi Jain): a Claude Code plugin with a four-stage PRD pipeline (intake, grounded research, synthesis, writer) where a stdlib controller stamps stage artifacts in a content-hash chain, deterministic evals check citation integrity, and humans gate scope and sign-off. Orbit absorbs this mechanism natively; dwh-suite is a reference spec, not a dependency.
- **The PREMIUM guide** (premium.mypaytm.com): the RU team's PM-agent methodology. Six skills (start-product, analyze-funnel, analyze-hypothesis, create-jira, update-funnel-map, discuss), a three-phase method (discovery, execution, feedback), mandatory INR impact on every finding, multi-source triangulation, and a knowledge flywheel. PREMIUM is interactive and unguarded; Orbit adds the unattended loop, mechanical gates, and engine-validated knowledge.
- **gstack** (MIT, Garry Tan): reference designs for the browser driver, scrape/skillify, auto-decide review gates, and design-review rubrics. Nothing from gstack is vendored or invoked. Every capability is rebuilt Orbit-native, crediting gstack in docs and comments.

## Decisions (locked with the user, 2026-08-20)

1. **Deployment**: each PM runs Orbit locally on their own machine with their own MCP credentials. No shared server.
2. **dwh-suite**: absorb the PRD flow into Orbit. Port the mechanism, not the code. Talk to Ravi so his copy freezes and drift ends.
3. **Target**: each product gets a product workspace git repo. PRD review is a PR review on that repo.
4. **V1 scope**: all four workflow families (PRD pipeline, MetaQL analysis, surveys, Figma read-side), staged in milestones.
5. **Autonomy**: the full unattended loop with human gates, same as dev Orbit. Scheduled adapters may file tasks.
6. **gstack**: reference only. Replicate needed skills natively in Orbit.
7. **Parallelism**: multiple products in parallel via a Chrome-style tab UI in the launcher, and concurrent tasks within one workspace.
8. **Tabs are dashboard-only in v1**. Interactive chat stays in the terminal. An embedded web chat is a v2 candidate.

## Design

### 1. Shape

One codebase.
"Product" is a profile flavor, not a fork: a workspace repo is a normal Orbit target whose `.autopilot/config.yaml` declares product categories, MCP servers, and publish targets.
The engine internals that change: MCP config generation, loop concurrency, and a new PRD state machine.
Everything else extends existing seams (skills, agents, router categories, signal adapters, launcher).

### 2. Product workspace

`orbit init --product` scaffolds a git repo per product:

```
<product>-workspace/
  CLAUDE.md                  briefing document, under 200 lines
  knowledge/                 screenshots, data-source docs, error mappings, competitor notes
  research/<run-id>/         PRD runs: 01-intake.json ... 04-prd.md plus research artifacts
  insights/                  funnel reports, hypothesis findings, ad-hoc analyses
  surveys/<name>/            questionnaire, raw responses, coded output, report
  .autopilot/                config, backlog, router override, state (runtime parts gitignored)
```

The knowledge flywheel reuses Orbit's existing knowledge machinery pointed at product context.
The engine post-validates what agents save, which replaces PREMIUM's rule that the PM hand-review every knowledge write for two weeks.
Approved artifacts leave the repo through publish adapters (section 11).

### 3. MCP seam

A new `mcp:` block in the profile declares servers, for example:

```yaml
mcp:
  metaql:  {transport: http, url: ..., token_env: METAQL_TOKEN}
  figma:   {transport: ...}
  atlassian: {transport: ...}
```

The engine generalizes the current Playwright-only per-run MCP config generation (`qa_check.py:build_mcp_config`) to emit `--mcp-config` from this block, and generates matching `mcp__<server>__*` permission entries into a per-profile settings file.
Servers that can mutate get read-only guards: MetaQL restricted to query/list tools, Jira creation gated behind category policy.
Tokens stay engine-side in env vars or Keychain and never enter the repo or the agent's prompt.

### 4. PRD pipeline

`engine/prd_flow.py`, a state machine modeled on `epic_plan.py`.

Stages: `intake -> [human: approve-scope] -> research -> synthesis -> draft -> [auto: review gate] -> [human: sign-off via PR merge] -> published`.

Mechanisms ported from dwh-suite, reimplemented natively:

- **Controller owns the verdict.** Only `prd_flow.py` stamps a stage done. Each artifact's content hash chains to the prior stage's, so a forged or hand-edited stage is mechanically detectable.
- **Tier-A deterministic evals**: schema validation, citation integrity (every claim's `source_ids` resolve), a lint that acceptance criteria are testable (contain a number, threshold, or pass/fail condition), complete references.
- **Tier-B LLM judge**: for example "is synthesis a genuine advance on research, not a paraphrase". Advisory by default, blockable by config.
- **Grounding contract** briefed into every stage as a track: no fact without a source, abstain over guess (record coverage gaps instead of fabricating), each stage stays in its lane, and a `pii_touch` flag defers compliance judgments to a human.
- **Research grounds against real systems**: MetaQL queries, Confluence and Jira via MCP, competitor scrapes, all cited as typed sources.
- Each stage is a loop task. Stages of different PRDs run in parallel; stages of one PRD are serial by construction of the stamp chain.
- Human gates surface as dashboard escalations and as the PR merge itself.

**Review gate**: an auto-decide review pass replicating gstack's autoplan pattern natively.
Two lenses run over the draft: a CEO lens (scope challenge, completeness scoring) and a feasibility lens (dependencies, rollout, instrumentation).
Every auto-decision lands in an audit-trail section of the draft so the human sign-off sees what was decided for them.

### 5. Analysis workflows

New skills, agents, and router categories encoding PREMIUM's methodology under dwh-suite's discipline.

- `orbit-analyze-funnel`: full-funnel pull for a cohort via MetaQL, ranked hypotheses with data signals.
- `orbit-hypothesis`: deep-dive on one hypothesis, triangulating MetaQL data, knowledge screenshots, CST exports, and scrapes. Validates or negates with evidence. INR impact is mandatory. Every number carries query provenance.
- `orbit-insight`: ad-hoc data questions producing a grounded insight doc, optionally a MetaQL canvas.
- Agents: `analyst` and `researcher` as drop-in `agents/*.md` files with router entries.
- Findings may auto-file follow-up tasks at `status: proposed` only, e.g. a validated hypothesis proposes a create-jira task. The human promotes.
- **Scheduled watches**: a `metric_watch` signal adapter runs configured MetaQL checks on the loop's cadence and files anomaly tasks into the backlog.

### 6. Surveys

- `orbit-survey-design`: goals to hypotheses to questionnaire, with the premise challenged rather than validated.
- `orbit-survey-analyze`: ingest a CSV or Sheets export, code open-text responses with verbatim citations, cross-tab against MetaQL behavioral cohorts, produce an insights report.

Responses arrive as files in `surveys/<name>/raw/` or via Drive MCP.
Direct survey-tool APIs are a later adapter.

### 7. Figma, read-side

Research and PRD stages may pull design context and screenshots via the Figma MCP and cite frames as typed sources.
`orbit-design-review` audits shipped screens against design-system rubrics (replicated natively from gstack's design-review criteria), fed by Figma frames or live pages.
Write-side design generation is deferred to v1.5: it is the least-proven MCP surface and a bad auto-generated design would cost PM trust in everything else.

### 8. Native browser layer

`engine/browse.py`: a thin Python Playwright driver with a persistent context, snapshot/click/fill/screenshot primitives, and `state save/load` for durable authenticated sessions across task runs.
It replaces the per-run Playwright-MCP wiring for QA, scrape, and design-review.
`orbit-scrape` is read-only by contract (refuses mutating intents, returns JSON), and a skillify-equivalent persists a proven scrape as a repeatable extractor under `.autopilot/scrapers/`.
This adds `playwright` as the one new Python dependency, required only by product and QA profiles.
gstack's browse/scrape/skillify designs are the acknowledged reference.

### 9. Parallelism

`concurrency: N` in the profile, default 2.
The loop becomes a small worker pool.
Workers atomically claim tasks through a lockfile-based claims file; the queue itself stays read-only and regenerated per iteration as today.
`ledger.py` writes serialize behind a file lock; the lifecycle state machine already rejects illegal transitions.
Serialization rules: one stage per PRD instance at a time, browser tasks hold a browse-session lock, everything else runs free.
Spend caps and cycle timeouts apply per worker.
Worktree-per-task isolation already exists, which is what makes this a contained change.

### 10. Tabs

`launcher.py` becomes the process manager.
A persistent Chrome-style tab strip shows one tab per product workspace, each backed by its own dashboard child process.
Per-tab controls start, stop, and pause that workspace's loop; the launcher owns the loop processes, so PMs never touch launchd.
Per-tab status badges: running, idle, needs-you, gate-waiting.
A cross-tab notification strip surfaces escalations from any product.

### 11. Publishing

Publish adapters run engine-side, gated on recorded human approval (PR merged or sign-off stamped), idempotent, and ledger-recorded:

- `confluence_publish`: approved PRD to a Confluence page.
- `jira_publish`: validated finding to a formatted ticket with evidence and INR impact.
- Optional Slack summary.

### 12. Dashboard additions

In the existing `command_center.py` single-file pattern:
a PRD pipeline view (stage progress, stamps, eval verdicts, gate buttons),
an artifact browser (rendered markdown of insights and PRDs with diffs),
and survey and metric-watch status.

### 13. Testing and rollout

Every engine piece gets pytest coverage in the existing hermetic style, with env-var injection replacing the `claude --print` call.

Build order:

- **M1**: MCP seam, workspace template, `orbit-insight`. First PM value.
- **M2**: PRD pipeline and review gate.
- **M3**: funnel and hypothesis skills, metric watches, surveys.
- **M4**: parallelism and tabs.
- **M5**: Figma read-side, publish adapters, `orbit doctor --product` installer hardening (no bun, Playwright install, Keychain checks).

Pilot: Arun plus one PM on one real product.

## Risks

- **Installer friction decides adoption.** PMs will not debug Playwright or Keychain. `orbit doctor --product` must diagnose every setup failure, and setup must be one command.
- **Drift against dwh-suite.** Until Ravi agrees to freeze his PRD flow, two live implementations of the same idea exist. Owner: Arun, conversation before M2 starts.
- **MetaQL MCP under headless permission mode is untested** inside Orbit's settings model. Validate in M1 before building M3 on top of it.
- **Concurrency touches the loop's oldest code.** Mitigated by the claims-and-locks design and existing worktree isolation, and by shipping it in M4 after the workflows are proven serial.
- **Survey source assumption**: v1 assumes CSV/Sheets exports. If the team standardizes on a survey tool with an API, that adapter moves up.

## Out of scope for v1

- Figma write-side design generation (v1.5).
- Embedded web chat per tab (v2).
- Direct survey-tool API integrations.
- Any shared or hosted deployment.
