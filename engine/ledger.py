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
`forced: true` on the entry, so overrides stay auditable. Every state-writing
verb also goes through one shared lock (`_locked_update`, below) around its
whole load-modify-save cycle, so two concurrent ledger.py invocations can
never lose one's update to the other's whole-file rewrite.

Other exit codes: 2 (bad arguments — nothing written), 5 (`claim --exclusive`
refused a non-reclaimable entry — nothing written), 6 (could not acquire the
ledger lock within its timeout — nothing written; some other holder is
either alive and slow, or genuinely wedged).

Usage:
  ledger.py claim <id> <title>          # mark in_progress
  ledger.py claim <id> <title> --exclusive --pid P --slot S
                                         # pid-aware exclusive claim: under a
                                         # real flock on the ledger, refuses
                                         # (exit 5, nothing written) a tid
                                         # that already has a non-reclaimable
                                         # entry (in_progress with a live pid,
                                         # committed, pushed, merged, ...); an
                                         # in_progress entry whose worker_pid
                                         # is dead IS reclaimable (takeover -
                                         # a sanctioned `reclaim` lifecycle
                                         # event; records the new pid/slot,
                                         # resets claimed_at, and appends to
                                         # a reclaimed_from list). --pid and
                                         # --slot require --exclusive (exit 2
                                         # otherwise). Non-exclusive claim (no
                                         # --exclusive) is unchanged except
                                         # that re-claiming an in_progress
                                         # task now needs --force (see
                                         # lifecycle.py).
  ledger.py committed <id> <branch> <sha>  # mark committed (atomic local commit made)
  ledger.py pushed <id> <remote_ref>    # mark pushed (on remote branch, awaiting review)
  ledger.py escalate <id> <reason>      # mark escalated (needs a human)
  ledger.py pr <id> <url>               # record the PR the wrapper opened for the ship
  ledger.py attach-patch <id> <path>    # record the wrapper's captured patch (backup)
  ledger.py attach <id> screenshot|log|report <path> [label]  # record a QA artifact
  ledger.py qa-verdict <id> pass|fail|unable [summary]  # record the QA gate's verdict
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
import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lifecycle
import locks

# locks.pid_alive is the public name (a parallel fix promoted it from the
# private _pid_alive this module originally called); fall back to the
# private name so this keeps working regardless of which lands first.
_pid_alive = getattr(locks, "pid_alive", None) or locks._pid_alive


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
LEDGER_LOCK = os.path.join(os.path.dirname(LEDGER), ".ledger.lock")


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


@contextlib.contextmanager
def _locked_update():
    # The one lock chokepoint every ledger state-writing verb goes through:
    # holds a real flock (locks.hold) around a load-modify-save cycle so two
    # concurrent ledger.py invocations can never lose an update to a
    # whole-file rewrite (_save always replaces the ENTIRE file, so two
    # overlapping load-then-save cycles racing on DIFFERENT tids would
    # otherwise silently drop whichever one wrote first). Yields the loaded
    # ledger dict for the caller to mutate in place; saves ONLY on a clean
    # exit — any exception (including the sys.exit a lifecycle refusal
    # raises) skips the save, matching every verb's "illegal -> nothing
    # written" contract. Callers must stay millisecond-fast in here and
    # NEVER invoke a subprocess (see locks.py's module docstring on why
    # holding a lock across a long-lived child is dangerous).
    with locks.hold(LEDGER_LOCK):
        data = _load()
        yield data
        _save(data)


def _slot_worker_matches(slot, tid):
    # True iff $AP_STATE/workers/<slot>.pid names a LIVE pid that is, right
    # now, recorded as working THIS exact tid. The slot loop (co-owned by a
    # later task) writes/refreshes this file immediately after claiming a
    # task, so it is the only signal that actually answers "is this specific
    # task still in flight" — an entry's claim-time worker_pid is the loop's
    # own long-lived pid, which stays alive across many tasks and would make
    # the reaper a permanent no-op if trusted directly (the bug an
    # adversarial review caught in the slot loop's wiring). A missing file,
    # a dead pid, or a pid file naming a DIFFERENT tid (the slot moved on;
    # this one is orphaned) all mean "not verified live".
    if slot is None:
        return False
    path = os.path.join(_ap_state(), "workers", f"{slot}.pid")
    try:
        with open(path) as f:
            info = json.load(f)
    except (OSError, ValueError):
        return False
    pid = info.get("pid")
    if pid is None or not _pid_alive(pid):
        return False
    return str(info.get("tid")) == str(tid)


def cmd_claim(tid, title, force=False, exclusive=False, pid=None, slot=None):
    # Record a task as in_progress at claim time. Non-exclusive (no --pid):
    # today's behavior exactly, unchanged except that lifecycle.py now makes
    # re-claiming an in_progress task illegal by default (see its docstring).
    if not exclusive:
        with _locked_update() as data:
            forced = _gate(data, tid, "claim", force)
            e = _entry(data, tid)
            e.update(state="in_progress", title=title,
                     claimed_at=e.get("claimed_at") or _now(), updated_at=_now())
            if forced:
                e["forced"] = True
        return
    cmd_claim_exclusive(tid, title, pid, slot, force=force)


def cmd_claim_exclusive(tid, title, pid, slot, force=False):
    # Exclusive, pid-aware claim: the whole point is a single winner when two
    # workers race the same tid, so the check-then-write has to happen under
    # a real lock with the ledger reloaded INSIDE it (a load taken before the
    # lock could be stale by the time we act on it). Refuses (exit 5, nothing
    # written) a tid that already has a non-reclaimable entry: in_progress
    # with a live worker_pid, or any reviewable/terminal state (committed,
    # pushed, merged, ...). An in_progress entry whose recorded worker_pid is
    # dead IS reclaimable — takeover, not refusal — since the loop invariant
    # this exists to protect ("at most one live worker per tid") already
    # holds; it just needs the entry updated to the new worker. The takeover
    # is routed through lifecycle as a sanctioned `reclaim` event (legal only
    # from in_progress) rather than a bare field mutation, so "every
    # state-writing verb is gated" stays true.
    if pid is None:
        print("claim --exclusive requires --pid P", file=sys.stderr)
        sys.exit(2)
    with _locked_update() as data:
        e = data["entries"].get(str(tid))
        reclaimed_from = None
        if e is not None:
            state = e.get("state")
            live_pid = e.get("worker_pid")
            reclaimable = state == "in_progress" and live_pid is not None \
                and not _pid_alive(live_pid)
            if not reclaimable:
                detail = (f"in_progress, worker_pid {live_pid} still live" if state == "in_progress"
                          else f"state '{state}'")
                if force:
                    print(f"task {tid}: FORCED exclusive claim over a non-reclaimable entry "
                          f"({detail})", file=sys.stderr)
                else:
                    print(f"task {tid}: exclusive claim refused — non-reclaimable entry "
                          f"({detail}) (--force to take over anyway)", file=sys.stderr)
                    sys.exit(5)
            else:
                reclaimed_from = live_pid
                _gate(data, tid, "reclaim", force=False)  # asserts the event's legality (always true here)
        entry = _entry(data, tid)
        claimed_at = _now() if reclaimed_from is not None else (entry.get("claimed_at") or _now())
        entry.update(state="in_progress", title=title, worker_pid=pid, slot=slot,
                     claimed_at=claimed_at, updated_at=_now())
        if reclaimed_from is not None:
            entry.setdefault("reclaimed_from", []).append(reclaimed_from)
        if force and e is not None and reclaimed_from is None:
            entry["forced"] = True


def cmd_committed(tid, branch, sha, force=False):
    # Mark a task committed: an atomic local commit was made on its per-task branch.
    with _locked_update() as data:
        forced = _gate(data, tid, "committed", force)
        e = _entry(data, tid)
        e.update(state="committed", branch=branch, sha=sha, updated_at=_now())
        if forced:
            e["forced"] = True


def cmd_pushed(tid, remote_ref, force=False):
    # Mark a task pushed: its per-task branch is on the remote, awaiting review.
    with _locked_update() as data:
        forced = _gate(data, tid, "pushed", force)
        e = _entry(data, tid)
        e.update(state="pushed", remote_ref=remote_ref, updated_at=_now())
        if forced:
            e["forced"] = True


def cmd_escalate(tid, reason, force=False):
    # Mark a task escalated: stopped, needs a human (see NEEDS_YOU.md).
    with _locked_update() as data:
        forced = _gate(data, tid, "escalate", force)
        e = _entry(data, tid)
        e.update(state="escalated", reason=reason, updated_at=_now())
        if forced:
            e["forced"] = True


def cmd_pr(tid, url):
    # Record the PR the wrapper opened for this ship (state unchanged — the PR
    # is metadata on the pushed branch; review outcome still comes via `mark`).
    with _locked_update() as data:
        _entry(data, tid).update(pr_url=url, updated_at=_now())


def cmd_route(tid, route_json):
    # Attach the claim-time resolved route to an EXISTING entry — metadata,
    # not a lifecycle event, same family as cmd_pr/cmd_attach (no _gate).
    # Refuses a missing entry: the recorder runs right after a won claim, so
    # "no entry" means the caller is wrong, not the ledger. sys.exit inside
    # _locked_update skips the save — refusals write nothing.
    try:
        route = json.loads(route_json)
    except ValueError:
        print("route: VALUE must be valid JSON", file=sys.stderr)
        sys.exit(2)
    if not isinstance(route, dict):
        print("route: VALUE must be a JSON object", file=sys.stderr)
        sys.exit(2)
    with _locked_update() as data:
        e = data["entries"].get(str(tid))
        if e is None:
            print(f"task {tid}: no ledger entry to attach a route to",
                  file=sys.stderr)
            sys.exit(4)
        e["route"] = route
        e["updated_at"] = _now()


def cmd_attach_patch(tid, path):
    # Record the patch path the wrapper captured for this task (best-effort).
    with _locked_update() as data:
        if str(tid) in data["entries"]:
            data["entries"][str(tid)].update(patch=path, updated_at=_now())


ATTACHMENT_KINDS = ("screenshot", "log", "report")
QA_VERDICTS = ("pass", "fail", "unable")


def cmd_attach(tid, kind, path, label=""):
    # Record a QA-produced artifact (screenshot/log/report) for this task —
    # metadata only, no lifecycle transition, no `state` change. Mirrors
    # cmd_attach_patch: an absent entry is left untouched (silent no-op)
    # rather than auto-vivified like cmd_pr does.
    if kind not in ATTACHMENT_KINDS:
        print(f"invalid attachment kind '{kind}' ({'|'.join(ATTACHMENT_KINDS)})", file=sys.stderr)
        sys.exit(2)
    with _locked_update() as data:
        if str(tid) in data["entries"]:
            e = data["entries"][str(tid)]
            e.setdefault("attachments", []).append(
                {"kind": kind, "path": path, "label": label, "at": _now()})
            e["updated_at"] = _now()


def cmd_qa_verdict(tid, verdict, summary=""):
    # Record the QA gate's verdict for this task — metadata only, no
    # lifecycle transition, no `state` change. Overwrites on repeat (latest
    # wins). Mirrors cmd_attach_patch: an absent entry is a silent no-op.
    if verdict not in QA_VERDICTS:
        print(f"invalid qa verdict '{verdict}' ({'|'.join(QA_VERDICTS)})", file=sys.stderr)
        sys.exit(2)
    with _locked_update() as data:
        if str(tid) in data["entries"]:
            e = data["entries"][str(tid)]
            e["qa"] = {"verdict": verdict, "summary": summary, "at": _now()}
            e["updated_at"] = _now()


def cmd_mark(tid, outcome, note="", force=False):
    # Record the human review outcome (merged / rejected) — the raw data behind
    # the dashboard's merge-rate-per-source metric. Keeps the entry so the
    # picker still skips the id.
    if outcome not in ("merged", "rejected"):
        print(f"invalid outcome '{outcome}' (merged|rejected)", file=sys.stderr)
        sys.exit(2)
    with _locked_update() as data:
        forced = _gate(data, tid, outcome, force)
        e = _entry(data, tid)
        e.update(state=outcome, reviewed_at=_now(), updated_at=_now())
        if note:
            e["review_note"] = note
        if forced:
            e["forced"] = True
    print(f"task {tid} → {outcome}")


def cmd_reverted(tid, note="", force=False):
    # Record that the operator reverted this ship after merge. State is kept
    # (the original sha stays an ancestor of base, so `merged` categorization
    # still holds) — this adds the WHY, which the calibration miner feeds on.
    with _locked_update() as data:
        forced = _gate(data, tid, "reverted", force)
        e = _entry(data, tid)
        e.update(reverted_at=_now(), updated_at=_now())
        if note:
            e["revert_note"] = note
        if forced:
            e["forced"] = True
    print(f"task {tid} → reverted recorded")


def cmd_reap(max_age_seconds):
    # Escalate entries stuck at a MID-CYCLE state (in_progress: claim-then-crash;
    # committed: the wrapper died between commit and push) for longer than
    # max_age_seconds. Without this they are silent limbo: worked-ids skips them
    # forever and no operator gate (digest/dashboard escalations) surfaces them.
    # Escalating is the lifecycle-legal way out — the operator's answer re-queues.
    # Prints the reaped ids space-separated (empty output = nothing reaped) so the
    # wrapper can notify. Entries without a parseable updated_at are left alone.
    #
    # Identity-aware (NOT pid-alone): an entry's claim-time worker_pid is the
    # slot LOOP's own long-lived pid, which keeps running across many tasks —
    # trusting it directly here would make the reaper a permanent no-op for
    # as long as that loop is alive, whether or not it is still working THIS
    # task (the bug an adversarial review caught in the slot loop's wiring).
    # The only signal that answers "is this specific task still in flight" is
    # $AP_STATE/workers/<slot>.pid: the slot loop writes/refreshes it right
    # after claiming, naming both its pid and the tid it's currently on
    # (see _slot_worker_matches). A match → skip (logged, not escalated). A
    # mismatch, dead pid, or missing file → escalate immediately, same as a
    # legacy entry with no slot at all (no verified signal, no benefit of
    # the doubt). Documented invariant change from Task 2's original
    # worker_pid-trusting version; full docs land in T10.
    #
    # Unconditional backstop: past 3x max_age, escalate regardless of any
    # liveness signal — a wedged or endlessly-refreshed pid file must not be
    # able to keep an entry in limbo forever.
    try:
        max_age = int(max_age_seconds)
    except (TypeError, ValueError):
        print(f"reap: max_age_seconds must be an integer, got '{max_age_seconds}'", file=sys.stderr)
        sys.exit(2)
    now = datetime.now(timezone.utc)
    reaped = []
    with _locked_update() as data:
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
            backstop = age > 3 * max_age
            if not backstop and _slot_worker_matches(e.get("slot"), tid):
                print(f"reap: task {tid} stuck at '{state}' for {int(age)}s but slot "
                      f"'{e.get('slot')}' worker is still alive and on this task — skipping",
                      file=sys.stderr)
                continue
            reason = (f"reaper: cycle died mid-flight (stuck at '{state}' for {int(age)}s; "
                      f"answer to re-queue, or clear to re-pick)")
            if backstop:
                print(f"reap: task {tid} backstop fired — escalating after {int(age)}s "
                      f"(> {3 * max_age}s = 3x timeout) regardless of any liveness signal",
                      file=sys.stderr)
                reason = "reaper: backstop escalation (3x timeout exceeded); " + reason
            e.update(state="escalated", updated_at=_now(), reason=reason)
            reaped.append(tid)
    if reaped:
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
    with _locked_update() as data:
        removed = data["entries"].pop(str(tid), None) is not None
    if removed:
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
        if e.get("worker_pid"):
            line += f"\n      worker: pid {e['worker_pid']}" + (f" slot {e['slot']}" if e.get("slot") else "")
            if e.get("reclaimed_from"):
                dead_pids = ", ".join(str(p) for p in e["reclaimed_from"])
                line += f" (reclaimed from dead pid(s) {dead_pids})"
        for a in e.get("attachments") or []:
            line += (f"\n      attach[{a.get('kind')}]: {a.get('path')}"
                      + (f" — {a['label']}" if a.get("label") else ""))
        if e.get("qa"):
            q = e["qa"]
            line += (f"\n      qa: {q.get('verdict')}"
                      + (f" — {q.get('summary')}" if q.get("summary") else ""))
        print(line)


def _pop_flag_value(argv, flag):
    # Remove `flag VALUE` from argv in place and return VALUE, or None if the
    # flag isn't present. Exits 2 if the flag is present with no value.
    if flag not in argv:
        return None
    i = argv.index(flag)
    if i + 1 >= len(argv):
        print(f"missing value for {flag}", file=sys.stderr)
        sys.exit(2)
    val = argv[i + 1]
    del argv[i:i + 2]
    return val


def main(argv):
    argv = list(argv)
    force = "--force" in argv
    argv = [a for a in argv if a != "--force"]
    exclusive = "--exclusive" in argv
    argv = [a for a in argv if a != "--exclusive"]
    pid = _pop_flag_value(argv, "--pid")
    slot = _pop_flag_value(argv, "--slot")
    if (pid is not None or slot is not None) and not exclusive:
        print("--pid/--slot require --exclusive (no silent non-exclusive degrade)", file=sys.stderr)
        return 2
    if pid is not None:
        try:
            pid = int(pid)
        except ValueError:
            print(f"--pid must be an integer, got '{pid}'", file=sys.stderr)
            return 2
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "claim":
            cmd_claim(rest[0], rest[1] if len(rest) > 1 else "", force=force,
                      exclusive=exclusive, pid=pid, slot=slot)
        elif cmd == "committed":
            cmd_committed(rest[0], rest[1], rest[2], force=force)
        elif cmd == "pushed":
            cmd_pushed(rest[0], rest[1], force=force)
        elif cmd == "escalate":
            cmd_escalate(rest[0], rest[1] if len(rest) > 1 else "", force=force)
        elif cmd == "pr":
            cmd_pr(rest[0], rest[1])
        elif cmd == "route":
            cmd_route(rest[0], rest[1])
        elif cmd == "attach-patch":
            cmd_attach_patch(rest[0], rest[1])
        elif cmd == "attach":
            cmd_attach(rest[0], rest[1], rest[2], rest[3] if len(rest) > 3 else "")
        elif cmd == "qa-verdict":
            cmd_qa_verdict(rest[0], rest[1], rest[2] if len(rest) > 2 else "")
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
    except locks.LockTimeout as e:
        print(f"ledger: could not acquire the ledger lock in time — {e}", file=sys.stderr)
        return 6
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
