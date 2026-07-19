#!/usr/bin/env python3
"""Autopilot task ledger — autopilot's OWN, self-contained record of which tasks
it has worked. It is the SOLE record of task lifecycle: it reads and writes
ledger.json under AP_STATE and depends on nothing external.

Why a ledger: autopilot's unit of completion is "reviewable diff produced", NOT
"task merged" (which expects a reviewed+merged PR). Tracking state here lets the
picker skip anything already in flight without touching any upstream task queue.

States: in_progress (claimed, mid-cycle) · committed (atomic commit on its branch) ·
pushed (branch on origin, awaiting review) · escalated (stopped, needs a human) ·
merged / rejected (human review outcome, terminal). Any id present here is skipped by
the picker so a task is never re-worked while its branch awaits review. Clear an entry
(or the whole file) to let autopilot pick it again.

Every state-writing verb is gated by the lifecycle state machine (lifecycle.py —
the single source of truth for legal transitions). An illegal transition prints
why and exits 3, writing nothing. `--force` overrides the gate and stamps
`forced: true` on the entry, so overrides stay auditable.

Usage:
  ledger.py claim <id> <title>          # mark in_progress
  ledger.py committed <id> <branch> <sha>  # mark committed (atomic local commit made)
  ledger.py pushed <id> <remote_ref>    # mark pushed (on remote branch, awaiting review)
  ledger.py escalate <id> <reason>      # mark escalated (needs a human)
  ledger.py pr <id> <url>               # record the PR the wrapper opened for the ship
  ledger.py attach-patch <id> <path>    # record the wrapper's captured patch (backup)
  ledger.py mark <id> merged|rejected [note]  # review outcome (feeds merge-rate metrics)
  ledger.py reverted <id> [note]        # record a post-merge revert + the operator's why
  ledger.py can <id> <event>            # would <event> be legal now? exit 0/3 (dry check)
  ledger.py reap <max_age_seconds>      # escalate entries stuck mid-cycle (in_progress/
                                        #   committed) older than the age — call only when
                                        #   no cycle is running (the age guards races)
  ledger.py state <id>                  # print the entry's state ('' if absent)
  ledger.py clear <id>                  # remove an entry so the picker can re-pick it (re-promote)
  ledger.py worked-ids                  # print every id already worked (one per line)
  ledger.py show                        # human-readable dump
  (any state-writing verb accepts --force to override the lifecycle gate)
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lifecycle


def _ap_state():
    # Resolve AP_STATE (the runtime-state dir): env AP_STATE, else $AP_HOME/state.
    # Fail loudly if neither is set — the ledger has nowhere to live otherwise.
    st = os.environ.get("AP_STATE")
    if st:
        return st
    home = os.environ.get("AP_HOME")
    if home:
        return os.path.join(home, "state")
    sys.exit("AP_STATE (and AP_HOME) unset — run inside a orbit target repo "
             "(set AP_HOME=<repo>/.autopilot, or AP_STATE explicitly)")


LEDGER = os.path.join(_ap_state(), "ledger.json")


def _now():
    # Current UTC timestamp as an ISO-8601 string.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load():
    # Read the ledger, tolerating a missing or empty/corrupt file.
    try:
        with open(LEDGER) as f:
            data = json.load(f)
            if isinstance(data, dict) and "entries" in data:
                return data
    except (FileNotFoundError, ValueError):
        pass
    return {"entries": {}}


def _save(data):
    # Atomically write the ledger (temp file + rename) so a crash never corrupts it.
    d = os.path.dirname(LEDGER)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".ledger-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, LEDGER)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _entry(data, tid):
    # Get or create the entry dict for a task id.
    return data["entries"].setdefault(str(tid), {})


def _gate(data, tid, event, force):
    # Lifecycle gate for every state-writing verb. Legal → returns False.
    # Illegal + --force → warns and returns True (caller stamps forced=true).
    # Illegal otherwise → explains on stderr and exits 3, writing nothing.
    e = data["entries"].get(str(tid))
    err = lifecycle.check((e or {}).get("state"), event, e)
    if not err:
        return False
    if force:
        print(f"task {tid}: FORCED past lifecycle gate — {err}", file=sys.stderr)
        return True
    print(f"task {tid}: illegal transition — {err} (--force to record anyway)",
          file=sys.stderr)
    sys.exit(3)


def cmd_claim(tid, title, force=False):
    # Record a task as in_progress at claim time.
    data = _load()
    forced = _gate(data, tid, "claim", force)
    e = _entry(data, tid)
    e.update(state="in_progress", title=title,
             claimed_at=e.get("claimed_at") or _now(), updated_at=_now())
    if forced:
        e["forced"] = True
    _save(data)


def cmd_committed(tid, branch, sha, force=False):
    # Mark a task committed: an atomic local commit was made on its per-task branch.
    data = _load()
    forced = _gate(data, tid, "committed", force)
    e = _entry(data, tid)
    e.update(state="committed", branch=branch, sha=sha, updated_at=_now())
    if forced:
        e["forced"] = True
    _save(data)


def cmd_pushed(tid, remote_ref, force=False):
    # Mark a task pushed: its per-task branch is on the remote, awaiting review.
    data = _load()
    forced = _gate(data, tid, "pushed", force)
    e = _entry(data, tid)
    e.update(state="pushed", remote_ref=remote_ref, updated_at=_now())
    if forced:
        e["forced"] = True
    _save(data)


def cmd_escalate(tid, reason, force=False):
    # Mark a task escalated: stopped, needs a human (see NEEDS_YOU.md).
    data = _load()
    forced = _gate(data, tid, "escalate", force)
    e = _entry(data, tid)
    e.update(state="escalated", reason=reason, updated_at=_now())
    if forced:
        e["forced"] = True
    _save(data)


def cmd_pr(tid, url):
    # Record the PR the wrapper opened for this ship (state unchanged — the PR
    # is metadata on the pushed branch; review outcome still comes via `mark`).
    data = _load()
    _entry(data, tid).update(pr_url=url, updated_at=_now())
    _save(data)


def cmd_attach_patch(tid, path):
    # Record the patch path the wrapper captured for this task (best-effort).
    data = _load()
    if str(tid) in data["entries"]:
        data["entries"][str(tid)].update(patch=path, updated_at=_now())
        _save(data)


def cmd_mark(tid, outcome, note="", force=False):
    # Record the human review outcome (merged / rejected) — the raw data behind
    # the dashboard's merge-rate-per-source metric. Keeps the entry so the
    # picker still skips the id.
    if outcome not in ("merged", "rejected"):
        print(f"invalid outcome '{outcome}' (merged|rejected)", file=sys.stderr)
        sys.exit(2)
    data = _load()
    forced = _gate(data, tid, outcome, force)
    e = _entry(data, tid)
    e.update(state=outcome, reviewed_at=_now(), updated_at=_now())
    if note:
        e["review_note"] = note
    if forced:
        e["forced"] = True
    _save(data)
    print(f"task {tid} → {outcome}")


def cmd_reverted(tid, note="", force=False):
    # Record that the operator reverted this ship after merge. State is kept
    # (the original sha stays an ancestor of base, so `merged` categorization
    # still holds) — this adds the WHY, which the calibration miner feeds on.
    data = _load()
    forced = _gate(data, tid, "reverted", force)
    e = _entry(data, tid)
    e.update(reverted_at=_now(), updated_at=_now())
    if note:
        e["revert_note"] = note
    if forced:
        e["forced"] = True
    _save(data)
    print(f"task {tid} → reverted recorded")


def cmd_reap(max_age_seconds):
    # Escalate entries stuck at a MID-CYCLE state (in_progress: claim-then-crash;
    # committed: the wrapper died between commit and push) for longer than
    # max_age_seconds. Without this they are silent limbo: worked-ids skips them
    # forever and no operator gate (digest/dashboard escalations) surfaces them.
    # Escalating is the lifecycle-legal way out — the operator's answer re-queues.
    # Prints the reaped ids space-separated (empty output = nothing reaped) so the
    # wrapper can notify. Entries without a parseable updated_at are left alone.
    try:
        max_age = int(max_age_seconds)
    except (TypeError, ValueError):
        print(f"reap: max_age_seconds must be an integer, got '{max_age_seconds}'", file=sys.stderr)
        sys.exit(2)
    data = _load()
    now = datetime.now(timezone.utc)
    reaped = []
    for tid, e in data["entries"].items():
        state = e.get("state")
        if state not in ("in_progress", "committed"):
            continue
        try:
            ts = datetime.strptime(e.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        age = (now - ts).total_seconds()
        if age <= max_age:
            continue
        e.update(state="escalated", updated_at=_now(),
                 reason=f"reaper: cycle died mid-flight (stuck at '{state}' for {int(age)}s; "
                        f"answer to re-queue, or clear to re-pick)")
        reaped.append(tid)
    if reaped:
        _save(data)
        print(" ".join(sorted(reaped)))


def cmd_can(tid, event):
    # Dry lifecycle check: would <event> be legal for this task right now?
    # Prints the verdict; exits 0 (legal) or 3 (illegal) — nothing is written.
    # Lets callers (e.g. the dashboard's rollback) validate BEFORE side effects.
    e = _load()["entries"].get(str(tid))
    err = lifecycle.check((e or {}).get("state"), event, e)
    if err:
        print(f"task {tid}: {err}", file=sys.stderr)
        sys.exit(3)
    print(f"task {tid}: '{event}' is legal from state "
          f"'{(e or {}).get('state') or '(never worked)'}'")


def cmd_state(tid):
    # Print the entry's state (empty line if the id was never worked).
    print(_load()["entries"].get(str(tid), {}).get("state", ""))


def cmd_clear(tid):
    # Remove a task's entry entirely so the picker can pick it again. Used to
    # re-promote an escalated task the operator judged the loop CAN now solve
    # (e.g. a stalled cycle that died before finishing). Returns 0 either way.
    data = _load()
    if data["entries"].pop(str(tid), None) is not None:
        _save(data)
        print(f"cleared ledger entry for '{tid}' — pickable again")
    else:
        print(f"no ledger entry for '{tid}' (nothing to clear)")


def cmd_worked_ids():
    # Print every id autopilot has already worked — the picker skips these.
    data = _load()
    for tid in sorted(data["entries"], key=lambda x: (len(x), x)):
        print(tid)


def cmd_show():
    # Human-readable dump for `install.sh status` and manual inspection.
    data = _load()
    if not data["entries"]:
        print("(ledger empty — no tasks worked yet)")
        return
    for tid in sorted(data["entries"], key=lambda x: (len(x), x)):
        e = data["entries"][tid]
        line = f"  task {tid}  [{e.get('state','?')}]  {e.get('title','')}"
        if e.get("branch"):
            line += f"\n      branch: {e['branch']}" + (f" @ {e['sha'][:10]}" if e.get("sha") else "")
        if e.get("remote_ref"):
            line += f"\n      remote: {e['remote_ref']}"
        if e.get("pr_url"):
            line += f"\n      pr: {e['pr_url']}"
        if e.get("patch"):
            line += f"\n      patch: {e['patch']}"
        if e.get("reason"):
            line += f"\n      reason: {e['reason']}"
        print(line)


def main(argv):
    force = "--force" in argv
    argv = [a for a in argv if a != "--force"]
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "claim":
            cmd_claim(rest[0], rest[1] if len(rest) > 1 else "", force=force)
        elif cmd == "committed":
            cmd_committed(rest[0], rest[1], rest[2], force=force)
        elif cmd == "pushed":
            cmd_pushed(rest[0], rest[1], force=force)
        elif cmd == "escalate":
            cmd_escalate(rest[0], rest[1] if len(rest) > 1 else "", force=force)
        elif cmd == "pr":
            cmd_pr(rest[0], rest[1])
        elif cmd == "attach-patch":
            cmd_attach_patch(rest[0], rest[1])
        elif cmd == "mark":
            cmd_mark(rest[0], rest[1], rest[2] if len(rest) > 2 else "", force=force)
        elif cmd == "reverted":
            cmd_reverted(rest[0], rest[1] if len(rest) > 1 else "", force=force)
        elif cmd == "can":
            cmd_can(rest[0], rest[1])
        elif cmd == "reap":
            cmd_reap(rest[0])
        elif cmd == "state":
            cmd_state(rest[0])
        elif cmd == "clear":
            cmd_clear(rest[0])
        elif cmd == "worked-ids":
            cmd_worked_ids()
        elif cmd == "show":
            cmd_show()
        else:
            print(f"unknown command: {cmd}", file=sys.stderr)
            return 2
    except IndexError:
        print(f"missing argument for '{cmd}' — see ledger.py with no args", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
