# Task 2: exclusive, pid-aware claims (engine/ledger.py `claim --exclusive
# --pid P --slot S`) and pid-aware reaping (`reap`). Exclusive claims add a
# second winner-take-all layer on top of the existing lifecycle gate: two
# workers racing the same tid must produce exactly one winner, verified
# under a real flock (engine/locks.py) with the ledger reloaded inside it.
# A dead worker's in_progress entry is the one case that's reclaimable
# (takeover, recorded via `reclaimed_from`) rather than refused; every other
# non-absent state is not. `reap` gets the same pid-awareness: a stale entry
# whose recorded worker is still alive is skipped, not escalated.
#
# Run: python3 -m pytest tests/test_slot_claims.py -v   (from orbit/)
import json
import multiprocessing as mp
import os
import subprocess
import sys
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
                       "updated_at": "2020-01-01T00:00:00Z"}}
    r = _ledger(tmp_path, "claim", "t1", "again", "--exclusive", "--pid", "555", "--slot", "slot-b",
                entries=entries)
    assert r.returncode == 0, r.stderr
    e = _entries(tmp_path)["t1"]
    assert e["worker_pid"] == 555
    assert e["slot"] == "slot-b"
    assert e["reclaimed_from"] == dead
    assert e["state"] == "in_progress"


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


# ── reap: pid-aware liveness gates escalation ───────────────────────────────

def test_reap_skips_stale_entry_with_a_live_worker_pid(tmp_path):
    entries = {
        "live": {"state": "in_progress", "worker_pid": os.getpid(),
                  "updated_at": "2020-01-01T00:00:00Z"},
    }
    r = _ledger(tmp_path, "reap", "3600", entries=entries)
    assert r.returncode == 0
    assert r.stdout.strip() == ""            # not reaped
    assert "still alive" in r.stderr
    assert _entries(tmp_path)["live"]["state"] == "in_progress"


def test_reap_escalates_dead_pid_and_legacy_pidless_entries(tmp_path):
    dead = _guaranteed_dead_pid()
    entries = {
        "dead-worker": {"state": "in_progress", "worker_pid": dead,
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
