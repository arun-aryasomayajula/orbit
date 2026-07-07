# G1 scoring

Partial ground truth: the 2026-07-03 six-agent review filed 61 findings into the
autopilot backlog (`ops/autopilot/backlog.yaml`, source: review) — 3 Critical
(export.py no-auth; /chart/data + /materialize cross-tenant IDOR; SessionContext
clean-unload overwrite), 24 Major, 34 Minor. Only the funnelhub-scoped subset of
those applies to this task's scope.

Score each run:
1. **Recall**: of the known funnelhub-scoped findings, how many did the run find?
2. **Precision**: of the run's findings, how many survive adversarial verification
   (spawn a skeptic agent per finding, prompted to refute)?
3. **Novel confirmed finds**: real findings NOT in the 2026-07-03 set (count double —
   this is the judgment-density signal).
4. **Blind judge**: give both runs (unlabeled A/B) to a judge agent: which review
   would you rather receive before merging? Why?

Record scores in runs/scores-<date>.md.
