# Golden calibration — what a STRONG output looks like, per category

These are graded exemplars distilled from real model runs (see the `G*/` dirs + `SCORECARD.md`
for the full evidence and the blind-judge verdicts). Orbit briefs the relevant card below into
the maker/checker/verifier for the task's category, the same way it loads a track. The card says
what a strong output has and the graded FLAWS that got marked down — match the strong pattern.

For a concrete model, a maker may Read the full winning exemplar named in each card.

---

## ALWAYS (every task) — from G1 + G3
**Both strong and weak models emit confident falsehoods.** Never trust a self-assessment.
- A model wrote a test that *looked* right but asserted a SQL shape the code never emits — it
  passed in both worlds and proved nothing (G3). **Execute the gate and the test; demonstrate
  fail-before/pass-after — never assert it.**
- A model certified "no injection path" over code that had a live SQL injection, and "every
  REGEXP_LIKE is ReDoS-guarded" when one wasn't (G1). **A confident "all clear" is a red flag,
  not a result — verify the claim against the code.**

## bug — from G3 (bug-archaeology). Exemplar: `G3-bug-archaeology/runs/fable-2026-07-06.md`
Strong: reproduces the symptom first, names the EXACT object/field/lifecycle (not a vague area),
explains *why only under the reported condition*, and ships a regression test that is executable
and demonstrably fails before the fix. Bonus: quantifies the mechanism (why the race window is
wide enough to bite). FLAW that lost: a plausible but non-executable test (asserted a shape the
builder never produces). Prefer removing shared state over adding a lock.

## feature — from G4 + G2 + G6. Exemplar: `G4-sql-builder/runs/*.md` (both tied, both strong)
Strong: correct code that follows the repo's guardrails UNPROMPTED (delegates literals through the
escaping boundary, fails closed on bad input), plus requirement-meeting tests that actually pass
and cover the real cases (numeric + string + malformed). For multi-file features, slice first
(G6): lead with the decisions most likely to change. Report REAL test numbers.

## refactor / code_quality — from G4 + G1
Strong: behavior-preserving (existing tests pass unchanged), one concept per commit, guardrails
followed unprompted. A refactor that needs a test changed is not a refactor. Surgical diffs beat
sprawling ones — the reviewer must be able to see it's safe.

## testing — from G3 + G4
Strong: every test can actually fail; asserts on real emitted behavior, not a mock echoing input;
covers the happy path + the malformed/edge path. FLAW that lost (G3): a test whose assertion never
matches the code's real output — it's worse than no test. Mirror the repo's framework; run it and
report real counts.

## documentation / spec — from G6 (spec-quality). Exemplar: `G6-spec-quality/runs/fable-SPEC.md`
Strong: leads with the decisions most likely to change (data model, API shape, the hard contract),
mechanical work last; **discloses its own uncertainty and approximations honestly** (the winner
disclosed a theta-sketch error source the loser silently relied on); grounds every claim in real
code — no invented APIs. A polished spec that hides its hard trade-offs loses to an honest one.

## frontend — from G5 (ui-slice). Exemplar: `G5-ui-slice/runs/*.md` (both tied)
Strong: matches the repo's toolchain EXACTLY (don't swap CRA↔Vite or Jest↔Vitest), typecheck +
tests green with zero baseline regressions, conventions followed unprompted (styling approach,
API wrapper shape, shared primitives), and every interaction state handled (loading/error/empty).

## code review (checker / verifier lens) — from G1 (code-review). Exemplar: `G1-code-review/runs/opus-2026-07-07.md`
Strong: ranks the genuinely highest-severity issue FIRST (the winner led with a real always-live
SQL injection the other reviewer missed), each finding has exact file:line + a concrete failure
scenario, and it never certifies safety it didn't verify. Breadth is good; severity calibration is
better. The ideal is the UNION of independent reviewers — one may catch what the other's confident
"all clear" hid.
