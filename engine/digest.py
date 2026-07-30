#!/usr/bin/env python3
"""digest.py — the operator's summary of every stalled human gate, in one read.

The loop's throughput is bounded by how long ships sit unreviewed and
escalations sit unanswered — this surfaces all of it: ships awaiting merge
(with age), open escalations, Jira tickets waiting on info, pickable queue
depth, today's spend. Print to stdout; `--send` fans it out via notify.py
(Slack/macOS) with the dashboard link. Schedule it (cron/launchd) for a
morning digest, or run `orbit digest <target>` ad hoc.

Usage:  digest.py <target-repo> [--send]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Sibling-module import (qa_teach.py lives next to this file under engine/) —
# the digest can be loaded via importlib from any cwd (tests do exactly
# this), so the engine dir must be put on sys.path explicitly rather than
# relying on the interpreter's script-dir default. _bad_flows_dir_reason is
# the ONE shared shape/escape check `orbit teach`/`orbit qa`/the dashboard's
# _qa_flows_root all use — imported (never re-implemented) so it can't drift.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from qa_teach import _bad_flows_dir_reason
from backlog_append import SLUG_RE   # the one slug gate every QA listing shares

DEFAULT_FLOWS_DIR = "qa/flows"        # each consumer keeps its own copy of the
                                       # literal default; only the guard itself is shared.


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _age_days(iso: str) -> str:
    try:
        then = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        d = (datetime.now(timezone.utc) - then).days
        return f"{d}d" if d else "<1d"
    except (ValueError, TypeError):
        return "?"


def _qa_pending_heals(state: Path) -> int:
    """Count of top-level $AP_STATE/qa/heal/*.json — mined heal candidates
    still awaiting the human approve/reject gate (command_center.py's
    do_qa_action heal-approve/heal-reject). heal/approved/ and heal/rejected/
    are subdirectories, never matched by this glob, so an archived candidate
    is never double-counted as pending. A missing qa/ or heal/ dir (nothing
    taught yet) yields 0, never a crash."""
    try:
        return sum(1 for p in (state / "qa" / "heal").glob("*.json") if p.is_file())
    except OSError:
        return 0


def _qa_pending_generated(state: Path) -> int:
    """Count of top-level $AP_STATE/qa/generated/<slug>/ directories — AI
    test candidates (qa_run.py --generate) still awaiting the human
    approve/reject gate (command_center.py's do_qa_action generate-approve/
    generate-reject). Mirrors command_center._qa_generated_candidates'
    listing discipline exactly: directories only (a candidate is a dir, not a
    file — the inverse of the heal listing above), the reserved `rejected/`
    archive subdir excluded, and each name SLUG_RE-gated so junk (`.DS_Store`)
    or a non-conforming dir is never counted. A missing qa/ or generated/ dir
    (nothing generated yet) yields 0, never a crash."""
    try:
        return sum(1 for p in (state / "qa" / "generated").iterdir()
                   if p.is_dir() and p.name != "rejected" and SLUG_RE.match(p.name))
    except OSError:
        return 0


def _qa_failing_flows(ap: Path, cfg: dict) -> list[str]:
    """Names (sorted) of taught flows whose meta.json `last_verdict` reads
    "fail" — read through the same guarded flows-root discipline `orbit
    teach`/`orbit qa`/the dashboard's _qa_flows_root all share: a bad
    qa.flows_dir falls back to the default rather than erroring (a digest
    run must never crash on a bad config value, same reasoning as
    _qa_flows_root's silent fallback). A missing flows dir, or any flow's
    missing/malformed/non-dict meta.json, contributes nothing — never a
    crash, never a guess."""
    qa_cfg = cfg.get("qa") if isinstance(cfg.get("qa"), dict) else {}
    flows_dir = qa_cfg.get("flows_dir")
    if _bad_flows_dir_reason(flows_dir):
        flows_dir = DEFAULT_FLOWS_DIR
    flows_root = ap / flows_dir
    if not flows_root.is_dir():
        return []
    try:
        names = sorted(p.name for p in flows_root.iterdir() if p.is_dir())
    except OSError:
        return []
    failing = []
    for name in names:
        try:
            meta = json.loads((flows_root / name / "meta.json").read_text())
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("last_verdict") == "fail":
            failing.append(name)
    return failing


def _next_action(escalated, ships, pickable, in_flight, dash: str) -> str:
    """The digest ends with ONE concrete action doable in under two minutes —
    the single highest-leverage gate, not a list. Escalations block a task
    outright, so they outrank ships; ships outrank an empty queue."""
    if escalated:
        tid, _ = min(escalated, key=lambda x: x[1].get("updated_at") or "")
        return f"→ next: answer escalation {tid} — {dash}"
    if ships:
        tid, e = min(ships, key=lambda x: x[1].get("updated_at") or "")
        return f"→ next: review & merge {tid} — {e.get('pr_url') or dash}"
    if not pickable and not in_flight:
        return f"→ next: queue a task — {dash}"
    return "→ next: nothing — the loop is unblocked."


def build(target: Path) -> tuple[str, str, str]:
    """→ (title, body, dashboard_url)."""
    ap = target / ".autopilot"
    state = ap / "state"
    cfg = yaml.safe_load((ap / "config.yaml").read_text()) if (ap / "config.yaml").exists() else {}
    cfg = cfg or {}
    dash = cfg.get("dashboard_url") or "http://127.0.0.1:8787"

    entries = _load_json(state / "ledger.json", {}).get("entries") or {}
    # Parked tasks (skips.txt, the dashboard's "Set aside") are deliberate
    # operator decisions — they must not count as "blocked on you" or be
    # offered as the next action. Ships stay regardless: they're reviewed
    # via merge/reject, not parked (same rule the dashboard applies).
    try:
        skips = {ln.strip() for ln in (state / "skips.txt").read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")}
    except OSError:
        skips = set()
    ships = [(tid, e) for tid, e in entries.items() if e.get("state") == "pushed"]
    escalated = [(tid, e) for tid, e in entries.items()
                 if e.get("state") == "escalated" and tid not in skips]
    in_flight = [(tid, e) for tid, e in entries.items()
                 if e.get("state") in ("in_progress", "committed") and tid not in skips]

    queue = _load_json(state / "queue.json", {})
    qtasks = queue.get("tasks") or []
    worked = set(entries)
    pickable = [t for t in qtasks if str(t.get("id")) not in worked]

    jira_map = _load_json(state / "jira_map.json", {})
    needs_info = [k for k, v in jira_map.items() if v.get("state") == "needs_info"]

    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    try:
        spend = float((state / f".spend-{today}").read_text().strip() or 0)
    except (OSError, ValueError):
        spend = 0.0

    lines = []
    if ships:
        lines.append(f"⏳ {len(ships)} ship(s) awaiting your review:")
        for tid, e in sorted(ships, key=lambda x: x[1].get("updated_at") or ""):
            extra = e.get("pr_url") or e.get("remote_ref") or ""
            lines.append(f"  · {tid} ({_age_days(e.get('updated_at'))}) {extra}")
    if escalated:
        lines.append(f"🙋 {len(escalated)} escalation(s) blocked on you:")
        for tid, e in sorted(escalated, key=lambda x: x[1].get("updated_at") or ""):
            lines.append(f"  · {tid} ({_age_days(e.get('updated_at'))}) — {e.get('reason') or ''}")
    if needs_info:
        lines.append(f"❓ {len(needs_info)} Jira ticket(s) waiting on info: " + ", ".join(sorted(needs_info)))
    if in_flight:
        lines.append("🔧 in flight: " + ", ".join(
            f"{tid} ({e.get('state')}, {_age_days(e.get('updated_at'))})"
            for tid, e in sorted(in_flight)))
    lines.append(f"📋 queue: {len(pickable)} pickable / {len(qtasks)} emitted · spend today ${spend:.2f}")

    # Project knowledge base staleness (written by `knowledge.py status`, kept
    # fresh by run.sh's autorefresh hook). Missing/unparseable file → no line —
    # this must never crash the digest for a repo that hasn't built one yet.
    staleness = _load_json(state / "knowledge-staleness.json", None)
    stale_docs = staleness.get("stale_docs") if isinstance(staleness, dict) else None
    if isinstance(stale_docs, list) and stale_docs:
        lines.append(f"📚 knowledge stale: {len(stale_docs)} doc(s) — orbit knowledge <target> refresh")

    # 🧪 QA feedback surfaces (Task 28): pending self-heal candidates and
    # flows whose last replay verdict was a fail. Either populates the
    # section; neither means it's entirely absent — same "no content, no
    # line" discipline as the knowledge-staleness line above.
    pending_heals = _qa_pending_heals(state)
    pending_generated = _qa_pending_generated(state)
    failing_flows = _qa_failing_flows(ap, cfg)
    if pending_heals or pending_generated or failing_flows:
        lines.append("🧪 QA:")
        if pending_heals:
            lines.append(f"  · {pending_heals} pending heal(s) — approve on the dashboard")
        if pending_generated:
            lines.append(f"  · {pending_generated} AI-generated test(s) awaiting review — "
                          f"approve on the dashboard")
        if failing_flows:
            shown = ", ".join(failing_flows[:3])
            lines.append(f"  · {len(failing_flows)} flow(s) last failed — "
                          f"orbit qa <target> --flow <name>: {shown}")

    if not ships and not escalated and not needs_info:
        lines.insert(0, "✅ no human gate is blocking the loop.")
    lines.append(_next_action(escalated, ships, pickable, in_flight, dash))

    return f"Orbit digest — {target.name}", "\n".join(lines), dash


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    target = Path(args[0]).resolve()
    if not (target / ".autopilot").is_dir():
        sys.exit(f"no .autopilot/ under {target} — run `orbit init` first")
    title, body, dash = build(target)
    print(f"{title}\n{'─' * len(title)}\n{body}")
    if "--send" in argv:
        # notify resolves its webhook from AP_HOME/AP_STATE — export them so a
        # bare `orbit digest <target> --send` finds the target's channel config.
        import os
        os.environ.setdefault("AP_HOME", str(target / ".autopilot"))
        os.environ.setdefault("AP_STATE", str(target / ".autopilot" / "state"))
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import notify
        notify.send(title, body, dash)
        print("(sent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
