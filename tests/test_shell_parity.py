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
