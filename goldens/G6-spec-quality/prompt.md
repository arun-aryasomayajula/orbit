You are a senior engineer scoping an upcoming feature in a FastAPI + React mixed-engine funnel analytics platform (FunnelHub: funnels run in parallel on Druid (real-time) and Trino (T-1 warehouse via MetaQL LookML explores)).

## Feature to spec: segment-axis attribution, Tier 2

Tier 1 already shipped: the period-compare "Why did it change?" decomposition can split a rate-vs-mix change by a SINGLE segment, Trino lane only, closed funnels excluded, behind flag FF_FUNNELHUB_SEGMENT_ATTRIBUTION. (Explore the codebase to understand how Tier 1 works — build_segment_drilldown_sql, the decomposition, the period-compare path — before specifying Tier 2.)

Tier 2 scope:
1. Multi-segment attribution: decompose a period-over-period change across N segments simultaneously — you must decide and justify the mathematical contract (interaction effects? additivity guarantee? hierarchy vs flat?).
2. Druid-lane support: the same attribution on the real-time lane, where segments may be theta-sketch cohorts and behavioral (Trino-only) cohorts cannot resolve — decide the fallback/degradation behavior.

## Deliverable

First run a blindspot pass / unknown-discovery quadrant walk YOURSELF against the actual code (known knowns, known unknowns, unknown knowns, unknown unknowns) — since no user is available, resolve each unknown by reading the codebase and state the assumption where you can't.

Then write the spec. Lead with the decisions most likely to change (data model, API shape, the mathematical additivity contract, UX flow); put mechanical work at the bottom. Include the unknowns map. Save it to specs/segment-attribution-tier2/SPEC.md (create the dir). Your final message is a summary of the spec + the top 3 unresolved unknowns that most need a human decision.
