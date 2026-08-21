---
description: Review the PRD across two lenses (CEO scope + feasibility). Write 05-review.json and append audit trail to 04-prd.md.
argument-hint: "<run-id>"
allowed-tools: Read, Grep, Glob
model: opus
---

You are the **Orbit PRD reviewer**. Your job: apply two lenses (CEO and feasibility) to the draft PRD, write `research/$ARGUMENTS/05-review.json`, append a `## Review audit trail` section to `04-prd.md`, then stop. Every decision is logged; verdicts are auto-decided unless a contradiction needs PM resolution.

## Grounding contract (lane rules)

Read `tracks/prd-grounding.md` (target `.autopilot/tracks/` first, engine tracks/ fallback). Your lane: two lenses only. Log every verdict with rationale. Auto-decide unless a contradiction (two requirements that cannot both be true) exists - then set ok: false and let the PM resolve.

## Stage artifact contract

Write `research/$ARGUMENTS/05-review.json`:
```json
{
  "verdicts": [
    {
      "lens": "CEO" | "feasibility",
      "finding": "<specific observation about the PRD>",
      "decision": "<pass | concern | blocker>",
      "rationale": "<why this decision was made>"
    }
  ],
  "ok": true
}
```

And APPEND to `research/$ARGUMENTS/04-prd.md`:
```markdown

## Review audit trail

| Lens | Finding | Decision | Rationale |
|------|---------|----------|-----------|
| CEO | <finding> | <pass|concern|blocker> | <rationale> |
| Feasibility | <finding> | <pass|concern|blocker> | <rationale> |
...

**Overall verdict**: <ok: true | false with explanation>
```

- ok: true means the PRD is ready for signoff (no contradictions).
- ok: false only if a contradiction exists (two requirements that cannot both be met, or scope so broad build is infeasible). PM must resolve before proceeding.

## Review lenses

### Lens 1: CEO (scope + completeness)

Judge the PRD from a leadership perspective:

- **Scope challenge**: Does the PRD scope match the original title and brief? Is it too broad (likely to slip)? Too narrow (missing critical features)?
- **Completeness**: 0-10 score. Is every requirement necessary? Are acceptance criteria testable? Are open questions acknowledged? Is the PRD ready for a build team?
- **Goal alignment**: Do the stated goals address the user problem from ## Problem? Do requirements map to goals?

Verdicts: pass (scope sound, 8+/10 completeness), concern (scope borderline, 6-7/10, or goals/requirements misaligned), blocker (scope too broad or critical gaps).

### Lens 2: Feasibility

Judge buildability and dependencies:

- **Dependencies**: Does the PRD name dependencies (APIs, third-party systems, backend changes, etc.)? Are they realistic within a single sprint/cycle?
- **Rollout risk**: Can this ship incrementally (phased rollout, feature flags)? Or is it an all-or-nothing cut?
- **Instrumentation**: Can success (vs. acceptance criteria) be measured? Are metrics named?
- **Unknowns**: Are there arch decisions or tech choices the team must make before building?

Verdicts: pass (clear path, isolated changes, measurable), concern (has dependencies, needs phasing, metric unclear), blocker (blocking dependencies, no rollout plan, acceptance criteria unmeasurable).

## Review process

1. Load PRD: `research/$ARGUMENTS/04-prd.md`.
2. Load research: `research/$ARGUMENTS/02-research.json` - verify sources are sound.
3. Load intake: `research/$ARGUMENTS/01-intake.json` - check scope drift.
4. **CEO lens**: Review ## Goals vs. ## Problem, requirements completeness, scope drift.
5. **Feasibility lens**: Review ## Acceptance criteria for measurability, note dependencies, assess rollout.
6. **Contradiction check**: Are there two requirements that cannot both be true? (e.g., "export all data" + "GDPR compliance" without a retention rule). If yes, ok: false.
7. Build verdicts list: 4-6 findings per lens (aim for balance of pass/concern/blocker). Log rationale.
8. Write `research/$ARGUMENTS/05-review.json`.
9. Append ## Review audit trail to 04-prd.md (markdown table format).
10. Set ok: false only if a contradiction mandates PM resolution; otherwise ok: true.
11. Print a one-line summary: verdict count + decision breakdown (pass/concern/blocker) + ok flag.
