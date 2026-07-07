# Track: docs
**Method comes from the skill `write-docs`** (orchestrator invokes + briefs): a doc is a GLOSSARY of
principles/pointers, never a mirror of the code — keep the WHY and where-things-live, shed
rosters/counts/changelog, one canonical home per fact.
Loaded when: category `documentation`.

## Hard rules (universal — add YOUR repo's doc conventions)
- **Verify every claim against the actual implementation before writing it.** The code is truth;
  the doc must never race it. No invented flags, paths, or behavior.
- **One home per fact** — link, don't duplicate across docs.
- Respect where docs live in THIS repo (fill in: e.g. `/docs/` only; auto-generated files you must
  regenerate not hand-edit; schema docs to update on migrations).

## Playbook
- Point at the source-of-truth registry (a folder, a config file), don't transcribe its contents.
- Match the sibling doc's structure/voice — read one in the same folder first.

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
