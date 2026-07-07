You are working in a FastAPI analytics backend (MetaQL/FunnelHub). Implement a small feature end to end.

Task: add a deep health-check endpoint `GET /api/funnelhub/health/deep` that returns JSON:
`{ "result": { "status": "ok"|"degraded", "checks": { "database": "ok"|"error", ... }, "version": <git short sha or app version if available> } }`.

- It must actually verify PostgreSQL connectivity using the project's existing DB access pattern (the `get_db_cursor()` context manager) — a real `SELECT 1`, not a stub. Run blocking DB IO off the event loop the way this codebase requires for async routes (read the CLAUDE.md guardrails and existing funnelhub routes first).
- Mount it on the existing FunnelHub router following the module's conventions (response wrapper `{ result: ... }`, error handling). Do not invent a new router or app mount.
- `status` is "ok" only if every check is "ok"; otherwise "degraded", and the endpoint must still return 200 (it's a health probe, not a hard failure).
- Add a test in tests/ that hits the endpoint with the DB check mocked both ok and failing, asserting status/checks and that it never 500s. The test must pass without a real database.

Constraints: match existing code style; parameterized SQL; don't weaken existing tests. Run the relevant tests and report actual pytest output. Final message: files changed (path:line), the endpoint's response shape, and pytest result.
