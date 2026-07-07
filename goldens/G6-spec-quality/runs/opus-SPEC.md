# FunnelHub — Segment-Axis Attribution, Tier 2

**Status:** Spec — pre-implementation (scoping only; no code approved yet)
**Author:** drafted with Claude (Opus 4.8), 2026-07-07
**Depends on:** Tier 1 (shipped) — `docs/features/funnelhub-segment-axis-attribution-tier1.md`
**Flag:** reuse `FF_FUNNELHUB_SEGMENT_ATTRIBUTION` (default OFF); Tier 2 rides the same gate.
**Scope:** (1) multi-segment attribution over N segments at once; (2) Druid-lane support with graceful degradation for Trino-only cohorts.

> Read Tier 1 first. This spec assumes you understand `_decompose_from_breakdowns`
> (`backend/services/funnelhub_service.py:668`), the Tier-1 branch of
> `mix_decomposition_v2` (`backend/routes/funnelhub_core.py:2250-2443`),
> `build_segment_drilldown_sql` (`backend/funnelhub/trino_sql_builder.py:976`),
> `TrinoDrilldownLevel`, the `segment_definition` module, and the Druid
> `SegmentDrilldown` per-cohort fan-out (`backend/services/funnelhub_drilldown.py`).

---

## 0. TL;DR of the decisions (read this, then the sections that scare you)

1. **Math contract — flat cross-classification with an explicit interaction term.**
   The *joint* rate/mix identity (`total = rate + mix`, per stage) is preserved
   exactly by decomposing over the **product partition** of the N segments (their
   In/Out or bucket cross-product), reusing Tier-1 math verbatim. **Rate is
   reported once, at the joint level** (it is a within-cell quantity — attributing
   it to an axis is meaningless). **Mix is attributed per segment** as each
   segment's Tier-1 marginal mix, plus a named **`interaction_pp`** residual so
   that `mix = Σ segment_mix + interaction` holds *by construction*. Flat, not
   hierarchical (no arbitrary axis ordering). Additivity: **guaranteed at both
   levels.** (§3)

2. **N is capped at 2 for v1** (stretch: 3 behind the existing `MAX_LEVELS`/cell
   caps). The product partition is `Π(bucket_count_k)` cells; cost and legibility
   both blow up past 2. (§3.4, §6)

3. **API — extend `mix_decomposition_v2` in place.** `trino_segment_id: int` →
   also accept `trino_segment_ids: list[int]`. Single-int stays for Tier-1
   back-compat. Druid lane is driven by the **same** segment id list (no separate
   Druid axis param). (§2)

4. **Response — additive superset of Tier-1.** Each per-stage entry keeps
   `total_pp / rate_effect_pp / mix_effect_pp`, and gains
   `segment_effects: [{segment_id, segment_name, mix_effect_pp}]` +
   `interaction_pp`. Tier-1 (single segment) responses are unchanged except
   `segment_effects` has one entry and `interaction_pp == 0`. (§2.2)

5. **Druid support — per-cell funnel fan-out + total-subtraction residual**, the
   same shape as the closed-funnel Trino path already in the route. (§4)

6. **Degradation — per-lane, per-segment, soft.** If *any* selected segment is
   not Druid-expressible (behavioral / cohort / regex-with-no-druid-binding),
   the **Druid lane returns `null` with a structured `degraded` reason** while
   Trino still computes. Never a hard 500. Symmetric on the Trino side for the
   rare Trino-inexpressible case. (§4.3)

---

## 1. Unknowns map (quadrant walk, resolved against the code)

I ran the blindspot pass myself (no user available). Each unknown is resolved by
reading the repo, or the assumption I'm proceeding on is stated.

### Known knowns (verified in code)
- Tier-1 single-segment attribution works via two Trino paths: **open funnels** →
  one exhaustive `build_segment_drilldown_sql` level (bucketed CASE, or In/Out
  membership CTE); **closed funnels** → 4 scoped/unscoped `make_trino_runner`
  runs, `Out = total − In` (`funnelhub_core.py:2286-2378`).
- `_decompose_from_breakdowns` is **axis-agnostic and partition-agnostic**: it
  keys rows by a single `cohort_column`, unions bucket keys across periods, and
  the symmetric avg-weight form makes `rate + mix = total` with no residual for
  *any* disjoint+exhaustive partition (`funnelhub_service.py:705-750`).
- The Druid lane already fans out **one funnel run per cohort** with row-scope
  predicates, bounded by `ThreadPoolExecutor(DRILLDOWN_PARALLELISM=12)` and
  `MAX_COMBINATIONS=50` (`funnelhub_drilldown.py`). This is the exact primitive
  Druid Tier-2 needs.
- Segment expressibility is introspectable *before* running:
  `segment_definition.effective_engines(defn)` returns `{druid?, trino?}`;
  behavioral/cohort kinds are Trino-only by construction
  (`segment_definition.py:511-524`, `703-704`).
- `build_breakdown_sql` already accepts a **list** of `dimensions` (multi-column
  GROUP BY), and `build_segment_drilldown_sql` accepts **up to 3 `levels`**
  (`trino_sql_builder.py:976`, `:1033`) — the product partition is a first-class
  concept already.
- Tier-1 Druid returns `null` for a segment axis today; the frontend already
  renders "Historical only" from that (`ResultsArea.jsx:671-763`), and the axis
  selector already carries a mutual-exclusion + reset-on-new-envelope discipline.

### Known unknowns (resolved by reading; assumption stated where not)
- **Does the Druid theta-sketch funnel support GROUP BY a membership axis?** No —
  Druid can't GROUP BY sketches (`funnelhub_service.py:817-823`); that's *why* the
  Druid path must fan out per cell. **Resolved: fan-out, not GROUP BY.**
- **How is an "Out"/residual cell expressed on Druid?** Two options: negate the
  predicate, or `total − Σ(In cells)`. Tier-1 closed-Trino already chose
  total-subtraction and it composes cleanly across N. **Decision: total-subtraction
  residual** (§4.2) — avoids relying on correct negation of regex/`IN`/null ops.
- **Can two arbitrary segments be cross-classified without overlap breaking the
  identity?** Yes — treat each segment as **In/Out membership** (a Boolean), so
  the product of N Booleans is `2^N` disjoint+exhaustive cells *by construction*,
  regardless of overlap. Overlap is not a problem; it just populates the A∩B cell.
  **Resolved.**
- **Does `_decompose_from_breakdowns` need changes for a composite cohort key?**
  No — it keys on one string column. **Assumption: synthesize a single composite
  cohort column** (e.g. `"In A | Out B"`) for the joint decomposition, and
  marginalize (collapse) to per-segment columns for the per-axis mix. Verified the
  function tolerates arbitrary string keys and a `__residual__`-style key already.
- **N-segment cap?** No hard product cap exists for attribution specifically;
  drilldown caps at `MAX_LEVELS=3` / `MAX_COMBINATIONS=50`. **Decision: cap N=2 in
  v1** (§3.4).
- **Where does per-segment eligibility surface to the picker?** Tier-1 added
  `segment_attribution_enabled` to the `/trino-dimensions` response and fed the
  picker `segmentOptions`. **Assumption: extend the same response** with a
  per-segment `engines` capability so the UI can show which axes the Druid lane
  will honor *before* running (§4.3, §5).

### Unknown knowns (things the codebase already decided that constrain us)
- **The symmetric (Bennet/avg-weight) decomposition is already the house style** —
  don't introduce a Laspeyres/Paasche base-period form; it would break the
  no-residual property other callers rely on.
- **Fresh builder/runner per call is mandatory** — `build_funnel_sql` stashes
  per-call state on the builder; a shared instance races (CLAUDE.md guardrail +
  `funnelhub_core.py:2338`). Every fan-out worker must construct its own runner.
- **All identifiers interpolated into SQL must be bare-id-validated + quoted**, and
  user regex must pass `_guard_regex_value` (CLAUDE.md). Tier-2 adds no new raw
  interpolation if it reuses the level/predicate builders — keep it that way.
- **Blocking IO from `async def` must go through `asyncio.to_thread`**, fan-out
  must be `Semaphore`-bounded with per-task `wait_for` + a total deadline
  (CLAUDE.md; the `analyze/v2` route at `funnelhub_core.py:2039-2087` is the
  reference pattern to copy).
- **`is_active = FALSE` soft-delete, `{result: data}` wrapper, `fh-` CSS prefix** —
  all still apply.

### Unknown unknowns (surfaced during the walk — flag for a human)
- **U1 — Is per-segment marginal mix actually what analysts want, or do they want
  strict Shapley fair-attribution?** Marginals + interaction residual is additive
  and cheap but the interaction term can be large and hard to narrate ("what does
  −3.2pp of interaction *mean*?"). Shapley is the textbook fair split but costs
  `2^N` coalition decompositions and is harder to explain. **This is the top open
  question.** (§3.3, §7)
- **U2 — Statistical significance / noise.** With `2^N` cells, small cohorts
  produce jumpy rate/mix numbers. Tier 1 sidestepped this (few big buckets). Do we
  need a min-cell-size floor or a "low confidence" flag? Not designed here. (§7)
- **U3 — Druid vs Trino cohort-semantics mismatch under cross-classification.**
  A documented divergence already exists (Druid = matching *events*, Trino cohort
  = all events of matching *customers*; `segment_definition.py:56-59`). Crossing
  two segments *multiplies* the semantic gap between lanes. The Compare tab may
  show two internally-consistent but mutually-incomparable decompositions. Needs a
  UX label at minimum; may need a product call on whether to even show Compare.
  (§4.4, §7)

---

## 2. API shape (most likely to change — decide first)

### 2.1 Request
Extend the existing endpoint; **do not** add a new route.

```
POST /api/funnelhub/query/mix-decomposition/v2
  ?funnel_id, start_date, end_date, compare_start_date, compare_end_date
  &cohort_column=<druid alias>              # existing Druid dimension axis
  &trino_dimension=<col>                    # existing single dimension axis (Tier 1)
  &trino_segment_id=<int>                   # existing single-segment axis (Tier 1)
  &trino_segment_ids=<int>&trino_segment_ids=<int>   # NEW: multi-segment axis (Tier 2)
```

Rules (validated up front, all → 422 with a specific message):
- Exactly one axis kind: `trino_dimension` XOR (`trino_segment_id` OR
  `trino_segment_ids`). Setting more than one kind → 422.
- `trino_segment_id` and `trino_segment_ids` are unioned then de-duped; the union
  is the axis. Preserves Tier-1 single-int callers with zero client change.
- `len(unique ids)` must be `1..N_MAX` (`N_MAX = 2` in v1). `> N_MAX` → 422 with
  the cell-count explanation (mirror `analyze/v2`'s `_MAX_ANALYZE_CELLS` message).
- Each id must resolve to a real, active segment; each must be **Trino-usable**
  (has a Trino binding OR is behavioral/cohort). A dimension-less, Druid-only
  segment → 422 ("segment '<name>' has no Historical binding").
- Flag OFF → 422 (unchanged).

**Decision — reuse `trino_*` naming even though Tier 2 also drives Druid.** The
Druid lane derives its cells from the *same* segment ids (a segment carries both
`druid_column` and `trino_dimension` bindings in one definition). A separate
`druid_segment_ids` param would let the two lanes attribute *different* cohorts,
which is a footgun, not a feature. One id list, both lanes. (The `trino_` prefix is
now a slight misnomer — documented, not renamed, to avoid churning Tier-1 clients.)

### 2.2 Response
Additive superset of Tier 1. Envelope unchanged:
`{ druid: <lane> | {error} | {degraded} | null, trino: <lane> | ..., total_execution_ms }`

A lane is a **list of per-stage entries**:

```jsonc
{
  "stage": "Payment",
  "total_pp": -4.10,          // observed step-conv Δ (A − B), pp. Identity anchor.
  "rate_effect_pp": -1.30,    // JOINT within-cell conversion shift (reported once)
  "mix_effect_pp": -2.80,     // JOINT cell-share shift
  // NEW in Tier 2 — attribution of mix_effect_pp across the N segment axes:
  "segment_effects": [
    {"segment_id": 10, "segment_name": "New users",   "mix_effect_pp": -1.90},
    {"segment_id":  4, "segment_name": "iOS",          "mix_effect_pp": -0.50}
  ],
  "interaction_pp": -0.40,    // mix_effect_pp − Σ segment_effects[].mix_effect_pp
  "cohort_axis": ["seg:10", "seg:4"],   // was a str in Tier 1; now a list
  "cohort_count": 4                     // product-partition cell count (2^N or Πbuckets)
}
```

Invariants the frontend can assert (and a test must guard):
- `total_pp ≈ rate_effect_pp + mix_effect_pp` (±0.01 rounding). *(Tier-1 identity, unchanged.)*
- `mix_effect_pp ≈ Σ segment_effects[].mix_effect_pp + interaction_pp` (±0.01). *(new.)*
- Single-segment call: `segment_effects` has one entry, `interaction_pp == 0.0`,
  `cohort_axis` is a 1-list → **byte-compatible with Tier 1** after the reader
  tolerates list-or-str for `cohort_axis`.

**Back-compat note:** Tier-1 emitted `cohort_axis` as a *string*. Making it a list
is a breaking read for the current UI. Either (a) keep `cohort_axis` a string
(join with `" × "`) and add `cohort_axes: [...]` as the structured field, or
(b) migrate the reader. **Recommend (a)** — least frontend churn. *(Confirm before
building; see §7 open item.)*

---

## 3. The mathematical contract (the part most likely to be wrong — argue it out)

### 3.1 Restating Tier 1
Per stage `s` with prior stage `p`, per cell `c` in a disjoint+exhaustive
partition, with `rate_c = focus_c / prior_c`, `share_c = prior_c / Σ prior`:

```
rate_effect = Σ_c avg_share_c · (rate_A,c − rate_B,c)
mix_effect  = Σ_c (share_A,c − share_B,c) · avg_rate_c
total       = rate_effect + mix_effect        # holds exactly for ANY partition
```
(`funnelhub_service.py:720-744`.) The identity's *only* precondition is that the
cells are disjoint and exhaustive — nothing about how they were defined.

### 3.2 Extending to N segments — the product partition
Treat each segment `k` as a categorical axis: In/Out (2 buckets) for a membership
segment, or its declared buckets (+ residual) for a bucketed segment. The joint
axis is the **Cartesian product** of the N axes:

```
cell = (bucket_1, bucket_2, …, bucket_N)      # e.g. (In New-users, Out iOS)
```

Because each axis is individually exhaustive (every entity is In or Out; every
value falls in some bucket or the residual), the product is **disjoint and
exhaustive by construction** — overlap between segments is irrelevant; it simply
determines which product cell an entity lands in. Feed this product partition to
the *unchanged* `_decompose_from_breakdowns` (composite string key per cell) and
the joint `rate_effect_pp` / `mix_effect_pp` fall out, identity intact.

**This is the additive backbone. It is exact and requires no new math.**

### 3.3 Attributing the mix effect to each segment (the genuinely new bit)
`rate_effect` is a *within-cell* quantity — attributing it to one axis is not
meaningful, so we report it once at the joint level and stop. `mix_effect`
(share shifts) *is* attributable. Contract:

```
segment_mix_pp[k] = Tier1 mix_effect over the MARGINAL partition of axis k
                    (collapse the product-partition rows onto axis k's buckets:
                     sum focus/prior across all other axes, then run the same
                     symmetric mix formula)
interaction_pp    = mix_effect_pp − Σ_k segment_mix_pp[k]      # residual, by definition
```

Properties:
- **Additive, guaranteed:** `mix = Σ segment_mix + interaction` is true *by
  construction* (interaction is the closing residual). No approximation.
- **Symmetric / order-free:** each `segment_mix_pp[k]` marginalizes over the
  others identically; there is no privileged axis, so no hierarchy and no
  order-dependence. This is deliberately **flat, not hierarchical** — hierarchy
  (nest axis 2 inside axis 1) would make the attribution depend on an arbitrary
  UI ordering, which we reject.
- **Interaction is named, not hidden:** when two segments' share shifts are
  correlated (e.g. the growth of "New users" is concentrated in "iOS"), the joint
  mix ≠ the sum of marginals, and that gap is surfaced as `interaction_pp` rather
  than silently dropped or double-counted.
- **Cheap:** all of this is pure CPU over already-fetched breakdown rows —
  one product-partition decomposition + N marginal decompositions. No extra
  warehouse round-trips beyond the single product-partition breakdown per period.

**Why not Shapley (the rejected alternative).** Shapley would distribute the joint
mix across axes with *no* interaction residual (interaction is averaged into the
axis shares), giving a "cleaner" N-number split. But: (a) it costs `2^N` coalition
decompositions; (b) it *hides* interaction rather than exposing it, and for a
diagnostic tool ("why did it change?") the interaction *is* the insight; (c) it's
markedly harder to explain to an analyst than "here's each segment's own effect,
here's what they explain jointly beyond that." We keep marginals + explicit
interaction and note Shapley as a future opt-in (§7, U1). **This is the #1
decision a human should sign off on.**

### 3.4 Why N is capped at 2 (v1)
- Product cells = `Π bucket_count_k`. Two In/Out segments = 4 cells; two 3-bucket
  segments = 9. Three segments push toward `MAX_COMBINATIONS=50` fast, and the
  Druid fan-out is `cells × 2 periods`.
- `interaction_pp` at N=2 is a single 2-way term — legible. At N=3 the residual
  conflates 2-way and 3-way interactions into one number that means little.
- The `levels`/`MAX_LEVELS=3` machinery *can* express 3, so N=3 is a low-cost
  stretch behind the same caps — but ship and validate N=2 first.

### 3.5 Worked micro-example (goes in the test suite)
Two membership segments A, B; one stage transition. Construct period A/B counts so
that A's share rises and B's share rises, with the rises correlated. Assert:
`total = rate + mix`; `mix = segA + segB + interaction`; `interaction ≠ 0` and has
the sign implied by the correlation. Hand-compute the expected pp values in the
test (this is the core correctness guarantee — a fix without this test is not done).

---

## 4. Druid-lane support

### 4.1 Mechanism — per-cell funnel fan-out
Druid can't GROUP BY membership, so build the `2^N` (or `Πbuckets`) cells and run
**one Druid funnel per cell per period**, applying each axis's row-scope predicate
(`segment_definition.to_druid_filters(defn)`), ANDed across axes. This is exactly
`SegmentDrilldown`'s existing primitive extended to a *product* of levels rather
than a single level — reuse its cohort-expansion + bounded pool, do not reimplement.

Cells × periods = fan-out breadth. N=2 In/Out → `4 × 2 = 8` runs. Bound with a
`Semaphore` + per-run `asyncio.wait_for` + a total deadline; copy the `analyze/v2`
harness (`funnelhub_core.py:2039-2093`). Fresh runner per worker.

### 4.2 The residual/"Out" cell — total-subtraction, not negation
For each period, also run the **unscoped** funnel once (the funnel total per stage).
Any cell whose definition is a negation (Out of a membership segment, or the bucket
`Other`) is computed as `total − Σ(sibling positive cells)` per stage, clamped ≥ 0
— the same trick Tier-1 closed-Trino already uses (`funnelhub_core.py:2361-2371`).
For N=2 this means: run the two positive In cells `A∩B`, and the marginals, plus
total, and derive the rest by subtraction. **Rationale:** negating regex / `IN` /
null Druid predicates correctly across all op types is error-prone; subtraction is
robust and matches the shipped closed-funnel semantics. (Exact run set to minimize
Druid load is an implementation detail; the contract is "cells are
disjoint+exhaustive and sum to total per stage.")

Feed the resulting per-cell rows into the **same** `_decompose_from_breakdowns` +
marginalization as Trino (§3). The math is engine-agnostic; only the row-fetch
differs. This mirrors how the dimension-axis mix-decomp already shares math across
lanes (`funnelhub_core.py:2154-2162`).

### 4.3 Degradation — the second decision the prompt demands
Per-lane, per-segment, **soft**:

- Compute `expressible_druid = all(segment_definition.effective_engines(defn) ⊇ {druid} for each selected segment)`.
- **If every selected segment is Druid-expressible** → full Druid attribution via §4.1.
- **If any selected segment is NOT Druid-expressible** (behavioral, cohort-backed,
  or an attribute segment with no `druid_column` on a needed rule) → the Druid lane
  returns **`{"degraded": {"reason": "...", "blocking_segments": [names]}}`**, not
  `null` and not an error. The UI renders "Historical only — <seg> is a
  behavioural/Trino-only cohort" on the Realtime tab. Trino still returns its full
  decomposition. *(Do not attempt a partial Druid decomposition over a subset of
  the axes — a decomposition that silently drops an axis is misleading. All-or-
  nothing per lane.)*
- **Regex asymmetry:** a regex attribute segment with a `druid_column` *is* Druid-
  expressible (Druid resolves distinct values, existing path) but is **not**
  expressible via the Trino open-funnel drilldown (Tier-1 note,
  `trino_sql_builder.py:1020-1026`). On Trino, route such a segment through the
  **In/Out membership cohort-CTE** path (which compiles regex to `REGEXP_LIKE` via
  `predicates`, guarded by `_guard_regex_value`) rather than the drilldown level.
  So expressibility is decided **per lane**, not globally.

### 4.4 Cross-lane comparability caveat (see U3)
Under cross-classification the Druid (event-scope) vs Trino (customer/entity-scope
cohort) semantic gap compounds. The two lanes each produce an internally-consistent
decomposition, but the Realtime and Historical numbers are **not** directly
comparable. Minimum: a persistent info label on the Compare tab for segment-axis
attribution. Product may decide to hide Compare entirely for this mode — flag for a
human (§7).

---

## 5. UX flow

Build on the Tier-1 picker in `PeriodCompareLanes` (`ResultsArea.jsx:671`).

- The **Segments** section becomes **multi-select** (chips), capped at `N_MAX=2`
  (disable further selection past the cap with a tooltip explaining the cell-count
  limit). Selecting ≥1 segment clears the dimension axis (existing mutual
  exclusion, extended).
- Each selectable segment shows a small **engine badge** (Realtime ✓ / Historical ✓)
  derived from the extended `/trino-dimensions` capability payload, so the analyst
  knows *before* running that picking a behavioural cohort means "Historical only."
- Results:
  - **Trino/Historical tab:** the existing per-stage attribution table gains a
    breakdown row group per stage: `Rate effect`, then `Mix effect` with a nested
    per-segment list (`New users −1.9pp`, `iOS −0.5pp`) and an `Interaction −0.4pp`
    line. Sign convention unchanged (positive = A higher than B).
  - **Druid/Realtime tab:** identical rendering when expressible; otherwise the
    degraded notice (§4.3).
  - **Compare tab:** the comparability caveat label (§4.4).
- Reset-on-new-envelope and user-triggered-only semantics carry over unchanged
  (`ResultsArea.jsx:735-763`).

Loading/empty/error states already exist for the Tier-1 mix fetch (`mixStatus`);
reuse. Fan-out means the Druid lane can be materially slower — the per-lane spinner
must be independent (it already is: `mix.druid` / `mix.trino` are separate).

---

## 6. Cost, limits, safety
- **Warehouse cost:** Trino = 1 product-partition breakdown per period (2 total) —
  same order as Tier 1. Druid = `cells × periods` funnel runs (N=2 → ~8), bounded.
- **Caps:** `N_MAX = 2` segments; reuse `MAX_COMBINATIONS`/`MAX_LEVELS`/
  `_MAX_ANALYZE_CELLS`-style guards; total request deadline with pending-task
  cancellation (copy `analyze/v2`).
- **Concurrency:** fresh builder/runner per fan-out worker; `Semaphore`-bounded;
  per-task `wait_for` + total `asyncio.wait(timeout=...)`; blocking calls via
  `to_thread`. (CLAUDE.md async guardrails — a reviewer will reject violations.)
- **SQL safety:** no new raw interpolation — go through `TrinoDrilldownLevel` /
  `predicates` / `to_druid_filters`, all of which already bare-id-validate + quote
  and `_guard_regex_value`. Add none of your own f-strings.
- **Tenant scoping:** segments loaded fresh from the DAO by id and authorized via
  the persisted funnel's own `vertical_id` (`_authorize_funnel`) — never trust a
  client-supplied vertical. (Tier 1 already does this; keep it.)

## 7. Open questions for a human (ranked)
1. **U1 — marginal + interaction vs Shapley.** Ship marginals + explicit
   `interaction_pp` (this spec), or invest in Shapley fair-attribution (no residual,
   `2^N` cost, harder to explain)? *Recommendation: ship marginals now, Shapley as a
   later opt-in.* **Blocks the response schema — decide before building §3.3.**
2. **U3 — Druid↔Trino comparability under cross-classification.** Label the Compare
   tab, or suppress Compare for segment-axis attribution entirely? Product call.
3. **`cohort_axis` back-compat.** Keep it a string (`"A × B"`) + add structured
   `cohort_axes`, or migrate the reader to a list? *Recommendation: keep string, add
   list — least churn.* Small but breaking if gotten wrong.
4. (Lower) **U2 — min-cell-size / low-confidence flag** for tiny product cells.
   Punt to a follow-up unless dogfood shows jumpy numbers.

---

## 8. Files to touch (mechanical — do last)

**Backend**
- `backend/routes/funnelhub_core.py`
  - `mix_decomposition_v2` (~2133): accept `trino_segment_ids: list[int]`; union
    with `trino_segment_id`; N-cap + eligibility validation; branch `_trino_decomp`
    and `_druid_decomp` to the multi-segment product-partition path.
  - Add a `_druid_decomp` segment branch (today it early-returns `null` for a
    segment axis, `:2219`): per-cell fan-out (§4), copying the `analyze/v2`
    bounded-fan-out harness (`:2039-2093`).
- `backend/services/funnelhub_service.py`
  - `_decompose_from_breakdowns` — **no change** (composite/marginal keys are just
    strings). Add a thin helper `_attribute_mix_to_segments(product_rows_a/b,
    axes)` that marginalizes per axis and computes `segment_effects` +
    `interaction_pp` (§3.3). Pure CPU, fully unit-testable.
- `backend/funnelhub/trino_sql_builder.py`
  - Reuse `build_segment_drilldown_sql` with **N `levels`** (already supported up to
    3) to get the product partition in one query for open funnels. Closed funnels:
    extend the Tier-1 scoped/unscoped run set to the product cells (§4.2 subtraction).
  - Add a small composite-cohort-key projection if the drilldown doesn't already
    emit one usable key (verify: it emits one column per level; the route can
    concat them into the composite key without SQL changes).
- `backend/services/funnelhub_drilldown.py` — reuse the Druid per-cohort fan-out;
  extend the level-resolution to a product of ≤2 levels for the Druid attribution
  path (or factor the cell-expansion into a shared helper the route calls).
- `backend/routes/funnelhub_core.py` (`/trino-dimensions` response) — add per-
  segment `engines` capability so the picker can badge Realtime/Historical.
- `backend/config.py` — reuse `FF_FUNNELHUB_SEGMENT_ATTRIBUTION`; no new flag.

**Frontend**
- `frontend/src/funnelhub/components/ResultsArea.jsx` (`PeriodCompareLanes`) —
  multi-select segments (cap 2); engine badges; per-segment + interaction rows in
  the attribution table; degraded/comparability notices.
- `frontend/src/funnelhub/FunnelDetailPage.jsx` (`handleFetchPeriodMixDecomp`) —
  accept `{ trinoSegmentIds }`; pass through.
- `frontend/src/services/funnelhubApi.js` (~587) — send repeated
  `trino_segment_ids`; keep `trinoSegmentId` for single-select back-compat.
- Segment eligibility/badge helper in `frontend/src/funnelhub/utils.js`.

**Tests (each fix lands with a test; run `pytest` + `CI=true npm test -- --watchAll=false`, report numbers)**
- Backend: the §3.5 worked example (both identities); marginalization correctness;
  N-cap + mixed-eligibility → 422; Druid degraded-lane returns `{degraded}` (not
  error) when a behavioural segment is selected; Druid total-subtraction residual
  sums to total per stage; closed-funnel product cells; regex per-lane routing.
- Frontend: multi-select cap + mutual exclusion; badges from capability payload;
  request sends `trino_segment_ids`; renders per-segment + interaction rows;
  degraded notice on Realtime.

## 9. Rollout
1. Ship behind `FF_FUNNELHUB_SEGMENT_ATTRIBUTION=false` (already the Tier-1 gate).
2. Dogfood N=2 on funnel 8 (RU Prepaid, closed) with two membership segments; hand-
   verify both identities against a computed example; confirm Druid degrades to
   Historical-only when one segment is behavioural.
3. Find/construct an open + Trino-resolvable funnel to exercise the drilldown
   product-partition path (Tier 1 couldn't dogfood this locally — carry the gap
   forward as a known validation hole until an env exists).
4. Stage → prod.
