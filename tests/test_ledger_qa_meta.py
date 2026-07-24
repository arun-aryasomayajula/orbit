# Task 15: stateless ledger metadata verbs for QA evidence (`attach`,
# `qa-verdict`) and the backlog `qa_findings` fold that puts a QA agent's
# findings in front of the dev agent on a re-pick. Both ledger verbs mirror
# the existing PR-url/patch-attachment verbs (cmd_pr / cmd_attach_patch):
# metadata only, no lifecycle.py gate, `state` untouched, atomic `_save`
# reused. `attach` mirrors cmd_attach_patch's missing-entry behavior
# (silent no-op — no auto-vivified entry), not cmd_pr's (which auto-creates
# via `_entry`/setdefault) — see test_attach_missing_entry_is_a_noop below.
#
# Run: python3 -m pytest tests/test_ledger_qa_meta.py -v   (from orbit/)
import json
import os
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"


def _ledger(tmp_path, *args, entries=None):
    if entries is not None:
        (tmp_path / "ledger.json").write_text(json.dumps({"entries": entries}))
    env = {"AP_STATE": str(tmp_path), "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, str(ENGINE / "ledger.py"), *args],
                          env=env, capture_output=True, text=True)


def _entries(tmp_path):
    p = tmp_path / "ledger.json"
    return json.loads(p.read_text())["entries"] if p.exists() else {}


# ── attach ───────────────────────────────────────────────────────────────────

def test_attach_valid_kinds_accumulate_in_order(tmp_path):
    _ledger(tmp_path, "attach", "t1", "screenshot", "shots/a.png", "before",
            entries={"t1": {"state": "committed"}})
    r = _ledger(tmp_path, "attach", "t1", "log", "logs/b.log")
    assert r.returncode == 0, r.stderr
    atts = _entries(tmp_path)["t1"]["attachments"]
    assert [a["kind"] for a in atts] == ["screenshot", "log"]
    assert atts[0]["path"] == "shots/a.png"
    assert atts[0]["label"] == "before"
    assert atts[1]["path"] == "logs/b.log"
    assert atts[1]["label"] == ""          # label optional → ""
    assert all("at" in a for a in atts)


def test_attach_invalid_kind_exits_2_and_ledger_unchanged(tmp_path):
    entries = {"t1": {"state": "committed"}}
    r = _ledger(tmp_path, "attach", "t1", "video", "clip.mp4", entries=entries)
    assert r.returncode == 2
    assert "invalid" in r.stderr.lower()
    assert _entries(tmp_path) == entries   # byte-identical, nothing written


def test_attach_never_touches_state(tmp_path):
    _ledger(tmp_path, "attach", "t1", "report", "reports/r.md",
            entries={"t1": {"state": "committed"}})
    assert _entries(tmp_path)["t1"]["state"] == "committed"


def test_attach_missing_entry_is_a_noop(tmp_path):
    # Mirrors cmd_attach_patch: an id with no ledger entry is left alone
    # rather than auto-vivified (unlike cmd_pr, which does auto-create).
    r = _ledger(tmp_path, "attach", "ghost", "log", "logs/x.log")
    assert r.returncode == 0
    assert _entries(tmp_path) == {}


def test_attach_shows_in_cmd_show(tmp_path):
    _ledger(tmp_path, "attach", "t1", "screenshot", "shots/a.png", "before",
            entries={"t1": {"state": "committed", "title": "fix the thing"}})
    show = _ledger(tmp_path, "show")
    assert show.returncode == 0
    assert "shots/a.png" in show.stdout
    assert "before" in show.stdout


# ── qa-verdict ───────────────────────────────────────────────────────────────

def test_qa_verdict_valid_enum_sets_fields(tmp_path):
    r = _ledger(tmp_path, "qa-verdict", "t1", "pass", "all green",
                entries={"t1": {"state": "committed"}})
    assert r.returncode == 0, r.stderr
    qa = _entries(tmp_path)["t1"]["qa"]
    assert qa["verdict"] == "pass"
    assert qa["summary"] == "all green"
    assert "at" in qa


def test_qa_verdict_summary_optional(tmp_path):
    _ledger(tmp_path, "qa-verdict", "t1", "unable",
            entries={"t1": {"state": "committed"}})
    assert _entries(tmp_path)["t1"]["qa"]["summary"] == ""


def test_qa_verdict_repeat_overwrites_latest_wins(tmp_path):
    _ledger(tmp_path, "qa-verdict", "t1", "fail", "broke X",
            entries={"t1": {"state": "committed"}})
    r = _ledger(tmp_path, "qa-verdict", "t1", "pass", "now green")
    assert r.returncode == 0, r.stderr
    qa = _entries(tmp_path)["t1"]["qa"]
    assert qa == {"verdict": "pass", "summary": "now green", "at": qa["at"]}


def test_qa_verdict_invalid_enum_exits_2_and_ledger_unchanged(tmp_path):
    entries = {"t1": {"state": "committed"}}
    r = _ledger(tmp_path, "qa-verdict", "t1", "maybe", entries=entries)
    assert r.returncode == 2
    assert "invalid" in r.stderr.lower()
    assert _entries(tmp_path) == entries


def test_qa_verdict_never_touches_state(tmp_path):
    _ledger(tmp_path, "qa-verdict", "t1", "fail",
            entries={"t1": {"state": "committed"}})
    assert _entries(tmp_path)["t1"]["state"] == "committed"


def test_qa_verdict_missing_entry_is_a_noop(tmp_path):
    r = _ledger(tmp_path, "qa-verdict", "ghost", "pass")
    assert r.returncode == 0
    assert _entries(tmp_path) == {}


def test_qa_verdict_shows_in_cmd_show(tmp_path):
    _ledger(tmp_path, "qa-verdict", "t1", "fail", "login button dead",
            entries={"t1": {"state": "committed", "title": "fix the thing"}})
    show = _ledger(tmp_path, "show")
    assert show.returncode == 0
    assert "fail" in show.stdout
    assert "login button dead" in show.stdout


# ── backlog qa_findings fold ────────────────────────────────────────────────

def _target(tmp_path, backlog_yaml):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n')
    (ap / "backlog.yaml").write_text(backlog_yaml)
    return tmp_path


def _convert(target):
    env = dict(os.environ, AP_HOME=str(target / ".autopilot"),
               AP_STATE=str(target / ".autopilot" / "state"))
    return subprocess.run([sys.executable, str(ENGINE / "backlog_to_tasks.py")],
                          capture_output=True, text=True, env=env)


def _queue_task_text(target):
    queue = json.loads((target / ".autopilot" / "state" / "queue.json").read_text())
    assert len(queue["tasks"]) == 1
    return queue["tasks"][0]["task"]


def test_fold_includes_qa_findings_when_present(tmp_path):
    t = _target(tmp_path, """
tasks:
  - id: "1"
    title: "Fix the login button click handler"
    status: queued
    autopilot: allow
    category: bug
    acceptance_criteria:
      - "Clicking login navigates to the dashboard"
    qa_findings: "Login button does nothing on click."
""")
    r = _convert(t)
    assert r.returncode == 0, r.stderr
    text = _queue_task_text(t)
    assert ("QA FINDINGS (a QA agent observed the previous attempt failing — "
            "read the full report file first): Login button does nothing on click."
            in text)


def test_fold_absent_when_qa_findings_absent_byte_identical(tmp_path):
    yaml_with = """
tasks:
  - id: "1"
    title: "Fix the login button click handler"
    status: queued
    autopilot: allow
    category: bug
    acceptance_criteria:
      - "Clicking login navigates to the dashboard"
    qa_findings: "Login button does nothing on click."
"""
    yaml_without = yaml_with.replace(
        '    qa_findings: "Login button does nothing on click."\n', "")

    t1 = _target(tmp_path / "with", yaml_with)
    r1 = _convert(t1)
    assert r1.returncode == 0, r1.stderr
    text_with = _queue_task_text(t1)
    assert "QA FINDINGS" in text_with

    t2 = _target(tmp_path / "without", yaml_without)
    r2 = _convert(t2)
    assert r2.returncode == 0, r2.stderr
    text_without = _queue_task_text(t2)
    assert "QA FINDINGS" not in text_without
    assert text_without == (
        "Fix the login button click handler"
        "\n\nDONE WHEN (acceptance criteria — the verifier grades these):"
        "\n- Clicking login navigates to the dashboard")


def test_fold_empty_qa_findings_is_absent(tmp_path):
    t = _target(tmp_path, """
tasks:
  - id: "1"
    title: "Fix the login button click handler"
    status: queued
    autopilot: allow
    category: bug
    acceptance_criteria:
      - "Clicking login navigates to the dashboard"
    qa_findings: "   "
""")
    r = _convert(t)
    assert r.returncode == 0, r.stderr
    text = _queue_task_text(t)
    assert "QA FINDINGS" not in text
