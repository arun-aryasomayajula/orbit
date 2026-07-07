# Ground truth (do NOT show to the model)

- Pinned ref: `66800d26` (= `237f8460^`, the commit BEFORE the fix).
- Shipped fix: `237f8460` "fix(funnelhub): fresh TrinoFunnelSQLBuilder per run — kill shared-state race (review FIX-3)".
- Root cause: `get_trino_funnel_sql_builder()` was an `@lru_cache` singleton; per-request
  fields (`_grain_override`, `_metric_type`, etc.) were stashed on the shared instance,
  so concurrent requests raced and one run's metric grain leaked into another's SQL.
  Druid unaffected because its lane doesn't share a mutated builder instance.
- Fix shape: construct a fresh builder per call / pass state as arguments instead of
  instance attributes.

## Scoring

- PASS: identifies the shared singleton + per-request attribute mutation as the race,
  explains why single-request inspection looks correct, proposes per-call construction
  (or explicit argument passing), and a concurrency regression test.
- PARTIAL: finds the right file/object but wrong mechanism, or proposes locking instead
  of removing shared state (works but inferior — note it).
- FAIL: blames Trino, caching layers, or the SQL text generation.
