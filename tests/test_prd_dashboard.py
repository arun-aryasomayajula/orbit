# M2 dashboard: /api/secrets — .autopilot/.secrets.env writer + masked reader.
# do_save_secrets validates names/values BEFORE touching disk (no partial
# writes on a rejected payload), writes atomically at mode 0600 in the exact
# bare KEY=VALUE format engine/doctor.py's _secrets_env parser (and the loop's
# secrets parser in run.sh) expects, and the GET side (secrets_payload) never
# returns a full value — only a name + short preview. Values are restricted
# to a shell-metacharacter-free allowlist: defense in depth on top of run.sh
# parsing (not sourcing) the file.
#
# Run: python3 -m pytest tests/test_prd_dashboard.py -v   (from orbit/)
import importlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    ap_home = tmp_path / ".autopilot"
    ap_state = ap_home / "state"
    ap_state.mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(ap_state))
    (ap_home / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n')

    sys.path.insert(0, str(ENGINE))
    sys.modules.pop("command_center", None)
    mod = importlib.import_module("command_center")
    importlib.reload(mod)
    yield mod
    sys.path.remove(str(ENGINE))


def _secrets_file(cc):
    return Path(cc.AP_HOME) / ".secrets.env"


def test_save_creates_file_mode_0600_with_exact_content(cc):
    msg = cc.do_save_secrets({"set": {"ANALYTICS_TOKEN": "sk-abc123"}})
    assert msg == "Saved 1 secret(s), removed 0"
    f = _secrets_file(cc)
    assert f.exists()
    assert f.read_text() == "ANALYTICS_TOKEN=sk-abc123\n"
    mode = stat.S_IMODE(f.stat().st_mode)
    assert mode == 0o600


def test_get_masks_values(cc):
    cc.do_save_secrets({"set": {"ANALYTICS_TOKEN": "sk-abc123", "SHORT": "ab"}})
    payload = cc.secrets_payload()
    dumped = json.dumps(payload)
    assert "sk-abc123" not in dumped
    by_name = {s["name"]: s["preview"] for s in payload["secrets"]}
    # Fixed mask + length only - no value characters (not even the first/last
    # two) ever leave the server, regardless of the value's length.
    assert by_name["ANALYTICS_TOKEN"] == "•••• (9 chars)"
    assert by_name["SHORT"] == "•••• (2 chars)"


def test_bad_name_rejected(cc):
    msg = cc.do_save_secrets({"set": {"bad-name": "value"}})
    assert "reject" in msg.lower()
    assert not _secrets_file(cc).exists()

    msg2 = cc.do_save_secrets({"set": {"1STARTSNUM": "value"}})
    assert "reject" in msg2.lower()
    assert not _secrets_file(cc).exists()


def test_multiline_value_rejected(cc):
    msg = cc.do_save_secrets({"set": {"GOOD_NAME": "line1\nline2"}})
    assert "reject" in msg.lower()
    assert not _secrets_file(cc).exists()


def test_shell_metacharacter_value_rejected(cc):
    # run.sh's loader parses (never sources) this file, but the value
    # allowlist is defense in depth: a value that looks like a command
    # substitution must never be accepted, let alone hit disk.
    msg = cc.do_save_secrets({"set": {"EVIL": "$(touch pwned)"}})
    assert "reject" in msg.lower()
    assert "pwned" not in msg   # value itself never echoed back
    assert not _secrets_file(cc).exists()


def test_jwt_like_value_with_dots_and_dashes_accepted(cc):
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N-XgL0n3I9PlFUP0THsR8U"
    msg = cc.do_save_secrets({"set": {"AUTH_TOKEN": jwt}})
    assert msg == "Saved 1 secret(s), removed 0"
    assert _secrets_file(cc).read_text() == f"AUTH_TOKEN={jwt}\n"


def test_delete_removes_key_and_preserves_others(cc):
    cc.do_save_secrets({"set": {"KEEP_ME": "keepvalue", "DROP_ME": "dropvalue"}})
    msg = cc.do_save_secrets({"delete": ["DROP_ME"]})
    assert msg == "Saved 0 secret(s), removed 1"
    content = _secrets_file(cc).read_text()
    assert "DROP_ME" not in content
    assert "KEEP_ME=keepvalue" in content


def test_delete_of_absent_key_reports_honestly(cc):
    # A delete for a name that was never set is a no-op, not a lie - the
    # message must say "removed 0", not claim credit for a key that was
    # never there to begin with.
    msg = cc.do_save_secrets({"delete": ["NEVER_SET"]})
    assert msg == "Saved 0 secret(s), removed 0"
    assert _secrets_file(cc).read_text() == ""


def test_reserved_name_path_rejected(cc):
    # A metacharacter-free VALUE isn't enough — run.sh's parse loop exports
    # whatever name/value it reads, so PATH=/tmp/evil poisons every bare
    # python3/git/gh call the loop makes on its next restart. No shell
    # metacharacters required: this is RCE via name alone.
    msg = cc.do_save_secrets({"set": {"PATH": "/tmp/evil"}})
    assert "reserved" in msg.lower()
    assert "PATH" in msg
    assert not _secrets_file(cc).exists()


def test_reserved_prefix_ld_preload_rejected(cc):
    msg = cc.do_save_secrets({"set": {"LD_PRELOAD": "/tmp/evil.so"}})
    assert "reserved" in msg.lower()
    assert not _secrets_file(cc).exists()

    # The prefix block also applies on the delete side (deleting a reserved
    # name is harmless in practice, but the guard should not need to
    # distinguish set vs delete to stay simple and obviously correct).
    msg2 = cc.do_save_secrets({"delete": ["DYLD_INSERT_LIBRARIES"]})
    assert "reserved" in msg2.lower()


def test_analytics_token_still_accepted_alongside_reserved_name_guard(cc):
    msg = cc.do_save_secrets({"set": {"ANALYTICS_TOKEN": "sk-abc123"}})
    assert msg == "Saved 1 secret(s), removed 0"
    assert _secrets_file(cc).read_text() == "ANALYTICS_TOKEN=sk-abc123\n"


@pytest.mark.parametrize("name,value", [
    ("NODE_OPTIONS", "--require=/tmp/evil.js"),
    ("NODE_PATH", "/tmp/evil"),
    ("PERL5LIB", "/tmp/evil"),
    ("RUBYOPT", "-r/tmp/evil"),
    ("GEM_PATH", "/tmp/evil"),
    ("CLASSPATH", "/tmp/evil.jar"),
])
def test_other_interpreter_loader_names_rejected(cc, name, value):
    """PYTHONPATH was never the only loader hook. A cycle reaches node (mcp
    stdio servers run via npx), and NODE_OPTIONS=--require=... is code
    execution with no shell metacharacters in sight."""
    msg = cc.do_save_secrets({"set": {name: value}})
    assert "reserved" in msg.lower()
    assert name in msg
    assert not _secrets_file(cc).exists()


def test_panel_delegates_to_the_shared_secrets_parser(cc):
    """One parser, four callers (run.sh's loop, this panel, doctor, prd_flow's
    stage children). This wrapper must stay thin - three re-implementations are
    how the format drifted the first time."""
    src = (Path(cc.ENGINE) / "command_center.py").read_text()
    body = src.split("def _read_secrets_env()", 1)[1].split("\ndef ", 1)[0]
    assert "secrets_env.read_path" in body
    assert "split(" not in body, "the panel re-implements the parse instead of delegating"


def test_name_deny_list_matches_run_sh_case_guard(cc):
    """The dashboard writer and run.sh's parse loop must refuse the same names -
    a name only one of them blocks is a hole in whichever end forgot it."""
    run_sh = (Path(cc.ENGINE) / "run.sh").read_text()
    guard = [ln for ln in run_sh.splitlines() if ") continue ;;" in ln and "PATH|" in ln]
    assert len(guard) == 1, "run.sh's secrets name guard moved; fix this parser, not the assert"
    patterns = guard[0].strip().split(")")[0].split("|")
    exact = {p for p in patterns if not p.endswith("*")}
    prefixes = tuple(p[:-1] for p in patterns if p.endswith("*"))
    assert exact == cc._SECRETS_NAME_DENY
    assert prefixes == cc._SECRETS_PREFIX_DENY


# ── .gitignore upkeep (the panel must never be the thing that leaks) ───────

def test_save_adds_the_gitignore_entry_when_it_is_missing(cc):
    gi = Path(cc.AP_HOME) / ".gitignore"
    gi.write_text("# Orbit secrets\n.jira_token\n.slack_webhook\n")   # pre-M2 shape
    cc.do_save_secrets({"set": {"ANALYTICS_TOKEN": "sk-abc123"}})
    lines = gi.read_text().splitlines()
    assert ".secrets.env" in lines
    assert lines.count(".secrets.env") == 1
    assert ".jira_token" in lines   # nothing existing was rewritten away


def test_save_creates_the_gitignore_when_absent_and_never_duplicates(cc):
    gi = Path(cc.AP_HOME) / ".gitignore"
    assert not gi.exists()
    cc.do_save_secrets({"set": {"ANALYTICS_TOKEN": "sk-abc123"}})
    assert ".secrets.env" in gi.read_text().splitlines()
    cc.do_save_secrets({"set": {"OTHER_TOKEN": "sk-def456"}})
    assert gi.read_text().splitlines().count(".secrets.env") == 1


def test_save_tolerates_a_gitignore_with_no_trailing_newline(cc):
    gi = Path(cc.AP_HOME) / ".gitignore"
    gi.write_text(".jira_token")   # no trailing newline
    cc.do_save_secrets({"set": {"ANALYTICS_TOKEN": "sk-abc123"}})
    lines = gi.read_text().splitlines()
    assert lines == [".jira_token", ".secrets.env"]


# ── mcp config card (Task 8) ──────────────────────────────────────────────
# mcp joins the gates/qa JSON-textarea family in the Admin panel:
# _validate_config only checks shape (must be a dict) — deep validation
# (transport, url/command, tools) stays in config.py's own `validate`, which
# do_save_config already shells out to post-write, rolling the file back to
# its exact prior bytes on failure.

def test_do_save_config_accepts_valid_mcp_block(cc):
    payload = {"mcp": {"analytics": {"transport": "http", "url": "http://localhost:9999", "tools": "*"}}}
    msg = cc.do_save_config(json.dumps(payload))
    assert msg.startswith("✓ Saved")
    saved = cc._load_cfg()
    assert saved["mcp"] == payload["mcp"]


def test_do_save_config_rejects_http_mcp_server_without_url_and_rolls_back(cc):
    cfg_path = Path(cc.AP_HOME) / "config.yaml"
    before = cfg_path.read_text()
    payload = {"mcp": {"analytics": {"transport": "http", "tools": "*"}}}
    msg = cc.do_save_config(json.dumps(payload))
    assert msg.startswith("Rejected")
    assert "mcp.analytics.url is required for http transport" in msg
    assert cfg_path.read_text() == before   # byte-identical rollback


# ── insight quick-add (Task 8) ────────────────────────────────────────────
# The quick-add button is pure JS prefill in the browser — the server side
# is exactly do_task_new. category=insight is in KNOWN_CATEGORIES and SAFE
# (via EMITTABLE), so queue=1 lands autopilot: allow the same way any other
# SAFE category does.

def test_task_new_insight_with_queue_lands_queued_and_allow(cc):
    (Path(cc.AP_HOME) / "backlog.yaml").write_text("tasks:\n")
    msg = cc.do_task_new(
        "insight-q3-churn", "Q3 churn drivers", "insight", "medium", "",
        "insights/ doc passes insight_lint (Answer + Basis, every number cited)",
        queue_now=True,
    )
    assert msg.startswith("Created insight-q3-churn")
    text = (Path(cc.AP_HOME) / "backlog.yaml").read_text()
    assert "category: insight" in text
    assert "status: queued" in text
    assert "autopilot: allow" in text


# ── PRD pipeline band (Task 9) ─────────────────────────────────────────────
# prd_pipeline_state()/build_state's `prd` key, /prd-action's dashboard-side
# legality mirror of prd_flow's status machine, and /prd-artifact's
# confinement (run-id shape, artifact allowlist, realpath-under-research/).

GOOD_INTAKE = {"run_id": "r1", "title": "t", "pm": "p", "goal": "g", "scope_in": ["a"],
              "scope_out": [], "data_targets": [], "code_targets": [], "pii_touch": False}


def test_prd_key_absent_without_research_dir(cc):
    assert not (Path(cc.REPO) / "research").exists()
    assert "prd" not in cc.build_state()
    assert cc.prd_pipeline_state() is None


def test_prd_pipeline_state_shape_and_next_actions(cc):
    cc.prd_flow.new(str(cc.REPO), "r1", "My PRD", brief="ctx")
    (Path(cc.REPO) / "research" / "r1" / "01-intake.json").write_text(json.dumps(GOOD_INTAKE))
    cc.prd_flow.record_stage(str(cc.REPO), "r1", "intake")

    runs = cc.prd_pipeline_state()
    assert runs is not None and len(runs) == 1
    r = runs[0]
    assert r["run_id"] == "r1"
    assert r["title"] == "My PRD"
    assert r["status"] == "awaiting-scope-approval"
    assert r["tamper"] == []
    names = [s["name"] for s in r["stages"]]
    assert names == ["intake", "research", "synthesis", "draft", "review"]
    intake_stage = r["stages"][0]
    assert intake_stage["ok"] is True and intake_stage["stamped_at"]
    research_stage = r["stages"][1]
    assert research_stage["ok"] is None and research_stage["stamped_at"] is None
    assert r["next_actions"] == ["approve-scope", "revise-intake"]

    # build_state carries the same key, present because research/ now exists.
    s = cc.build_state()
    assert "prd" in s and s["prd"][0]["run_id"] == "r1"


def test_prd_action_new_records_a_pm_without_asking_for_one(cc):
    """The New PRD form deliberately does not prompt for a PM - the server
    resolves it from the workspace's git identity. What it must never do is
    leave it empty, which is what stalled intake's eval on the first real run."""
    msg = cc.do_prd_action("r1", "new", "My PRD", "ctx")
    assert msg.startswith("Created")
    assert cc.prd_flow.load_marker(str(cc.REPO), "r1")["pm"]


def test_prd_action_new_passes_an_explicit_pm_through(cc):
    cc.do_prd_action("r1", "new", "My PRD", "ctx", pm="Grace Hopper")
    assert cc.prd_flow.load_marker(str(cc.REPO), "r1")["pm"] == "Grace Hopper"


def test_prd_action_approve_scope_flips_marker_status(cc):
    cc.prd_flow.new(str(cc.REPO), "r1", "My PRD")
    (Path(cc.REPO) / "research" / "r1" / "01-intake.json").write_text(json.dumps(GOOD_INTAKE))
    cc.prd_flow.record_stage(str(cc.REPO), "r1", "intake")

    msg = cc.do_prd_action("r1", "approve-scope")
    assert "approved" in msg.lower()
    assert cc.prd_flow.load_marker(str(cc.REPO), "r1")["status"] == "research"

    # Illegal from the new status - no mutation, an error message instead.
    msg2 = cc.do_prd_action("r1", "approve-scope")
    assert "cannot" in msg2.lower()
    assert cc.prd_flow.load_marker(str(cc.REPO), "r1")["status"] == "research"


def test_prd_action_run_chain_spawns_detached_popen_with_chain_flag(cc, monkeypatch):
    cc.prd_flow.new(str(cc.REPO), "r1", "My PRD")
    (Path(cc.REPO) / "research" / "r1" / "01-intake.json").write_text(json.dumps(GOOD_INTAKE))
    cc.prd_flow.record_stage(str(cc.REPO), "r1", "intake")
    cc.prd_flow.approve_scope(str(cc.REPO), "r1")   # status -> "research"

    calls = []

    class FakeProc:
        pass

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakeProc()

    monkeypatch.setattr(cc.subprocess, "Popen", fake_popen)
    msg = cc.do_prd_action("r1", "run-chain")
    assert "launched" in msg.lower()
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert "run" in cmd
    assert "r1" in cmd
    assert "research" in cmd          # the marker's CURRENT stage, not "intake"
    assert "--chain" in cmd
    assert kwargs.get("start_new_session") is True


def test_prd_action_run_intake_has_no_chain_flag(cc, monkeypatch):
    cc.prd_flow.new(str(cc.REPO), "r1", "My PRD")   # status stays "intake"
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    cc.do_prd_action("r1", "run-intake")
    cmd, kwargs = calls[0]
    assert "intake" in cmd
    assert "--chain" not in cmd
    assert kwargs.get("start_new_session") is True


def test_prd_artifact_confinement(cc):
    cc.prd_flow.new(str(cc.REPO), "r1", "My PRD")
    (Path(cc.REPO) / "research" / "r1" / "01-intake.json").write_text(json.dumps(GOOD_INTAKE))
    cc.prd_flow.record_stage(str(cc.REPO), "r1", "intake")

    assert cc.read_prd_artifact("r1", "../../etc/passwd") is None
    assert cc.read_prd_artifact("r1", "not-a-real-artifact.json") is None
    assert cc.read_prd_artifact("../r1", "01-intake.json") is None
    assert cc.read_prd_artifact("R1", "01-intake.json") is None   # uppercase not in the allowed shape

    body = cc.read_prd_artifact("r1", "01-intake.json")
    assert body is not None
    assert json.loads(body) == GOOD_INTAKE


def test_corrupt_marker_does_not_freeze_the_state_poll(cc):
    # A prd-run.json that's valid JSON but not an object (null, a bare list,
    # a string) must not take down build_state()'s every-3s poll for every
    # other run - one bad marker is skipped, not fatal.
    cc.prd_flow.new(str(cc.REPO), "r1", "Healthy PRD")

    (Path(cc.REPO) / "research" / "r2").mkdir()
    (Path(cc.REPO) / "research" / "r2" / "prd-run.json").write_text("null")

    (Path(cc.REPO) / "research" / "r3").mkdir()
    (Path(cc.REPO) / "research" / "r3" / "prd-run.json").write_text("[]")

    runs = cc.prd_pipeline_state()
    assert runs is not None
    assert [r["run_id"] for r in runs] == ["r1"]

    s = cc.build_state()   # the actual /api/state path - must not raise
    assert [r["run_id"] for r in s["prd"]] == ["r1"]


# ── status machine hygiene ────────────────────────────────────────────────

def test_status_actions_cover_exactly_the_controllers_statuses(cc):
    """The dashboard mirrors prd_flow's status machine so illegal buttons never
    render. A status the controller can reach but this table has never heard of
    renders a card with no way forward; an entry for a status that no longer
    exists is dead code nobody notices."""
    assert set(cc._PRD_STATUS_ACTIONS) == set(cc.prd_flow.STAGES) | {
        "awaiting-scope-approval", "awaiting-signoff", "done"}


def test_run_id_shape_is_the_controllers(cc):
    assert cc._PRD_RUN_RE is cc.prd_flow.RUN_RE


def test_done_run_can_be_reopened(cc):
    """Sign-off is not the end of a PRD's life. Without revise-intake on `done`
    the only way to reopen a shipped run was hand-editing the marker."""
    cc.prd_flow.new(str(cc.REPO), "r1", "My PRD")
    (Path(cc.REPO) / "research" / "r1" / "01-intake.json").write_text(json.dumps(GOOD_INTAKE))
    cc.prd_flow.record_stage(str(cc.REPO), "r1", "intake")
    cc.prd_flow.approve_scope(str(cc.REPO), "r1")
    marker = cc.prd_flow.load_marker(str(cc.REPO), "r1")
    marker["status"] = "awaiting-signoff"
    cc.prd_flow._write_marker(str(cc.REPO), "r1", marker)
    cc.prd_flow.approve_prd(str(cc.REPO), "r1")

    run = cc.prd_pipeline_state()[0]
    assert run["status"] == "done"
    assert run["next_actions"] == ["revise-intake"]

    msg = cc.do_prd_action("r1", "revise-intake")
    assert "revising" in msg.lower()
    reopened = cc.prd_flow.load_marker(str(cc.REPO), "r1")
    assert reopened["status"] == "intake"
    assert reopened["approvals"] == {"scope": None, "prd": None}
    assert reopened["stages"] == {}


def test_revise_synthesis_is_legal_from_awaiting_signoff(cc):
    """awaiting-signoff must offer a way back to synthesis, not just draft/
    research - a review that finds the SYNTHESIS wrong (not just its writeup)
    otherwise has no legal action to say so."""
    cc.prd_flow.new(str(cc.REPO), "r1", "My PRD")
    marker = cc.prd_flow.load_marker(str(cc.REPO), "r1")
    marker["status"] = "awaiting-signoff"
    marker["stages"] = {s: "x" for s in cc.prd_flow.STAGES}
    cc.prd_flow._write_marker(str(cc.REPO), "r1", marker)

    assert "revise-synthesis" in cc._prd_next_actions("awaiting-signoff")

    msg = cc.do_prd_action("r1", "revise-synthesis")
    assert "revising" in msg.lower()
    reopened = cc.prd_flow.load_marker(str(cc.REPO), "r1")
    assert reopened["status"] == "synthesis"
    assert reopened["approvals"]["prd"] is None
    assert set(reopened["stages"]) == {"intake", "research"}


# ── pii_touch scope gate (dashboard end) ──────────────────────────────────

PII_INTAKE = dict(GOOD_INTAKE, pii_touch=True)


def _pii_run_at_gate(cc):
    cc.prd_flow.new(str(cc.REPO), "r1", "Sensitive PRD")
    (Path(cc.REPO) / "research" / "r1" / "01-intake.json").write_text(json.dumps(PII_INTAKE))
    cc.prd_flow.record_stage(str(cc.REPO), "r1", "intake")


def test_pii_flag_reaches_the_card(cc):
    _pii_run_at_gate(cc)
    assert cc.prd_pipeline_state()[0]["pii_touch"] is True


def test_approve_scope_on_a_pii_run_needs_ack_pii(cc):
    _pii_run_at_gate(cc)
    msg = cc.do_prd_action("r1", "approve-scope")
    assert "cannot approve scope" in msg.lower()
    assert "PII acknowledgement" in msg
    assert cc.prd_flow.load_marker(str(cc.REPO), "r1")["status"] == "awaiting-scope-approval"

    msg = cc.do_prd_action("r1", "approve-scope", ack_pii=True)
    assert "approved" in msg.lower()
    assert cc.prd_flow.load_marker(str(cc.REPO), "r1")["status"] == "research"


def test_shell_asks_a_second_question_before_approving_a_pii_scope(cc):
    """The grounding contract defers PII compliance to a human. One click on the
    same button every other run uses is not that human saying yes to PII."""
    shell = (Path(cc.ENGINE) / "cc_shell.html").read_text()
    assert "PRD_PII_CONFIRM" in shell
    assert "PII/sensitive data" in shell
    assert "ack_pii:'1'" in shell
    assert "r.pii_touch" in shell


# ── watches / surveys / insight_docs ───────────────────────────────────────
# build_state's absent-unless-configured trio: watches_state (config
# `watches:` + $AP_STATE/watches.json + the newest watch-<name>-* backlog
# task), surveys_state (surveys/*/ subdirs), insight_docs_state
# (insights/*.md), plus the confined /product-artifact GET for the two
# families /prd-artifact doesn't cover.

def test_watches_surveys_insight_docs_absent_when_unconfigured(cc):
    """No watches: block AT ALL (as opposed to an empty one - see below):
    the key is omitted entirely, so an unconfigured target's /api/state is
    byte-identical to what it was before any of this shipped."""
    s = cc.build_state()
    assert "watches" not in s
    assert "surveys" not in s
    assert "insight_docs" not in s
    assert cc.watches_state([]) is None
    assert cc.surveys_state() is None
    assert cc.insight_docs_state() is None


def _write_watches_config(cc, watches):
    import yaml
    cfg_path = Path(cc.AP_HOME) / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    cfg["watches"] = watches
    cfg_path.write_text(yaml.safe_dump(cfg))


def test_watches_state_empty_block_is_an_empty_list_not_absent(cc):
    """A block that IS there but empty is a different fact from no block, and
    the band renders it differently ("No watches configured." rather than
    vanishing) - which is exactly what the operator who just added the block
    and is hunting for it needs to see."""
    _write_watches_config(cc, [])
    assert cc.watches_state([]) == []
    assert cc.build_state()["watches"] == []


def test_watches_state_null_block_reads_as_empty_not_absent(cc):
    """`watches:` with nothing after it parses as None - indistinguishable
    from absent by VALUE, which is why the check is on the key."""
    cfg_path = Path(cc.AP_HOME) / "config.yaml"
    cfg_path.write_text(cfg_path.read_text() + "watches:\n")
    assert cc.watches_state([]) == []


def test_watches_state_malformed_block_is_omitted(cc):
    _write_watches_config(cc, {"not": "a list"})
    assert cc.watches_state([]) is None
    assert "watches" not in cc.build_state()


def test_watches_state_shape_no_task_filed_yet(cc):
    _write_watches_config(cc, [{"name": "wow-signup", "question": "What changed WoW?",
                                "cadence_hours": 168, "category": "insight"}])
    ws = cc.watches_state([])
    assert ws == [{"name": "wow-signup", "question": "What changed WoW?",
                   "cadence_hours": 168, "category": "insight", "last_filed": None}]
    s = cc.build_state()
    assert s["watches"] == ws


def test_watches_state_joins_last_filed_and_latest_backlog_task(cc):
    _write_watches_config(cc, [{"name": "wow-signup", "question": "What changed WoW?"}])
    (Path(cc.AP_STATE) / "watches.json").write_text(
        json.dumps({"wow-signup": "2026-08-10T00:00:00+00:00"}))
    backlog = [
        {"id": "watch-wow-signup-20260803", "status": "proposed"},
        {"id": "watch-wow-signup-20260810", "status": "queued"},   # newest by id - wins
        {"id": "watch-other-watch-20260811", "status": "proposed"},   # different watch - ignored
    ]
    ws = cc.watches_state(backlog)
    assert len(ws) == 1
    w = ws[0]
    assert w["last_filed"] == "2026-08-10T00:00:00+00:00"
    assert w["task_id"] == "watch-wow-signup-20260810"
    assert w["task_status"] == "queued"
    # defaults applied when the config omits them
    assert w["cadence_hours"] == 168
    assert w["category"] == "insight"


def test_watches_state_tolerates_corrupt_state_file(cc):
    _write_watches_config(cc, [{"name": "wow-signup", "question": "q"}])
    (Path(cc.AP_STATE) / "watches.json").write_text("not json{")
    ws = cc.watches_state([])
    assert ws[0]["last_filed"] is None
    s = cc.build_state()   # must not raise
    assert "watches" in s


def test_surveys_state_absent_without_surveys_dir_present_with_shape(cc):
    root = Path(cc.REPO) / "surveys" / "pilot"
    (root / "raw").mkdir(parents=True)
    (root / "raw" / "responses.csv").write_text("q1,q2\n")
    (root / "raw" / "responses2.csv").write_text("q1,q2\n")
    (root / "questionnaire.md").write_text("# Pilot\n")

    surveys = cc.surveys_state()
    assert surveys == [{"name": "pilot", "has_questionnaire": True,
                        "raw_count": 2, "has_report": False}]
    s = cc.build_state()
    assert s["surveys"] == surveys

    (root / "report.md").write_text("# Pilot\n## Answer\n## Basis\n")
    assert cc.surveys_state()[0]["has_report"] is True


def test_insight_docs_state_absent_without_insights_dir_present_with_stems(cc):
    root = Path(cc.REPO) / "insights"
    root.mkdir()
    (root / "wow-signup.md").write_text("# WoW signup\n")
    (root / "funnel-drop.md").write_text("# Funnel drop\n")

    docs = cc.insight_docs_state()
    assert docs == [{"name": "funnel-drop", "jira_key": None, "publish_error": None},
                    {"name": "wow-signup", "jira_key": None, "publish_error": None}]
    s = cc.build_state()
    assert s["insight_docs"] == docs


def test_product_artifact_serves_seeded_insight_and_survey_report(cc):
    ir = Path(cc.REPO) / "insights"
    ir.mkdir()
    (ir / "wow-signup.md").write_text("# WoW signup\n## Answer\nit went up\n## Basis\n- S1: data\n")

    sr = Path(cc.REPO) / "surveys" / "pilot"
    sr.mkdir(parents=True)
    (sr / "report.md").write_text("# Pilot\n## Answer\nfound X\n## Basis\n- S1: data\n")

    body = cc.read_product_artifact("insight", "wow-signup")
    assert body is not None and "WoW signup" in body

    body2 = cc.read_product_artifact("survey", "pilot")
    assert body2 is not None and "Pilot" in body2


def test_product_artifact_rejects_traversal_unknown_kind_and_missing(cc):
    ir = Path(cc.REPO) / "insights"
    ir.mkdir()
    (ir / "real.md").write_text("# Real\n")

    assert cc.read_product_artifact("insight", "../secret") is None
    assert cc.read_product_artifact("insight", "../../etc/passwd") is None
    assert cc.read_product_artifact("insight", "..") is None
    assert cc.read_product_artifact("bogus", "real") is None
    assert cc.read_product_artifact("survey", "nope") is None
    assert cc.read_product_artifact("insight", "not-real") is None
    # a real name still resolves
    assert cc.read_product_artifact("insight", "real") is not None


def test_category_fallback_lists_insight(cc):
    """The pre-first-poll fallback must list `insight`: product workspaces run on
    it, and the insight quick-add preselects it - a missing option silently
    falls back to `bug`."""
    shell = (Path(cc.ENGINE) / "cc_shell.html").read_text()
    fallback = shell.split("function _catOptions", 1)[1].split("\n}", 1)[0]
    assert "'insight'" in fallback


# ── per-slot workers (Task 7) ────────────────────────────────────────────
# workers_state() reads $AP_STATE/workers/<slot>.pid (run.sh's
# write_worker_pid: {pid, tid, started}) and stamps each with `alive`
# (locks.pid_alive) - the same identity-aware liveness ledger.py's reap
# already trusts (a slot loop's own pid outlives any single task, so a plain
# "is the loop alive" check would never go false). Absent from build_state
# (not merely empty) when there's nothing to show: the dir is empty or
# missing AND concurrency is 1 (today's overwhelming default) - same
# discipline as watches_state/qa_services_state.

def _guaranteed_dead_pid() -> int:
    """A pid that has definitely already exited - not a made-up number that
    might collide with something real and alive on the test machine (mirrors
    tests/test_slot_claims.py's helper of the same name)."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _write_worker_pid(cc, slot, pid, tid, started="2026-08-22T00:00:00Z",
                      cycle_pid=None, cycle_pgid=None):
    d = Path(cc.AP_STATE) / "workers"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"pid": pid, "tid": tid, "started": started}
    if cycle_pid is not None:
        rec["cycle_pid"] = cycle_pid
    if cycle_pgid is not None:
        rec["cycle_pgid"] = cycle_pgid
    (d / f"{slot}.pid").write_text(json.dumps(rec))


def _write_loop_pid(cc, slot, pid):
    """run.sh's lifetime loop-<slot>.pid, which lives in the SAME dir as the
    per-cycle files and must never be read as one."""
    d = Path(cc.AP_STATE) / "workers"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"loop-{slot}.pid").write_text(
        json.dumps({"pid": pid, "slot": str(slot), "started": "2026-08-22T00:00:00Z"}))


def _write_ledger(cc, entries):
    (Path(cc.AP_STATE) / "ledger.json").write_text(json.dumps({"entries": entries}))


def test_workers_absent_when_unconfigured(cc):
    """No workers/ dir at all, concurrency unset (defaults to 1) - the key
    must not appear, so a single-slot target's /api/state stays
    byte-identical to before T7."""
    assert cc.workers_state() is None
    assert "workers" not in cc.build_state()


def test_workers_shape_alive_and_dead_pid(cc):
    dead = _guaranteed_dead_pid()
    _write_worker_pid(cc, "0", os.getpid(), "t-live", "2026-08-22T00:00:00Z")
    _write_worker_pid(cc, "1", dead, "t-dead", "2026-08-22T00:01:00Z")
    ws = cc.workers_state()
    by_slot = {w["slot"]: w for w in ws}
    assert by_slot["0"] == {"slot": "0", "pid": os.getpid(), "tid": "t-live",
                            "started": "2026-08-22T00:00:00Z", "alive": True,
                            "cycle_pid": None, "cycle_pgid": None}
    assert by_slot["1"] == {"slot": "1", "pid": dead, "tid": "t-dead",
                            "started": "2026-08-22T00:01:00Z", "alive": False,
                            "cycle_pid": None, "cycle_pgid": None}
    s = cc.build_state()
    assert s["workers"] == ws


def test_workers_state_ignores_the_lifetime_loop_pid_files(cc):
    """F3: loop-<slot>.pid shares the workers dir but means "slot owned", not
    "task in flight". Counting one as a worker would leave a phantom row on
    the strip and make build_state's `live` true for the loop's whole life."""
    _write_loop_pid(cc, 0, os.getpid())
    assert cc.workers_state() is None          # nothing worth a strip
    assert "workers" not in cc.build_state()
    _write_worker_pid(cc, "0", os.getpid(), "t-live")
    ws = cc.workers_state()
    assert [w["slot"] for w in ws] == ["0"]    # the numeric stem only


def _write_aplog(cc, lines):
    log = Path(cc.AP_STATE) / "logs" / "orbit.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(lines))


_ONE_CYCLE_LOG = [
    "[2026-08-22 10:00:00] [slot 0] cycle start (task t-a, 2 pickable)\n",
    "[2026-08-22 10:04:00] [slot 0] pushed origin/autopilot/task-t-a (abc) — review\n",
]


def test_turnaround_is_reported_with_a_single_live_worker(cc):
    """Control for the interleaved-pairing test below: one worker means the
    start/terminal pairing in the shared log is unambiguous, so the number is
    real and must still be published."""
    _write_worker_pid(cc, "0", os.getpid(), "t-a")
    _write_aplog(cc, _ONE_CYCLE_LOG)
    rt = cc.probe_runtime()
    assert rt["last_cycle_dur"] == 240
    assert rt["avg_cycle_sec"] == 240
    assert rt["cycle_samples"] == 1


def test_turnaround_pairs_correctly_when_several_workers_interleave(cc):
    """M5: turnaround pairing is keyed by each line's "[slot N]" tag (from M4),
    not by position in the shared orbit.log. Interleaved so a position-based
    pairing would get it wrong: slot 1 starts and finishes entirely inside
    slot 0's still-open cycle, so a same-order algorithm would either close
    slot 0's start against slot 1's terminal line, or lose slot 0's pairing
    outright. Keyed pairing gets both right and no longer needs to go dark at
    concurrency > 1 (replaces F9's suppress-when->1 test)."""
    _write_worker_pid(cc, "0", os.getpid(), "t-a")
    _write_worker_pid(cc, "1", os.getpid(), "t-b")
    _write_aplog(cc, [
        "[2026-08-22 10:00:00] [slot 0] cycle start (task t-a, 2 pickable)\n",
        "[2026-08-22 10:01:00] [slot 1] cycle start (task t-b, 1 pickable)\n",
        "[2026-08-22 10:03:00] [slot 1] cycle committed 1 change(s) (def)\n",
        "[2026-08-22 10:05:00] [slot 0] pushed origin/autopilot/task-t-a (abc) — review\n",
    ])
    rt = cc.probe_runtime()
    assert rt["cycle_samples"] == 2                # slot 1 (120s) then slot 0 (300s)
    assert rt["last_cycle_dur"] == 300              # the LAST terminal line seen: slot 0's
    assert rt["avg_cycle_sec"] == 210               # (120 + 300) / 2
    assert rt["heartbeat"] is not None


def test_turnaround_pairs_untagged_legacy_lines_together(cc):
    """Logs written before slot tagging existed (or a single-slot deployment's
    untagged history) carry no "[slot N]" tag at all. Those lines share one
    bucket and keep pairing against each other exactly as they did before
    M5, unaffected by any slot-tagged lines elsewhere in the same log."""
    _write_worker_pid(cc, "0", os.getpid(), "t-a")
    _write_aplog(cc, [
        "[2026-08-22 09:00:00] cycle start (task t-old, 1 pickable)\n",
        "[2026-08-22 09:03:00] pushed origin/autopilot/task-t-old (abc) — review\n",
        "[2026-08-22 10:00:00] [slot 0] cycle start (task t-a, 2 pickable)\n",
        "[2026-08-22 10:04:00] [slot 0] pushed origin/autopilot/task-t-a (abc) — review\n",
    ])
    rt = cc.probe_runtime()
    assert rt["cycle_samples"] == 2                 # legacy pair (180s), slot 0 pair (240s)
    assert rt["last_cycle_dur"] == 240
    assert rt["avg_cycle_sec"] == 210                # (180 + 240) / 2


def test_probe_runtime_reuses_build_states_worker_read(cc, monkeypatch):
    """F9: one poll, one read of the pid files. build_state needs
    workers_state() for `live`/`workers`; probe_runtime needed the same list
    and used to recompute it (re-reading and liveness-probing every file) on
    every 3-second poll."""
    _write_worker_pid(cc, "0", os.getpid(), "t-a")
    real = cc.workers_state
    calls = []

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(cc, "workers_state", counting)
    cc.build_state()
    assert len(calls) == 1, f"workers_state() ran {len(calls)}x in one poll"
    # ...and standalone callers still get a self-sufficient probe_runtime.
    calls.clear()
    assert cc.probe_runtime()["daily_count"] == 0
    assert len(calls) == 1


def test_workers_present_but_empty_when_concurrency_over_one(cc):
    """concurrency > 1 with an empty/missing workers dir still shows the key
    ([] not absent) - an operator who just turned it on and is looking for
    the strip needs to see "no workers right now", not have it vanish."""
    import yaml
    cfg_path = Path(cc.AP_HOME) / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    cfg["concurrency"] = 3
    cfg_path.write_text(yaml.safe_dump(cfg))
    assert cc.workers_state() == []
    assert cc.build_state()["workers"] == []


def test_workers_tolerates_corrupt_pid_file(cc):
    d = Path(cc.AP_STATE) / "workers"
    d.mkdir(parents=True)
    (d / "0.pid").write_text("not json{")
    assert cc.workers_state() == []
    s = cc.build_state()   # must not raise
    assert s["workers"] == []


# ── board `live`: replacing the pgrep-based bool (Task 7) ────────────────
# An in_progress ledger entry only shows as RUNNING when build_state's `live`
# is true AND it's the claimed marker's task. `live` now comes from
# workers_state() (any alive worker) whenever $AP_STATE/workers/ exists at
# all - including "empty means genuinely idle", not a reason to fall back -
# and only drops back to the machine-wide cycle_pids() pgrep when that dir
# has never been created (a pre-T4 state dir, or the dashboard polled before
# the loop's very first iteration).

def test_in_progress_shows_running_when_a_worker_is_alive(cc, monkeypatch):
    # cycle_pids() still feeds the unrelated legacy `cycle_running` field
    # probe_runtime always computes - stub it to a harmless empty result so
    # this test isolates `live` (the ledger running/escalated split), the
    # thing workers_state() actually replaced.
    monkeypatch.setattr(cc, "cycle_pids", lambda: [])
    _write_ledger(cc, {"t1": {"state": "in_progress"}})
    (Path(cc.AP_STATE) / ".current-task-id").write_text("t1")
    _write_worker_pid(cc, "0", os.getpid(), "t1")
    s = cc.build_state()
    assert [r["id"] for r in s["running"]] == ["t1"]
    assert s["escalated"] == []


def test_in_progress_not_running_when_workers_dir_present_but_empty(cc, monkeypatch):
    """An empty but PRESENT workers/ dir means genuinely idle - this must NOT
    fall back to the machine-wide pgrep. cycle_pids() is stubbed to return a
    LIVE pid here specifically to prove the point: if `live` were still
    falling back to pgrep, this task would wrongly show as running."""
    monkeypatch.setattr(cc, "cycle_pids", lambda: [os.getpid()])
    (Path(cc.AP_STATE) / "workers").mkdir()
    _write_ledger(cc, {"t1": {"state": "in_progress"}})
    (Path(cc.AP_STATE) / ".current-task-id").write_text("t1")
    s = cc.build_state()
    assert s["running"] == []
    assert [r["id"] for r in s["escalated"]] == ["t1"]


def test_legacy_pgrep_fallback_only_when_workers_dir_never_existed(cc, monkeypatch):
    """No workers/ dir at all - falls back to cycle_pids(), exactly the
    pre-T7 behavior, so a state dir from before the pid-file contract existed
    still gets a working live/running signal."""
    assert not (Path(cc.AP_STATE) / "workers").exists()
    _write_ledger(cc, {"t1": {"state": "in_progress"}})
    (Path(cc.AP_STATE) / ".current-task-id").write_text("t1")

    monkeypatch.setattr(cc, "cycle_pids", lambda: [])
    s = cc.build_state()
    assert s["running"] == []
    assert [r["id"] for r in s["escalated"]] == ["t1"]
    assert "workers" not in s

    monkeypatch.setattr(cc, "cycle_pids", lambda: [12345])
    s2 = cc.build_state()
    assert [r["id"] for r in s2["running"]] == ["t1"]


# ── do_killtask(tid): identity-aware abort (Task 7) ───────────────────────
# Targeting a specific slot's process group replaces the old machine-wide
# pgrep-and-kill-everything behavior, which would abort every OTHER slot's
# in-flight task (and any unrelated orbit-cycle process on the box) right
# along with the one the operator actually clicked Abort on.

def _spy_signals(cc, monkeypatch):
    """Record every real signal do_killtask sends, while letting signal 0
    through: locks.pid_alive probes liveness with os.kill(pid, 0) on the SAME
    module object, so swallowing that would make every pid look dead and the
    targeted path would never be reached at all."""
    blasted, killed = [], []
    real_kill = cc.os.kill

    def spy_kill(pid, sig):
        if sig == 0:
            return real_kill(pid, sig)
        killed.append((pid, sig))

    monkeypatch.setattr(cc.os, "kill", spy_kill)
    monkeypatch.setattr(cc.os, "killpg", lambda pgid, sig: blasted.append((pgid, sig)))
    return blasted, killed


def test_killtask_targets_the_recorded_cycle_group_never_the_loop(cc, monkeypatch):
    # F2/C2: the pid file's `pid` is the slot LOOP's - killing its group is
    # what used to take the whole slot down with one aborted task. The signal
    # must go to `cycle_pgid` (run.sh's record_cycle_pid) and to nothing else.
    # Both pid files must name genuinely LIVE pids - workers_state() gates the
    # match on `alive` (locks.pid_alive), so a made-up pid would never match
    # and the test would silently exercise the legacy fallback instead.
    other_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        _write_worker_pid(cc, "1", os.getpid(), "t-target",
                          cycle_pid=555001, cycle_pgid=555001)
        _write_worker_pid(cc, "0", other_proc.pid, "t-other",
                          cycle_pid=555002, cycle_pgid=555002)
        monkeypatch.setattr(cc, "cycle_pids", lambda: (_ for _ in ()).throw(
            AssertionError("must not touch the machine-wide pgrep once a pid file matched")))
        monkeypatch.setattr(cc.os, "getpgid", lambda pid: (_ for _ in ()).throw(
            AssertionError("the loop's own group is never consulted, let alone signalled")))
        blasted, killed = _spy_signals(cc, monkeypatch)
        msg = cc.do_killtask("t-target")
        # ONLY the target's own cycle group - not the loop's, not t-other's
        assert blasted == [(555001, cc.signal.SIGTERM)]
        assert killed == []
        assert "t-target" in msg and "slot 1" in msg
        assert str(os.getpid()) not in msg          # the loop pid is not the story
        assert "t-target" in cc.load_skips()
    finally:
        other_proc.kill()
        other_proc.wait()


def test_killtask_terms_the_bare_cycle_pid_when_it_leads_no_group(cc, monkeypatch):
    """No cycle_pgid means run.sh could not confirm the cycle leads its own
    group (setsid refused), and killpg would then blast the LOOP's group. A
    plain TERM on the cycle pid is all that is safe."""
    _write_worker_pid(cc, "0", os.getpid(), "t-nogroup", cycle_pid=555003)
    blasted, killed = _spy_signals(cc, monkeypatch)
    msg = cc.do_killtask("t-nogroup")
    assert killed == [(555003, cc.signal.SIGTERM)]
    assert blasted == []
    assert "t-nogroup" in cc.load_skips()
    assert "555003" in msg


def test_killtask_refuses_to_guess_when_no_cycle_is_recorded_yet(cc, monkeypatch):
    """Claimed but no cycle pid on file (the window between the claim and the
    cycle's first instant). The only other pid there is the loop's, so
    nothing is signalled - but the task is still skipped, and the message
    says so rather than implying a kill that never happened."""
    _write_worker_pid(cc, "0", os.getpid(), "t-early")
    monkeypatch.setattr(cc, "cycle_pids", lambda: (_ for _ in ()).throw(
        AssertionError("must not fall through to the machine-wide pgrep")))
    blasted, killed = _spy_signals(cc, monkeypatch)
    msg = cc.do_killtask("t-early")
    assert blasted == [] and killed == []
    assert "nothing was stopped" in msg
    assert "t-early" in cc.load_skips()


def test_killtask_falls_back_to_legacy_pgrep_when_tid_unmatched(cc, monkeypatch):
    """No pid file names this tid (a stale dashboard tab, or a state dir with
    no workers/ at all) - the original machine-wide behavior, so the Abort
    button still does something instead of silently no-op'ing."""
    killpg_calls = []
    monkeypatch.setattr(cc.os, "killpg", lambda *a: killpg_calls.append(a))
    killed = []
    monkeypatch.setattr(cc.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(cc, "cycle_pids", lambda: [9999])
    msg = cc.do_killtask("t-unmatched")
    assert killed == [(9999, 15)]
    assert not killpg_calls
    assert "t-unmatched" in msg
    assert "t-unmatched" in cc.load_skips()


def test_killtask_no_cycle_running_reports_nothing_to_stop(cc, monkeypatch):
    monkeypatch.setattr(cc, "cycle_pids", lambda: [])
    msg = cc.do_killtask("")
    assert "nothing to stop" in msg.lower()


# ── concurrency setting (Task 7) ──────────────────────────────────────────
# `concurrency` joins interval_seconds/max_tasks_per_day/cycle_timeout_seconds
# as a validated positive-int field in the Admin panel's Cadence card -
# _validate_config's cheap check runs first; config.py's own `validate` (the
# authoritative schema) already covers it a second time on write.

def test_concurrency_round_trips_through_do_save_config(cc):
    msg = cc.do_save_config(json.dumps({"concurrency": 3}))
    assert msg.startswith("✓ Saved"), msg
    assert cc._load_cfg()["concurrency"] == 3


@pytest.mark.parametrize("value", [0, -1, 33, 3200, True, 1.5, "3"])
def test_concurrency_rejects_out_of_range_and_rolls_back(cc, value):
    """F5: the Admin panel refuses exactly what `config.py validate` refuses
    (1..CONCURRENCY_MAX). A value saved here that the loop's own preflight
    then rejects would leave the workspace unable to start."""
    cfg_path = Path(cc.AP_HOME) / "config.yaml"
    before = cfg_path.read_text()
    msg = cc.do_save_config(json.dumps({"concurrency": value}))
    assert msg.startswith("Rejected"), msg
    assert "concurrency" in msg
    assert cfg_path.read_text() == before


def test_concurrency_accepts_the_top_of_the_range(cc):
    assert cc.do_save_config(json.dumps({"concurrency": cc.CONCURRENCY_MAX})).startswith("✓ Saved")
    assert cc._load_cfg()["concurrency"] == cc.CONCURRENCY_MAX


# ── watches editor card (Task 8) ──────────────────────────────────────────
# watches joins the mcp/gates/qa/plan/knowledge family in the Admin panel,
# but as a structured card (cc_shell.html's watchRows/watchRowHtml/
# validateWatchRows) rather than a JSON textarea. _validate_config only
# checks shape (must be a list-or-absent) - deep per-row validation (name
# slug, non-empty question, positive cadence_hours, category enum) stays
# config.py's own `validate`, which do_save_config already shells out to
# post-write, rolling the file back to its exact prior bytes on failure -
# same two-pass discipline as the mcp/concurrency cards above.

def test_validate_config_rejects_non_list_watches_shape_only(cc):
    """_validate_config's cheap pass only checks shape - it neither knows nor
    cares about name/question/cadence rules, those are config.py's."""
    assert cc._validate_config({"watches": {"not": "a list"}})
    assert cc._validate_config({"watches": [{"name": "not a slug!"}]}) == []
    assert cc._validate_config({"watches": []}) == []
    assert cc._validate_config({}) == []


def test_watches_add_two_remove_one_round_trips_through_do_save_config(cc):
    """The editor's add/remove/save cycle: two rows saved, then a second save
    with one removed - each save must survive config.py's own validate() and
    land exactly the rows sent (shallow merge means the sender's list wins
    outright, same as sources/workable_categories)."""
    first = [
        {"name": "wow-signup", "question": "What changed WoW in signup?",
         "cadence_hours": 24, "category": "funnel"},
        {"name": "activation-drop", "question": "Why did activation drop?",
         "cadence_hours": 168, "category": "insight"},
    ]
    msg = cc.do_save_config(json.dumps({"watches": first}))
    assert msg.startswith("✓ Saved"), msg
    assert cc._load_cfg()["watches"] == first

    second = [first[1]]   # "wow-signup" removed
    msg2 = cc.do_save_config(json.dumps({"watches": second}))
    assert msg2.startswith("✓ Saved"), msg2
    assert cc._load_cfg()["watches"] == second


def test_watches_empty_list_clears_prior_rows_not_omitted(cc):
    """Removing every row and saving must actually clear watches: on disk -
    the editor always sends the list (never omits the key), because omitting
    it would leave the shallow merge unable to ever clear a stale block."""
    cc.do_save_config(json.dumps({"watches": [{"name": "wow-signup", "question": "q?"}]}))
    assert cc._load_cfg()["watches"]

    msg = cc.do_save_config(json.dumps({"watches": []}))
    assert msg.startswith("✓ Saved"), msg
    assert cc._load_cfg()["watches"] == []
    assert cc.watches_state([]) == []   # present-but-empty, not absent - config.py's own reading


def test_watches_bad_row_rejected_by_config_py_and_rolled_back(cc):
    """A row that fails config.py's deep rules (bad name shape here) is
    caught by the SECOND pass, after the write - and rolled back byte-exact,
    same as the mcp http-without-url case above."""
    cfg_path = Path(cc.AP_HOME) / "config.yaml"
    before = cfg_path.read_text()
    msg = cc.do_save_config(json.dumps({"watches": [{"name": "Not A Slug!", "question": "q?"}]}))
    assert msg.startswith("Rejected")
    assert "watches[0].name" in msg
    assert cfg_path.read_text() == before


# ── publish (Task 6: dashboard publish surfaces) ───────────────────────────
# do_prd_action's `publish` (legal only from status done, second-gated on
# publish.confluence being configured) and do_artifact_action's `file-jira`
# (insight-only, gated on publish.jira, same confinement read_product_artifact
# uses) - plus the state surfaces the shell reads to decide what to render:
# prd_pipeline_state's `published` + filtered `next_actions`,
# insight_docs_state's `jira_key`.

CONFLUENCE_CFG = {"base_url": "https://confluence.example.com", "space": "PROD"}
JIRA_CFG = {"base_url": "https://jira.example.com", "project": "PROD", "issue_type": "Task"}


def _write_publish_config(cc, confluence=None, jira=None):
    import yaml
    cfg_path = Path(cc.AP_HOME) / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    pub = {}
    if confluence is not None:
        pub["confluence"] = confluence
    if jira is not None:
        pub["jira"] = jira
    cfg["publish"] = pub
    cfg_path.write_text(yaml.safe_dump(cfg))


def _done_run(cc, run="r1"):
    cc.prd_flow.new(str(cc.REPO), run, "My PRD")
    marker = cc.prd_flow.load_marker(str(cc.REPO), run)
    marker["status"] = "done"
    cc.prd_flow._write_marker(str(cc.REPO), run, marker)


def test_publish_channels_absent_by_default(cc):
    assert cc._publish_channels() == {"confluence": False, "jira": False}
    s = cc.build_state()
    assert s["publish_channels"] == {"confluence": False, "jira": False}


def test_publish_channels_true_only_when_all_required_fields_present(cc):
    _write_publish_config(cc, confluence={"base_url": "https://x"})   # missing space
    assert cc._publish_channels()["confluence"] is False
    _write_publish_config(cc, confluence=CONFLUENCE_CFG, jira=JIRA_CFG)
    assert cc._publish_channels() == {"confluence": True, "jira": True}


def test_publish_action_illegal_outside_done(cc):
    cc.prd_flow.new(str(cc.REPO), "r1", "My PRD")   # status stays "intake"
    _write_publish_config(cc, confluence=CONFLUENCE_CFG)
    msg = cc.do_prd_action("r1", "publish")
    assert "cannot" in msg.lower()
    assert "status" in msg.lower()


def test_publish_action_unconfigured_gives_clear_message_and_no_popen(cc, monkeypatch):
    _done_run(cc)
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    msg = cc.do_prd_action("r1", "publish")
    assert "publish.confluence" in msg
    assert "not configured" in msg
    assert calls == []


class _FakePopen:
    """Stand-in for the detached Popen handle do_prd_action/do_artifact_
    action now read `.pid` off of, to write the publish-pending record
    (see _write_publish_pending)."""
    pid = 424242


def test_publish_action_spawns_detached_popen_with_logging(cc, monkeypatch):
    _done_run(cc)
    _write_publish_config(cc, confluence=CONFLUENCE_CFG)
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda cmd, **kw: (calls.append((cmd, kw)), _FakePopen())[1])
    msg = cc.do_prd_action("r1", "publish")
    assert "launched" in msg.lower()
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == [cc.sys.executable, str(Path(cc.ENGINE) / "publish.py"), "prd", str(cc.REPO), "r1"]
    assert kwargs.get("start_new_session") is True
    log = Path(cc.AP_STATE) / "logs" / "publish-prd-r1.log"
    assert f"logs/{log.name}" in msg


def test_next_actions_hides_publish_button_when_confluence_unconfigured(cc):
    _done_run(cc)
    run = cc.prd_pipeline_state()[0]
    assert "publish" not in run["next_actions"]
    assert run["published"] is None


def test_next_actions_shows_publish_button_when_confluence_configured(cc):
    _done_run(cc)
    _write_publish_config(cc, confluence=CONFLUENCE_CFG)
    run = cc.prd_pipeline_state()[0]
    assert "publish" in run["next_actions"]


def test_published_url_surfaced_from_seeded_marker(cc):
    _done_run(cc)
    marker = cc.prd_flow.load_marker(str(cc.REPO), "r1")
    marker["published"] = {"confluence": {"page_id": "123",
                                          "url": "https://confluence.example.com/x",
                                          "at": "2026-08-22T00:00:00Z"}}
    cc.prd_flow._write_marker(str(cc.REPO), "r1", marker)
    run = cc.prd_pipeline_state()[0]
    assert run["published"] == {"page_id": "123", "url": "https://confluence.example.com/x",
                                "at": "2026-08-22T00:00:00Z"}
    s = cc.build_state()
    assert s["prd"][0]["published"]["url"] == "https://confluence.example.com/x"


def test_published_url_with_unsafe_scheme_passes_through_state_unfiltered(cc):
    """The server never sanitises `published.confluence.url` - it is a
    trusted-writer field (only publish.py's own _record_published sets it,
    behind the marker lock) and command_center reads it back verbatim. The
    ACTUAL defense against a tampered/hand-edited marker carrying something
    like `javascript:...` lives client-side, in cc_shell.html's
    isSafeLinkUrl gate (pinned by test_prd_publish_link_gated_on_http_scheme
    below) - this test only pins that the raw value still reaches the state
    payload unmodified, which is exactly why that client-side gate is load-
    bearing rather than defense-in-depth on top of a server-side filter."""
    _done_run(cc)
    marker = cc.prd_flow.load_marker(str(cc.REPO), "r1")
    marker["published"] = {"confluence": {"page_id": "123",
                                          "url": "javascript:alert(1)",
                                          "at": "2026-08-22T00:00:00Z"}}
    cc.prd_flow._write_marker(str(cc.REPO), "r1", marker)
    run = cc.prd_pipeline_state()[0]
    assert run["published"]["url"] == "javascript:alert(1)"


def test_prd_publish_link_gated_on_http_scheme(cc):
    """cc_shell.html's prdPublishArea must never hand `published.confluence.url`
    straight to an <a href> - escAttr stops attribute breakout (a stray '"')
    but does nothing about the SCHEME, so a marker carrying
    `url: "javascript:..."` would otherwise become a live anchor the moment
    the card renders (see the test above: the server hands that value
    through unfiltered). No JS runtime in this suite, so this is a source-
    text pin: the scheme check exists, and the render path actually calls it
    before building the anchor, with an escaped-plain-text fallback when it
    fails."""
    shell = (Path(cc.ENGINE) / "cc_shell.html").read_text()
    assert "function isSafeLinkUrl" in shell
    assert r"^https?:\/\/" in shell   # the actual check, not just the function's existence
    area = shell.split("function prdPublishArea", 1)[1].split("\nfunction ", 1)[0]
    assert "isSafeLinkUrl(url)" in area
    assert "<a href=" in area and "esc(url)" in area   # anchor branch AND the plain-text fallback


# ── /artifact-action: file-jira ─────────────────────────────────────────────

def test_artifact_action_file_jira_unconfigured_gives_message_no_popen(cc, monkeypatch):
    ir = Path(cc.REPO) / "insights"
    ir.mkdir()
    (ir / "wow-signup.md").write_text("# WoW signup\n## Answer\nit went up\n## Basis\n- S1: data\n")
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    msg = cc.do_artifact_action("insight", "wow-signup", "file-jira")
    assert "publish.jira" in msg
    assert "not configured" in msg
    assert calls == []


def test_artifact_action_file_jira_spawns_detached_popen_with_logging(cc, monkeypatch):
    ir = Path(cc.REPO) / "insights"
    ir.mkdir()
    (ir / "wow-signup.md").write_text("# WoW signup\n## Answer\nit went up\n## Basis\n- S1: data\n")
    _write_publish_config(cc, jira=JIRA_CFG)
    calls = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda cmd, **kw: (calls.append((cmd, kw)), _FakePopen())[1])
    msg = cc.do_artifact_action("insight", "wow-signup", "file-jira")
    assert "filing" in msg.lower()
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == [cc.sys.executable, str(Path(cc.ENGINE) / "publish.py"), "finding",
                   str(cc.REPO), "wow-signup"]
    assert kwargs.get("start_new_session") is True


def test_artifact_action_rejects_traversal_and_unknown_kind(cc):
    ir = Path(cc.REPO) / "insights"
    ir.mkdir()
    (ir / "real.md").write_text("# Real\n")
    _write_publish_config(cc, jira=JIRA_CFG)

    assert "invalid" in cc.do_artifact_action("insight", "../secret", "file-jira").lower()
    assert "invalid" in cc.do_artifact_action("insight", "../../etc/passwd", "file-jira").lower()
    assert "no insight doc" in cc.do_artifact_action("insight", "not-real", "file-jira").lower()
    assert "only insight docs" in cc.do_artifact_action("survey", "real", "file-jira").lower()
    assert "unknown artifact action" in cc.do_artifact_action("insight", "real", "bogus-action").lower()


def test_insight_docs_state_surfaces_jira_key_when_record_line_exists(cc):
    ir = Path(cc.REPO) / "insights"
    ir.mkdir()
    (ir / "wow-signup.md").write_text(
        "# WoW signup\n## Answer\nit went up\n## Basis\n- S1: data\n\n"
        "<!-- published: jira PROD-42 -->\n")
    (ir / "unfiled.md").write_text("# Unfiled\n")

    docs = {d["name"]: d["jira_key"] for d in cc.insight_docs_state()}
    assert docs["wow-signup"] == "PROD-42"
    assert docs["unfiled"] is None


# ── publish failure surfacing (Task/Important 1) ────────────────────────────
# do_prd_action's `publish` and do_artifact_action's `file-jira` launch
# publish.py detached and return immediately - a bad config/secret/lint/HTTP/
# lock failure used to be invisible unless someone opened logs/publish-*.log
# by hand. _write_publish_pending/_publish_reconcile close that: a small
# pending-record json next to the log lets prd_pipeline_state/insight_docs_
# state reconcile pid-alive / succeeded / failed on every poll.

def _dead_pid():
    """A pid guaranteed dead (spawned and reaped by THIS process, so
    locks.pid_alive sees ProcessLookupError) - never a guessed large number,
    which could theoretically collide with something still alive."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _write_pending(cc, log_stem, pid, launched_at):
    log = Path(cc.AP_STATE) / "logs" / f"{log_stem}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    if not log.exists():
        log.touch()
    cc._publish_pending_path(log).write_text(
        json.dumps({"pid": pid, "log": log_stem, "launched_at": launched_at}))
    return log


def test_publish_error_absent_when_no_pending_record(cc):
    _done_run(cc)
    run = cc.prd_pipeline_state()[0]
    assert run["publish_error"] is None


def test_publish_error_absent_while_the_launched_pid_is_still_alive(cc):
    """Still publishing - nothing to show yet, not an error."""
    _done_run(cc)
    _write_pending(cc, "publish-prd-r1", os.getpid(), time.time())
    run = cc.prd_pipeline_state()[0]
    assert run["publish_error"] is None


def test_publish_error_absent_and_pending_file_cleared_on_success(cc):
    """pid gone + a success record newer than the launch time = this
    attempt (or one that superseded it) succeeded - the pending file is
    reconciled away so it is never re-checked again."""
    _done_run(cc)
    dead = _dead_pid()
    log = _write_pending(cc, "publish-prd-r1", dead, time.time() - 100)
    marker = cc.prd_flow.load_marker(str(cc.REPO), "r1")
    marker["published"] = {"confluence": {"page_id": "1", "url": "https://x/1",
                                          "at": "2026-08-22T00:00:00Z"}}
    cc.prd_flow._write_marker(str(cc.REPO), "r1", marker)

    run = cc.prd_pipeline_state()[0]
    assert run["publish_error"] is None
    assert not cc._publish_pending_path(log).exists()


def test_publish_error_present_when_pid_gone_and_no_success_record(cc):
    """pid gone, no success record at all - a real failure, and the log's
    own `publish[<code>]: <msg>` line drives the short reason."""
    _done_run(cc)
    dead = _dead_pid()
    log = _write_pending(cc, "publish-prd-r1", dead, time.time())
    log.write_text("some log noise\npublish[5]: POST https://x/y failed: HTTP 503\n")

    run = cc.prd_pipeline_state()[0]
    assert run["publish_error"] == {"reason": "HTTP 503", "log": "publish-prd-r1"}
    # left in place - not silently forgotten after being shown once
    assert cc._publish_pending_path(log).exists()


@pytest.mark.parametrize("code,msg,reason", [
    (2, "no `publish:` block", "config"),
    (3, "missing ATLASSIAN_EMAIL", "secrets"),
    (4, "insight_lint failed", "unapproved/lint"),
    (5, "POST x failed: HTTP 500", "HTTP 500"),
    (6, "could not acquire the lock", "lock contention"),
])
def test_publish_error_reason_maps_every_exit_code(cc, code, msg, reason):
    _done_run(cc)
    dead = _dead_pid()
    log = _write_pending(cc, "publish-prd-r1", dead, time.time())
    log.write_text(f"publish[{code}]: {msg}\n")
    run = cc.prd_pipeline_state()[0]
    assert run["publish_error"]["reason"] == reason


def test_publish_error_surfaces_on_insight_docs_state_too(cc):
    ir = Path(cc.REPO) / "insights"
    ir.mkdir()
    (ir / "wow-signup.md").write_text("# WoW signup\n## Answer\nit went up\n## Basis\n- S1: data\n")
    dead = _dead_pid()
    log = _write_pending(cc, "publish-finding-wow-signup", dead, time.time())
    log.write_text("publish[3]: missing ATLASSIAN_API_TOKEN\n")

    docs = {d["name"]: d for d in cc.insight_docs_state()}
    assert docs["wow-signup"]["publish_error"] == {"reason": "secrets", "log": "publish-finding-wow-signup"}


def test_publish_error_cleared_on_insight_docs_state_when_filed(cc):
    ir = Path(cc.REPO) / "insights"
    ir.mkdir()
    doc = ir / "wow-signup.md"
    dead = _dead_pid()
    log = _write_pending(cc, "publish-finding-wow-signup", dead, time.time() - 100)
    doc.write_text("# WoW signup\n## Answer\nit went up\n## Basis\n- S1: data\n\n"
                   "<!-- published: jira PROD-42 -->\n")

    docs = {d["name"]: d for d in cc.insight_docs_state()}
    assert docs["wow-signup"]["publish_error"] is None
    assert not cc._publish_pending_path(log).exists()


def test_shell_renders_publish_error_on_prd_card_and_insight_row(cc):
    """Source-text pin (no JS runtime in this suite, same template as
    test_prd_publish_link_gated_on_http_scheme): publishErrorLine must exist,
    escape its reason text, and actually be called from both prdCard (the
    PRD band) and insightArtifactRow (the artifact browser) - the two places
    build_state's publish_error can appear."""
    shell = (Path(cc.ENGINE) / "cc_shell.html").read_text()
    assert "function publishErrorLine" in shell
    body = shell.split("function publishErrorLine", 1)[1].split("\nfunction ", 1)[0]
    assert "esc(err.reason" in body
    prd_card = shell.split("function prdCard", 1)[1].split("\nfunction ", 1)[0]
    assert "publishErrorLine(r.publish_error)" in prd_card
    insight_row = shell.split("function insightArtifactRow", 1)[1].split("\nfunction ", 1)[0]
    assert "publishErrorLine(d.publish_error)" in insight_row


def test_cmdlog_route_serves_publish_prd_and_finding_logs(cc):
    """The route's confinement (do_GET's `_cmdlog_name_ok`) must extend to
    the publish-{prd,finding} log stems, shape-checked the same way as the
    run id / insight stem they're built from - never widened to an arbitrary
    filesystem path."""
    log = Path(cc.AP_STATE) / "logs" / "publish-prd-r1.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("hello from publish-prd-r1\n")
    assert "hello from publish-prd-r1" in cc.read_cmdlog("publish-prd-r1")

    log2 = Path(cc.AP_STATE) / "logs" / "publish-finding-wow-signup.log"
    log2.write_text("hello from publish-finding-wow-signup\n")
    assert "hello from publish-finding-wow-signup" in cc.read_cmdlog("publish-finding-wow-signup")


@pytest.mark.parametrize("name", [
    "publish-prd-../../etc/passwd", "publish-prd-R1", "publish-prd-",
    "publish-finding-../secret", "publish-finding-", "publish-finding-a/b",
    "publish-other-x", "../../etc/passwd", "publish-prd-r1.log",
])
def test_cmdlog_route_rejects_malformed_publish_log_names(cc, name):
    # A secret file OUTSIDE logs/ that a traversal attempt would otherwise
    # reach, to prove a bad name never gets anywhere near it.
    secret = Path(cc.AP_STATE).parent / "secret.txt"
    secret.write_text("do not serve me")
    assert not cc._cmdlog_name_ok(name)
    assert cc.read_cmdlog(name) == ""


def test_first_dashboard_task_on_inline_empty_backlog_stays_valid_yaml(cc):
    """init writes an empty backlog as `tasks: []` (inline). The dashboard's
    block appender must rewrite that to block form on first append — the
    regression corrupted backlog.yaml and stalled the loop's queue refresh."""
    import yaml as _yaml
    cc.BACKLOG.write_text("# header comment\ntasks: []\n")
    msg = cc.do_task_new("first-task", "First task", "documentation",
                         acc_raw="it parses", queue_now=False)
    assert msg.startswith("Created"), msg
    doc = _yaml.safe_load(cc.BACKLOG.read_text())
    assert [t["id"] for t in doc["tasks"]] == ["first-task"]
