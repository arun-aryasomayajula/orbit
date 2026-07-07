# FunnelHub — Segment-Axis Attribution, Tier 2

**Status:** Spec — ready for review
**Author:** drafted with Claude, 2026-07-07
**Predecessor:** `docs/features/funnelhub-segment-axis-attribution-tier1.md` (shipped; flag `FF_FUNNELHUB_SEGMENT_ATTRIBUTION`)
**Flag:** `FF_FUNNELHUB_SEGMENT_ATTRIBUTION_T2` (new, default OFF; Tier 1 flag untouched)

Tier 1 shipped: the period-compare "Why did it change?" picker splits the A→B change
by **one** segment, **Trino lane only**; the Druid lane returns `null` for a segment
axis (`funnelhub_core.py:2216-2220`). Tier 2 adds (1) **multi-segment attribution**
and (2) **Druid-lane support with defined degradation**.

Decisions are ordered most-likely-to-change first. The unknowns map (quadrant walk,
run against the code since no user was available) is at [§7](#7-unknowns-map).

---

## 1. The mathematical contract (Decision D1 — most likely to change)

### D1a. The axis is the **joint partition** (cross-product of per-segment partitions)

Each selected segment already induces a disjoint + exhaustive partition of the funnel
population (Tier 1 established this): a plain/behavioral/cohort segment → `{In, Out}`;
a bucketed segment → `{bucket₁…bucketₙ, Other}` (buckets are first-match-wins deduped —
`segment_definition.dedup_buckets`, so disjoint; `bucket_other='show'` forced, so
exhaustive). For N segments, Tier 2's cohort axis is the **cross product of those
partitions** — e.g. segments A and B (both plain) give 4 cells:
`In A·In B`, `In A·Out B`, `Out A·In B`, `Out A·Out B`.

**Why:** the cross product of disjoint+exhaustive partitions is itself disjoint +
exhaustive — the *only* shape for which the existing symmetric rate-vs-mix identity
(`_decompose_from_breakdowns`, `backend/services/funnelhub_service.py:668-750`) is
exact. Overlap between segments (the reason multi-segment was deferred from Tier 1)
is not worked around — it is *represented*: the overlap **is** the `In A·In B` cell.

### D1b. Additivity guarantee — what is exact and what is a view

- **Exact (the contract):** per stage, `rate_effect_pp + mix_effect_pp = total Δpp`,
  where each effect is the sum of per-**cell** contributions. Same identity as
  Tier 1, unchanged math, exact on Trino. On Druid it holds up to theta-sketch
  estimation error (see D4) and is labeled `approximate: true`.
- **Per-segment marginal roll-ups are a VIEW, not an allocation.** "Effect within
  In A" = sum of contributions of all cells whose A-coordinate is In. Marginals
  across *different* segments overlap (the `In A·In B` cell counts toward both) and
  therefore **do not sum to the total**. The UI must label them "within-cohort
  contribution", never stack them into a 100% bar.
- **Interaction effects are NOT separately allocated.** They live inside the joint
  cells, visibly. We considered and rejected:
  - *N independent Tier-1 runs side-by-side:* each axis "explains" 100% of the same
    change independently; overlap double-counts; no joint story. (Still available —
    the user can just run Tier 1 twice.)
  - *Shapley allocation across segments:* exact additive per-segment attribution, but
    needs decompositions over all 2^N sub-partitions and is hard to explain in UI
    copy. With the N=2 cap (D1c) the marginal view carries nearly the same
    information. Revisit if N grows.

### D1c. **Flat, capped at N = 2** (not hierarchical)

- Hard cap: **2 segments** per attribution request (422 above it).
- Rationale: (a) closed-funnel Trino and all Druid paths compute cells by
  scoped-run subtraction — run count is exponential in N (§3, §4); (b) the Trino
  drilldown builder caps at 3 levels anyway (`trino_sql_builder.py:1033-1034`);
  (c) 4–6 cells is the ceiling of what the period-compare panel can present
  legibly. Flat (no L1→L2 hierarchy): the decomposition identity is over one
  partition; hierarchy is a *presentation* concern the marginal view already covers.
- **Cell-count cap: 12 cells** per request (bucketed × plain can reach
  `(buckets+1)×2`). Exceeding → 422 `"Attribution supports at most 12 cohort cells;
  pick a smaller bucketed segment or a single axis."` Never silently truncate —
  dropping cells breaks exhaustiveness and therefore the identity.

### D1d. Per-cell contributions become part of the response

Today `_decompose_from_breakdowns` returns only per-stage aggregates
(`rate_effect_pp`, `mix_effect_pp`); the per-cohort terms are computed in its loop
(`funnelhub_service.py:722-738`) and discarded. Tier 2 adds an opt-in
`include_cohorts=True` that emits, per stage, per cell:
`{label, share_a, share_b, rate_a, rate_b, rate_contrib_pp, mix_contrib_pp}` —
this is what makes multi-segment attribution *actionable* ("biggest mover:
`In Android·Out New users`, −2.1pp rate"). Backward compatible (default off; Tier 1
callers unchanged).

**Composite cell key:** the math keys rows by a single `cohort_column`. Rather than
teach it tuple keys, the route synthesizes one composite label column
(`__seg_axis__` = level values joined with `" · "`) before calling the decomp —
mirrors what the Tier 1 closed path already does (`funnelhub_core.py:2361-2377`).
The math loop stays untouched.

## 2. API shape (Decision D2)

Extend `POST /api/funnelhub/query/mix-decomposition/v2` (no new endpoint — the
frontend already points here with a 5-minute timeout, `funnelhubApi.js:590-615`):

```
?segment_ids=<int>&segment_ids=<int>        # NEW unified axis, repeated, max 2
 trino_segment_id=<int>                     # Tier 1 alias, kept; == segment_ids=[x]
 trino_dimension=<col>                      # unchanged; mutually exclusive with any segment axis
 cohort_column=<alias>                      # Druid *dimension* axis — IGNORED when a segment axis is set
```

- `segment_ids` is **engine-agnostic**: one axis, both lanes resolve it (that's the
  point of Tier 2 — same question answered on both engines). Per-lane axes ("segment
  on Trino, platform on Druid") are rejected as incoherent; the lanes must answer
  the same question or degrade (D4).
- Validation order: flag check (`segment_ids` with >1 entry, or any segment axis
  wanting the Druid lane, requires `FF_FUNNELHUB_SEGMENT_ATTRIBUTION_T2`; a single
  segment still needs Tier 1's flag) → mutual exclusion with `trino_dimension` →
  existence (`dao.get_segment`) → cell-count cap.
- Response envelope unchanged: `{druid, trino, total_execution_ms}`. Each lane entry
  gains `cohorts: [...]` (D1d). The Druid lane entry gains
  `approximate: true` and, when degraded, becomes
  `{skipped: true, reason: "<human message>", unsupported_segments: ["name", ...]}`
  instead of bare `null` — the UI needs the *why* (D4c).

## 3. Trino lane, multi-segment (Decision D3)

### Open funnels — one query per period, N drill levels (already mechanically supported)

`build_segment_drilldown_sql` accepts up to 3 levels and already handles **multiple
membership levels** — each gets its own `seg_lvl_K` cohort CTE LEFT JOINed for its
In/Out column (`trino_sql_builder.py:1190-1220`), and bucketed levels emit CASE
columns. Tier 2 passes `levels=[level_for(seg1), level_for(seg2)]` (exact same level
construction as Tier 1, `funnelhub_core.py:2297-2321`) and GROUP BY gives the joint
cells directly. Still **2 queries per request** (one per period) — no new SQL method.
Rows carry N level columns; the route composites them into `__seg_axis__` (D1d).

### Closed funnels — inclusion–exclusion over scoped runs

The drilldown builder can't do the closed cumulative-AND (Tier 1 hit this too). Tier 1
ran `In = scoped run`, `Out = total − In`. For two plain segments, per period run
**4 funnels**: `total` (unscoped), `A` (scoped seg1), `B` (scoped seg2), `AB` (scoped
to both — `make_trino_runner`'s `segments` list already INNER-JOIN-intersects N
cohorts). Cells by inclusion–exclusion:

```
In A·In B  = AB                    In A·Out B = A − AB
Out A·In B = B − AB                Out A·Out B = total − A − B + AB
```

8 runs per request (4 × 2 periods), all independent → bounded concurrent fan-out
(semaphore of 4, matching Tier 1's pattern and the async guardrails; each run through
`fh_funnel_cache`, so repeat attributions are cheap). Clamp every derived cell ≥ 0
(scope-skew protection, as Tier 1 does at `funnelhub_core.py:2370`) — **note:**
clamping trades exact additivity for sanity; when a clamp fires, set
`clamped: true` on the lane so the UI can show a precision hint instead of users
discovering rate+mix ≠ total.

Bucketed × anything on **closed** funnels: each bucket is a scoped run
(`total, b₁…bₙ, B2, b₁∩B2…`) → run count `2(buckets+1)` per period. The 12-cell cap
(D1c) bounds this at ~14 closed runs worst case; acceptable only because of the
Trino cache — if review disagrees, restrict closed funnels to plain×plain.

## 4. Druid lane (Decision D4)

### D4a. Mechanism: scoped-run fan-out + subtraction (no new SQL shapes)

Druid **cannot GROUP BY the counted entity** — `pulse-production` stores
customers/sessions as `COMPLEX<thetaSketch>` (`druid_sql_builder.py:47-53, 676-681`),
and the Druid builder has no CASE-projection support (`build_breakdown_sql` emits
plain column refs only, `:572`). So the Trino open-funnel trick (one exhaustive
GROUP BY query) is unavailable. Instead, reuse the closed-Trino Tier 1 pattern on
Druid, where it's cheap (runs are ~1–2s, uncached by design —
`trino_funnel_cache.py:18`):

- Per period, run `service.execute_query` once per **inclusion term**: `total`,
  each `In`/bucket scope, and each pairwise intersection (`_apply_cohort_scope`
  already intersects N cohorts — the 2026-06-15 generalization,
  `funnelhub_service.py:127-177`). Derive `Out`/`Other`/mixed cells by subtraction,
  clamped ≥ 0. Same formulas as §3-closed; works uniformly for open AND close Druid
  funnels (both are just scoped `execute_query` runs), and avoids needing predicate
  *negation* (NOT-regex etc.) entirely.
- Run counts: single plain = 4; single bucketed = `(buckets+2)×2`; two plain = 8;
  bucketed×plain ≤ `2(buckets+1)×2` — all within the 12-cell cap. Fan out via
  `asyncio.to_thread` under a semaphore (6), each with a per-run timeout; total
  lane deadline stays inside the dispatcher-style 300s Druid budget
  (`config.py:127`).
- Feed the synthesized per-cell rows into the same `_decompose_from_breakdowns`
  with `cohort_column="__seg_axis__"`.

### D4b. Theta-sketch approximation is disclosed, not hidden

Every Druid count is an `APPROX_COUNT_DISTINCT_DS_THETA` / sketch-intersection
estimate. Subtraction of estimates (`Out = total − In`) compounds relative error,
and mix effects are often sub-1pp — small enough to be swamped by sketch noise on
big cohorts. Contract: the Druid lane always returns `approximate: true`; UI renders
a "~" prefix and a tooltip ("Realtime attribution is sketch-estimated; Historical is
exact"). We do **not** attempt error bars in Tier 2 (sketch stddev isn't exposed
through the current executor).

### D4c. Degradation: **all-or-nothing per lane**, with a reason

Eligibility is decided per segment via the v2-aware
`effective_engines(resolve_definition(seg))` (`segment_definition.py:511-524`) —
**not** the legacy empty-`filters` heuristic `_apply_cohort_scope` warns on. A
segment is Druid-eligible iff `"druid" ∈ effective_engines` (attribute rules with
`druid_column`, incl. bucketed). Behavioral (INTERSECT/UNION/EXCEPT trees) and
cohort-backed segments are Trino-only *by construction* (`:257-261`).

If **any** selected segment is Druid-ineligible, the **whole Druid lane degrades**:
`{skipped: true, reason: "Realtime attribution unavailable: segment 'New users
(behavioral)' can only be evaluated on the Historical lane.", unsupported_segments:
[...]}`. We explicitly reject partial degradation (dropping the ineligible segment
and attributing by the other alone on Druid): the two lanes would then answer
*different questions* under one picker selection — worse than an honest "not
available here". The Trino lane proceeds regardless.

## 5. UX flow (Decision D5)

- **Picker** (`ResultsArea.jsx` `PeriodCompareLanes`, `:848-873`): the Segments
  combobox becomes a **multi-select capped at 2** (chips). Selecting any segment
  still clears the dimension axis and vice-versa (mutual exclusion preserved).
  Gate: 1 segment → Tier 1 flag (existing `segment_attribution_enabled` on
  `/trino-dimensions`); 2 segments → new
  `segment_attribution_t2_enabled` capability on the same response.
- **Results** stay in the per-lane Druid|Trino tabs. New per-stage expandable cell
  table (from D1d `cohorts`): label, share A→B, rate A→B, rate/mix contribution,
  **biggest-mover flag** on the largest |contribution| (reuse the compare-breakdown
  biggest-mover convention). Marginal per-segment view is a client-side aggregation
  of the same rows — labeled "within cohort", never stacked to 100%.
- **Realtime tab, degraded:** show the backend `reason` verbatim where the
  attribution table would be — not an empty state.
- All existing states handled: loading (per lane), error (`{error}` entry), degraded
  (`{skipped, reason}`), empty (no eligible axis → section hidden).

## 6. Data model, config, and mechanical work

**No DB schema change.** Segments (`fh_segments.definition`, migrations 003/057/061)
already carry everything needed.

Backend
- `backend/config.py` — `FF_FUNNELHUB_SEGMENT_ATTRIBUTION_T2` (default False).
- `backend/routes/funnelhub_core.py::mix_decomposition_v2` (~2133) —
  `segment_ids` param + validation ladder (D2); generalize the Tier 1 segment branch:
  build N levels (open Trino), inclusion–exclusion runner fan-out (closed Trino, §3),
  new `_druid_segment_decomp` (D4a) replacing the unconditional `return None`;
  composite `__seg_axis__` synthesis; eligibility check via `effective_engines`.
- `backend/services/funnelhub_service.py::_decompose_from_breakdowns` (~668) —
  optional `include_cohorts` per-cell output (D1d). No math change.
- `/trino-dimensions` response (~3579) — add `segment_attribution_t2_enabled`.
- Guardrails compliance: every fan-out bounded (semaphore) + per-task `wait_for` +
  total deadline; all DB/Trino/Druid calls under `asyncio.to_thread`; no state on
  shared builders (fresh runner per call, as Tier 1 does); identifiers already
  validated by the level-construction path.

Frontend
- `ResultsArea.jsx` — multi-select picker, cell table, degraded-reason rendering.
- `FunnelDetailPage.jsx::handleFetchPeriodMixDecomp` (~401) +
  `funnelhubApi.js::executeMixDecompositionV2` (~590) — `segmentIds: number[]`
  (keep `trinoSegmentId` back-compat).

Tests (each lands with its change; run `pytest` + `CI=true npm test -- --watchAll=false`)
- **Identity check per stage** for cross-product cells (open Trino, 2 membership
  levels): `rate+mix = total Δpp` — the core guarantee, over a synthetic fixture.
- Inclusion–exclusion cell derivation incl. negative-clamp case (+ `clamped: true`).
- Druid fan-out: run-count per shape (4/8/bucketed), semaphore bound, per-run
  timeout, `approximate: true` always set.
- Eligibility: behavioral + attribute pair → Druid `{skipped, reason}` while Trino
  succeeds; both-eligible → both lanes populated.
- Validation: >2 segments → 422; >12 cells → 422; segment axis + `trino_dimension`
  → 422; T2 flag OFF → multi-segment and Druid lane both refused, Tier 1 behavior
  byte-identical.
- Frontend: multi-select cap, mutual exclusion, degraded-reason rendering, cohorts
  table render, flag-off = Tier 1 UI unchanged.

Rollout: ship dark → dogfood funnel 5 (Recharge, Druid-live; segments 2×10 already
live-validated as an intersection pair on 2026-06-15) and funnel 8 (closed, Trino)
with a hand-computed identity check → stage → prod. Update
`docs/features/funnelhub.md` + the Tier 1 doc's "Out of scope" section on ship;
record `docs/decisions/DECISION_segment_attribution_tier2.md`.

## 7. Unknowns map

Quadrant walk run against the code (autonomous — assumptions stated where a human
answer is missing).

**Known knowns** (verified, file:line)
- Decomposition math is axis-agnostic, symmetric-weighted, exact only for
  disjoint+exhaustive partitions; returns aggregates only
  (`funnelhub_service.py:668-750`).
- Trino drilldown builder: ≤3 levels; multiple membership levels & CASE-bucket
  levels supported → joint cells in one query (`trino_sql_builder.py:1033,
  1190-1220`).
- Closed-Trino Tier 1 precedent: scoped+total runs, `Out = total − In`, clamp,
  bounded gather (`funnelhub_core.py:2327-2378`); runner `segments` list intersects.
- Druid: no GROUP BY on sketches, no CASE in builder; segments = WHERE predicates;
  `_apply_cohort_scope` intersects N cohorts; drilldown fan-out precedent
  (`DRILLDOWN_PARALLELISM=12`, `MAX_COMBINATIONS=50`); Druid uncached, Trino cached.
- Segment model v2: `effective_engines`, `to_druid_filters`, first-match-wins
  bucket dedup; behavioral/cohort = Trino-only by construction
  (`segment_definition.py`).
- Frontend picker is single-select with dimension mutual exclusion; capability flag
  rides `/trino-dimensions`; 5-min axios timeout.

**Known unknowns → resolved by reading**
- *Can the builder host two membership levels in one query?* Yes (`seg_lvl_K` list).
- *Does the decomp expose per-cohort terms?* No — D1d adds it.
- *Can closed-Trino scope two segments in one run?* Yes (INNER-JOIN intersection).
- *Does Druid scope intersect N cohorts?* Yes (2026-06-15 generalization).
- *How to detect Druid-ineligible segments?* `effective_engines` (v2-aware), not the
  legacy empty-`filters` warning.

**Unknown knowns (assumptions I'm making — flag if wrong)**
- Theta-sketch error on pulse-production is small enough that sketch-estimated
  attribution is *useful*, given disclosure (D4b). Not measurable from the repo.
- The Trino warehouse tolerates ≤14 concurrent-ish closed-funnel runs per
  attribution click (cache-assisted). Based on Tier 1's accepted 4-run cost.
- Users can read 4–6 cross-product cell labels (`In A·Out B`). Based on the
  compare-drill table precedent; no user research.

**Unknown unknowns surfaced by the walk (landmines now on the map)**
- Single-`cohort_column` keying vs N level columns → composite-key synthesis (D1d);
  would have silently keyed on one level and produced wrong-but-plausible numbers.
- Clamping derived cells breaks the exact-additivity promise → `clamped: true`
  disclosure rather than silent violation.
- `cohort_column="platform"` default: with a segment axis the Druid lane must run
  *segment* attribution, not silently fall back to the platform dimension.
- Cell-count explosion for bucketed×bucketed → hard 12-cell cap with 422, never
  truncation (truncation breaks exhaustiveness → invalid identity).
- Partial Druid degradation would make the two lanes answer different questions →
  all-or-nothing per lane (D4c).
- Empty cells (a cohort present in period A only) are already handled — the decomp
  unions keys across periods and treats missing as 0 (`funnelhub_service.py:701`).

## 8. Open questions needing a human

1. **Per-segment attribution semantics** — is the marginal (non-additive) view
   enough, or does product want Shapley-style additive allocation? (Changes D1b/D1d
   and all UI copy.)
2. **Cost envelope** — confirm N=2 + 12-cell caps against warehouse/broker budget;
   whether closed-funnel bucketed pairs should be excluded.
3. **Is approximate Druid attribution worth showing at all**, given sketch noise vs
   typically sub-1pp mix effects — or should Realtime stay a degraded lane until we
   can bound the error?
