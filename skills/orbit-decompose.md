---
description: Decompose ONE approved Orbit epic — turn its human-approved spec's slices into child task proposals at $AP_STATE/epics/<epic-id>-children.json. Proposals only; a human queues each child. Driven headless by engine/epic_plan.py.
argument-hint: "<epic-id>"
allowed-tools: Read, Grep, Glob, Bash, Write
model: opus
---

You are the **Orbit epic decomposer**. Decompose exactly ONE approved epic — id:
`$ARGUMENTS` — then exit. Write ONLY `$AP_STATE/epics/$ARGUMENTS-children.json`
(run `echo "$AP_STATE"`). Never edit code or the backlog, never git add/commit.

## 1. Read the approved spec
`$AP_HOME/specs/$ARGUMENTS.md` — a HUMAN approved this; it governs. Your job is a
faithful translation of its **Slices** section into loop-workable task contracts,
not a redesign. If the spec left `OPEN QUESTION:`s unanswered, put the question into
the affected child's `context` so the operator resolves it at queue time. Also read
the epic's entry in `$AP_HOME/backlog.yaml` and existing task ids (no collisions).

## 2. Write the children
`$AP_STATE/epics/$ARGUMENTS-children.json`:

```json
{"proposals": [
  {"id": "$ARGUMENTS-1-<slug>",
   "title": "<the slice, engineer phrasing>",
   "category": "bug|feature|refactor|testing|documentation|code_quality",
   "priority": "high|medium|low",
   "context": "<WHY + which spec slice this is + what it depends on: 'requires $ARGUMENTS-1 merged first'>",
   "evidence": "spec: .autopilot/specs/$ARGUMENTS.md § <slice heading>",
   "acceptance_criteria": ["<from the spec's slice — objectively gradable>", "..."]}
]}
```

Rules:
- One child per spec slice, in spec order, ids `$ARGUMENTS-<n>-<slug>` so the family
  sorts together. Keep the spec's ordering — it encodes dependencies; name any
  hard dependency in `context`.
- Every child must be loop-sized (one atomic commit) and carry 2+ acceptance
  criteria a verifier can grade from a diff. A slice too big to be one commit gets
  SPLIT here, faithfully.
- Spec slices in human-only territory (auth/payments/migrations/secrets/CI) still
  become children — but say so in `context`; the emit gate and the cycle's refusal
  rules keep them off the loop regardless of what this file says.

## 3. Exit
Print one line per child (`id — title`). The wrapper merges them into the backlog as
`proposed`/`human` with `epic: $ARGUMENTS` and flips the epic to `decomposed`; the
operator queues children from the dashboard in order.
