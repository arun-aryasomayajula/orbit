# Track: refactor
**Method comes from the skill `simplify`** (orchestrator invokes + briefs).
Loaded when: category `code_quality` or `refactor` — restructuring without changing behavior.

## Hard rules (universal)
- **Behavior-preserving means the existing tests are the contract** — they must all still pass
  UNCHANGED. If a test has to change, it's not a refactor; escalate.
- **No scope creep.** One concept consolidated per commit; no "while I'm here" fixes — they make
  the diff unreviewable and the ship gets rejected.
- **Deletes are DENIED unattended** — removing a file/function needs a human; escalate.
- Leave a re-export shim rather than editing every import site in one cycle.
- Run the FULL gate (not just touched files) — refactors leak across modules.

## Playbook (add repo specifics)
- Match the neighbours: read the surrounding file's naming/idiom before renaming.
- Keep the public surface stable; change internals. Signature changes are feature/bug tasks, not refactors.

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
