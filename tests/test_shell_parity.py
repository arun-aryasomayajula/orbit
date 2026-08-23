# Every POST endpoint the dashboard server exposes must be reachable from the
# served UI (engine/cc_shell.html). The UI once forked — features landed in a
# dead inline template inside command_center.py and were invisible for weeks —
# so this pins two invariants: (1) cc_shell.html references every operator
# endpoint, (2) the inline-shell fallback never comes back.
#
# Run: python3 -m pytest tests/test_shell_parity.py -v   (from orbit/)
import re
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"
PY = (ENGINE / "command_center.py").read_text()
SHELL_PATH = ENGINE / "cc_shell.html"


def _post_paths():
    post_src = PY.split("def do_POST", 1)[1]
    return sorted(set(re.findall(r'path == "(/[^"]+)"', post_src)))


def test_shell_file_exists():
    assert SHELL_PATH.exists(), "cc_shell.html is the only shell — it must ship with the engine"


def test_every_post_endpoint_is_wired_into_the_served_shell():
    shell = SHELL_PATH.read_text()
    paths = _post_paths()
    assert paths, "no POST endpoints parsed — the do_POST anchor moved; fix the parser, not the assert"
    missing = [p for p in paths if p not in shell]
    assert not missing, (
        f"POST endpoints with no UI reference in cc_shell.html: {missing} — "
        "a feature was added to the server without a button/call in the served shell"
    )


def test_inline_shell_fork_is_gone():
    assert 'SHELL = r"""' not in PY, (
        "an inline SHELL template is back in command_center.py — the UI lives ONLY "
        "in cc_shell.html; a second copy WILL silently diverge again"
    )


# ── F9: Start stays reachable as a TOP-UP, disabled only at full ─────────────
# Both shells used to disable Start whenever the loop was "running" at all, so
# at concurrency > 1 a crashed slot could only be brought back by stopping the
# slots that were still working. The launcher already refuses to double-start a
# live slot and starts the free ones, so the button must match that.

LAUNCHER_SHELL = (ENGINE / "launcher_shell.html").read_text()
CC_SHELL = SHELL_PATH.read_text()


def test_both_shells_gate_start_on_full_not_on_running():
    for name, shell in (("launcher_shell.html", LAUNCHER_SHELL), ("cc_shell.html", CC_SHELL)):
        assert "loop_tracked >= p.loop_slots" in shell or "loop_tracked>=p.loop_slots" in shell, name
        # the old, wrong condition must be gone from the Start button
        assert "${p.loop_running ? 'disabled' : ''}" not in shell, name
        assert "${p.loop_running?'disabled':''}" not in shell, name


# ── Task 9: polish batch + parked-minors sweep ───────────────────────────────

def test_abort_and_skip_uses_escjsattr_not_esc_for_the_task_id():
    """The M4-deferred consistency fix: a task id interpolated into a
    single-quoted JS string literal inside an onclick="..." attribute needs
    escJsAttr's defense-in-depth (a bare ' or " in the id could otherwise
    break out of the string), the same as the hero's Abort cycle button
    already did. esc() alone only neutralizes HTML, not the JS string layer."""
    i = CC_SHELL.find("Abort &amp; skip</button>")
    assert i != -1, "Abort & skip button not found (markup moved?) — update this test's pattern"
    line = CC_SHELL.rfind("btns=`<button", 0, i)
    assert line != -1
    snippet = CC_SHELL[line:i]
    assert "escJsAttr(t.id)" in snippet
    assert "esc(t.id)" not in snippet.replace("escJsAttr(t.id)", "")


def test_watch_rows_are_addressed_by_a_stable_id_not_array_index():
    """Row operations (Remove, oninput/onchange mutation) must target a row
    by an id assigned once at creation, not by its position in the array at
    render time — an index-based handler baked into the HTML string would
    silently target the wrong row the instant a row above it is added or
    removed. `data-i="<index>"` must be gone in favor of `data-id="<id>"`."""
    assert 'data-id="${id}"' in CC_SHELL or "data-id=\"${id}\"" in CC_SHELL
    assert 'data-i="${i}"' not in CC_SHELL
    assert "removeWatchRow(${id})" in CC_SHELL
    assert "_watchRowById(" in CC_SHELL


def test_workers_strip_lives_beside_the_queue_boards_live_indicator():
    """The workers strip (per-slot concurrency liveness) moved off the QA
    Dev-session band, which has nothing to do with it, to sit beside the
    Queue view's "In orbit" column header — the board's live-count
    indicator — while still self-hiding exactly as before (renderWorkers)."""
    qa_dev_band = re.search(r'id="qaDevBand".*?</div>\s*</div>', CC_SHELL, re.S)
    assert qa_dev_band, "qaDevBand markup not found — update this test's pattern"
    assert "workersStrip" not in qa_dev_band.group(0)
    queue_section = CC_SHELL.split("function renderQueue()", 1)[1].split("\n}\n", 1)[0]
    assert "workersStrip" in queue_section
    assert "col('In orbit'" in queue_section


def test_hero_status_goes_slot_aware_when_workers_are_live():
    """With live per-slot workers, the hero/status line names how many
    cycles are live (+ their tids) instead of a single global current_task —
    accurate at concurrency > 1, where more than one task can be in flight
    at once. Idle rendering (idleWhy) is untouched by this."""
    assert "function liveWorkers(r)" in CC_SHELL
    assert "cycle${lw.length===1?'':'s'} live" in CC_SHELL
    assert "function tidsCompact(ws)" in CC_SHELL
