# Orbit Branch-Management Tab — Rationale Record

**Shipped:** 2026-07-10 · **Repo:** `~/master/orbit` (engine + dashboard) · commits `bbcfc1d..21bace2`
**Status:** Done. This is the why-and-what-must-hold record; the code is the source of truth for how.
Build history (TDD steps, exact code) is preserved alongside in `2026-07-10-branch-management-tab-plan.md`.

## What shipped

The orbit dashboard has a **Branches** tab (`⎇ Branches` in the nav). It reconciles the loop's
ledger against every `autopilot/*` branch on origin and sorts each into one of four buckets —
**awaiting review / merged / orphan / rejected** — with per-branch and bulk **delete**. It is the
git-hygiene lens; the pre-existing **Ships** tab stays the task-review lens (ledger-only).

Entry points: `branch_reconcile()` + `remote_branches()` + `trunk_ancestry()` + the `"branches"`
block in `build_state()` (`engine/command_center.py`); `renderBranches()`/`branchRow()` and the
`view-branches` section (`engine/cc_shell.html`). User guide: the "Branches tab" section of
`docs/OPERATOR-GUIDE.md`.

## The problem it solves

One `autopilot/task-<id>` branch ships per task, so branches pile up and the operator can't tell
which are merged, which await review, and which are junk. The old Ships tab is ledger-only, so
branches that exist on origin but aren't the ledger's current ref (orphans, re-run duplicates) are
invisible on the dashboard while still cluttering Bitbucket — "I'm confused which are merged." There
was also no delete/cleanup lever anywhere. This tab is the single screen that reconciles ledger
against git reality and lets the operator prune.

## Why it works this way (and not the alternatives)

- **Local git ancestry, no Bitbucket API.** Merge status is "is this branch tip an ancestor of
  `origin/<BASE_BRANCH>`?" computed from local refs (`trunk_ancestry()`), reusing the proven
  `merged_map()` technique. Bitbucket API credentials aren't available in this environment, and the
  loop already `git fetch`es every cycle — ancestry is sufficient and needs nothing new. The tab
  adds its own cached `git fetch --prune` so status stays correct even while the loop is paused.
- **Per-task-branch model kept, not replaced.** Batching tasks onto a shared branch was considered
  and rejected: the operator chose "manage the branches we have" over "make fewer branches." This
  tab manages; it does not change how `run.sh` creates branches.
- **Manual, one-click cleanup — never automatic.** No auto-prune of merged branches. Deletion is an
  operator action so control stays with the human.
- **A new tab, not an extension of Ships.** Ships answers "what did each task ship, and do I
  merge/reject it?" (task-centric). Branches answers "what branches exist on origin, and which are
  junk?" (git-centric). Different questions, different lenses; they share the ledger and the
  merge/reject/review actions.

## Invariants (must stay true)

1. **Deletion is prefix-scoped.** `do_delete_branch` refuses any branch not under `PREFIX + "/"`
   (`autopilot/`) before touching git. Named/team branches (`main`, `feature/*`, deploy pointers)
   can never be deleted from this UI. This encodes the standing branch-deletion policy.
2. **Bulk delete is server-classified, never client-trusted.** `do_delete_branches_bulk(kind)`
   accepts only `kind ∈ {merged, rejected}`, recomputes `branch_reconcile(...)` server-side, and
   deletes only branches the server itself put in that bucket (and under the prefix). Awaiting and
   orphan branches — anything possibly-unmerged-and-unreviewed — are never in a bulk sweep; they are
   per-branch delete + confirm only.
3. **Delete routes live inside the CSRF/origin gate.** `/delete-branch` and `/delete-branches-bulk`
   sit inside `do_POST`, after the same `_host_ok()`/`_origin_ok()` + `X-CC-Token` HMAC check every
   other POST action passes.
4. **`branch_reconcile` is pure.** All state (branch list, ancestry set, ledger, now-timestamp) is
   passed in; it does no git/filesystem/clock IO. That is what makes it unit-testable and is why the
   tests are real rather than mocks.
5. **Ledger keys are bare task ids.** Ledger entries are keyed `"5"`, `"be-scheduler-…"` — NO
   `task-` prefix. `_task_id_from_branch("autopilot/task-5")` yields `"5"`, and the lookup is
   `ledger.get(tid)`. Prefixing the lookup with `task-` orphans every real branch (see Dead ends).
6. **The render pipeline refreshes on branch changes.** `sig()` in `cc_shell.html` includes
   `r.branches`; `refresh()` only re-renders when `sig` changes. Omit `branches` and the list goes
   stale after a delete/merge-flip (the row lingers, a second delete hits a gone ref) — silently, and
   worst exactly in the paused-loop case the feature advertises.
7. **Caches are 60s-TTL'd** (`_fetch_prune`, `remote_branches`, `trunk_ancestry`), so the 3s
   `/api/state` poll stays cheap; `bust_branch_caches()` clears all three (including the fetch stamp)
   after a delete so the next poll sees fresh remote state.
8. **`BASE_BRANCH` never silently falls back.** `_resolve_base_branch()` reads env override →
   target `.autopilot/config.yaml` → a *loud* stderr warning + `"main"`. A silent `main` fallback
   would make every merge badge lie.

## Divergences from the plan (the part worth keeping)

- **The base branch was already correct.** The plan opened by "fixing" `base_branch` from `main` to
  `feature/funnelhub-auto-loop`. Reality: the active `.autopilot/config.yaml` and the dashboard's
  launchd env were already set to `feature/funnelhub-auto-loop`; only the orbit *schema default*
  (`config/schema.yaml`) said `main`. So Part A shrank from "change the base" to "harden the
  resolution so it can't silently regress to `main` under an unusual launch." Verify before you fix.
- **A test bent the code, briefly.** The first `branch_reconcile` implementation used
  `ledger.get("task-" + tid)` to satisfy a test whose fixture keys were wrongly `task-`-prefixed.
  Against the real ledger that resolves nothing → every branch misclassified as orphan. Fixed by
  reverting to the bare-id lookup and correcting the test fixtures (invariant #5). The lesson:
  fixtures must use the real key format, and a green test proves nothing if it was bent to the code.
- **No JS test harness exists for the dashboard.** The plan assumed a Jest/RTL harness; the dashboard
  is one server-rendered HTML file with inline vanilla JS. UI is verified by curl + a manual browser
  pass; only the Python logic is unit-tested.
- **Merges are recorded, not performed, from this tab.** The plan floated a "Merge into trunk"
  action; `/merge-to-loop` is allowlist-gated to feature-agent builds and *refuses* task branches.
  So awaiting rows use `/mark` (record the outcome) + ancestry auto-detection + the Bitbucket PR
  link — not an in-dashboard merge.
- **Duplicate detection deferred.** Re-run `…-<TIMESTAMP>` branches surface as ordinary orphans
  (deletable per-branch); there is no sibling-aware "delete superseded" bulk path.

## Dead ends (do not re-walk)

- `ledger.get("task-" + tid)` — wrong; ledger keys are bare (invariant #5).
- `/merge-to-loop` for an `autopilot/task-*` branch — refused by its feature-build allowlist.
- Changing the `base_branch` config value — it was already right; the schema default was the red
  herring.
- A `sig()` that omits `branches` — compiles and renders on first load, then silently stops
  refreshing the list after mutations.

## Known, accepted trade-offs

- **Single-branch delete does not server-enforce category.** A valid same-origin+token POST can
  delete an *awaiting* `autopilot/*` branch, bypassing the UI confirm. Deliberate: the confirm lives
  on the UI path, only the operator can mint the request, and the prefix guard still holds. Tighten
  with a server-side category check if that ever needs to be enforced, not just conventioned.
- **`trunk_ancestry()` inherits `merged_map`'s `rev-list -8000` cap.** A branch merged more than 8000
  commits back on the trunk would false-negative as unmerged. Direction is safe (merged work never
  wrongly lands in a bulk-delete set) and recent autopilot branches are unaffected.

## Code & test pointers

- Backend (`engine/command_center.py`): `_resolve_base_branch()`/`BASE_BRANCH`; `PREFIX`,
  `_git()`, `_fetch_prune()`, `remote_branches()`, `trunk_ancestry()`, `bust_branch_caches()`;
  `branch_reconcile()`/`_task_id_from_branch()`/`_TS_SUFFIX`; the `"branches"` decoration in
  `build_state()`; `do_delete_branch()`, `do_delete_branches_bulk()`, and the `/delete-branch` +
  `/delete-branches-bulk` routes in `do_POST`.
- Frontend (`engine/cc_shell.html`): `renderBranches()`, `branchRow()`, `delBranch()`, `delBulk()`,
  the `view-branches` section, the `nb-branches` badge, and `sig()`.
- Tests (`tests/`): `test_base_branch.py` (resolution precedence + loud fallback),
  `test_branch_reconcile.py` (all four categories, bare-id lookup, timestamp-dup → orphan),
  `test_state_branches.py` (`/api/state` decoration), `test_delete_branch.py` (prefix refusal with
  zero git calls, bulk merged/rejected touches only its bucket, invalid-kind refused).
