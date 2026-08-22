# Task 4: the loop wrapper (engine/run.sh) as a SLOT — one of N independent
# workers, each claiming its own task before spending a cycle on it.
#
# What is actually verified here, and why this shape:
#   · config.py grows `concurrency` (shellenv's ORBIT_CONCURRENCY, which is what
#     run.sh defaults ORBIT_SLOTS to) plus its validation.
#   · run.sh's real claim-then-run functions are EXTRACTED from run.sh and run in
#     bash against a fake ledger.py, the same technique tests/test_secrets.py and
#     tests/test_autopromote.py use. A copy of the logic here would silently stop
#     testing the shipped logic the first time the two drift.
#   · the per-slot filename block is extracted and driven the same way, because
#     those names are a compatibility contract with the dashboard: slot 0 MUST
#     keep the legacy unsuffixed names.
#   · the worker pid file (the only liveness signal ledger.py's reaper trusts) is
#     written by the shipped shell function, so its json shape is checked here
#     against what ledger.py reads.
#
# What only the end-to-end smoke can cover: a real `claude` cycle, and therefore
# the ordering of housekeeping-vs-claim across two live slots. The source-level
# guards below pin the structure (housekeeping under `if [ "$SLOT" = "0" ]`, the
# watchdog's fd detachment, /orbit-cycle <tid>) so a refactor cannot quietly
# unwire it between smoke runs.
#
# Run: python3 -m pytest tests/test_slot_loop.py -v   (from orbit/)
import json
import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ORBIT = Path(__file__).resolve().parent.parent
ENGINE = ORBIT / "engine"
RUN_SH = (ENGINE / "run.sh").read_text()


# ── config: concurrency → ORBIT_CONCURRENCY ────────────────────────────────

def _cfg_repo(tmp_path, extra=""):
    ap = tmp_path / ".autopilot"
    ap.mkdir(parents=True)
    (ap / "config.yaml").write_text(
        'repo: "."\nbase_branch: "main"\ngates:\n  test: {cmd: "true", cwd: "."}\n' + extra)
    return tmp_path


def _config(*args):
    return subprocess.run([sys.executable, str(ENGINE / "config.py"), *args],
                          capture_output=True, text=True)


def _shellenv(target):
    r = _config("shellenv", str(target))
    assert r.returncode == 0, r.stderr
    out = {}
    for line in r.stdout.splitlines():
        assert line.startswith("export ")
        name, _, val = line[len("export "):].partition("=")
        out[name] = shlex.split(val)[0] if val else ""
    return out


def test_shellenv_emits_orbit_concurrency_default_one(tmp_path):
    """run.sh reads ORBIT_SLOTS as ${ORBIT_SLOTS:-${ORBIT_CONCURRENCY:-1}} - an
    absent config key must still deliver today's single worker."""
    assert _shellenv(_cfg_repo(tmp_path))["ORBIT_CONCURRENCY"] == "1"


def test_shellenv_emits_configured_concurrency(tmp_path):
    assert _shellenv(_cfg_repo(tmp_path, "concurrency: 4\n"))["ORBIT_CONCURRENCY"] == "4"


def test_shellenv_emits_workable_categories_for_the_wrappers_pick(tmp_path):
    """run.sh's own pick has to honour workable_categories (the clause the agent
    used to apply), so shellenv hands it over comma-joined. No key = empty = no
    category restriction."""
    assert _shellenv(_cfg_repo(tmp_path))["ORBIT_WORKABLE"] == ""
    target = _cfg_repo(tmp_path / "b", "workable_categories: [documentation, testing]\n")
    assert _shellenv(target)["ORBIT_WORKABLE"] == "documentation,testing"


def test_validate_accepts_positive_concurrency(tmp_path):
    r = _config("validate", str(_cfg_repo(tmp_path, "concurrency: 3\n")))
    assert r.returncode == 0, r.stdout + r.stderr


def test_validate_absent_concurrency_passes(tmp_path):
    r = _config("validate", str(_cfg_repo(tmp_path)))
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("value", ["0", "-2", '"three"', "true", "1.5", "33", "3200"])
def test_validate_rejects_out_of_range_concurrency(tmp_path, value):
    """F5: ONE contract, 1..32. Above the ceiling it stops being a config and
    becomes a fork bomb - every slot is a full claude cycle with its own
    worktree."""
    r = _config("validate", str(_cfg_repo(tmp_path, f"concurrency: {value}\n")))
    assert r.returncode == 1, r.stdout
    assert "concurrency" in r.stdout
    assert "1 and 32" in r.stdout


def test_validate_accepts_the_top_of_the_range(tmp_path):
    r = _config("validate", str(_cfg_repo(tmp_path, "concurrency: 32\n")))
    assert r.returncode == 0, r.stdout + r.stderr


# ── the shipped shell blocks, extracted and driven ─────────────────────────

def _extract(start_marker: str, end_marker: str) -> str:
    """Pull a block out of run.sh itself (tests/test_secrets.py's technique)."""
    assert start_marker in RUN_SH and end_marker in RUN_SH, "run.sh markers moved"
    return textwrap.dedent(RUN_SH.split(start_marker, 1)[1].split(end_marker, 1)[0])


_WALK = _extract(
    "# --- claim-then-run candidate walk (extracted verbatim by tests/test_slot_loop.py) ---\n",
    "# --- end claim-then-run candidate walk ---")
_AUTOFEED = _extract(
    "# --- auto-feed refill threshold (extracted verbatim by tests/test_autopromote.py) ---\n",
    "# --- end auto-feed refill threshold ---")
_LIVENESS = _extract(
    "# --- worker liveness (extracted verbatim by tests/test_slot_loop.py) ---\n",
    "# --- end worker liveness ---")
_SLOT_NAMES = _extract(
    "# --- per-slot state filenames -------------------------------------------------\n",
    "export AP_HOME AP_STATE ORBIT_HOME")
_LOOP_LIVENESS = _extract(
    "# --- loop liveness (extracted verbatim by tests/test_slot_loop.py) ---\n",
    "# --- end loop liveness ---")
_BOUND = _extract(
    "# --- concurrency bound (extracted verbatim by tests/test_slot_loop.py) --------\n",
    "# --- end concurrency bound ---")


def _fake_ledger(engine_dir: Path, rc_by_tid: dict, default_rc: int = 0):
    """A stand-in for ledger.py that answers `claim` with a scripted exit code
    per task id, and records how it was called."""
    (engine_dir / "ledger.py").write_text(
        "import sys, os, json\n"
        f"RC = {rc_by_tid!r}\n"
        f"DEFAULT = {default_rc}\n"
        "argv = sys.argv[1:]\n"
        "if argv and argv[0] == 'worked-ids':\n"
        "    sys.exit(0)\n"
        "tid = argv[argv.index('--exclusive') + 1] if '--exclusive' in argv else ''\n"
        "calls = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'calls.jsonl')\n"
        "open(calls, 'a').write(json.dumps(argv) + '\\n')\n"
        "sys.exit(RC.get(tid, DEFAULT))\n")


def _walk_env(tmp_path, queue_tasks, rc_by_tid=None, default_rc=0):
    state = tmp_path / "state"
    engine = tmp_path / "engine"
    state.mkdir(exist_ok=True)
    engine.mkdir(exist_ok=True)
    (state / "queue.json").write_text(json.dumps({"tasks": queue_tasks}))
    (state / "logs").mkdir(exist_ok=True)
    _fake_ledger(engine, rc_by_tid or {}, default_rc)
    return state, engine


_WALK_PRELUDE = (
    'STATE="$1"; ENGINE="$2"; SLOT="$3"; LOGDIR="$STATE/logs"\n'
    'ts(){ echo TS; }\n'
    'logf(){ echo "$*" >> "$LOGDIR/orbit.log"; }\n'
)


def _run_walk(tmp_path, queue_tasks, tail, *, rc_by_tid=None, default_rc=0, slot="0",
              workable=None):
    state, engine = _walk_env(tmp_path, queue_tasks, rc_by_tid, default_rc)
    # Both blocks: count_pickable (auto-feed) has to agree with pickable_ids, so
    # the tests drive them side by side.
    script = _WALK_PRELUDE + _AUTOFEED + _WALK + "\n" + tail
    env = dict(os.environ)
    if workable is not None:
        env["ORBIT_WORKABLE"] = workable
    else:
        env.pop("ORBIT_WORKABLE", None)
    r = subprocess.run(["bash", "-uo", "pipefail", "-c", script, "bash",
                        str(state), str(engine), slot],
                       capture_output=True, text=True, env=env)
    calls = []
    cf = engine / "calls.jsonl"
    if cf.exists():
        calls = [json.loads(l) for l in cf.read_text().splitlines() if l.strip()]
    return r, calls


def test_pickable_ids_orders_by_priority_and_skips_worked_and_skipped(tmp_path):
    """The agent used to pick the highest-priority pickable task for itself; the
    wrapper claims on its behalf now and must not silently downgrade that to
    backlog order."""
    tasks = [{"id": "low-1", "priority": "low"},
             {"id": "med-1", "priority": "medium"},
             {"id": "high-1", "priority": "high"},
             {"id": "med-2"},                       # no priority = medium
             {"id": "skipped-1", "priority": "high"}]
    state = tmp_path / "state"
    state.mkdir()
    (state / "skips.txt").write_text("skipped-1\n")
    r, _ = _run_walk(tmp_path, tasks, "pickable_ids")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["high-1", "med-1", "med-2", "low-1"]


def test_pickable_ids_skips_categories_this_target_cannot_work(tmp_path):
    """The clause the wrapper's pick must not lose: the agent only ever picked a
    task whose category is in config.workable_categories. Without it the wrapper
    claims work the agent will refuse — a burnt cycle and an escalation, every
    tick. Probe shape from the review: workable [documentation] + a HIGHER
    priority testing task must still pick the documentation task."""
    tasks = [{"id": "t-testing", "category": "testing", "priority": "high"},
             {"id": "t-doc", "category": "documentation", "priority": "medium"}]
    r, _ = _run_walk(tmp_path, tasks, "pickable_ids", workable="documentation")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["t-doc"]


def test_pickable_ids_lets_a_forced_task_through_any_category(tmp_path):
    """`forced: true` IS the operator saying "yes, this excluded category"."""
    tasks = [{"id": "t-forced", "category": "security", "priority": "low", "forced": True},
             {"id": "t-doc", "category": "documentation", "priority": "low"}]
    r, _ = _run_walk(tmp_path, tasks, "pickable_ids", workable="documentation")
    assert sorted(r.stdout.split()) == ["t-doc", "t-forced"]


def test_pickable_ids_filters_nothing_when_no_categories_are_configured(tmp_path):
    """An empty ORBIT_WORKABLE means the target has no workable_categories key.
    Such a deployment must keep behaving exactly as it does today, not starve."""
    tasks = [{"id": "t-testing", "category": "testing"}, {"id": "t-none"}]
    r, _ = _run_walk(tmp_path, tasks, "pickable_ids", workable="")
    assert sorted(r.stdout.split()) == ["t-none", "t-testing"]


def test_count_pickable_applies_the_same_filters_as_pickable_ids(tmp_path):
    """Auto-feed tops the queue up to SLOTS *pickable* tasks. If the count
    includes tasks no slot may claim, it refuses to refill while every slot
    idles."""
    tasks = [{"id": "t-testing", "category": "testing"},
             {"id": "t-doc", "category": "documentation"},
             {"id": "t-doc", "category": "documentation"},        # duplicate id
             {"id": "t-forced", "category": "release", "forced": True}]
    r, _ = _run_walk(tmp_path, tasks, "count_pickable", workable="documentation")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "2"                                # t-doc + t-forced


def test_pickable_ids_is_empty_without_a_queue(tmp_path):
    r, _ = _run_walk(tmp_path, [], "pickable_ids")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""


def test_task_title_is_the_first_line_of_the_task_contract(tmp_path):
    tasks = [{"id": "t1", "task": "Add a missing test\n\nWHY:\nbecause\n"}]
    r, _ = _run_walk(tmp_path, tasks, 'task_title t1')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "Add a missing test"


def test_task_title_falls_back_to_the_id(tmp_path):
    r, _ = _run_walk(tmp_path, [{"id": "t1", "task": ""}], 'task_title t1')
    assert r.stdout.strip() == "t1"
    r, _ = _run_walk(tmp_path, [], 'task_title ghost')
    assert r.stdout.strip() == "ghost"


def test_claim_first_walks_past_lost_claims_to_the_winner(tmp_path):
    """exit 5 = another slot already holds that task. The walk must try the next
    candidate rather than burn the tick (or worse, work a claimed task)."""
    tasks = [{"id": "t1", "priority": "high"}, {"id": "t2", "priority": "high"},
             {"id": "t3", "priority": "high"}]
    r, calls = _run_walk(tmp_path, tasks,
                         'tid="$(claim_first "$(pickable_ids)")"; echo "rc=$? won=$tid"',
                         rc_by_tid={"t1": 5, "t2": 5, "t3": 0})
    assert "rc=0 won=t3" in r.stdout, r.stdout + r.stderr
    claimed = [c[2] for c in calls if c[0] == "claim"]
    assert claimed == ["t1", "t2", "t3"]


def test_claim_first_passes_exclusive_pid_and_slot(tmp_path):
    r, calls = _run_walk(tmp_path, [{"id": "t1", "task": "Fix the thing\nmore"}],
                         'claim_first "$(pickable_ids)" >/dev/null', slot="2")
    argv = [c for c in calls if c[0] == "claim"][0]
    assert argv[0:3] == ["claim", "--exclusive", "t1"]
    assert argv[3] == "Fix the thing"          # title, not the whole contract
    assert "--pid" in argv and "--slot" in argv
    assert argv[argv.index("--slot") + 1] == "2"
    assert int(argv[argv.index("--pid") + 1]) > 0


def test_claim_first_reports_nothing_left_to_win(tmp_path):
    r, _ = _run_walk(tmp_path, [{"id": "t1"}, {"id": "t2"}],
                     'claim_first "$(pickable_ids)"; echo "rc=$?"',
                     rc_by_tid={"t1": 5, "t2": 5})
    assert "rc=1" in r.stdout
    assert "t1" not in r.stdout.replace("rc=1", "")   # stdout carries ONLY a won id


def test_claim_first_separates_a_broken_ledger_from_a_lost_race(tmp_path):
    """An unexpected claim failure must not read as "another slot has it": one is
    a healthy queue, the other is a broken ledger nobody would notice."""
    r, _ = _run_walk(tmp_path, [{"id": "t1"}, {"id": "t2"}],
                     'claim_first "$(pickable_ids)"; echo "rc=$?"',
                     rc_by_tid={"t1": 5, "t2": 1})
    assert "rc=3" in r.stdout
    assert 'every claim attempt failed' in RUN_SH   # the caller's distinct headline


def test_claim_first_does_not_let_a_child_eat_the_candidate_list(tmp_path):
    """The walk reads its candidates on stdin; a claim child that read stdin
    would swallow the rest of them, so the call is `</dev/null`."""
    assert '--pid "$$" --slot "$SLOT" </dev/null' in RUN_SH


def test_claim_first_treats_a_lock_timeout_as_transient(tmp_path):
    """exit 6 = the ledger lock was busy. Not a loss: stop for this tick (the
    caller idles) instead of stampeding the rest of the queue."""
    r, calls = _run_walk(tmp_path, [{"id": "t1"}, {"id": "t2"}],
                         'claim_first "$(pickable_ids)"; echo "rc=$?"',
                         rc_by_tid={"t1": 6, "t2": 0})
    assert "rc=2" in r.stdout
    assert [c[2] for c in calls if c[0] == "claim"] == ["t1"]


def test_claim_first_stdout_is_only_the_won_id(tmp_path):
    """The caller reads this with $( ) — a log line leaking to stdout would be
    spliced into the task id."""
    r, _ = _run_walk(tmp_path, [{"id": "t1"}, {"id": "t2"}],
                     'claim_first "$(pickable_ids)"',
                     rc_by_tid={"t1": 5, "t2": 0})
    assert r.stdout.strip() == "t2"
    assert (tmp_path / "state" / "logs" / "orbit.log").exists()   # the skip WAS logged


# ── worker pid file: the reaper's only liveness signal ─────────────────────

def _run_liveness(tmp_path, tail, slot="0"):
    state = tmp_path / "state"
    (state / "logs").mkdir(parents=True, exist_ok=True)
    script = (f'STATE="$1"; SLOT="$2"; LOGDIR="$STATE/logs"\n'
              'WORKER_PID_FILE="$STATE/workers/$SLOT.pid"\n'
              'ts(){ echo TS; }\n'
              'logf(){ echo "$*" >> "$LOGDIR/orbit.log"; }\n'
              + _LIVENESS + "\n" + tail)
    return subprocess.run(["bash", "-uo", "pipefail", "-c", script, "bash", str(state), slot],
                          capture_output=True, text=True, env=dict(os.environ))


def test_write_worker_pid_writes_the_shape_the_reaper_reads(tmp_path):
    r = _run_liveness(tmp_path, 'write_worker_pid task-42; echo "$$"', slot="3")
    assert r.returncode == 0, r.stderr
    info = json.loads((tmp_path / "state" / "workers" / "3.pid").read_text())
    assert info["tid"] == "task-42"
    assert isinstance(info["pid"], int) and info["pid"] > 0
    assert info["started"].endswith("Z") and len(info["started"]) == 20


def test_record_cycle_pid_folds_the_cycles_identity_into_the_same_file(tmp_path):
    """F2: the file the dashboard's Abort reads must name the CYCLE, not just
    the loop. cycle_pgid is only written when that pid really leads its own
    group - here the shell's own $$ does not, so it must be absent while
    cycle_pid is present, and the loop's own pid/tid must survive."""
    r = _run_liveness(tmp_path, 'write_worker_pid t1; record_cycle_pid "$$"')
    assert r.returncode == 0, r.stderr
    info = json.loads((tmp_path / "state" / "workers" / "0.pid").read_text())
    assert info["tid"] == "t1"
    assert info["cycle_pid"] == info["pid"]        # $$ of the same shell
    assert isinstance(info["pid"], int)
    assert info["started"].endswith("Z")           # not clobbered by the update


def test_record_cycle_pid_records_a_pgid_only_for_a_real_group_leader(tmp_path):
    """A setsid'd cycle IS its own group leader, and only then may an aborter
    killpg it - otherwise the group is the LOOP's and killpg would take the
    slot down with the task (the C2 bug)."""
    # A python child that setsids itself, exactly like run.sh's wrapper, then
    # sleeps long enough for record_cycle_pid's own probe to see it.
    r = _run_liveness(tmp_path, (
        'python3 -c \'import os, time\n'
        'os.setsid()\n'
        'time.sleep(5)\' & CPID=$!\n'
        'write_worker_pid t1; record_cycle_pid "$CPID"; kill "$CPID" 2>/dev/null; echo "$CPID"'))
    assert r.returncode == 0, r.stderr
    cpid = int(r.stdout.strip().splitlines()[-1])
    info = json.loads((tmp_path / "state" / "workers" / "0.pid").read_text())
    assert info["cycle_pid"] == cpid
    assert info["cycle_pgid"] == cpid


def test_record_cycle_pid_leaves_a_missing_worker_file_alone(tmp_path):
    """No claim, no file: the update must not invent one (a file with no
    pid/tid reads as an abandoned claim to the reaper)."""
    r = _run_liveness(tmp_path, 'record_cycle_pid 12345; echo done')
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "state" / "workers" / "0.pid").exists()
    assert "could not record cycle pid" in (tmp_path / "state" / "logs" / "orbit.log").read_text()


# ── the loop pid file: "slot N is owned", idle naps included ───────────────

def _run_loop_liveness(tmp_path, tail, slot="0"):
    state = tmp_path / "state"
    (state / "logs").mkdir(parents=True, exist_ok=True)
    script = (f'STATE="$1"; SLOT="$2"; LOGDIR="$STATE/logs"\n'
              'LOOP_PID_FILE="$STATE/workers/loop-$SLOT.pid"\n'
              'ts(){ echo TS; }\n'
              'logf(){ echo "$*" >> "$LOGDIR/orbit.log"; }\n'
              + _LOOP_LIVENESS + "\n" + tail)
    return subprocess.run(["bash", "-uo", "pipefail", "-c", script, "bash", str(state), slot],
                          capture_output=True, text=True, env=dict(os.environ))


def test_write_loop_pid_writes_the_shape_the_launcher_reads(tmp_path):
    r = _run_loop_liveness(tmp_path, 'write_loop_pid; echo "$$"', slot="2")
    assert r.returncode == 0, r.stderr
    info = json.loads((tmp_path / "state" / "workers" / "loop-2.pid").read_text())
    assert info["slot"] == "2"
    assert isinstance(info["pid"], int) and info["pid"] > 0
    assert info["started"].endswith("Z") and len(info["started"]) == 20


def test_clear_loop_pid_removes_it_and_is_idempotent(tmp_path):
    r = _run_loop_liveness(tmp_path, 'write_loop_pid; clear_loop_pid; clear_loop_pid')
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "state" / "workers" / "loop-0.pid").exists()


def test_loop_pid_file_is_written_at_startup_and_dropped_by_the_exit_trap():
    """It has to cover the loop's whole life - written before the first
    iteration, removed on the way out - or an idle slot reads as unowned and
    Start puts a second process on it."""
    up_at = RUN_SH.index('log "==== orbit up')
    assert up_at < RUN_SH.index("write_loop_pid\n")
    assert RUN_SH.index("write_loop_pid\n") < RUN_SH.index("while true; do")
    assert "trap 'clear_worker_pid; clear_loop_pid' EXIT" in RUN_SH
    assert 'LOOP_PID_FILE="$STATE/workers/loop-$SLOT.pid"' in RUN_SH


# ── the concurrency bound, clamped in the shell ────────────────────────────

def _run_bound(slots_env):
    script = ('SLOTS="$1"\n' + _BOUND + '\necho "$SLOTS|$SLOTS_WARN"\n')
    r = subprocess.run(["bash", "-uo", "pipefail", "-c", script, "bash", slots_env],
                       capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0, r.stderr
    slots, warn = r.stdout.strip().split("|", 1)
    return slots, warn


@pytest.mark.parametrize("given,expect", [("1", "1"), ("2", "2"), ("32", "32")])
def test_run_sh_leaves_in_range_slot_counts_alone(given, expect):
    slots, warn = _run_bound(given)
    assert slots == expect
    assert warn == ""


@pytest.mark.parametrize("given,expect", [("0", "1"), ("33", "32"), ("999", "32"),
                                          ("", "1"), ("two", "1"), ("1.5", "1")])
def test_run_sh_clamps_out_of_range_slot_counts_and_warns(given, expect):
    """ORBIT_SLOTS can be set by hand or by a service unit older than the
    bound, so config.py validate is not the last line of defence. Clamping
    beats refusing to boot - but never silently."""
    slots, warn = _run_bound(given)
    assert slots == expect
    assert warn and "clamped" in warn


def test_clear_worker_pid_removes_it(tmp_path):
    r = _run_liveness(tmp_path, 'write_worker_pid t1; clear_worker_pid; clear_worker_pid')
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "state" / "workers" / "0.pid").exists()


def test_ledger_reap_reads_the_pid_file_this_loop_writes(tmp_path):
    """End-to-end on the contract between the two: run.sh's own function writes
    the file, and ledger.py's liveness check must accept it for THIS pid+tid and
    reject it for another tid."""
    state = tmp_path / "state"
    (state / "logs").mkdir(parents=True)
    r = _run_liveness(tmp_path, 'write_worker_pid t1; echo "$$"')
    assert r.returncode == 0, r.stderr
    written_pid = json.loads((state / "workers" / "0.pid").read_text())["pid"]
    env = dict(os.environ, AP_STATE=str(state))
    probe = subprocess.run(
        [sys.executable, "-c",
         "import os, sys; sys.path.insert(0, sys.argv[1]); import ledger;"
         "print(ledger._slot_worker_matches(0, sys.argv[2]),"
         "      ledger._slot_worker_matches(0, 'other'))",
         str(ENGINE), "t1"],
        capture_output=True, text=True, env=env)
    assert probe.returncode == 0, probe.stderr
    # The bash process that wrote the file has exited, so liveness is False for
    # both — what matters is that ledger.py PARSED it (no crash) and that the tid
    # mismatch is rejected. Re-write it naming a live pid to check the match arm.
    (state / "workers" / "0.pid").write_text(json.dumps(
        {"pid": os.getpid(), "tid": "t1", "started": "2026-08-22T00:00:00Z"}))
    probe = subprocess.run(
        [sys.executable, "-c",
         "import os, sys; sys.path.insert(0, sys.argv[1]); import ledger;"
         "print(ledger._slot_worker_matches(0, 't1'),"
         "      ledger._slot_worker_matches(0, 'other'))",
         str(ENGINE)],
        capture_output=True, text=True, env=env)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.split() == ["True", "False"]
    assert written_pid > 0


# ── per-slot filenames: the dashboard compatibility contract ───────────────

def _run_slot_names(slot):
    script = ('SLOT="$1"; STATE=/st\n' + _SLOT_NAMES
              + '\necho "$MARKER|$CAND_MARKER|$IDLE_MARKER|$WORKER_PID_FILE"\n')
    r = subprocess.run(["bash", "-uo", "pipefail", "-c", script, "bash", str(slot)],
                       capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().split("|")


def test_slot_zero_keeps_the_legacy_marker_filenames():
    """command_center.py reads these three by exact name. A rename at slot 0
    would blank the dashboard's task identity and idle reason."""
    marker, cand, idle, pidfile = _run_slot_names(0)
    assert marker == "/st/.current-task-id"
    assert cand == "/st/.cycle-candidate"
    assert idle == "/st/.idle-reason"
    assert pidfile == "/st/workers/0.pid"


def test_higher_slots_namespace_their_markers():
    marker, cand, idle, pidfile = _run_slot_names(2)
    assert marker == "/st/.current-task-id.2"
    assert cand == "/st/.cycle-candidate.2"
    assert idle == "/st/.idle-reason.2"
    assert pidfile == "/st/workers/2.pid"


# ── structural guards on the parts only a live cycle exercises ─────────────

def test_worktree_path_is_per_slot():
    assert 'WT="${AP_WORKTREE:-${REPO}-orbit-wt-${SLOT}}"' in RUN_SH


def test_slot_and_slots_default_to_todays_single_worker():
    assert 'SLOT="${ORBIT_SLOT:-0}"' in RUN_SH
    assert 'SLOTS="${ORBIT_SLOTS:-${ORBIT_CONCURRENCY:-1}}"' in RUN_SH


def test_log_lines_carry_the_slot_after_the_timestamp():
    """The dashboard anchors heartbeat/duration parsing on a leading "[<ts>]"
    (command_center.py _LOGTS), so the slot tag must come after it."""
    assert 'log(){ echo "[$(ts)] [slot $SLOT] $*" | tee -a "$LOGDIR/orbit.log"; }' in RUN_SH


def test_repo_wide_housekeeping_runs_only_on_slot_zero():
    """One ledger, one backlog, one queue, one knowledge base, one sweep: N
    slots must not each run these. Every job named here has to sit inside a
    `[ "$SLOT" = "0" ]` block."""
    guarded = ""
    rest = RUN_SH
    while 'if [ "$SLOT" = "0" ]; then' in rest:
        _, rest = rest.split('if [ "$SLOT" = "0" ]; then', 1)
        block, rest = rest.split("\n  fi\n", 1)
        guarded += block
    for job in ("ledger.py\" reap", "autoclose.py", "knowledge.py", "qa_run.py",
                "backlog_to_tasks.py", "autopromote.py"):
        assert job in guarded, f"{job} is not inside a slot-0-only block"
    # ...and the claim/cycle path must NOT be inside one.
    for shared in ("run_cycle \"$cyclelog\"", "claim_first", "mcp.py"):
        assert shared not in guarded, f"{shared} must run on every slot"


def test_cycle_is_told_which_task_to_work():
    assert 'prompt="/orbit-cycle $2"' in RUN_SH
    assert 'run_cycle "$cyclelog" "$tid"' in RUN_SH


def test_log_and_patch_are_named_after_the_task():
    assert 'cyclelog="$LOGDIR/cycle-$safe_tid-$stamp.log"' in RUN_SH
    assert 'patch="$DIFFDIR/cycle-$safe_tid-$stamp.patch"' in RUN_SH


def test_worker_pid_file_is_written_right_after_the_claim_and_released_after():
    claim_at = RUN_SH.index('tid="$(claim_first "$candidates")"')
    write_at = RUN_SH.index('write_worker_pid "$tid"')
    run_at = RUN_SH.index('run_cycle "$cyclelog" "$tid"')
    assert claim_at < write_at < run_at, "the pid file must be written between the claim and the cycle"
    assert RUN_SH.count("release_task\n") >= 4, "every post-cycle exit path must release the task"
    assert "trap 'clear_worker_pid; clear_loop_pid' EXIT" in RUN_SH


def test_no_signal_traps_are_installed():
    """A TERM/INT trap would be run only AFTER the current foreground command
    finishes, so `kill` (or a launchd stop) would have to wait out a `sleep 600`
    or a whole cycle instead of ending the process at once, as it does today. A
    leaked pid file is harmless by comparison: every reader believes one only
    while its pid is still alive."""
    traps = [ln.strip() for ln in RUN_SH.splitlines() if ln.strip().startswith("trap ")]
    assert traps == ["trap 'clear_worker_pid; clear_loop_pid' EXIT"], traps


def test_watchdog_stays_detached_from_the_command_substitution_pipe():
    """Regression guard, NOT a style check: if the watchdog subshell inherits
    one_iteration's stdout pipe, killing it orphans the `sleep` holding the
    pipe's write end open and the whole loop wedges for a full CYCLE_TIMEOUT."""
    assert ('( sleep "$CYCLE_TIMEOUT"; { kill -TERM -"$cpid" || kill -TERM "$cpid"; } 2>/dev/null; '
            'sleep 10; { kill -KILL -"$cpid" || kill -KILL "$cpid"; } 2>/dev/null ) '
            '</dev/null >/dev/null 2>&1 &') in RUN_SH
    assert 'pkill -P "$wpid"' in RUN_SH


def test_watchdog_kills_the_cycles_group_with_a_bare_pid_fallback():
    """The cycle leads its own group now (see the session wrapper), so the
    watchdog can and must take the whole cycle tree: TERMing only $cpid hits
    the caffeinate wrapper, which does not forward signals, leaving claude
    running while the loop believes it killed the cycle. The `||` fallback
    covers a cycle that leads no group (setsid refused)."""
    wd = RUN_SH.split("# Watchdog:", 1)[1].split("local wpid", 1)[0]
    assert 'kill -TERM -"$cpid" || kill -TERM "$cpid"' in wd
    assert 'kill -KILL -"$cpid" || kill -KILL "$cpid"' in wd


def test_stop_is_still_checked_only_at_the_top_of_the_outer_loop():
    """STOP stops new claims; it must never abort an in-flight cycle. Two
    checks now: the outer loop's, and the ONE_SHOT seam's (which is a
    different admission point, not a second check inside the loop)."""
    assert RUN_SH.count('[ -f "$STOP_FILE" ]') == 2
    loop = RUN_SH.split("while true; do", 1)[1]
    assert loop.count('[ -f "$STOP_FILE" ]') == 1
    assert '[ -f "$STOP_FILE" ]' in loop.split("status=\"$(one_iteration)\"", 1)[0]


def test_one_shot_honors_stop():
    """F9: ORBIT_ONE_SHOT was the one path that walked past the workspace kill
    switch. A smoke that wants its iteration disarms STOP explicitly."""
    one_shot = RUN_SH.split('if [ "$ONE_SHOT" = "1" ]; then', 1)[1].split("while true; do", 1)[0]
    stop_at = one_shot.index('[ -f "$STOP_FILE" ]')
    assert stop_at < one_shot.index('one_iteration'), "STOP must be checked BEFORE the iteration"
    assert 'exit 0' in one_shot[stop_at:]


def test_the_cycle_runs_in_its_own_session():
    """F2/C2: without this the cycle shares the loop's process group, so any
    signal aimed at the cycle (Abort, the watchdog's group kill) hits the loop
    too. python3 os.setsid+execvp is the portable mechanism - setsid(1) does
    not exist on macOS and `set -m` is interactive-only."""
    assert "os.setsid()" in RUN_SH
    assert "os.execvp(sys.argv[1], sys.argv[1:])" in RUN_SH
    assert 'python3 -c "$CYCLE_SESSION_PY" $caff "$CLAUDE_BIN" --print "$prompt"' in RUN_SH
    # ...and the cycle's identity is recorded where an aborter can find it,
    # after the spawn (the pid does not exist before it) and before the wait.
    spawn = RUN_SH.index('local cpid=$!')
    assert spawn < RUN_SH.index('record_cycle_pid "$cpid"') < RUN_SH.index('wait "$cpid"')


def test_slots_max_matches_config_pys_concurrency_bound():
    """ONE concurrency contract: run.sh clamps to the same ceiling config.py
    validate refuses above."""
    sys.path.insert(0, str(ENGINE))
    import config
    assert f"SLOTS_MAX={config.CONCURRENCY_MAX}" in RUN_SH


def test_the_no_commit_escalation_net_is_keyed_on_ledger_state():
    """The old guard ("id not in worked-ids") cannot fire now that the wrapper's
    own claim puts it there, which left every no-op cycle — watchdog kill, agent
    refusal, usage-limit cut — parked in_progress and invisible until the reaper."""
    assert "worked-ids 2>/dev/null | tr ' ' '\\n' | grep -qx" not in RUN_SH
    assert 'lstate="$(python3 "$ENGINE/ledger.py" state "$tid"' in RUN_SH
    assert "committed|pushed|merged|rejected|escalated|queued) : ;;" in RUN_SH


def test_higher_slots_do_not_wait_out_slot_zeros_refill():
    """Only slot 0 regenerates the queue, so a higher slot napping ten minutes on
    an empty queue can sit idle with claimable work already waiting."""
    assert 'EMPTY_BACKOFF=600; [ "$SLOT" = "0" ] || EMPTY_BACKOFF=60' in RUN_SH
    assert 'EMPTY) set_idle_reason "queue-empty" "$EMPTY_BACKOFF"; sleep "$EMPTY_BACKOFF" ;;' in RUN_SH


def test_worktree_existence_check_is_anchored():
    """`grep -q "$WT"` also matches another slot's worktree (…-wt-1 is a prefix
    of …-wt-10), so a slot could think its own worktree exists and never make it."""
    assert 'worktree list --porcelain | grep -qx "worktree $WT"' in RUN_SH


def test_spend_and_count_go_through_spend_py():
    assert 'spend.py" add "$STATE" "$(today)" "$cost"' in RUN_SH
    assert 'spend.py" bump-count "$STATE" "$(today)" --cap "$MAX_TASKS_PER_DAY"' in RUN_SH
    # The lost-update-prone inline writers are gone for good.
    assert 'echo $((count+1)) > "$STATE/.count-$(today)"' not in RUN_SH
    assert 'open(sf, "w").write' not in RUN_SH
