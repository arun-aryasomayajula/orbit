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
import sys
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
    msg = cc.do_save_secrets({"set": {"METAQL_TOKEN": "sk-abc123"}})
    assert msg == "Saved 1 secret(s)"
    f = _secrets_file(cc)
    assert f.exists()
    assert f.read_text() == "METAQL_TOKEN=sk-abc123\n"
    mode = stat.S_IMODE(f.stat().st_mode)
    assert mode == 0o600


def test_get_masks_values(cc):
    cc.do_save_secrets({"set": {"METAQL_TOKEN": "sk-abc123", "SHORT": "ab"}})
    payload = cc.secrets_payload()
    dumped = json.dumps(payload)
    assert "sk-abc123" not in dumped
    by_name = {s["name"]: s["preview"] for s in payload["secrets"]}
    assert by_name["METAQL_TOKEN"] == "sk...23"
    assert by_name["SHORT"] == "set"   # len < 6 -> generic "set", never the value


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
    assert msg == "Saved 1 secret(s)"
    assert _secrets_file(cc).read_text() == f"AUTH_TOKEN={jwt}\n"


def test_delete_removes_key_and_preserves_others(cc):
    cc.do_save_secrets({"set": {"KEEP_ME": "keepvalue", "DROP_ME": "dropvalue"}})
    msg = cc.do_save_secrets({"delete": ["DROP_ME"]})
    assert msg == "Saved 1 secret(s)"
    content = _secrets_file(cc).read_text()
    assert "DROP_ME" not in content
    assert "KEEP_ME=keepvalue" in content


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


def test_metaql_token_still_accepted_alongside_reserved_name_guard(cc):
    msg = cc.do_save_secrets({"set": {"METAQL_TOKEN": "sk-abc123"}})
    assert msg == "Saved 1 secret(s)"
    assert _secrets_file(cc).read_text() == "METAQL_TOKEN=sk-abc123\n"


# ── mcp config card (Task 8) ──────────────────────────────────────────────
# mcp joins the gates/qa JSON-textarea family in the Admin panel:
# _validate_config only checks shape (must be a dict) — deep validation
# (transport, url/command, tools) stays in config.py's own `validate`, which
# do_save_config already shells out to post-write, rolling the file back to
# its exact prior bytes on failure.

def test_do_save_config_accepts_valid_mcp_block(cc):
    payload = {"mcp": {"metaql": {"transport": "http", "url": "http://localhost:9999", "tools": "*"}}}
    msg = cc.do_save_config(json.dumps(payload))
    assert msg.startswith("✓ Saved")
    saved = cc._load_cfg()
    assert saved["mcp"] == payload["mcp"]


def test_do_save_config_rejects_http_mcp_server_without_url_and_rolls_back(cc):
    cfg_path = Path(cc.AP_HOME) / "config.yaml"
    before = cfg_path.read_text()
    payload = {"mcp": {"metaql": {"transport": "http", "tools": "*"}}}
    msg = cc.do_save_config(json.dumps(payload))
    assert msg.startswith("Rejected")
    assert "mcp.metaql.url is required for http transport" in msg
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
