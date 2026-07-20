# The digest must end with exactly ONE concrete next action (the single
# highest-leverage human gate): escalations outrank ships awaiting review,
# ships outrank an empty queue, and an unblocked loop says so explicitly.
# It must also restate what is in flight so the operator never has to
# reconstruct loop position from the ledger.
#
# Run: python3 -m pytest tests/test_digest.py -v   (from orbit/)
import importlib.util
import json
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parent.parent / "engine"
_spec = importlib.util.spec_from_file_location("digest", ENGINE / "digest.py")
digest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(digest)

DASH = "http://dash.example:8787"


def _target(tmp_path, entries=None, queue=None, skips=None):
    ap = tmp_path / ".autopilot"
    state = ap / "state"
    state.mkdir(parents=True)
    (ap / "config.yaml").write_text(yaml.safe_dump({"dashboard_url": DASH}))
    (state / "ledger.json").write_text(json.dumps({"entries": entries or {}}))
    (state / "queue.json").write_text(json.dumps({"tasks": queue or []}))
    if skips:
        (state / "skips.txt").write_text("\n".join(skips) + "\n")
    return tmp_path


def _next_line(body: str) -> str:
    lines = body.splitlines()
    assert lines[-1].startswith("→ next: "), f"digest must END with the next action, got: {lines[-1]}"
    assert sum(1 for l in lines if l.startswith("→ next: ")) == 1, "exactly one next action"
    return lines[-1]


def _build(tmp_path, entries=None, queue=None):
    return digest.build(_target(tmp_path, entries, queue))


def test_escalation_outranks_ship(tmp_path):
    entries = {
        "t-ship": {"state": "pushed", "updated_at": "2026-07-01T00:00:00Z"},
        "t-esc": {"state": "escalated", "updated_at": "2026-07-10T00:00:00Z", "reason": "needs a call"},
    }
    _, body, _ = _build(tmp_path, entries)
    nxt = _next_line(body)
    assert "t-esc" in nxt and DASH in nxt


def test_oldest_ship_named_with_pr_url(tmp_path):
    entries = {
        "t-new": {"state": "pushed", "updated_at": "2026-07-10T00:00:00Z"},
        "t-old": {"state": "pushed", "updated_at": "2026-07-01T00:00:00Z", "pr_url": "https://gh/pr/7"},
    }
    _, body, _ = _build(tmp_path, entries)
    nxt = _next_line(body)
    assert "t-old" in nxt and "https://gh/pr/7" in nxt


def test_empty_queue_says_queue_a_task(tmp_path):
    _, body, _ = _build(tmp_path)
    nxt = _next_line(body)
    assert "queue a task" in nxt and DASH in nxt


def test_unblocked_loop_says_nothing_needed(tmp_path):
    queue = [{"id": "t-1", "priority": 1}]
    _, body, _ = _build(tmp_path, entries={}, queue=queue)
    nxt = _next_line(body)
    assert "nothing" in nxt
    assert body.splitlines()[0].startswith("✅")


def test_parked_escalation_is_not_blocked_on_you(tmp_path):
    # "Set aside" (skips.txt) is a deliberate operator decision — a parked
    # escalation must not be counted, listed, or offered as the next action.
    entries = {
        "t-parked": {"state": "escalated", "updated_at": "2026-06-01T00:00:00Z", "reason": "parked"},
        "t-live": {"state": "escalated", "updated_at": "2026-07-10T00:00:00Z", "reason": "real"},
    }
    tgt = _target(tmp_path, entries, skips=["t-parked"])
    _, body, _ = digest.build(tgt)
    assert "t-parked" not in body
    nxt = _next_line(body)
    assert "t-live" in nxt


def test_in_flight_task_is_restated(tmp_path):
    entries = {"t-12": {"state": "in_progress", "updated_at": "2026-07-19T00:00:00Z"}}
    _, body, _ = _build(tmp_path, entries)
    assert any("in flight" in l and "t-12" in l and "in_progress" in l for l in body.splitlines())
    # a task in flight means the loop is working — not "queue a task"
    assert "queue a task" not in _next_line(body)
