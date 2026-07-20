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
import sys
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
APLOG = LOGDIR / "orbit.log"   # the loop's live log (run.sh writes here). Heartbeat + cycle stats read from it.
CONVERTER = ENGINE / "backlog_to_tasks.py"

# The lifecycle machine + lint vocabulary, in-process (same modules ledger.py
# and the emit gate enforce) — the UI's action buttons and the task form are
# driven by the SAME rules, never a client-side guess.
sys.path.insert(0, str(ENGINE))
import lifecycle  # noqa: E402
from backlog_lint import KNOWN_CATEGORIES  # noqa: E402


def _resolve_base_branch() -> str:
    # Trunk to build on + measure "merged?" against. Precedence: explicit env
    # override (set by run.sh / launchd) → the target's config.yaml (source of
    # truth, same value run.sh uses) → a LOUD 'main' fallback (never silent —
    # a wrong base makes every merge badge lie).
    for var in ("ORBIT_BASE_BRANCH", "AP_BASE_BRANCH"):
        v = os.environ.get(var)
        if v:
            return v
    try:
        cfg = yaml.safe_load((AP_HOME / "config.yaml").read_text()) or {}
        if cfg.get("base_branch"):
            return str(cfg["base_branch"])
    except Exception:
        pass
    print("WARNING: base_branch unresolved (no ORBIT_BASE_BRANCH/AP_BASE_BRANCH env, "
          "no base_branch in config.yaml) — merge detection will be WRONG. "
          "Defaulting to 'main'.", file=sys.stderr)
    return "main"


BASE_BRANCH = _resolve_base_branch()
PORT = int(os.environ.get("PORT", "8787"))
SHELL_FILE = ENGINE / "cc_shell.html"         # the SPA shell — read per request so design edits go live on refresh
REVIEWS = AP_STATE / "reviews"                # per-ship review packets (review_packet.py)
DIFFDIR = AP_STATE / "diffs"                  # wrapper backup patches
# launchd job label (used only to detect whether the daemon is loaded).
LAUNCHD_LABEL = os.environ.get("ORBIT_LAUNCHD_LABEL", "com.orbit.autopilot")
# Dollar spend is informational only — displayed, never a cap (the wrapper's
# only hard daily limit is the task count).
# Git host + repo drive the PR-create and source-browse links. config.yaml is the
# single source of truth (env kept as a fallback for back-compat). reload_settings()
# refreshes these live from config.yaml each state build, so Admin-panel edits show
# up within one poll without restarting the dashboard.
GIT_HOST = "bitbucket"
GIT_REPO = os.environ.get("ORBIT_BB_REPO", "")
BB_PR_NEW = ""
BB_SRC = ""


def _load_cfg() -> dict:
    """Read the target repo's .autopilot/config.yaml (empty dict on any failure)."""
    try:
        return yaml.safe_load((AP_HOME / "config.yaml").read_text()) or {}
    except Exception:
        return {}


def _git_urls(host: str, repo: str, base: str) -> tuple:
    """(source-browse URL, new-PR URL) for the configured git host."""
    if not repo:
        return "", ""
    if host == "github":
        return (f"https://github.com/{repo}/tree/{base}/",
                f"https://github.com/{repo}/compare/{base}?expand=1")
    if host == "gitlab":
        return (f"https://gitlab.com/{repo}/-/tree/{base}",
                f"https://gitlab.com/{repo}/-/merge_requests/new")
    return (f"https://bitbucket.org/{repo}/src/{base}/",
            f"https://bitbucket.org/{repo}/pull-requests/new")


def reload_settings() -> None:
    """Refresh mutable product settings from config.yaml (config-first, env
    fallback). Called at the top of each build_state() so Admin edits reflect live."""
    global BASE_BRANCH, GIT_HOST, GIT_REPO, PREFIX, BB_SRC, BB_PR_NEW
    cfg = _load_cfg()
    BASE_BRANCH = str(cfg.get("base_branch") or os.environ.get("ORBIT_BASE_BRANCH")
                      or os.environ.get("AP_BASE_BRANCH") or "main")
    GIT_HOST = str(cfg.get("git_host") or "bitbucket")
    GIT_REPO = str(cfg.get("git_repo") or os.environ.get("ORBIT_BB_REPO") or "")
    PREFIX = str(cfg.get("branch_prefix") or os.environ.get("ORBIT_BRANCH_PREFIX") or "autopilot")
    BB_SRC, BB_PR_NEW = _git_urls(GIT_HOST, GIT_REPO, BASE_BRANCH)
# Operator guide served at /guide (project copy wins so operators can localize).
GUIDE_FILES = [AP_HOME / "GUIDE.md", ORBIT_HOME / "docs" / "OPERATOR-GUIDE.md"]

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
    """PIDs of the in-flight cycle (claude --print /orbit-cycle + its caffeinate)."""
    try:
        out = subprocess.run(["pgrep", "-f", "orbit-cycle"], capture_output=True, text=True).stdout
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

    # Why the loop is idle + when it resumes (written by run.sh each iteration): "<reason> <resume-epoch>".
    idle_reason, resume_at = None, None
    try:
        parts = (AP_STATE / ".idle-reason").read_text().split()
        idle_reason = parts[0]
        resume_at = int(parts[1]) if len(parts) > 1 else None
    except Exception:
        pass

    # Task identity: the agent's authoritative claim (.current-task-id) wins; until
    # it lands (minutes into a cycle) fall back to run.sh's provisional .cycle-candidate
    # so the dashboard names the task in seconds. tentative=True flags the fallback so
    # the UI can mark it as still-confirming.
    claimed = MARKER.read_text().strip() if MARKER.exists() else None
    cand_file = AP_STATE / ".cycle-candidate"
    candidate = cand_file.read_text().strip() if cand_file.exists() else None
    current_task = claimed or candidate

    return {
        "now": time.time(),
        "loop_loaded": loop_loaded(),
        "stopped": STOP.exists(),
        "stop_note": (STOP.read_text().strip() if STOP.exists() else None),
        "cycle_running": running,
        "cycle_start": start if running else None,
        "current_task": current_task,
        "current_task_tentative": bool(current_task and not claimed),
        "heartbeat": heartbeat,
        "idle_reason": idle_reason,
        "resume_at": resume_at,
        "last_cycle_dur": last_dur,
        "avg_cycle_sec": avg_cycle,
        "cycle_samples": len(durs),
        "daily_count": daily,
        "daily_spend": round(spend, 2),
        "max_tasks": MAX_TASKS,
        "interval": INTERVAL,
        "cycle_timeout": CYCLE_TIMEOUT,
        "base_branch": BASE_BRANCH,
        "bb_src": BB_SRC,
        "categories": sorted(KNOWN_CATEGORIES),   # the task form's vocabulary — same set the lint gate accepts
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


def _next_up_order() -> list[tuple[str, str]]:
    """The Next-up column's exact display order: queue.json filtered the way
    build_state filters it (worked/skipped dropped, human-only held unless
    forced), stable-sorted by priority. Returns (id, priority) pairs so
    do_move can tell a tie-break move from a priority-band crossing."""
    worked = set(load_ledger().keys())
    skips = load_skips()
    rows = []
    for q in load_queue():
        tid = str(q.get("id"))
        if tid in worked or tid in skips:
            continue
        if q.get("category") not in EMITTABLE and not q.get("forced"):
            continue
        rows.append((tid, q.get("priority", "medium")))
    rows.sort(key=lambda r: PRIORITY_RANK.get(r[1], 1))
    return rows


def do_move(tid: str, direction: str) -> str:
    """Reorder within the Next-up column AS DISPLAYED. The column is a filtered,
    priority-sorted view of backlog.yaml, so two cards adjacent on screen are
    usually many blocks apart in the file — swapping raw file neighbors (the old
    behavior) moved the task past an invisible done/proposed block and the
    visible order never changed. Instead: find the on-screen neighbor and
    relocate this task's block just past the neighbor's block."""
    header, blocks, ids = _read_blocks()
    if tid not in ids:
        return f"Task {tid} not found."
    order = _next_up_order()
    disp_ids = [r[0] for r in order]
    if tid in disp_ids:
        k = disp_ids.index(tid)
        nk = k - 1 if direction == "up" else k + 1
        if nk < 0 or nk >= len(order):
            return f"{tid} is already at the {'top' if direction=='up' else 'bottom'}."
        n_tid, n_pri = order[nk]
        pri = order[k][1]
        if PRIORITY_RANK.get(n_pri, 1) != PRIORITY_RANK.get(pri, 1):
            # Order is a tie-break WITHIN a priority band — the picker sorts by
            # priority first, so no amount of reordering crosses a band.
            return (f"{tid} is at the {'top' if direction == 'up' else 'bottom'} of its "
                    f"'{pri}' priority band — use {'⬆ pri' if direction == 'up' else '⬇ pri'} "
                    f"to move it {'sooner' if direction == 'up' else 'later'}.")
        if n_tid in ids:
            i = ids.index(tid)
            blk = blocks.pop(i)
            ids.pop(i)
            j = ids.index(n_tid)
            at = j if direction == "up" else j + 1
            blocks.insert(at, blk)
            _write_blocks(header, blocks)
            _run_converter()
            return f"Moved {tid} {direction} (now {'before' if direction == 'up' else 'after'} {n_tid})."
    # Fallback (task not in the Next-up view, e.g. queue.json missing): the old
    # raw file-neighbor swap, so the button still does something sensible.
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


# ── task CRUD from the dashboard ─────────────────────────────────────────────
_TID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


def _block_indent(blocks: list) -> str:
    # Match the file's existing list-item convention (flat "- id:" vs legacy
    # "  - id:") — mixing indents under one `tasks:` key is invalid YAML.
    return "  " if (blocks and blocks[0].startswith("  - id:")) else ""


def _strip_field(block: str, field: str) -> str:
    # Remove a field line PLUS its nested continuation lines (literal blocks,
    # list items) — the multi-line counterpart of _set_field.
    out, skipping = [], None
    for ln in block.splitlines(keepends=True):
        m = re.match(rf"^(\s*){re.escape(field)}:", ln)
        if m and skipping is None:
            skipping = len(m.group(1))
            continue
        if skipping is not None:
            if not ln.strip() or (len(ln) - len(ln.lstrip())) > skipping:
                continue
            skipping = None
        out.append(ln)
    return "".join(out)


def _upsert_scalar(block: str, field: str, value: str, fi: str) -> str:
    if re.search(rf"(?m)^  (?:  )?{re.escape(field)}: ", block):
        return _set_field(block, field, value)
    return re.sub(r"(?m)^((?:  )?- id: .*)$",
                  lambda m: f"{m.group(1)}\n{fi}{field}: {value}", block, count=1)


def _split_acc(raw: str) -> list:
    return [a.strip() for a in (raw or "").splitlines() if a.strip()]


def do_task_new(tid: str, title: str, category: str, priority: str = "medium",
                context: str = "", acc_raw: str = "", queue_now: bool = False) -> str:
    """Create a backlog task from the dashboard — same contract bar as the lint
    hard gate (category must be known, acceptance criteria required), so a task
    born in the UI can never be silently held from the queue."""
    tid = (tid or "").strip().lower()
    if not _TID_RE.match(tid):
        return "Task id must be a lowercase slug (letters/digits/._-, max 80 chars)."
    if not (title or "").strip():
        return "A title is required."
    if category not in KNOWN_CATEGORIES:
        return f"Unknown category '{category}' — one of: {', '.join(sorted(KNOWN_CATEGORIES))}."
    acc = _split_acc(acc_raw)
    if not acc:
        return ("At least one acceptance criterion is required (one per line) — "
                "without it the verifier has nothing objective to grade and the "
                "lint gate would hold the task from the queue anyway.")
    header, blocks, ids = _read_blocks()
    if tid in ids:
        return f"Task {tid} already exists on the backlog — edit it instead."
    if load_ledger().get(tid):
        return f"Task id '{tid}' was already worked (see Shipped/Escalated) — pick a fresh id."
    ind = _block_indent(blocks)
    status = "queued" if queue_now else "proposed"
    gate = "allow" if (queue_now and category in EMITTABLE) else "human"
    lines = [f"{ind}- id: {tid}",
             f"{ind}  title: {json.dumps(' '.join(title.split()))}",
             f"{ind}  category: {category}",
             f"{ind}  autopilot: {gate}",
             f"{ind}  status: {status}",
             f"{ind}  priority: {priority if priority in ('high', 'medium', 'low') else 'medium'}",
             f"{ind}  source: dashboard"]
    ctx = (context or "").strip()
    if ctx:
        lines.append(f"{ind}  context: |")
        lines += [f"{ind}    {ln}" for ln in ctx.splitlines()]
    lines.append(f"{ind}  acceptance_criteria:")
    lines += [f"{ind}    - {json.dumps(a)}" for a in acc]
    blocks.append("\n".join(lines) + "\n")
    _write_blocks(header, blocks)
    _run_converter()
    if status == "queued" and gate == "allow":
        return f"Created {tid} → ⏭ Next up (the loop can pick it)."
    if status == "queued":
        return f"Created {tid} as queued — '{category}' is human-only, so the loop won't touch it."
    return f"Created {tid} → 📋 Board as proposed. Promote it when it's ready for the loop."


def do_task_edit(tid: str, title: str = "", category: str = "", priority: str = "",
                 context: str = "", acc_raw: str = "") -> str:
    """Edit a backlog task's contract in place, comment-preserving. Empty form
    fields mean 'leave unchanged' — the UI can't blank a field by accident."""
    header, blocks, ids = _read_blocks()
    if tid not in ids:
        return f"Task {tid} not found on the backlog."
    i = ids.index(tid)
    b = blocks[i]
    fi = _block_indent(blocks) + "  "
    changed = []
    if (title or "").strip():
        b = _upsert_scalar(b, "title", json.dumps(" ".join(title.split())), fi)
        changed.append("title")
    if category:
        if category not in KNOWN_CATEGORIES:
            return f"Unknown category '{category}'."
        b = _upsert_scalar(b, "category", category, fi)
        changed.append("category")
    if priority in ("high", "medium", "low"):
        b = _upsert_scalar(b, "priority", priority, fi)
        changed.append("priority")
    if (context or "").strip():
        b = _strip_field(b, "context")
        if not b.endswith("\n"):
            b += "\n"
        b += f"{fi}context: |\n" + "".join(f"{fi}  {ln}\n" for ln in context.strip().splitlines())
        changed.append("context")
    acc = _split_acc(acc_raw)
    if acc:
        b = _strip_field(b, "acceptance_criteria")
        if not b.endswith("\n"):
            b += "\n"
        b += f"{fi}acceptance_criteria:\n" + "".join(f"{fi}  - {json.dumps(a)}\n" for a in acc)
        changed.append("acceptance criteria")
    if not changed:
        return f"Nothing to change on {tid} (empty fields keep current values)."
    blocks[i] = b
    _write_blocks(header, blocks)
    _run_converter()
    return f"Updated {tid} ({', '.join(changed)})."


def do_task_delete(tid: str) -> str:
    """Delete an UN-WORKED task's block from the backlog. Worked tasks are
    refused — their block is the contract behind a real ship/escalation; use
    Skip to set them aside instead."""
    header, blocks, ids = _read_blocks()
    if tid not in ids:
        return f"Task {tid} not found on the backlog."
    led = load_ledger().get(tid) or {}
    if led.get("state"):
        return (f"Won't delete {tid}: it was worked (ledger state '{led['state']}') — "
                f"the block documents a real ship/escalation. Skip it instead.")
    blocks.pop(ids.index(tid))
    _write_blocks(header, blocks)
    if tid in load_skips():
        kept = [ln for ln in SKIPS.read_text().splitlines() if ln.strip() != tid]
        SKIPS.write_text("\n".join(kept) + ("\n" if kept else ""))
    _run_converter()
    return f"Deleted {tid} from the backlog."


def do_reap() -> str:
    """Manually reap tasks stuck mid-cycle (the wrapper does this each
    iteration, but a paused loop never iterates — this button covers that)."""
    r = subprocess.run(["python3", str(ENGINE / "ledger.py"), "reap", str(CYCLE_TIMEOUT + 600)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return f"Reap failed: {r.stderr.strip()}"
    reaped = r.stdout.strip()
    return (f"Reaped {reaped.count(' ') + 1} stalled task(s) → Escalated: {reaped}" if reaped
            else "Nothing stalled — no mid-cycle entry is older than the cycle timeout.")


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


def do_rollback(tid: str, note: str = "") -> str:
    led = load_ledger().get(tid, {})
    sha = led.get("sha")
    if not sha:
        return f"No recorded commit sha for task {tid} — nothing to roll back."
    if not note.strip():
        return "A revert reason is required — it's recorded on the task and mined to tune the loop."
    # Lifecycle gate BEFORE any git side effect: reverting an unshipped task is
    # refused here, not discovered after a git revert has already run.
    chk = subprocess.run(["python3", str(ENGINE / "ledger.py"), "can", tid, "reverted"],
                         capture_output=True, text=True)
    if chk.returncode != 0:
        why = (chk.stderr.strip() or chk.stdout.strip()).splitlines()[-1] if (chk.stderr or chk.stdout) else "lifecycle check failed"
        return f"Won't roll back {tid}: {why}"
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
    rec = subprocess.run(["python3", str(ENGINE / "ledger.py"), "reverted", tid, note],
                         capture_output=True, text=True)
    warn = "" if rec.returncode == 0 else f" (WARNING: revert done but not recorded in the ledger: {rec.stderr.strip()})"
    return f"Rolled back task {tid} (reverted {sha[:8]}) and pushed to origin/{BASE_BRANCH}.{warn}"


_MERGED_CACHE = {"t": 0.0, "v": {}}

PREFIX = os.environ.get("ORBIT_BRANCH_PREFIX", "autopilot")

_FETCH_CACHE = {"t": 0.0}
_BRANCHES_CACHE = {"t": 0.0, "v": []}
_ANCESTRY_CACHE = {"t": 0.0, "v": set()}


def _git(*args, timeout=15):
    # Thin wrapper: git in the target repo, captured, never raises on non-zero.
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True, timeout=timeout)


def _fetch_prune():
    # Refresh remote-tracking refs (and drop refs deleted on origin) so branch
    # status is accurate even while the loop is paused. Cached 60s. --prune
    # never deletes REMOTE branches, only stale local tracking refs.
    now = time.time()
    if now - _FETCH_CACHE["t"] < 60:
        return
    try:
        _git("fetch", "--prune", "origin", "--quiet")
    except Exception:
        pass
    _FETCH_CACHE["t"] = now


def remote_branches() -> list:
    # [(name, tip_sha, committer_ts)] for origin/<PREFIX>/* — from local
    # remote-tracking refs (fresh via _fetch_prune), one for-each-ref call.
    now = time.time()
    if now - _BRANCHES_CACHE["t"] < 60:
        return _BRANCHES_CACHE["v"]
    _fetch_prune()
    out = []
    try:
        r = _git("for-each-ref", "--format=%(refname:short) %(objectname) %(committerdate:unix)",
                 f"refs/remotes/origin/{PREFIX}/")
        for ln in r.stdout.splitlines():
            parts = ln.split()
            if len(parts) != 3:
                continue
            short, sha, ts = parts  # short = "origin/autopilot/task-x"
            name = short[len("origin/"):] if short.startswith("origin/") else short
            try:
                out.append((name, sha, int(ts)))
            except ValueError:
                continue
    except Exception:
        out = []
    _BRANCHES_CACHE.update(t=now, v=out)
    return out


def trunk_ancestry() -> set:
    # Full SHAs reachable from origin/<BASE_BRANCH>. A branch tip in this set is
    # merged. Same technique as merged_map, shared here. Cached 60s.
    now = time.time()
    if now - _ANCESTRY_CACHE["t"] < 60:
        return _ANCESTRY_CACHE["v"]
    s = set()
    try:
        r = _git("rev-list", "-8000", f"origin/{BASE_BRANCH}")
        s = set(r.stdout.split())
    except Exception:
        s = set()
    _ANCESTRY_CACHE.update(t=now, v=s)
    return s


_TS_SUFFIX = re.compile(r"-\d{8}T\d{6}$")


def _task_id_from_branch(name: str) -> str:
    # "autopilot/task-foo-bar" -> "foo-bar"; strips a trailing -<TIMESTAMP> re-run suffix.
    base = name[len(PREFIX) + 1:] if name.startswith(PREFIX + "/") else name
    if base.startswith("task-"):
        base = base[len("task-"):]
    return _TS_SUFFIX.sub("", base)


def branch_reconcile(branches, ancestry, ledger, now_ts) -> list:
    # Join origin/<PREFIX>/* branches to the ledger and categorize each.
    # Pure: all inputs injected. Category order: awaiting > merged > rejected > orphan.
    rows = []
    for name, tip, ts in branches:
        tid = _task_id_from_branch(name)
        # Ledger keys are bare task ids (no "task-" prefix) — match _task_id_from_branch's output.
        entry = ledger.get(tid, {})
        entry_ref = (entry.get("remote_ref") or "").removeprefix("origin/") or entry.get("branch") or ""
        is_current_ref = bool(entry) and entry_ref == name
        state = entry.get("state") if is_current_ref else None
        merged = (tip in ancestry) or (is_current_ref and state == "merged")
        is_orphan = (not entry) or (not is_current_ref)

        if state == "pushed" and not merged:
            category = "awaiting"
        elif merged:
            category = "merged"
        elif state == "rejected":
            category = "rejected"
        else:
            category = "orphan"

        rows.append({
            "branch": name,
            "task_id": tid,
            "tip": tip,
            "merged": merged,
            "ledger_state": state,
            "is_current_ref": is_current_ref,
            "is_orphan": is_orphan,
            "age_days": max(0, (now_ts - ts) // 86400),
            "category": category,
        })
    return rows


def bust_branch_caches():
    _FETCH_CACHE["t"] = 0.0
    _BRANCHES_CACHE["t"] = 0.0
    _ANCESTRY_CACHE["t"] = 0.0


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


def do_mark(tid: str, outcome: str, note: str = "", force: bool = False) -> str:
    """Record the human review outcome (merged / rejected) in the ledger — the
    raw data behind merge-rate-per-source. A reject REQUIRES a reason: it is
    the loop's only ground-truth on why a ship was wrong (the calibration
    miner learns from it). Reject keeps the branch on origin
    (delete it manually if you want); the id stays worked so it isn't re-picked.
    force=True passes --force past the lifecycle gate — reason required, and the
    ledger stamps forced:true so the override stays auditable."""
    if outcome not in ("merged", "rejected"):
        return f"Invalid outcome '{outcome}'."
    if outcome == "rejected" and not note.strip():
        return "A reject reason is required — it's recorded on the task and mined to tune the loop."
    if force and not note.strip():
        return "Forcing past the lifecycle gate requires a reason — it's recorded on the entry."
    cmd = ["python3", str(ENGINE / "ledger.py"), "mark", tid, outcome, note]
    if force:
        cmd.append("--force")
    r = subprocess.run(cmd, capture_output=True, text=True)
    _MERGED_CACHE.update(t=0.0)   # re-check ancestry on the next poll
    if r.returncode != 0:
        return f"ledger mark failed: {r.stderr.strip()}"
    if outcome == "merged":
        # Merge marker (best-effort): the anchor signal adapters use to attribute
        # a production regression to the ship that likely caused it.
        try:
            led = load_ledger().get(tid, {})
            marker = {"task_id": tid, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "sha": led.get("sha") or "", "branch": led.get("branch") or "",
                      "pr_url": led.get("pr_url") or "", "patch": led.get("patch") or ""}
            with (AP_STATE / "merge_markers.jsonl").open("a") as f:
                f.write(json.dumps(marker) + "\n")
        except OSError:
            pass
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
    # Legacy status "pushed" → "built" (renamed: it collided with the ledger's
    # `pushed` lifecycle state — different store, different machine).
    if not FEATURE_BUILDS.exists():
        return []
    try:
        builds = json.loads(FEATURE_BUILDS.read_text()).get("builds", []) or []
    except Exception:
        return []
    for b in builds:
        if b.get("status") == "pushed":
            b["status"] = "built"
    return builds


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


def do_feature_start(tid: str, title: str, branch: str) -> str:
    """Kick off a dedicated feature-agent build in its own worktree.

    Delegates to engine/feature_build.py, which registers the build in
    feature_builds.json, carves a worktree off origin/<base_branch>, runs the
    agent headless, and pushes the work to `branch` for review. Returns fast —
    the actual build runs detached.
    """
    title = (title or tid)[:200]
    branch = branch or f"feature/{tid}"
    try:
        r = subprocess.run(
            [sys.executable, str(ENGINE / "feature_build.py"), "start", tid, title, branch],
            capture_output=True, text=True, timeout=30)
    except Exception as e:
        return f"Could not start feature agent: {e}"
    if r.returncode != 0:
        return f"Could not start feature agent: {(r.stderr or r.stdout).strip()[:200]}"
    return (r.stdout or "Feature agent started.").strip()


# Planning-tier actions the dashboard may trigger. Stage legality mirrors
# epic_plan.TRANSITIONS (the CLI re-checks anyway — this is the fast-fail).
_EPIC_ACTIONS = {"plan": {"proposed", "spec_ready"},
                 "approve": {"spec_ready"},
                 "decompose": {"approved"}}


def do_epic_action(tid: str, action: str) -> str:
    """Drive the planning tier from the dashboard. Approve is synchronous (a
    pure human act, no model). plan/decompose spawn epic_plan.py DETACHED —
    they run a headless agent for minutes; the card's stage (set immediately
    by epic_plan) is the progress indicator."""
    if action not in _EPIC_ACTIONS:
        return f"unknown epic action '{action}'"
    t = next((x for x in load_backlog() if str(x.get("id")) == tid), None)
    if not t or t.get("category") != "epic":
        return f"'{tid}' is not an epic"
    stage = t.get("status") or "proposed"
    if stage not in _EPIC_ACTIONS[action]:
        return f"cannot {action} from stage '{stage}'"
    script = str(ENGINE / "epic_plan.py")
    if action == "approve":
        r = subprocess.run([sys.executable, script, str(REPO), "approve", tid],
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout or r.stderr or "").strip()
        return out.splitlines()[-1] if out else f"approved {tid}"
    log = AP_STATE / "logs" / f"epic-{tid}-{action}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as lf:
        subprocess.Popen([sys.executable, script, str(REPO), action, tid],
                         stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    who = "Planner" if action == "plan" else "Decomposer"
    return f"{who} launched for {tid} — the card's stage updates as it works (log: logs/{log.name})"


def load_calibration_candidates() -> list:
    """Pending calibration-miner candidates for the dashboard's strip."""
    out = []
    d = AP_HOME / "goldens" / "candidates"
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            try:
                c = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            out.append({k: c.get(k) for k in ("id", "kind", "task_id", "category",
                                              "title", "card_line", "evidence", "mined_at")})
    return out


def do_calibration_action(cid: str, action: str, reason: str = "") -> str:
    """Approve/reject a mined calibration candidate — the ONLY path by which a
    learned lesson starts being briefed into agents (approve appends its card
    line to goldens/LEARNED.md; the cycle reads that alongside the engine's
    calibration). Reject requires a reason and archives it — miner input."""
    if not re.match(r"^[A-Za-z0-9._-]{1,120}$", cid):
        return "invalid candidate id"
    gold = AP_HOME / "goldens"
    src = gold / "candidates" / f"{cid}.json"
    if not src.exists():
        return f"no pending candidate '{cid}'"
    try:
        c = json.loads(src.read_text())
    except (OSError, ValueError):
        return f"candidate '{cid}' is unreadable"
    if action == "approve":
        learned = gold / "LEARNED.md"
        cat = (c.get("category") or "").strip() or "always"
        text = learned.read_text() if learned.exists() else (
            "# Learned calibration — THIS repo's graded lessons, mined from its own ledger\n"
            "\n"
            "Approved on the dashboard's Calibration strip; the cycle briefs the `always`\n"
            "section plus the task category's section ALONGSIDE the engine's calibration.\n"
            "Every line cites the ledger evidence it came from (goldens/approved/). Prune\n"
            "lines that stop earning their keep — `orbit report` shows whether the\n"
            "rejected/reverted/escalated curves actually bend.\n")
        marker = f"## {cat}"
        lines = text.splitlines()
        if marker in lines:
            lines.insert(lines.index(marker) + 1, f"- {c.get('card_line') or ''}")
            text = "\n".join(lines) + "\n"
        else:
            text = text.rstrip("\n") + f"\n\n{marker}\n- {c.get('card_line') or ''}\n"
        learned.write_text(text)
        (gold / "approved").mkdir(parents=True, exist_ok=True)
        src.rename(gold / "approved" / src.name)
        return f"Approved {cid} → goldens/LEARNED.md (briefed from the next cycle)."
    if action == "reject":
        if not reason.strip():
            return "A rejection reason is required — it's archived with the candidate and feeds the miner."
        c["rejected_reason"] = reason
        (gold / "rejected").mkdir(parents=True, exist_ok=True)
        (gold / "rejected" / src.name).write_text(json.dumps(c, indent=2) + "\n")
        src.unlink()
        return f"Rejected {cid} (archived with reason)."
    return f"unknown calibration action '{action}'"


def do_delete_branch(branch: str) -> str:
    # Delete a single remote branch. HARD guardrail: only <PREFIX>/* refs — named
    # / team branches can never be deleted from this UI. Per-branch delete is
    # allowed for ANY category (the UI gates unmerged ones behind a confirm).
    if not branch.startswith(PREFIX + "/"):
        return f"Refused: '{branch}' is not under '{PREFIX}/' — only orbit task branches are deletable here."
    r = _git("push", "origin", "--delete", branch)
    bust_branch_caches()
    if r.returncode != 0:
        return f"Delete of '{branch}' FAILED:\n{r.stderr.strip()}"
    return f"Deleted origin/{branch}."


def do_delete_branches_bulk(kind: str) -> str:
    # Bulk delete only branches the SERVER classifies as `kind` (merged|rejected).
    # Never trusts the client; awaiting-review and orphan branches are never
    # touched here — those are per-branch, confirmed deletes only.
    if kind not in ("merged", "rejected"):
        return f"Refused: bulk delete only supports merged|rejected (got '{kind}')."
    rows = branch_reconcile(remote_branches(), trunk_ancestry(), load_ledger(), int(time.time()))
    targets = [r["branch"] for r in rows
               if r["category"] == kind and r["branch"].startswith(PREFIX + "/")]
    deleted, failed = [], []
    for b in targets:
        if _git("push", "origin", "--delete", b).returncode == 0:
            deleted.append(b)
        else:
            failed.append(b)
    bust_branch_caches()
    tail = f" ({len(failed)} failed)" if failed else ""
    return f"Deleted {len(deleted)} {kind} branch(es){tail}."


# ── admin / settings (config.yaml is the portable single source of truth) ─────
CATEGORIES_ALL = ["bug", "feature", "refactor", "code_quality", "testing", "documentation",
                  "dependencies", "developer_experience", "security", "infrastructure",
                  "release_management", "git_practices"]
SOURCES_ALL = ["backlog", "backlog-research", "maturity-score", "logwatch", "ui-test",
               "code-review", "security-review", "network-review"]
HOSTS_ALL = ["bitbucket", "github", "gitlab"]
_PREFIX_RE = re.compile(r"^[A-Za-z0-9._/-]{1,60}$")


def _dashboard_label() -> str:
    """This dashboard's launchd label, derived from the loop label."""
    if LAUNCHD_LABEL.startswith("com.orbit.orbit-"):
        return "com.orbit.dashboard-" + LAUNCHD_LABEL[len("com.orbit.orbit-"):]
    return "com.orbit.dashboard-" + REPO.name


def config_payload() -> dict:
    """Full config.yaml + resolved runtime meta for the Admin panel."""
    reload_settings()
    src, pr = _git_urls(GIT_HOST, GIT_REPO, BASE_BRANCH)
    return {
        "config": _load_cfg(),
        "meta": {
            "ap_home": str(AP_HOME), "repo": str(REPO), "port": PORT,
            "loop_label": LAUNCHD_LABEL, "dashboard_label": _dashboard_label(),
            "base_branch": BASE_BRANCH, "git_host": GIT_HOST, "git_repo": GIT_REPO,
            "git_src_url": src, "git_pr_url": pr,
            "categories_all": CATEGORIES_ALL, "sources_all": SOURCES_ALL, "hosts_all": HOSTS_ALL,
        },
    }


def _validate_config(d: dict) -> list:
    """Return a list of human-readable errors for an incoming config patch (empty = ok)."""
    errs = []
    def _posint(k):
        if k in d and (not isinstance(d[k], int) or d[k] <= 0):
            errs.append(f"{k} must be a positive integer")
    def _nonempty(k):
        if k in d and (not isinstance(d[k], str) or not d[k].strip()):
            errs.append(f"{k} must be a non-empty string")
    _nonempty("base_branch"); _nonempty("model"); _nonempty("permission_mode"); _nonempty("repo")
    _posint("interval_seconds"); _posint("max_tasks_per_day"); _posint("cycle_timeout_seconds")
    if "git_host" in d and d["git_host"] not in HOSTS_ALL:
        errs.append("git_host must be one of " + ", ".join(HOSTS_ALL))
    if "pull_requests" in d and d["pull_requests"] not in ("off", "github"):
        errs.append('pull_requests must be "off" or "github"')
    if "branch_prefix" in d and not _PREFIX_RE.match(str(d.get("branch_prefix", ""))):
        errs.append("branch_prefix has invalid characters")
    for k in ("sources", "workable_categories"):
        if k in d and not (isinstance(d[k], list) and all(isinstance(x, str) for x in d[k])):
            errs.append(f"{k} must be a list of strings")
    if "gates" in d:
        g = d["gates"]
        if not isinstance(g, dict):
            errs.append("gates must be an object of name -> {cmd, cwd, needs}")
        else:
            for name, spec in g.items():
                if not isinstance(spec, dict) or not str(spec.get("cmd", "")).strip():
                    errs.append(f"gate '{name}' needs a non-empty cmd")
    return errs


def _write_config(cfg: dict) -> None:
    """Atomically write config.yaml with a generated header (the Admin panel owns it)."""
    header = ("# Orbit config for this target repo — the single source of truth for\n"
              "# both the loop and the dashboard. Edited via the dashboard Admin panel.\n\n")
    tmp = AP_HOME / "config.yaml.tmp"
    tmp.write_text(header + yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False, allow_unicode=True))
    tmp.replace(AP_HOME / "config.yaml")


def do_save_config(raw: str) -> str:
    """Validate an incoming JSON patch, merge it into config.yaml, and persist.
    Keys not present in the patch are preserved (gates/env/etc. survive)."""
    try:
        incoming = json.loads(raw or "{}")
    except Exception as e:
        return f"Invalid JSON: {e}"
    if not isinstance(incoming, dict):
        return "Config payload must be an object."
    errs = _validate_config(incoming)
    if errs:
        return "Rejected — " + "; ".join(errs)
    cfg = _load_cfg()
    cfg.update(incoming)   # merge: preserves keys the UI did not send
    _write_config(cfg)
    reload_settings()
    return "✓ Saved config.yaml. Use 'Restart loop' to apply it to the running loop."


def do_restart(label: str, delay: float = 0) -> str:
    """Restart an orbit launchd service via kickstart. Guarded to com.orbit.* labels.
    A delay lets the HTTP response return before the dashboard restarts itself."""
    if not label.startswith("com.orbit."):
        return f"Refused: '{label}' is not an orbit service."
    target = f"gui/{os.getuid()}/{label}"
    if delay:
        # Detached delayed kickstart (no shell) so the HTTP response returns before
        # this process is restarted out from under it.
        subprocess.Popen(
            [sys.executable, "-c",
             f"import time,subprocess;time.sleep({float(delay)});"
             f"subprocess.run(['launchctl','kickstart','-k',{target!r}])"],
            start_new_session=True)
        return f"Restarting {label} in ~{int(delay)}s — the dashboard will blink; hard-refresh after."
    r = subprocess.run(["launchctl", "kickstart", "-k", target], capture_output=True, text=True)
    if r.returncode != 0:
        return f"Restart of {label} failed: {(r.stderr or r.stdout).strip()[:200]}"
    return f"✓ Restarted {label}."


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


def _on_board(t: dict) -> bool:
    """Deep-space (board) membership: proposals and human/review-gated work
    that is still OPEN. `worked` only covers loop-shipped ids, so a task marked
    status:done in the backlog (shipped by a human outside the loop — never in
    the ledger) must be dropped here or it haunts the board forever."""
    if t.get("status") == "done":
        return False
    return t.get("autopilot") in ("review-only", "human") or t.get("status") == "proposed"


def build_state() -> dict:
    reload_settings()   # config.yaml is the source of truth — reflect Admin edits live
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
                # Plain-language enrichment (optional backlog fields) — lets a
                # non-engineer owner judge each card without reading code.
                "plain": meta.get("plain", ""), "impact": meta.get("impact", ""),
                "risk_if_skipped": meta.get("risk_if_skipped", ""),
                # Provenance for intake/signal leads (the WHY + file:line evidence)
                # and the epic a decomposed child belongs to.
                "context": meta.get("context", ""), "epic": meta.get("epic", ""),
                "effort": meta.get("effort", ""), "decision_hint": meta.get("decision_hint", ""),
                "docs": [d for d in (meta.get("docs") or []) if isinstance(d, str)],
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
                    # Escalation decision support (written by the operator/reconcile):
                    # a plain-language brief + a concrete recommended action.
                    "operator_brief": e.get("operator_brief", ""),
                    "recommendation": e.get("recommendation", ""),
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
                # Lifecycle-driven action flags: the SAME machine ledger.py
                # enforces decides which buttons render — no client-side guess.
                "state": st,
                "forced": bool(e.get("forced")),
                "reverted": bool(e.get("reverted_at")),
                "can_merge": lifecycle.check(st, "merged", e) is None,
                "can_reject": lifecycle.check(st, "rejected", e) is None,
                "can_revert": lifecycle.check(st, "reverted", e) is None,
                "review_note": e.get("review_note", ""),
                # A REAL PR the wrapper opened (config pull_requests) wins; else
                # the prefilled PR-create link, only while it still makes sense
                # (per-task branch, not yet merged, not rejected).
                "pr_url": e.get("pr_url") or
                          (f"{BB_PR_NEW}?source={urllib.parse.quote(branch, safe='')}"
                           f"&dest={urllib.parse.quote(BASE_BRANCH, safe='')}"
                           if BB_PR_NEW and branch.startswith("autopilot/") and not merged and st != "rejected"
                           else None),
                "pr_open": bool(e.get("pr_url")),
                "has_packet": (REVIEWS / f"task-{tid}.md").exists(),
                "has_patch": bool(e.get("patch")),
            })
            done.append(row)
        elif st == "escalated":
            row.update({"state": st, "forced": bool(e.get("forced")),
                        "can_reject": lifecycle.check(st, "rejected", e) is None})
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
        # Join the backlog block for the plain-language fields (queue.json only
        # carries the loop contract, not the operator enrichment).
        bmeta = by_id.get(tid, {})
        next_up.append({"id": tid, "title": (q.get("task") or tid).split("\n", 1)[0],
                        "category": q.get("category", ""), "autopilot": q.get("autopilot", "allow"),
                        "source": q.get("source", ""), "priority": q.get("priority", "medium"),
                        "status": "queued",
                        "plain": bmeta.get("plain", ""), "impact": bmeta.get("impact", ""),
                        "risk_if_skipped": bmeta.get("risk_if_skipped", ""),
                        "effort": bmeta.get("effort", ""), "decision_hint": bmeta.get("decision_hint", ""),
                        "docs": [d for d in (bmeta.get("docs") or []) if isinstance(d, str)],
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
        if t.get("category") == "epic":
            continue   # epics get their own strip (planning tier), not triage cards
        if _on_board(t):
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

    # EPICS — the planning tier's strip: stage machine (status field is truth,
    # epic_plan.py moves it) + a rollup of decomposed children's progress.
    epic_children: dict = {}
    for t in backlog:
        if t.get("epic"):
            epic_children.setdefault(str(t["epic"]), []).append(str(t.get("id")))
    epics = []
    for t in backlog:
        if t.get("category") != "epic" or t.get("status") == "done":
            continue
        tid = str(t.get("id"))
        rollup = {"total": 0, "proposed": 0, "queued": 0, "shipped": 0, "merged": 0}
        for k in epic_children.get(tid, []):
            e = ledger.get(k, {})
            st = e.get("state")
            rollup["total"] += 1
            if st == "merged" or (e.get("sha") and mm.get(e["sha"])):
                rollup["merged"] += 1
            elif st in ("pushed", "committed"):
                rollup["shipped"] += 1
            elif by_id.get(k, {}).get("status") == "queued":
                rollup["queued"] += 1
            else:
                rollup["proposed"] += 1
        epics.append({"id": tid, "title": t.get("title") or tid,
                      "stage": t.get("status") or "proposed",
                      "priority": t.get("priority", "medium"),
                      "has_spec": (AP_HOME / "specs" / f"{tid}.md").exists(),
                      "children": rollup, "context": t.get("context", ""),
                      "acceptance_criteria": [a for a in (t.get("acceptance_criteria") or [])
                                              if isinstance(a, str)]})

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

    # Reconciled autopilot/* branches for the Branches tab. Decorate the pure
    # rows with filesystem/URL facts build_state owns.
    _now = int(time.time())
    _branch_rows = branch_reconcile(remote_branches(), trunk_ancestry(), ledger, _now)
    for _b in _branch_rows:
        _b["has_packet"] = (REVIEWS / f"task-{_b['task_id']}.md").exists()
        _real_pr = ledger.get(_b["task_id"], {}).get("pr_url") if _b["is_current_ref"] else None
        _b["pr_url"] = _real_pr or (
            f"{BB_PR_NEW}?source={urllib.parse.quote(_b['branch'], safe='')}"
            f"&dest={urllib.parse.quote(BASE_BRANCH, safe='')}"
            if BB_PR_NEW and not _b["merged"] and _b["category"] != "rejected"
            else None
        )
        _b["pr_open"] = bool(_real_pr)

    rt = probe_runtime()
    rt.update({"running": running, "next_up": next_up, "board": board, "skipped": skipped,
               "auto_feed": AUTO_PROMOTE.exists(), "metrics": agent_metrics(), "history": hist["days"],
               "funnel": funnel,
               "feature_builds": feature_builds_annotated(),
               "epics": epics,
               "calibration": load_calibration_candidates(),
               "spend_history": spend_history(),
               "done": done, "escalated": escalated, "skips": sorted(skips),
               "runlog": runlog,
               "branches": _branch_rows,
               "counts": {"next": len(next_up), "board": len(board), "done": len(done),
                          "escalated": len(escalated), "skipped": len(skips)}})
    return rt


# ── HTML shell (static; the SPA fills it from /api/state) ─────────────────────


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
            # cc_shell.html is the ONLY shell — no inline fallback. A silent
            # fallback is how the UI once forked (features landed in a dead
            # template and were invisible for weeks); missing file = loud error.
            if not SHELL_FILE.exists():
                self._send(b"engine/cc_shell.html is missing - restore it (git checkout) and reload", "text/plain", 500)
                return
            body = SHELL_FILE.read_text().replace("__BRANCH__", BASE_BRANCH).replace("__TOKEN__", _TOKEN)
            self._send(body.encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(json.dumps(build_state()).encode(), "application/json")
        elif path == "/api/config":
            self._send(json.dumps(config_payload()).encode(), "application/json")
        elif path == "/api/cycletail":
            self._send(json.dumps(cycletail()).encode(), "application/json")
        elif path in ("/review", "/diff"):
            if not tid or not _TID_RE.match(tid):
                self._send(b"invalid id", "text/plain", 400); return
            body = read_packet(tid) if path == "/review" else read_patch(tid)
            if body is None:
                self._send(b"not found for this task", "text/plain", 404); return
            self._send(body.encode(), "text/plain; charset=utf-8")
        elif path == "/epic-spec":
            # The planner's spec for one epic — rendered in the packet viewer.
            if not tid or not _TID_RE.match(tid):
                self._send(b"invalid id", "text/plain", 400); return
            f = AP_HOME / "specs" / f"{tid}.md"
            if not f.exists():
                self._send(b"no spec for this epic yet - run Plan first", "text/plain", 404); return
            self._send(f.read_text().encode(), "text/plain; charset=utf-8")
        elif path == "/guide":
            # Operator guide: what each section means and how to decide.
            for gf in GUIDE_FILES:
                if gf.exists():
                    self._send(gf.read_text().encode(), "text/plain; charset=utf-8"); return
            self._send(b"No guide found. Create GUIDE.md in your .autopilot/ directory.",
                       "text/plain; charset=utf-8")
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        # CSRF / DNS-rebinding guard on the state-changing control plane.
        if not self._host_ok() or not self._origin_ok():
            self._send(b'{"ok":false,"msg":"forbidden host/origin"}', "application/json", 403); return
        if not hmac.compare_digest(self.headers.get("X-CC-Token", ""), _TOKEN):
            self._send(b'{"ok":false,"msg":"missing/invalid token - hard-refresh the dashboard"}', "application/json", 403); return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode() if length else ""
        data = urllib.parse.parse_qs(raw)
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
                msg = do_rollback(tid, (data.get("note", [""])[0]).strip())
            elif path == "/answer":
                msg = do_answer(tid, (data.get("text", [""])[0]).strip())
            elif path == "/mark":
                msg = do_mark(tid, (data.get("outcome", [""])[0]).strip(),
                              (data.get("note", [""])[0]).strip(),
                              force=(data.get("force", ["0"])[0]).strip() == "1")
            elif path == "/task-new":
                msg = do_task_new(tid, (data.get("title", [""])[0]).strip(),
                                  (data.get("category", [""])[0]).strip(),
                                  (data.get("priority", ["medium"])[0]).strip(),
                                  data.get("context", [""])[0],
                                  data.get("acc", [""])[0],
                                  queue_now=(data.get("queue", ["0"])[0]).strip() == "1")
            elif path == "/task-edit":
                msg = do_task_edit(tid, (data.get("title", [""])[0]).strip(),
                                   (data.get("category", [""])[0]).strip(),
                                   (data.get("priority", [""])[0]).strip(),
                                   data.get("context", [""])[0],
                                   data.get("acc", [""])[0])
            elif path == "/task-del":
                msg = do_task_delete(tid)
            elif path == "/reap":
                msg = do_reap()
            elif path == "/lintok":
                msg = do_lintok(tid)
            elif path == "/epic-action":
                if not tid:
                    self._send(b'{"ok":false,"msg":"missing epic id"}', "application/json", 400); return
                msg = do_epic_action(tid, (data.get("action", [""])[0]).strip())
            elif path == "/calibration-action":
                if not tid:
                    self._send(b'{"ok":false,"msg":"missing candidate id"}', "application/json", 400); return
                msg = do_calibration_action(tid, (data.get("action", [""])[0]).strip(),
                                            (data.get("reason", [""])[0]).strip())
            elif path == "/merge-to-loop":
                branch = (data.get("branch", [""])[0]).strip()
                if not re.match(r"^[A-Za-z0-9._/-]{1,120}$", branch):
                    self._send(b'{"ok":false,"msg":"invalid branch"}', "application/json", 400); return
                msg = do_merge_to_loop(branch)
            elif path == "/feature-start":
                if not tid:
                    self._send(b'{"ok":false,"msg":"missing task id"}', "application/json", 400); return
                branch = (data.get("branch", [""])[0]).strip()
                if branch and not re.match(r"^[A-Za-z0-9._/-]{1,120}$", branch):
                    self._send(b'{"ok":false,"msg":"invalid branch"}', "application/json", 400); return
                msg = do_feature_start(tid, (data.get("title", [""])[0]).strip(), branch)
            elif path == "/delete-branch":
                branch = (data.get("branch", [""])[0]).strip()
                if not re.match(r"^[A-Za-z0-9._/-]{1,120}$", branch):
                    self._send(b'{"ok":false,"msg":"invalid branch"}', "application/json", 400); return
                msg = do_delete_branch(branch)
            elif path == "/delete-branches-bulk":
                msg = do_delete_branches_bulk((data.get("kind", [""])[0]).strip())
            elif path == "/api/config":
                msg = do_save_config(raw)
            elif path == "/restart-loop":
                msg = do_restart(LAUNCHD_LABEL)
            elif path == "/restart-dashboard":
                msg = do_restart(_dashboard_label(), delay=1)
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
