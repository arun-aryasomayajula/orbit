# engine/autopromote.py - the AUTO_PROMOTE flag's one-task-per-call feeder.
#
# The finding this pins: backlog_append writes EVERY machine-filed proposal as
# `autopilot: human`, so before this the unattended product path did not exist -
# a metric watch's weekly insight task sat on the board until someone clicked
# Promote, every week, for work they had already asked to happen on a cadence.
#
# Eligibility is now category + provenance (autopromote._eligible): an
# unattended signal adapter's task is auto-feedable; a hand-written one is not,
# including the dashboard's own New-task form, which also stamps a `source:`
# field (`dashboard`) and so must NOT be waved through by mere presence of it.
#
# Run: python3 -m pytest tests/test_autopromote.py -v   (from orbit/)
import json
import os
import shlex
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
import yaml

ORBIT = Path(__file__).resolve().parent.parent
SCRIPT = ORBIT / "engine" / "autopromote.py"


def _task(tid, category, *, autopilot="human", source=None, priority="medium"):
    lines = [f"  - id: {tid}",
             '    title: "a task with a long enough title"',
             f"    category: {category}",
             f"    autopilot: {autopilot}",
             "    status: proposed",
             f"    priority: {priority}"]
    if source is not None:
        lines.append(f"    source: {source}")
    lines.append('    acceptance_criteria:')
    lines.append('      - "does the thing"')
    return "\n".join(lines) + "\n"


@pytest.fixture()
def target(tmp_path):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text('repo: "."\nbase_branch: "main"\ngates:\n  t: {cmd: "true"}\n')
    return tmp_path


def _write_backlog(target, *blocks):
    (target / ".autopilot" / "backlog.yaml").write_text("tasks:\n" + "".join(blocks))


def _run(target):
    ap = target / ".autopilot"
    env = dict(os.environ, AP_HOME=str(ap), AP_STATE=str(ap / "state"),
               ORBIT_HOME=str(ORBIT))
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=str(target),
                          capture_output=True, text=True, env=env)


def _tasks(target):
    doc = yaml.safe_load((target / ".autopilot" / "backlog.yaml").read_text()) or {}
    return {t["id"]: t for t in (doc.get("tasks") or [])}


# --- the unattended product path -------------------------------------------

def test_watch_filed_insight_task_is_promoted(target):
    """A metric_watch-filed task: `autopilot: human` (backlog_append writes
    nothing else) plus an unattended source. AUTO_PROMOTE covers it."""
    _write_backlog(target, _task("watch-wow-signup-20260101", "insight",
                                 source="metric_watch"))
    r = _run(target)
    assert r.returncode == 0, r.stderr
    t = _tasks(target)["watch-wow-signup-20260101"]
    assert t["status"] == "queued"
    assert t["autopilot"] == "allow"


@pytest.mark.parametrize("category", ["insight", "funnel", "hypothesis"])
def test_every_product_category_is_promotable(target, category):
    _write_backlog(target, _task(f"watch-x-{category}", category, source="metric_watch"))
    assert _run(target).returncode == 0
    assert _tasks(target)[f"watch-x-{category}"]["status"] == "queued"


# --- hand-written work stays the operator's call ----------------------------

def test_hand_written_human_task_is_never_promoted(target):
    """No source field at all - a task typed straight into backlog.yaml."""
    _write_backlog(target, _task("hand-written-insight", "insight"))
    r = _run(target)
    assert r.returncode == 0, r.stderr
    t = _tasks(target)["hand-written-insight"]
    assert t["status"] == "proposed"
    assert t["autopilot"] == "human"
    assert "no eligible" in r.stdout


def test_dashboard_filed_task_is_never_promoted(target):
    """The dashboard's New-task form stamps `source: dashboard`. Presence of a
    source field must not be what makes a task eligible - the operator who
    typed it can queue it in the same click."""
    _write_backlog(target, _task("typed-in-the-ui", "insight", source="dashboard"))
    r = _run(target)
    assert r.returncode == 0, r.stderr
    assert _tasks(target)["typed-in-the-ui"]["status"] == "proposed"
    assert "no eligible" in r.stdout


def test_escalation_answer_source_is_never_promoted(target):
    _write_backlog(target, _task("answered-escalation", "bug", source="escalation-answer"))
    assert _run(target).returncode == 0
    assert _tasks(target)["answered-escalation"]["status"] == "proposed"


# --- the pre-existing contract still holds ---------------------------------

def test_ungated_chore_task_still_promotes(target):
    """The original arm: `autopilot` is anything but human, so the operator
    already un-gated it. No source field needed."""
    _write_backlog(target, _task("add-missing-tests", "testing", autopilot="allow"))
    assert _run(target).returncode == 0
    assert _tasks(target)["add-missing-tests"]["status"] == "queued"


def test_risky_category_is_never_promoted(target):
    _write_backlog(target, _task("rewrite-the-core", "refactor", autopilot="allow"),
                   _task("watch-refactor-thing", "refactor", source="metric_watch"))
    r = _run(target)
    assert r.returncode == 0, r.stderr
    assert all(t["status"] == "proposed" for t in _tasks(target).values())


def test_one_task_per_call_highest_priority_first(target):
    _write_backlog(target,
                   _task("watch-low", "insight", source="metric_watch", priority="low"),
                   _task("watch-high", "insight", source="metric_watch", priority="high"))
    assert _run(target).returncode == 0
    tasks = _tasks(target)
    assert tasks["watch-high"]["status"] == "queued"
    assert tasks["watch-low"]["status"] == "proposed"      # one per call


def test_worked_and_skipped_ids_are_excluded(target):
    state = target / ".autopilot" / "state"
    (state / "ledger.json").write_text(json.dumps({"entries": {"watch-worked": {}}}))
    (state / "skips.txt").write_text("watch-skipped\n")
    _write_backlog(target,
                   _task("watch-worked", "insight", source="metric_watch"),
                   _task("watch-skipped", "insight", source="metric_watch"))
    r = _run(target)
    assert r.returncode == 0, r.stderr
    assert all(t["status"] == "proposed" for t in _tasks(target).values())
    assert "no eligible" in r.stdout


# --- Task 3: one lock for every backlog.yaml writer -------------------------

def test_write_goes_through_a_lock_and_tmp_replace(target):
    """Regression guard for the two things Task 3 changed in autopromote.py's
    write path: (1) the whole read-modify-write is under the shared backlog
    lock (locks.hold), and (2) the write itself is tmp-then-os.replace, not a
    bare BACKLOG.write_text() - a reader must never be able to observe a
    truncated backlog.yaml mid-write. Source-level rather than timing-based,
    since a single small write is too fast to reliably catch mid-flight."""
    src = (Path(__file__).resolve().parent.parent / "engine" / "autopromote.py").read_text()
    assert "import locks" in src
    assert "locks.hold(AP_STATE / \".backlog.lock\")" in src
    assert "os.replace(tmp, BACKLOG)" in src
    assert "BACKLOG.write_text(\"\".join(parts))" not in src   # the old bare (non-atomic) write


def test_reader_never_observes_a_torn_backlog_during_promote(target):
    """Probe via a concurrent reader loop: while autopromote.py runs (as a
    real separate process) and rewrites backlog.yaml, a reader thread in THIS
    process hammers the file. Every successful read must be complete,
    parseable YAML — os.replace()'s atomic rename means a reader only ever
    sees the fully-old or fully-new file, never a partial write."""
    blocks = [_task(f"pad-{i}", "testing", autopilot="allow") for i in range(300)]
    blocks.append(_task("watch-it", "insight", source="metric_watch"))
    _write_backlog(target, *blocks)
    backlog_path = target / ".autopilot" / "backlog.yaml"

    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            try:
                text = backlog_path.read_text()
            except OSError:
                continue
            try:
                doc = yaml.safe_load(text)
            except yaml.YAMLError as e:
                bad.append(f"unparseable mid-read: {e}")
                continue
            if not isinstance(doc, dict) or "tasks" not in doc:
                bad.append(f"malformed doc mid-read: {text[:200]!r}")

    t = threading.Thread(target=reader)
    t.start()
    try:
        r = _run(target)
    finally:
        stop.set()
        t.join(5)

    assert r.returncode == 0, r.stderr
    assert not bad, f"reader observed a torn/partial backlog.yaml: {bad[:3]}"


def _extract_run_sh_block(start_marker: str, end_marker: str) -> str:
    """Pull a run.sh block out of run.sh itself (same technique as
    tests/test_secrets.py's `_extract_secrets_parse_loop`) rather than
    keeping a second copy here — a copy silently stops testing the shipped
    logic the first time the two drift."""
    text = (Path(__file__).resolve().parent.parent / "engine" / "run.sh").read_text()
    assert start_marker in text and end_marker in text, "run.sh markers moved"
    return textwrap.dedent(text.split(start_marker, 1)[1].split(end_marker, 1)[0])


_AUTOFEED_SNIPPET = _extract_run_sh_block(
    "# --- auto-feed refill threshold (extracted verbatim by tests/test_autopromote.py) ---\n",
    "# --- end auto-feed refill threshold ---")


def _should_autofeed(state_dir: Path, *, auto_promote: bool, slots) -> bool:
    """Drive run.sh's real `should_autofeed`/`count_pickable` functions (not
    a reimplementation) against a fake STATE dir, so this test breaks the
    moment the shipped refill condition stops matching
    `count_pickable() < slots`."""
    if auto_promote:
        (state_dir / "AUTO_PROMOTE").write_text("on\n")
    script = (
        f'STATE={_sh(str(state_dir))}\n'
        f'ENGINE={_sh(str(Path(__file__).resolve().parent.parent / "engine"))}\n'
        f'SLOTS={_sh(str(slots))}\n'
        + _AUTOFEED_SNIPPET
        + '\nif should_autofeed; then echo YES; else echo NO; fi\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return "YES" in r.stdout.split()


def _sh(s: str) -> str:
    return shlex.quote(s)


@pytest.mark.parametrize("pickable,slots,expected", [
    (0, 1, True),    # default SLOTS=1 - the old "== 0" trigger, unchanged
    (1, 1, False),
    (0, 3, True),
    (2, 3, True),    # under a bigger SLOTS, auto-feed now fires earlier
    (3, 3, False),
])
def test_should_autofeed_uses_count_pickable_lt_slots(target, pickable, slots, expected):
    state = target / ".autopilot" / "state"
    (state / "queue.json").write_text(json.dumps(
        {"tasks": [{"id": f"q-{i}"} for i in range(pickable)]}))
    assert _should_autofeed(state, auto_promote=True, slots=slots) == expected


def test_should_autofeed_is_off_without_auto_promote_marker(target):
    state = target / ".autopilot" / "state"
    (state / "queue.json").write_text(json.dumps({"tasks": []}))
    assert _should_autofeed(state, auto_promote=False, slots=1) is False
