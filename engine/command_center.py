#!/usr/bin/env python3
"""command_center.py — live dashboard + control panel for the autopilot loop.

Zero external deps (stdlib http.server). Serves a small single-page console that
polls /api/state every 3s and ticks timers client-side every 1s, so it stays
live WITHOUT disrupting the loop (the launchd daemon runs in parallel; this only
reads state + edits backlog.yaml/skips/STOP, which the loop re-reads each cycle).

Run:    python3 $ORBIT_HOME/engine/command_center.py    # http://127.0.0.1:8787
        PORT=9000 python3 $ORBIT_HOME/engine/command_center.py
        (AP_HOME / AP_STATE must be set — see config.py shellenv.)

Controls (the loop honors these on its next cycle):
  Stop loop   → writes STOP (kill switch); Resume → removes it.
  Stop task   → kills the IN-FLIGHT cycle process + skips that id (abort & don't retry).
  Skip id     → appends to skips.txt so the converter drops it from the queue.
  Promote     → flip a proposed task to queued (loop-pickable if autopilot:allow + safe).
  ▲/▼ Priority → bump a task's priority (the picker takes highest-priority first).
  ▲/▼ Move    → reorder a task's block in backlog.yaml (ties broken by order).
  Rollback    → git revert the task's pushed commit on the loop branch (+ push, never --force).
All edits are block-level text edits on backlog.yaml (comments preserved) followed
by re-running backlog_to_tasks.py so the loop queue reflects them immediately.
"""
from __future__ import annotations
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from backlog_lint import lint_task

# ── path model ───────────────────────────────────────────────────────────────
# ENGINE       : this repo's engine dir (ships command_center.py, its converter,
#                and the SPA shell). ORBIT_HOME falls back to two dirs up.
# AP_HOME      : <target-repo>/.autopilot — config.yaml, backlog.yaml, router.yaml, tracks/.
# AP_STATE     : <target-repo>/.autopilot/state — ALL runtime state.
# REPO         : the target repo working tree (where the loop's git ops run).
ENGINE = Path(__file__).resolve().parent
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or ENGINE.parent)


def _ap_home() -> Path:
    # AP_HOME must be set (or derivable) — the dashboard reads/edits its config there.
    home = os.environ.get("AP_HOME")
    if not home:
        raise SystemExit("AP_HOME unset — run inside a orbit target repo (set AP_HOME=<repo>/.autopilot)")
    return Path(home)


AP_HOME = _ap_home()
AP_STATE = Path(os.environ.get("AP_STATE") or AP_HOME / "state")
REPO = Path(os.environ.get("ORBIT_REPO") or AP_HOME.parent)

BACKLOG = AP_HOME / "backlog.yaml"
QUEUE = AP_STATE / "queue.json"
LEDGER = AP_STATE / "ledger.json"
STATE = AP_STATE / "STATE.md"
STOP = AP_STATE / "STOP"
SKIPS = AP_STATE / "skips.txt"
MARKER = AP_STATE / ".current-task-id"
FEATURE_BUILDS = AP_STATE / "feature_builds.json"   # dedicated feature-agent builds (outside the loop)
AUTO_PROMOTE = AP_STATE / "AUTO_PROMOTE"   # presence = auto-feed ON (wrapper reads it)
LOGDIR = AP_STATE / "logs"
APLOG = LOGDIR / "autopilot.log"
CONVERTER = ENGINE / "backlog_to_tasks.py"
BASE_BRANCH = os.environ.get("ORBIT_BASE_BRANCH") or os.environ.get("AP_BASE_BRANCH", "main")
PORT = int(os.environ.get("PORT", "8787"))
SHELL_FILE = ENGINE / "cc_shell.html"         # the SPA shell — read per request so design edits go live on refresh
REVIEWS = AP_STATE / "reviews"                # per-ship review packets (review_packet.py)
DIFFDIR = AP_STATE / "diffs"                  # wrapper backup patches
# launchd job label (used only to detect whether the daemon is loaded).
LAUNCHD_LABEL = os.environ.get("ORBIT_LAUNCHD_LABEL", "com.orbit.autopilot")
# Dollar spend is informational only — displayed, never a cap (the wrapper's
# only hard daily limit is the task count).
# Bitbucket "org/repo" for the prefilled PR-create URL; empty → URL omitted.
BB_REPO = os.environ.get("ORBIT_BB_REPO", "")
BB_PR_NEW = f"https://bitbucket.org/{BB_REPO}/pull-requests/new" if BB_REPO else ""

# CSRF / DNS-rebinding defense for the state-changing control plane:
#  - Host must be exactly localhost:<PORT> (a rebound DNS name won't match).
#  - cross-origin POSTs are rejected (Origin/Referer must be same-origin).
#  - POSTs require a per-process token (in-memory, embedded in the SPA), so a
#    malicious page can't forge a request (it can't read the token cross-origin).
_TOKEN = secrets.token_urlsafe(18)
_ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}
_TID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

# Mirror run.sh tunables so the timers/budget match reality. Accept the
# ORBIT_* names (what engine/config.py exports) with AP_* as legacy fallback.
INTERVAL = int(os.environ.get("ORBIT_INTERVAL") or os.environ.get("AP_INTERVAL", "180"))
MAX_TASKS = int(os.environ.get("ORBIT_MAX_TASKS") or os.environ.get("AP_MAX_TASKS", "12"))
CYCLE_TIMEOUT = int(os.environ.get("ORBIT_CYCLE_TIMEOUT") or os.environ.get("AP_CYCLE_TIMEOUT", "3600"))

PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
PRIORITY_LADDER = ["low", "medium", "high"]
SAFE = {"testing", "documentation", "dependencies", "code_quality", "developer_experience"}
# What the loop CAN work when promoted (autopilot:allow). Must match
# backlog_to_tasks.EMITTABLE_CATEGORIES. HUMAN_ONLY is never loop-pickable.
EMITTABLE = SAFE | {"bug", "feature", "refactor"}
HUMAN_ONLY = {"security", "infrastructure", "release_management", "git_practices"}
_LOGTS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]")
_TERMINAL = re.compile(r"pushed origin/|cycle produced no changes|cycle rc=|REFUSING|"
                       r"single-cycle|cycle committed|cycle left uncommitted|no-op|daily budget|daily task cap")


# ── data loaders ─────────────────────────────────────────────────────────────
def load_backlog() -> list[dict]:
    try:
        return (yaml.safe_load(BACKLOG.read_text()) or {}).get("tasks", []) or []
    except Exception:
        return []


def load_queue() -> list[dict]:
    try:
        return json.loads(QUEUE.read_text()).get("tasks", []) if QUEUE.exists() else []
    except Exception:
        return []


def load_ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text()).get("entries", {}) if LEDGER.exists() else {}
    except Exception:
        return {}


def load_skips() -> set[str]:
    if not SKIPS.exists():
        return set()
    return {ln.strip() for ln in SKIPS.read_text().splitlines() if ln.strip() and not ln.startswith("#")}


def _ts_to_epoch(s: str) -> float | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return None


def cycle_pids() -> list[int]:
    """PIDs of the in-flight cycle (claude --print /autopilot-cycle + its caffeinate)."""
    try:
        out = subprocess.run(["pgrep", "-f", "autopilot-cycle"], capture_output=True, text=True).stdout
        return [int(x) for x in out.split()]
    except Exception:
        return []


def loop_loaded() -> bool:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        return LAUNCHD_LABEL in out
    except Exception:
        return False


def probe_runtime() -> dict:
    """Everything the live console needs that isn't in backlog/ledger."""
    pids = cycle_pids()
    running = bool(pids)
    # Cycle start = the timestamp encoded in the newest cycle-<ts>.log filename
    # (created at cycle start; --print only flushes the body at the end, so the
    # filename is the reliable start clock).
    start = None
    cyclelogs = sorted(LOGDIR.glob("cycle-*.log")) if LOGDIR.exists() else []
    if cyclelogs:
        m = re.search(r"cycle-(\d{8}T\d{6})\.log", cyclelogs[-1].name)
        if m:
            try:
                start = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").timestamp()
            except Exception:
                start = None

    # Heartbeat + completed-cycle durations from autopilot.log (turnaround time).
    heartbeat = None
    last_dur = None
    last_start = None
    durs = []
    if APLOG.exists():
        for ln in APLOG.read_text(errors="ignore").splitlines()[-1200:]:
            m = _LOGTS.match(ln)
            if not m:
                continue
            ep = _ts_to_epoch(m.group(1))
            if ep is None:
                continue
            heartbeat = ep
            if "cycle start" in ln:
                last_start = ep
            elif _TERMINAL.search(ln) and last_start:
                last_dur = max(0, int(ep - last_start))
                durs.append(last_dur)
                last_start = None
    recent = durs[-12:]
    avg_cycle = round(sum(recent) / len(recent)) if recent else None

    today = datetime.now().strftime("%Y-%m-%d")
    try:
        daily = int((AP_STATE / f".count-{today}").read_text().strip())
    except Exception:
        daily = 0
    try:
        spend = float((AP_STATE / f".spend-{today}").read_text().strip())
    except Exception:
        spend = 0.0

    return {
        "now": time.time(),
        "loop_loaded": loop_loaded(),
        "stopped": STOP.exists(),
        "cycle_running": running,
        "cycle_start": start if running else None,
        "current_task": (MARKER.read_text().strip() if MARKER.exists() else None),
        "heartbeat": heartbeat,
        "last_cycle_dur": last_dur,
        "avg_cycle_sec": avg_cycle,
        "cycle_samples": len(durs),
        "daily_count": daily,
        "daily_spend": round(spend, 2),
        "max_tasks": MAX_TASKS,
        "interval": INTERVAL,
        "cycle_timeout": CYCLE_TIMEOUT,
        "base_branch": BASE_BRANCH,
    }


# ── backlog block editing (comment-preserving) ───────────────────────────────
def _read_blocks() -> tuple[str, list[str], list[str]]:
    text = BACKLOG.read_text()
    # Tolerate both list indents: legacy "  - id:" and flat "- id:" (yaml.dump /
    # the backlog lint pass canonicalizes to flat — the dashboard must not care).
    parts = re.split(r"(?m)(?=^(?:  )?- id: )", text)
    header, blocks = parts[0], parts[1:]
    ids = []
    for b in blocks:
        m = re.match(r"^(?:  )?- id: (\S+)", b)
        ids.append(m.group(1) if m else "")
    return header, blocks, ids


def _write_blocks(header: str, blocks: list[str]):
    BACKLOG.write_text(header + "".join(blocks))


def _run_converter():
    subprocess.run(["python3", str(CONVERTER)], cwd=str(ENGINE), capture_output=True)


def _set_field(block: str, field: str, value: str) -> str:
    if re.search(rf"(?m)^  (?:  )?{field}: ", block):
        return re.sub(rf"(?m)^(  (?:  )?{field}: ).*$", lambda m: m.group(1) + value, block, count=1)
    return block  # field absent — leave untouched


def _get_field(block: str, field: str) -> str | None:
    m = re.search(rf"(?m)^  (?:  )?{field}: (.*)$", block)
    return m.group(1).strip() if m else None


# ── control actions ──────────────────────────────────────────────────────────
def do_stop():
    STOP.write_text(f"stopped via command center {datetime.now(timezone.utc).isoformat()}\n")


def do_resume():
    STOP.unlink(missing_ok=True)


def do_skip(tid: str):
    cur = load_skips()
    if tid and tid not in cur:
        with SKIPS.open("a") as f:
            f.write(tid + "\n")
    _run_converter()


def do_killtask() -> str:
    """Abort the in-flight cycle: SIGTERM the cycle process, then skip the task so
    the loop won't immediately re-pick it. Reversible by editing skips.txt."""
    pids = cycle_pids()
    tid = MARKER.read_text().strip() if MARKER.exists() else ""
    if not pids:
        return "No cycle is currently running — nothing to stop."
    for p in pids:
        try:
            os.kill(p, 15)
        except ProcessLookupError:
            pass
    if tid:
        do_skip(tid)
        return (f"Stopped the running cycle (pid {pids[0]}) and skipped '{tid}' so it won't be retried. "
                f"The wrapper will reset the worktree and start a fresh cycle after its backoff. "
                f"Un-skip by editing skips.txt.")
    return f"Stopped the running cycle (pid {pids[0]}). No task id was claimed yet."


def do_priority(tid: str, direction: str) -> str:
    header, blocks, ids = _read_blocks()
    if tid not in ids:
        return f"Task {tid} not found."
    i = ids.index(tid)
    cur = (_get_field(blocks[i], "priority") or "medium").strip()
    idx = PRIORITY_LADDER.index(cur) if cur in PRIORITY_LADDER else 1
    idx = min(2, idx + 1) if direction == "up" else max(0, idx - 1)
    new = PRIORITY_LADDER[idx]
    blocks[i] = _set_field(blocks[i], "priority", new)
    _write_blocks(header, blocks)
    _run_converter()
    return f"{tid}: priority {cur} → {new}."


def do_move(tid: str, direction: str) -> str:
    header, blocks, ids = _read_blocks()
    if tid not in ids:
        return f"Task {tid} not found."
    i = ids.index(tid)
    j = i - 1 if direction == "up" else i + 1
    if j < 0 or j >= len(blocks):
        return f"{tid} is already at the {'top' if direction=='up' else 'bottom'}."
    blocks[i], blocks[j] = blocks[j], blocks[i]
    _write_blocks(header, blocks)
    _run_converter()
    return f"Moved {tid} {direction}."


def do_promote(tid: str) -> str:
    """Board → Next up — works on a task in ANY status (proposed or already
    queued review-only). For an EMITTABLE category (chores + bug/feature/refactor)
    we set status:queued + autopilot:allow so it lands in Next up and the loop can
    work it. For a HUMAN-ONLY category (security/infra/release/git) we queue it but
    leave the gate — the loop never touches those, so it stays board-tracked."""
    header, blocks, ids = _read_blocks()
    if tid not in ids:
        return f"Task {tid} not found."
    i = ids.index(tid)
    blocks[i] = _set_field(blocks[i], "status", "queued")
    cat = _get_field(blocks[i], "category")
    if cat in EMITTABLE:
        blocks[i] = _set_field(blocks[i], "autopilot", "allow")
        tail = " → now in ⏭ Next up (loop will pick it)."
    else:
        tail = (f" Queued, but '{cat}' is human-only — the loop never works it. "
                f"Handle it manually / via PR.")
    _write_blocks(header, blocks)
    _run_converter()
    return f"Promoted {tid}.{tail}"


def do_force(tid: str) -> str:
    """Operator OVERRIDE for a human-only (security/infra) task: hand it to the loop.
    Sets status:queued + autopilot:allow + forced:true so the converter emits it and
    the cycle works it. The loop fixes the code-fixable ones and escalates the rest
    (secret rotation, infra grants, running migrations) back to a human. Auto-feed
    still never touches these — only this explicit action does."""
    header, blocks, ids = _read_blocks()
    if tid not in ids:
        return f"Task {tid} not found."
    i = ids.index(tid)
    cat = _get_field(blocks[i], "category")
    blocks[i] = _set_field(blocks[i], "status", "queued")
    blocks[i] = _set_field(blocks[i], "autopilot", "allow")
    if re.search(r"(?m)^  (?:  )?forced: ", blocks[i]):
        blocks[i] = _set_field(blocks[i], "forced", "true")
    else:   # insert the field right after the autopilot line, matching its indent
        blocks[i] = re.sub(r"(?m)^(\s{2,4})(autopilot: .*)$",
                           lambda m: f"{m.group(1)}{m.group(2)}\n{m.group(1)}forced: true",
                           blocks[i], count=1)
    _write_blocks(header, blocks)
    _run_converter()
    return (f"⚠ Forced {tid} ({cat}) to the agent → now in ⏭ Next up. The loop will attempt a "
            f"fix on the branch for review, or escalate it if it's not code-fixable (rotation/infra).")


def do_runnow(tid: str) -> str:
    """Force a task to be the loop's NEXT pick: move it to the top of the backlog,
    set priority high (+ allow if workable), and resume the loop. The agent picks
    it on the next cycle (≤~20s if idle, else after the current cycle finishes)."""
    header, blocks, ids = _read_blocks()
    if tid not in ids:
        return f"Task {tid} not found."
    i = ids.index(tid)
    blk = blocks.pop(i)
    blk = _set_field(blk, "status", "queued")
    blk = _set_field(blk, "priority", "high")
    cat = _get_field(blk, "category")
    if cat in EMITTABLE:
        blk = _set_field(blk, "autopilot", "allow")
    blocks.insert(0, blk)
    _write_blocks(header, blocks)
    do_resume()           # clear STOP so the loop actually runs
    _run_converter()
    if cat not in EMITTABLE:
        return (f"{tid} moved to top + loop resumed, but '{cat}' is human-only — "
                f"the loop won't run it. Handle it manually.")
    return (f"▶ {tid} set to RUN NEXT (top + high priority) and loop resumed — "
            f"the agent picks it on the next cycle (≤~20s if idle).")


def do_unskip(tid: str) -> str:
    """Remove an id from skips.txt so it can be queued/picked again."""
    if not SKIPS.exists():
        return f"{tid} was not skipped."
    kept = [ln for ln in SKIPS.read_text().splitlines() if ln.strip() != tid]
    SKIPS.write_text("\n".join(kept) + ("\n" if kept else ""))
    _run_converter()
    return f"Un-skipped {tid} — back on the board."


def do_autofeed(on: bool) -> str:
    """Toggle auto-feed: when ON the wrapper auto-promotes the next proposed task
    whenever the loop queue is empty, so the agent keeps running."""
    if on:
        AUTO_PROMOTE.write_text("on\n")
        return "Auto-feed ON — the loop will auto-promote the next safe task when the queue empties."
    AUTO_PROMOTE.unlink(missing_ok=True)
    return "Auto-feed OFF — the loop only works what you promote."


def do_rollback(tid: str) -> str:
    led = load_ledger().get(tid, {})
    sha = led.get("sha")
    if not sha:
        return f"No recorded commit sha for task {tid} — nothing to roll back."
    cur = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--abbrev-ref", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "-uno"],
                           capture_output=True, text=True).stdout.strip()
    manual = f"git checkout {BASE_BRANCH} && git pull && git revert --no-edit {sha} && git push origin {BASE_BRANCH}"
    if cur != BASE_BRANCH or dirty:
        return (f"Won't auto-revert: main checkout is on '{cur}'"
                + (" with uncommitted tracked changes" if dirty else "")
                + f" (need a clean '{BASE_BRANCH}'). Run manually:\n  {manual}")
    rev = subprocess.run(["git", "-C", str(REPO), "revert", "--no-edit", sha],
                         capture_output=True, text=True)
    if rev.returncode != 0:
        return f"git revert failed for {sha}:\n{rev.stderr.strip()}\nResolve manually."
    push = subprocess.run(["git", "-C", str(REPO), "push", "origin",
                           f"HEAD:refs/heads/{BASE_BRANCH}"], capture_output=True, text=True)
    if push.returncode != 0:
        return f"Reverted {sha} locally but PUSH failed (non-ff?):\n{push.stderr.strip()}"
    return f"Rolled back task {tid} (reverted {sha[:8]}) and pushed to origin/{BASE_BRANCH}."


_MERGED_CACHE = {"t": 0.0, "v": {}}


def merged_map() -> dict:
    """sha → bool: is a shipped commit already an ancestor of the loop base
    branch (i.e. its branch/PR got merged)? Local-refs-only ancestry check
    (run.sh fetches each cycle), cached 60s so the 3s poll stays cheap."""
    now = time.time()
    if now - _MERGED_CACHE["t"] < 60:
        return _MERGED_CACHE["v"]
    shas = [e.get("sha") for e in load_ledger().values()
            if e.get("sha") and e.get("state") in ("committed", "pushed", "merged")]
    out = {}
    if shas:
        # One rev-list membership check instead of a merge-base subprocess per
        # entry — the cold-cache path must not stall the first /api/state poll.
        try:
            r = subprocess.run(["git", "-C", str(REPO), "rev-list", "-8000",
                                f"origin/{BASE_BRANCH}"],
                               capture_output=True, text=True, timeout=10)
            reachable = r.stdout.split()
            full = set(reachable)
            # Some older ledger entries recorded SHORT shas — prefix-match those.
            out = {s: (s in full if len(s) >= 40 else any(f.startswith(s) for f in reachable))
                   for s in shas}
        except Exception:
            out = {s: False for s in shas}
    _MERGED_CACHE.update(t=now, v=out)
    return out


def do_mark(tid: str, outcome: str, note: str = "") -> str:
    """Record the human review outcome (merged / rejected) in the ledger — the
    raw data behind merge-rate-per-source. Reject keeps the branch on origin
    (delete it manually if you want); the id stays worked so it isn't re-picked."""
    if outcome not in ("merged", "rejected"):
        return f"Invalid outcome '{outcome}'."
    r = subprocess.run(["python3", str(ENGINE / "ledger.py"), "mark", tid, outcome, note],
                       capture_output=True, text=True)
    _MERGED_CACHE.update(t=0.0)   # re-check ancestry on the next poll
    if r.returncode != 0:
        return f"ledger mark failed: {r.stderr.strip()}"
    extra = ("" if outcome == "merged" else
             f" Branch stays on origin — delete with: git push origin :refs/heads/<branch>.")
    return f"Task {tid} marked {outcome}.{extra}"


def do_answer(tid: str, text: str) -> str:
    """Answer an escalated task and RE-QUEUE it: the answer lands in the task's
    contract (operator_answer), the ledger entry is cleared so the picker can
    take it again, and any skip is lifted. Closes the escalation dead-end."""
    text = " ".join(text.split())   # single-line: YAML field is one quoted line
    if not text:
        return "Empty answer — nothing done."
    header, blocks, ids = _read_blocks()
    synthesized = False
    if tid not in ids:
        # Orphan escalation (legacy foundry bare-number id, or any ledger-only task
        # with no backlog block). Instead of refusing — which left the card stuck in
        # the inbox — synthesize a block from the ledger so the answer IS the
        # contract. The human answering is the decision to let the loop work it.
        led = load_ledger().get(tid, {})
        title = (led.get("title") or f"Answered escalation {tid}").replace('"', "'")
        reason = (led.get("reason") or "").replace('"', "'")
        ctx = "Re-queued from an answered escalation that had no backlog block."
        if reason:
            ctx += f" Original stop reason: {reason}"
        new_block = (   # write in the FLAT format the file actually uses
            f"- id: {tid}\n"
            f'  title: "{title}"\n'
            f"  category: bug\n"
            f"  autopilot: allow\n"
            f"  status: queued\n"
            f"  priority: medium\n"
            f"  source: escalation-answer\n"
            f"  context: |\n"
            f"    {ctx}\n"
            f"  operator_answer: {json.dumps(text)}\n"
            f"  acceptance_criteria:\n"
            f"    - {json.dumps(text)}\n"
        )
        blocks.append(new_block)
        ids.append(tid)
        synthesized = True
    i = ids.index(tid)
    blocks[i] = _set_field(blocks[i], "status", "queued")
    cat = _get_field(blocks[i], "category")
    if cat in EMITTABLE:
        blocks[i] = _set_field(blocks[i], "autopilot", "allow")
    quoted = json.dumps(text)   # safe one-line double-quoted YAML scalar
    if re.search(r"(?m)^\s{2,4}operator_answer: ", blocks[i]):
        blocks[i] = re.sub(r"(?m)^(\s{2,4})operator_answer: .*$",
                           lambda m: f"{m.group(1)}operator_answer: {quoted}", blocks[i], count=1)
    else:
        blocks[i] = re.sub(r"(?m)^(\s{2,4})(autopilot: .*)$",
                           lambda m: f"{m.group(1)}{m.group(2)}\n{m.group(1)}operator_answer: {quoted}",
                           blocks[i], count=1)
    _write_blocks(header, blocks)
    subprocess.run(["python3", str(ENGINE / "ledger.py"), "clear", tid], capture_output=True)
    if tid in load_skips():
        kept = [ln for ln in SKIPS.read_text().splitlines() if ln.strip() != tid]
        SKIPS.write_text("\n".join(kept) + ("\n" if kept else ""))
    _run_converter()
    if synthesized:
        return (f"💬 Answered {tid} (no prior backlog block) — created one from your answer as the "
                f"contract and re-queued it as a bug. The loop will pick it up.")
    if cat not in EMITTABLE:
        return f"Answered {tid}, but '{cat}' is human-only — it's queued on the board, not loop-pickable."
    return f"💬 Answered {tid} and re-queued it — the loop will re-pick it with your answer in the contract."


def do_lintok(tid: str) -> str:
    """Waive the backlog linter's hard gate for one task (sets lint_ok: true).
    Use when the thin contract is intentional (e.g. a trivial chore)."""
    header, blocks, ids = _read_blocks()
    if tid not in ids:
        return f"Task {tid} not found."
    i = ids.index(tid)
    if re.search(r"(?m)^    lint_ok: ", blocks[i]):
        blocks[i] = _set_field(blocks[i], "lint_ok", "true")
    else:
        blocks[i] = re.sub(r"(?m)^(    category: .*)$", r"\1\n    lint_ok: true", blocks[i], count=1)
    _write_blocks(header, blocks)
    _run_converter()
    return f"Lint gate waived for {tid} — it can now be emitted despite the thin contract."


def read_packet(tid: str) -> str | None:
    # Serve a ship's review packet (reviews/task-<id>.md). tid already validated.
    p = REVIEWS / f"task-{tid}.md"
    return p.read_text(errors="ignore") if p.exists() else None


def read_patch(tid: str) -> str | None:
    # Serve a ship's backup patch — only ever a ledger-recorded file under diffs/.
    path = load_ledger().get(tid, {}).get("patch")
    if not path:
        return None
    p = Path(path).resolve()
    if not str(p).startswith(str(DIFFDIR.resolve()) + os.sep) or not p.is_file():
        return None
    return p.read_text(errors="ignore")[:400_000]


def cycletail(max_lines: int = 120) -> dict:
    """Human-readable live tail of the newest cycle log. stream-json logs are
    parsed into narrator lines (agent text, tool calls, final cost); legacy
    plain-text logs pass through as-is."""
    logs = sorted(LOGDIR.glob("cycle-*.log")) if LOGDIR.exists() else []
    if not logs:
        return {"file": None, "lines": []}
    p = logs[-1]
    try:
        raw = p.read_text(errors="ignore").splitlines()[-600:]
    except Exception:
        raw = []
    lines = []
    for ln in raw:
        ln = ln.strip()
        if not ln:
            continue
        if not ln.startswith("{"):
            lines.append(ln)
            continue
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            lines.append(f"● cycle started · model {ev.get('model', '?')}")
        elif t == "assistant":
            for blk in (ev.get("message") or {}).get("content", []) or []:
                if blk.get("type") == "text" and (blk.get("text") or "").strip():
                    lines.append(blk["text"].strip())
                elif blk.get("type") == "tool_use":
                    inp = blk.get("input") or {}
                    hint = (inp.get("description") or inp.get("file_path")
                            or inp.get("command") or inp.get("subagent_type") or "")
                    lines.append(f"→ {blk.get('name', '?')}: {str(hint)[:110]}")
        elif t == "result":
            cost = ev.get("total_cost_usd")
            lines.append(f"■ cycle finished · cost ${cost:.2f}"
                         if isinstance(cost, (int, float)) else "■ cycle finished")
    return {"file": p.name, "lines": lines[-max_lines:]}


def _load_builds() -> list:
    # Raw worktree-agent build registry (feature_builds.json), or [] if absent.
    if not FEATURE_BUILDS.exists():
        return []
    try:
        return json.loads(FEATURE_BUILDS.read_text()).get("builds", []) or []
    except Exception:
        return []


def feature_builds_annotated() -> list:
    """Worktree-agent builds, each annotated with `merged` — whether its commit is
    already an ancestor of the loop branch. Local-only ancestry check (reads
    origin/<branch> refs already fetched; no network), so it is cheap per poll."""
    builds = _load_builds()
    loop_ref = f"origin/{BASE_BRANCH}"
    for b in builds:
        b["merged"] = False
        target = b.get("sha") or (f"origin/{b['branch']}" if b.get("branch") else None)
        if not target:
            continue
        try:
            r = subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", target, loop_ref],
                               capture_output=True, text=True, timeout=5)
            b["merged"] = (r.returncode == 0)
        except Exception:
            pass
    return builds


def do_merge_to_loop(branch: str) -> str:
    """Merge a worktree-agent branch into the loop branch and push it.

    Safety: runs entirely in a throwaway worktree (the main checkout is never
    touched), ABORTS with no push on conflict, and NEVER force-pushes. Only
    branches registered in feature_builds.json may be merged (allowlist), so the
    endpoint can't be used to merge arbitrary refs.
    """
    builds = _load_builds()
    known = {b.get("branch") for b in builds if b.get("branch")}
    if branch not in known:
        return f"Refused: '{branch}' is not a registered worktree-agent branch."
    loop_ref, src_ref = f"origin/{BASE_BRANCH}", f"origin/{branch}"

    def git(*args):
        return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)

    git("fetch", "origin", "--quiet")
    if git("merge-base", "--is-ancestor", src_ref, loop_ref).returncode == 0:
        return f"'{branch}' is already merged into {BASE_BRANCH} — nothing to do."

    wt = tempfile.mkdtemp(prefix="cc-merge-")
    try:
        add = git("worktree", "add", "--detach", wt, loop_ref)
        if add.returncode != 0:
            return f"Could not create merge worktree:\n{add.stderr.strip()}"

        def gwt(*args):
            return subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True)

        mg = gwt("merge", "--no-ff", src_ref,
                 "-m", f"merge: bring worktree feature '{branch}' onto the loop branch")
        if mg.returncode != 0:
            conflicts = gwt("ls-files", "-u").stdout
            gwt("merge", "--abort")
            files = sorted({ln.split("\t")[-1] for ln in conflicts.splitlines() if "\t" in ln})
            return (f"CONFLICT merging '{branch}' into {BASE_BRANCH} — aborted, nothing pushed.\n"
                    f"Conflicting files: {', '.join(files) or '(see git)'}.\n"
                    f"If this is the #7 alerting duplicate, the loop already shipped its own build "
                    f"(c91fe56b) — revert that first or reconcile manually.")
        push = gwt("push", "origin", f"HEAD:refs/heads/{BASE_BRANCH}")
        if push.returncode != 0:
            return f"Merged '{branch}' cleanly but PUSH failed (non-ff? loop moved?):\n{push.stderr.strip()}"
        for b in builds:
            if b.get("branch") == branch:
                b["merged"] = True
        try:
            FEATURE_BUILDS.write_text(json.dumps({"builds": builds}, indent=2) + "\n")
        except Exception:
            pass
        return f"✓ Merged '{branch}' → origin/{BASE_BRANCH} and pushed. It's now on the loop branch."
    finally:
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", wt],
                       capture_output=True, text=True)
        shutil.rmtree(wt, ignore_errors=True)
        subprocess.run(["git", "-C", str(REPO), "worktree", "prune"], capture_output=True, text=True)


# ── state assembly (JSON for the SPA) ─────────────────────────────────────────
def agent_metrics() -> dict:
    """Productivity stats — local & fast (ledger states + the wrapper's saved
    patches under diffs/). No git/network, so it's safe to call every poll."""
    from collections import Counter
    m = {"pushed": 0, "escalated": 0, "changes_total": 0, "changes_today": 0,
         "changes_7d": 0, "loc_added_7d": 0, "loc_removed_7d": 0}
    try:
        c = Counter(v.get("state") for v in load_ledger().values())
        # merged/rejected are POST-review states of shipped work — still ships.
        m["pushed"] = (c.get("pushed", 0) + c.get("committed", 0)
                       + c.get("merged", 0) + c.get("rejected", 0))
        m["merged"] = c.get("merged", 0)
        m["rejected"] = c.get("rejected", 0)
        m["escalated"] = c.get("escalated", 0)
    except Exception:
        pass
    now = time.time()
    diffdir = AP_STATE / "diffs"
    if diffdir.exists():
        for p in diffdir.glob("*.patch"):
            try:
                age = now - p.stat().st_mtime
            except Exception:
                continue
            m["changes_total"] += 1
            if age < 86400:
                m["changes_today"] += 1
            if age < 7 * 86400:
                m["changes_7d"] += 1
                try:
                    for ln in p.read_text(errors="ignore").splitlines():
                        if ln.startswith("+") and not ln.startswith("+++"):
                            m["loc_added_7d"] += 1
                        elif ln.startswith("-") and not ln.startswith("---"):
                            m["loc_removed_7d"] += 1
                except Exception:
                    pass
    return m


_HISTORY_CACHE = {"t": 0.0, "v": None}


def _classify_subject(s: str) -> str:
    s = (s or "").lower()
    if s.startswith("docs"):
        return "docs"
    if s.startswith("test"):
        return "tests"
    if s.startswith(("feat", "feature")):
        return "feature"
    if s.startswith(("fix", "bug")):
        return "bug"
    if s.startswith("refactor"):
        return "refactor"
    if s.startswith(("chore", "build", "ci", "perf")):
        return "chore"
    return "other"


def agent_history() -> dict:
    """Per-day shipped activity from the loop branch's git log (autopilot commits),
    + a sha→{date,type,subj} map so cards can show commit dates. 30-day window.
    Cached 60s — git is the only network/disk-heavy bit, so the 3s poll stays fast."""
    now = time.time()
    if _HISTORY_CACHE["v"] is not None and now - _HISTORY_CACHE["t"] < 60:
        return _HISTORY_CACHE["v"]
    days, commits = {}, {}
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "log", f"origin/{BASE_BRANCH}",
             "--grep", "Co-Authored-By: Claude", "--since", "30 days ago",
             "--date=short", "--numstat", "--format=__C__%x09%H%x09%ad%x09%s"],
            capture_output=True, text=True, timeout=12).stdout
        cur = None
        for ln in out.splitlines():
            if ln.startswith("__C__"):
                parts = ln.split("\t")
                if len(parts) >= 4:
                    sha, date, subj = parts[1], parts[2], parts[3]
                    typ = _classify_subject(subj)
                    cur = date
                    d = days.setdefault(date, {"commits": 0, "added": 0, "removed": 0, "types": {}})
                    d["commits"] += 1
                    d["types"][typ] = d["types"].get(typ, 0) + 1
                    commits[sha] = {"date": date, "type": typ, "subj": subj}
            elif cur and "\t" in ln:
                p = ln.split("\t")
                if len(p) == 3 and p[0].isdigit() and p[1].isdigit():
                    days[cur]["added"] += int(p[0])
                    days[cur]["removed"] += int(p[1])
    except Exception:
        pass
    v = {"days": days, "commits": commits}
    _HISTORY_CACHE.update(t=now, v=v)
    return v


def spend_history(days: int = 14) -> list[dict]:
    """Per-day spend + task count from the wrapper's .spend-* / .count-* files —
    the raw series behind the Insights cost band. Cheap file reads, no git."""
    out = []
    today = datetime.now()
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            sp = float((AP_STATE / f".spend-{d}").read_text().strip())
        except Exception:
            sp = 0.0
        try:
            ct = int((AP_STATE / f".count-{d}").read_text().strip())
        except Exception:
            ct = 0
        out.append({"date": d, "spend": round(sp, 2), "tasks": ct})
    return out


def build_state() -> dict:
    backlog = load_backlog()
    ledger = load_ledger()
    skips = load_skips()
    by_id = {str(t.get("id")): t for t in backlog}
    worked = set(ledger.keys())
    hist = agent_history()
    commit_map = hist["commits"]

    def base(tid, meta):
        return {"id": tid, "title": meta.get("title") or tid,
                "category": meta.get("category", ""), "autopilot": meta.get("autopilot", ""),
                "source": meta.get("source", ""), "priority": meta.get("priority", "medium"),
                "status": meta.get("status", ""),
                "acceptance_criteria": [a for a in (meta.get("acceptance_criteria") or [])
                                        if isinstance(a, str)]}

    live = bool(cycle_pids())   # is a real cycle process actually running right now?
    cur_marker = MARKER.read_text().strip() if MARKER.exists() else ""
    mm = merged_map()
    running, done, escalated = [], [], []
    for tid, e in ledger.items():
        meta = by_id.get(tid, {})
        row = base(tid, meta)
        # Legacy foundry ids are bare numbers with no backlog block — give them a
        # readable name instead of showing the id twice on the card.
        row["title"] = (meta.get("title") or e.get("title")
                        or (f"Foundry task #{tid} (legacy)" if tid.isdigit() else tid))
        row.update({"sha": e.get("sha"), "remote_ref": e.get("remote_ref"),
                    "reason": e.get("reason", ""), "updated": e.get("updated_at", ""),
                    "committed_at": commit_map.get(e.get("sha", ""), {}).get("date", "")})
        st = e.get("state")
        # "Set aside" on an escalated/orphaned card writes skips.txt; drop those
        # here so they leave the Escalated column and land in the Skipped section
        # (otherwise the card never moves and the button looks dead). Shipped rows
        # stay — they're reviewed via merge/reject, not skipped.
        if tid in skips and st not in ("committed", "pushed", "merged", "rejected"):
            continue
        if st == "in_progress":
            # Show as RUNNING only if this id is the CURRENT cycle's claimed task
            # (matches the live marker). An in_progress entry from an earlier cycle
            # that died before committing is an orphan, not "running" — even while a
            # different cycle is live. (Regression: 4-day-old claims showed as
            # running because the check was merely `if live`.)
            if live and tid == cur_marker:
                running.append(row)
            else:
                row["reason"] = row.get("reason") or "stalled — claimed but not the live cycle's task (orphaned; Answer & re-queue or Set aside)"
                escalated.append(row)
        elif st in ("committed", "pushed", "merged", "rejected"):
            branch = (e.get("remote_ref") or "").removeprefix("origin/") or e.get("branch") or ""
            merged = st == "merged" or mm.get(e.get("sha") or "", False)
            row.update({
                "branch": branch,
                "merged": merged,
                "rejected": st == "rejected",
                "review_note": e.get("review_note", ""),
                # Prefilled Bitbucket PR-create link, only while it still makes sense
                # (per-task branch, not yet merged, not rejected).
                "pr_url": (f"{BB_PR_NEW}?source={urllib.parse.quote(branch, safe='')}"
                           f"&dest={urllib.parse.quote(BASE_BRANCH, safe='')}"
                           if BB_PR_NEW and branch.startswith("autopilot/") and not merged and st != "rejected"
                           else None),
                "has_packet": (REVIEWS / f"task-{tid}.md").exists(),
                "has_patch": bool(e.get("patch")),
            })
            done.append(row)
        elif st == "escalated":
            escalated.append(row)

    # NEXT UP = the loop's real pick order: queue.json, safe + not worked/skipped,
    # sorted by priority (the picker's key), ties kept in queue (= backlog) order.
    next_up = []
    for q in load_queue():
        tid = str(q.get("id"))
        if tid in worked or tid in skips:
            continue
        if q.get("category") not in EMITTABLE and not q.get("forced"):
            continue  # human-only category only reaches Next up when explicitly forced
        next_up.append({"id": tid, "title": (q.get("task") or tid).split("\n", 1)[0],
                        "category": q.get("category", ""), "autopilot": q.get("autopilot", "allow"),
                        "source": q.get("source", ""), "priority": q.get("priority", "medium"),
                        "status": "queued",
                        "acceptance_criteria": [a for a in (q.get("acceptance_criteria") or [])
                                                if isinstance(a, str)]})
    next_up.sort(key=lambda t: PRIORITY_RANK.get(t.get("priority", "medium"), 1))

    # BOARD = human-curated, not auto-shipped (review-only / human / proposed),
    # minus anything worked or skipped (skipped get their own section).
    board = []
    for t in backlog:
        tid = str(t.get("id"))
        if tid in worked or tid in skips:
            continue
        if t.get("autopilot") in ("review-only", "human") or t.get("status") == "proposed":
            r = base(tid, t)
            # promotable = will actually reach Next up in one click (loop-workable)
            r["promotable"] = t.get("category") in EMITTABLE
            # Contract-quality flags — a hard-failed task is held by the converter
            # even after Promote, so surface it (and the waive action) right here.
            hard, soft = lint_task(t)
            r["lint_hard"], r["lint_soft"] = hard, soft
            board.append(r)
    board.sort(key=lambda t: (0 if t.get("status") == "queued" else 1,
                              PRIORITY_RANK.get(t.get("priority", "medium"), 1)))

    # SKIPPED = explicitly skipped ids (their own section, with an Un-skip action).
    skipped = []
    for tid in sorted(skips):
        meta = by_id.get(tid, {})
        led = ledger.get(tid, {})
        skipped.append({"id": tid,
                        "title": meta.get("title") or led.get("title") or tid,
                        "category": meta.get("category", ""), "autopilot": meta.get("autopilot", ""),
                        "source": meta.get("source", ""), "priority": meta.get("priority", "medium"),
                        "reason": led.get("reason", ""),
                        "status": "skipped", "acceptance_criteria": []})

    # DISCOVERY FUNNEL — per source: how tasks flow proposed → queued → shipped /
    # escalated / skipped. Shows which discovery source actually produces merged work.
    def _blank():
        return {"proposed": 0, "queued": 0, "shipped": 0, "merged": 0,
                "rejected": 0, "escalated": 0, "skipped": 0}

    def _stage(tid, t):
        e = ledger.get(tid, {})
        st = e.get("state")
        if st == "merged" or (e.get("sha") and mm.get(e["sha"])):
            return "merged"
        if st == "rejected":
            return "rejected"
        if st in ("pushed", "committed"):
            return "shipped"
        if st == "escalated":
            return "escalated"
        if tid in skips:
            return "skipped"
        if (t.get("status") == "queued" and t.get("autopilot") == "allow"
                and t.get("category") in EMITTABLE):
            return "queued"
        return "proposed"

    funnel = {}
    for t in backlog:
        src = t.get("source") or "other"
        funnel.setdefault(src, _blank())[_stage(str(t.get("id")), t)] += 1
    for tid, e in ledger.items():       # worked legacy ids not in backlog (early foundry runs)
        if tid in by_id:
            continue
        st = e.get("state")
        bucket = funnel.setdefault("maturity-score", _blank())
        if st == "merged" or (e.get("sha") and mm.get(e["sha"])):
            bucket["merged"] += 1
        elif st == "rejected":
            bucket["rejected"] += 1
        elif st in ("pushed", "committed"):
            bucket["shipped"] += 1
        elif st == "escalated":
            bucket["escalated"] += 1

    runlog = []
    if STATE.exists():
        runlog = [ln[2:] for ln in STATE.read_text().splitlines() if ln.strip().startswith("- ")][-10:]

    rt = probe_runtime()
    rt.update({"running": running, "next_up": next_up, "board": board, "skipped": skipped,
               "auto_feed": AUTO_PROMOTE.exists(), "metrics": agent_metrics(), "history": hist["days"],
               "funnel": funnel,
               "feature_builds": feature_builds_annotated(),
               "spend_history": spend_history(),
               "done": done, "escalated": escalated, "skips": sorted(skips),
               "runlog": runlog,
               "counts": {"next": len(next_up), "board": len(board), "done": len(done),
                          "escalated": len(escalated), "skipped": len(skips)}})
    return rt


# ── HTML shell (static; the SPA fills it from /api/state) ─────────────────────
SHELL = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Autopilot Command Center</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='88'>🛰️</text></svg>">
<style>
:root{--bg:#f4f5f7;--panel:#ffffff;--col:#f7f8f9;--card:#ffffff;--card2:#deebff;--ink:#172b4d;--mut:#5e6c84;
--ok:#00875a;--warn:#ff8b00;--stop:#de350b;--accent:#0052cc;--violet:#6554c0;--line:#dfe1e6}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;min-height:100vh}
header{display:flex;align-items:center;gap:14px;padding:13px 22px;background:var(--panel);
border-bottom:1px solid var(--line);position:sticky;top:0;z-index:10;flex-wrap:wrap;box-shadow:0 1px 2px rgba(9,30,66,.08)}
header h1{font-size:16px;margin:0;letter-spacing:.2px;font-weight:700}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--mut);
background:var(--col);border:1px solid var(--line);border-radius:20px;padding:4px 11px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.dot-on{background:var(--ok)} .dot-off{background:var(--stop)}
.dot-run{background:var(--accent);animation:pulse 1.3s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.branch{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--accent);font-weight:600}
.spacer{margin-left:auto}
.btn{border:0;border-radius:5px;padding:6px 12px;font-size:12px;cursor:pointer;color:#fff;font-weight:600}
.btn:hover{filter:brightness(.95)} .btn:active{transform:translateY(1px)} .btn:disabled{opacity:.4;cursor:not-allowed}
.btn-stop{background:var(--stop)} .btn-go{background:var(--ok)} .btn-mini{padding:4px 9px;font-size:11px;font-weight:600}
.btn-skip{background:#f4f5f7;color:#42526e;border:1px solid var(--line)} .btn-rb{background:var(--violet)} .btn-pr{background:var(--accent)}
.btn-mv{background:#ebecf0;color:#42526e} .btn-kill{background:var(--stop)}
.btn-force{background:#fff0b3;color:#974f0c;border:1px solid #ff8b00;font-weight:700}
.hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;padding:18px 22px 4px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px;box-shadow:0 1px 1px rgba(9,30,66,.12)}
.tile .k{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;font-weight:700}
.tile .v{font-size:26px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums;color:var(--ink)}
.tile .sub{font-size:11px;color:var(--mut);margin-top:2px}
.bar{height:6px;border-radius:4px;background:#ebecf0;margin-top:9px;overflow:hidden}
.bar > i{display:block;height:100%;border-radius:4px;background:var(--accent);transition:width .5s}
.bar.warn > i{background:var(--warn)} .bar.ok > i{background:var(--ok)}
.board{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:16px;padding:14px 22px 30px}
.col{background:var(--col);border:1px solid var(--line);border-radius:8px;padding:0 12px 12px;min-height:90px;
max-height:calc(100vh - 250px);overflow-y:auto}
.col::-webkit-scrollbar{width:8px} .col::-webkit-scrollbar-thumb{background:#c1c7d0;border-radius:4px}
.col h2{font-size:12px;margin:0 -12px 12px;padding:12px 12px 10px;color:var(--mut);text-transform:uppercase;letter-spacing:.07em;
display:flex;align-items:center;gap:8px;font-weight:700;position:sticky;top:0;background:var(--col);z-index:2;border-bottom:1px solid var(--line)}
.col h2 .cnt{background:#dfe1e6;border-radius:10px;padding:1px 8px;font-size:11px;color:#42526e}
.col.c-run{border-top:3px solid var(--accent)} .col.c-next{border-top:3px solid var(--warn)}
.col.c-done{border-top:3px solid var(--ok)} .col.c-board{border-top:3px solid var(--violet)}
.col.c-esc{border-top:3px solid var(--stop)}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px 12px;margin-bottom:10px;box-shadow:0 1px 1px rgba(9,30,66,.18)}
.card.live{background:var(--card2);border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.card-h{display:flex;align-items:center;gap:6px;margin-bottom:5px;flex-wrap:wrap}
.tid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10.5px;color:var(--mut)}
.title{font-weight:600;font-size:13.5px;margin:2px 0;color:var(--ink)}
.meta{font-size:11px;color:var(--mut);margin-top:3px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.b{font-size:10px;padding:2px 7px;border-radius:3px;text-transform:uppercase;letter-spacing:.03em;font-weight:700}
.b-ok{background:#e3fcef;color:#006644} .b-warn{background:#fff0b3;color:#974f0c}
.b-stop{background:#ffebe6;color:#bf2600} .b-muted{background:#dfe1e6;color:#42526e}
.b-vio{background:#eae6ff;color:#403294} .b-acc{background:#deebff;color:#0747a6}
details{margin-top:7px} summary{cursor:pointer;color:var(--mut);font-size:11.5px}
ul.acc{margin:6px 0 0 16px;padding:0;font-size:11.5px;color:#42526e}
.timer{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-weight:700;color:var(--accent);font-variant-numeric:tabular-nums}
.wd{height:4px;border-radius:3px;background:#ebecf0;margin-top:6px;overflow:hidden}.wd>i{display:block;height:100%;background:var(--accent)}
.btns{display:flex;gap:5px;margin-top:9px;flex-wrap:wrap;align-items:flex-start}
.ord{display:flex;flex-direction:column;gap:2px;margin-right:2px}
.reason{font-size:11.5px;color:#bf2600;margin-top:4px}
.toast{position:fixed;right:18px;bottom:18px;max-width:460px;background:#fff;border:1px solid var(--accent);
border-radius:8px;padding:12px 16px;font-size:12.5px;white-space:pre-wrap;box-shadow:0 8px 24px rgba(9,30,66,.25);
opacity:0;transform:translateY(10px);transition:.25s;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;z-index:50;color:var(--ink)}
.toast.show{opacity:1;transform:none}
.empty{color:#97a0af;font-size:12px;text-align:center;padding:14px 0}
.runlog{padding:0 22px 34px}.runlog h3{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.06em;font-weight:700}
.runlog ul{list-style:none;margin:8px 0 0;padding:0}
.runlog li{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:var(--mut);padding:4px 0;border-bottom:1px solid #ebecf0}
.off{font-size:11px;color:var(--mut);margin-left:6px}
.filterbar{display:flex;flex-wrap:wrap;gap:8px;padding:8px 22px 0;align-items:center}
.chip{border:1px solid var(--line);background:var(--panel);color:var(--mut);border-radius:16px;padding:5px 11px;font-size:12px;cursor:pointer;font-weight:600}
.chip:hover{border-color:var(--accent)} .chip.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.chip b{font-weight:800;margin-left:3px} .chip-go{margin-left:auto;background:var(--ok);color:#fff;border-color:var(--ok)}
.col.c-skip{border-top:3px solid #8993a4}
.histband{margin:8px 22px 0;padding:14px 16px;background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:0 1px 1px rgba(9,30,66,.12)}
.histhead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.histhead h3{margin:0;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;font-weight:700}
.histsum{font-size:12px;color:var(--mut)} .histsum b{color:var(--ink)}
.hrange{display:flex;gap:6px;margin-left:auto}
.hchart{display:flex;align-items:flex-end;gap:3px;height:96px;overflow-x:auto;padding-top:6px}
.hday{display:flex;flex-direction:column;justify-content:flex-end;align-items:center;min-width:13px;flex:1 0 13px}
.hbar{width:80%;display:flex;flex-direction:column-reverse;border-radius:2px 2px 0 0;overflow:hidden;min-height:2px;background:#ebecf0}
.hbar i{display:block;width:100%} .hday:hover .hbar{outline:2px solid var(--accent)}
.hlbl{font-size:8.5px;color:var(--mut);margin-top:3px;writing-mode:vertical-rl;transform:rotate(180deg);white-space:nowrap}
.hlegend{display:flex;gap:11px;flex-wrap:wrap;font-size:10.5px;color:var(--mut);margin-top:9px}
.hlegend span{display:inline-flex;align-items:center;gap:4px} .hlegend i{width:9px;height:9px;border-radius:2px;display:inline-block}
.funnelband{margin:8px 22px 0;padding:14px 16px;background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:0 1px 1px rgba(9,30,66,.12)}
.frow{display:flex;align-items:center;gap:12px;padding:5px 0}
.fsrc{width:115px;font-size:12px;font-weight:600;color:var(--ink);flex:none}
.fbar{flex:1;display:flex;height:14px;border-radius:7px;overflow:hidden;background:#ebecf0;min-width:80px}
.fbar i{display:block} .fnum{font-size:11px;color:var(--mut);flex:none;font-family:ui-monospace,monospace}
.modal{display:none;position:fixed;inset:0;background:rgba(9,30,66,.45);z-index:100;align-items:center;justify-content:center}
.modalbox{background:#fff;border-radius:10px;padding:18px 20px;width:min(640px,92vw);box-shadow:0 16px 48px rgba(9,30,66,.35)}
.modalbox textarea{width:100%;height:300px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;border:1px solid var(--line);border-radius:6px;padding:10px;color:var(--ink);background:var(--col);resize:vertical}
.featureband{margin:8px 22px 0;padding:14px 16px 12px;background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--violet);border-radius:8px;box-shadow:0 1px 1px rgba(9,30,66,.12)}
.fbuild{display:flex;align-items:flex-start;gap:10px;padding:7px 0;border-bottom:1px solid #ebecf0}
.fbuild:last-child{border-bottom:0}
.fb-dot{width:10px;height:10px;border-radius:50%;margin-top:4px;flex:none}
.fb-dot.run{animation:pulse 1.3s infinite}
.fb-title{font-weight:600;font-size:13px;color:var(--ink)} .fb-meta{font-size:11px;color:var(--mut);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;margin-top:2px}
.fb-badge{font-size:11px;font-weight:700;color:var(--violet);background:#eae6ff;border-radius:12px;padding:2px 9px}
.fb-run{font-size:11px;font-weight:700;color:var(--accent)}
.fb-empty{font-size:12px;color:var(--mut);padding:6px 0 2px;line-height:1.5}
.fb-empty code,.fb-flow code{font-family:ui-monospace,Menlo,monospace;background:var(--col);border:1px solid var(--line);border-radius:4px;padding:1px 5px;color:var(--ink)}
.fb-flow{font-size:11.5px;color:var(--mut);margin:2px 0 10px;line-height:1.6} .fb-flow b{color:var(--violet)}
.fb-act{margin-left:auto;flex:none;display:flex;align-items:center;padding-left:10px}
.fb-merged{font-size:11px;font-weight:700;color:var(--ok);background:#e3fcef;border-radius:12px;padding:3px 9px;white-space:nowrap}
.fb-mergebtn{font-size:11px;font-weight:700;color:#fff;background:var(--violet);border:0;border-radius:5px;padding:6px 11px;cursor:pointer;white-space:nowrap}
.fb-mergebtn:hover{filter:brightness(1.08)}
.statusline{padding:10px 22px;background:var(--panel);border-bottom:1px solid var(--line);font-size:13px;color:var(--ink)}
.statusline b{font-weight:700}
.inboxband{margin:14px 22px 0;padding:14px 16px;background:#fffaf0;border:1px solid #ffe2bd;border-left:4px solid var(--warn);border-radius:8px;box-shadow:0 1px 1px rgba(9,30,66,.12)}
.inboxband.zero{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--ok)}
.inboxgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:10px;margin-top:6px}
.tailband{margin:8px 22px 0;padding:14px 16px;background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:8px;box-shadow:0 1px 1px rgba(9,30,66,.12)}
.viewer{max-height:60vh;overflow:auto;background:var(--col);border:1px solid var(--line);border-radius:6px;padding:12px;margin:0;font:11.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-word;color:var(--ink)}
#tailbody{max-height:260px}
.dl-add{color:#006644;background:#e3fcef;display:block}.dl-del{color:#bf2600;background:#ffebe6;display:block}.dl-hunk{color:#0747a6;font-weight:700;display:block}
.modalbox-wide{width:min(980px,94vw)}
.lint{font-size:11px;color:#974f0c;margin-top:4px}
</style></head><body>
<header>
  <h1>🛰️ Autopilot Command Center</h1>
  <span class="pill" id="loopPill">…</span>
  <span class="pill">base <span class="branch">__BRANCH__</span></span>
  <span class="pill" id="stalePill" style="display:none">⚠ reconnecting…</span>
  <span class="spacer"></span>
  <button class="btn btn-skip" id="feedBtn" onclick="toggleFeed()" title="when ON, auto-promote the next safe task whenever the queue empties">🔁 Auto-feed</button>
  <button class="btn" id="loopBtn" onclick="toggleLoop()">…</button>
</header>
<div class="statusline" id="statusline">…</div>
<div class="inboxband" id="inboxband" style="display:none"></div>
<div class="hero" id="hero"></div>
<div class="tailband" id="tailband" style="display:none">
  <div class="histhead"><h3>📡 Live cycle</h3><span class="histsum" id="tailfile"></span></div>
  <pre class="viewer" id="tailbody"></pre>
</div>
<div class="histband" id="histband"></div>
<div class="funnelband" id="funnelband"></div>
<div class="featureband" id="featureband"></div>
<div class="filterbar" id="filterbar"></div>
<div class="modal" id="modal" onclick="closeModal(event)"><div class="modalbox" onclick="event.stopPropagation()">
  <h3 style="margin:0 0 8px;font-size:14px">📋 Weekly digest</h3>
  <textarea id="digesttext" readonly></textarea>
  <div style="margin-top:10px;display:flex;gap:8px"><button class="btn btn-pr btn-mini" onclick="copyDigest()">📑 Copy</button><button class="btn btn-skip btn-mini" onclick="closeModal()">Close</button></div>
</div></div>
<div class="modal" id="modal2" onclick="closeModal2()"><div class="modalbox modalbox-wide" onclick="event.stopPropagation()">
  <h3 id="m2title" style="margin:0 0 8px;font-size:14px"></h3>
  <pre id="m2body" class="viewer"></pre>
  <div style="margin-top:10px"><button class="btn btn-skip btn-mini" onclick="closeModal2()">Close</button></div>
</div></div>
<div class="board" id="board"></div>
<div class="runlog"><h3>Recent run log</h3><ul id="runlog"></ul></div>
<div class="toast" id="toast"></div>
<script>
const fmt = s => { s=Math.max(0,Math.floor(s)); const m=Math.floor(s/60),r=s%60;
  return (m<60? `${m}:${String(r).padStart(2,'0')}` : `${Math.floor(m/60)}h${String(m%60).padStart(2,'0')}m`); };
let S=null, fetchedAt=0, serverNow=0, lastBoardSig='';
const TOKEN="__TOKEN__";   // per-process CSRF token, embedded server-side
const POST_HDRS={'Content-Type':'application/x-www-form-urlencoded','X-CC-Token':TOKEN};
const esc = s => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const srvClock = () => serverNow + (Date.now()-fetchedAt)/1000;   // est. server time, ticking

function badge(t,c){ return t? `<span class="b b-${c}">${esc(t)}</span>`:''; }
function gateCls(g){ return {allow:'ok','review-only':'warn',human:'stop'}[g]||'muted'; }
function acc(list){ if(!list||!list.length) return '';
  return `<details><summary>acceptance criteria (${list.length})</summary><ul class=acc>`+
    list.map(a=>`<li>${esc(a)}</li>`).join('')+`</ul></details>`; }
function post(path, params, ok){
  const b=new URLSearchParams(params).toString();
  fetch(path,{method:'POST',headers:POST_HDRS,body:b})
    .then(r=>r.json()).then(j=>{toast(j.msg||'done'); refresh();}).catch(e=>toast('error: '+e));
}
function toast(m){ const t=document.getElementById('toast'); t.textContent=m; t.classList.add('show');
  clearTimeout(t._t); t._t=setTimeout(()=>t.classList.remove('show'),6000); }

function card(t,kind){
  const cat=badge(t.category, (t.category && ['testing','documentation','dependencies','code_quality','developer_experience'].includes(t.category))?'ok':'warn');
  const gate=badge(t.autopilot, gateCls(t.autopilot));
  const src=t.source?badge(t.source,'muted'):'';
  let live='', timer='', btns='';
  if(kind==='run'){ live='live';
    timer=`<div class="meta">running <span class="timer" data-start="${t._start||''}">…</span>`+
      `<div class="wd"><i data-wd="${t._start||''}" style="width:0%"></i></div></div>`;
    btns=`<button class="btn btn-kill btn-mini" onclick="if(confirm('Stop & skip ${esc(t.id)}?'))post('/killtask',{},1)">■ Stop task</button>`;
  } else if(kind==='next'){
    btns=`<button class="btn btn-go btn-mini" title="run this NEXT + resume the loop" onclick="post('/runnow',{id:'${esc(t.id)}'})">▶ Run next</button>
      <div class=ord>
        <button class="btn btn-mv btn-mini" title="move up (tie-break sooner)" onclick="post('/move',{id:'${esc(t.id)}',dir:'up'})">▲</button>
        <button class="btn btn-mv btn-mini" title="move down" onclick="post('/move',{id:'${esc(t.id)}',dir:'down'})">▼</button></div>
      <button class="btn btn-pr btn-mini" title="raise priority" onclick="post('/priority',{id:'${esc(t.id)}',dir:'up'})">⬆ pri</button>
      <button class="btn btn-pr btn-mini" title="lower priority" onclick="post('/priority',{id:'${esc(t.id)}',dir:'down'})">⬇ pri</button>
      <button class="btn btn-skip btn-mini" onclick="post('/skip',{id:'${esc(t.id)}'})">Skip</button>`;
  } else if(kind==='board'){
    if(t.promotable){
      btns+=`<button class="btn btn-go btn-mini" title="queue it → the loop will work it" onclick="post('/promote',{id:'${esc(t.id)}'})">→ Next up</button>`;
    } else {
      btns+=`<button class="btn btn-force btn-mini" title="human override: hand this security/infra task to the agent" onclick="forceTask('${esc(t.id)}','${esc(t.category)}')">⚠ Force to agent</button>`;
    }
    if(t.lint_hard&&t.lint_hard.length){
      btns+=`<button class="btn btn-skip btn-mini" title="waive the contract-quality gate for this task" onclick="post('/lintok',{id:'${esc(t.id)}'})">🧹 Lint OK</button>`;
    }
    btns+=`<button class="btn btn-mv btn-mini" title="raise priority" onclick="post('/priority',{id:'${esc(t.id)}',dir:'up'})">⬆ pri</button>
      <button class="btn btn-skip btn-mini" onclick="post('/skip',{id:'${esc(t.id)}'})">Skip</button>`;
  } else if(kind==='skipped'){
    btns=`<button class="btn btn-go btn-mini" onclick="post('/unskip',{id:'${esc(t.id)}'})">↺ Un-skip</button>`;
  } else if(kind==='esc'){
    btns=`<button class="btn btn-go btn-mini" title="answer the escalation — the answer joins the contract and the task re-queues" onclick="answerTask('${esc(t.id)}')">💬 Answer &amp; re-queue</button>
      <button class="btn btn-skip btn-mini" onclick="post('/skip',{id:'${esc(t.id)}'})">Skip</button>`;
  } else if(kind==='done'){
    const ref=`${(t.sha||'').slice(0,8)} → ${esc(t.branch||t.remote_ref||'')}`;
    const when=t.committed_at?` · ${t.committed_at} (${relDays(t.committed_at)})`:'';
    btns='';
    if(t.has_packet) btns+=`<button class="btn btn-pr btn-mini" title="the 2-minute review packet (contract + diffstat + verifier notes)" onclick="openText('🔍 Review packet — ${esc(t.id)}','/review?id=${encodeURIComponent(t.id)}',false)">🔍 Review</button>`;
    if(t.has_patch) btns+=`<button class="btn btn-mv btn-mini" title="full diff of the ship" onclick="openText('Δ Diff — ${esc(t.id)}','/diff?id=${encodeURIComponent(t.id)}',true)">Δ Diff</button>`;
    if(t.pr_url) btns+=`<a class="btn btn-pr btn-mini" style="text-decoration:none" href="${esc(t.pr_url)}" target="_blank" rel="noopener" title="prefilled Bitbucket PR-create page">⬆ Open PR</a>`;
    if(!t.merged && !t.rejected){
      btns+=`<button class="btn btn-go btn-mini" title="record that you merged this ship (feeds merge-rate metrics)" onclick="post('/mark',{id:'${esc(t.id)}',outcome:'merged'})">✓ Merged</button>
        <button class="btn btn-kill btn-mini" title="record that you rejected this ship" onclick="rejectTask('${esc(t.id)}')">✗ Reject</button>`;
    }
    if(t.merged) btns+=`<button class="btn btn-rb btn-mini" onclick="if(confirm('Revert ${esc(t.id)} on the loop branch?'))post('/rollback',{id:'${esc(t.id)}'})">↩ Rollback</button>`;
    timer=`<div class="meta">${ref}${when}</div>`;
  }
  const pri = (kind==='next'||kind==='board') ? badge('p:'+(t.priority||'medium'), t.priority==='high'?'warn':'acc') : '';
  const rev = (kind==='done') ? (t.merged?badge('merged','ok'):t.rejected?badge('rejected','stop'):badge('awaiting review','warn')) : '';
  const reason = t.reason? `<div class=reason>🚩 ${esc(t.reason)}</div>`:'';
  const lint = (kind==='board' && ((t.lint_hard&&t.lint_hard.length)||(t.lint_soft&&t.lint_soft.length)))?
    `<div class=lint>🧹 ${esc([...(t.lint_hard||[]).map(x=>'HELD: '+x),...(t.lint_soft||[])].join(' · '))}</div>`:'';
  const note = t.review_note? `<div class=lint>✗ ${esc(t.review_note)}</div>`:'';
  return `<div class="card ${live}">
    <div class="card-h"><span class="tid">${esc(t.id)}</span> ${cat} ${gate} ${pri} ${rev} ${src}</div>
    <div class="title">${esc(t.title)}</div>
    ${timer}${reason}${lint}${note}${acc(t.acceptance_criteria)}
    <div class="btns">${btns}</div></div>`;
}
function col(title,cls,items,kind){
  const body = items.length? items.map(t=>card(t,kind)).join('') : '<div class=empty>—</div>';
  return `<section class="col ${cls}"><h2>${title} <span class=cnt>${items.length}</span></h2>${body}</section>`;
}
function tile(k,v,sub,barPct,barCls){
  const bar = barPct==null?'':`<div class="bar ${barCls||''}"><i style="width:${Math.min(100,barPct)}%"></i></div>`;
  return `<div class="tile"><div class=k>${k}</div><div class="v">${v}</div><div class=sub>${sub||''}</div>${bar}</div>`;
}

function renderHero(){
  const r=S, t=srvClock();
  // loop status pill + button
  const lp=document.getElementById('loopPill'), lb=document.getElementById('loopBtn');
  if(r.stopped){ lp.innerHTML='<span class="dot dot-off"></span> PAUSED'; lb.textContent='▶ Resume loop'; lb.className='btn btn-go'; }
  else if(r.cycle_running){ lp.innerHTML='<span class="dot dot-run"></span> RUNNING a cycle'; lb.textContent='⏸ Pause loop'; lb.className='btn btn-stop'; }
  else { lp.innerHTML='<span class="dot dot-on"></span> ARMED (idle)'; lb.textContent='⏸ Pause loop'; lb.className='btn btn-stop'; }
  if(!r.loop_loaded) lp.innerHTML+=' <span class=off>(daemon not loaded)</span>';
  const fbtn=document.getElementById('feedBtn');
  fbtn.textContent = r.auto_feed?'🔁 Auto-feed: ON':'🔁 Auto-feed: OFF';
  fbtn.className = 'btn '+(r.auto_feed?'btn-go':'btn-skip');

  const tiles=[];
  if(r.cycle_running && r.cycle_start){
    const el=t-r.cycle_start;
    tiles.push(tile('Current cycle', `<span class=timer id=curTimer>${fmt(el)}</span>`,
      'task: '+esc(r.current_task||'(claiming…)'), el/r.cycle_timeout*100, el>r.cycle_timeout*0.8?'warn':''));
  } else {
    tiles.push(tile('Current cycle','—', r.stopped?'loop stopped':'idle — waiting for next tick'));
  }
  const beat = r.heartbeat? fmt(t-r.heartbeat)+' ago':'—';
  tiles.push(tile('Last activity', beat, 'heartbeat from autopilot.log'));
  tiles.push(tile('Daily budget', `${r.daily_count}/${r.max_tasks}`,
    `tasks · $${(r.daily_spend||0).toFixed(2)} est. spend (info only)`,
    r.daily_count/r.max_tasks*100,
    r.daily_count>=r.max_tasks?'warn':'ok'));
  tiles.push(tile('Last cycle', r.last_cycle_dur!=null?fmt(r.last_cycle_dur):'—',
    `interval ${r.interval}s · watchdog ${Math.floor(r.cycle_timeout/60)}m`));
  tiles.push(tile('Avg turnaround', r.avg_cycle_sec!=null?fmt(r.avg_cycle_sec):'—',
    `per task · ${r.cycle_samples||0} cycles`));
  tiles.push(tile('Queue', r.counts.next, `${r.counts.board} on board · ${r.counts.skipped} skipped`));
  const mx=r.metrics||{};
  tiles.push(tile('Shipped (total)', mx.pushed||0, 'commits by the agent'));
  tiles.push(tile('Changes · 7d', mx.changes_7d||0, `+${mx.loc_added_7d||0} / −${mx.loc_removed_7d||0} LOC · ${mx.changes_today||0} today`));
  tiles.push(tile('Escalated', mx.escalated||0, 'auto-skipped (done / unsafe)'));
  document.getElementById('hero').innerHTML=tiles.join('');
  renderStatusline();
}
function renderStatusline(){
  const r=S, t=srvClock(), el=document.getElementById('statusline'); if(!el||!r) return;
  const ships=(r.done||[]).filter(x=>!x.merged&&!x.rejected).length;
  const escN=(r.escalated||[]).length;
  const money=`$${(r.daily_spend||0).toFixed(2)} est.`;
  let s;
  if(r.stopped) s=`⏸ <b>Paused</b> — nothing runs or spends until you resume · ${r.counts.next} task(s) queued · ${money} spent today.`;
  else if(r.cycle_running&&r.cycle_start) s=`▶ <b>Working ${esc(r.current_task||'(claiming…)')}</b> — ${fmt(t-r.cycle_start)} in · ${r.daily_count}/${r.max_tasks} tasks + ${money} today · ${r.counts.next} queued behind it.`;
  else s=`● <b>Armed, idle</b> — next pick within ~${r.interval}s · ${r.daily_count}/${r.max_tasks} tasks + ${money} today · ${r.counts.next} queued.`;
  if(ships||escN) s+=` <b style="color:#974f0c">📥 Needs you: ${escN} escalation(s), ${ships} ship(s) to review.</b>`;
  el.innerHTML=s;
}
function renderInbox(){
  const band=document.getElementById('inboxband'); if(!band||!S) return;
  const escItems=S.escalated||[];
  const ships=(S.done||[]).filter(t=>!t.merged&&!t.rejected);
  const proposed=(S.board||[]).filter(t=>t.status==='proposed').length;
  band.style.display='';
  const n=escItems.length+ships.length;
  if(!n){
    band.className='inboxband zero';
    band.innerHTML=`<div class="histhead"><h3>📥 Needs you</h3><span class="histsum">inbox zero ✓ — ${proposed} proposed task(s) on the board await triage</span></div>`;
    return;
  }
  band.className='inboxband';
  band.innerHTML=`<div class="histhead"><h3>📥 Needs you</h3>
    <span class="histsum"><b>${escItems.length}</b> escalation(s) to answer · <b>${ships.length}</b> ship(s) awaiting review · ${proposed} proposed on the board</span></div>
    <div class="inboxgrid">${escItems.map(t=>card(t,'esc')).join('')+ships.map(t=>card(t,'done')).join('')}</div>`;
}
function answerTask(id){
  const a=prompt('Answer for "'+id+'"\n\nWhat should the agent do / what decision unblocks it?\nYour answer is appended to the task contract and the task RE-QUEUES.');
  if(a&&a.trim()) post('/answer',{id:id,text:a.trim()});
}
function rejectTask(id){
  const note=prompt('Reject "'+id+'" — why? (recorded on the task; tunes what gets auto-fed)');
  if(note!==null) post('/mark',{id:id,outcome:'rejected',note:note||''});
}
function openText(title,url,isDiff){
  fetch(url).then(r=>{ if(!r.ok) throw new Error('HTTP '+r.status); return r.text(); }).then(txt=>{
    document.getElementById('m2title').textContent=title;
    const body=document.getElementById('m2body');
    if(isDiff){
      body.innerHTML=txt.split('\n').map(l=>{
        const e2=esc(l);
        if(l.startsWith('+')&&!l.startsWith('+++')) return `<span class=dl-add>${e2}</span>`;
        if(l.startsWith('-')&&!l.startsWith('---')) return `<span class=dl-del>${e2}</span>`;
        if(l.startsWith('@@')) return `<span class=dl-hunk>${e2}</span>`;
        return e2;
      }).join('\n');
    } else body.textContent=txt;
    document.getElementById('modal2').style.display='flex';
  }).catch(e=>toast('load failed: '+e.message));
}
function closeModal2(){ document.getElementById('modal2').style.display='none'; }
function pollTail(){
  const band=document.getElementById('tailband'); if(!band) return;
  if(!S||!S.cycle_running){ band.style.display='none'; return; }
  fetch('/api/cycletail').then(r=>r.json()).then(j=>{
    band.style.display='';
    document.getElementById('tailfile').textContent=(j.file||'')+' — what the agent is doing right now (5s refresh)';
    const pre=document.getElementById('tailbody');
    const atBottom=pre.scrollHeight-pre.scrollTop-pre.clientHeight<50;
    pre.textContent=(j.lines&&j.lines.length)?j.lines.join('\n'):'(cycle started — waiting for first output…)';
    if(atBottom) pre.scrollTop=pre.scrollHeight;
  }).catch(()=>{});
}
function renderBoard(){
  const r=S, boardEl=document.getElementById('board');
  // Preserve each column's scroll position across the rebuild (otherwise the
  // periodic refresh snaps the long Board column back to the top mid-scroll).
  const scrolls=[...boardEl.querySelectorAll('.col')].map(c=>c.scrollTop);
  const fb=filtered(r.board);
  const blabel='📋 Board'+(activeFilter!=='all'?' · '+esc(activeFilter):'');
  const doneF=r.done.filter(t=>withinRange(t.committed_at));
  const dlabel='✅ Shipped'+(rangeDays<=365?' · '+(rangeDays===1?'today':rangeDays+'d'):'');
  boardEl.innerHTML=
    col('▶ Running','c-run',r.running.map(x=>({...x,_start:r.cycle_start})),'run')+
    col('⏭ Next up','c-next',r.next_up,'next')+
    col(dlabel,'c-done',doneF,'done')+
    col(blabel,'c-board',fb,'board')+
    col('⏸ Skipped','c-skip',r.skipped,'skipped')+
    col('🚩 Escalated','c-esc',r.escalated,'esc');
  [...boardEl.querySelectorAll('.col')].forEach((c,i)=>{ if(scrolls[i]) c.scrollTop=scrolls[i]; });
  renderInbox(); renderFilters(); renderHistory(); renderFunnel(); renderFeatureBuilds();
  document.getElementById('runlog').innerHTML = r.runlog.length?
    r.runlog.map(l=>`<li>${esc(l)}</li>`).join('') : '<li>no runs yet</li>';
}
function boardSig(r){ return JSON.stringify([r.running,r.next_up,r.done,r.board,r.skipped,r.escalated,r.runlog,r.history,r.funnel,r.feature_builds]); }
const FILTERS=[
  {k:'all',label:'All'},
  {k:'bug',label:'🐛 Bugs',test:t=>t.category==='bug'},
  {k:'feature',label:'✨ Features',test:t=>t.category==='feature'},
  {k:'foundry',label:'🔧 Foundry',test:t=>t.source==='maturity-score'},
  {k:'logwatch',label:'📜 Prod-logs',test:t=>t.source==='logwatch'},
  {k:'uitest',label:'🧪 UI-test',test:t=>t.source==='ui-test'},
  {k:'review',label:'🔬 Review',test:t=>['code-review','security-review','network-review'].includes(t.source)},
  {k:'infra',label:'🔒 Infra/Sec',test:t=>['security','infrastructure','release_management','git_practices'].includes(t.category)},
  {k:'docs',label:'📘 Docs/Tests',test:t=>['documentation','testing'].includes(t.category)},
];
let activeFilter='all';
function filtered(items){ const f=FILTERS.find(x=>x.k===activeFilter); return (f&&f.test)?items.filter(f.test):items; }
function setFilter(k){ activeFilter=k; renderBoard(); }
function renderFilters(){
  const r=S, bar=document.getElementById('filterbar');
  bar.innerHTML = FILTERS.map(f=>{
    const n = f.test? r.board.filter(f.test).length : r.board.length;
    return `<button class="chip ${f.k===activeFilter?'on':''}" onclick="setFilter('${f.k}')">${f.label}<b>${n}</b></button>`;
  }).join('') + `<button class="chip chip-go" onclick="promoteAllShown()">⤴ Promote all shown</button>`;
}
function promoteAllShown(){
  const vis=filtered(S.board).filter(t=>t.promotable && t.status==='proposed');
  if(!vis.length){ toast('No promotable (safe, proposed) cards in this filter.'); return; }
  if(!confirm('Promote '+vis.length+' shown task(s) → Next up?')) return;
  Promise.all(vis.map(t=>fetch('/promote',{method:'POST',headers:POST_HDRS,body:'id='+encodeURIComponent(t.id)})))
    .then(()=>{toast('Promoted '+vis.length+' task(s) → Next up.'); refresh();});
}
function toggleFeed(){ if(!S) return; post('/autofeed',{on:S.auto_feed?'0':'1'}); }
function forceTask(id,cat){
  const msg='⚠ '+id+'\n\nThis is a '+cat+' (human-only) task. The agent will ATTEMPT a fix on '
    +((S&&S.base_branch)||'the loop branch')+' for your review (no merge). Some work — secret rotation, '
    +'infra grants, migrations — it cannot do and will escalate back.\n\nForce it to the agent?';
  if(confirm(msg)) post('/force',{id:id});
}
function relSince(ep){ if(!ep) return ''; const s=Math.max(0,Math.floor(Date.now()/1000-ep)),m=Math.floor(s/60); return m<60?m+'m':Math.floor(m/60)+'h'+String(m%60).padStart(2,'0')+'m'; }
function mergeToLoop(branch){
  if(!branch) return;
  const loop=(S&&S.base_branch)||'the loop branch';
  if(confirm('⧉ Merge worktree branch\n\n'+branch+'\n\ninto '+loop+' and push?\n\n'
    +'• runs in an isolated worktree (your checkout is untouched)\n'
    +'• aborts on conflict — nothing is pushed\n'
    +'• never force-pushes'))
    post('/merge-to-loop',{branch:branch});
}
function renderFeatureBuilds(){
  const fb=(S&&S.feature_builds)||[], band=document.getElementById('featureband'); if(!band) return;
  band.style.display='';   // always visible — this is a first-class track, not a strip that vanishes when idle
  const loop=(S&&S.base_branch)||'loop branch';
  const nRun=fb.filter(b=>b.status==='running').length;
  const badge=`<span class="fb-badge">${fb.length} agent${fb.length===1?'':'s'}</span>`
    +(nRun?` <span class="fb-run">● ${nRun} building</span>`:'');
  const head=`<div class="histhead"><h3>🚧 Worktree agents</h3>${badge}`
    +`<span class="histsum">dedicated agents for multi-file features (14–15 acceptance criteria) the atomic loop is too small for — each runs in an isolated git worktree and pushes its OWN branch</span></div>`
    +`<div class="fb-flow">Two tracks: small tasks accumulate on the loop branch <code>${esc(loop)}</code>; big features land on their own branch here. `
    +`Flow: worktree branch → <b>⧉ Merge → loop</b> → <code>${esc(loop)}</code> → <i>(you merge)</i> → <code>phase-2</code>.</div>`;
  if(!fb.length){
    band.innerHTML=head+`<div class="fb-empty">No worktree agents active yet.<br>Register one with <code>python3 feature_build.py start &lt;id&gt; "&lt;title&gt;" &lt;branch&gt;</code>, then <code>done</code>/<code>fail</code> as it ships.</div>`;
    return;
  }
  const SC={running:['#0052cc','● building'],pushed:['#00875a','✓ pushed — review'],failed:['#de350b','✗ failed']};
  const rows=fb.map(b=>{
    const sc=SC[b.status]||['#8993a4',b.status];
    const when=b.status==='running'?('running '+relSince(b.started)):(b.finished?('finished '+relSince(b.finished)+' ago'):'');
    const act=b.merged
      ? `<span class="fb-merged">✓ on loop branch</span>`
      : (b.status==='pushed' && b.branch)
        ? `<button class="fb-mergebtn" onclick="mergeToLoop('${esc(b.branch)}')">⧉ Merge → loop</button>`
        : '';
    return `<div class="fbuild"><span class="fb-dot ${b.status==='running'?'run':''}" style="background:${sc[0]}"></span>
      <div class="fb-main"><div class="fb-title">${esc(b.title)}</div>
        <div class="fb-meta">${esc(b.branch||'')}${b.sha?(' @ '+esc(String(b.sha).slice(0,8))):''} · <span style="color:${sc[0]};font-weight:700">${sc[1]}</span> · ${when}${b.note?(' · '+esc(b.note)):''}</div></div>
      <div class="fb-act">${act}</div></div>`;
  }).join('');
  band.innerHTML=head+rows;
}
const TYPE_COLORS={docs:'#0052cc',tests:'#00875a',feature:'#6554c0',bug:'#de350b',refactor:'#ff8b00',chore:'#8993a4',other:'#97a0af'};
let rangeDays=7;
function setRange(d){ rangeDays=d; renderBoard(); }
function daysList(n){ const out=[],d=new Date(); for(let i=n-1;i>=0;i--){const x=new Date(d);x.setDate(d.getDate()-i);out.push(x.toISOString().slice(0,10));} return out; }
function withinRange(dstr){ if(rangeDays>365) return true; if(!dstr) return false;
  const c=new Date(); c.setHours(0,0,0,0); c.setDate(c.getDate()-(rangeDays-1)); return new Date(dstr+'T00:00:00')>=c; }
function relDays(d){ if(!d) return ''; const n=Math.floor((Date.now()-new Date(d+'T00:00:00'))/864e5); return n<=0?'today':n===1?'1d ago':n+'d ago'; }
function renderHistory(){
  const h=(S&&S.history)||{}, band=document.getElementById('histband'); if(!band) return;
  let span;
  if(rangeDays>365){ const ks=Object.keys(h).sort(); if(ks.length){const arr=[];for(let x=new Date(ks[0]);x<=new Date();x.setDate(x.getDate()+1))arr.push(x.toISOString().slice(0,10));span=arr.slice(-60);} else span=daysList(14); }
  else span=daysList(Math.max(7,rangeDays));
  const maxC=Math.max(1,...span.map(d=>(h[d]||{}).commits||0));
  let totC=0,totA=0,totR=0;
  const bars=span.map(date=>{ const d=h[date]||{commits:0,added:0,removed:0,types:{}};
    totC+=d.commits;totA+=d.added;totR+=d.removed;
    const hpct=d.commits?Math.max(6,d.commits/maxC*100):0;
    const segs=Object.entries(d.types||{}).map(([t,n])=>`<i style="height:${n/Math.max(1,d.commits)*100}%;background:${TYPE_COLORS[t]||TYPE_COLORS.other}"></i>`).join('');
    return `<div class="hday" title="${date}: ${d.commits} commit(s), +${d.added}/−${d.removed} LOC"><div class="hbar" style="height:${hpct}%">${segs}</div><div class="hlbl">${date.slice(5)}</div></div>`;
  }).join('');
  const avg=(totC/span.length).toFixed(1);
  const legend=Object.entries(TYPE_COLORS).map(([t,c])=>`<span><i style="background:${c}"></i>${t}</span>`).join('');
  const chips=[['Today',1],['7d',7],['30d',30],['All',9999]].map(([l,d])=>`<button class="chip ${rangeDays===d?'on':''}" onclick="setRange(${d})">${l}</button>`).join('')
    + `<button class="chip chip-go" onclick="buildDigest()">📋 Weekly digest</button>`;
  band.innerHTML=`<div class="histhead"><h3>📈 Activity</h3>
     <span class="histsum"><b>${totC}</b> commits · <b>+${totA}</b>/<b>−${totR}</b> LOC · <b>${avg}</b>/day · shipped filter: ${rangeDays>365?'all':rangeDays===1?'today':rangeDays+'d'}</span>
     <span class="hrange">${chips}</span></div>
   <div class="hchart">${bars}</div><div class="hlegend">${legend}</div>`;
}
function ftot(c){ return (c.proposed||0)+(c.queued||0)+(c.shipped||0)+(c.merged||0)+(c.rejected||0)+(c.escalated||0)+(c.skipped||0); }
function renderFunnel(){
  const f=(S&&S.funnel)||{}, band=document.getElementById('funnelband'); if(!band) return;
  const STAGES=[['proposed','#97a0af'],['queued','#ff8b00'],['shipped','#00875a'],['merged','#006644'],['rejected','#bf2600'],['escalated','#de350b'],['skipped','#8993a4']];
  const SRC={'maturity-score':'🔧 Foundry','logwatch':'📜 Prod-logs','ui-test':'🧪 QA / UI','backlog':'✍️ Manual','backlog-research':'🔬 Research','other':'• Other'};
  const rows=Object.entries(f).filter(([s,c])=>ftot(c)>0).sort((a,b)=>ftot(b[1])-ftot(a[1])).map(([src,c])=>{
    const segs=STAGES.map(([k,col])=>c[k]?`<i title="${k}: ${c[k]}" style="flex:${c[k]};background:${col}"></i>`:'').join('');
    const nums=STAGES.filter(([k])=>c[k]).map(([k,col])=>`<span style="color:${col}">${c[k]} ${k}</span>`).join(' · ');
    return `<div class="frow"><div class="fsrc">${SRC[src]||esc(src)}</div><div class="fbar">${segs}</div><div class="fnum">${nums}</div></div>`;
  }).join('');
  band.innerHTML=`<div class="histhead"><h3>🔭 Discovery funnel</h3><span class="histsum">source → proposed → queued → shipped → merged/rejected (which source produces MERGED work — the value metric)</span></div>${rows||'<div class=empty>—</div>'}`;
}
function buildDigest(){
  const h=(S&&S.history)||{}, days=daysList(7);
  let c=0,a=0,r=0,types={};
  days.forEach(d=>{const x=h[d]; if(x){c+=x.commits;a+=x.added;r+=x.removed;Object.entries(x.types||{}).forEach(([t,n])=>types[t]=(types[t]||0)+n);}});
  const cut=new Date(); cut.setHours(0,0,0,0); cut.setDate(cut.getDate()-6);
  const ships=(S.done||[]).filter(t=>t.committed_at && new Date(t.committed_at+'T00:00:00')>=cut)
    .map(t=>`  • ${t.title} (${t.committed_at})`);
  const fn=S.funnel||{};
  const disc=Object.entries(fn).filter(([s,x])=>ftot(x)>0).map(([s,x])=>`  ${s}: ${x.merged||0} merged, ${x.shipped||0} awaiting review, ${x.rejected||0} rejected, ${(x.proposed||0)+(x.queued||0)} open`).join('\n');
  const awaiting=(S.done||[]).filter(t=>!t.merged&&!t.rejected).length;
  const mergedN=(S.done||[]).filter(t=>t.merged).length;
  const txt=`📊 Autopilot — last 7 days (as of ${new Date().toISOString().slice(0,10)})
Shipped: ${c} commits, +${a} / −${r} LOC · ${mergedN} merged, ${awaiting} awaiting review
By type: ${Object.entries(types).map(([t,n])=>t+' '+n).join(', ')||'—'}
Avg turnaround: ${S.avg_cycle_sec?fmt(S.avg_cycle_sec):'—'} per task
Spend today: $${(S.daily_spend||0).toFixed(2)} (est., informational)
Branches: per-task autopilot/task-<id> off ${S.base_branch} (review each, merge or reject)

Recent ships:
${ships.slice(0,15).join('\n')||'  (none)'}

Discovery (by source):
${disc||'  —'}`;
  document.getElementById('digesttext').value=txt;
  document.getElementById('modal').style.display='flex';
}
function copyDigest(){ const t=document.getElementById('digesttext'); t.select();
  (navigator.clipboard?navigator.clipboard.writeText(t.value):Promise.reject()).then(()=>toast('Digest copied to clipboard.')).catch(()=>{document.execCommand('copy');toast('Digest copied.');}); }
function closeModal(){ document.getElementById('modal').style.display='none'; }
function tick(){            // 1s ticker — updates live timers without refetch
  if(!S) return; const t=srvClock();
  const ct=document.getElementById('curTimer');
  if(ct && S.cycle_running && S.cycle_start){
    const el=t-S.cycle_start; ct.textContent=fmt(el);
  }
  document.querySelectorAll('.timer[data-start]').forEach(e=>{
    const s=parseFloat(e.dataset.start); if(s) e.textContent=fmt(t-s);});
  document.querySelectorAll('.wd > i[data-wd]').forEach(e=>{
    const s=parseFloat(e.dataset.wd); if(s) e.style.width=Math.min(100,(t-s)/S.cycle_timeout*100)+'%';});
}
function refresh(){
  fetch('/api/state').then(r=>r.json()).then(j=>{
    S=j; serverNow=j.now; fetchedAt=Date.now();
    document.getElementById('stalePill').style.display='none';
    renderHero();
    // Only rebuild the board when its data actually changed — avoids the 3s
    // churn that collapsed open details + fought your scrolling.
    const sig=boardSig(j);
    if(sig!==lastBoardSig){ lastBoardSig=sig; renderBoard(); }
  }).catch(e=>{ document.getElementById('stalePill').style.display='inline-flex'; });
}
function toggleLoop(){ if(!S) return; post(S.stopped?'/resume':'/stop',{}); }
refresh(); setInterval(refresh,3000); setInterval(tick,1000); setInterval(renderHero,1000); pollTail(); setInterval(pollTail,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Never let the browser cache the SPA shell — otherwise a redeploy of the
        # dashboard JS leaves a stale tab (buttons "missing" until a hard refresh).
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        # Exact-match the Host header → a DNS-rebound name resolving to 127.0.0.1
        # carries a different Host and is rejected.
        return self.headers.get("Host", "") in _ALLOWED_HOSTS

    def _origin_ok(self) -> bool:
        # Reject cross-origin POSTs (browsers attach Origin/Referer on them).
        for h in ("Origin", "Referer"):
            v = self.headers.get(h)
            if v and urllib.parse.urlparse(v).netloc not in _ALLOWED_HOSTS:
                return False
        return True

    def do_GET(self):
        if not self._host_ok():
            self._send(b"forbidden host", "text/plain", 403); return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        tid = (qs.get("id", [""])[0]).strip()
        if path == "/":
            shell = SHELL_FILE.read_text() if SHELL_FILE.exists() else SHELL
            body = shell.replace("__BRANCH__", BASE_BRANCH).replace("__TOKEN__", _TOKEN)
            self._send(body.encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(json.dumps(build_state()).encode(), "application/json")
        elif path == "/api/cycletail":
            self._send(json.dumps(cycletail()).encode(), "application/json")
        elif path in ("/review", "/diff"):
            if not tid or not _TID_RE.match(tid):
                self._send(b"invalid id", "text/plain", 400); return
            body = read_packet(tid) if path == "/review" else read_patch(tid)
            if body is None:
                self._send(b"not found for this task", "text/plain", 404); return
            self._send(body.encode(), "text/plain; charset=utf-8")
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        # CSRF / DNS-rebinding guard on the state-changing control plane.
        if not self._host_ok() or not self._origin_ok():
            self._send(b'{"ok":false,"msg":"forbidden host/origin"}', "application/json", 403); return
        if not hmac.compare_digest(self.headers.get("X-CC-Token", ""), _TOKEN):
            self._send(b'{"ok":false,"msg":"missing/invalid token - hard-refresh the dashboard"}', "application/json", 403); return
        length = int(self.headers.get("Content-Length", 0))
        data = urllib.parse.parse_qs(self.rfile.read(length).decode())
        tid = (data.get("id", [""])[0]).strip()
        if tid and not _TID_RE.match(tid):
            self._send(b'{"ok":false,"msg":"invalid task id"}', "application/json", 400); return
        direction = (data.get("dir", [""])[0]).strip()
        path = self.path.split("?")[0]
        try:
            if path == "/stop":
                do_stop(); msg = "Loop PAUSED — the in-flight cycle finishes; no new cycles start. Resume anytime."
            elif path == "/resume":
                do_resume(); msg = "Loop resumed."
            elif path == "/killtask":
                msg = do_killtask()
            elif path == "/skip":
                do_skip(tid); msg = f"Skipped {tid} — moved to the Skipped section."
            elif path == "/unskip":
                msg = do_unskip(tid)
            elif path == "/autofeed":
                msg = do_autofeed((data.get("on", ["0"])[0]).strip() == "1")
            elif path == "/priority":
                msg = do_priority(tid, direction or "up")
            elif path == "/move":
                msg = do_move(tid, direction or "up")
            elif path == "/promote":
                msg = do_promote(tid)
            elif path == "/force":
                msg = do_force(tid)
            elif path == "/runnow":
                msg = do_runnow(tid)
            elif path == "/rollback":
                msg = do_rollback(tid)
            elif path == "/answer":
                msg = do_answer(tid, (data.get("text", [""])[0]).strip())
            elif path == "/mark":
                msg = do_mark(tid, (data.get("outcome", [""])[0]).strip(),
                              (data.get("note", [""])[0]).strip())
            elif path == "/lintok":
                msg = do_lintok(tid)
            elif path == "/merge-to-loop":
                branch = (data.get("branch", [""])[0]).strip()
                if not re.match(r"^[A-Za-z0-9._/-]{1,120}$", branch):
                    self._send(b'{"ok":false,"msg":"invalid branch"}', "application/json", 400); return
                msg = do_merge_to_loop(branch)
            else:
                self._send(b'{"ok":false,"msg":"unknown action"}', "application/json", 404); return
            self._send(json.dumps({"ok": True, "msg": msg}).encode(), "application/json")
        except Exception as e:
            self._send(json.dumps({"ok": False, "msg": f"error: {e}"}).encode(), "application/json", 500)

    def log_message(self, *a):  # quiet
        pass


def main():
    print(f"Command center → http://127.0.0.1:{PORT}  (base branch: {BASE_BRANCH})")
    print("Ctrl-C stops the dashboard only (NOT the loop — use the Stop button / STOP file).")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
