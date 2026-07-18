# Autonomy expansion — Jira intake, fast human gates, runtime proof, regression attribution, goldens learning loop

Status: **draft — awaiting operator approval** (then build slice by slice, one commit per slice-step).
Predecessor: `done/2026-07-17-sdlc-expansion.md` (intake / epics / PRs / signals / dashboard).

## Why

The 2026-07-17 expansion gave Orbit the left half of the lifecycle. What's still manual
or missing, in observed-pain order:

1. **The backlog entry point is still hand-fed.** Product work lives in Jira; a human
   re-types it into `backlog.yaml`. Tickets arrive thin (no acceptance criteria), so
   even when copied they fail the hard gate or force guessing.
2. **The human gates are slow, not just human.** Merge and queue decisions wait for the
   operator to *open the dashboard*. A gate that takes days makes the loop days-slow;
   the same gate answered from a phone notification in minutes keeps the loop honest
   AND fast.
3. **Verification proves the diff, not the product.** The metaql audit found ships that
   were gate-green and verifier-approved but unobserved in the running app ("fixed but
   not fixed"). Nothing drives the affected flow.
4. **Ships are unowned after merge.** Signal adapters propose work open-loop; nothing
   attributes a new production error to the ship that caused it.
5. **The loop never learns.** Every merge, rejection, revert, human branch-amendment,
   and escalation resolution is recorded in the ledger and then ignored. Meanwhile the
   goldens system (graded exemplars + CALIBRATION cards briefed into every cycle)
   already exists as the delivery mechanism for exactly this kind of lesson — it's just
   frozen at its 2026-07-06 seed corpus.

**Operator decisions carried into this spec:** merge stays fully human — **no CI
watcher, no auto-merge, no release automation** (re-affirmed 2026-07-18). The learning
loop must feed the **goldens/calibration system**, capturing feedback automatically
from agents and from the human-in-the-loop, so recurring mistakes stop recurring.

## Non-goals

- CI watching, auto-merge, deploy/release automation (operator decision).
- Editing the engine's seed goldens corpus (`goldens/G*`, `goldens/CALIBRATION.md`) —
  learned material is target-local.
- Jira write-side workflow automation beyond comment + transition on our own tickets.
- Parallel build lanes (worth doing; separate epic — it touches the loop's core
  one-task invariant and deserves its own spec).

## Invariants (must stay true through every slice)

1. **Merge and queue are human acts.** The only relaxation in this spec: a human
   applying the configured Jira ready-label *is* the queue act — and even that is
   opt-in (`jira.auto_queue`, default `false`; default behavior lands `proposed`).
2. **Credentials are wrapper-only.** Jira token lives in `$AP_HOME/.jira_token`
   (one line, gitignored — same pattern as `.slack_webhook`). Engine code reads it;
   no agent prompt ever contains it; all Jira REST calls happen in engine Python.
3. **A ticket without buildable acceptance criteria gets questions, not a build.**
   Enrichment output must pass the existing acceptance-criteria hard gate or the
   engine posts the agent's specific questions back to the ticket and skips import.
4. **All backlog writes go through `backlog_append.py`** (unchanged from predecessor).
   Jira import is idempotent by ticket key (key ↔ task id mapping in state).
5. **Every outward side-effect is best-effort, degrade-to-log.** Notifications, Jira
   comments/transitions, runtime checks, digests — none may ever fail a cycle.
   (Same posture as `raise_pr` and `notify.py` today.)
6. **The loop learns by proposing, never by self-modifying.** The calibration miner
   writes *candidate* exemplars and *proposed* card edits; nothing is briefed into
   agents until a human approves it. Every proposed lesson must cite its ledger
   evidence (task ids, decisions, diffs) — no vibes-based calibration.
7. **Engine goldens are seed; learned goldens are target-local** (`.autopilot/goldens/`
   — the extension point the goldens README already reserves).

---

## Slice 1 — Jira intake adapter + requirement enrichment

The emptiest part of the pipeline, and the operator's priority.

- **Config** (`.autopilot/config.yaml`):
  ```yaml
  jira:
    base_url: https://<org>.atlassian.net
    project: CDP                # issues considered
    ready_label: orbit-ready    # human sets this = human queue/triage act
    auto_queue: false           # true → enriched+gated tasks land queued, not proposed
    writeback: true             # comment PR link on ship; transition on merge
  ```
  Token: `$AP_HOME/.jira_token` (`email:api_token` for cloud basic auth). `doctor`
  checks reachability read-only when a `jira:` block exists.
- **`engine/jira_intake.py`** — a *source adapter* under the existing signals contract
  (invoked by `run_source_adapters`, env AP_HOME/AP_STATE/ORBIT_HOME, cwd = repo root),
  so target repos can override/extend it like any adapter. Steps per poll:
  1. JQL: project + ready_label, not yet in `state/jira_map.json`.
  2. For each ticket, run a headless **enrichment agent** (claude -p, same wrapper
     pattern as intake): read ticket text/comments + survey the code it touches →
     draft task block (title, category, context with evidence file:line refs,
     acceptance criteria).
  3. **Gate the enrichment**: criteria must be concrete and verifiable (reuse the
     backlog lint / hard-gate check). Pass → `backlog_append` (status per
     `auto_queue`). Fail → engine posts the agent's questions as a Jira comment,
     applies `orbit-needs-info`, records the attempt; ticket is retried only after
     a new human comment appears.
- **Writeback** (engine-side, never agent): on PR raised → comment the PR URL on the
  ticket; on dashboard **merge** action → comment + transition (configurable target
  status); on **reject** → comment the operator's reason (see Slice 2).
- **Acceptance criteria for the slice**: a labeled well-formed ticket becomes a
  gated backlog task with evidence-backed criteria and no human typing; a thin ticket
  receives specific questions on the ticket and is NOT imported; deleting the label
  never duplicates on re-poll; `.jira_token` absent → adapter warns loudly and no-ops;
  no agent transcript contains the token.

## Slice 2 — Fast human gates: actionable notifications, richer review packets, reject reasons, daily digest

Once Jira feeds volume in, operator latency is the bottleneck. Merge stays human;
make the human's decision take a minute, not a session.

- **`notify.py` events**: extend to typed events (`escalation`, `ship_ready`,
  `needs_info`, `digest`) with a link to the relevant dashboard anchor
  (`dashboard_url` config, default `http://127.0.0.1:8787`). Slack incoming webhooks
  are one-way — buttons are out of scope; links are the action path.
- **Review packet upgrades** (`review_packet.py`): a *decision header* — risk notes
  (files touched, category, proximity to escalation-list surfaces), "look here first"
  (the diff hunks the verifier judged most load-bearing), evidence links (gate output,
  runtime evidence once Slice 3 lands), and the one-click revert pointer
  (`state/diffs/` patch) that already exists but isn't surfaced.
- **Reject/revert reasons**: dashboard reject and revert actions require a one-line
  reason, stored in the ledger. This is cheap here and is the *human feedback channel
  Slice 5 mines* — without it the learning loop has decisions but no why.
- **`orbit digest`** verb + optional scheduled send: ships awaiting merge (with age),
  open escalations, needs-info tickets, yesterday's ledger summary, spend.

## Slice 3 — Runtime verification: prove it in the product

Kills the "fixed but not fixed" class. Evidence for the human, not a new merge gate.

- **Config**: optional `runtime_check:` block — how to launch the app (or attach to a
  dev server) and, per category, whether a check is expected (`ui: required`,
  default `off`).
- After verify-spec, for eligible tasks, a **runtime agent** drives the affected flow
  (browser for `frontend`, CLI/API invocation otherwise) and writes evidence —
  screenshot/trace/output — into the review packet. Wrapper-launched, sandboxed to
  read-only on the repo.
- Degrade-to-log (invariant 5) — but when a category is configured `required` and the
  check *ran and observably contradicted* an acceptance criterion, the cycle routes to
  escalation instead of ship (same path as verifier rejection). Unable-to-run ≠ failed.
- **Acceptance criteria**: a UI task's packet contains a screenshot of the changed
  flow; a task whose runtime check contradicts criteria escalates with the trace; a
  repo with no `runtime_check:` block behaves exactly as today.

## Slice 4 — Post-ship regression attribution

- Dashboard **merge** action records a ledger marker (ship id, merge time, baseline
  signal snapshot via the logwatch adapter's signature extraction).
- `logwatch` gains a compare mode: post-merge window vs baseline; **new** error
  signatures → one `proposed` task per signature, `context` linking the suspect ship
  (task id, PR, revert patch path). Attribution is *suspicion with evidence*, stated
  as such — the human triages; nothing auto-reverts.
- **Acceptance criteria**: an error signature first seen after a merge produces a
  proposed task naming that ship and its revert patch; pre-existing signatures do not;
  repos without logwatch configured are untouched.

## Slice 5 — The goldens learning loop (calibration miner)

The operator's emphasis: feedback captured **automatically from agents and from the
human in between**, flowing into the golden-dataset system, so the loop makes fewer
mistakes going forward. The delivery mechanism already exists — goldens/CALIBRATION
cards are briefed into maker/checker/verifier every cycle; this slice writes the
*intake* side of that system.

**Feedback sources** (all already recorded or added by Slice 2 — no new agent chatter):

| source | signal | who produced it |
|---|---|---|
| ledger | gate failures, checker retries, verifier rejections, escalations per category | agents (automatic) |
| ledger | merge / reject / revert + one-line reason | human |
| git | **review-delta**: human amendments to a review branch before merge (diff of ship vs merged) | human |
| dashboard | escalation resolutions | human |

**`engine/calibration_miner.py`** (+ `orbit learn <repo>` verb, also runnable on a
schedule): mines the above since its last watermark and emits, **all as proposals**:

1. **Candidate exemplars** → `.autopilot/goldens/candidates/<task-id>/` — a ship
   merged *untouched* in a category becomes a positive exemplar (prompt, diff, packet);
   a rejected/reverted ship becomes an anti-pattern entry carrying the human's reason.
2. **Proposed card edits** → a diff against the *target-local* calibration file
   (`.autopilot/goldens/CALIBRATION.md`, briefed alongside the engine's card per the
   existing `goldens:` router block), each lesson citing its ledger evidence. Recurring
   review-deltas are the strongest input: "human moved X in 3 of last 5 frontend
   merges" is a card line the builder should see.
3. **Escalation memory** → resolved escalations distilled into proposed track facts,
   so the same class of question doesn't escalate twice.

A human approves candidates/edits on the dashboard (small **Calibration** strip:
pending candidates, approve → promoted into the briefed card/corpus; reject →
archived with reason — which is itself miner input). Until approval, nothing changes
what agents are briefed.

**Measurement (so "fewer mistakes" is a number, not a feeling)**: `orbit report` —
per-category trend of verifier-rejection rate, human-reject rate, review-delta size,
escalation repeat rate. The learning loop's success criterion is these curves bending
down after cards land; a card that doesn't move its curve is a candidate for removal.

**Acceptance criteria for the slice**: a rejected ship with a reason produces a cited
anti-pattern candidate; an untouched merge produces a positive candidate; approving a
card edit changes the next cycle's briefing for that category (verifiable in the
transcript); nothing in `goldens/` (engine) is modified; `orbit report` prints the
four trend lines from ledger data alone.

---

## Build order & dependencies

1 (Jira) and 2 (gates/notify) are independent — build 1 first (operator priority),
then 2 immediately (1 creates the volume that makes 2 urgent). 2's **reject reasons**
are a hard prerequisite for 5. 3 (runtime) is independent. 4 depends on the merge
marker (2's dashboard work) + existing logwatch. 5 last — it mines what 1–4 record.

## Open questions (resolve before the affected slice, not before starting)

- Jira auth flavor: cloud basic (`email:token`) vs PAT for server/DC — detect from
  config or add `jira.auth: basic|pat`. (Slice 1)
- Enrichment retry policy on needs-info tickets: poll comments vs require label
  re-add. Lean: re-add label = human says "answered". (Slice 1)
- Dashboard reachability for notification links off-LAN (tailscale/port-forward is
  operator's problem; we just make the URL configurable). (Slice 2)
- Review-delta capture point: merged-commit vs ship-branch diff is trivial when the
  human merges via dashboard; document that out-of-band merges lose the delta. (Slice 5)
