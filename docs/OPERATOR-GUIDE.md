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

## Safety facts

- Every task ships on its own branch; nothing merges without a human click.
- Merged ships have one-click Revert.
- The robot refuses auth, payments, migrations, secrets, and architecture
  decisions — those escalate to you instead.
- Park / Bring back / Answer are reversible. Nothing here deploys anywhere.
