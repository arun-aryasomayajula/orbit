# Orbit goldens — graded exemplars of good coding output

A third reference type alongside **skills** (general method) and **tracks** (repo-specific facts):
**goldens** are real, graded examples of what a strong output looks like for each kind of coding
task, plus the flaws that got marked down. Orbit briefs the relevant calibration into the
maker/checker/verifier every cycle (see the router's `goldens:` block and `orbit-cycle.md` §2.6).

## How agents use it
- `CALIBRATION.md` — the distilled, agent-facing cards ("what good looks like per category").
  This is what the orchestrator loads per cycle: the `always` card + the category's card.
- `G*/` dirs — the full evidence corpus for each exemplar task: the frozen `prompt.md`, the actual
  model `runs/`, and the blind-judge `scores-*.md`. A maker may Read a winning exemplar for a
  concrete model to match.
- `SCORECARD.md` — the cross-task verdict these cards were distilled from.

## Origin
Seeded from a Fable-5-vs-Opus-4.8 benchmark on a real FastAPI+React codebase (cdp-metaql): six
tasks — code review (G1), feature slice (G2), bug root-cause (G3), SQL-builder feature (G4), UI
slice (G5), spec (G6) — each run on both models and blind-judged against source. The *lessons* in
CALIBRATION.md are general (executable tests, severity-first review, honest specs); the *exemplars*
are that codebase's. A target repo may add its own graded exemplars under `.autopilot/goldens/`.

## The load-bearing lesson
Both strong and weak models emit confident falsehoods (a plausible non-executable test; a false
"no injection path"). The goldens exist to hold agents to *demonstrated* quality, not asserted.
