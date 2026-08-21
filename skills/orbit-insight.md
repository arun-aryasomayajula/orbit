---
description: Methodology for one grounded product-data insight — question to evidence-backed doc via MetaQL discovery, under the grounding contract (no fact without a source, abstain over guess). Invoked by the orchestrator to brief the analyst maker.
argument-hint: "(no args — the task contract arrives via the cycle brief)"
allowed-tools: Read, Grep, Glob
model: opus
---

You are briefing an insight task (category `insight`). Distill the following
method into the maker brief.

## Method
1. **Phase 0 — load context silently**: `CLAUDE.md`, `knowledge/` (data-source
   notes, query gotchas), the two most recent `insights/*.md`. Do not narrate.
2. **Restate the question** as a testable claim with a cohort and a time window.
   If the task lacks either, choose the most conservative reading and say so in
   the doc.
3. **Discover before querying**: verticals → projects → use cases → explores →
   fields. Only then generate and execute the query. Wrong-column guessing is
   the top failure mode; the knowledge folder's gotchas exist to prevent it.
4. **Triangulate**: one source is a clue, two are a finding. Where a second
   source exists (another explore, a knowledge-folder export), cross-check the
   headline number.
5. **Quantify impact** in INR or affected users when the data supports it.
6. **Write** `insights/<task-id>-<slug>.md`: `# title`, `## Answer`, `## Basis`
   with typed sources. Every inline number cites `(S<n>)`. Gaps are recorded in
   Basis, never papered over.
7. **Definition of done**: `python3 $ORBIT_HOME/engine/insight_lint.py .` exits
   0 on the worktree, and the doc answers the task's acceptance criteria.

Design references: the PREMIUM PM-agent methodology (funnel thinking,
triangulation, INR impact) and dwh-suite's grounding contract.
