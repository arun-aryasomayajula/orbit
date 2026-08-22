# Auto-close reconcile: a task whose recorded commit is an ancestor of the base
# branch has shipped, so it must be dropped from the backlog and flipped to
# 'merged' in the ledger — never re-escalated. Tasks with no ship, a non-ancestor
# ship, or a human 'rejected' verdict must be left untouched.
#
# Run: python3 -m pytest tests/test_autoclose.py -v   (from orbit/)
import importlib.util
import json
import multiprocessing as mp
import os
import subprocess
import sys
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parent.parent / "engine"
_spec = importlib.util.spec_from_file_location("autoclose", ENGINE / "autoclose.py")
autoclose = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autoclose)

NOW = "2026-07-13T00:00:00Z"
CTX = mp.get_context("fork")


def _tasks(*ids):
    return [{"id": i, "title": i, "status": "queued"} for i in ids]


def test_closes_task_whose_sha_is_ancestor():
    tasks = _tasks("shipped", "open")
    entries = {"shipped": {"state": "escalated", "sha": "abc1234"}}
    # only 'abc1234' is an ancestor of base
    closed, kept, out = autoclose.reconcile(tasks, entries, lambda t: t == "abc1234", NOW)
    assert closed == ["shipped"]
    assert [t["id"] for t in kept] == ["open"]           # shipped removed from backlog
    assert out["shipped"]["state"] == "merged"           # ledger reflects truth
    assert out["shipped"]["auto_closed"] is True
    assert out["shipped"]["sha"] == "abc1234"            # sha preserved


def test_keeps_task_with_no_ledger_entry():
    tasks = _tasks("neverworked")
    closed, kept, out = autoclose.reconcile(tasks, {}, lambda t: True, NOW)
    assert closed == []
    assert [t["id"] for t in kept] == ["neverworked"]


def test_keeps_task_whose_sha_is_not_ancestor():
    tasks = _tasks("inflight")
    entries = {"inflight": {"state": "pushed", "sha": "deadbeef"}}
    closed, kept, out = autoclose.reconcile(tasks, entries, lambda t: False, NOW)
    assert closed == []
    assert [t["id"] for t in kept] == ["inflight"]
    assert out["inflight"]["state"] == "pushed"          # untouched


def test_never_autocloses_a_rejected_ship():
    tasks = _tasks("rejected_one")
    entries = {"rejected_one": {"state": "rejected", "sha": "abc1234"}}
    # even though the sha is an ancestor, a human rejection is never auto-closed
    closed, kept, out = autoclose.reconcile(tasks, entries, lambda t: True, NOW)
    assert closed == []
    assert [t["id"] for t in kept] == ["rejected_one"]
    assert out["rejected_one"]["state"] == "rejected"


def test_falls_back_to_remote_ref_when_no_sha():
    tasks = _tasks("byref")
    entries = {"byref": {"state": "pushed", "remote_ref": "origin/autopilot/task-byref"}}
    seen = {}
    def anc(t):
        seen["target"] = t
        return True
    closed, kept, out = autoclose.reconcile(tasks, entries, anc, NOW)
    assert closed == ["byref"]
    assert seen["target"] == "origin/autopilot/task-byref"   # ref used as the ancestry target


# ── F1: the ledger write takes the LEDGER lock, and re-reads under it ────────
# autoclose writes ledger.json wholesale. Before F1 it read the entries, spent
# real time in `git merge-base` deciding, then wrote its snapshot back with no
# ledger lock and no re-read - so any ledger verb that landed in between
# (another slot's `pushed`, the reaper's `escalate`, a claim) was silently
# erased. Lock order is backlog -> ledger, everywhere.

def _workspace(tmp_path, entries=None, tasks=("shipped", "open")):
    """A minimal target repo with an origin/main ref, a backlog and a ledger."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "f.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True, env=env)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    # origin/main without a real remote - the ref is all the ancestry check reads
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", sha],
                   check=True)
    ap = repo / ".autopilot"
    state = ap / "state"
    state.mkdir(parents=True)
    (ap / "config.yaml").write_text('repo: "."\nbase_branch: "main"\n')
    (ap / "backlog.yaml").write_text(yaml.safe_dump(
        {"tasks": [{"id": t, "title": t, "status": "queued"} for t in tasks]},
        sort_keys=False))
    if entries is None:
        entries = {"shipped": {"state": "escalated", "sha": sha}}
    (state / "ledger.json").write_text(json.dumps({"entries": entries}))
    return repo, ap, state, sha


def _autoclose_env(ap, state, repo):
    return dict(os.environ, AP_HOME=str(ap), AP_STATE=str(state),
                ORBIT_REPO=str(repo), ORBIT_BASE_BRANCH="main")


def _entries(state):
    return json.loads((state / "ledger.json").read_text())["entries"]


def test_autoclose_reruns_its_decision_on_a_reread_ledger(tmp_path, monkeypatch):
    """The deterministic proof, with no timing in it: the injection happens
    INSIDE the ancestry check, i.e. exactly in the window between the probe
    read and the commit re-read. A blind write-back would drop `late`."""
    repo, ap, state, sha = _workspace(tmp_path)
    monkeypatch.setenv("AP_HOME", str(ap))
    monkeypatch.setenv("AP_STATE", str(state))
    monkeypatch.setenv("ORBIT_REPO", str(repo))
    monkeypatch.setenv("ORBIT_BASE_BRANCH", "main")
    monkeypatch.setattr(sys, "argv", ["autoclose.py"])

    class _Result:
        returncode = 0

    def fake_run(argv, **kw):
        # Stand-in for `git merge-base --is-ancestor` that ALSO does what a
        # concurrent ledger verb would do while it runs.
        data = json.loads((state / "ledger.json").read_text())
        data["entries"]["late"] = {"state": "pushed", "remote_ref": "origin/x",
                                   "title": "landed mid-decision"}
        (state / "ledger.json").write_text(json.dumps(data))
        return _Result()

    monkeypatch.setattr(autoclose.subprocess, "run", fake_run)
    autoclose.main()

    e = _entries(state)
    assert e["shipped"]["state"] == "merged"        # the close still happened
    assert e["shipped"]["auto_closed"] is True
    assert e["late"]["state"] == "pushed"           # ...and nothing was clobbered
    assert e["late"]["remote_ref"] == "origin/x"
    assert e["late"]["title"] == "landed mid-decision"
    kept = yaml.safe_load((ap / "backlog.yaml").read_text())["tasks"]
    assert [t["id"] for t in kept] == ["open"]


def _run_autoclose(ap, state, repo, barrier, q):
    barrier.wait()
    r = subprocess.run([sys.executable, str(ENGINE / "autoclose.py")],
                       env=_autoclose_env(ap, state, repo), capture_output=True, text=True)
    q.put(("autoclose", r.returncode, r.stdout + r.stderr))


def _run_pushed(state, barrier, q):
    barrier.wait()
    r = subprocess.run([sys.executable, str(ENGINE / "ledger.py"), "pushed", "t-other",
                        "origin/autopilot/task-t-other"],
                       env=dict(os.environ, AP_STATE=str(state)),
                       capture_output=True, text=True)
    q.put(("pushed", r.returncode, r.stdout + r.stderr))


def test_autoclose_and_a_concurrent_pushed_lose_nothing(tmp_path):
    """The real two-process race (tests/test_slot_claims.py's pattern): both
    verbs rewrite the WHOLE ledger file, so whichever saved first used to be
    droppable by the other. Whatever the interleaving, both must survive."""
    repo, ap, state, sha = _workspace(tmp_path)
    barrier = CTX.Barrier(2)
    q = CTX.Queue()
    p1 = CTX.Process(target=_run_autoclose, args=(ap, state, repo, barrier, q))
    p2 = CTX.Process(target=_run_pushed, args=(state, barrier, q))
    p1.start()
    p2.start()
    p1.join(60)
    p2.join(60)
    assert p1.exitcode == 0 and p2.exitcode == 0

    results = {row[0]: row for row in (q.get(timeout=5) for _ in range(2))}
    assert results["autoclose"][1] == 0, results["autoclose"][2]
    assert results["pushed"][1] == 0, results["pushed"][2]

    e = _entries(state)
    assert e["shipped"]["state"] == "merged"       # autoclose's flip survived
    assert e["shipped"]["sha"] == sha
    assert e["t-other"]["state"] == "pushed"       # the concurrent verb's did too
    assert e["t-other"]["remote_ref"] == "origin/autopilot/task-t-other"


def test_autoclose_holds_the_ledger_lock_for_its_write():
    """Source-level guard on the contract the two tests above rely on: the
    ledger read AND the ledger write happen inside locks.hold on
    .ledger.lock, nested inside the backlog hold (order backlog -> ledger),
    and the git subprocess is NOT inside the ledger hold."""
    src = (ENGINE / "autoclose.py").read_text()
    assert 'locks.hold(state_dir / ".backlog.lock")' in src
    assert 'ledger_lock = state_dir / ".ledger.lock"' in src
    # Order backlog -> ledger: the backlog hold opens BEFORE the commit
    # pass's ledger hold (rindex - the first ledger hold is read_ledger's,
    # defined further up).
    assert src.index('locks.hold(state_dir / ".backlog.lock")') < src.rindex("with locks.hold(ledger_lock)")
    # ...and the ancestry walk is paid for outside every ledger hold: the
    # commit pass never shells out to git.
    commit = src.rindex("# Commit pass")
    assert src.index("# Probe pass") < commit
    assert "merge-base" not in src[commit:]
