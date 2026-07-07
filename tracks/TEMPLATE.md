# Track: <name>   ← authoring guide + skeleton (copy this into <target>/.autopilot/tracks/<name>.md)

A **track** is a distilled, repo-specific playbook the orchestrator loads for matching tasks
and briefs into the maker + checker. It holds ONLY what a general skill or CLAUDE.md can't:
the gotchas, invariants, and pattern-file pointers that are true for THIS repo.

## The one rule
**Skills carry general method; tracks carry repo facts. Never duplicate.** If a line would be
true in any repo, it belongs in a skill (or CLAUDE.md), not here. Delete it.

## Structure (keep it lean — 30–45 lines)
```
# Track: <name>
**Method comes from the skill `<skill>`** (orchestrator invokes + briefs) — this track adds only REPO-SPECIFIC facts.
Loaded when: <category and/or path triggers — must match the router's tracks/path_tracks>.

## Hard rules (violations shipped bugs or burned cycles — cite the incident where you can)
- <a rule that encodes a real bug this repo actually shipped>
- <an invariant no general skill knows — a shared contract, a reserved word, a required call>

## Playbook (repo-specific patterns + pattern-file pointers)
- <"do it like <file>:<symbol>"> — point at the canonical example in-repo
- <the gate/command quirk specific to this repo>

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
```

The `## Learned here` section is self-maintaining: the cycle appends a dated one-liner when a
task in this area teaches a reusable gotcha, so the track stays alive without manual upkeep.
