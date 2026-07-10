# Regression test for the launch-pad ▲/▼ move buttons: do_move must reorder
# tasks relative to their ON-SCREEN neighbor in the Next-up column, not their
# raw file neighbor in backlog.yaml (which is usually an invisible
# done/proposed block — the old behavior made the buttons look dead).
#
# Run: python3 -m pytest tests/test_command_center_move.py -v   (from orbit/)

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"


def _block(tid, status, priority, autopilot="allow", category="bug"):
    return (f"- id: {tid}\n"
            f"  title: '{tid}'\n"
            f"  category: {category}\n"
            f"  autopilot: {autopilot}\n"
            f"  status: {status}\n"
            f"  priority: {priority}\n")


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    """Import command_center against a temp AP_HOME with a backlog where the
    two queued medium-priority tasks are separated by blocks that never reach
    the Next-up column (done / proposed / human-gated)."""
    ap_home = tmp_path / ".autopilot"
    ap_state = ap_home / "state"
    ap_state.mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(ap_state))

    backlog = ("version: 1\ntasks:\n" + "".join(
        _block("t-first", "queued", "medium"),
    ) + "".join([
        _block("t-done", "done", "high"),
        _block("t-proposed", "proposed", "high"),
        _block("t-human", "queued", "medium", autopilot="human"),
        _block("t-second", "queued", "medium"),
    ]))
    (ap_home / "backlog.yaml").write_text(backlog)

    # queue.json mirrors what the converter emits: queued+allow+emittable only,
    # in backlog order.
    queue = {"version": 2, "tasks": [
        {"id": "t-first", "category": "bug", "priority": "medium", "autopilot": "allow"},
        {"id": "t-second", "category": "bug", "priority": "medium", "autopilot": "allow"},
    ]}
    (ap_state / "queue.json").write_text(json.dumps(queue))

    sys.path.insert(0, str(ENGINE))
    for m in list(sys.modules):
        if m == "command_center":
            del sys.modules[m]
    mod = importlib.import_module("command_center")
    importlib.reload(mod)
    # Keep the test hermetic: don't shell out to the real converter.
    monkeypatch.setattr(mod, "_run_converter", lambda: None)
    yield mod
    sys.path.remove(str(ENGINE))


def _next_up_ids(mod):
    return [tid for tid, _ in mod._next_up_order()]


def test_move_up_reorders_past_hidden_blocks(cc):
    # On screen, t-second sits directly below t-first — but in the file they
    # are separated by 3 hidden blocks. One ▲ click must swap the DISPLAY
    # order, not shuffle t-second past one invisible block.
    assert _next_up_ids(cc) == ["t-first", "t-second"]
    msg = cc.do_move("t-second", "up")
    assert "Moved t-second up" in msg
    _, _, ids = cc._read_blocks()
    assert ids.index("t-second") < ids.index("t-first")


def test_move_down_reorders_past_hidden_blocks(cc):
    msg = cc.do_move("t-first", "down")
    assert "Moved t-first down" in msg
    _, _, ids = cc._read_blocks()
    assert ids.index("t-first") > ids.index("t-second")


def test_move_at_edge_reports_top(cc):
    msg = cc.do_move("t-first", "up")
    assert "already at the top" in msg


def test_move_across_priority_band_is_refused_with_hint(cc, tmp_path):
    # A high-priority card always displays above a medium one; reordering can
    # never cross that, so the button must say to use ⬆ pri instead of
    # silently doing nothing.
    ap_state = tmp_path / ".autopilot" / "state"
    queue = json.loads((ap_state / "queue.json").read_text())
    queue["tasks"].insert(0, {"id": "t-hi", "category": "bug",
                              "priority": "high", "autopilot": "allow"})
    (ap_state / "queue.json").write_text(json.dumps(queue))
    backlog_path = tmp_path / ".autopilot" / "backlog.yaml"
    backlog_path.write_text(backlog_path.read_text() +
                            "- id: t-hi\n  title: 't-hi'\n  category: bug\n"
                            "  autopilot: allow\n  status: queued\n  priority: high\n")
    msg = cc.do_move("t-first", "up")
    assert "priority band" in msg and "pri" in msg
    _, _, ids = cc._read_blocks()
    assert ids.index("t-first") < ids.index("t-second")  # unchanged


def test_done_human_task_leaves_the_board(cc):
    # A human-gated task marked status:done in the backlog was shipped OUTSIDE
    # the loop, so it never enters the ledger's `worked` set — the board filter
    # itself must drop it, or the card haunts Deep space forever.
    assert cc._on_board({"autopilot": "human", "status": "proposed"})
    assert cc._on_board({"autopilot": "human", "status": "queued"})
    assert cc._on_board({"autopilot": "review-only", "status": "queued"})
    assert cc._on_board({"autopilot": "allow", "status": "proposed"})
    assert not cc._on_board({"autopilot": "human", "status": "done"})
    assert not cc._on_board({"autopilot": "review-only", "status": "done"})
    assert not cc._on_board({"autopilot": "allow", "status": "queued"})


def test_fallback_swap_when_queue_missing(cc, tmp_path):
    # With no queue.json the display order is unknowable — keep the old
    # adjacent-block swap so the button still does something sensible.
    (tmp_path / ".autopilot" / "state" / "queue.json").unlink()
    msg = cc.do_move("t-second", "up")
    assert "Moved t-second up" in msg
    _, _, ids = cc._read_blocks()
    # old behavior: swapped with its immediate file neighbor (t-human)
    assert ids.index("t-second") == 3
