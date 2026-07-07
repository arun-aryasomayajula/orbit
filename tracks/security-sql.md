# Track: security-sql
**Audit method comes from the skill `security-review`** (orchestrator invokes on the diff + briefs).
Loaded when: the task builds a query or moves user/client input toward a datastore.
Generic template — see examples/security-sql.md for a filled-in (SQL warehouse) version; add YOUR repo's facts.

## Hard rules (universal)
- **Parameterize values; validate identifiers.** Values → bound parameters. Identifiers (table/column/
  dimension) can't be parameterized → validate against a strict allowlist/regex, then quote via the
  driver's quoting helper. NEVER f-string client input into a query.
- **Guard user-supplied regex** for length + catastrophic backtracking (ReDoS) at EVERY call site.
- **Scope tenant data by the persisted row's own tenant id, never a client-supplied one.**

## Playbook (add repo specifics)
- Point at THIS repo's query-builder / escaping boundary and its allowlist (fill in the file path).
- Note any reserved words, required qualification, or partition/sargability rules for your engine.

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
