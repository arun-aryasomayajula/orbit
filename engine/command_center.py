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
import atexit
import contextlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from backlog_lint import lint_task
from backlog_append import SLUG_RE
from qa_teach import _bad_flows_dir_reason
import qa_env
import qa_teach
import prd_flow
import secrets_env

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
WORKERS_DIR = AP_STATE / "workers"   # per-slot <slot>.pid liveness files (run.sh write_worker_pid)
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
import locks  # noqa: E402
# qa_check resolves its own AP_STATE/AP_HOME at import time, which is why
# qa_run.py imports it lazily; here those are already resolved above, so a
# module-level import is safe. Imported for the ONE qa.services mutex
# (services_lock_path) - the Dev session is one of its consumers.
import qa_check  # noqa: E402
from backlog_lint import KNOWN_CATEGORIES  # noqa: E402
# The ONE concurrency ceiling (see engine/config.py) - imported, never
# re-stated, so the Admin panel and the loop's preflight can never disagree.
from config import CONCURRENCY_MAX  # noqa: E402


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
QA_DIR = AP_STATE / "qa"                      # QA gate screenshots/reports (confined /qa-shot, /qa-report)
DEFAULT_QA_FLOWS_DIR = "qa/flows"             # mirrors qa_teach.py/qa_run.py's own default — the bare
                                               # string constant is still each script's own tiny copy (see
                                               # qa_teach.py's _bad_flows_dir_reason comment for why that's
                                               # fine); the shape/escape check itself IS shared — imported
                                               # below and used by _qa_flows_root, never re-implemented here.
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
_TOKEN = os.environ.get("CC_TOKEN") or secrets.token_urlsafe(18)  # shared token when spawned by the launcher; else per-process
_ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}
_TID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

# Mirror run.sh tunables so the timers/budget match reality. Accept the
# ORBIT_* names (what engine/config.py exports) with AP_* as legacy fallback.
INTERVAL = int(os.environ.get("ORBIT_INTERVAL") or os.environ.get("AP_INTERVAL", "180"))
MAX_TASKS = int(os.environ.get("ORBIT_MAX_TASKS") or os.environ.get("AP_MAX_TASKS", "12"))
CYCLE_TIMEOUT = int(os.environ.get("ORBIT_CYCLE_TIMEOUT") or os.environ.get("AP_CYCLE_TIMEOUT", "3600"))

PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
PRIORITY_LADDER = ["low", "medium", "high"]
SAFE = {"testing", "documentation", "dependencies", "code_quality", "developer_experience",
        "insight", "funnel", "hypothesis", "survey_design", "survey_analysis", "design_review"}
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
    """PIDs of every orbit-cycle process on the MACHINE (claude --print
    /orbit-cycle + its caffeinate) - a machine-wide pgrep, so it cannot tell
    ours from an unrelated deployment's, or one slot's from another's. Kept
    only as a legacy fallback (single-slot deployments, or a state dir from
    before T4's per-slot pid files existed) - see workers_state()/do_killtask
    for the identity-aware replacement everything else now prefers."""
    try:
        out = subprocess.run(["pgrep", "-f", "orbit-cycle"], capture_output=True, text=True).stdout
        return [int(x) for x in out.split()]
    except Exception:
        return []


def workers_state() -> list | None:
    """Per-slot worker liveness for the dashboard's workers strip: one entry
    per $AP_STATE/workers/<slot>.pid file (run.sh's write_worker_pid - {pid,
    tid, started}, removed the instant a cycle ends), each stamped with
    `alive` (locks.pid_alive on the recorded pid - the ONLY trustworthy
    "still in flight" signal per ledger.py's _slot_worker_matches docstring;
    the loop's own pid outlives any single task).

    None (key omitted from build_state) when there is nothing worth a strip:
    the dir is empty or missing AND concurrency is 1 - the overwhelming
    majority of deployments today, which must see a byte-identical
    /api/state (same discipline as watches_state/qa_services_state). Once
    concurrency > 1, [] is returned instead of None even with no pid files
    yet (dir freshly created, nothing claimed) - an operator who just turned
    on concurrency and is looking for the strip needs to see "no workers
    right now", not have it vanish.

    Only NUMERIC stems are per-cycle worker files. The same directory also
    holds run.sh's lifetime `loop-<slot>.pid` files, which say "this slot is
    owned" rather than "this task is in flight" - counting one as a worker
    would put a permanent phantom row on the strip and, worse, make `live`
    (build_state) read as a running cycle for as long as the loop is up."""
    concurrency = 1
    try:
        concurrency = int(_load_cfg().get("concurrency") or 1)
    except (TypeError, ValueError):
        pass
    try:
        pid_files = sorted(p for p in WORKERS_DIR.glob("*.pid")
                           if p.stem.isdigit()) if WORKERS_DIR.is_dir() else []
    except OSError:
        pid_files = []
    if not pid_files and concurrency <= 1:
        return None
    out = []
    for p in pid_files:
        try:
            info = json.loads(p.read_text())
        except (OSError, ValueError):
            continue   # a torn tmp+rename read - same tolerance as every other state-file parse here
        if not isinstance(info, dict):
            continue
        pid = info.get("pid")
        # cycle_pid/cycle_pgid: the CYCLE this slot is running, as opposed to
        # `pid` (the slot loop). Absent until run.sh's record_cycle_pid has
        # run; cycle_pgid additionally absent unless the cycle leads its own
        # process group. do_killtask is the consumer - see its docstring.
        out.append({"slot": p.stem, "pid": pid, "tid": info.get("tid"),
                    "started": info.get("started"),
                    "cycle_pid": info.get("cycle_pid"),
                    "cycle_pgid": info.get("cycle_pgid"),
                    "alive": pid is not None and locks.pid_alive(pid)})
    out.sort(key=lambda w: (int(w["slot"]) if str(w["slot"]).isdigit() else 0, w["slot"]))
    return out


def loop_loaded() -> bool:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        return LAUNCHD_LABEL in out
    except Exception:
        return False


_CYCLE_LOG_STAMP = re.compile(r"(\d{8}T\d{6})\.log$")
_CYCLE_LOG_TID = re.compile(r"^cycle-(.+)-\d{8}T\d{6}\.log$")


def _cycle_log_stamp(name: str) -> str:
    """The start stamp in a cycle log's filename, '' if there isn't one. Two
    shapes exist: the historical cycle-<ts>.log and the slot loop's
    cycle-<tid>-<ts>.log — whose task id makes a plain lexicographic sort of the
    filenames meaningless, hence sorting on this instead."""
    m = _CYCLE_LOG_STAMP.search(name)
    return m.group(1) if m else ""


def _cycle_log_tid(name: str) -> str:
    """The task id embedded in a slot loop's cycle-<tid>-<ts>.log filename,
    '' for the historical cycle-<ts>.log shape (no tid at all) or anything
    that doesn't match."""
    m = _CYCLE_LOG_TID.match(name)
    return m.group(1) if m else ""


def probe_runtime(workers: list | None = None) -> dict:
    """Everything the live console needs that isn't in backlog/ledger.

    `workers`: build_state already computed workers_state() for its own
    `live`/`workers` keys, and every pid file it reads would otherwise be
    read and liveness-probed a SECOND time here, once per 3-second poll.
    Pass it in. Omitted (None) means "compute it myself" - the standalone
    callers and tests that call probe_runtime() on its own."""
    if workers is None:
        workers = workers_state()
    pids = cycle_pids()
    running = bool(pids)
    # Cycle start = the timestamp encoded in the newest cycle log's filename
    # (created at cycle start; --print only flushes the body at the end, so the
    # filename is the reliable start clock). The newest stamp across every cycle
    # log, whichever filename shape it has (see _cycle_log_stamp) - UNLESS pid
    # files name live workers, in which case narrow to the logs those workers
    # actually own first: with concurrency>1 every slot writes its own
    # cycle-<tid>-<ts>.log concurrently, so the single newest timestamp across
    # ALL of them can belong to a different slot than the one this heuristic is
    # trying to time (Task 4's interleaving finding). Falls back to the
    # unfiltered set when no live worker's tid matches any log on disk (e.g. a
    # legacy single-slot deployment with no workers dir at all).
    logs = list(LOGDIR.glob("cycle-*.log")) if LOGDIR.exists() else []
    live_tids = {str(w["tid"]) for w in (workers or []) if w.get("alive") and w.get("tid")}
    if live_tids:
        owned = [p for p in logs if _cycle_log_tid(p.name) in live_tids]
        if owned:
            logs = owned
    start = None
    stamps = sorted(s for s in (_cycle_log_stamp(p.name) for p in logs) if s)
    if stamps:
        try:
            start = datetime.strptime(stamps[-1], "%Y%m%dT%H%M%S").timestamp()
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
    # Turnaround is measured by PAIRING a "cycle start" line with the next
    # terminal line in one shared orbit.log. With several slots interleaving
    # their lines into that log, the pairing is arbitrary: slot 1's terminal
    # line closes slot 0's start, and the "duration" is the gap between two
    # unrelated events. Rather than publish a number that is wrong in a way
    # nobody can see, suppress it while more than one worker is live (the UI
    # renders null as "—"). Slot-tagged pairing - the honest fix, since every
    # line already carries "[slot N]" - is M5.
    if len(live_tids) > 1:
        last_dur = avg_cycle = None
        durs = []

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


# ── backlog.yaml write lock (dashboard side) ─────────────────────────────────
# ThreadingHTTPServer serves each request on its own thread, so a plain
# per-process flock is NOT enough on its own: locks.hold()'s reentrancy guard
# is keyed by os.getpid() (see locks.py), which is IDENTICAL across every
# thread in this process - two request-handler threads both calling
# locks.hold() on the same path at once would not queue on the flock, the
# second would immediately raise ReentrantLock (indistinguishable, from
# inside this module, from a real self-deadlock). This threading.Lock
# serializes route handlers onto the flock one thread at a time; the flock
# itself still provides the actual cross-process exclusion against
# autopromote/autoclose/qa_check/backlog_append running in other processes.
_BACKLOG_LOCK = threading.Lock()


@contextlib.contextmanager
def _backlog_txn():
    """The WHOLE read-modify-write of one dashboard backlog action (its
    `_read_blocks()` ... `_write_blocks()` span), under ONE acquisition of the
    shared `<AP_STATE>/.backlog.lock` - the same lock backlog_append.py
    (append_tasks/append_raw_blocks/set_task_field), autopromote.py,
    autoclose.py, and qa_check.py (via set_task_field) all take around their
    own backlog.yaml writes, so a dashboard edit can never lose - or cause the
    loss of - any of theirs.

    CRITICAL: never call `_run_converter()` (or anything else that shells out)
    from INSIDE this - `_run_converter()` runs backlog_to_tasks.py, which can
    itself launch a source-adapter subprocess that calls straight back into
    backlog_append's NOW lock-protected append_tasks/set_task_field. That
    child is a DIFFERENT process (not a reentrant same-pid re-entry - locks.py
    would raise ReentrantLock for that, not deadlock), so it would instead
    block waiting for the very flock this process is still holding while this
    process's own `subprocess.run()` blocks waiting for the child to exit - a
    real deadlock, not just a slow contender. Every caller below acquires this
    txn ONLY around the read+mutate+write, and calls `_run_converter()` after
    it exits.

    None of this module's backlog-writing functions call each other or
    otherwise re-enter this contextmanager while already inside it, so a
    single acquisition per call never nests (locks.hold() is not reentrant -
    see locks.py's module docstring)."""
    with _BACKLOG_LOCK, locks.hold(AP_STATE / ".backlog.lock"):
        yield


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


def do_killtask(tid: str = "") -> str:
    """Abort ONE task's in-flight cycle, then skip it so the loop won't
    immediately re-pick it. Reversible by editing skips.txt.

    Targeted path (concurrency-safe): if `tid` names a LIVE worker in
    workers_state(), the signal goes to the CYCLE that worker recorded
    ($AP_STATE/workers/<slot>.pid's `cycle_pid`/`cycle_pgid`, written by
    run.sh's record_cycle_pid) - never to the worker file's `pid`, which is
    the slot LOOP's own pid. That was the C2 bug: killpg'ing the loop's group
    aborted the whole slot along with the one task, so the operator's Abort
    silently stopped the workspace. Aborting a task must leave its loop
    running, ready to escalate the task and pick up the next one.

    `cycle_pgid` is only present when the cycle genuinely leads its own
    process group (run.sh's session wrapper), which is the same leader guard
    the launcher applies before killpg (do_loop_stop): with it, the whole
    cycle tree goes - claude, caffeinate, node children; without it, a plain
    TERM on the cycle pid is all that is safe.

    Legacy fallback: no `tid` given, or it doesn't match any live worker
    (e.g. a state dir from before T4's per-slot pid files existed) - the
    original machine-wide behavior, kept only so a single-slot deployment
    without a populated workers dir still has a working Abort button."""
    workers = workers_state() or []
    match = next((w for w in workers if w.get("alive") and str(w.get("tid")) == str(tid)), None) if tid else None
    if match is not None:
        cycle_pid, cycle_pgid = match.get("cycle_pid"), match.get("cycle_pgid")
        if not cycle_pid:
            # Claimed, but no cycle recorded yet (the window between the claim
            # and the cycle's first instant), or a pid file written by a run.sh
            # from before cycle_pid existed. There is nothing safe to signal:
            # the only other pid on file is the loop's. Skip the task so it is
            # not re-picked and say plainly that nothing was killed.
            do_skip(tid)
            return (f"'{tid}' is claimed by slot {match['slot']} but its cycle process isn't recorded "
                    f"yet - nothing was stopped. The task is skipped so it won't be retried; if a cycle "
                    f"really is running, try Abort again in a few seconds. Un-skip by editing skips.txt.")
        try:
            if cycle_pgid:
                os.killpg(cycle_pgid, signal.SIGTERM)
            else:
                os.kill(cycle_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass   # already gone - still skip it below so it isn't re-picked
        except Exception as e:
            return f"Could not stop '{tid}' (slot {match['slot']}, cycle pid {cycle_pid}): {e}"
        do_skip(tid)
        return (f"Stopped {tid}'s cycle (slot {match['slot']}, cycle pid {cycle_pid}) and skipped it so "
                f"it won't be retried. That slot's loop is untouched - it will escalate the task, reset "
                f"its worktree and start a fresh cycle after its backoff. Un-skip by editing skips.txt.")
    pids = cycle_pids()
    if not pids:
        return "No cycle is currently running — nothing to stop."
    for p in pids:
        try:
            os.kill(p, 15)
        except ProcessLookupError:
            pass
    # No pid file matched `tid` - fall back to the legacy single-slot marker
    # so the message (and the skip) still names the right task when there is
    # no workers dir to consult at all.
    fallback_tid = tid or (MARKER.read_text().strip() if MARKER.exists() else "")
    if fallback_tid:
        do_skip(fallback_tid)
        return (f"Stopped the running cycle (pid {pids[0]}) and skipped '{fallback_tid}' so it won't be retried. "
                f"The wrapper will reset the worktree and start a fresh cycle after its backoff. "
                f"Un-skip by editing skips.txt.")
    return f"Stopped the running cycle (pid {pids[0]}). No task id was claimed yet."


def do_priority(tid: str, direction: str) -> str:
    with _backlog_txn():
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
    moved_before_after = None   # set to n_tid iff the on-screen-neighbor path wrote
    with _backlog_txn():
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
                moved_before_after = n_tid
        if moved_before_after is None:
            # Fallback (task not in the Next-up view, e.g. queue.json missing,
            # or its on-screen neighbor wasn't found on the backlog): the old
            # raw file-neighbor swap, so the button still does something sensible.
            i = ids.index(tid)
            j = i - 1 if direction == "up" else i + 1
            if j < 0 or j >= len(blocks):
                return f"{tid} is already at the {'top' if direction=='up' else 'bottom'}."
            blocks[i], blocks[j] = blocks[j], blocks[i]
            _write_blocks(header, blocks)
    _run_converter()
    if moved_before_after is not None:
        return (f"Moved {tid} {direction} (now "
                f"{'before' if direction == 'up' else 'after'} {moved_before_after}).")
    return f"Moved {tid} {direction}."


def do_promote(tid: str) -> str:
    """Board → Next up — works on a task in ANY status (proposed or already
    queued review-only). For an EMITTABLE category (chores + bug/feature/refactor)
    we set status:queued + autopilot:allow so it lands in Next up and the loop can
    work it. For a HUMAN-ONLY category (security/infra/release/git) we queue it but
    leave the gate — the loop never touches those, so it stays board-tracked."""
    with _backlog_txn():
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
    with _backlog_txn():
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
    with _backlog_txn():
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
    with _backlog_txn():
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
    with _backlog_txn():
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
    iteration, but a paused loop never iterates — this button covers that).
    ledger.py's reap is now identity-aware: a stale entry whose slot worker
    is still alive and on that exact task is skipped rather than escalated
    (logged on stderr) — surface that count too, so "nothing stalled" is
    distinguishable from "some entries are stale but their worker is still
    on it"."""
    r = subprocess.run(["python3", str(ENGINE / "ledger.py"), "reap", str(CYCLE_TIMEOUT + 600)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return f"Reap failed: {r.stderr.strip()}"
    reaped = r.stdout.strip()
    skipped = r.stderr.count("still alive")
    msg = (f"Reaped {reaped.count(' ') + 1} stalled task(s) → Escalated: {reaped}" if reaped
           else "Nothing stalled — no mid-cycle entry is older than the cycle timeout.")
    if skipped:
        msg += f" ({skipped} skipped — worker still alive on that task)"
    return msg


def do_runnow(tid: str) -> str:
    """Force a task to be the loop's NEXT pick: move it to the top of the backlog,
    set priority high (+ allow if workable), and resume the loop. The agent picks
    it on the next cycle (≤~20s if idle, else after the current cycle finishes)."""
    with _backlog_txn():
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
    synthesized = False
    with _backlog_txn():
        header, blocks, ids = _read_blocks()
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
    # Both below are subprocess/file side effects best-effort-sequenced AFTER
    # the backlog lock is released - ledger.py's `clear` takes ITS OWN lock
    # (a different lock domain; see ledger.py's `_locked_update`), and neither
    # needs to be inside the backlog critical section above.
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
    with _backlog_txn():
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


# ── QA evidence routes (/qa-shot, /qa-report) ─────────────────────────────────
# Unlike read_patch (which trusts a ledger-recorded path), the candidate path
# here comes straight off the query string — untrusted. `_qa_path_ok` is a
# cheap shape gate (charset/length, no absolute path, no ".." segment) run
# BEFORE any filesystem touch; read_qa_shot/read_qa_report then apply the
# read_patch confinement template (resolve → assert resolved-under-root, which
# also catches symlink escapes a ".."-free string can still pull off → is_file)
# plus an extension allowlist and a size cap.
_QA_PATH_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,200}$")
_QA_SHOT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
_QA_REPORT_EXTS = {".md", ".txt", ".log", ".json"}
_QA_MAX_BYTES = 5 * 1024 * 1024


def _qa_path_ok(p: str) -> bool:
    return bool(p) and bool(_QA_PATH_RE.match(p)) and not p.startswith("/") and ".." not in p.split("/")


def _qa_resolve(p: str) -> Path | None:
    # Same confinement template as read_patch: resolve → assert the RESOLVED
    # path is under the RESOLVED root → is_file(). Resolving (not just string
    # concatenation) is what catches a symlink inside qa/ pointing outside it.
    root = QA_DIR.resolve()
    resolved = (QA_DIR / p).resolve()
    if not str(resolved).startswith(str(root) + os.sep) or not resolved.is_file():
        return None
    return resolved


def read_qa_shot(p: str) -> tuple[bytes, str] | None:
    # Serve a QA screenshot — $AP_STATE/qa/<p>. `p`'s shape is pre-validated by
    # the caller (do_GET's _qa_path_ok); confinement + extension + size here.
    resolved = _qa_resolve(p)
    if resolved is None:
        return None
    ctype = _QA_SHOT_TYPES.get(resolved.suffix.lower())
    if ctype is None or resolved.stat().st_size > _QA_MAX_BYTES:
        return None
    return resolved.read_bytes(), ctype


def read_qa_report(p: str) -> str | None:
    # Serve a QA report/log — $AP_STATE/qa/<p>. Same confinement as
    # read_qa_shot, text-extension allowlist instead of image types.
    resolved = _qa_resolve(p)
    if resolved is None:
        return None
    if resolved.suffix.lower() not in _QA_REPORT_EXTS or resolved.stat().st_size > _QA_MAX_BYTES:
        return None
    return resolved.read_text(errors="ignore")[:400_000]


# ── onboarding: knowledge-base doc browser (confined GET /api/knowledge-doc) ──
# Same confinement template as _qa_resolve/read_qa_report, plus one extra gate:
# the name must be an exact match against a LIVE directory listing (not merely
# shape-valid) — the allowlist here is "whatever knowledge.py actually wrote",
# not "anything that looks like a filename". A shape-valid name for a file
# that doesn't exist (or was deleted) must never fall through to a read.
_KDOC_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_KDOC_MAX_BYTES = 2 * 1024 * 1024


def _knowledge_dir() -> Path:
    return AP_HOME / "knowledge"


def _knowledge_docs() -> list[str]:
    """Names of files directly under $AP_HOME/knowledge/ — the allowlist for
    both build_state's onboarding.knowledge_docs and read_knowledge_doc.
    Never raises: an absent/unreadable dir just yields no docs."""
    d = _knowledge_dir()
    if not d.is_dir():
        return []
    try:
        return sorted(p.name for p in d.iterdir() if p.is_file() and _KDOC_NAME_RE.match(p.name))
    except OSError:
        return []


def read_knowledge_doc(name: str) -> str | None:
    """Serve one knowledge doc — $AP_HOME/knowledge/<name>. `name` must both
    be shape-valid AND currently listed by _knowledge_docs() (the real
    allowlist), then resolve → assert the RESOLVED path is under the
    RESOLVED root → is_file() (catches a symlink escape the same way
    _qa_resolve does)."""
    if not isinstance(name, str) or name not in _knowledge_docs():
        return None
    root = _knowledge_dir().resolve()
    resolved = (_knowledge_dir() / name).resolve()
    if not str(resolved).startswith(str(root) + os.sep) or not resolved.is_file():
        return None
    if resolved.stat().st_size > _KDOC_MAX_BYTES:
        return None
    try:
        # No further truncation below the size gate above: a 400_000-char
        # slice on a file already confirmed <= _KDOC_MAX_BYTES (2 MB) bytes
        # used to cut a ~400 KB-2 MB graph.json off mid-object, serving
        # invalid JSON the client's JSON.parse then correctly (but
        # misleadingly) reported as "malformed — rebuild" for a file that
        # was actually fine on disk. Serve the whole allowed size.
        return resolved.read_text(errors="ignore")
    except OSError:
        return None


# ── vendored client-side libs (confined GET /vendor/<file>) ──────────────────
# The Knowledge view's graph mode loads cytoscape.js from here. Unlike
# read_knowledge_doc (allowlist = a live directory listing), the vendor
# allowlist is a FIXED small dict — there is never a reason to add a file to
# this route without a code change, so "in the dict" is the whole gate; no
# shape regex needed, an exact-match dict lookup already rejects every
# traversal/encoding trick (a name like "../command_center.py" or
# "%2e%2e%2fcommand_center.py" simply isn't a key). Same resolve → assert
# resolved-under-root → is_file confinement as _qa_resolve/read_knowledge_doc
# underneath, belt-and-suspenders against a symlink inside vendor/.
_VENDOR_DIR = ENGINE / "vendor"
_VENDOR_ALLOW = {"cytoscape.min.js": "application/javascript"}


def read_vendor(name: str) -> tuple[bytes, str] | None:
    """Serve one vendored lib — engine/vendor/<name>. `name` must be an exact
    key in _VENDOR_ALLOW (the whole allowlist), then resolve → assert the
    RESOLVED path is under the RESOLVED root → is_file()."""
    ctype = _VENDOR_ALLOW.get(name)
    if ctype is None:
        return None
    root = _VENDOR_DIR.resolve()
    resolved = (_VENDOR_DIR / name).resolve()
    if not str(resolved).startswith(str(root) + os.sep) or not resolved.is_file():
        return None
    return resolved.read_bytes(), ctype


# ── QA flow library (Task 23) — the dashboard's QA view ──────────────────────
# Data sources: $AP_HOME/<flows_dir>/<name>/{intent.md, meta.json} (the flow-
# file format Task 20's `orbit teach` writes — see qa_teach.py), $AP_STATE/
# qa/runs/*.json (run verdicts Task 21's replay skill writes, read by Task
# 22's qa_run.py), $AP_STATE/qa/heal/*.json (heal candidates the replay skill
# mines — Task 26's do_qa_action heal-approve/heal-reject are the human gate
# that approves/rejects them; this view surfaces each pending one).
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _qa_flows_root() -> Path:
    # Bad/escaping flows_dir shapes (absolute, '..' segments, empty/non-string
    # — same _bad_flows_dir_reason `orbit teach`/`orbit qa` enforce with a
    # loud exit 2) fall back to the default here instead: this feeds a poll
    # loop (build_state → /api/state every ~3s), which must never error on a
    # bad config value the way a one-shot CLI is allowed to.
    cfg = _load_cfg()
    qa_cfg = cfg.get("qa") if isinstance(cfg.get("qa"), dict) else {}
    flows_dir = qa_cfg.get("flows_dir")
    if _bad_flows_dir_reason(flows_dir):
        flows_dir = DEFAULT_QA_FLOWS_DIR
    return AP_HOME / flows_dir


def _qa_exploratory_enabled() -> bool:
    """True iff `qa.exploratory.enabled` is exactly `true` in config.yaml —
    the SAME gate qa_run.py's `--explore` enforces (exit 2 otherwise, see its
    `_run_explore`). Both the dashboard's Explore button (via qa_flows'
    `explore_enabled`) and do_qa_action's `run-explore` action read this, so
    the two can never disagree about whether exploratory crawling is on."""
    cfg = _load_cfg()
    qa_cfg = cfg.get("qa") if isinstance(cfg.get("qa"), dict) else {}
    exploratory_cfg = qa_cfg.get("exploratory") if isinstance(qa_cfg.get("exploratory"), dict) else {}
    return exploratory_cfg.get("enabled") is True


QA_LOCK_NAME = ".qa-running"   # mirrors engine/qa_run.py's own LOCK_NAME — the
                                # one lock shared by --regression/--explore/--generate.


def _qa_browser_busy() -> bool:
    """True iff qa_run.py's shared `.qa-running` lock file is present — a
    regression sweep, exploratory crawl, OR generate pass already has the
    browser (all three drive it against the same qa.base_url and share one
    lock — see qa_run.py's own module docstring). Existence-only, NOT
    staleness-checked the way qa_run.py's own `_acquire_lock` is: this only
    ever feeds a UI hint (disabling the dashboard's "Generate tests" button
    so a double-click doesn't even bother trying — qa_run.py's own lock,
    staleness and all, is still the actual correctness guard underneath).
    A lock left behind by a crashed run keeps this reading True until
    qa_run.py's own next invocation replaces it — nothing here writes or
    removes the lock itself."""
    return (QA_DIR / QA_LOCK_NAME).exists()


def _read_flow_intent(flow_dir: Path) -> tuple[str | None, str | None]:
    """(intent_line, entry_path) from a flow's intent.md: frontmatter
    (name/created/entry_path/tags) + the user's free-text intent (see
    qa_teach.py's _render_intent_md — this is that format's reader). Never
    raises: a missing file, unreadable bytes, no frontmatter, or a bad YAML
    block all yield (None, None) rather than crash build_state (Task 23
    requirement — a hand-edited or corrupt flow dir must still list)."""
    try:
        text = (flow_dir / "intent.md").read_text()
    except OSError:
        return None, None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, None
    fm_text, body = m.group(1), m.group(2)
    entry_path = None
    try:
        fm = yaml.safe_load(fm_text)
        if isinstance(fm, dict) and isinstance(fm.get("entry_path"), str):
            entry_path = fm["entry_path"]
    except Exception:
        pass
    intent_line = None
    for ln in body.splitlines():
        if ln.strip():
            intent_line = ln.strip()
            break
    return intent_line, entry_path


def _read_flow_meta(flow_dir: Path) -> tuple[str | None, str | None, int | None, list[str], bool]:
    """(last_verdict, last_run_at, runs, requires, provides_auth) from a
    flow's meta.json. Defensive, same discipline as _read_flow_intent:
    missing/unreadable/non-JSON/non-dict meta.json, or fields of the wrong
    type, all yield None (or, for `requires`/`provides_auth` — test
    chaining, Task "prerequisites" — their own empty/false defaults) rather
    than a crash or a lying value. A pre-chaining flow's meta.json simply has
    no `requires`/`provides_auth` keys at all, which reads the same as an
    explicit `[]`/`false` — there's no third "unknown" state for these two,
    unlike last_verdict/last_run_at/runs."""
    try:
        meta = json.loads((flow_dir / "meta.json").read_text())
        if not isinstance(meta, dict):
            raise ValueError
    except (OSError, ValueError):
        return None, None, None, [], False
    lv, lra, runs = meta.get("last_verdict"), meta.get("last_run_at"), meta.get("runs")
    req = meta.get("requires")
    # SLUG_RE-gate each entry, matching qa_run.py's _read_requires: a generated
    # meta.json is agent-written, untrusted input, so a comma-/garbage-bearing
    # prerequisite never reaches the "needs:" chip (or any consumer) here either.
    requires = [r for r in req if isinstance(r, str) and SLUG_RE.match(r)] if isinstance(req, list) else []
    provides_auth = meta.get("provides_auth") is True
    return (lv if isinstance(lv, str) else None,
            lra if isinstance(lra, str) else None,
            runs if isinstance(runs, int) else None,
            requires, provides_auth)


def _qa_heal_candidate_entry(p: Path) -> dict:
    """One pending heal candidate for build_state's `heals` list. `new_steps`
    is deliberately omitted — it can be large, and the approve action
    (do_qa_action's heal-approve) re-reads the file from disk rather than
    trusting anything round-tripped through /api/state. JSON that fails to
    parse, or parses to something other than an object, still needs to reach
    the operator (there's no other way to clear it) — it's surfaced as
    `{file, malformed: True}` instead of the full shape; heal-reject
    deliberately allows rejecting this shape through (heal-approve refuses
    it)."""
    try:
        c = json.loads(p.read_text())
        if not isinstance(c, dict):
            raise ValueError
    except (OSError, ValueError):
        return {"file": p.name, "malformed": True}
    return {"file": p.name, "flow": c.get("flow"), "reason": c.get("reason"),
            "step_diff": c.get("step_diff"), "screenshot": c.get("screenshot"),
            "mined_at": c.get("mined_at")}


# ── AI-generated candidate flows — the dashboard's review strip for
# engine/qa_run.py's `--generate` (mirrors the heal-candidate strip above:
# machines propose under $AP_STATE/qa/generated/<slug>/, a human approves or
# rejects on the dashboard — see do_qa_action's generate-approve/
# generate-reject below). A candidate dir has the exact same three files a
# taught flow does (intent.md/steps.json/meta.json — see
# skills/orbit-qa-generate.md step 3) plus `meta.json.generated: true`.
def _read_candidate_criteria(cand_dir: Path) -> list[str]:
    """Every bullet under intent.md's '## Success criteria' heading (same
    shape qa_teach.py's _render_intent_md writes for a taught flow) — []  on
    anything short of a clean parse (missing file, unreadable, no
    frontmatter, no heading, no bullets). A candidate's card shows these
    (joined into `summary`, see _qa_generated_candidates below) so the
    operator can review sight-unseen without opening intent.md by hand."""
    try:
        text = (cand_dir / "intent.md").read_text()
    except OSError:
        return []
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return []
    body = m.group(2)
    idx = body.find("## Success criteria")
    if idx == -1:
        return []
    out = []
    for ln in body[idx:].splitlines()[1:]:
        s = ln.strip()
        if s.startswith("- "):
            out.append(s[2:].strip())
        elif s and not s.startswith("-"):
            break   # ran past the bullet list into whatever (if anything) follows
    return out


def _qa_generated_candidates() -> list[dict]:
    """Pending generate candidates for build_state's `qa_flows.generated` —
    top-level directories directly under $AP_STATE/qa/generated/ (a
    `rejected/` subdirectory holds ARCHIVED ones — see do_qa_action's
    generate-reject — and is deliberately excluded here, mirroring how the
    heal listing above excludes heal/approved/ and heal/rejected/). Listed
    names are SLUG_RE-gated, same discipline as qa_flow_library's own flow
    names and _qa_heal_candidate_entry's heal filenames — a non-slug
    directory name is unrunnable/unapprovable anyway (generate-approve/
    generate-reject both refuse it via the same gate), and cc_shell.html's
    onclick attributes aren't quote-safe on their own (see esc/escJsAttr's
    own comments) — filtering the LISTING is the primary guard, escaping in
    the template is defense-in-depth on top of it.

    Each entry is `{name, intent, summary, requires}` — `intent` re-reads
    intent.md exactly like a taught flow's row does (_read_flow_intent);
    `summary` is the success-criteria bullets joined with '; ' (falling back
    to `intent` when there are none, so the card is never blank) — a
    candidate has no run history yet, so there's no verdict-derived `summary`
    the way a real run row has; `requires` re-reads meta.json exactly like a
    taught flow's row does (_read_flow_meta) but is ALWAYS present (possibly
    `[]`) rather than omitted-when-empty — unlike the flow-row precedent
    (which omits requires/provides_auth only to stay byte-identical for
    flows taught before chaining existed), every generated candidate is
    freshly written by THIS feature, so there's no legacy shape to protect.
    Defensive throughout: a malformed candidate dir yields nulls/empties, it
    never crashes build_state (same posture as _read_flow_intent/
    _read_flow_meta for a hand-edited or partially-written flow dir)."""
    root = QA_DIR / "generated"
    if not root.is_dir():
        return []
    try:
        names = sorted(p.name for p in root.iterdir()
                        if p.is_dir() and p.name != "rejected" and SLUG_RE.match(p.name))
    except OSError:
        return []
    out = []
    for name in names:
        cand_dir = root / name
        intent_line, _entry_path = _read_flow_intent(cand_dir)
        criteria = _read_candidate_criteria(cand_dir)
        summary = "; ".join(criteria) if criteria else intent_line
        _, _, _, requires, _ = _read_flow_meta(cand_dir)
        out.append({"name": name, "intent": intent_line, "summary": summary, "requires": requires})
    return out


def qa_flow_library() -> dict | None:
    """The QA view's data: the taught-flow library, the 10 newest run
    verdicts, and every pending heal candidate. None when the target has no
    flows dir (or one with zero SLUG_RE-valid flows) AND exploratory QA is
    disabled — build_state then OMITS the `qa_flows` key entirely, so a
    target with no QA story yet sees a byte-identical /api/state (existing
    consumers see no change). Present (with an EMPTY `flows` list) whenever
    EITHER flows exist OR exploratory is enabled: `explore_enabled` lives
    INSIDE this dict, so if it stayed None whenever there were zero taught
    flows, enabling exploratory with no flows taught yet would hide the
    whole QA view — Explore button included.
    Listed flow names are filtered to SLUG_RE — a non-slug directory is never
    runnable anyway, and it's what the dashboard's HTML template trusts to
    be quote-safe (see the `names = sorted(...)` line below)."""
    explore_enabled = _qa_exploratory_enabled()
    flows_root = _qa_flows_root()
    # SLUG_RE-gated: a non-slug directory name is unrunnable anyway (do_qa_action
    # and qa_run.py both refuse it), and cc_shell.html's onclick="runFlow('${esc(
    # f.name)}')" isn't quote-safe — esc() only neuters &/</>, not '/". Filtering
    # the LISTING here (rather than only escaping in the template) means a
    # crafted committed flow-dir name can never reach the page at all.
    names = (sorted(p.name for p in flows_root.iterdir() if p.is_dir() and SLUG_RE.match(p.name))
             if flows_root.is_dir() else [])
    cfg = _load_cfg()
    # Present whenever the project has a qa: block — so the "New test" panel is
    # reachable to teach the FIRST flow. Otherwise it's chicken-and-egg: teach
    # lives inside the QA view, which would stay hidden until a flow already
    # existed. None (QA view omitted, byte-identical /api/state) only when there
    # is no QA story at all: no qa: block, no flows, no exploratory.
    qa_configured = isinstance(cfg.get("qa"), dict)
    if not names and not explore_enabled and not qa_configured:
        return None

    flows = []
    for name in names:
        flow_dir = flows_root / name
        intent_line, entry_path = _read_flow_intent(flow_dir)
        last_verdict, last_run_at, runs, requires, provides_auth = _read_flow_meta(flow_dir)
        entry = {"name": name, "intent_line": intent_line, "entry_path": entry_path,
                 "last_verdict": last_verdict, "last_run_at": last_run_at, "runs": runs}
        # Test chaining (Task "prerequisites"): only added when non-default,
        # so a flow with no requires/provides_auth (every flow taught before
        # this feature existed, or one that simply doesn't use it) keeps the
        # exact same row shape the dashboard has always sent.
        if requires:
            entry["requires"] = requires
        if provides_auth:
            entry["provides_auth"] = True
        flows.append(entry)

    runs_dir = QA_DIR / "runs"
    recent_runs = []
    if runs_dir.is_dir():
        dated = []
        for p in runs_dir.glob("*.json"):
            try:
                dated.append((p.stat().st_mtime, p))
            except OSError:
                continue
        dated.sort(key=lambda t: t[0], reverse=True)
        for _, p in dated[:10]:
            try:
                v = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(v, dict):
                continue
            row = dict(v)
            row["file"] = p.name
            recent_runs.append(row)

    # Only top-level files under heal/ are pending — heal/approved/ and
    # heal/rejected/ are subdirectories (never `.is_file()`), so an archived
    # candidate is excluded from both the listing and the count without a
    # separate check. Also gated to _HEAL_FILE_RE (same regex that guards
    # heal-approve/heal-reject via _heal_candidate_path): a name that doesn't
    # match is either junk (e.g. `.DS_Store`) or, worse, agent-written text
    # that never went through this module's own naming convention and so
    # isn't trusted to be quote-safe once it reaches cc_shell.html's onclick
    # attributes (same reasoning as qa_flow_library's SLUG_RE filter on flow
    # names above — filter the LISTING, don't rely on escaping alone).
    # Filtering here also means a non-conforming file can never become an
    # unclearable card: previously it was listed but heal-approve/reject both
    # refused it (wrong shape), so Reject always errored and the card stuck
    # around forever — now it's simply never listed. Sorted by name for a
    # deterministic strip ordering, same convention as
    # load_calibration_candidates' `sorted(d.glob(...))`.
    heal_dir = QA_DIR / "heal"
    heals = []
    if heal_dir.is_dir():
        try:
            names = sorted(p.name for p in heal_dir.iterdir()
                            if p.is_file() and _HEAL_FILE_RE.match(p.name))
        except OSError:
            names = []
        heals = [_qa_heal_candidate_entry(heal_dir / name) for name in names]

    # base_url rides along here (rather than a separate /api/config fetch)
    # purely so the "New test" panel's URL field can placeholder it without
    # an extra round trip — same qa: block _qa_flows_root/_qa_exploratory_enabled
    # already re-read independently above.
    qa_cfg = cfg.get("qa") if isinstance(cfg.get("qa"), dict) else {}
    base_url = qa_cfg.get("base_url") if isinstance(qa_cfg.get("base_url"), str) else None

    result = {"flows": flows, "recent_runs": recent_runs, "heals": heals,
              "pending_heals": len(heals), "explore_enabled": explore_enabled,
              "base_url": base_url, "qa_busy": _qa_browser_busy()}
    # AI-generated candidates (engine/qa_run.py's --generate): present only
    # when at least one is pending, so a target that has never run --generate
    # (i.e. every existing target, and every existing test fixture in
    # tests/test_command_center_qa_view.py) sees a byte-identical qa_flows
    # dict — same "added only when non-default" precedent as requires/
    # provides_auth on a flow row above, just applied to a whole new key
    # instead of two fields on an existing one.
    generated = _qa_generated_candidates()
    if generated:
        result["generated"] = generated
    return result


# ── heal approve/reject (Task 26) — the human gate that closes the QA
# self-healing loop. Structure mirrors do_calibration_action exactly: approve
# mutates a committed $AP_HOME artifact (there: goldens/LEARNED.md; here: the
# flow's own steps.json) + archives the candidate; reject requires a reason,
# stamps it, archives. The one asymmetry from calibration: approve here has
# real shape validation to do first (a candidate's new_steps could be
# garbage), so it runs every check BEFORE any write — all-or-nothing, never
# a partial apply.
_HEAL_FILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}--\d{8}T\d{6}Z\.json$")
_HEAL_STEP_ACTIONS = {"goto", "click", "fill", "expect"}


def _heal_candidate_path(file) -> Path | None:
    """None unless `file` is a bare `<slug>--<ts>.json` basename (the regex's
    character class contains no '/', so a '..' segment, an absolute path, or
    a subdirectory path all fail to match by SHAPE — there's no separate
    traversal check to forget) naming a file that actually exists directly
    under $AP_STATE/qa/heal/ (never heal/approved/ or heal/rejected/ — a bare
    basename can't reach into a subdirectory)."""
    if not isinstance(file, str) or not _HEAL_FILE_RE.match(file):
        return None
    p = QA_DIR / "heal" / file
    return p if p.is_file() else None


def _qa_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _do_heal_approve(file: str) -> dict:
    """Approve a mined heal candidate: write its new_steps into the flow's
    own committed steps.json (pretty JSON — the ONE new machine write this
    dashboard makes into a committed dir; it's human-clicked, same precedent
    as calibration approve's LEARNED.md append), append a `heals` entry to
    the flow's meta.json (runs/last_run_at/last_verdict untouched), then
    archive the candidate under heal/approved/. Every validation below runs
    BEFORE any write — a failure at any check leaves steps.json, meta.json,
    and the candidate file exactly as they were."""
    p = _heal_candidate_path(file)
    if p is None:
        return {"ok": False, "msg": f"'{file}' is not a pending heal candidate"}
    try:
        c = json.loads(p.read_text())
        if not isinstance(c, dict):
            raise ValueError
    except (OSError, ValueError):
        return {"ok": False, "msg": f"candidate '{file}' is malformed JSON — reject it instead"}
    flow = c.get("flow")
    if not isinstance(flow, str) or not SLUG_RE.match(flow):
        return {"ok": False, "msg": f"candidate '{file}' names an invalid flow {flow!r}"}
    flow_dir = _qa_flows_root() / flow
    if not flow_dir.is_dir():
        return {"ok": False, "msg": f"candidate's flow '{flow}' has no flow dir at {flow_dir}"}
    new_steps = c.get("new_steps")
    if not isinstance(new_steps, list) or not new_steps:
        return {"ok": False, "msg": f"candidate '{file}' has no usable new_steps"}
    for i, step in enumerate(new_steps):
        if not isinstance(step, dict) or step.get("action") not in _HEAL_STEP_ACTIONS:
            return {"ok": False, "msg": f"candidate '{file}' new_steps[{i}] has an invalid action"}

    # All-or-nothing point of no return: every check above passed, so the
    # writes below are safe to make unconditionally.
    (flow_dir / "steps.json").write_text(json.dumps(new_steps, indent=2) + "\n")
    meta_path = flow_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text())
        if not isinstance(meta, dict):
            raise ValueError
    except (OSError, ValueError):
        # Mirrors qa_run.py's _update_meta fallback — a missing/corrupt
        # meta.json must never block the approve that's already committed
        # steps.json.
        meta = {"created_at": None, "last_run_at": None, "last_verdict": None,
                 "runs": 0, "heals": []}
    heals = meta.get("heals")
    if not isinstance(heals, list):
        heals = []
    heals.append({"at": _qa_now_iso(), "reason": c.get("reason"), "candidate": file})
    meta["heals"] = heals
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")

    approved_dir = QA_DIR / "heal" / "approved"
    approved_dir.mkdir(parents=True, exist_ok=True)
    p.rename(approved_dir / p.name)
    return {"ok": True,
            "msg": f"Approved heal for '{flow}' — steps.json updated (archived to heal/approved/)."}


def _do_heal_reject(file: str, reason: str) -> dict:
    """Reject a heal candidate: a reason is required (archived with it, same
    discipline as do_calibration_action's reject) and is checked before the
    filename even needs to be valid. Same filename validation as approve.
    Unlike approve, a malformed candidate IS rejectable — its raw text is
    archived best-effort under `malformed_raw` since it can't be parsed back
    into a dict; this is the operator's only way to clear junk out of the
    pending strip."""
    if not reason.strip():
        return {"ok": False, "msg": "A rejection reason is required."}
    p = _heal_candidate_path(file)
    if p is None:
        return {"ok": False, "msg": f"'{file}' is not a pending heal candidate"}
    try:
        raw = p.read_text()
    except OSError as e:
        return {"ok": False, "msg": f"cannot read candidate '{file}': {e}"}
    try:
        c = json.loads(raw)
        if not isinstance(c, dict):
            raise ValueError
    except ValueError:
        c = {"malformed_raw": raw}
    c["rejected_reason"] = reason
    c["rejected_at"] = _qa_now_iso()
    rejected_dir = QA_DIR / "heal" / "rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    (rejected_dir / p.name).write_text(json.dumps(c, indent=2) + "\n")
    p.unlink()
    return {"ok": True, "msg": f"Rejected heal candidate '{file}' (archived with reason)."}


# ── generate approve/reject — the human gate that closes the AI test-
# generation loop (mirrors _do_heal_approve/_do_heal_reject exactly, one
# level up: a heal candidate replaces one FILE inside an existing flow dir;
# a generate candidate IS a whole flow dir, so "approve" MOVES it wholesale
# into the committed library instead of rewriting one file in place).
_GENERATED_ROOT_NAME = "generated"


def _generated_candidate_dir(name) -> Path | None:
    """None unless `name` is SLUG_RE-valid (closes traversal the same way
    _heal_candidate_path's regex does — SLUG_RE's character class contains
    no '/', so '..'/absolute/subdirectory shapes all fail by SHAPE, not by a
    separate check) AND isn't the reserved `rejected` name (that's the
    archive subdirectory generate-reject moves into, never a candidate
    itself) AND actually exists as a directory directly under
    $AP_STATE/qa/generated/."""
    if not isinstance(name, str) or not SLUG_RE.match(name) or name == "rejected":
        return None
    p = QA_DIR / _GENERATED_ROOT_NAME / name
    return p if p.is_dir() else None


def _do_generate_approve(name: str) -> dict:
    """Approve a generated candidate: validate EVERYTHING first (name shape,
    candidate dir exists, has all three of intent.md/steps.json/meta.json,
    steps.json is a non-empty list, intent.md has a readable one-line
    intent, and — the one check with no heal-approve analogue — a flow of
    this name doesn't already exist in the committed library) — only once
    every check passes does the directory actually move, mirroring
    _do_heal_approve's "every validation runs BEFORE any write" discipline.
    The move itself preserves the candidate's meta.json BYTE-FOR-BYTE (no
    read-modify-write the way heal-approve's meta.json update is) — its
    `requires`/`provides_auth` (and the `generated: true` marker, left in
    place as harmless provenance — _read_flow_meta only ever reads the keys
    it knows about) survive untouched simply because nothing rewrites the
    file. FLOWMAP.md gets a fresh row (qa_teach._update_flowmap, same
    helper `orbit teach`/the dashboard's teach-save path already use) —
    a moved-in flow has never had a row before, unlike a heal (which only
    ever touches an EXISTING flow's last-verdict cell)."""
    cand_dir = _generated_candidate_dir(name)
    if cand_dir is None:
        return {"ok": False, "msg": f"'{name}' is not a pending generated candidate"}
    for fname in ("intent.md", "steps.json", "meta.json"):
        if not (cand_dir / fname).is_file():
            return {"ok": False,
                    "msg": f"candidate '{name}' is missing {fname} — cannot approve, reject it instead"}
    try:
        steps = json.loads((cand_dir / "steps.json").read_text())
        if not isinstance(steps, list) or not steps:
            raise ValueError
    except (OSError, ValueError):
        return {"ok": False,
                "msg": f"candidate '{name}' has malformed or empty steps.json — reject it instead"}
    try:
        meta_raw = json.loads((cand_dir / "meta.json").read_text())
        if not isinstance(meta_raw, dict):
            raise ValueError
    except (OSError, ValueError):
        return {"ok": False, "msg": f"candidate '{name}' has malformed meta.json — reject it instead"}
    intent_line, entry_path = _read_flow_intent(cand_dir)
    if not intent_line:
        return {"ok": False,
                "msg": f"candidate '{name}' has no readable intent in intent.md — reject it instead"}

    flows_root = _qa_flows_root()
    flow_dir = flows_root / name
    if flow_dir.exists():
        return {"ok": False,
                "msg": f"a flow named '{name}' already exists at {flow_dir} — cannot approve "
                       f"(reject the candidate, or rename/remove the existing flow first)"}

    # All-or-nothing point of no return: every check above passed.
    # shutil.move is atomic (os.rename) when $AP_STATE and $AP_HOME share a
    # filesystem — the normal case (both under the target's .autopilot/). On
    # the rare cross-device layout it degrades to copy-then-delete, which is
    # NOT atomic: a mid-copy failure can leave partial debris at flow_dir while
    # the candidate itself survives intact under generated/<name> (the source
    # is removed only after a clean copy, so nothing is ever lost) — a retry
    # then trips the "already exists" refusal above until the debris is cleared
    # by hand. That's a safe-but-annoying failure, never data loss or a clobber.
    flows_root.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(cand_dir), str(flow_dir))
    except OSError as e:
        return {"ok": False, "msg": f"could not move candidate '{name}' into the flow library ({e})"}

    flowmap_path = flows_root.parent / "FLOWMAP.md"
    try:
        qa_teach._update_flowmap(flowmap_path, name, intent_line, entry_path or "", "never run")
    except OSError as e:
        # Mirrors qa_teach.main()/do_qa_teach_save: a FLOWMAP write failure
        # never masks an already-written flow — the flow itself is done.
        return {"ok": True,
                "msg": f"Approved '{name}' — now a real flow; FLOWMAP update failed ({e})."}
    return {"ok": True, "msg": f"Approved '{name}' — now a real flow in the library."}


def _do_generate_reject(name: str) -> dict:
    """Archive a generated candidate under generated/rejected/<name> — no
    reason required (unlike heal-reject): a generate candidate is an AI's
    own first-draft proposal the operator never approved into anything, not
    a mined correction to an EXISTING flow the operator needs to justify
    turning down. A same-name re-reject (an already-rejected `name` sitting
    under rejected/) is disambiguated with a UTC timestamp suffix so a
    second rejection of a same-slugged regeneration is never silently
    dropped or clobbered."""
    cand_dir = _generated_candidate_dir(name)
    if cand_dir is None:
        return {"ok": False, "msg": f"'{name}' is not a pending generated candidate"}
    rejected_root = QA_DIR / _GENERATED_ROOT_NAME / "rejected"
    rejected_root.mkdir(parents=True, exist_ok=True)
    dest = rejected_root / name
    if dest.exists():
        dest = rejected_root / f"{name}--{_qa_now_iso().replace(':', '')}"
    try:
        shutil.move(str(cand_dir), str(dest))
    except OSError as e:
        return {"ok": False, "msg": f"could not archive candidate '{name}' ({e})"}
    return {"ok": True, "msg": f"Rejected candidate '{name}' (archived under generated/rejected/)."}


# ── QA persistent "Dev session" — bring qa.services up ONCE and keep them
# warm, so `orbit qa --flow`/teach stop rebooting the frontend/backend on
# every run (a React dev server recompiling from scratch is the whole reason
# this exists). Own lock, deliberately separate from _TEACH_LOCK — the two
# sessions can each independently start/stop and neither should ever block
# waiting on the other's lock. States: stopped -> starting -> running |
# error. `_QA_SERVICES["state"]` doubles as the ownership/cancellation
# signal a background bring-up thread checks (see _qa_services_bring_up) —
# there's only ever ONE of these, module-wide (an operator's own dashboard
# tab, same posture as _teach_session), so unlike teach's session-identity
# check, "is this thread's own bring-up still wanted" reduces to "is the
# state still 'starting'".
_QA_SERVICES_LOCK = threading.Lock()
# `ports` holds the workspace-wide qa.services mutex (qa_check's
# SERVICES_LOCK_NAME) for the session's WHOLE lifetime - see
# do_qa_services_start.
_QA_SERVICES = {"state": "stopped", "handles": None, "error": None, "ports": None}
# How long a Start waits for the mutex before telling the operator the ports
# are busy. Short on purpose: whoever holds it is a loop QA gate or a
# sweep/explore/generate run that lasts minutes, so waiting would just hang
# the request; a click is better answered with "busy, try again".
QA_SERVICES_LOCK_WAIT = 2


def _qa_services_release_ports() -> None:
    """Drop the qa.services mutex if this session holds it. Idempotent, and
    never raises - a release failure must not break the Stop path. (Process
    death releases it too: flock lives on the fd, which the kernel tears down
    on exit, so a crashed dashboard never wedges the lock.)"""
    with _QA_SERVICES_LOCK:
        stack = _QA_SERVICES.get("ports")
        _QA_SERVICES["ports"] = None
    if stack is not None:
        try:
            stack.close()
        except Exception:
            pass


def _qa_services_bring_up(qa_cfg: dict, log_dir: Path) -> None:
    """Runs off a background thread spawned by do_qa_services_start(): brings
    qa.services up via qa_env.up() and flips state to `running` — or, on
    ANY failure, `error` (qa_env.up() has ALREADY torn down everything IT
    started before raising — see its own contract — so there's nothing left
    to tear down here). If /qa-services-stop ran while this was mid-flight
    (state is no longer `starting` by the time up() returns), this bring-up
    is cancelled: don't resurrect a stopped session — tear down what THIS
    call just started instead of leaking it."""
    try:
        handles = qa_env.up(REPO, qa_cfg, log_dir)
    except Exception as e:
        with _QA_SERVICES_LOCK:
            if _QA_SERVICES["state"] == "starting":
                _QA_SERVICES["state"] = "error"
                _QA_SERVICES["error"] = f"could not bring qa.services up ({e})"
                _QA_SERVICES["handles"] = None
        # Nothing is bound any more (qa_env.up tore down what it started), so
        # the mutex must go back - otherwise a failed Start would lock every
        # other qa.services consumer out until the dashboard restarts.
        _qa_services_release_ports()
        return

    with _QA_SERVICES_LOCK:
        cancelled = _QA_SERVICES["state"] != "starting"
        if not cancelled:
            _QA_SERVICES["handles"] = handles
            _QA_SERVICES["state"] = "running"
            _QA_SERVICES["error"] = None
    if cancelled:
        qa_env.down(handles)   # stopped while services were coming up
        # The Stop that cancelled us already released the mutex; this is the
        # belt-and-braces call for the ordering where it had not yet.
        _qa_services_release_ports()


def do_qa_services_start() -> dict:
    """POST /qa-services-start — brings the target's qa.services up ONCE and
    keeps them running (unlike a plain `orbit qa --flow`/`orbit teach`,
    which each bring their own copy up and tear it down every invocation).
    Requires a qa: block with at least one service configured. Idempotent:
    already running/starting just reports so — a double-click (or a stale
    tab re-posting) must never spawn a second overlapping set of services.

    Ports: this is NOT the only thing that can bind qa.services' fixed
    ports. The loop's own QA gate (qa_check.py) and a manual
    --regression/--explore/--generate run each bring qa.services up and down
    themselves. All of them - this route included - now serialize on the one
    workspace mutex (qa_check.SERVICES_LOCK_NAME), so the loser is told the
    ports are busy instead of racing for the bind. This session holds that
    mutex from Start until Stop, because that is exactly how long the ports
    stay bound.

    Still uncovered, and deliberately so: an un-attached `orbit teach`
    mid-recording brings its OWN qa.services up (see _qa_teach_bring_up's
    non-attached branch) and is not on this mutex, because a teach recording
    is an interactive session an operator is watching, not an unattended
    consumer to be silently skipped."""
    with _QA_SERVICES_LOCK:
        if _QA_SERVICES["state"] in ("running", "starting"):
            return {"ok": True, "msg": f"already {_QA_SERVICES['state']}"}
        cfg = _load_cfg()
        qa_cfg = cfg.get("qa") if isinstance(cfg.get("qa"), dict) else None
        if not isinstance(qa_cfg, dict) or not qa_cfg.get("services"):
            return {"ok": False,
                    "msg": "no 'qa: services' configured in .autopilot/config.yaml — "
                           "see config/schema.yaml's qa.services block and add one first"}
        # Take the workspace's qa.services mutex BEFORE promising the operator
        # anything. ExitStack is the stdlib way to hold a context manager open
        # past the block that entered it: the flock lives on the fd (not the
        # thread), so entering here and closing on the Stop request is sound.
        lock_path = qa_check.services_lock_path(AP_STATE)
        stack = contextlib.ExitStack()
        try:
            stack.enter_context(locks.hold(lock_path, timeout=QA_SERVICES_LOCK_WAIT,
                                           break_stale=True))
        except locks.LockTimeout:
            return {"ok": False,
                    "msg": "your app's ports are busy - the loop's QA gate or a QA "
                           "sweep/explore/generate run is using them right now. "
                           "Try again once it finishes."}
        _QA_SERVICES["ports"] = stack
        _QA_SERVICES["state"] = "starting"
        _QA_SERVICES["error"] = None
        _QA_SERVICES["handles"] = None

    log_dir = AP_HOME / "state" / "qa" / "dev-logs"
    threading.Thread(target=_qa_services_bring_up, args=(qa_cfg, log_dir), daemon=True).start()
    return {"ok": True, "msg": "bringing your app up…"}


def do_qa_services_stop() -> dict:
    """POST /qa-services-stop — tears the persistent session down (if any
    handles ever came up) and resets to `stopped`, from WHATEVER state it
    was in (running, error, or even starting — flipping state away from
    `starting` here is what _qa_services_bring_up's own cancellation check
    sees, so a stop mid-bring-up tears down as soon as that thread notices,
    not left running forever). qa_env.down on an empty/None list is always
    safe (see its own contract)."""
    with _QA_SERVICES_LOCK:
        handles = _QA_SERVICES.get("handles")
        _QA_SERVICES["state"] = "stopped"
        _QA_SERVICES["handles"] = None
        _QA_SERVICES["error"] = None
    if handles:
        qa_env.down(handles)
    # Ports first freed, mutex released after: the next consumer must never be
    # let in while this session's services are still tearing down.
    _qa_services_release_ports()
    return {"ok": True, "msg": "app stopped"}


def do_qa_services_restart(payload: dict) -> dict:
    """POST /qa-services-restart {service?} — restarts ONE named service (via
    qa_env.restart_one, updating the stored handles) or, with no `service`,
    the whole session (down then up again). Requires `running` — restarting
    a session that never came up (stopped/error/starting) makes no sense;
    Start it fresh instead. `service`, if given, must be one of the
    currently configured qa.services names. Never crashes the handler: any
    qa_env failure is reported as {ok: false, msg}, same as every other QA
    action here — and on a named-service failure, the OTHER still-alive
    handles are deliberately kept in `_QA_SERVICES["handles"]` (not
    discarded to None) so a later Stop/atexit still tears them down instead
    of orphaning them; a restart-ALL failure has nothing left to keep (the
    old set was already down()'d before the failed up() attempt)."""
    service = (payload.get("service") or "").strip()
    with _QA_SERVICES_LOCK:
        if _QA_SERVICES["state"] != "running":
            return {"ok": False,
                    "msg": f"app is '{_QA_SERVICES['state']}', not running — start it first"}
        handles = _QA_SERVICES["handles"] or []

    cfg = _load_cfg()
    qa_cfg = cfg.get("qa") if isinstance(cfg.get("qa"), dict) else {}
    configured = qa_cfg.get("services") or []
    names = [str(s.get("name") or f"service{i}") for i, s in enumerate(configured) if isinstance(s, dict)]
    if service and service not in names:
        return {"ok": False,
                "msg": f"no configured service named '{service}' — have {', '.join(names) or '(none)'}"}

    log_dir = AP_HOME / "state" / "qa" / "dev-logs"
    try:
        if service:
            new_handles = qa_env.restart_one(REPO, qa_cfg, service, handles, log_dir)
        else:
            qa_env.down(handles)
            new_handles = qa_env.up(REPO, qa_cfg, log_dir)
    except Exception as e:
        with _QA_SERVICES_LOCK:
            _QA_SERVICES["state"] = "error"
            _QA_SERVICES["error"] = f"restart failed ({e})"
            if service:
                # Named-service restart: qa_env.restart_one's contract is
                # that on failure ONLY the named service is dead (its old
                # process was already killed before the fresh spawn attempt
                # was tried) — every OTHER handle is untouched and still
                # alive. Keep tracking the pre-call `handles` list (the
                # local var above, unmutated) so a later /qa-services-stop
                # or the atexit cleanup still tears the survivors down
                # instead of orphaning them; a future down() on the now-dead
                # named entry is a harmless no-op (_kill_one no-ops once
                # proc.poll() is not None).
                _QA_SERVICES["handles"] = handles
            else:
                # Restart-ALL already down()'d the old set before attempting
                # up() again, and up() itself tears down anything it
                # partially started before raising — nothing from the old
                # set survives to track.
                _QA_SERVICES["handles"] = None
        if not service:
            # Nothing of this session is bound any more, so hand the ports
            # mutex back instead of holding it in `error` until someone
            # remembers to click Stop. A NAMED-service failure keeps it: the
            # other services are still up and still own their ports.
            _qa_services_release_ports()
        return {"ok": False, "msg": f"restart failed: {e}"}

    with _QA_SERVICES_LOCK:
        # A concurrent /qa-services-stop could have run while the restart
        # above was in flight — don't resurrect a session the operator just
        # stopped; tear down what this restart just brought up instead of
        # leaking it.
        stopped_meanwhile = _QA_SERVICES["state"] == "stopped"
        if not stopped_meanwhile:
            _QA_SERVICES["handles"] = new_handles
            _QA_SERVICES["state"] = "running"
            _QA_SERVICES["error"] = None
    if stopped_meanwhile:
        qa_env.down(new_handles)
        _qa_services_release_ports()   # the Stop released it already; idempotent
        return {"ok": True, "msg": "app was stopped while restarting — nothing to do"}
    return {"ok": True, "msg": f"restarted {service or 'all services'}"}


def qa_services_state() -> dict | None:
    """build_state's `qa_services` key — present ONLY when the target has a
    qa: block with `services` configured (else omitted entirely, so a
    target with no dev-session story sees a byte-identical /api/state —
    same discipline as qa_flow_library's own None-means-omitted contract).
    Drives the dashboard's "Dev session" panel: state (stopped/starting/
    running/error) + the configured service names/ready URLs, read fresh
    from config.yaml every poll (not from _QA_SERVICES's own possibly-stale
    handles) so a config edit is reflected even while stopped."""
    cfg = _load_cfg()
    qa_cfg = cfg.get("qa") if isinstance(cfg.get("qa"), dict) else {}
    services = qa_cfg.get("services")
    if not services or not isinstance(services, list):
        return None
    rows = [{"name": str(s.get("name") or f"service{i}"),
             "ready": s.get("ready") if isinstance(s.get("ready"), str) else None}
            for i, s in enumerate(services) if isinstance(s, dict)]
    with _QA_SERVICES_LOCK:
        state = _QA_SERVICES["state"]
        error = _QA_SERVICES["error"]
    return {"state": state, "services": rows, "error": error}


def _qa_services_atexit_cleanup() -> None:
    """Best-effort orphan guard mirroring _teach_atexit_cleanup: if the
    persistent dev session's services are (or were becoming) up when the
    dashboard process exits cleanly (Ctrl-C, /restart-dashboard), tear them
    down instead of leaking them. Can't run past a SIGKILL/crash — which is
    exactly why qa_env's own services are spawned with their own process
    group regardless (see qa_env._spawn's start_new_session=True)."""
    with _QA_SERVICES_LOCK:
        handles = _QA_SERVICES.get("handles")
    if handles:
        qa_env.down(handles)


atexit.register(_qa_services_atexit_cleanup)


def do_qa_action(payload: dict) -> dict:
    """POST /qa-action — flow-library actions from the QA view. run-flow
    validates the flow name (SLUG_RE discipline, same as qa_teach.py/
    qa_run.py) and that the flow actually exists, then spawns `qa_run.py
    <target> --flow <name>` DETACHED (do_epic_action's Popen pattern),
    logging to $AP_STATE/logs/qa-run-<name>.log — plus `--attach` whenever
    the persistent "Dev session" (_QA_SERVICES) is `running`: the flow then
    attaches to the already-warm app (see skills/orbit-qa-flow.md step 1)
    instead of qa_env bringing its own copy of qa.services up and killing it
    again on exit. heal-approve/heal-reject (Task 26) close the self-healing
    loop — see _do_heal_approve/_do_heal_reject, which mirror
    do_calibration_action's precedent.
    run-regression (Task 27) joins run-flow: a detached spawn of `qa_run.py
    <target> --regression` — no flow param, and deliberately no
    --if-changed (a human click means "sweep now", unconditionally; the
    cursor/cadence gating is the run.sh loop hook's own concern) — logged to
    $AP_STATE/logs/qa-regression.log. run-explore (Task 29) is the same
    detached-spawn shape again (`qa_run.py <target> --explore`, logged to
    $AP_STATE/logs/qa-explore.log) but gated FIRST on `_qa_exploratory_enabled()`
    — exploratory crawling is opt-in, so a disabled target refuses the
    action (and never spawns anything) even if a stale client somehow still
    posts it; qa_run.py's own --explore enforces the identical gate
    independently, so the two can never disagree.
    run-generate (AI test generation) is the same detached-spawn shape once
    more (`qa_run.py <target> --generate`, logged to
    $AP_STATE/logs/qa-generate.log) — no extra opt-in gate to check here
    (unlike run-explore's _qa_exploratory_enabled()): generating CANDIDATE
    tests never touches the committed flow library by itself, so it needs no
    config flag beyond a `qa:` block existing at all (qa_run.py's own
    --generate enforces that, independently). Like run-flow, it passes
    --attach whenever the persistent "Dev session" is `running`.
    generate-approve/generate-reject close the AI-generation loop exactly
    the way heal-approve/heal-reject close the self-healing one — see
    _do_generate_approve/_do_generate_reject."""
    action = (payload.get("action") or "").strip()
    if action == "heal-approve":
        return _do_heal_approve((payload.get("file") or "").strip())
    if action == "heal-reject":
        return _do_heal_reject((payload.get("file") or "").strip(), payload.get("reason") or "")
    if action == "generate-approve":
        return _do_generate_approve((payload.get("name") or "").strip())
    if action == "generate-reject":
        return _do_generate_reject((payload.get("name") or "").strip())
    if action == "run-regression":
        log = LOGDIR / "qa-regression.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as lf:
            subprocess.Popen([sys.executable, str(ENGINE / "qa_run.py"), str(REPO), "--regression"],
                             stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "msg": "regression sweep started"}
    if action == "run-explore":
        if not _qa_exploratory_enabled():
            return {"ok": False,
                    "msg": "exploratory crawling is disabled — set qa.exploratory.enabled: true "
                           "in .autopilot/config.yaml to turn it on"}
        log = LOGDIR / "qa-explore.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as lf:
            subprocess.Popen([sys.executable, str(ENGINE / "qa_run.py"), str(REPO), "--explore"],
                             stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "msg": "exploratory crawl started"}
    if action == "run-generate":
        log = LOGDIR / "qa-generate.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with _QA_SERVICES_LOCK:
            attach = _QA_SERVICES["state"] == "running"
        cmd = [sys.executable, str(ENGINE / "qa_run.py"), str(REPO), "--generate"]
        if attach:
            cmd.append("--attach")
        with log.open("ab") as lf:
            subprocess.Popen(cmd, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True)
        msg = "test generation started" + (" (attached to the running dev session)" if attach else "")
        return {"ok": True, "msg": msg}
    flow = (payload.get("flow") or "").strip()
    if action != "run-flow":
        return {"ok": False, "msg": f"unknown qa action '{action}'"}
    if not SLUG_RE.match(flow):
        return {"ok": False,
                "msg": f"'{flow}' is not a valid flow name — use a lowercase slug "
                       f"(letters/digits/./_/-, starting with a letter or digit)"}
    flow_dir = _qa_flows_root() / flow
    if not flow_dir.is_dir():
        return {"ok": False, "msg": f"no flow named '{flow}' at {flow_dir}"}
    log = LOGDIR / f"qa-run-{flow}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with _QA_SERVICES_LOCK:
        attach = _QA_SERVICES["state"] == "running"
    cmd = [sys.executable, str(ENGINE / "qa_run.py"), str(REPO), "--flow", flow]
    if attach:
        cmd.append("--attach")
    with log.open("ab") as lf:
        subprocess.Popen(cmd, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True)
    msg = f"flow {flow} started" + (" (attached to the running dev session)" if attach else "")
    return {"ok": True, "msg": msg}


# ── QA teach session — the dashboard's "New test / Record" panel, the web
# equivalent of `orbit teach` (qa_teach.py). A CLI `teach` run is one process:
# it blocks on qa_env.up(), then blocks on a foreground `codegen` recorder,
# then blocks on stdin for intent/criteria, all before writing anything. The
# dashboard has no stdin and must never block an HTTP handler for however
# long qa.services take to become ready (up to ready_timeout_seconds) or for
# however long the operator spends driving the recorder — so the same three
# steps are spread across THREE requests instead of one process:
#   POST /qa-teach-start  → validates exactly like qa_teach.main(), then
#                           returns immediately while a background thread
#                           does the (slow) qa_env.up() + spawns codegen,
#                           THEN blocks on the recorder's own exit and tears
#                           qa.services down itself the moment it happens.
#   GET  /qa-teach-status → polled every ~1.5s by the panel; a pure read of
#                           starting -> recording -> recorded|failed (no
#                           side effects — the background thread above is
#                           what drives that transition, not this route).
#   POST /qa-teach-save   → once `recorded` (which already implies
#                           qa.services are down), the operator's intent/
#                           criteria are POSTed; reuses qa_teach._write_flow/
#                           _update_flowmap verbatim, then removes the
#                           scratch spec_dir and clears the session.
# Exactly one session at a time (a single module-level dict + lock — this is
# an operator's own dashboard tab, not a multi-tenant API). `_teach_session`
# is the ONLY mutable global among AP_HOME/AP_STATE/REPO/etc in this file
# (everything else here is either read-only or reloaded whole from disk).
_TEACH_LOCK = threading.Lock()
_teach_session: dict | None = None   # None = no session active


def _teach_kill_proc(proc) -> None:
    """Best-effort: terminate the codegen recorder if it's still alive, same
    grace-then-force shape as qa_env's own _kill_one (SIGTERM, a few seconds
    grace, then kill) — but simpler, since a lone Popen (not a whole process
    group) is all a `codegen` child ever is here. Never raises."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


def _teach_claim_teardown(sess: dict) -> bool:
    """Atomically claims responsibility for tearing THIS session's qa.services
    down: several independent paths can each decide a given session's
    services need tearing down (the bring-up thread's own post-recording
    wait, /qa-teach-cancel, and the atexit hook) — under _TEACH_LOCK,
    check-and-set `sess["torn_down"]` so exactly one of them actually calls
    qa_env.down(handles); every other path sees it already True and skips
    its own call. Returns True iff the CALLER must now call
    qa_env.down(sess["handles"]) itself — deliberately done outside this
    lock (down() can block for a few seconds per service tearing down
    gracefully; nothing else should be blocked on that)."""
    with _TEACH_LOCK:
        if sess.get("torn_down"):
            return False
        sess["torn_down"] = True
        return True


def _qa_teach_bring_up(sess: dict, qa_cfg: dict, log_dir: Path) -> None:
    """Runs off the request thread (spawned by do_qa_teach_start): brings
    qa.services up, launches the recorder DETACHED (unlike qa_teach.py's own
    foreground, blocking `_run_codegen`), then — still off this same thread —
    blocks on the recorder's own exit and tears qa.services down the moment
    it happens, before ever flipping the session to `recorded`/`failed`.
    This (not GET /qa-teach-status) is what detects the recorder finishing:
    a GET must be a pure read, never a place a teardown side effect can be
    triggered from a blind, token-less, cross-origin-reachable request.
    Mutates `sess` (the exact dict stored in _teach_session) in place at
    each checkpoint, always re-checking `_teach_session is sess` under the
    lock first: if /qa-teach-cancel ran while this thread was mid-flight,
    cancel already tore down whatever existed AT THAT MOMENT — anything this
    thread brings up/spawns AFTER that is this thread's own responsibility
    to tear back down, never left running as an orphan. Every actual
    qa_env.down() call (here and in cancel/atexit) goes through
    `_teach_claim_teardown` so the same handles are never torn down twice.
    Any exception bringing services up or launching the recorder (a
    malformed qa.services entry, a launch OSError, a malformed
    ORBIT_CODEGEN_CMD raising ValueError out of qa_teach.codegen_argv's
    shlex.split, a ready-timeout QaEnvError — deliberately `except Exception`,
    not just OSError: an uncaught exception here would silently kill this
    daemon thread and strand the session in `starting` forever with
    qa.services left running) tears down whatever THIS call started and
    marks the session `failed` with the error message — the panel's next
    status poll surfaces it.

    ATTACH MODE: if the persistent "Dev session" (_QA_SERVICES) is already
    `running` when this thread starts, qa_env.up() is skipped ENTIRELY —
    calling it again would try to bind the exact same ports/processes the
    persistent session already holds and fail. `sess["attached"]` is set
    True and `sess["handles"]` stays `[]`; every qa_env.down() call below
    (and in do_qa_teach_cancel/_teach_atexit_cleanup) checks that flag and
    skips teardown when it's set — the persistent session's own
    /qa-services-stop is the ONLY thing that ever tears those services
    down. When _QA_SERVICES is not running, teach behaves exactly as
    before: it brings its own qa.services up and tears them back down
    itself, same as always."""
    with _QA_SERVICES_LOCK:
        attached = _QA_SERVICES["state"] == "running"

    handles = []
    if attached:
        with _TEACH_LOCK:
            cancelled = _teach_session is not sess
            if not cancelled:
                sess["attached"] = True
                sess["handles"] = handles
        if cancelled:
            return   # nothing this thread started — nothing to tear down
    else:
        try:
            handles = qa_env.up(REPO, qa_cfg, log_dir)
        except Exception as e:
            with _TEACH_LOCK:
                if _teach_session is sess:
                    sess["state"] = "failed"
                    sess["error"] = f"could not bring qa.services up ({e})"
            return

        with _TEACH_LOCK:
            cancelled = _teach_session is not sess
            if not cancelled:
                sess["handles"] = handles
        if cancelled:
            if _teach_claim_teardown(sess):
                qa_env.down(handles)   # cancelled while services were coming up
            return

    try:
        proc = subprocess.Popen(
            qa_teach.codegen_argv(sess["url"], sess["spec_path"], sess["browser"]),
            cwd=str(REPO),
            env={**os.environ, "ORBIT_CODEGEN_OUTPUT": sess["spec_path"],
                 "ORBIT_CODEGEN_URL": sess["url"]},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:   # OSError (launch) or ValueError (bad ORBIT_CODEGEN_CMD)
        if not attached and _teach_claim_teardown(sess):
            qa_env.down(handles)
        with _TEACH_LOCK:
            if _teach_session is sess:
                sess["state"] = "failed"
                sess["error"] = f"could not launch the recorder ({e})"
        return

    with _TEACH_LOCK:
        cancelled = _teach_session is not sess
        if not cancelled:
            sess["proc"] = proc
            sess["state"] = "recording"
    if cancelled:
        _teach_kill_proc(proc)
        if not attached and _teach_claim_teardown(sess):
            qa_env.down(handles)          # cancelled while codegen was launching
        return

    # Block on the recorder's own exit — the same "foreground" wait
    # qa_teach.py's CLI _run_codegen does, just off the request thread
    # instead of the operator's terminal. Whatever happens next (services
    # torn down, state flipped) happens right here, unconditionally, whether
    # or not anyone is still polling /qa-teach-status.
    rc = proc.wait()

    with _TEACH_LOCK:
        if _teach_session is not sess:
            return   # cancelled meanwhile — cancel's own kill+down+rmtree covers this
        spec_p = Path(sess["spec_path"])
        ok = rc == 0 and spec_p.exists() and spec_p.stat().st_size > 0

    if not attached and _teach_claim_teardown(sess):
        qa_env.down(handles)

    with _TEACH_LOCK:
        if _teach_session is not sess:
            return
        if ok:
            sess["state"] = "recorded"
            sess["error"] = None
        else:
            sess["state"] = "failed"
            sess["error"] = (f"recorder exited {rc} — no flow recorded" if rc != 0
                              else "recorder produced no output — no flow recorded")

    if not ok:
        # Nothing will ever save a failed recording — clean its scratch spec
        # dir up right away rather than leaving it for an explicit cancel (or
        # the atexit hook) to eventually get to.
        shutil.rmtree(sess["spec_dir"], ignore_errors=True)


def do_qa_teach_start(payload: dict) -> dict:
    """POST /qa-teach-start — validates exactly like `orbit teach`'s
    qa_teach.main() (SLUG_RE → qa: block present → flows_dir shape →
    flow-not-exists → URL resolved), refuses a second concurrent session,
    then hands the slow part (qa_env.up + spawning codegen) to a background
    thread and returns immediately."""
    global _teach_session
    name = (payload.get("name") or "").strip()
    url_in = (payload.get("url") or "").strip()

    with _TEACH_LOCK:
        if _teach_session is not None:
            return {"ok": False,
                    "msg": f"a teach session for '{_teach_session['name']}' is already "
                           f"active — cancel or finish it first"}
        if not SLUG_RE.match(name):
            return {"ok": False,
                    "msg": f"'{name}' is not a valid flow name — use a lowercase slug "
                           f"(letters/digits/./_/-, starting with a letter or digit)"}
        cfg = _load_cfg()
        qa_cfg = cfg.get("qa")
        if not isinstance(qa_cfg, dict):
            return {"ok": False,
                    "msg": "no 'qa:' block configured in .autopilot/config.yaml — "
                           "see config/schema.yaml's qa: section and add one first"}
        flows_dir = qa_cfg.get("flows_dir") or DEFAULT_QA_FLOWS_DIR
        bad = _bad_flows_dir_reason(flows_dir)
        if bad:
            return {"ok": False, "msg": f"qa.flows_dir '{flows_dir}' is invalid ({bad})"}
        flow_dir = AP_HOME / flows_dir / name
        if flow_dir.exists():
            return {"ok": False,
                    "msg": f"a flow named '{name}' already exists at {flow_dir} — pick a new name"}
        url = url_in or qa_cfg.get("base_url")
        if not url:
            return {"ok": False,
                    "msg": "no target URL — pass one or set qa.base_url in config.yaml"}

        spec_dir = tempfile.mkdtemp(prefix="orbit-teach-cc-")
        spec_path = str(Path(spec_dir) / "steps.spec.ts")
        flowmap = (AP_HOME / flows_dir).parent / "FLOWMAP.md"
        sess = {
            "state": "starting", "name": name, "url": url, "browser": qa_cfg.get("browser"),
            "flow_dir": flow_dir, "flowmap": flowmap,
            "spec_dir": spec_dir, "spec_path": spec_path,
            "handles": [], "proc": None, "error": None, "torn_down": False,
            # Set True by _qa_teach_bring_up when the persistent "Dev
            # session" (_QA_SERVICES) is already running — see its own
            # docstring's "ATTACH MODE" for what this flag then suppresses.
            "attached": False,
        }
        _teach_session = sess

    log_dir = AP_HOME / "state" / "qa" / "teach-logs"
    threading.Thread(target=_qa_teach_bring_up, args=(sess, qa_cfg, log_dir), daemon=True).start()
    return {"ok": True, "msg": f"bringing your app up for '{name}'…"}


def do_qa_teach_status() -> dict:
    """GET /qa-teach-status — polled every ~1.5s by the panel while a session
    is active. A PURE READ of `sess["state"]` — no proc.poll(), no
    qa_env.down(), no mutation of any kind. The `recording` -> `recorded`/
    `failed` transition (and tearing qa.services down when it happens) is
    now entirely _qa_teach_bring_up's job, off its own background thread,
    because this route is reachable via a blind, token-less, cross-origin
    GET (only the Host header is checked — see do_GET) and a side effect
    like downing qa.services must never be triggerable from that."""
    with _TEACH_LOCK:
        sess = _teach_session
        if sess is None:
            return {"active": False}
        return {"active": True, "state": sess["state"], "name": sess["name"],
                "error": sess["error"]}


def do_qa_teach_save(payload: dict) -> dict:
    """POST /qa-teach-save — once `recorded`, writes the taught flow exactly
    the way `orbit teach` does (reusing qa_teach._write_flow/_update_flowmap
    verbatim), removes the scratch spec_dir, and clears the session.
    qa.services are NOT downed here — by the time a session can reach
    `recorded`, _qa_teach_bring_up has already torn them down itself (that's
    the whole point of it blocking on the recorder's exit instead of a GET
    poll doing it). An invalid intent/criteria/requires payload leaves the
    session in `recorded` (never consumes it) so the operator can fix the
    form and resubmit without re-recording.
    Claims the session by flipping state to `saving` (still under the same
    lock acquisition as the `recorded` check — no other request can observe
    `recorded` and start a second save, or a cancel run out from under a
    write in progress) BEFORE releasing the lock to do the (slower) file
    I/O; do_qa_teach_cancel refuses outright while state is `saving`.

    `requires` (test chaining, Task "prerequisites" — comma-separated string
    or a list, same dual-shape acceptance as `criteria`) and `provides_auth`
    (any truthy value) are threaded straight into qa_teach._write_flow, which
    is the one place that validates each requires entry's SLUG_RE shape
    (raises on an invalid one — caught below, same as any other _write_flow
    failure). A named prerequisite that's SLUG_RE-valid but doesn't exist as
    a flow YET is deliberately NOT blocked — warn, don't block (the operator
    may be teaching flows out of order, or a chain's own login flow hasn't
    been recorded yet) — the success message just notes which names weren't
    found. A flow naming ITSELF in `requires` gets its own clearer note
    ("a flow can't require itself") instead of being lumped in with that
    "not taught yet" case — its own dir genuinely doesn't exist yet at this
    point (mid-save), but that's not the real problem, so saying so would be
    misleading; qa_run.py's `_resolve_chain` already refuses a self-loop at
    run time (loud exit 2), this is just a friendlier heads-up now."""
    global _teach_session
    with _TEACH_LOCK:
        sess = _teach_session
        if sess is None:
            return {"ok": False, "msg": "no teach session is active"}
        if sess["state"] != "recorded":
            return {"ok": False, "msg": f"nothing to save yet — session is '{sess['state']}'"}

        intent = (payload.get("intent") or "").strip()
        criteria_in = payload.get("criteria")
        if isinstance(criteria_in, str):
            criteria = [ln.strip() for ln in criteria_in.splitlines() if ln.strip()]
        elif isinstance(criteria_in, list):
            criteria = [str(c).strip() for c in criteria_in if str(c).strip()]
        else:
            criteria = []
        if not intent:
            return {"ok": False, "msg": "an intent (one line) is required"}
        if not criteria:
            return {"ok": False, "msg": "at least one success criterion is required"}

        requires_in = payload.get("requires")
        if isinstance(requires_in, str):
            requires = [s.strip() for s in requires_in.split(",") if s.strip()]
        elif isinstance(requires_in, list):
            requires = [str(s).strip() for s in requires_in if str(s).strip()]
        else:
            requires = []
        bad = next((r for r in requires if not SLUG_RE.match(r)), None)
        if bad is not None:
            return {"ok": False,
                    "msg": f"'{bad}' is not a valid prerequisite flow name — use a lowercase "
                           f"slug (letters/digits/./_/-, starting with a letter or digit)"}
        provides_auth = bool(payload.get("provides_auth"))

        sess["state"] = "saving"
        name, url = sess["name"], sess["url"]
        flow_dir, flowmap = sess["flow_dir"], sess["flowmap"]
        spec_path, spec_dir = sess["spec_path"], sess["spec_dir"]

    # A flow naming ITSELF in `requires` is a distinct case from a genuinely
    # missing prerequisite: `flow_dir` (== flow_dir.parent / name) never
    # exists yet at this point (we're still mid-save, before _write_flow
    # below), so a naive "not (flow_dir.parent / r).is_dir()" check would
    # lump a self-reference in with "not taught yet" — confusing, since a
    # flow obviously can't be taught before itself, that's not what's wrong
    # here. Checked separately so the note says the real problem instead.
    # Run-time `_resolve_chain` (qa_run.py) already refuses a self-loop
    # (loud exit 2) — this is purely a friendlier teach-time message, not a
    # save-time block: the operator may fix `requires` later, same
    # warn-don't-block posture as a missing prerequisite.
    self_requires = name in requires
    missing_prereqs = [r for r in requires if r != name and not (flow_dir.parent / r).is_dir()]

    try:
        qa_teach._write_flow(flow_dir, name, url, intent, criteria, Path(spec_path),
                              requires=requires, provides_auth=provides_auth)
    except Exception as e:
        # _write_flow does flow_dir.mkdir(parents=True) with no exist_ok, then
        # writes intent.md/steps.spec.ts/meta.json in sequence — a failure
        # partway through (e.g. disk full on steps.spec.ts) leaves flow_dir
        # existing-but-incomplete. Left alone, EVERY retry (save again, or
        # even a fresh /qa-teach-start for this name after cancelling) would
        # hit "already exists" forever. Mirrors qa_teach.main()'s own
        # `except: shutil.rmtree(flow_dir, ignore_errors=True)`.
        shutil.rmtree(flow_dir, ignore_errors=True)
        with _TEACH_LOCK:
            if _teach_session is sess:
                sess["state"] = "recorded"   # let the operator retry the save
        return {"ok": False, "msg": f"failed writing the flow ({e})"}

    notes = []
    if self_requires:
        notes.append("a flow can't require itself — remove it from 'requires', "
                      "or this flow's chain will fail every time it runs")
    if missing_prereqs:
        notes.append(f"prerequisite(s) not taught yet: {', '.join(missing_prereqs)} — "
                      f"teach them before running this flow's chain")
    prereq_note = f" (note: {'; '.join(notes)})" if notes else ""

    try:
        qa_teach._update_flowmap(flowmap, name, intent, url, "never run")
    except OSError as e:
        # Mirrors qa_teach.main(): a FLOWMAP write failure never masks an
        # already-written flow — the flow itself is done; just say so.
        shutil.rmtree(spec_dir, ignore_errors=True)
        with _TEACH_LOCK:
            if _teach_session is sess:
                _teach_session = None
        return {"ok": True, "msg": f"flow '{name}' saved; FLOWMAP update failed ({e}){prereq_note}",
                "flow": name}

    shutil.rmtree(spec_dir, ignore_errors=True)
    with _TEACH_LOCK:
        if _teach_session is sess:
            _teach_session = None
    return {"ok": True, "msg": f"flow '{name}' saved{prereq_note}", "flow": name}


def do_qa_teach_cancel() -> dict:
    """POST /qa-teach-cancel — kill the recorder if it's still alive, tear
    qa.services down (unless _qa_teach_bring_up already claimed that — see
    _teach_claim_teardown; harmless/idempotent either way, but the claim
    keeps it to exactly one call), remove the scratch spec_dir, and clear
    the session — for whatever state it was in (`starting`/`recording`/
    `recorded`/`failed`), EXCEPT `saving`: a save in progress owns spec_dir
    (mid-copy into flow_dir) and qa.services' fate, so cancel refuses rather
    than rmtree-ing spec_dir or re-downing handles out from under it (that
    race is what used to turn a concurrent save into a FileNotFoundError,
    trip the "orphaned flow_dir" bug, and double-down the same handles).
    ATTACH MODE: if this session attached to the persistent "Dev session"
    (sess["attached"]), qa.services are never downed here — they were never
    THIS session's to tear down (handles stays `[]` in that mode anyway, so
    down() would be a no-op either way, but the flag makes the intent
    explicit rather than relying on that)."""
    global _teach_session
    with _TEACH_LOCK:
        sess = _teach_session
        if sess is None:
            return {"ok": False, "msg": "no teach session is active"}
        if sess["state"] == "saving":
            return {"ok": False, "msg": "a save is in progress — try again in a moment"}
        _teach_session = None
        proc, handles, spec_dir = sess["proc"], sess["handles"], sess["spec_dir"]
        attached = sess.get("attached", False)
    _teach_kill_proc(proc)
    if not attached and _teach_claim_teardown(sess):
        qa_env.down(handles)
    shutil.rmtree(spec_dir, ignore_errors=True)
    return {"ok": True, "msg": "teach session cancelled"}


def _teach_atexit_cleanup() -> None:
    """Best-effort orphan guard for the dashboard-restart-mid-session case:
    command_center.py had no existing shutdown hook to piggyback on (grepped
    for atexit/signal handlers — none), so this registers the first one.
    Covers a clean process exit (Ctrl-C, /restart-dashboard) only — it
    can't run after a SIGKILL/crash, which is why qa_env's own services and
    the codegen child are each spawned with their own process group/lifetime
    rather than relying solely on this to ever run. Kills the recorder,
    tears qa.services down (via the same claim-guard everything else uses,
    in case the bring-up thread is mid-teardown itself right now — and
    skipped entirely when the session attached to the persistent "Dev
    session", same as do_qa_teach_cancel), and removes the scratch spec_dir
    — this last part used to be missing here (unlike cancel), leaking a
    tmpdir on every restart mid-session."""
    sess = _teach_session
    if sess is None:
        return
    _teach_kill_proc(sess.get("proc"))
    if not sess.get("attached") and _teach_claim_teardown(sess):
        qa_env.down(sess.get("handles") or [])
    spec_dir = sess.get("spec_dir")
    if spec_dir:
        shutil.rmtree(spec_dir, ignore_errors=True)


atexit.register(_teach_atexit_cleanup)


# ── onboarding: intake + knowledge-base build/refresh ─────────────────────────
# Same detached-Popen discipline as do_epic_action/do_qa_action: spawn, log to
# a FIXED name under $AP_STATE/logs/ (not timestamped — the Run view polls that
# one name via /api/cmdlog), return immediately with a started-msg. The log is
# opened "wb" (truncate) rather than "ab" (append) deliberately: unlike the QA
# actions above (one log per flow/regression run, never reused), intake/
# knowledge share ONE fixed filename across every re-run, so appending would
# silently mix an old run's output into the next one's tail.
_CMDLOG_ALLOW = {"intake", "knowledge"}
_KNOWLEDGE_SUBS = {"build", "refresh"}


def do_intake_run() -> str:
    """Spawn `intake.py <REPO>` detached — the zero-day survey that proposes
    backlog tasks (always status:proposed/autopilot:human; a human triages
    them on Deep space). Safe to re-run any time."""
    log = LOGDIR / "intake.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as lf:
        subprocess.Popen([sys.executable, str(ENGINE / "intake.py"), str(REPO)],
                         stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True)
    return "Intake started — surveying the repo and proposing backlog tasks. Watch the log below."


def do_knowledge_run(sub: str) -> str:
    """Spawn `knowledge.py <REPO> build|refresh` detached — builds/refreshes
    $AP_HOME/knowledge/, the architecture map the agent reads each cycle."""
    sub = (sub or "build").strip() or "build"
    if sub not in _KNOWLEDGE_SUBS:
        return f"Invalid knowledge action '{sub}' — use build or refresh."
    log = LOGDIR / "knowledge.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as lf:
        subprocess.Popen([sys.executable, str(ENGINE / "knowledge.py"), str(REPO), sub],
                         stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True)
    verb = "Building" if sub == "build" else "Refreshing"
    return f"{verb} the knowledge base — this can take a few minutes. Watch the log below."


def read_cmdlog(name: str, max_lines: int = 400) -> str:
    """Tail of $AP_STATE/logs/<name>.log for the Run view's live poll. `name`
    must be pre-validated by the caller (do_GET checks membership in
    _CMDLOG_ALLOW BEFORE calling this, same confinement discipline as
    read_qa_shot/read_qa_report) — double-checked here too. Never raises and
    never 500s: a log that hasn't been written yet (action never run) yields
    an empty string, not an error — the Run view treats that as 'idle'."""
    if name not in _CMDLOG_ALLOW:
        return ""
    p = LOGDIR / f"{name}.log"
    if not p.is_file():
        return ""
    try:
        return "\n".join(p.read_text(errors="ignore").splitlines()[-max_lines:])
    except OSError:
        return ""


def cycletail(max_lines: int = 120) -> dict:
    """Human-readable live tail of the newest cycle log. stream-json logs are
    parsed into narrator lines (agent text, tool calls, final cost); legacy
    plain-text logs pass through as-is."""
    # Newest by the stamp in the filename, not by the filename itself: a
    # cycle-<tid>-<ts>.log sorts by its task id otherwise (see _cycle_log_stamp).
    logs = sorted(LOGDIR.glob("cycle-*.log"), key=lambda p: _cycle_log_stamp(p.name)) \
        if LOGDIR.exists() else []
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


# ── PRD pipeline (Deep space band) ─────────────────────────────────────────
# research/<run>/prd-run.json is prd_flow-owned; this dashboard layer only
# reads it (prd_pipeline_state), mirrors its status-machine legality
# dashboard-side (_prd_next_actions, so illegal buttons never render), and
# drives it the same two ways do_epic_action drives epic_plan: synchronous
# calls for pure human acts (new/approve-*/revise), a detached CLI Popen for
# the agent stages (run-intake/run-chain).
_PRD_RUN_RE = prd_flow.RUN_RE   # the controller owns the run-id shape; no second copy
_PRD_ARTIFACT_NAMES = {"01-intake.json", "02-research.json", "03-synthesis.json",
                       "04-prd.md", "05-review.json", prd_flow.MARKER_NAME}
# Legal next_actions per marker status. "revise-intake" (full reset) is ALSO
# legal from every status except "intake" itself (already there) - appended
# below, not listed per-entry. A "done" run keeps it explicitly: signing off is
# not the end of a PRD's life, and reopening one was otherwise a dead end that
# forced the PM to hand-edit the marker.
_PRD_STATUS_ACTIONS = {
    "intake": ["run-intake"],
    "awaiting-scope-approval": ["approve-scope", "revise-intake"],
    "research": ["run-chain"],
    "synthesis": ["run-chain"],
    "draft": ["run-chain"],
    "review": ["run-chain"],
    "awaiting-signoff": ["approve-prd", "revise-draft", "revise-research", "revise-synthesis"],
    "done": ["revise-intake"],
}


def _prd_next_actions(status: str) -> list:
    actions = list(_PRD_STATUS_ACTIONS.get(status, []))
    if status != "intake" and "revise-intake" not in actions:
        actions.append("revise-intake")
    return actions


def _prd_research_root() -> Path:
    return REPO / "research"


def prd_pipeline_state() -> list | None:
    """Deep space's PRD band data: one entry per research/*/prd-run.json.
    Present only when the target repo actually has a research/ dir (absent-
    unless-configured discipline, same as qa_flow_library/qa_services_state -
    a target with no PRD story yet sees a byte-identical /api/state)."""
    root = _prd_research_root()
    if not root.is_dir():
        return None
    runs = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (d / prd_flow.MARKER_NAME).exists():
            continue
        try:
            marker = prd_flow.load_marker(str(REPO), d.name)
            if not isinstance(marker, dict):
                continue  # valid JSON but not an object (null/[]/a string) - not a marker
            tamper = prd_flow.verify(str(REPO), d.name)
        except (OSError, ValueError, AttributeError, TypeError, KeyError):
            continue  # unreadable/malformed marker - skip rather than 500 the whole board
        status = marker.get("status", "")
        stamps = marker.get("stages", {})
        stages = [{"name": name,
                  "ok": (stamps[name].get("evals", {}).get("ok") if name in stamps else None),
                  "stamped_at": (stamps[name].get("stamped_at") if name in stamps else None)}
                 for name in prd_flow.STAGES]
        runs.append({"run_id": marker.get("run_id", d.name), "title": marker.get("title", ""),
                    "status": status, "stages": stages, "tamper": tamper,
                    "pii_touch": bool(marker.get("pii_touch")),
                    "next_actions": _prd_next_actions(status)})
    return runs


def do_prd_action(run: str, action: str, title: str = "", brief: str = "",
                  ack_pii: bool = False, pm: str = "") -> str:
    """Drive the PRD pipeline from the dashboard. `new` validates the run id
    itself (no marker exists yet to check legality against); every other
    action is checked against the CURRENT marker's status via
    _prd_next_actions before anything mutates - an illegal action is a no-op
    error, never a partial mutation."""
    if action == "new":
        if not _PRD_RUN_RE.match(run):
            return f"invalid run id '{run}' - lowercase letters, digits, hyphens, must start alphanumeric"
        if not title.strip():
            return "a title is required"
        if (_prd_research_root() / run / prd_flow.MARKER_NAME).exists():
            return f"a PRD run named '{run}' already exists"
        try:
            # pm is optional on the wire: left out, prd_flow resolves it from
            # the workspace's git identity. That default is better UX than a
            # fourth prompt on the New PRD form, so the form doesn't ask.
            prd_flow.new(str(REPO), run, title.strip(), brief=brief, pm=pm.strip())
        except (OSError, prd_flow.GateError) as e:
            return f"could not create PRD run '{run}': {e}"
        return f"Created PRD run '{run}' - intake is next."

    try:
        marker = prd_flow.load_marker(str(REPO), run)
    except (OSError, ValueError):
        return f"no PRD run named '{run}'"
    status = marker.get("status", "")
    if action not in _prd_next_actions(status):
        return f"cannot '{action}' from status '{status}'"

    if action == "approve-scope":
        try:
            prd_flow.approve_scope(str(REPO), run, ack_pii=ack_pii)
        except prd_flow.GateError as e:
            return f"cannot approve scope: {e}"
        return f"Scope approved for '{run}' - research is next."
    if action == "approve-prd":
        prd_flow.approve_prd(str(REPO), run)
        return f"'{run}' signed off - done."
    if action.startswith("revise-"):
        stage = action[len("revise-"):]
        try:
            prd_flow.revise(str(REPO), run, stage)
        except prd_flow.GateError as e:
            return f"cannot revise: {e}"
        return f"Revising '{run}' from '{stage}' - that stamp and every later one dropped."
    if action in ("run-intake", "run-chain"):
        stage = "intake" if action == "run-intake" else status
        script = str(ENGINE / "prd_flow.py")
        cmd = [sys.executable, script, "run", str(REPO), run, stage]
        if action == "run-chain":
            cmd.append("--chain")
        log = AP_STATE / "logs" / f"prd-{run}-{stage}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as lf:
            subprocess.Popen(cmd, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        return f"Stage '{stage}' launched for '{run}' - the card updates as it works (log: logs/{log.name})"
    return f"unknown prd action '{action}'"


def read_prd_artifact(run: str, file: str) -> str | None:
    """Confined GET for the artifact viewer - `run` and `file` are both
    client input. Same template as _qa_resolve/read_knowledge_doc: shape-
    check both, then resolve → assert the RESOLVED path stays under the
    RESOLVED research/ root → is_file() (catches a symlink escape)."""
    if not _PRD_RUN_RE.match(run) or file not in _PRD_ARTIFACT_NAMES:
        return None
    root = _prd_research_root().resolve()
    resolved = (_prd_research_root() / run / file).resolve()
    if not str(resolved).startswith(str(root) + os.sep) or not resolved.is_file():
        return None
    return resolved.read_text(errors="ignore")


# ── Watches + surveys (Deep space bands) + product artifact browser ────────
# Watches is config-schema-first: config.py validates the `watches:` block,
# and adapters/metric_watch_to_backlog.py is the ONLY writer of both
# $AP_STATE/watches.json and watch-<name>-<YYYYMMDD> backlog tasks - this
# dashboard layer only reads all three, the same read-only relationship
# prd_pipeline_state has with prd_flow. Surveys and the insight-doc listing
# are filesystem-first: surveys/<name>/ and insights/*.md are written by the
# orbit-survey-design/-analyze and orbit-analyze-funnel/-hypothesis skills.
# All three helpers share the absent-unless-configured, corrupt-tolerant
# discipline prd_pipeline_state set: a target with none of this configured
# sees a byte-identical /api/state.
_WATCH_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")   # config.py's own watch-name shape
# Governs both what the artifact browser LISTS (insight_docs_state,
# surveys_state's directory names) and what read_product_artifact will SERVE
# - same "the allowlist is a live listing, not just a shape check" discipline
# as _knowledge_docs()/read_knowledge_doc(). Deliberately wider than
# _WATCH_NAME_RE (mixed case, dots) since these names come from human/agent
# free text, not a validated config slug; the leading-alnum + no-"/" shape
# still rejects a `..`/traversal name outright (`..` and `../x` both start
# with "." and are never a full match).
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _insights_root() -> Path:
    return REPO / "insights"


def _surveys_root() -> Path:
    return REPO / "surveys"


def watches_state(backlog: list) -> list | None:
    """Deep space's Watches band: one entry per config `watches:` entry,
    joined with $AP_STATE/watches.json's last_filed and the newest
    watch-<name>-* backlog task (if the adapter has ever filed one for that
    watch - `backlog` is build_state's already-loaded list, not reread here).

    None (key omitted from build_state) when the profile has no watches:
    block at all - a target that never configured watches sees a
    byte-identical /api/state. A block that IS there but empty returns [],
    which is a different fact and worth showing: the band can say "No
    watches configured." instead of vanishing, which is what an operator who
    just added the block and is looking for it needs to see. (`watches:` with
    nothing after it parses as None, indistinguishable from absent by value -
    hence the key test.) A malformed block (not a list) is omitted, same
    corrupt-tolerant discipline as the state file below."""
    cfg = _load_cfg()
    if "watches" not in cfg:
        return None
    watches = cfg.get("watches") or []
    if not isinstance(watches, list):
        return None
    try:
        raw = json.loads((AP_STATE / "watches.json").read_text())
        last_filed = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        last_filed = {}   # missing/corrupt state - every watch just reads as never-filed

    out = []
    for w in watches:
        if not isinstance(w, dict):
            continue
        name = w.get("name")
        if not isinstance(name, str) or not _WATCH_NAME_RE.match(name):
            continue
        entry = {"name": name, "question": w.get("question", ""),
                 "cadence_hours": w.get("cadence_hours") or 168,
                 "category": w.get("category") or "insight",
                 "last_filed": last_filed.get(name)}
        # Latest (by id - the YYYYMMDD suffix sorts chronologically as a
        # string) watch-<name>-* task on the backlog, if the adapter has ever
        # filed one - the exact id shape build_proposal() writes.
        task_re = re.compile(rf"^watch-{re.escape(name)}-\d{{8}}$")
        latest = None
        for t in backlog:
            tid = str(t.get("id", ""))
            if task_re.match(tid) and (latest is None or tid > latest["id"]):
                latest = {"id": tid, "status": t.get("status", "")}
        if latest:
            entry["task_id"] = latest["id"]
            entry["task_status"] = latest["status"]
        out.append(entry)
    return out


def surveys_state() -> list | None:
    """Deep space's Surveys band: one entry per surveys/<name>/ subdirectory.
    None (key omitted from build_state) unless <repo>/surveys/ exists.
    Directory names are filtered to _ARTIFACT_NAME_RE - the same "list only
    what you'd also serve" discipline read_knowledge_doc uses - so a listed
    name is always one /product-artifact?kind=survey can actually open."""
    root = _surveys_root()
    if not root.is_dir():
        return None
    try:
        names = sorted(p.name for p in root.iterdir() if p.is_dir() and _ARTIFACT_NAME_RE.match(p.name))
    except OSError:
        return []
    out = []
    for name in names:
        d = root / name
        raw_dir = d / "raw"
        try:
            raw_count = sum(1 for f in raw_dir.iterdir() if f.is_file()) if raw_dir.is_dir() else 0
        except OSError:
            raw_count = 0
        out.append({"name": name,
                    "has_questionnaire": (d / "questionnaire.md").is_file(),
                    "raw_count": raw_count,
                    "has_report": (d / "report.md").is_file()})
    return out


def insight_docs_state() -> list | None:
    """Deep space artifact browser's Insights group: stems of insights/*.md,
    filtered to _ARTIFACT_NAME_RE for the same list-what-you-serve reason
    surveys_state filters directory names. None (key omitted from
    build_state) unless <repo>/insights/ exists."""
    root = _insights_root()
    if not root.is_dir():
        return None
    try:
        return sorted(p.stem for p in root.glob("*.md") if _ARTIFACT_NAME_RE.match(p.stem))
    except OSError:
        return []


# kind -> (root dir, relative-path template) for read_product_artifact. The
# two families /prd-artifact doesn't already cover.
_ARTIFACT_KINDS = {"insight": (_insights_root, "{}.md"),
                   "survey": (_surveys_root, "{}/report.md")}


def read_product_artifact(kind: str, name: str) -> str | None:
    """Confined GET for the product artifact browser - insights/<name>.md or
    surveys/<name>/report.md. Same template as read_prd_artifact/
    read_knowledge_doc: shape-check `kind` (fixed allowlist) and `name`
    (_ARTIFACT_NAME_RE), then resolve -> assert the RESOLVED path stays under
    the RESOLVED kind-scoped root -> is_file() (catches a symlink escape)."""
    spec = _ARTIFACT_KINDS.get(kind)
    if spec is None or not isinstance(name, str) or not _ARTIFACT_NAME_RE.match(name):
        return None
    root_fn, rel_tmpl = spec
    root = root_fn()
    resolved_root = root.resolve()
    resolved = (root / rel_tmpl.format(name)).resolve()
    if not str(resolved).startswith(str(resolved_root) + os.sep) or not resolved.is_file():
        return None
    return resolved.read_text(errors="ignore")


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
                  "release_management", "git_practices",
                  "insight", "funnel", "hypothesis", "survey_design", "survey_analysis", "design_review"]
SOURCES_ALL = ["backlog", "backlog-research", "maturity-score", "logwatch", "ui-test",
               "code-review", "security-review", "network-review", "metric_watch"]
HOSTS_ALL = ["bitbucket", "github", "gitlab"]
_PREFIX_RE = re.compile(r"^[A-Za-z0-9._/-]{1,60}$")


def _dashboard_label() -> str:
    """This dashboard's launchd label, derived from the loop label."""
    if LAUNCHD_LABEL.startswith("com.orbit.orbit-"):
        return "com.orbit.dashboard-" + LAUNCHD_LABEL[len("com.orbit.orbit-"):]
    return "com.orbit.dashboard-" + REPO.name


def _checkbox_options(known: list, current) -> list:
    """The known options PLUS whatever this config already holds.

    The Admin panel renders ONE checkbox per served option and Save sends back
    exactly the checked ones, so an option the server omits from the meta is an
    option the next Save silently deletes from config.yaml. Serving the union
    makes that structurally impossible: a value already on disk - a category
    this build predates, one a hand edit added, one a newer profile writes -
    always renders (checked, since it is in the config) and always survives the
    round trip. Known options lead in their declared order; extras follow
    sorted, so the served list is deterministic."""
    if not isinstance(current, list):
        return list(known)
    extra = sorted({v for v in current if isinstance(v, str) and v not in known})
    return list(known) + extra


def config_payload() -> dict:
    """Full config.yaml + resolved runtime meta for the Admin panel."""
    reload_settings()
    src, pr = _git_urls(GIT_HOST, GIT_REPO, BASE_BRANCH)
    cfg = _load_cfg()
    return {
        "config": cfg,
        "meta": {
            "ap_home": str(AP_HOME), "repo": str(REPO), "port": PORT,
            "loop_label": LAUNCHD_LABEL, "dashboard_label": _dashboard_label(),
            "base_branch": BASE_BRANCH, "git_host": GIT_HOST, "git_repo": GIT_REPO,
            "git_src_url": src, "git_pr_url": pr,
            "categories_all": _checkbox_options(CATEGORIES_ALL, cfg.get("workable_categories")),
            "sources_all": _checkbox_options(SOURCES_ALL, cfg.get("sources")),
            "hosts_all": HOSTS_ALL,
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
    def _bounded_int(k, lo, hi):
        if k in d and (not isinstance(d[k], int) or isinstance(d[k], bool)
                       or d[k] < lo or d[k] > hi):
            errs.append(f"{k} must be an integer between {lo} and {hi}")
    _posint("interval_seconds"); _posint("max_tasks_per_day"); _posint("cycle_timeout_seconds")
    # ONE concurrency contract (see engine/config.py's CONCURRENCY_MAX): the
    # Admin panel must refuse exactly what `config.py validate` refuses, or a
    # value saved here would fail the loop's own preflight instead.
    _bounded_int("concurrency", 1, CONCURRENCY_MAX)
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
    if "mcp" in d and d["mcp"] is not None and not isinstance(d["mcp"], dict):
        errs.append("mcp must be an object of server-name -> {transport, url/command, tools}")
    if "watches" in d and d["watches"] is not None and not isinstance(d["watches"], list):
        errs.append("watches must be a list of {name, question, cadence_hours, category}")
    return errs


def _atomic_write_config_text(text: str) -> None:
    """Write `text` to config.yaml via the same temp+rename pattern — the
    ONLY way bytes ever land in config.yaml, whether that's a fresh save
    (_write_config) or a rollback restore (do_save_config). Sharing this
    means the restore-after-a-bad-validate path gets the same atomicity
    guarantee as a normal save: a crash mid-write can never leave config.yaml
    truncated, before OR after a rollback."""
    tmp = AP_HOME / "config.yaml.tmp"
    tmp.write_text(text)
    tmp.replace(AP_HOME / "config.yaml")


def _write_config(cfg: dict) -> None:
    """Atomically write config.yaml with a generated header (the Admin panel owns it)."""
    header = ("# Orbit config for this target repo — the single source of truth for\n"
              "# both the loop and the dashboard. Edited via the dashboard Admin panel.\n\n")
    _atomic_write_config_text(header + yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False, allow_unicode=True))


def _config_py_validate(target: Path) -> tuple[bool, list[str]]:
    """Run config.py's OWN `validate` subcommand against `target` — the same
    authoritative schema check the loop's startup and `orbit intake` enforce
    (gates, qa, plan, knowledge, pull_requests, spec, repo). Subprocess, not
    an import: validate() calls sys.exit() on failure (a CLI contract, not a
    library one) and reads config.yaml straight off disk — shelling out gets
    both right for free instead of teaching this module a second copy of the
    schema (which is exactly what config.py's docstring says it's for)."""
    try:
        r = subprocess.run([sys.executable, str(ENGINE / "config.py"), "validate", str(target)],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        return False, [f"could not run config.py validate: {e}"]
    if r.returncode == 0:
        return True, []
    errs = [ln.strip().lstrip("✗").strip() for ln in (r.stdout or "").splitlines()
            if ln.strip().startswith("✗")]
    if not errs:
        errs = [(r.stdout or r.stderr or "config validate failed").strip()]
    return False, errs


def do_save_config(raw: str) -> str:
    """Validate an incoming JSON patch, merge it into config.yaml, and persist.
    Keys not present in the patch are preserved (gates/env/etc. survive).

    Two validation passes: _validate_config's cheap shape checks run first
    (no disk I/O — they cover fields config.py's validate() doesn't:
    permission_mode, interval/max-tasks/timeout, git_host, sources,
    workable_categories). The merged config is then WRITTEN and re-checked
    with config.py's own validate() — the full-schema authority for gates/
    qa/plan/knowledge/mcp/pull_requests/spec/repo. A validate() failure rolls
    the write back (restoring the prior bytes via the same atomic temp+rename
    _atomic_write_config_text uses, or removing the file if it didn't exist
    before) so a bad qa/plan/knowledge/mcp block is rolled back within seconds -
    it is genuinely on disk for the brief window the validate() subprocess
    runs, but never stays there, and the restore itself can never leave a
    half-written file even if the process dies mid-rollback."""
    try:
        incoming = json.loads(raw or "{}")
    except Exception as e:
        return f"Invalid JSON: {e}"
    if not isinstance(incoming, dict):
        return "Config payload must be an object."
    errs = _validate_config(incoming)
    if errs:
        return "Rejected — " + "; ".join(errs)
    cfg_path = AP_HOME / "config.yaml"
    existed = cfg_path.exists()
    backup = cfg_path.read_text() if existed else None
    cfg = _load_cfg()
    cfg.update(incoming)   # merge: preserves keys the UI did not send
    _write_config(cfg)
    ok, cfg_errs = _config_py_validate(REPO)
    if not ok:
        if existed:
            _atomic_write_config_text(backup)
        else:
            cfg_path.unlink(missing_ok=True)
        return "Rejected — " + "; ".join(cfg_errs)
    reload_settings()
    return "✓ Saved config.yaml. Use 'Restart loop' to apply it to the running loop."


# ── secrets (.autopilot/.secrets.env) ─────────────────────────────────────
# MCP tokens the loop needs at runtime (e.g. METAQL_TOKEN) but that must never
# round-trip through config.yaml (committed) or the dashboard's state poll.
# File format is a contract shared with two other readers: engine/doctor.py's
# _secrets_env (bare KEY=VALUE, no quotes, no export) and run.sh's own line
# parser (which reads, never sources/executes, this file) — so the writer
# below must emit exactly that.
_SECRETS_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# run.sh's loader PARSES this file line-by-line rather than sourcing it (a
# deliberate fix — the file used to be `. "$file"`'d, which would execute any
# shell metacharacters in a value with the loop's privileges). This allowlist
# is defense in depth on top of that: even a hand-edited file, or a future
# consumer that DOES source it, can never contain a value that does anything
# but sit there as an inert string. Covers tokens, API keys, base64, JWTs
# (dots/dashes/underscores), URLs, and file paths — nothing that a shell would
# treat specially ($, `, ;, |, &, quotes, whitespace, newlines are all out).
_SECRETS_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-./:+=@,~]+$")
# A metacharacter-free VALUE isn't enough: a crafted NAME can be just as
# dangerous with no metacharacters at all. run.sh's parse loop `export`s
# whatever name/value pair it reads, so a saved PATH=/tmp/evil silently
# poisons every bare `python3`/`git`/`gh` call the loop makes on its next
# restart — that's RCE via name alone, no shell metacharacters required.
# Block the interpreter/shell variables an attacker would reach for, plus
# this process's own AP_HOME/AP_STATE/ORBIT_HOME (overriding those would let
# a "secret" redirect the loop at a different config/state tree entirely).
# PYTHONPATH is not the only loader hook: node, perl, ruby and java each read
# their own, and a cycle or stage child reaches at least node (mcp stdio
# servers run via npx). Keep this list and run.sh's `case` guard identical.
_SECRETS_NAME_DENY = {"PATH", "IFS", "ENV", "BASH_ENV", "SHELL", "HOME", "PS4", "CDPATH",
                      "PYTHONPATH", "PYTHONSTARTUP", "AP_HOME", "AP_STATE", "ORBIT_HOME",
                      "NODE_OPTIONS", "NODE_PATH", "PERL5LIB", "RUBYOPT", "GEM_PATH",
                      "CLASSPATH"}
_SECRETS_PREFIX_DENY = ("LD_", "DYLD_", "BASH_")
_SECRETS_LOCK = threading.Lock()  # ThreadingHTTPServer: guards concurrent writers


def _secrets_path() -> Path:
    return AP_HOME / ".secrets.env"


def _read_secrets_env() -> dict:
    """Parse .autopilot/.secrets.env as KEY=VALUE lines. Thin wrapper over the
    shared parser (engine/secrets_env.py) - one definition of the format for
    this panel, doctor.py, run.sh's loop and prd_flow's stage children."""
    return secrets_env.read_path(str(_secrets_path()))


def _ensure_secrets_gitignored() -> None:
    """Make sure .autopilot/.gitignore lists .secrets.env before we create that
    file. A workspace scaffolded before .secrets.env existed has a .gitignore
    without the entry (init.py used to write it once and never top it up), and
    this panel must not be the thing that drops a token into a tracked path.
    Appends only what is missing; never rewrites what is already there."""
    gi = AP_HOME / ".gitignore"
    entry = ".secrets.env"
    try:
        body = gi.read_text()
    except OSError:
        gi.parent.mkdir(parents=True, exist_ok=True)
        gi.write_text(f"# Orbit secrets - never commit\n{entry}\n")
        return
    if entry in {ln.strip() for ln in body.splitlines()}:
        return
    sep = "" if (not body or body.endswith("\n")) else "\n"
    with gi.open("a") as f:
        f.write(f"{sep}{entry}\n")


def _atomic_write_secrets(secrets_map: dict) -> None:
    """Write secrets_map as bare KEY=VALUE lines to .secrets.env, atomically and
    at mode 0600 throughout (the temp file too - a crash between mkstemp and
    os.replace must never leave a world-readable temp copy of these values
    lying next to the real file). Matches prd_flow._atomic_write_json's
    mkstemp + unlink-on-failure pattern."""
    path = _secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_secrets_gitignored()
    body = "".join(f"{k}={v}\n" for k, v in sorted(secrets_map.items()))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".secrets-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)  # belt-and-suspenders against a umask widening mkstemp's mode
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.replace(tmp, str(path))  # same-dir rename - atomic, no partial-file window
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def secrets_payload() -> dict:
    """GET /api/secrets - name + length-only preview. Full values are NEVER
    returned once saved (that's the whole point of a secrets store), and
    neither are any of its characters - a `first2...last2` preview still
    leaks real value bytes, so this masks fully and reports only the length."""
    out = []
    for name, value in sorted(_read_secrets_env().items()):
        preview = f"•••• ({len(value)} chars)"
        out.append({"name": name, "preview": preview})
    return {"secrets": out}


def do_save_secrets(payload: dict) -> str:
    """POST /api/secrets {"set": {NAME: value, ...}, "delete": [NAME, ...]}.
    Validates everything BEFORE writing anything (a rejected payload leaves
    the file untouched, never partially applied). Values are never logged —
    only names appear in returned/raised messages."""
    if not isinstance(payload, dict):
        return "Rejected — payload must be an object"
    to_set = payload.get("set") or {}
    to_delete = payload.get("delete") or []
    if not isinstance(to_set, dict):
        return "Rejected — 'set' must be an object of NAME -> value"
    if not isinstance(to_delete, list):
        return "Rejected — 'delete' must be a list of names"
    names = list(to_set.keys()) + [str(n) for n in to_delete]
    bad = sorted({str(n) for n in names if not _SECRETS_NAME_RE.match(str(n))})
    if bad:
        return "Rejected — invalid secret name(s): " + ", ".join(bad) + " (must match ^[A-Z][A-Z0-9_]*$)"
    reserved = sorted({n for n in names if n in _SECRETS_NAME_DENY or n.startswith(_SECRETS_PREFIX_DENY)})
    if reserved:
        return "Rejected — reserved variable name: " + ", ".join(reserved)
    for name, value in to_set.items():
        if not isinstance(value, str) or not _SECRETS_VALUE_RE.match(value):
            # Never echo the value back — name only. This also rejects
            # multi-line values (newlines aren't in the allowlist) and empty
            # strings (the regex requires at least one char), so those don't
            # need separate checks.
            return f"Rejected — value for {name} must be non-empty and contain only letters, digits, and _-./:+=@,~ (no shell metacharacters)"
    with _SECRETS_LOCK:
        current = _read_secrets_env()
        removed = 0
        for name in to_delete:
            if current.pop(str(name), None) is not None:
                removed += 1
        for name, value in to_set.items():
            current[name] = value
        _atomic_write_secrets(current)
    return f"Saved {len(to_set)} secret(s), removed {removed}"


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


def _qa_evidence(e: dict) -> dict:
    # attachments/qa surfaced verbatim from a ledger entry (Task 15's attach /
    # qa-verdict verbs) — absent on the entry means the key is ABSENT on the
    # row (not null), so existing consumers that don't know these keys yet
    # see no change.
    extra = {}
    if e.get("attachments"):
        extra["attachments"] = e["attachments"]
    if e.get("qa"):
        extra["qa"] = e["qa"]
    return extra


# ── onboarding status (Run/Knowledge views) ────────────────────────────────
# Terminal markers are each script's OWN final-line text (intake.py's very
# last print; knowledge.py's success/failure lines) — deliberately NOT
# anything printed mid-run (e.g. intake's per-gate "✓ gate <name>: PASS"
# lines, which appear well before the run finishes and would read as done
# while the survey agent is still working).
_INTAKE_DONE_MARKERS = ("next: triage the proposals",)
_KNOWLEDGE_DONE_MARKERS = ("knowledge build complete", "knowledge refresh complete",
                          "VALIDATE:", "survey agent failed", "no knowledge base (run:",
                          "knowledge is fresh")
_CMDLOG_STALE_SECONDS = 1800   # 30 min — beyond this, treat a markerless log as dead, not running


def _cmdlog_running(name: str, markers: tuple) -> bool:
    """Best-effort 'is this onboarding action still going': the log exists,
    was touched recently, and its tail doesn't yet show one of the action's
    own terminal markers. Not a PID check — the spawned process is detached,
    so the log is the only signal a (possibly-restarted) dashboard process
    can see. Markerless-and-stale (>30min untouched) falls back to 'not
    running' so a crash that skipped every marker doesn't stay stuck forever."""
    p = LOGDIR / f"{name}.log"
    if not p.is_file():
        return False
    try:
        age = time.time() - p.stat().st_mtime
        tail = p.read_text(errors="ignore")[-4000:]
    except OSError:
        return False
    if any(m in tail for m in markers):
        return False
    return age <= _CMDLOG_STALE_SECONDS


def onboarding_state() -> dict:
    """Best-effort onboarding status for the Run/Knowledge views. Every value
    defaults to a falsy/empty shape — a target with no onboarding story yet
    (no knowledge/, no gates, nothing ever run) still gets a well-formed
    dict, never an error; the UI reads absence as 'not done yet'."""
    cfg = _load_cfg()
    qa_cfg, plan_cfg = cfg.get("qa"), cfg.get("plan")
    return {
        "has_knowledge": (AP_HOME / "knowledge" / "INDEX.md").is_file(),
        "knowledge_docs": _knowledge_docs(),
        "intake_running": _cmdlog_running("intake", _INTAKE_DONE_MARKERS),
        "knowledge_running": _cmdlog_running("knowledge", _KNOWLEDGE_DONE_MARKERS),
        "gates_configured": bool(cfg.get("gates")),
        "qa_configured": isinstance(qa_cfg, dict) and bool(qa_cfg),
        "plan_configured": isinstance(plan_cfg, dict) and bool(plan_cfg),
    }


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

    workers = workers_state()   # None only when unconfigured/empty AND concurrency==1 - see workers_state()
    if WORKERS_DIR.is_dir():
        # T4 always mkdir -p's this dir before its first iteration, so its
        # presence means the pid-file contract is in force - trust it
        # completely, including "empty = nothing live right now" (a
        # concurrency>1 deployment between cycles is genuinely idle, not a
        # reason to fall back to a machine-wide pgrep).
        live = any(w.get("alive") for w in (workers or []))
    else:
        # Legacy fallback ONLY when the workers dir has never existed at all
        # (a pre-T4 state dir, or the dashboard polled before the loop's very
        # first iteration) - cycle_pids() is a machine-wide pgrep and would
        # misattribute liveness to an unrelated orbit-cycle process elsewhere
        # on the box, but it's the only signal available until then.
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
                row.update(_qa_evidence(e))
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
            row.update(_qa_evidence(e))
            done.append(row)
        elif st == "escalated":
            row.update({"state": st, "forced": bool(e.get("forced")),
                        "can_reject": lifecycle.check(st, "rejected", e) is None})
            row.update(_qa_evidence(e))
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

    # `workers` was computed at the top of build_state (for `live`) - hand it
    # over so one poll reads the pid files ONCE, not twice.
    rt = probe_runtime(workers)
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

    # Present only when the target actually has a flow library — absent (not
    # null) so a target with no QA story sees a byte-identical /api/state.
    qa_flows = qa_flow_library()
    if qa_flows is not None:
        rt["qa_flows"] = qa_flows
    # Same discipline: present only when qa.services is configured, so a
    # target with no dev-session story sees a byte-identical /api/state.
    qa_svc = qa_services_state()
    if qa_svc is not None:
        rt["qa_services"] = qa_svc
    # Same discipline again: present only when the target repo has a
    # research/ dir, so a target with no PRD story yet sees a byte-identical
    # /api/state.
    prd = prd_pipeline_state()
    if prd is not None:
        rt["prd"] = prd
    # Same discipline again: watches/surveys/insight_docs are each present
    # only when their own story is configured, so a target with none of
    # M3's product workflows yet sees a byte-identical /api/state.
    watches = watches_state(backlog)
    if watches is not None:
        rt["watches"] = watches
    surveys = surveys_state()
    if surveys is not None:
        rt["surveys"] = surveys
    insight_docs = insight_docs_state()
    if insight_docs is not None:
        rt["insight_docs"] = insight_docs
    # Same discipline again: `workers` (computed above, for `live`) is present
    # only when workers_state() has something worth a strip - see its
    # docstring for the exact None-vs-[] contract.
    if workers is not None:
        rt["workers"] = workers
    rt["onboarding"] = onboarding_state()
    return rt


# ── HTML shell (static; the SPA fills it from /api/state) ─────────────────────


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str, code=200, cache: str | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Never let the browser cache the SPA shell (or any other route) by
        # default — otherwise a redeploy of the dashboard JS leaves a stale tab
        # (buttons "missing" until a hard refresh). Routes that explicitly opt
        # in (e.g. /vendor/* — static files not edited live; NOTE the URL is
        # unversioned, not content-addressed, so a future edit to a vendored
        # file needs a cache-busting rename, not just an in-place overwrite)
        # pass their own `cache` value instead.
        self.send_header("Cache-Control", cache or "no-store, must-revalidate")
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
        elif path == "/api/secrets":
            # Read-only like /api/config — host-checked above, token-less, and
            # MUST NEVER return a full value (see secrets_payload's masking).
            self._send(json.dumps(secrets_payload()).encode(), "application/json")
        elif path == "/api/cycletail":
            self._send(json.dumps(cycletail()).encode(), "application/json")
        elif path == "/api/cmdlog":
            # Confined tail for the Run view's live poll — name must be in the
            # fixed allowlist (never a filesystem path from the client).
            name = (qs.get("name", [""])[0]).strip()
            if name not in _CMDLOG_ALLOW:
                self._send(b"invalid name", "text/plain", 400); return
            self._send(read_cmdlog(name).encode(), "text/plain; charset=utf-8")
        elif path == "/api/knowledge-doc":
            # Confined GET for the Knowledge view's doc browser — see
            # read_knowledge_doc (allowlist = a LIVE listing of $AP_HOME/knowledge/,
            # not just a shape check).
            name = (qs.get("name", [""])[0]).strip()
            doc = read_knowledge_doc(name)
            if doc is None:
                self._send(b"not found", "text/plain", 404); return
            self._send(doc.encode(), "text/plain; charset=utf-8")
        elif path.startswith("/vendor/"):
            # Confined GET for vendored client-side libs (e.g. cytoscape.min.js
            # for the Knowledge view's graph mode) — token-less like /review,
            # same posture (loopback-bound, GET is read-only). See read_vendor
            # for the allowlist + confinement; cached since these are static
            # files never edited live (unlike cc_shell.html, which is read
            # per-request precisely so it ISN'T cached). NOTE: the URL is
            # unversioned/not content-addressed (just the bare filename) — a
            # future edit to a vendored file needs a cache-busting rename (or
            # a shorter max-age), not just overwriting the file in place.
            served = read_vendor(path[len("/vendor/"):])
            if served is None:
                self._send(b"not found", "text/plain", 404); return
            body, ctype = served
            self._send(body, ctype, cache="public, max-age=86400"); return
        elif path in ("/review", "/diff"):
            if not tid or not _TID_RE.match(tid):
                self._send(b"invalid id", "text/plain", 400); return
            body = read_packet(tid) if path == "/review" else read_patch(tid)
            if body is None:
                self._send(b"not found for this task", "text/plain", 404); return
            self._send(body.encode(), "text/plain; charset=utf-8")
        elif path in ("/qa-shot", "/qa-report"):
            # Confined GET routes for QA evidence — token-less like /review and
            # /diff (loopback-bound, GET is read-only). `p` is a path relative
            # to $AP_STATE/qa/; see _qa_path_ok/read_qa_shot/read_qa_report.
            qp = (qs.get("p", [""])[0]).strip()
            if not _qa_path_ok(qp):
                self._send(b"invalid path", "text/plain", 400); return
            if path == "/qa-shot":
                shot = read_qa_shot(qp)
                if shot is None:
                    self._send(b"not found", "text/plain", 404); return
                body, ctype = shot
                self._send(body, ctype); return
            report = read_qa_report(qp)
            if report is None:
                self._send(b"not found", "text/plain", 404); return
            self._send(report.encode(), "text/plain; charset=utf-8")
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
        elif path == "/prd-artifact":
            # Confined GET for the PRD artifact viewer - see read_prd_artifact
            # for the run-id/filename allowlist + realpath confinement.
            run = (qs.get("run", [""])[0]).strip()
            file = (qs.get("file", [""])[0]).strip()
            body = read_prd_artifact(run, file)
            if body is None:
                self._send(b"not found", "text/plain", 404); return
            self._send(body.encode(), "text/plain; charset=utf-8")
        elif path == "/product-artifact":
            # Confined GET for the product artifact browser - the two
            # families /prd-artifact doesn't cover (insights/*.md,
            # surveys/*/report.md). See read_product_artifact for the
            # kind-scoped root + name-shape + realpath confinement.
            kind = (qs.get("kind", [""])[0]).strip()
            name = (qs.get("name", [""])[0]).strip()
            body = read_product_artifact(kind, name)
            if body is None:
                self._send(b"not found", "text/plain", 404); return
            self._send(body.encode(), "text/plain; charset=utf-8")
        elif path == "/qa-teach-status":
            # Read-only poll for the "New test / Record" panel — same
            # token-less GET posture as /api/state (host-checked above, no
            # X-CC-Token; it changes nothing).
            self._send(json.dumps(do_qa_teach_status()).encode(), "application/json")
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
                msg = do_killtask(tid)
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
            elif path == "/intake-run":
                msg = do_intake_run()
            elif path == "/knowledge-run":
                msg = do_knowledge_run((data.get("sub", ["build"])[0]).strip())
            elif path == "/qa-action":
                result = do_qa_action({"action": (data.get("action", [""])[0]).strip(),
                                       "flow": (data.get("flow", [""])[0]).strip(),
                                       "file": (data.get("file", [""])[0]).strip(),
                                       "name": (data.get("name", [""])[0]).strip(),
                                       "reason": (data.get("reason", [""])[0]).strip()})
                self._send(json.dumps(result).encode(), "application/json"); return
            elif path in ("/qa-teach-start", "/qa-teach-save", "/qa-teach-cancel"):
                # JSON body (postJSON, not this handler's usual form-encoded
                # `data`) — mirrors do_save_config's raw-body JSON parsing for
                # /api/config, the one other JSON-bodied POST route here.
                try:
                    payload = json.loads(raw) if raw else {}
                except Exception:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                if path == "/qa-teach-start":
                    result = do_qa_teach_start(payload)
                elif path == "/qa-teach-save":
                    result = do_qa_teach_save(payload)
                else:
                    result = do_qa_teach_cancel()
                self._send(json.dumps(result).encode(), "application/json"); return
            elif path in ("/qa-services-start", "/qa-services-stop", "/qa-services-restart"):
                # Same JSON-body posture as the /qa-teach-* trio above —
                # /qa-services-restart's optional `service` field is the
                # only payload any of these three ever take.
                try:
                    payload = json.loads(raw) if raw else {}
                except Exception:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                if path == "/qa-services-start":
                    result = do_qa_services_start()
                elif path == "/qa-services-stop":
                    result = do_qa_services_stop()
                else:
                    result = do_qa_services_restart(payload)
                self._send(json.dumps(result).encode(), "application/json"); return
            elif path == "/epic-action":
                if not tid:
                    self._send(b'{"ok":false,"msg":"missing epic id"}', "application/json", 400); return
                msg = do_epic_action(tid, (data.get("action", [""])[0]).strip())
            elif path == "/prd-action":
                run = (data.get("run", [""])[0]).strip()
                if not run:
                    self._send(b'{"ok":false,"msg":"missing run id"}', "application/json", 400); return
                msg = do_prd_action(run, (data.get("action", [""])[0]).strip(),
                                    (data.get("title", [""])[0]).strip(),
                                    data.get("brief", [""])[0],
                                    ack_pii=(data.get("ack_pii", [""])[0] == "1"),
                                    pm=data.get("pm", [""])[0])
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
            elif path == "/api/secrets":
                # JSON body (postJSON), same posture as /api/config's raw-body
                # JSON parsing — {"set": {...}, "delete": [...]}.
                try:
                    payload = json.loads(raw) if raw else {}
                except Exception:
                    payload = {}
                msg = do_save_secrets(payload)
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
