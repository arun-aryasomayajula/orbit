# Golden baseline scorecard — Fable 5 vs Opus 4.8 (2026-07-06/07)

Six tasks, frozen prompts, pinned refs, isolated worktrees, objective gates + blind judges
who verified claims against real source. Full detail in each G*/runs/scores-*.md.

| # | Task | Axis | Winner | Margin |
|---|------|------|--------|--------|
| G1 | Code review of backend/funnelhub | Open-ended discovery | **Opus** | narrow — complementary |
| G2 | Deep health endpoint | Directed feature | **Tie** | — |
| G3 | Root-cause reverted race bug | Directed root-cause | **Fable** | narrow |
| G4 | `between` operator + tests | Domain depth | **Tie** | — |
| G5 | Campaign UI search box | Frontend + conventions | **Tie** | — |
| G6 | Tier-2 attribution spec | Ambiguity / spec quality | **Fable** | narrow |

## The pattern (this is the actionable part)

1. **On directed, well-scoped work with local conventions to follow (G2, G4, G5): Opus is
   at FULL PARITY.** Both produced correct, guardrail-compliant code with passing,
   requirement-meeting tests. This is most of day-to-day coding. Opus needs no compensation
   here.

2. **On harder judgment axes, Fable holds a NARROW edge — always traceable to ONE thing:
   intellectual honesty about its own uncertainty / verification.**
   - G3: both found the exact root cause + fix; Fable won because Opus's regression test was
     plausible but NON-EXECUTABLE (asserted a SQL shape the builder never emits).
   - G1: Opus found a REAL always-live SQL injection Fable missed AND certified absent
     (Fable's false "no injection path"); but Fable found 6-8 unique real bugs Opus missed.
     Genuinely complementary — neither dominates.
   - G6: both specs excellently grounded, NEITHER hallucinated an API (write-tool access to
     real code prevented the G1-style false-negative); Fable won on honesty about
     non-additivity + theta-sketch error, which Opus used but never disclosed.

3. **Both models emit confident falsehoods — different flavors.** Opus: plausible-but-wrong
   TESTS + a false "no vulns" certification. Fable: a false all-clear on injection + ReDoS.
   Neither's self-assessment is trustworthy on "is it safe / does it pass."

## The Opus playbook (validated by these runs)

- **Directed coding: trust Opus at parity.** No extra ceremony for well-scoped feature/bugfix
  work with clear conventions.
- **High-stakes review/discovery: run BOTH models, UNION findings.** They have complementary
  discovery profiles (Opus deep+severity-led, Fable broad). Autopilot's multi-agent structure
  already supports this. Cost is low; coverage gain is real.
- **NEVER accept a single reviewer's "no vulnerabilities / all clear."** Both models produced
  exactly this falsely.
- **ALWAYS execute model-written tests** — demonstrate fails-before/passes-after, never accept
  the assertion. This is THE load-bearing compensator; superpowers TDD + verification skills
  enforce it when invoked, so keep them mandatory on Opus.
- **Spec/design work: Opus is strong but grade it on honesty** — does it disclose its
  approximations and non-additivity, or paper over them? Prefer specs that flag their own
  uncertainty.

## Real bug surfaced (not a benchmark artifact)
G1 found a VERIFIED always-live SQL injection at HEAD: predicate_compiler.py:149-157
(emit_literal DATE'/TIMESTAMP'/CURRENT_DATE prefix passthrough, client-controlled via
FunnelStageFilter.val which lacks a validator) + trino/adapter.py:123-124 (date_literal no
quote-escape). Needs a real fix + regression test — tracked separately.

## Harness gotchas (for re-runs)
- `claude -p` session-limit hits write a "You've hit your session limit" stub and exit 1 —
  check exit code, retry after reset.
- Campaign repo: run npm from frontend/, NOT repo root (root globs wrong test scope → false
  failures). Clean demo-v11 frontend baseline = 9 files / 59 tests.
- Golden repo worktrees have no venv — use ~/master/cdp-metaql/venv/bin/python for pytest.
