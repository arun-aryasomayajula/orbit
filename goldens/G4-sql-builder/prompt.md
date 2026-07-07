You are working in a FastAPI analytics backend. FunnelHub builds Trino SQL for funnel stages from client-supplied filters.

Task: add a `between` range filter operator to the Trino funnel filter path, end to end.

- A `between` filter carries two values (low, high) and must emit `col >= low AND col <= high` in the generated SQL, correctly typed (numbers unquoted, strings/dates quoted+escaped via the existing literal-emission path — do NOT hand-format literals).
- Follow the EXISTING operator-mapping and guardrails in this codebase exactly: bare-identifier validation before any identifier reaches SQL, adapter identifier quoting, and the project's literal-escaping boundary. Read neighboring operators first and match their style.
- If the value is missing/malformed (not exactly two values), fail closed the same way peer operators handle bad input — do not silently drop the filter or emit a partial predicate.
- Add a unit test in tests/ that (a) asserts the emitted SQL for a numeric and a string `between`, and (b) asserts a malformed `between` is rejected. The test must pass.

Constraints: match existing code style; do not weaken or delete any existing test; parameterized/escaped SQL only. Run the relevant tests and report the actual pytest output. Your final message must state: files changed (path:line), the emitted SQL shape, and the pytest result.
