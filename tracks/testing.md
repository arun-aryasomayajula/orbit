# Track: testing
**Method comes from the skill `superpowers:test-driven-development`** (orchestrator invokes + briefs).
Loaded when: category `testing`, or the task touches test files / test config.

## Hard rules (universal)
- **A test that can't fail is worse than no test.** Every new test must be able to fail — assert on
  real behavior, not on a mock returning its own input.
- **Mirror the repo's existing framework and conventions** — read a sibling test first. Do NOT
  introduce a new test framework or runner.
- If writing a test surfaces a real PRODUCTION bug, STOP and report it (`STOPPED — production bug`)
  so the orchestrator hands the fix to the builder — do not change production code to make a test pass.
- Never delete/skip an existing test to make the suite green.

## Playbook (add repo specifics)
- Know the exact gate command(s) for THIS repo (the orchestrator briefs them from config.gates) and
  run the ones covering your change; report real pass counts.
- Fill in: framework quirks (e.g. CI flags, coverage caveats, DB fixtures) that have bitten before.

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
