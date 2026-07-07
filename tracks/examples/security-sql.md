# Track: security-sql
**Audit method comes from the skill `security-review`** (orchestrator invokes it on the
diff + briefs) — this track adds only REPO-SPECIFIC security facts.
Loaded when: the task builds SQL or moves user/client input toward a query —
`backend/query_builder/`, `*sql_builder*`, `predicates.py`,
`filter_translator.py`, or any string that ends up in Trino/Druid/Postgres.
CLAUDE.md's SQL/security guardrails (identifier regex, ReDoS guard, tenant
scoping) already apply — this track holds what CLAUDE.md does NOT.

## Hard rules (repo-specific — violations have shipped bugs or burned cycles)
- A LookML view with no `sql_table_name:` resolves to a BARE base_table →
  Trino `MISSING_SCHEMA_NAME`. Qualify (`_qualify_table`) or pre-filter such
  views before emitting SQL.
- Druid: every drilldown/breakdown dimension must be in the `fh_druid_columns`
  allowlist; `value` is a Calcite RESERVED word (alias/quote it);
  `stage_filter` belongs in the outer WHERE, never inside a FILTER aggregator.
- UI "platform" is the Druid column `clientid`; the static alias table lives in
  `backend/funnelhub/filter_translator.py` — extend it there, not inline.
- EVERY new `REGEXP_LIKE` call site runs `_guard_regex_value` (length +
  catastrophic-backtracking check) — the guard exists; the bug is forgetting
  it at the second call site.
- Tenant scoping: authorize against the PERSISTED row's `vertical_id`
  (`_authorize_funnel` pattern) — never a client-supplied one. New scoping
  enforcement ships behind a default-OFF flag.

## Playbook (distilled patterns that fit this repo)
- Parameterize values; identifiers can't be parameterized — validate with the
  bare-identifier regex then `adapter.quote_identifier(...)`.
- Sargable predicates: no functions wrapped around partition/date columns in
  WHERE (kills Trino partition pruning — scan-prune is why funnels run ~2s).
- LIMIT every exploratory query against warehouse tables.
- Pattern files: `backend/funnelhub/trino_sql_builder.py` (`_guard_regex_value`),
  `backend/funnelhub/predicates.py`, `backend/query_builder/tier_coercion.py`.

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
