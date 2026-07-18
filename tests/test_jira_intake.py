# The Jira source contract: a human's ready-label is the triage act; enrichment
# must pass the backlog lint hard gate or the ticket gets QUESTIONS commented
# back (retried only after a human updates the ticket); imports land proposed/
# human unless the operator opted into auto_queue; writeback (PR url / merged /
# rejected) is engine-side, at most once per event; no token → loud no-op; the
# enrichment agent never receives the token.
#
# Run: python3 -m pytest tests/test_jira_intake.py -v   (from orbit/)
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ADAPTER = ROOT / "adapters" / "jira_to_backlog.py"

GOOD_ENRICH = {"task": {
    "title": "Export admin users list as CSV from the users screen",
    "category": "feature",
    "priority": "medium",
    "context": "Reporter wants offline audit of admin users.",
    "evidence": "src/admin/users.py:42 list endpoint",
    "acceptance_criteria": ["a CSV download route exists and returns all columns",
                            "the export button renders on the users screen"],
}}


def issue(key="CDP-7", updated="2026-07-18T10:00:00.000+0530", summary="CSV export"):
    return {"key": key, "fields": {
        "summary": summary, "description": "as a user I want csv",
        "comment": {"comments": [{"author": {"displayName": "PM"}, "body": "asap pls"}]},
        "priority": {"name": "High"}, "labels": ["orbit-ready"],
        "status": {"name": "To Do"}, "updated": updated,
        "reporter": {"displayName": "PM"}}}


def target(tmp_path, extra_cfg=""):
    ap = tmp_path / "repo" / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text(textwrap.dedent(f"""\
        repo: "."
        base_branch: "main"
        sources: [backlog, jira]
        gates: {{t: {{cmd: "true"}}}}
        jira:
          base_url: "https://example.atlassian.net"
          project: "CDP"
          ready_label: "orbit-ready"
        {extra_cfg}"""))
    (ap / "backlog.yaml").write_text("tasks: []\n")
    (ap / "state" / ".jira_token").write_text("user@example.com:tok123\n")
    return tmp_path / "repo"


def fixtures(tmp_path, issues):
    fx = tmp_path / "fx"
    fx.mkdir(exist_ok=True)
    (fx / "search.json").write_text(json.dumps({"issues": issues}))
    (fx / "transitions.json").write_text(json.dumps(
        {"transitions": [{"id": "31", "name": "Done"}]}))
    return fx


def enrich_script(tmp_path, payload):
    """A fake enrichment agent: copies payload to <KEY>.enrichment.json and counts runs."""
    pf = tmp_path / "enrich-payload.json"
    pf.write_text(json.dumps(payload))
    script = tmp_path / "enrich.py"
    script.write_text(textwrap.dedent(f"""\
        import os, shutil
        from pathlib import Path
        key = os.environ["JIRA_KEY"]
        d = Path(os.environ["AP_STATE"]) / "jira"
        d.mkdir(parents=True, exist_ok=True)
        runs = d / "enrich-runs.txt"
        runs.write_text((runs.read_text() if runs.exists() else "") + key + "\\n")
        shutil.copy({str(pf)!r}, d / (key + ".enrichment.json"))
        """))
    return f"{sys.executable} {script}"


def run(repo, fx=None, enrich=None, *flags):
    env = dict(os.environ, AP_HOME=str(repo / ".autopilot"),
               AP_STATE=str(repo / ".autopilot" / "state"), ORBIT_HOME=str(ROOT))
    env.pop("ORBIT_JIRA_FIXTURES", None)
    env.pop("ORBIT_JIRA_ENRICH_CMD", None)
    if fx:
        env["ORBIT_JIRA_FIXTURES"] = str(fx)
    if enrich:
        env["ORBIT_JIRA_ENRICH_CMD"] = enrich
    return subprocess.run([sys.executable, str(ADAPTER), *flags], cwd=repo,
                          capture_output=True, text=True, env=env)


def tasks(repo):
    return (yaml.safe_load((repo / ".autopilot" / "backlog.yaml").read_text()) or {}).get("tasks") or []


def posted(fx):
    f = fx / "posted.jsonl"
    return [json.loads(ln) for ln in f.read_text().splitlines()] if f.exists() else []


def test_no_token_noops_loudly(tmp_path):
    repo = target(tmp_path)
    (repo / ".autopilot" / "state" / ".jira_token").unlink()
    r = run(repo)  # no fixtures → would need real creds; must refuse before any I/O
    assert r.returncode == 0, r.stderr
    assert "no token" in r.stdout
    assert tasks(repo) == []


def test_enriched_ticket_imports_as_proposed_human(tmp_path):
    repo = target(tmp_path)
    fx = fixtures(tmp_path, [issue()])
    r = run(repo, fx, enrich_script(tmp_path, GOOD_ENRICH))
    assert r.returncode == 0, r.stderr
    t = tasks(repo)
    assert [x["id"] for x in t] == ["jira-cdp-7"]
    assert t[0]["status"] == "proposed" and t[0]["autopilot"] == "human"
    assert t[0]["source"] == "jira"
    assert "From Jira CDP-7" in t[0]["context"]
    m = json.loads((repo / ".autopilot" / "state" / "jira_map.json").read_text())
    assert m["CDP-7"]["state"] == "imported"


def test_import_is_idempotent(tmp_path):
    repo = target(tmp_path)
    fx = fixtures(tmp_path, [issue()])
    enrich = enrich_script(tmp_path, GOOD_ENRICH)
    run(repo, fx, enrich)
    run(repo, fx, enrich)
    assert len(tasks(repo)) == 1
    runs = (repo / ".autopilot" / "state" / "jira" / "enrich-runs.txt").read_text()
    assert runs.count("CDP-7") == 1          # imported → never re-enriched


def test_auto_queue_flips_status_and_autopilot(tmp_path):
    repo = target(tmp_path, extra_cfg="  auto_queue: true\n")
    fx = fixtures(tmp_path, [issue()])
    run(repo, fx, enrich_script(tmp_path, GOOD_ENRICH))
    t = tasks(repo)[0]
    assert t["status"] == "queued" and t["autopilot"] == "allow"


def test_thin_ticket_gets_questions_not_a_task(tmp_path):
    repo = target(tmp_path)
    fx = fixtures(tmp_path, [issue()])
    r = run(repo, fx, enrich_script(tmp_path, {"questions": ["Which screen?", "What columns?"]}))
    assert r.returncode == 0, r.stderr
    assert tasks(repo) == []
    p = posted(fx)
    assert len(p) == 1 and "/comment" in p[0]["path"]
    assert "Which screen?" in p[0]["payload"]["body"]
    m = json.loads((repo / ".autopilot" / "state" / "jira_map.json").read_text())
    assert m["CDP-7"]["state"] == "needs_info"


def test_draft_failing_lint_hard_gate_becomes_questions(tmp_path):
    repo = target(tmp_path)
    fx = fixtures(tmp_path, [issue()])
    bad = {"task": dict(GOOD_ENRICH["task"], acceptance_criteria=[])}
    run(repo, fx, enrich_script(tmp_path, bad))
    assert tasks(repo) == []                  # no criteria → never imported
    body = posted(fx)[0]["payload"]["body"]
    assert "contract gate" in body


def test_needs_info_retries_only_after_human_update(tmp_path):
    repo = target(tmp_path)
    fx = fixtures(tmp_path, [issue(updated="2026-07-18T10:00:00.000+0530")])
    q = enrich_script(tmp_path, {"questions": ["Which screen?"]})
    run(repo, fx, q)
    run(repo, fx, q)                          # ticket unchanged → no re-enrich, no re-comment
    runs = (repo / ".autopilot" / "state" / "jira" / "enrich-runs.txt").read_text()
    assert runs.count("CDP-7") == 1
    assert len(posted(fx)) == 1
    fixtures(tmp_path, [issue(updated="2026-07-18T11:00:00.000+0530")])  # human replied
    run(repo, fx, enrich_script(tmp_path, GOOD_ENRICH))
    assert [x["id"] for x in tasks(repo)] == ["jira-cdp-7"]


def test_pr_and_merge_writeback_exactly_once(tmp_path):
    repo = target(tmp_path, extra_cfg='  on_merge_transition: "Done"\n')
    fx = fixtures(tmp_path, [])
    st = repo / ".autopilot" / "state"
    (st / "jira_map.json").write_text(json.dumps(
        {"CDP-7": {"task_id": "jira-cdp-7", "state": "imported", "updated": "x"}}))
    (st / "ledger.json").write_text(json.dumps({"entries": {"jira-cdp-7": {
        "state": "pushed", "pr_url": "https://github.com/x/r/pull/9"}}}))
    run(repo, fx)
    run(repo, fx)
    p = posted(fx)
    assert len(p) == 1 and "pull/9" in p[0]["payload"]["body"]
    # now the operator merges (dashboard `ledger.py mark merged`)
    (st / "ledger.json").write_text(json.dumps({"entries": {"jira-cdp-7": {
        "state": "merged", "pr_url": "https://github.com/x/r/pull/9"}}}))
    run(repo, fx)
    run(repo, fx)
    p = posted(fx)
    kinds = [(x["method"], x["path"].rsplit("/", 1)[-1]) for x in p]
    assert kinds.count(("POST", "comment")) == 2       # PR once, merged once
    assert kinds.count(("POST", "transitions")) == 1   # Done transition once


def test_rejected_writeback_carries_the_reason(tmp_path):
    repo = target(tmp_path)
    fx = fixtures(tmp_path, [])
    st = repo / ".autopilot" / "state"
    (st / "jira_map.json").write_text(json.dumps(
        {"CDP-7": {"task_id": "jira-cdp-7", "state": "imported", "updated": "x"}}))
    (st / "ledger.json").write_text(json.dumps({"entries": {"jira-cdp-7": {
        "state": "rejected", "review_note": "wrong screen entirely"}}}))
    run(repo, fx)
    assert "wrong screen entirely" in posted(fx)[0]["payload"]["body"]


def test_agent_input_never_contains_the_token(tmp_path):
    repo = target(tmp_path)
    fx = fixtures(tmp_path, [issue()])
    run(repo, fx, enrich_script(tmp_path, GOOD_ENRICH))
    ticket = (repo / ".autopilot" / "state" / "jira" / "CDP-7.ticket.json").read_text()
    assert "tok123" not in ticket


def test_dry_run_writes_nothing(tmp_path):
    repo = target(tmp_path)
    fx = fixtures(tmp_path, [issue()])
    run(repo, fx, enrich_script(tmp_path, GOOD_ENRICH), "--dry-run")
    assert tasks(repo) == []
    assert not (repo / ".autopilot" / "state" / "jira_map.json").exists()
    assert posted(fx) == []


def test_missing_jira_block_warns_and_noops(tmp_path):
    repo = target(tmp_path)
    cfg = repo / ".autopilot" / "config.yaml"
    cfg.write_text('repo: "."\nbase_branch: "main"\nsources: [backlog, jira]\ngates: {t: {cmd: "true"}}\n')
    r = run(repo)
    assert r.returncode == 0
    assert "jira: block" in r.stdout
