#!/usr/bin/env python3
"""The task lifecycle state machine — the single source of truth for which
ledger transitions are legal. ledger.py gates every state-writing verb through
check(); autoclose.py consults it before flipping a shipped no-op to merged.
(epic_plan.py has its own, separate machine for the BACKLOG-layer epic stages —
this module governs the ledger layer only.)

The lifecycle::

    (absent) ──claim──▶ in_progress ──committed──▶ committed ──pushed──▶ pushed
                             │                        │                    │
                             ├────────escalate────────┴──────────┬─────────┤
                             ▼                                   ▼         ▼
                        escalated ──(with ship evidence)──▶ merged │ rejected
                                                              │
                                                    reverted (annotation —
                                                    state stays merged)

Two kinds of event, two levels of strictness:

- LOOP events (claim / committed / pushed / escalate) record facts that already
  happened in git. They are permissive — including late-recorded facts: an
  agent that skipped `claim` still gets its `committed` recorded, and the
  wrapper's `pushed` lands even if the agent recorded nothing. Refusing to
  record a fact loses state (run.sh swallows verb failures), so these only
  reject genuinely nonsensical moves (e.g. claiming a merged task).

- REVIEW events (merged / rejected / reverted) record human judgments and are
  strict: they require the entry to be at a reviewable stage, and merged /
  reverted additionally require ship evidence (a sha, remote_ref, or branch) —
  you cannot merge or revert work that never shipped.

Deliberate asymmetries, each covering a real flow:

- escalated → merged IS legal (with evidence): a task that committed, then
  escalated at the runtime check, whose branch the operator merged anyway —
  autoclose reconciles exactly this. escalated → rejected is the operator's
  "won't do".
- reverted is legal from committed/pushed, not just merged: git trunk ancestry
  is the authority for "merged" (a GitHub-UI merge never calls `mark`), so the
  ledger state may lag the truth the dashboard shows.
- Terminal states (merged / rejected) accept nothing further — a double-mark
  would write a duplicate merge marker and poison regression attribution.
  Operators can override any refusal with ledger.py --force (recorded).
"""

ABSENT = None                      # entry not present in the ledger yet
STATES = ("in_progress", "committed", "pushed", "escalated", "merged", "rejected")
TERMINAL = ("merged", "rejected")

# event → the states it may legally fire FROM (ABSENT = no entry yet).
TRANSITIONS = {
    "claim":     {ABSENT, "in_progress"},
    "committed": {ABSENT, "in_progress", "committed"},
    "pushed":    {ABSENT, "in_progress", "committed", "pushed"},
    "escalate":  {ABSENT, "in_progress", "committed", "pushed", "escalated"},
    "merged":    {"committed", "pushed", "escalated"},
    "rejected":  {"committed", "pushed", "escalated"},
    "reverted":  {"committed", "pushed", "merged"},
}

# event → the state it lands in ("reverted" is an annotation: state unchanged).
RESULT = {
    "claim": "in_progress", "committed": "committed", "pushed": "pushed",
    "escalate": "escalated", "merged": "merged", "rejected": "rejected",
    "reverted": None,
}

# Review events that require proof a ship exists on the entry.
SHIP_EVIDENCE = ("sha", "remote_ref", "branch")
NEEDS_EVIDENCE = ("merged", "reverted")

EVENTS = tuple(TRANSITIONS)


def allowed(current, event):
    """Is `event` legal from ledger state `current`? (Evidence not checked.)"""
    return current in TRANSITIONS.get(event, set())


def check(current, event, entry=None):
    """Full gate: return None if the event is legal, else a one-line human
    explanation of why not. `entry` (the ledger entry dict, or None) is needed
    for the ship-evidence check on merged/reverted."""
    if event not in TRANSITIONS:
        return f"unknown lifecycle event '{event}' (one of: {', '.join(EVENTS)})"
    if not allowed(current, event):
        cur = current or "(never worked)"
        legal = ", ".join(sorted(s or "(never worked)" for s in TRANSITIONS[event]))
        return f"'{event}' is not legal from state '{cur}' (legal from: {legal})"
    if event in NEEDS_EVIDENCE and not any((entry or {}).get(k) for k in SHIP_EVIDENCE):
        return (f"'{event}' requires ship evidence on the entry "
                f"(one of: {', '.join(SHIP_EVIDENCE)}) — nothing was ever shipped")
    return None
