# Orbit operator guide

Plain-words map of the dashboard. (Fallback copy — put a project-specific
`GUIDE.md` in your target repo's `.autopilot/` to override what `/guide` serves.)

## The 30-second mental model

A robot engineer works your backlog **one task at a time**. Every finished
task lands on its **own branch** for human review — it never touches the base
branch by itself. You control what it may work on (approve proposals), the
order (reorder the queue), and what ships (Merge / Reject per branch).
If you do nothing, it only works what was already approved.

## Sections

| Section | What it is | What you do |
|---|---|---|
| **In orbit** | The task being built right now | Nothing — watch, or Abort |
| **Launch pad** (Queue) | Approved tasks in pick order | Reorder / Park |
| **Deep space** | Proposals — inert until you approve | "Queue it" or Park |
| **Inbox → Escalations** | Robot stopped; needs a human call | Follow the 👉 recommendation; Answer or Park |
| **Inbox → Ships awaiting review** | Finished branches | Review packet → Merge / Reject |
| **Ships** | Shipped / merged / rejected history | Reference |
| **Graveyard orbit** | Parked tasks | Bring back anytime |

## Reading a card

Title = engineer phrasing. **Plain line** = what it means in product terms.
**👉 hint** = recommended call. **Effort chip**: S ≈ hours, M ≈ 1–2 days,
L = multi-day (L usually deserves a human-planned sprint slot). Expand
**why it matters** for impact / risk-if-skipped / background docs, and
**done when…** for the acceptance contract.

## Category → business meaning

bug = users see something wrong · security = data/access exposure ·
feature = new capability (product call) · code_quality/refactor = hygiene,
no visible change · observability/testing = cheap insurance · infrastructure/
dependencies = plumbing (eng judgment) · documentation = approve freely.

## Branches tab

Every `autopilot/*` branch on `origin`, reconciled against the task ledger,
grouped into four buckets:

- **Awaiting review** — finished work, not yet merged or rejected. Gets
  Review / Open PR / Mark merged / Reject actions, same as Ships.
- **Merged** — the branch's tip is a git ancestor of the base branch (or it
  was marked merged). Reference only; safe to bulk-delete.
- **Orphan** — no live ledger entry points at this branch anymore (e.g. a
  task was re-run and produced a new timestamped branch) and it isn't merged.
  Re-run timestamp branches from a retried task show up here once the old
  branch is superseded.
- **Rejected** — explicitly rejected. Safe to bulk-delete.

Merge status is computed from **local git ancestry against the configured
base branch**, refreshed on an internal fetch every ~60s — it stays accurate
even while the autopilot loop is paused, because it doesn't depend on the
loop running.

**Delete guardrails:** only `autopilot/*` branches are ever eligible for
deletion (never the base branch or an unrelated branch). Bulk delete only
targets the **merged** or **rejected** buckets — there's no bulk-delete for
awaiting/orphan. Deleting an unmerged branch (awaiting or orphan) always
requires a per-branch confirm with an extra warning that its work may be
unreviewed.

## Safety facts

- Every task ships on its own branch; nothing merges without a human click.
- Merged ships have one-click Revert.
- The robot refuses auth, payments, migrations, secrets, and architecture
  decisions — those escalate to you instead.
- Park / Bring back / Answer are reversible. Nothing here deploys anywhere.
