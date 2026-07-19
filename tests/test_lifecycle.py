# The task lifecycle state machine (engine/lifecycle.py) and its enforcement in
# ledger.py: loop verbs (claim/committed/pushed/escalate) permissively record
# facts, review verbs (merged/rejected/reverted) strictly gate judgments, an
# illegal transition exits 3 writing NOTHING, and --force overrides auditable
# (forced: true). autoclose consults the same machine before flipping to merged.
#
# Run: python3 -m pytest tests/test_lifecycle.py -v   (from orbit/)
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))
import lifecycle  # noqa: E402


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, ENGINE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── the pure machine ─────────────────────────────────────────────────────────

def test_happy_path_is_legal():
    assert lifecycle.check(None, "claim") is None
    assert lifecycle.check("in_progress", "committed") is None
    assert lifecycle.check("committed", "pushed") is None
    assert lifecycle.check("pushed", "merged", {"sha": "a"}) is None
    assert lifecycle.check("pushed", "rejected") is None


def test_late_recorded_facts_are_legal():
    # An agent that skipped `claim` still gets its facts recorded.
    assert lifecycle.check(None, "committed") is None
    assert lifecycle.check(None, "pushed") is None
    assert lifecycle.check(None, "escalate") is None
    assert lifecycle.check("in_progress", "claim") is None      # crash re-claim


def test_escalate_is_legal_until_reviewed():
    for st in (None, "in_progress", "committed", "pushed", "escalated"):
        assert lifecycle.check(st, "escalate") is None
    for st in ("merged", "rejected"):
        assert lifecycle.check(st, "escalate") is not None


def test_judgments_require_a_reviewable_state():
    # The holes the machine closes: judging work that never shipped.
    assert lifecycle.check(None, "merged", None) is not None
    assert lifecycle.check(None, "rejected", None) is not None
    assert lifecycle.check("in_progress", "merged", {"sha": "a"}) is not None
    assert lifecycle.check("in_progress", "reverted", {"sha": "a"}) is not None


def test_terminal_states_accept_nothing():
    for st in ("merged", "rejected"):
        for ev in ("claim", "committed", "pushed", "escalate", "merged", "rejected"):
            assert lifecycle.check(st, ev, {"sha": "a"}) is not None, f"{st} → {ev}"


def test_merged_requires_ship_evidence():
    assert lifecycle.check("escalated", "merged", {"reason": "stuck"}) is not None
    assert lifecycle.check("escalated", "merged", {"sha": "abc"}) is None  # autoclose flow
    assert lifecycle.check("pushed", "merged", {"remote_ref": "origin/x"}) is None


def test_reverted_legal_when_ancestry_outruns_ledger():
    # A GitHub-UI merge never calls `mark` — ledger may still say pushed/committed.
    assert lifecycle.check("merged", "reverted", {"sha": "a"}) is None
    assert lifecycle.check("pushed", "reverted", {"sha": "a"}) is None
    assert lifecycle.check("committed", "reverted", {"sha": "a"}) is None
    assert lifecycle.check("rejected", "reverted", {"sha": "a"}) is not None
    assert lifecycle.check("merged", "reverted", {}) is not None   # no evidence


def test_unknown_event_is_named():
    assert "unknown lifecycle event" in lifecycle.check("pushed", "yeeted")


# ── enforcement in ledger.py ─────────────────────────────────────────────────

def _ledger(tmp_path, *args, entries=None):
    if entries is not None:
        (tmp_path / "ledger.json").write_text(json.dumps({"entries": entries}))
    env = {"AP_STATE": str(tmp_path), "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, str(ENGINE / "ledger.py"), *args],
                          env=env, capture_output=True, text=True)


def _entries(tmp_path):
    p = tmp_path / "ledger.json"
    return json.loads(p.read_text())["entries"] if p.exists() else {}


def test_illegal_transition_exits_3_and_writes_nothing(tmp_path):
    r = _ledger(tmp_path, "mark", "ghost", "merged")
    assert r.returncode == 3
    assert "illegal transition" in r.stderr
    assert _entries(tmp_path) == {}


def test_double_mark_merged_is_refused(tmp_path):
    r = _ledger(tmp_path, "mark", "t1", "merged",
                entries={"t1": {"state": "merged", "sha": "a"}})
    assert r.returncode == 3
    assert _entries(tmp_path)["t1"].get("reviewed_at") is None


def test_claim_of_reviewed_task_is_refused(tmp_path):
    r = _ledger(tmp_path, "claim", "t1", "again",
                entries={"t1": {"state": "merged", "sha": "a"}})
    assert r.returncode == 3
    assert _entries(tmp_path)["t1"]["state"] == "merged"


def test_force_overrides_and_is_stamped(tmp_path):
    r = _ledger(tmp_path, "mark", "t1", "merged", "--force",
                entries={"t1": {"state": "in_progress"}})
    assert r.returncode == 0 and "FORCED" in r.stderr
    e = _entries(tmp_path)["t1"]
    assert e["state"] == "merged" and e["forced"] is True


def test_happy_path_through_the_real_verbs(tmp_path):
    assert _ledger(tmp_path, "claim", "t1", "a title").returncode == 0
    assert _ledger(tmp_path, "committed", "t1", "autopilot/task-t1", "abc123").returncode == 0
    assert _ledger(tmp_path, "pushed", "t1", "origin/autopilot/task-t1").returncode == 0
    assert _ledger(tmp_path, "mark", "t1", "merged").returncode == 0
    assert _entries(tmp_path)["t1"]["state"] == "merged"


def test_can_verb_is_a_dry_check(tmp_path):
    ok = _ledger(tmp_path, "can", "t1", "reverted",
                 entries={"t1": {"state": "pushed", "sha": "a"}})
    assert ok.returncode == 0
    bad = _ledger(tmp_path, "can", "t1", "claim")
    assert bad.returncode == 3
    assert _entries(tmp_path)["t1"]["state"] == "pushed"   # nothing written either way


def test_committed_then_runtime_escalation(tmp_path):
    # The runtime-check flow: agent committed, wrapper escalates instead of pushing.
    assert _ledger(tmp_path, "committed", "t1", "autopilot/task-t1", "abc").returncode == 0
    assert _ledger(tmp_path, "escalate", "t1", "runtime contradiction").returncode == 0
    assert _entries(tmp_path)["t1"]["state"] == "escalated"


# ── the reaper: stale mid-cycle entries escalate instead of rotting ──────────

def test_reap_escalates_stale_mid_cycle_entries(tmp_path):
    r = _ledger(tmp_path, "reap", "3600", entries={
        "crashed":  {"state": "in_progress", "updated_at": "2020-01-01T00:00:00Z"},
        "stranded": {"state": "committed", "sha": "a", "updated_at": "2020-01-01T00:00:00Z"},
    })
    assert r.returncode == 0
    assert r.stdout.split() == ["crashed", "stranded"]
    e = _entries(tmp_path)
    assert e["crashed"]["state"] == "escalated" and "reaper" in e["crashed"]["reason"]
    assert e["stranded"]["state"] == "escalated"
    assert e["stranded"]["sha"] == "a"                     # ship evidence preserved


def test_reap_leaves_fresh_and_reviewable_entries_alone(tmp_path):
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = {
        "running":  {"state": "in_progress", "updated_at": fresh},
        "shipped":  {"state": "pushed", "updated_at": "2020-01-01T00:00:00Z"},
        "done":     {"state": "merged", "updated_at": "2020-01-01T00:00:00Z"},
        "no-clock": {"state": "in_progress"},              # unparseable age → untouched
    }
    r = _ledger(tmp_path, "reap", "3600", entries=entries)
    assert r.returncode == 0 and r.stdout.strip() == ""
    e = _entries(tmp_path)
    assert {k: v["state"] for k, v in e.items()} == {
        "running": "in_progress", "shipped": "pushed", "done": "merged",
        "no-clock": "in_progress"}


def test_reap_rejects_a_non_integer_age(tmp_path):
    r = _ledger(tmp_path, "reap", "soon",
                entries={"t": {"state": "in_progress", "updated_at": "2020-01-01T00:00:00Z"}})
    assert r.returncode == 2
    assert _entries(tmp_path)["t"]["state"] == "in_progress"


# ── autoclose consults the same machine ──────────────────────────────────────

def test_autoclose_never_closes_mid_cycle_entries():
    autoclose = _mod("autoclose")
    tasks = [{"id": "inflight", "title": "x", "status": "queued"}]
    entries = {"inflight": {"state": "in_progress", "branch": "autopilot/task-inflight"}}
    closed, kept, out = autoclose.reconcile(tasks, entries, lambda t: True, "2026-07-19T00:00:00Z")
    assert closed == [] and [t["id"] for t in kept] == ["inflight"]
    assert out["inflight"]["state"] == "in_progress"
