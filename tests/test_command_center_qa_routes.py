# QA evidence on the dashboard (Task 17): confined GET routes serving
# screenshots/reports from $AP_STATE/qa/, and build_state() surfacing ledger
# `attachments`/`qa` on escalated + done rows.
#
# Test level: this repo's command_center tests exercise the module functions
# a route handler calls directly (do_move/do_task_new/do_mark in
# test_command_center_move.py / test_task_crud.py), not a live HTTP socket —
# there is no existing precedent anywhere in this suite for instantiating
# BaseHTTPRequestHandler without a real connection. We follow that convention:
# _qa_path_ok is the exact 400-vs-not gate do_GET runs on the raw query value,
# and read_qa_shot/read_qa_report are the exact functions do_GET calls for the
# 200/404 path — together they cover every status code the route can produce
# without needing a socket.
#
# Run: python3.11 -m pytest tests/test_command_center_qa_routes.py -v   (from orbit/)
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, ENGINE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "backlog.yaml").write_text("tasks:\n")
    monkeypatch.setenv("AP_HOME", str(ap))
    monkeypatch.setenv("AP_STATE", str(ap / "state"))
    return _mod("command_center")


@pytest.fixture()
def qa_dir(cc):
    d = cc.QA_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── _qa_path_ok — the 400 gate on the raw query value ─────────────────────────

def test_qa_path_ok_accepts_a_plain_relative_path(cc):
    assert cc._qa_path_ok("task-1/shot.png") is True


def test_qa_path_ok_rejects_empty(cc):
    assert cc._qa_path_ok("") is False


def test_qa_path_ok_rejects_dotdot_traversal(cc):
    assert cc._qa_path_ok("../secret.png") is False
    assert cc._qa_path_ok("task-1/../../secret.png") is False


def test_qa_path_ok_rejects_absolute_path(cc):
    assert cc._qa_path_ok("/etc/passwd") is False


def test_qa_path_ok_rejects_disallowed_characters(cc):
    assert cc._qa_path_ok("has spaces.png") is False
    assert cc._qa_path_ok("shot;rm -rf.png") is False


# ── /qa-shot — read_qa_shot ────────────────────────────────────────────────────

def test_qa_shot_serves_valid_png(cc, qa_dir):
    (qa_dir / "task-1").mkdir()
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
    (qa_dir / "task-1" / "01-login.png").write_bytes(data)
    result = cc.read_qa_shot("task-1/01-login.png")
    assert result is not None
    body, ctype = result
    assert body == data
    assert ctype == "image/png"


def test_qa_shot_content_type_per_extension(cc, qa_dir):
    (qa_dir / "a.jpg").write_bytes(b"x")
    (qa_dir / "b.jpeg").write_bytes(b"x")
    (qa_dir / "c.webp").write_bytes(b"x")
    assert cc.read_qa_shot("a.jpg")[1] == "image/jpeg"
    assert cc.read_qa_shot("b.jpeg")[1] == "image/jpeg"
    assert cc.read_qa_shot("c.webp")[1] == "image/webp"


def test_qa_shot_rejects_disallowed_extension(cc, qa_dir):
    (qa_dir / "note.txt").write_text("not an image")
    assert cc.read_qa_shot("note.txt") is None


def test_qa_shot_rejects_missing_file(cc, qa_dir):
    assert cc.read_qa_shot("ghost.png") is None


def test_qa_shot_rejects_oversize_file(cc, qa_dir):
    big = qa_dir / "huge.png"
    big.write_bytes(b"0" * (5 * 1024 * 1024 + 1))
    assert cc.read_qa_shot("huge.png") is None


def test_qa_shot_rejects_symlink_escape(cc, qa_dir, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside-data")
    link = qa_dir / "escape.png"
    link.symlink_to(outside)
    assert cc.read_qa_shot("escape.png") is None


# ── /qa-report — read_qa_report ────────────────────────────────────────────────

def test_qa_report_serves_valid_markdown(cc, qa_dir):
    (qa_dir / "task-1").mkdir()
    (qa_dir / "task-1" / "findings.md").write_text("# QA findings\nAll good.")
    assert cc.read_qa_report("task-1/findings.md") == "# QA findings\nAll good."


def test_qa_report_accepts_txt_log_json(cc, qa_dir):
    (qa_dir / "a.txt").write_text("t")
    (qa_dir / "b.log").write_text("l")
    (qa_dir / "c.json").write_text("{}")
    assert cc.read_qa_report("a.txt") == "t"
    assert cc.read_qa_report("b.log") == "l"
    assert cc.read_qa_report("c.json") == "{}"


def test_qa_report_rejects_disallowed_extension(cc, qa_dir):
    (qa_dir / "shot.png").write_bytes(b"\x89PNG")
    assert cc.read_qa_report("shot.png") is None


def test_qa_report_rejects_missing_file(cc, qa_dir):
    assert cc.read_qa_report("ghost.md") is None


def test_qa_report_rejects_oversize_file(cc, qa_dir):
    big = qa_dir / "huge.log"
    big.write_bytes(b"0" * (5 * 1024 * 1024 + 1))
    assert cc.read_qa_report("huge.log") is None


def test_qa_report_rejects_symlink_escape(cc, qa_dir, tmp_path):
    outside = tmp_path / "outside.log"
    outside.write_text("secret log data")
    link = qa_dir / "escape.log"
    link.symlink_to(outside)
    assert cc.read_qa_report("escape.log") is None


# ── build_state — attachments/qa surfaced on escalated + done rows ────────────

def test_build_state_escalated_row_carries_attachments_and_qa(cc):
    ledger = {"entries": {
        "t-esc": {
            "state": "escalated",
            "attachments": [{"kind": "screenshot", "path": "task-t-esc/01.png",
                             "label": "login page", "at": "2026-07-24T00:00:00Z"}],
            "qa": {"verdict": "fail", "summary": "login is broken", "at": "2026-07-24T00:00:00Z"},
        },
        "t-esc-plain": {"state": "escalated"},
    }}
    (Path(os.environ["AP_STATE"]) / "ledger.json").write_text(json.dumps(ledger))
    st = cc.build_state()
    rows = {r["id"]: r for r in st["escalated"]}
    assert rows["t-esc"]["attachments"] == ledger["entries"]["t-esc"]["attachments"]
    assert rows["t-esc"]["qa"] == ledger["entries"]["t-esc"]["qa"]
    assert "attachments" not in rows["t-esc-plain"]
    assert "qa" not in rows["t-esc-plain"]


def test_build_state_done_row_carries_attachments_and_qa(cc):
    ledger = {"entries": {
        "t-done": {
            "state": "pushed", "sha": "abc123",
            "attachments": [{"kind": "report", "path": "task-t-done/findings.md",
                             "label": "", "at": "2026-07-24T00:00:00Z"}],
            "qa": {"verdict": "pass", "summary": "", "at": "2026-07-24T00:00:00Z"},
        },
        "t-done-plain": {"state": "pushed", "sha": "def456"},
    }}
    (Path(os.environ["AP_STATE"]) / "ledger.json").write_text(json.dumps(ledger))
    st = cc.build_state()
    rows = {r["id"]: r for r in st["done"]}
    assert rows["t-done"]["attachments"] == ledger["entries"]["t-done"]["attachments"]
    assert rows["t-done"]["qa"] == ledger["entries"]["t-done"]["qa"]
    assert "attachments" not in rows["t-done-plain"]
    assert "qa" not in rows["t-done-plain"]
