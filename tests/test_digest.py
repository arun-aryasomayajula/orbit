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


def test_knowledge_stale_line_present_with_count(tmp_path):
    tgt = _target(tmp_path)
    (tgt / ".autopilot" / "state" / "knowledge-staleness.json").write_text(json.dumps({
        "checked_at": "2026-07-22T00:00:00Z",
        "generated_at": "2026-07-01T00:00:00Z",
        "stale_docs": ["endpoints.md", "flows/checkout.md"],
        "merges_considered": 5,
    }))
    _, body, _ = digest.build(tgt)
    assert "📚 knowledge stale: 2 doc(s) — orbit knowledge <target> refresh" in body


def test_knowledge_stale_line_absent_without_file(tmp_path):
    tgt = _target(tmp_path)
    _, body, _ = digest.build(tgt)
    assert "knowledge stale" not in body


def test_knowledge_stale_line_absent_on_malformed_json(tmp_path):
    tgt = _target(tmp_path)
    (tgt / ".autopilot" / "state" / "knowledge-staleness.json").write_text("{not valid json")
    _, body, _ = digest.build(tgt)
    assert "knowledge stale" not in body


def test_knowledge_stale_line_absent_when_stale_docs_empty(tmp_path):
    tgt = _target(tmp_path)
    (tgt / ".autopilot" / "state" / "knowledge-staleness.json").write_text(json.dumps({
        "checked_at": "2026-07-22T00:00:00Z",
        "generated_at": "2026-07-01T00:00:00Z",
        "stale_docs": [],
        "merges_considered": 0,
    }))
    _, body, _ = digest.build(tgt)
    assert "knowledge stale" not in body


# ── 🧪 QA section (Task 28) ──────────────────────────────────────────────────
# Two independent feedback surfaces: pending heal candidates (top-level
# $AP_STATE/qa/heal/*.json) and flows whose last taught-flow replay verdict
# was "fail" (guarded flows-root meta.json). Either populates the section;
# neither means it's entirely absent; malformed/missing inputs never crash
# the digest and never masquerade as real data.

def _write_flow_meta(tgt, name, last_verdict, flows_dir="qa/flows", malformed=False):
    flow_dir = tgt / ".autopilot" / flows_dir / name
    flow_dir.mkdir(parents=True, exist_ok=True)
    if malformed:
        (flow_dir / "meta.json").write_text("{not valid json")
    else:
        (flow_dir / "meta.json").write_text(json.dumps({
            "created_at": "2026-07-01T00:00:00Z", "last_run_at": "2026-07-20T00:00:00Z",
            "last_verdict": last_verdict, "runs": 3, "heals": [],
        }))


def _write_heal_files(tgt, n, subdir=None):
    heal_dir = tgt / ".autopilot" / "state" / "qa" / "heal"
    (heal_dir / subdir if subdir else heal_dir).mkdir(parents=True, exist_ok=True)
    for i in range(n):
        d = (heal_dir / subdir) if subdir else heal_dir
        (d / f"cand-{i}.json").write_text(json.dumps({"flow": f"f{i}", "reason": "diff"}))


def test_qa_section_absent_with_no_heals_or_failing_flows(tmp_path):
    tgt = _target(tmp_path)
    _, body, _ = digest.build(tgt)
    assert "🧪" not in body


def test_qa_pending_heals_line_with_count(tmp_path):
    tgt = _target(tmp_path)
    _write_heal_files(tgt, 2)
    _, body, _ = digest.build(tgt)
    assert any("🧪" in l for l in body.splitlines())
    assert any("2 pending heal" in l and "dashboard" in l for l in body.splitlines())


def test_qa_heal_approved_rejected_subdirs_not_counted(tmp_path):
    # heal/approved/ and heal/rejected/ are subdirectories of top-level
    # heal/ — an archived candidate must never be double-counted as pending.
    tgt = _target(tmp_path)
    _write_heal_files(tgt, 1)
    _write_heal_files(tgt, 3, subdir="approved")
    _, body, _ = digest.build(tgt)
    assert any("1 pending heal" in l for l in body.splitlines())


def test_qa_failing_flows_line_with_names_capped_at_three(tmp_path):
    tgt = _target(tmp_path)
    for name in ("login", "checkout", "signup", "search"):
        _write_flow_meta(tgt, name, "fail")
    _write_flow_meta(tgt, "profile", "pass")
    _, body, _ = digest.build(tgt)
    qa_lines = [l for l in body.splitlines() if "last failed" in l]
    assert len(qa_lines) == 1
    line = qa_lines[0]
    assert "4 flow(s) last failed" in line
    assert "orbit qa" in line and "--flow <name>" in line
    shown = sum(1 for name in ("login", "checkout", "signup", "search") if name in line)
    assert shown == 3          # capped at 3 names even though 4 flows failed
    assert "profile" not in line


def test_qa_malformed_flow_meta_skipped_silently(tmp_path):
    tgt = _target(tmp_path)
    _write_flow_meta(tgt, "broken", "fail", malformed=True)
    _write_flow_meta(tgt, "login", "fail")
    _, body, _ = digest.build(tgt)
    qa_lines = [l for l in body.splitlines() if "last failed" in l]
    assert len(qa_lines) == 1
    assert "1 flow(s) last failed" in qa_lines[0]
    assert "login" in qa_lines[0] and "broken" not in qa_lines[0]


def _write_generated_candidates(tgt, names, subdir=None):
    root = tgt / ".autopilot" / "state" / "qa" / "generated"
    base = (root / subdir) if subdir else root
    for name in names:
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps({"generated": True, "runs": 0}))


def test_qa_pending_generated_line_with_count(tmp_path):
    tgt = _target(tmp_path)
    _write_generated_candidates(tgt, ["checkout-flow", "signup-flow"])
    _, body, _ = digest.build(tgt)
    assert any("🧪" in l for l in body.splitlines())
    assert any("2 AI-generated test(s) awaiting review" in l and "dashboard" in l
               for l in body.splitlines())


def test_qa_generated_rejected_subdir_and_non_slug_dirs_not_counted(tmp_path):
    # generated/rejected/ holds ARCHIVED candidates (do_qa_action's
    # generate-reject) and a non-slug dir is unapprovable junk — neither is a
    # pending human gate, so neither is counted (mirrors _qa_generated_candidates).
    tgt = _target(tmp_path)
    _write_generated_candidates(tgt, ["real-candidate"])
    _write_generated_candidates(tgt, ["was-rejected"], subdir="rejected")
    _write_generated_candidates(tgt, ["UPPER-CASE"])   # non-slug → ignored
    _, body, _ = digest.build(tgt)
    assert any("1 AI-generated test(s) awaiting review" in l for l in body.splitlines())


def test_qa_pending_generated_joins_heals_under_one_section(tmp_path):
    tgt = _target(tmp_path)
    _write_heal_files(tgt, 1)
    _write_generated_candidates(tgt, ["checkout-flow"])
    _, body, _ = digest.build(tgt)
    assert sum(1 for l in body.splitlines() if l.strip() == "🧪 QA:") == 1
    assert any("1 pending heal" in l for l in body.splitlines())
    assert any("1 AI-generated test(s) awaiting review" in l for l in body.splitlines())


def test_qa_respects_custom_flows_dir(tmp_path):
    ap = tmp_path / ".autopilot"
    state = ap / "state"
    state.mkdir(parents=True)
    (ap / "config.yaml").write_text(yaml.safe_dump({"dashboard_url": DASH, "qa": {"flows_dir": "custom/flows"}}))
    (state / "ledger.json").write_text(json.dumps({"entries": {}}))
    (state / "queue.json").write_text(json.dumps({"tasks": []}))
    _write_flow_meta(tmp_path, "login", "fail", flows_dir="custom/flows")
    _, body, _ = digest.build(tmp_path)
    assert any("1 flow(s) last failed" in l and "login" in l for l in body.splitlines())


def test_qa_bad_flows_dir_falls_back_without_crash(tmp_path):
    ap = tmp_path / ".autopilot"
    state = ap / "state"
    state.mkdir(parents=True)
    (ap / "config.yaml").write_text(yaml.safe_dump({"dashboard_url": DASH, "qa": {"flows_dir": "/etc/passwd"}}))
    (state / "ledger.json").write_text(json.dumps({"entries": {}}))
    (state / "queue.json").write_text(json.dumps({"tasks": []}))
    # should not crash, and should fall back to the default "qa/flows" root
    _write_flow_meta(tmp_path, "login", "fail", flows_dir="qa/flows")
    _, body, _ = digest.build(tmp_path)
    assert any("1 flow(s) last failed" in l and "login" in l for l in body.splitlines())


def test_qa_missing_flows_and_heal_dirs_no_crash(tmp_path):
    tgt = _target(tmp_path)
    _, body, _ = digest.build(tgt)
    assert "🧪" not in body
    assert "next:" in body.splitlines()[-1]
