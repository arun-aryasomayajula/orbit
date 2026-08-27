# Track: prd-grounding

The grounding contract for Orbit's five-stage PRD flow. Every artifact and stage operates under these rules.

## Universal rules (all stages)

- **No fact without a source** - Every claim in the PRD must trace to a typed source entry in research (02-research.json) via inline citations (S<n>).
- **Abstain over guess** - Unknown or unverified claims go to `coverage_gaps` or `open_questions`, never into findings or requirements.
- **Stay in your lane** - Per-stage responsibilities are strict and non-overlapping. A stage that invents work outside its contract creates waste downstream.
  - Intake: grounds on title and brief, does not design or research.
  - Research: gathers evidence via real systems (analytics, Jira, Confluence, code, docs), every finding cites source_ids.
  - Synthesis: advances beyond research (transforms findings into requirements), adds no new claims.
  - Writer (draft): adds no fact absent upstream, cites (S<n>) inline, numbering per acceptance criteria.
  - Review: two lenses only (CEO + feasibility), auto-decides, logs every verdict.
- **Controller owns the verdict** - prd_flow.py and prd_evals.py are the sole authority on whether a stage passes. Self-reporting is never trusted.
- **PII touch defers to human** - If intake.pii_touch is true, the scope gate needs an explicit PII acknowledgement on top of the normal approval: `prd_flow.approve_scope` refuses without it (`--ack-pii` on the CLI, a second confirm in the dashboard), so the run cannot reach research until a human says yes to the PII specifically. The skill records the flag; compliance is not the skill's job.
- **A stamped artifact is immutable** - later stages never edit earlier artifacts.

## Stage-specific lanes

### Intake (01-intake.json)

**Input:** title, brief (from marker)  
**Output:** {run_id, title, pm, goal, scope_in: [...], scope_out: [...], data_targets: [...], code_targets: [...], pii_touch: bool}

- Extract FROM title and brief only; do not hypothesize.
- scope_in: features/areas explicitly in scope (required, non-empty).
- scope_out: what is explicitly out (clarify non-obvious exclusions).
- data_targets: systems/tables/explores the research will query (e.g., "analytics:lending", "Jira:PLAT").
- code_targets: repos/modules that research will review.
- pii_touch: whether the PRD touches personally identifiable information (true/false only; no interpretation).
- pm: the stakeholder/PM name if brief names one, otherwise empty string.

### Research (02-research.json)

**Input:** 01-intake.json  
**Output:** {run_id, sources: [...], findings: [...], coverage_gaps: [...]}

- One typed source entry per tool result used (analytics queries, Jira reads, code extracts, etc.).
- source.id format: "S<n>" (S1, S2, ...).
- source.kind: one of analytics|jira|confluence|scrape|doc|code.
- Every finding.claim must cite at least one source_id; unsourced claims are a hard failure.
- coverage_gaps: explicitly record what research could not reach (e.g., "user sentiment data not available", "legacy system X not accessible").

### Synthesis (03-synthesis.json)

**Input:** 02-research.json  
**Output:** {run_id, requirements: [...], gaps: [...], open_questions: [...]}

- Transform findings into requirements: one or more findings may combine into a single requirement.
- requirement.id format: "R<n>" (R1, R2, ...).
- requirement.source_ids: cite the research sources that support this requirement (never empty).
- gaps: problems identified in research that fall outside this PRD's scope (separate from requirements).
- open_questions: clarifications the PM must answer before the writer can proceed (inherit from research.coverage_gaps + new ones).

### Writer (04-prd.md)

**Input:** 02-research.json, 03-synthesis.json  
**Output:** Markdown PRD with headings ## Problem, ## Goals, ## Non-goals, ## Requirements, ## Acceptance criteria, ## Open questions, ## Basis

- Every number, every metric, every specific fact must cite a research source inline: (S<n>).
- Acceptance criteria are numbered and must each carry a measurable threshold or pass-fail condition.
- ## Basis section lists all sources (copy from 02-research.json; inline citations resolve here).
- No claims about what is not in research; gaps are named in ## Open questions.

### Review (05-review.json)

**Input:** 04-prd.md, 02-research.json  
**Output:** {verdicts: [{lens, finding, decision, rationale}...], ok: bool}

- Two lenses only:
  1. **CEO (scope + completeness)**: Does the PRD address the stated goal? Is it complete (0-10 completeness score)? Does scope match the initial brief?
  2. **Feasibility**: What dependencies, rollout risks, or instrumentation gaps must the team own?
- Every verdict logged as a structure: {lens, finding, decision, rationale}.
- ok: false only if a contradiction exists that the PM must resolve (e.g., two requirements that cannot both be met). Otherwise true.
- The verdicts list IS the audit trail. The review stage never modifies 04-prd.md or any other stamped artifact.

## Design references

Patterns inspired by dwh-suite's prd_run.py and gstack's autoplan skill.
