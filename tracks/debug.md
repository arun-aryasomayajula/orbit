# Track: debug
**Method comes from the skill `superpowers:systematic-debugging`** (orchestrator invokes + briefs).
Loaded when: category `bug`, or a task whose job is "why is X wrong / failing".
This generic template ships enabled; add YOUR repo's debugging gotchas below and it improves.

## Hard rules (universal — keep; add repo-specific ones)
- **The regression test must fail before the fix and pass after — DEMONSTRATE it, never assert it.**
  Run it against the pre-fix code, watch it fail, then fix. (A plausible test that asserts a shape
  the code never produces passes in both worlds and proves nothing.)
- **Never weaken/delete a check to reach green.** If the code is right and the test is wrong, say so
  and escalate — don't edit the test to pass.
- Same failure twice in a row → STOP + escalate (you're guessing, not diagnosing).

## Playbook (universal starters — replace with repo specifics)
- "Only under load / intermittent" → suspect shared mutable state on a cached/singleton object.
- "Wrong output, no error" → a value passed validation but at the wrong scope; trace write-vs-read span.
- Know how to restart the service and tail its logs for THIS repo (fill in) — testing stale code wastes cycles.

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
