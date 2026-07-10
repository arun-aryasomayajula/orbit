# Orbit Branch-Management Tab — Design

**Date:** 2026-07-10
**Status:** Approved (design) — pending implementation plan
**Repo:** `~/master/orbit` (engine + dashboard); target repo `cdp-metaql`
**Author:** Arun + Claude

## Problem

Orbit ships one `autopilot/task-<id>` branch per task. In practice this produces a
pile of branches that are hard to track and merge, and the operator can't tell at a
glance which are merged, which await review, and which are junk. Measured on
2026-07-10 against `cdp-metaql`:

- **16** `autopilot/*` branches on origin; **15** in `pushed` state (awaiting review).
- Ledger totals: 68 merged, 15 pushed, 4 rejected, 11 escalated, 3 in_progress, 1 committed.
- **2** branches are re-run duplicates (`…-<TIMESTAMP>`): when a task is re-run and the
  clean branch name already exists on origin, `run.sh` pushes a second timestamped
  branch (`engine/run.sh` ~line 200), and the ledger only records the latest ref — so the
  older branch becomes an untracked orphan. (Special duplicate detection is deferred — see
  Non-goals; these simply surface as orphans in this iteration.)

### Root causes

1. **No git-reality view.** The existing **Ships** tab is ledger-only. It cannot show
   branches that exist on origin but aren't the ledger's current ref (orphans, re-run
   duplicates), so the dashboard's picture disagrees with what the operator sees in
   Bitbucket. That gap — branches visible in Bitbucket but absent/unlabeled on the
   dashboard — is the core of "I'm confused which are merged."
2. **No cleanup lever.** There is no delete-branch action and no bulk operations, so merged
   and rejected branches accumulate; nothing prunes them.
3. **Volume.** 15 branches sit in `pushed` (awaiting review) with no bulk review/merge/delete,
   so working through them is one-at-a-time and easy to lose track of.

### What is NOT the problem (verified 2026-07-10)

The base / merge target is **already correct**. `.autopilot/config.yaml:3` sets
`base_branch: feature/funnelhub-auto-loop`; `run.sh` bases worktrees on it, and the dashboard
resolves the same value (launchd sets `ORBIT_BASE_BRANCH=feature/funnelhub-auto-loop`; live
`/api/state` reports `base_branch: feature/funnelhub-auto-loop`). So `merged_map()` already
measures "merged?" against the trunk the operator actually integrates into — merge badges on
Ships are correct. (Earlier notes that `base_branch` was `main` referred to the orbit
*schema default* in `config/schema.yaml:5`, not the active target config.)

There is, however, one **latent** robustness bug: `command_center.py:77` resolves
`BASE_BRANCH` from env with a hardcoded `"main"` fallback. If the dashboard is ever launched
without `ORBIT_BASE_BRANCH`/`AP_BASE_BRANCH` (e.g. run by hand), merge detection silently
reverts to `main` and every badge goes wrong. Part A hardens this.

## Goals

- Keep merge status **trustworthy** — it is already measured against the right trunk; harden
  the resolution so it can't silently fall back to `main`.
- Give one screen — a new **Branches** tab — that reconciles the ledger against every
  `autopilot/*` branch on origin and categorizes each as awaiting-review / merged /
  orphan / rejected.
- Provide safe **cleanup**: per-branch and bulk delete of remote branches, hard-scoped to
  the `autopilot/*` prefix.
- Work with **no Bitbucket API** — pure local git ancestry (the loop fetches each cycle;
  the tab refreshes on its own too).

## Non-goals (YAGNI)

- No Bitbucket API integration (credentials are not available in this environment; ancestry
  is sufficient and already proven by `merged_map()`).
- No task-batching / shared integration branch — the per-task-branch model stays; this
  design manages it, it does not replace it.
- No *automatic* branch deletion. Cleanup is one-click manual so the operator stays in
  control. (An opt-in auto-prune-merged flag may come later; out of scope here.)
- No change to how branches are *created* (no force-push of the original on re-run —
  that would violate the no-force-push rule).
- No special **duplicate** detection/labeling or "delete superseded" bulk path (deferred).
  Re-run duplicates surface as ordinary orphans and are deletable per-branch. A future
  iteration may add sibling-aware detection and, separately, fix `run.sh` to stop creating
  the timestamped second branch.

## Design

### Part A — Foundation: make merge-target resolution robust

The active base is already `feature/funnelhub-auto-loop` (see "What is NOT the problem"), so
no behavior change is needed — but the resolution is fragile. Harden `command_center.py:77`:
read `base_branch` from the target's `.autopilot/config.yaml` as the source of truth, using
the env var only as an override, and drop the silent `"main"` fallback (if nothing resolves,
log a loud warning rather than defaulting to a branch the operator doesn't use). This mirrors
how `run.sh` already sources the value from config, so the dashboard and the loop can never
disagree on which trunk merge status is measured against.

Small, defensive, and independent of the tab; landing it first guarantees the Branches tab's
merge badges are computed against the right trunk under every launch path.

### Part B — The reconciler (backend, pure)

A new function (proposed `branch_reconcile()` in `command_center.py`) returns one row per
`origin/<prefix>/*` branch, joining git reality to the ledger. It must be **pure/injectable**:
it takes the branch list, the ancestry set, and the ledger as inputs so it can be unit-tested
without git or network.

Branch row schema:

```
{
  "branch":        "autopilot/task-fe-datafilter-custom-option-dead",
  "task_id":       "fe-datafilter-custom-option-dead",   # name minus prefix + minus -<TS> suffix
  "tip":           "<sha>",
  "merged":        true|false,        # tip is ancestor of origin/<base_branch>, OR ledger state==merged
  "ledger_state":  "pushed"|"merged"|"rejected"|"committed"|"escalated"|null,
  "is_current_ref":true|false,        # matches the ledger entry's remote_ref for this task
  "is_orphan":     true|false,        # on origin but no ledger entry (or not the current ref)
  "age_days":      3,                 # from tip commit date
  "has_packet":    true|false,        # reviews/task-<id>.md exists
  "pr_url":        "<bitbucket compare link>"|null
}
```

Category derivation (mutually exclusive display buckets, evaluated in order):

1. **awaiting-review**: `ledger_state == "pushed"` and not `merged`.
2. **merged**: `merged == true`.
3. **rejected**: `ledger_state == "rejected"` and not merged.
4. **orphan**: `is_orphan` (and not caught above). Re-run timestamp-dupes fall here.

Freshness: the tab triggers a cached (~60s TTL, same cadence as `merged_map`) refresh that
runs `git fetch --prune origin` before enumerating. `--prune` drops local stale
remote-tracking refs for branches already deleted on origin; it never deletes remote
branches. This keeps the view accurate even while the loop is paused.

Merge detection reuses the `merged_map()` technique — one `git rev-list origin/<base_branch>`
membership check — extended to test each **branch tip** (not just ledger shas).

### Part C — The Branches tab (frontend)

New nav entry `⬀ Branches` in `cc_shell.html` (`nav` block, ~line 334), between Ships and the
Fleet group, with a count badge = awaiting-review + orphans (the actionable set).

Rendered as four collapsible sections mirroring the categories, newest-actionable first:

- **Awaiting review** (N) — per row: age, `🔍 Review` (packet), `Open PR`, `Merge`, `Reject`,
  `Delete`.
- **Merged** (N) — per row: `Delete`. Section header: **Delete all merged** button.
- **Orphans** (N) — branches on origin not tracked as the ledger's current ref (includes
  re-run timestamp-dupes). Per row: `Delete` with confirm — these may be unmerged, unreviewed
  work, so there is **no bulk delete** here (consistent with guardrail D#3).
- **Rejected** (N) — per row: `Delete`. Section header: **Delete all rejected** button.

Ships tab is unchanged (task-review lens); Branches is the git-hygiene lens. They share the
ledger and the merge/reject/review actions; only Branches adds delete + reconciliation.

### Part D — Delete endpoint + guardrails (backend)

New POST route (proposed `/delete-branch`, same handler pattern and origin/host checks as
the existing POST routes in `do_GET`/`do_POST`). Runs `git push origin --delete <branch>`.

**Hard guardrails (non-negotiable — encode the standing branch-deletion policy):**

1. The branch name **must** match `^<ORBIT_BRANCH_PREFIX>/` (i.e. `autopilot/`). Any other
   branch is rejected outright — named/team branches (`main`, `feature/*`, deploy pointers)
   can never be deleted from this UI by construction.
2. **Bulk** delete endpoints (`delete all merged` / `delete all rejected`) may only delete
   branches the reconciler classified as **merged** or **rejected** respectively — verified
   server-side, not trusted from the client.
3. Any branch whose work could be **unmerged and unreviewed** — an awaiting-review branch or
   an orphan — is deletable only via the **per-branch** `Delete` with an explicit confirm; it
   is never included in any bulk sweep.
4. On successful delete, the reconciler cache is invalidated so the row disappears on the
   next poll.

## Data flow

```
run.sh (each cycle)         command_center (dashboard)
  fetch origin  ───────────▶  git ls-remote origin autopilot/*      ┐
  reset WT to base            git rev-list origin/<base_branch>      ├─ branch_reconcile(branches, ancestry, ledger)
  build → commit → push       load ledger.json                      ┘        │
  ledger.py pushed/…                                                          ▼
                                                          /api/state → { branches: [...], counts }
                                                                              │
                              Branches tab renders 4 sections ◀───────────────┘
                              operator clicks Delete / Merge / Reject
                                    │
                              POST /delete-branch (guarded)  →  git push origin --delete <autopilot/...>
                              POST /mark, /merge (existing)  →  ledger + merge into <base_branch>
```

## Components & interfaces

| Unit | Location | Responsibility | Depends on |
| --- | --- | --- | --- |
| `BASE_BRANCH` resolution | `command_center.py:77` | Read trunk from target config; no silent `main` fallback | `.autopilot/config.yaml` |
| `branch_reconcile()` | `command_center.py` | Pure: (branches, ancestry, ledger) → categorized rows | ledger, git plumbing (injected) |
| refresh + cache | `command_center.py` | `git fetch --prune`, `ls-remote`, `rev-list`; 60s TTL | git |
| `/delete-branch` route | `command_center.py` | Guarded remote-branch delete (single + bulk) | `branch_reconcile`, git |
| Branches tab | `cc_shell.html` | Render 4 sections + actions | `/api/state.branches` |

## Testing

Backend (pytest-style, no git/network — inject inputs):

- `branch_reconcile`: given a synthetic branch list + ancestry set + ledger, asserts correct
  category for each fixture — pushed-unmerged→awaiting, ancestor-of-base→merged,
  on-origin-no-ledger (incl. a `-<TS>` re-run branch)→orphan, marked-rejected→rejected.
- Delete guardrail: rejects any name not under `autopilot/`; bulk-merged rejects a branch not
  in the merged set; awaiting-review and orphan branches are excluded from every bulk path.

Frontend: existing Jest/RTL harness for the tab — renders the four sections from a mocked
`/api/state`, bulk buttons call the right endpoint with the right branch set, and the delete
confirm gate fires for unmerged branches.

## Rollout

1. Land Part A — harden `BASE_BRANCH` resolution (read target config, drop silent `main`
   fallback). Independent and low-risk; guarantees the badges are computed against the right
   trunk under every launch path before the tab depends on them.
2. Land backend reconciler + delete route + tests.
3. Land Branches tab.
4. Update `docs/OPERATOR-GUIDE.md`: new tab, delete guardrails, how merge status is derived.

## Open questions

None blocking. Possible follow-ups (out of scope): opt-in auto-prune of merged branches after
N days; surfacing the same delete action inside Ships.
