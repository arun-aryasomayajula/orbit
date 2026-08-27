---
description: Methodology for a funnel-drop analysis - discovery-phase pull across a cohort's full funnel, stages ranked by biggest absolute drop, with hypotheses for the top drop. Invoked by the orchestrator to brief the analyst maker.
argument-hint: "(no args - the task contract arrives via the cycle brief)"
allowed-tools: Read, Grep, Glob, Write, Edit, mcp__*
model: opus
---

You are briefing a funnel task (category `funnel`). Distill the following
method into the maker brief.

## Method
1. **Phase 0 - load context silently**: `CLAUDE.md`, `knowledge/` (data-source
   notes, query gotchas), the two most recent `insights/*.md`. Do not narrate.
2. **Restate the question** as a testable claim: which cohort, which funnel,
   which time window. If the task lacks any of these, choose the most
   conservative reading and say so in the doc.
3. **Discover before querying**: verticals -> projects -> use cases ->
   explores -> fields. Pull the FULL funnel for the cohort in one discovery
   pass, not stage-by-stage guesses. Wrong-column guessing is the top failure
   mode; the knowledge folder's gotchas exist to prevent it.
4. **Rank by absolute drop**: compute conversion between every consecutive
   stage, then rank stage-to-stage drops by ABSOLUTE count/user drop, not by
   percentage. A stage with a small base and a big percentage drop is often
   noise; the biggest absolute drop is where the real leak is.
5. **Generate 3-5 hypotheses** for the biggest drop. Each one needs:
   - a concrete data signal that would confirm or refute it (checkable now or
     in an obvious follow-up query, never a vibe)
   - an estimated impact (INR or affected users) if the hypothesis holds
6. **Triangulate** where a second source exists (another explore, a
   knowledge-folder export): cross-check the headline drop before trusting it.
7. **Write** `insights/<task-id>-funnel-<slug>.md`: `# title`, `## Answer`
   (a funnel table across stages with conversion % and absolute drop, then
   the ranked hypothesis list), `## Basis` with typed sources. Every inline
   number cites `(S<n>)`. Gaps are recorded in Basis, never papered over.
8. **Basis lines MUST start `- S<n>: `** at column 0 (no leading spaces, no
   bullet nesting) - the lint gate matches this exact shape. Literally:

   ```
   ## Basis
   - S1: analytics query <data model>, 2026-08-21
   - S2: doc raw/responses.csv
   ```
9. **Definition of done**: `python3 $ORBIT_HOME/engine/insight_lint.py .`
   exits 0 on the worktree, and the doc answers the task's acceptance
   criteria.

Design references: the PREMIUM PM-agent methodology (funnel thinking - rank by
absolute drop, hypotheses paired with checkable signals) and dwh-suite's
grounding contract.
