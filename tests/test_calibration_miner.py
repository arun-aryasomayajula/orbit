# Slice-5 contract (the goldens learning loop): the miner turns recorded
# outcomes into CANDIDATES only — merged-untouched → positive exemplar,
# amended merge → review-delta, rejected/reverted → anti-pattern carrying the
# human's reason, answered escalation → escalation memory — each citing its
# ledger evidence, idempotently. NOTHING is briefed until a human approves on
# the dashboard (approve appends the card line to goldens/LEARNED.md under the
# category section; reject requires a reason). The engine's seed goldens are
# never touched.
#
# Run: python3 -m pytest tests/test_calibration_miner.py -v   (from orbit/)
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

_spec = importlib.util.spec_from_file_location("calibration_miner",
                                               ROOT / "engine" / "calibration_miner.py")
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


def target(tmp_path, ledger_entries, backlog_yaml=None):
    """A git target repo: one commit on main (merged-untouched anchor) and one
    on a side branch (amended anchor). Returns (repo, sha_on_main, sha_off_main)."""
    repo = tmp_path / "repo"
    ap = repo / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates: {t: {cmd: "true"}}\n')
    (ap / "backlog.yaml").write_text(backlog_yaml or "tasks: []\n")
    _git(repo.parent, "-C", str(repo), "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "on main")
    on_main = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
    _git(repo, "checkout", "-qb", "side")
    (repo / "b.txt").write_text("two\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "off main")
    off_main = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    _git(repo, "checkout", "-q", "main")
    entries = ledger_entries(on_main, off_main) if callable(ledger_entries) else ledger_entries
    (ap / "state" / "ledger.json").write_text(json.dumps({"entries": entries}))
    return repo, on_main, off_main


BACKLOG = """tasks:
  - id: t-good
    title: "shipped clean"
    category: feature
    status: done
  - id: t-bad
    title: "shipped wrong"
    category: bug
    status: done
  - id: t-asked
    title: "needed a decision"
    category: refactor
    status: queued
    operator_answer: "always keep the legacy column until Q4"
"""


def test_miner_emits_the_four_candidate_kinds(tmp_path):
    repo, on_main, off_main = target(tmp_path, lambda om, xm: {
        "t-good": {"state": "merged", "sha": om, "branch": "autopilot/task-t-good"},
        "t-amend": {"state": "merged", "sha": xm},
        "t-bad": {"state": "rejected", "review_note": "asserts on a mock"},
        "t-undone": {"state": "merged", "sha": om, "reverted_at": "x",
                     "revert_note": "broke prod search"},
        "t-asked": {"state": "escalated", "reason": "drop legacy column?"},
    }, BACKLOG)
    found = cm.mine(repo)
    kinds = {c["id"]: c for c in found}
    assert "t-good-positive" in kinds and "merged untouched" in kinds["t-good-positive"]["card_line"]
    assert "t-amend-amended" in kinds and "amended" in kinds["t-amend-amended"]["card_line"]
    assert "t-bad-rejected" in kinds and "asserts on a mock" in kinds["t-bad-rejected"]["card_line"]
    assert "t-undone-reverted" in kinds and "broke prod search" in kinds["t-undone-reverted"]["card_line"]
    esc = kinds["t-asked-escalation-memory"]
    assert "drop legacy column?" in esc["card_line"] and "until Q4" in esc["card_line"]
    for c in found:
        assert c["evidence"], c["id"]                    # every lesson cites its ledger


BACKLOG_ONE = """tasks:
  - id: t-bad
    title: "shipped wrong"
    category: bug
    status: done
"""


def test_miner_is_idempotent_across_runs_and_decisions(tmp_path):
    repo, *_ = target(tmp_path, lambda om, xm: {
        "t-bad": {"state": "rejected", "review_note": "nope"}}, BACKLOG_ONE)
    assert len(cm.mine(repo)) == 1
    assert len(cm.mine(repo)) == 0                       # already a candidate
    gold = repo / ".autopilot" / "goldens"
    (gold / "rejected").mkdir(parents=True)
    (gold / "candidates" / "t-bad-rejected.json").rename(
        gold / "rejected" / "t-bad-rejected.json")
    assert len(cm.mine(repo)) == 0                       # human said no — stays no


@pytest.fixture()
def cc_target(tmp_path):
    repo, *_ = target(tmp_path, lambda om, xm: {
        "t-bad": {"state": "rejected", "review_note": "asserts on a mock"}}, BACKLOG_ONE)
    cm.mine(repo)
    ap = repo / ".autopilot"
    os.environ["AP_HOME"] = str(ap)
    os.environ["AP_STATE"] = str(ap / "state")
    spec = importlib.util.spec_from_file_location("command_center",
                                                  ROOT / "engine" / "command_center.py")
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    return cc, repo


def test_approve_appends_to_learned_and_archives(cc_target):
    cc, repo = cc_target
    gold = repo / ".autopilot" / "goldens"
    assert len(cc.load_calibration_candidates()) == 1
    msg = cc.do_calibration_action("t-bad-rejected", "approve")
    assert "LEARNED.md" in msg
    learned = (gold / "LEARNED.md").read_text()
    assert "## bug" in learned and "asserts on a mock" in learned
    assert (gold / "approved" / "t-bad-rejected.json").exists()
    assert cc.load_calibration_candidates() == []        # gone from pending
    # second approval of a different candidate lands under an existing section
    (gold / "candidates" / "x-rejected.json").write_text(json.dumps(
        {"id": "x-rejected", "kind": "rejected", "task_id": "x",
         "category": "bug", "card_line": "FLAW (task x, bug): second lesson"}))
    cc.do_calibration_action("x-rejected", "approve")
    learned = (gold / "LEARNED.md").read_text()
    assert learned.count("## bug") == 1 and "second lesson" in learned


def test_reject_requires_a_reason_and_archives_with_it(cc_target):
    cc, repo = cc_target
    gold = repo / ".autopilot" / "goldens"
    assert "reason is required" in cc.do_calibration_action("t-bad-rejected", "reject", "")
    assert (gold / "candidates" / "t-bad-rejected.json").exists()   # untouched
    cc.do_calibration_action("t-bad-rejected", "reject", "one-off, not a pattern")
    archived = json.loads((gold / "rejected" / "t-bad-rejected.json").read_text())
    assert archived["rejected_reason"] == "one-off, not a pattern"
    assert not (gold / "LEARNED.md").exists()            # rejected → never briefed


def test_engine_goldens_are_never_touched(tmp_path):
    repo, *_ = target(tmp_path, lambda om, xm: {
        "t-bad": {"state": "rejected", "review_note": "x"}}, BACKLOG)
    before = sorted(p.name for p in (ROOT / "goldens").rglob("*") if p.is_file())
    cm.mine(repo)
    after = sorted(p.name for p in (ROOT / "goldens").rglob("*") if p.is_file())
    assert before == after


def test_report_tallies_by_category(tmp_path):
    repo, *_ = target(tmp_path, lambda om, xm: {
        "t-good": {"state": "merged", "sha": om},
        "t-bad": {"state": "rejected", "review_note": "n"},
        "t-asked": {"state": "escalated", "reason": "q"},
    }, BACKLOG)
    out = cm.report(repo)
    assert "feature" in out and "bug" in out and "refactor" in out
    assert "bend" in out                                 # states the success criterion
