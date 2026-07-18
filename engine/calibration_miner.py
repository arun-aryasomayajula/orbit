#!/usr/bin/env python3
"""calibration_miner.py — turn the loop's own outcomes into golden-dataset candidates.

The goldens system already DELIVERS lessons (orbit-cycle §2.6 briefs calibration
cards into the maker/checker/verifier); this is its INTAKE side. It mines what
the loop and its human already recorded — no new agent chatter:

  agents (automatic)   ledger states per task (pushed/escalated/…)
  the human            merge vs amended-merge (git ancestry of the ship sha),
                       reject reasons, revert reasons (both required by the
                       dashboard), answered escalations (operator_answer)

…and emits CANDIDATES, never briefed lessons:

  $AP_HOME/goldens/candidates/<task>-<kind>.json
    kind: positive           merged untouched → match this shape
          amended            operator amended before merging → find the gap
          rejected|reverted  the human's reason, verbatim → avoid this pattern
          escalation-memory  answered escalation → don't re-escalate this class

Each candidate carries a proposed `card_line` citing its ledger evidence. A
human approves/rejects on the dashboard's Calibration strip: approve appends
the line to `$AP_HOME/goldens/LEARNED.md` (repo-local lessons, briefed by the
cycle ALONGSIDE the engine's calibration — see orbit-cycle §2.6); reject
archives it with a reason. Until approval, nothing changes what agents see.
The engine's seed corpus (`$ORBIT_HOME/goldens/`) is never touched.

Idempotent: a task+kind already in candidates/, approved/ or rejected/ is
never re-emitted.

Usage:  calibration_miner.py <target-repo> [--dry-run]
        calibration_miner.py <target-repo> --report     # per-category outcome rates
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

KINDS = ("positive", "amended", "rejected", "reverted", "escalation-memory")


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _backlog_tasks(ap: Path) -> dict[str, dict]:
    try:
        tasks = (yaml.safe_load((ap / "backlog.yaml").read_text()) or {}).get("tasks") or []
        return {str(t.get("id")): t for t in tasks if t.get("id")}
    except OSError:
        return {}


def merged_untouched(repo: Path, sha: str, base: str) -> bool | None:
    """Is the ship commit an ancestor of base (merged as-is)? None = can't tell."""
    if not sha:
        return None
    for ref in (f"origin/{base}", base):
        ok = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", ref],
                            capture_output=True)
        if ok.returncode != 0:
            continue
        r = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, ref],
                           capture_output=True)
        return r.returncode == 0
    return None


def card_line(kind: str, tid: str, t: dict, e: dict, base: str) -> str:
    cat = t.get("category") or "?"
    if kind == "positive":
        where = e.get("branch") or e.get("remote_ref") or e.get("sha", "")[:10]
        return (f"STRONG (task {tid}, {cat}): merged untouched — match this ship's shape "
                f"(see {where}" + (f", patch {e['patch']}" if e.get("patch") else "") + ").")
    if kind == "amended":
        return (f"REVIEW-DELTA (task {tid}, {cat}): the operator amended this ship before "
                f"merging — compare `git diff {e.get('sha', '')[:10]}..origin/{base}` on its "
                f"files and close that gap unprompted next time.")
    if kind == "rejected":
        return (f"FLAW (task {tid}, {cat}): REJECTED — {e.get('review_note') or '(no reason)'}"
                f" — avoid this pattern.")
    if kind == "reverted":
        return (f"FLAW (task {tid}, {cat}): merged then REVERTED — "
                f"{e.get('revert_note') or '(no reason)'} — this class of change must be "
                f"observed in the product before shipping.")
    # escalation-memory
    q = (e.get("reason") or "(escalation reason not recorded)").strip()
    a = (t.get("operator_answer") or "").strip()
    return (f"ESCALATION MEMORY (task {tid}, {cat}): Q: {q} → A: {a} — treat the answer "
            f"as standing policy; don't re-escalate this class.")


def evidence(kind: str, tid: str, e: dict) -> list[str]:
    ev = [f"ledger entry {tid}: state={e.get('state')}"]
    for k in ("sha", "branch", "pr_url", "patch", "review_note", "revert_note",
              "reason", "reviewed_at", "reverted_at"):
        if e.get(k):
            ev.append(f"{k}: {e[k]}")
    return ev


def mine(target: Path, dry: bool = False) -> list[dict]:
    ap = target / ".autopilot"
    cfg = yaml.safe_load((ap / "config.yaml").read_text()) if (ap / "config.yaml").exists() else {}
    base = (cfg or {}).get("base_branch") or "main"
    tasks = _backlog_tasks(ap)
    entries = _load_json(ap / "state" / "ledger.json", {}).get("entries") or {}

    gold = ap / "goldens"
    cand_dir = gold / "candidates"
    have = {p.stem for d in ("candidates", "approved", "rejected")
            for p in (gold / d).glob("*.json")} if gold.exists() else set()

    found = []

    def emit(tid: str, kind: str, e: dict):
        cid = f"{tid}-{kind}"
        if cid in have:
            return
        t = tasks.get(tid, {})
        found.append({
            "id": cid, "kind": kind, "task_id": tid,
            "category": t.get("category") or "", "title": t.get("title") or e.get("title") or tid,
            "card_line": card_line(kind, tid, t, e, base),
            "evidence": evidence(kind, tid, e),
            "mined_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    for tid, e in entries.items():
        st = e.get("state")
        if e.get("reverted_at"):
            emit(tid, "reverted", e)
        elif st == "merged":
            untouched = merged_untouched(target, e.get("sha") or "", base)
            if untouched is True:
                emit(tid, "positive", e)
            elif untouched is False:
                emit(tid, "amended", e)
            # None → can't verify ancestry here; no candidate beats a wrong one
        elif st == "rejected":
            emit(tid, "rejected", e)

    for tid, t in tasks.items():
        if (t.get("operator_answer") or "").strip():
            e = entries.get(tid, {})
            if f"{tid}-escalation-memory" not in have:
                emit(tid, "escalation-memory", e)

    if not dry:
        cand_dir.mkdir(parents=True, exist_ok=True)
        for c in found:
            (cand_dir / f"{c['id']}.json").write_text(json.dumps(c, indent=2) + "\n")
    return found


def report(target: Path) -> str:
    """Per-category outcome tallies — the curves the learning loop must bend."""
    ap = target / ".autopilot"
    tasks = _backlog_tasks(ap)
    entries = _load_json(ap / "state" / "ledger.json", {}).get("entries") or {}
    rows: dict[str, dict] = {}
    for tid, e in entries.items():
        cat = tasks.get(tid, {}).get("category") or "(unknown)"
        r = rows.setdefault(cat, {"worked": 0, "merged": 0, "rejected": 0,
                                  "reverted": 0, "escalated": 0})
        r["worked"] += 1
        st = e.get("state")
        if st in r:
            r[st] += 1
        if e.get("reverted_at"):
            r["reverted"] += 1
    lines = [f"{'category':22} {'worked':>7} {'merged':>7} {'rejected':>9} {'reverted':>9} {'escalated':>10}"]
    for cat in sorted(rows):
        r = rows[cat]
        lines.append(f"{cat:22} {r['worked']:>7} {r['merged']:>7} {r['rejected']:>9} "
                     f"{r['reverted']:>9} {r['escalated']:>10}")
    if not rows:
        lines.append("(ledger empty — nothing worked yet)")
    lines.append("\nthe learning loop succeeds when rejected/reverted/escalated rates "
                 "FALL after LEARNED.md cards land — a card that doesn't bend its "
                 "curve is a candidate for removal.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    target = Path(args[0]).resolve()
    if not (target / ".autopilot").is_dir():
        sys.exit(f"no .autopilot/ under {target} — run `orbit init` first")
    if "--report" in argv:
        print(report(target))
        return 0
    found = mine(target, dry="--dry-run" in argv)
    print(f"calibration miner: {len(found)} new candidate(s)")
    for c in found:
        print(f"  + {c['id']}: {c['card_line'][:100]}")
    if found and "--dry-run" not in argv:
        print(f"\nreview them on the dashboard's Calibration strip "
              f"(approve → goldens/LEARNED.md, briefed from the next cycle).")
    if "--dry-run" in argv:
        print("(--dry-run: nothing written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
