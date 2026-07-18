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


def build(target: Path) -> tuple[str, str, str]:
    """→ (title, body, dashboard_url)."""
    ap = target / ".autopilot"
    state = ap / "state"
    cfg = yaml.safe_load((ap / "config.yaml").read_text()) if (ap / "config.yaml").exists() else {}
    cfg = cfg or {}
    dash = cfg.get("dashboard_url") or "http://127.0.0.1:8787"

    entries = _load_json(state / "ledger.json", {}).get("entries") or {}
    ships = [(tid, e) for tid, e in entries.items() if e.get("state") == "pushed"]
    escalated = [(tid, e) for tid, e in entries.items() if e.get("state") == "escalated"]

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
    lines.append(f"📋 queue: {len(pickable)} pickable / {len(qtasks)} emitted · spend today ${spend:.2f}")
    if not ships and not escalated and not needs_info:
        lines.insert(0, "✅ no human gate is blocking the loop.")

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
