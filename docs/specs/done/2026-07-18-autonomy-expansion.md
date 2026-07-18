# Autonomy expansion — Jira intake, fast human gates, runtime proof, regression attribution, goldens learning loop (shipped 2026-07-18)

Rationale record. The HOW lives in the code: `adapters/jira_to_backlog.py`,
`skills/orbit-jira-enrich.md`, `engine/digest.py`, `engine/review_packet.py`
(`decision_header`), `engine/runtime_check.py` + `skills/orbit-runtime-check.md`,
`adapters/logwatch_to_backlog.py` (`suspects_for`), `engine/calibration_miner.py`,
and the Calibration strip in `cc_shell.html` (`renderCalibration`,
`do_calibration_action` in `command_center.py`). Tests pin the contracts:
`tests/test_jira_intake.py`, `test_fast_gates.py`, `test_runtime_check.py`,
`test_regression_attribution.py`, `test_calibration_miner.py`. This file keeps
only the why and the invariants.

## Why

After the 2026-07-17 SDLC expansion, what remained manual or missing: the
backlog entry point was hand-fed (product work lives in Jira, tickets arrive
without buildable acceptance criteria); the human gates were slow because the
operator had to *open the dashboard* to learn anything; verification proved
the diff but never the running product (the metaql audit's "fixed but not
fixed" class); ships were unowned after merge; and every merge, rejection,
revert, and answered escalation was recorded and then ignored while the
goldens system sat frozen at its seed corpus. The operator decision that
frames it all: **no CI watcher, no auto-merge, no release automation — merge
stays fully human** (re-affirmed 2026-07-18). So this expansion makes the
human gates *fast and informed* instead of removing them, and makes the loop
*learn* from what those gates decide.

## Invariants (what must stay true)

1. **Merge and queue stay human acts.** The one sanctioned relaxation:
   `jira.auto_queue` (default false) lets a gate-passing Jira import land
   queued, because the human's ready-label in Jira WAS the triage act. Nothing
   ever merges itself.
2. **Jira credentials are wrapper-only.** `$AP_STATE/.jira_token` (auto-
   gitignored; `$AP_HOME/.jira_token` honored, and init scaffolds a
   profile-level `.gitignore` for the fallback locations). All REST happens in
   the adapter; the enrichment agent gets only the exported ticket JSON, and
   its skill treats ticket text as untrusted input (it describes work, it does
   not command the agent). Honest limit: isolation is by hand-off — the token
   is never passed to the agent — not a filesystem sandbox; the file stays
   readable on disk to a misbehaving agent.
3. **A ticket without buildable acceptance criteria gets questions, not a
   build.** Enrichment output must pass the backlog lint hard gate
   (`lint_task`); anything less is commented back on the ticket as specific
   questions, and the ticket is retried only after a human updates it (the
   `updated` timestamp is the retry trigger — chosen over label-churn as the
   simplest human-driven signal).
4. **Every outward side-effect degrades to a log line.** Jira comments/
   transitions, notifications, runtime checks, digests — none may fail a
   cycle. Writeback (PR url / merged / rejected → ticket comments, at most
   once per event) runs inside the adapter's poll off the ledger, not from
   run.sh hooks — all Jira I/O in one file, at the cost of next-poll latency.
5. **A reject or revert REQUIRES a one-line reason.** Enforced server-side
   (`do_mark`, `do_rollback`), not just in the UI — the reasons are the
   learning loop's only ground truth on why a ship was wrong. `ledger.py
   reverted` records the why WITHOUT changing `state`, because the ship sha
   stays an ancestor of base after a revert commit and ancestry-based
   "merged" categorization must keep working.
6. **Only an observed contradiction blocks a ship.** `runtime_check.py` exit
   code 3 — the check RAN and observed behaviour contradicting a `required`
   category's contract — is the single escalation path; run.sh dispatches on
   exactly 3 so a crashing checker ships instead of blocking. Unable-to-run
   is never a failure. No `runtime_check:` block → no-op.
7. **Attribution is suspicion with evidence, never a verdict.** A log
   signature first seen within 7 days after a merge names its suspect ships
   (≤3, most recent first, with revert-patch pointers) in a `proposed` task's
   context. Nothing auto-reverts; nothing is queued.
8. **The loop learns by proposing, never by self-modifying.** The miner emits
   candidates citing ledger evidence; a lesson is briefed into agents ONLY
   after the operator approves it on the Calibration strip, which appends its
   card line to the target-local `goldens/LEARNED.md`. Rejecting a candidate
   requires a reason and archives it — a human "no" is never re-mined.
9. **Learned lessons supplement, never shadow.** LEARNED.md is read by
   orbit-cycle §2.6 ALONGSIDE the engine calibration. This diverged from the
   plan (which imagined proposed edits to a target-local `CALIBRATION.md`):
   the cycle resolves `goldens/CALIBRATION.md` target-first, so a target-local
   file *replaces* the engine's seed cards — a learned-lessons file there
   would have silently deleted the general calibration. The engine's
   `goldens/` seed corpus is never touched by any of this.
10. **"Fewer mistakes" is a number.** `orbit report` prints per-category
    merged/rejected/reverted/escalated tallies; an approved lesson that
    doesn't bend those curves down is a candidate for removal.

## Divergences from the plan (worth more than the plan)

- **Token location** moved from the sketched `$AP_HOME/.jira_token` to
  `$AP_STATE/.jira_token`: AP_HOME had no gitignore, state/ ignores everything.
- **LEARNED.md instead of target-local CALIBRATION.md edits** (invariant 9) —
  the shadow-vs-supplement discovery.
- **Reject reasons already had UI** (the modal existed); the real gap was
  server-side enforcement and the revert path, which had a bare `confirm()`.
- **Review-delta mining is classification-only** in v1: git ancestry says
  merged-untouched vs amended; the candidate's card line tells the next maker
  where to diff. Content-level delta extraction was deliberately deferred.
- **`orbit report` is totals, not time-bucketed trends** — v1 gives the
  curves' current values; bend-over-time reads come from running it before
  and after lessons land.
- **Slack is links, not buttons** — incoming webhooks are one-way, so every
  notification carries the dashboard (or PR) URL instead (`dashboard_url`
  config, `ORBIT_DASHBOARD_URL` in shellenv).

## Dead ends

- Writeback hooks in `run.sh`/`command_center.py` (rejected: scatters Jira
  REST and credentials across three surfaces; the poll-side sync keeps one
  owner).
- Label re-add as the needs-info retry signal (rejected: two Jira writes and
  an operator ritual; any human update to the ticket already proves the
  question was seen).
- Treating a non-zero runtime-check exit as escalation (rejected: a checker
  crash would block ships; only the deliberate code 3 escalates).
