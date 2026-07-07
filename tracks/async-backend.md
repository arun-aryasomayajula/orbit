# Track: async-backend
Loaded when: the task changes backend code on async/concurrent paths.
Generic template — see examples/async-backend.md for a filled-in version; add YOUR repo's facts.

## Hard rules (universal)
- **Never call blocking IO from an async handler unwrapped** (DB, network, disk) — offload it
  (e.g. `asyncio.to_thread`) or the event loop stalls every concurrent request.
- **Bound fan-out** (semaphore / chunked gather) over any user-controlled list; add per-task AND
  total-deadline timeouts. A cancel/timeout does NOT kill an already-running worker — the real
  backstop is the client+server timeout on the outbound call.
- **No per-request state on a singleton/cached object** — it races across concurrent requests. Build
  fresh per call or pass state as arguments.

## Playbook (add repo specifics)
- Fill in: this repo's dispatcher/executor entry points, connection-pool size, timeout budgets, port.

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
