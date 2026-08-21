---
description: Methodology for triangulating one specific hypothesis to a VALIDATED/NEGATED/INCONCLUSIVE verdict with quantified impact and a recommended fix. Invoked by the orchestrator to brief the analyst maker.
argument-hint: "(no args — the task contract arrives via the cycle brief)"
allowed-tools: Read, Grep, Glob, Write, Edit, mcp__*
model: opus
---

You are briefing a hypothesis task (category `hypothesis`). Distill the
following method into the maker brief.

## Method
1. **Phase 0 - load context silently**: `CLAUDE.md`, `knowledge/`, and the
   funnel/insight doc that raised this hypothesis if one exists. Do not
   narrate.
2. **Restate the hypothesis** as one falsifiable claim tied to a cohort and a
   time window. If the task's hypothesis is vague, sharpen it to the most
   conservative testable form and say so in the doc.
3. **Discover before querying**: verticals -> projects -> use cases ->
   explores -> fields. Never hand-write a query against guessed columns.
4. **Triangulate**: pull evidence from at least two independent sources where
   they exist (a query plus a knowledge-folder export, or two explores). One
   source is a clue, two are a finding. If only one source exists, say so in
   Basis and lower confidence accordingly rather than presenting it as proof.
5. **Render a verdict**, stated first in the Answer section:
   - `VALIDATED` - the evidence agrees the hypothesis holds.
   - `NEGATED` - the evidence contradicts it.
   - `INCONCLUSIVE` - the data cannot decide either way; record exactly what
     is missing in Basis, never guess past it.
6. **Quantify impact** in INR or affected users whenever the data supports
   it - mandatory when the data supports it, not optional polish.
7. **Recommend one concrete fix** (or, for NEGATED/INCONCLUSIVE, the next
   investigation step), grounded in what the evidence actually showed.
8. **Write** `insights/<task-id>-hypothesis-<slug>.md`: `# title`,
   `## Answer` (verdict first, then evidence, quantified impact, recommended
   fix), `## Basis` with typed sources. Every inline number cites `(S<n>)`.
   Gaps are recorded in Basis, never papered over.
9. **Definition of done**: `python3 $ORBIT_HOME/engine/insight_lint.py .`
   exits 0 on the worktree, and the doc answers the task's acceptance
   criteria.

Design references: the PREMIUM PM-agent methodology (triangulation, INR
impact, verdict discipline) and dwh-suite's grounding contract.
