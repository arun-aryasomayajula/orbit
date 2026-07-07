# Track: observability
Loaded when: category `observability` (forced), or work touching logging/metrics/actuator.
Pairs with `async-backend` for anything on a request path.

## Hard rules
- **Never log PII.** No customer_id, email, phone, or raw request bodies at INFO. Gate
  verbose output behind a debug flag and turn it off once fixed.
- Levels: ERROR (exceptions, 5xx), WARN (recoverable, 4xx), INFO (request lifecycle,
  startup), DEBUG (detailed, local only). Don't log at the wrong level — it poisons the
  logwatch loop that reads prod logs.
- Metrics go through the existing `MetricsMiddleware` / actuator endpoint
  (`backend/routes/actuator.py`) — don't hand-roll a parallel metrics path.
- A new log line on a hot async path must not do blocking IO (no sync DB/file writes on
  the event loop) — see `async-backend`.

## Playbook
- One logger module; logs to `logs/` timestamped with `latest.log` pointing at current.
- Add structured context (task/request id) not free-text blobs — the log-analyst agent
  parses signatures, so make them greppable and stable.
- If adding a metric, also note where it surfaces (Prometheus scrape / dashboard) so it
  isn't write-only.

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
