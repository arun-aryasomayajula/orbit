# Track: observability
Loaded when: category `observability`, or work touching logging/metrics. Pairs with async-backend on request paths.

## Hard rules (universal)
- **Never log PII** (ids, emails, phones, raw request bodies) at INFO. Gate verbose behind a debug flag.
- Levels: ERROR (exceptions, 5xx), WARN (recoverable, 4xx), INFO (lifecycle), DEBUG (local only).
- Metrics go through the existing metrics middleware/endpoint — don't hand-roll a parallel path.
- No blocking IO for a log/metric on a hot async path.

## Playbook (add repo specifics)
- One logger module; structured, greppable context (task/request id). Fill in: log location + metrics sink.

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
