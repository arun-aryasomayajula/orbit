# Task 2: exclusive, pid-aware claims (engine/ledger.py `claim --exclusive
# --pid P --slot S`) and identity-aware reaping (`reap`). Exclusive claims
# add a second winner-take-all layer on top of the existing lifecycle gate:
# two workers racing the same tid must produce exactly one winner, verified
# under a real flock (engine/locks.py) with the ledger reloaded inside it.
# A dead worker's in_progress entry is the one case that's reclaimable
# (takeover, sanctioned via lifecycle's `reclaim` event and recorded on a
# `reclaimed_from` list) rather than refused; every other non-absent state
# is not.
#
# `reap` is identity-, not pid-, aware: an entry's claim-time worker_pid is
# the slot LOOP's own long-lived pid, so trusting it alone (as an earlier
# version of this fix did) would make the reaper a permanent no-op for as
# long as that loop keeps running, whether or not it's still on THIS task —
# the bug an adversarial review caught. Liveness now requires
# $AP_STATE/workers/<slot>.pid to name a live pid AND the exact tid in
# question; a mismatch, dead pid, or missing file all escalate. An
# unconditional backstop escalates past 3x the timeout regardless of any
# liveness signal.
#
# Every ledger state-writing verb (claim, committed, pushed, escalate, mark,
# clear, reap, ...) now goes through one shared lock (`_locked_update`) for
# its whole load-modify-save cycle, so two concurrent verb invocations can
# never lose one's update to the other's whole-file rewrite.
#
# Run: python3 -m pytest tests/test_slot_claims.py -v   (from orbit/)
import json
import multiprocessing as mp
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"
CTX = mp.get_context("fork")


def _ledger(tmp_path, *args, entries=None):
    if entries is not None:
        (tmp_path / "ledger.json").write_text(json.dumps({"entries": entries}))
    env = {"AP_STATE": str(tmp_path), "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, str(ENGINE / "ledger.py"), *args],
                          env=env, capture_output=True, text=True)


def _entries(tmp_path):
    p = tmp_path / "ledger.json"
    return json.loads(p.read_text())["entries"] if p.exists() else {}


def _guaranteed_dead_pid() -> int:
    """A pid that has definitely already exited - not a made-up number that
    might collide with something real and alive on the test machine (mirrors
    tests/test_locks.py's helper of the same name)."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# ── two concurrent exclusive claims on one tid: exactly one wins ───────────

def _claim_worker(state_dir, slot, start_barrier, finish_barrier, result_queue):
    # Use OUR OWN pid as --pid: this process (not the subprocess it spawns)
    # is the "worker", and it must stay alive for the whole race so whichever
    # claim loses correctly sees a LIVE worker_pid on the winner's entry
    # instead of racing its own exit — a worker that exits the instant its
    # claim call returns would make the *other* side's liveness check read
    # it as dead and wrongly "reclaim" it, which is a race in the test, not
    # in ledger.py. finish_barrier holds both processes alive until BOTH
    # claim attempts have completed.
    my_pid = os.getpid()
    start_barrier.wait()  # line both processes up so the race is real, not sequential
    env = {"AP_STATE": state_dir, "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, str(ENGINE / "ledger.py"), "claim", "t1", "title",
                         "--exclusive", "--pid", str(my_pid), "--slot", slot],
                        env=env, capture_output=True, text=True)
    result_queue.put((my_pid, r.returncode))
    finish_barrier.wait()


def test_two_concurrent_exclusive_claims_exactly_one_wins(tmp_path):
    start_barrier = CTX.Barrier(2)
    finish_barrier = CTX.Barrier(2)
    q = CTX.Queue()
    p1 = CTX.Process(target=_claim_worker, args=(str(tmp_path), "slot-a", start_barrier, finish_barrier, q))
    p2 = CTX.Process(target=_claim_worker, args=(str(tmp_path), "slot-b", start_barrier, finish_barrier, q))
    p1.start()
    p2.start()
    p1.join(20)
    p2.join(20)
    assert p1.exitcode == 0 and p2.exitcode == 0

    results = dict(q.get(timeout=5) for _ in range(2))
    assert sorted(results.values()) == [0, 5]

    entries = _entries(tmp_path)
    assert len(entries) == 1
    winner_pid = next(pid for pid, code in results.items() if code == 0)
    assert entries["t1"]["worker_pid"] == winner_pid
    assert entries["t1"]["state"] == "in_progress"
    assert "reclaimed_from" not in entries["t1"]  # both workers genuinely live throughout


# ── dead-pid takeover ────────────────────────────────────────────────────

def test_dead_pid_in_progress_is_reclaimable_with_takeover_note(tmp_path):
    dead = _guaranteed_dead_pid()
    entries = {"t1": {"state": "in_progress", "worker_pid": dead, "slot": "slot-a",
                       "claimed_at": "2019-01-01T00:00:00Z",
                       "updated_at": "2020-01-01T00:00:00Z"}}
    r = _ledger(tmp_path, "claim", "t1", "again", "--exclusive", "--pid", "555", "--slot", "slot-b",
                entries=entries)
    assert r.returncode == 0, r.stderr
    e = _entries(tmp_path)["t1"]
    assert e["worker_pid"] == 555
    assert e["slot"] == "slot-b"
    assert e["reclaimed_from"] == [dead]           # a list, not a bare scalar
    assert e["state"] == "in_progress"
    assert e["claimed_at"] != "2019-01-01T00:00:00Z"  # takeover resets claimed_at


def test_second_takeover_appends_to_reclaimed_from_list(tmp_path):
    first_dead = 111111
    second_dead = _guaranteed_dead_pid()
    entries = {"t1": {"state": "in_progress", "worker_pid": second_dead, "slot": "slot-b",
                       "reclaimed_from": [first_dead],
                       "updated_at": "2020-01-01T00:00:00Z"}}
    r = _ledger(tmp_path, "claim", "t1", "again", "--exclusive", "--pid", "777", "--slot", "slot-c",
                entries=entries)
    assert r.returncode == 0, r.stderr
    assert _entries(tmp_path)["t1"]["reclaimed_from"] == [first_dead, second_dead]  # appended, not overwritten


def test_lifecycle_check_can_verb_confirms_reclaim_is_legal(tmp_path):
    # Nothing calls `ledger.py can ... reclaim` directly today, but this
    # confirms the same lifecycle.check() path cmd_claim_exclusive routes
    # its takeover through actually says yes for the state it fires from.
    r = _ledger(tmp_path, "can", "t1", "reclaim", entries={"t1": {"state": "in_progress"}})
    assert r.returncode == 0


# ── non-reclaimable entries refuse with exit 5, nothing written ────────────

def test_live_pid_in_progress_refuses_exclusive_claim(tmp_path):
    live = os.getpid()  # this test process — definitely alive right now
    entries = {"t1": {"state": "in_progress", "worker_pid": live, "slot": "slot-a",
                       "updated_at": "2020-01-01T00:00:00Z"}}
    r = _ledger(tmp_path, "claim", "t1", "again", "--exclusive", "--pid", "555", "--slot", "slot-b",
                entries=entries)
    assert r.returncode == 5
    assert len(r.stderr.strip().splitlines()) == 1
    assert _entries(tmp_path) == entries  # byte-identical, nothing written


def test_committed_entry_refuses_exclusive_claim(tmp_path):
    entries = {"t1": {"state": "committed", "branch": "autopilot/task-t1", "sha": "abc"}}
    r = _ledger(tmp_path, "claim", "t1", "again", "--exclusive", "--pid", "555", "--slot", "a",
                entries=entries)
    assert r.returncode == 5
    assert len(r.stderr.strip().splitlines()) == 1
    assert _entries(tmp_path) == entries


def test_merged_entry_refuses_exclusive_claim(tmp_path):
    entries = {"t1": {"state": "merged", "sha": "abc"}}
    r = _ledger(tmp_path, "claim", "t1", "again", "--exclusive", "--pid", "555", "--slot", "a",
                entries=entries)
    assert r.returncode == 5
    assert _entries(tmp_path) == entries


# ── --force still overrides the exclusive-claim refusal ────────────────────

def test_force_overrides_exclusive_refusal_and_is_stamped(tmp_path):
    entries = {"t1": {"state": "pushed", "remote_ref": "origin/x"}}
    r = _ledger(tmp_path, "claim", "t1", "again", "--exclusive", "--pid", "555", "--slot", "a",
                "--force", entries=entries)
    assert r.returncode == 0, r.stderr
    e = _entries(tmp_path)["t1"]
    assert e["state"] == "in_progress"
    assert e["worker_pid"] == 555
    assert e["forced"] is True


# ── non-exclusive claim: unchanged (backward compat for the no-arg cycle path) ─

def test_non_exclusive_claim_omits_worker_fields(tmp_path):
    r = _ledger(tmp_path, "claim", "t1", "a title")
    assert r.returncode == 0, r.stderr
    e = _entries(tmp_path)["t1"]
    assert "worker_pid" not in e
    assert "slot" not in e
    assert e["state"] == "in_progress"


def test_non_exclusive_claim_over_in_progress_is_now_refused_without_force(tmp_path):
    # lifecycle.py: claim-over-in_progress is illegal by default (Task 2).
    r = _ledger(tmp_path, "claim", "t1", "again",
                entries={"t1": {"state": "in_progress", "updated_at": "2020-01-01T00:00:00Z"}})
    assert r.returncode == 3
    assert _entries(tmp_path)["t1"]["updated_at"] == "2020-01-01T00:00:00Z"  # untouched


def test_non_exclusive_claim_over_in_progress_force_still_overrides(tmp_path):
    r = _ledger(tmp_path, "claim", "t1", "again", "--force",
                entries={"t1": {"state": "in_progress"}})
    assert r.returncode == 0, r.stderr
    e = _entries(tmp_path)["t1"]
    assert e["state"] == "in_progress"
    assert e["forced"] is True


# ── reap: identity-aware liveness gates escalation ──────────────────────────

def _write_worker_pid_file(tmp_path, slot, pid, tid):
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir(exist_ok=True)
    (workers_dir / f"{slot}.pid").write_text(json.dumps({"pid": pid, "tid": tid}))


def test_reap_skips_when_worker_pid_file_matches_live_pid_and_tid(tmp_path):
    # Must land strictly between max_age and 3x max_age — old enough to
    # reap, not so old the unconditional backstop fires regardless of the
    # (genuinely live) match this test is actually checking.
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_worker_pid_file(tmp_path, "slot-a", os.getpid(), "live")
    entries = {
        "live": {"state": "in_progress", "slot": "slot-a", "worker_pid": os.getpid(),
                  "updated_at": recent},
    }
    r = _ledger(tmp_path, "reap", "60", entries=entries)   # 3x = 180s — 90s is safely inside it
    assert r.returncode == 0
    assert r.stdout.strip() == ""            # not reaped
    assert "still alive" in r.stderr
    assert _entries(tmp_path)["live"]["state"] == "in_progress"


def test_reap_escalates_when_pid_file_names_a_different_tid(tmp_path):
    # The slot loop moved on to a different task; this entry is orphaned.
    _write_worker_pid_file(tmp_path, "slot-a", os.getpid(), "some-other-task")
    entries = {
        "orphaned": {"state": "in_progress", "slot": "slot-a", "worker_pid": os.getpid(),
                      "updated_at": "2020-01-01T00:00:00Z"},
    }
    r = _ledger(tmp_path, "reap", "3600", entries=entries)
    assert r.returncode == 0
    assert r.stdout.split() == ["orphaned"]
    assert _entries(tmp_path)["orphaned"]["state"] == "escalated"


def test_reap_escalates_when_pid_file_is_absent(tmp_path):
    entries = {
        "abandoned": {"state": "in_progress", "slot": "slot-z", "worker_pid": 424242,
                       "updated_at": "2020-01-01T00:00:00Z"},
    }
    r = _ledger(tmp_path, "reap", "3600", entries=entries)
    assert r.returncode == 0
    assert r.stdout.split() == ["abandoned"]
    assert _entries(tmp_path)["abandoned"]["state"] == "escalated"


def test_worker_pid_alone_without_slot_no_longer_grants_liveness(tmp_path):
    # This is exactly the bug an adversarial review caught: worker_pid is the
    # slot LOOP's own long-lived pid, not a per-task signal — trusting it
    # alone would make the reaper a permanent no-op for as long as the loop
    # keeps running. A bare (legacy or slot-less) worker_pid now grants
    # nothing; only a matching $AP_STATE/workers/<slot>.pid file does.
    entries = {
        "t1": {"state": "in_progress", "worker_pid": os.getpid(),
                "updated_at": "2020-01-01T00:00:00Z"},
    }
    r = _ledger(tmp_path, "reap", "3600", entries=entries)
    assert r.returncode == 0
    assert r.stdout.split() == ["t1"]
    assert _entries(tmp_path)["t1"]["state"] == "escalated"


def test_reap_escalates_dead_pid_and_legacy_pidless_entries(tmp_path):
    dead = _guaranteed_dead_pid()
    entries = {
        "dead-worker": {"state": "in_progress", "slot": "slot-a", "worker_pid": dead,
                         "updated_at": "2020-01-01T00:00:00Z"},
        "legacy":      {"state": "committed", "sha": "a",
                         "updated_at": "2020-01-01T00:00:00Z"},
    }
    r = _ledger(tmp_path, "reap", "3600", entries=entries)
    assert r.returncode == 0
    assert r.stdout.split() == ["dead-worker", "legacy"]
    e = _entries(tmp_path)
    assert e["dead-worker"]["state"] == "escalated"
    assert e["legacy"]["state"] == "escalated"
    assert e["legacy"]["sha"] == "a"          # ship evidence preserved


def test_reap_backstop_fires_past_3x_timeout_regardless_of_liveness(tmp_path):
    _write_worker_pid_file(tmp_path, "slot-a", os.getpid(), "stuck")
    ancient = "2000-01-01T00:00:00Z"  # decades old — safely past any 3x window
    entries = {
        "stuck": {"state": "in_progress", "slot": "slot-a", "worker_pid": os.getpid(),
                   "updated_at": ancient},
    }
    r = _ledger(tmp_path, "reap", "60", entries=entries)  # 3x = 180s — ancient blows past it
    assert r.returncode == 0
    assert r.stdout.split() == ["stuck"]
    assert "backstop" in r.stderr
    assert _entries(tmp_path)["stuck"]["state"] == "escalated"


def test_reap_within_backstop_window_still_skips_a_live_match(tmp_path):
    # Sanity check on the boundary: comfortably inside 3x, a genuine live
    # match is still honored (the backstop test above is deliberately WAY
    # past 3x so it can't be confused with ordinary liveness skipping).
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_worker_pid_file(tmp_path, "slot-a", os.getpid(), "t1")
    entries = {
        "t1": {"state": "in_progress", "slot": "slot-a", "worker_pid": os.getpid(),
                "updated_at": recent},
    }
    r = _ledger(tmp_path, "reap", "60", entries=entries)  # age ~120s: > max_age, < 3x (180s)
    assert r.returncode == 0
    assert r.stdout.strip() == ""
    assert _entries(tmp_path)["t1"]["state"] == "in_progress"


# ── --pid/--slot require --exclusive (no silent non-exclusive degrade) ─────

def test_pid_without_exclusive_exits_2(tmp_path):
    r = _ledger(tmp_path, "claim", "t1", "title", "--pid", "555")
    assert r.returncode == 2
    assert len(r.stderr.strip().splitlines()) == 1
    assert _entries(tmp_path) == {}


def test_slot_without_exclusive_exits_2(tmp_path):
    r = _ledger(tmp_path, "claim", "t1", "title", "--slot", "a")
    assert r.returncode == 2
    assert _entries(tmp_path) == {}


# ── every state-writing verb is locked: concurrent verbs lose nothing ──────

def _run_verb_worker(state_dir, args, barrier, result_queue):
    barrier.wait()
    env = {"AP_STATE": state_dir, "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, str(ENGINE / "ledger.py"), *args],
                        env=env, capture_output=True, text=True)
    result_queue.put((args[0], r.returncode, r.stderr))


def test_concurrent_committed_and_reap_lose_nothing(tmp_path):
    # Two DIFFERENT verbs, on two DIFFERENT tids, in the same ledger.json:
    # a naive load-modify-save race (no shared lock) can silently drop
    # whichever one saved first, since _save always rewrites the WHOLE file.
    # With every verb going through _locked_update, both changes must
    # survive regardless of interleaving. t1 is deliberately FRESH (not
    # reap-eligible) so `reap`'s own escalation of stale entries can never
    # legitimately race `committed`'s transition on the SAME tid — the
    # thing under test here is the shared file, not a business-logic race
    # over t1's state.
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = {
        "t1": {"state": "in_progress", "updated_at": fresh},
        "t2": {"state": "in_progress", "updated_at": "2020-01-01T00:00:00Z"},
    }
    (tmp_path / "ledger.json").write_text(json.dumps({"entries": entries}))

    barrier = CTX.Barrier(2)
    q = CTX.Queue()
    p1 = CTX.Process(target=_run_verb_worker, args=(
        str(tmp_path), ["committed", "t1", "autopilot/task-t1", "abc123"], barrier, q))
    p2 = CTX.Process(target=_run_verb_worker, args=(
        str(tmp_path), ["reap", "3600"], barrier, q))
    p1.start()
    p2.start()
    p1.join(20)
    p2.join(20)
    assert p1.exitcode == 0 and p2.exitcode == 0

    results = {row[0]: row for row in (q.get(timeout=5) for _ in range(2))}
    assert results["committed"][1] == 0, results["committed"][2]
    assert results["reap"][1] == 0, results["reap"][2]

    e = _entries(tmp_path)
    assert e["t1"]["state"] == "committed"    # not lost to the reap's overlapping save
    assert e["t2"]["state"] == "escalated"    # not lost to the commit's overlapping save
