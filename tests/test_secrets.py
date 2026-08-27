"""Tests for .autopilot/.secrets.env parsing, scaffold, and doctor awareness."""
import os
import shlex
import subprocess
import stat
import sys
import textwrap
from pathlib import Path

import pytest

ORBIT = Path(__file__).resolve().parents[1]


def _init_product(tmp_path):
    """Scaffold a product workspace."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    r = subprocess.run([sys.executable, str(ORBIT / "install" / "init.py"),
                        str(tmp_path), str(ORBIT), "--product"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    return r


def _secrets_env_module():
    sys.path.insert(0, str(ORBIT / "engine"))
    import secrets_env
    return secrets_env


def test_shared_parser_reads_the_documented_format(tmp_path):
    """One parser, four callers (run.sh's loop, the dashboard panel, doctor,
    prd_flow's stage children). Three Python copies had already drifted."""
    se = _secrets_env_module()
    ap = tmp_path / ".autopilot"
    ap.mkdir()
    (ap / ".secrets.env").write_text(
        "# a comment\n"
        "\n"
        "ANALYTICS_TOKEN=tok123\n"
        "  SPACED_KEY = spaced value \n"
        "WITH_EQUALS=a=b=c\n"
        "no-equals-line\n")
    got = se.read(str(tmp_path))
    assert got["ANALYTICS_TOKEN"] == "tok123"
    assert got["SPACED_KEY"] == " spaced value"   # key stripped, value from the first '='
    assert got["WITH_EQUALS"] == "a=b=c"          # split on the FIRST '=' only
    assert "no-equals-line" not in got
    assert "# a comment" not in got


def test_shared_parser_treats_a_missing_file_as_no_secrets(tmp_path):
    se = _secrets_env_module()
    assert se.read(str(tmp_path)) == {}


def test_doctor_delegates_to_the_shared_parser():
    """The wrapper must stay thin - a re-implementation is how the three Python
    copies of this parser drifted the first time."""
    doctor = (ORBIT / "engine" / "doctor.py").read_text()
    body = doctor.split("def _secrets_env(target):", 1)[1].split("\nprint(", 1)[0]
    assert "secrets_env.read(target)" in body
    assert "split(" not in body, "doctor re-implements the parse instead of delegating"


def test_doctor_quiet_when_token_env_in_secrets_file(tmp_path):
    """Doctor should not warn if ANALYTICS_TOKEN is in .secrets.env."""
    _init_product(tmp_path)
    cfg = tmp_path / ".autopilot" / "config.yaml"
    cfg.write_text(cfg.read_text() + '\nmcp:\n  analytics:\n    transport: http\n'
                   '    url: "https://m/mcp"\n    token_env: ANALYTICS_TOKEN\n    tools: "*"\n')

    # Create .secrets.env with ANALYTICS_TOKEN
    secrets = tmp_path / ".autopilot" / ".secrets.env"
    secrets.write_text("# My secrets\nANALYTICS_TOKEN=test-token-value\n")

    env = dict(os.environ, ORBIT_HOME=str(ORBIT))
    env.pop("ANALYTICS_TOKEN", None)  # Remove from shell environment

    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)

    # Doctor should NOT warn about the missing token env (it's in the file)
    assert "ANALYTICS_TOKEN" not in r.stdout, f"doctor should not warn when token is in .secrets.env: {r.stdout}"
    assert "ALL CHECKS PASSED" in r.stdout, f"doctor should pass: {r.stdout}"


def test_doctor_warns_on_loose_secrets_permissions(tmp_path):
    """Doctor should warn if .secrets.env exists with group/other bits readable."""
    _init_product(tmp_path)
    secrets = tmp_path / ".autopilot" / ".secrets.env"
    env = dict(os.environ, ORBIT_HOME=str(ORBIT))

    # Test 1: 0644 (group-readable) should warn
    secrets.write_text("ANALYTICS_TOKEN=test-token-value\n")
    os.chmod(str(secrets), 0o644)
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)
    assert ".secrets.env" in r.stdout and "mode" in r.stdout.lower(), \
        f"doctor should warn about 0644 permissions: {r.stdout}"

    # Test 2: 0400 (owner read-only, stricter) should NOT warn
    os.chmod(str(secrets), 0o400)
    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)
    assert ".secrets.env" not in r.stdout or "mode" not in r.stdout.lower(), \
        f"doctor should not warn about 0400 permissions: {r.stdout}"
    assert "ALL CHECKS PASSED" in r.stdout, \
        f"doctor should pass with 0400 permissions: {r.stdout}"


def test_product_scaffold_gitignore_covers_secrets_env(tmp_path):
    """Product scaffold should extend .gitignore to include .secrets.env."""
    _init_product(tmp_path)

    gitignore = tmp_path / ".autopilot" / ".gitignore"
    assert gitignore.exists(), ".gitignore should exist"
    content = gitignore.read_text()
    assert ".secrets.env" in content, ".gitignore should include .secrets.env"


def test_product_scaffold_creates_secrets_env_example(tmp_path):
    """Product scaffold should create .secrets.env.example."""
    _init_product(tmp_path)

    example = tmp_path / ".autopilot" / ".secrets.env.example"
    assert example.exists(), ".secrets.env.example should exist"

    content = example.read_text()
    assert "ANALYTICS_TOKEN" in content, ".secrets.env.example should mention ANALYTICS_TOKEN"
    assert content.startswith("#"), ".secrets.env.example should be all comments (example)"


def test_run_sh_syntax_is_valid(tmp_path):
    """run.sh should have valid bash syntax (bash -n)."""
    r = subprocess.run(["bash", "-n", str(ORBIT / "engine" / "run.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"run.sh has syntax errors: {r.stderr}"


def _extract_secrets_parse_loop():
    """Pull run.sh's secrets loop out of run.sh itself rather than keeping a
    second copy here - a copy silently stops testing the shipped loop the
    first time one of them changes."""
    text = (ORBIT / "engine" / "run.sh").read_text()
    start = "# --- secrets parse loop (extracted verbatim by tests/test_secrets.py) ---\n"
    end = "# --- end secrets parse loop ---"
    assert start in text and end in text, "run.sh's secrets parse loop markers moved"
    body = text.split(start, 1)[1].split(end, 1)[0]
    return "\n" + textwrap.dedent(body)


_SECRETS_PARSE_LOOP = _extract_secrets_parse_loop()


def test_run_sh_secrets_parse_snippet_syntax_valid(tmp_path):
    """run.sh no longer sources .secrets.env (that would execute its content
    as shell) — it parses it line-by-line via this loop. Verify the loop
    itself is syntactically valid standalone bash."""
    snippet = 'AP_HOME=".autopilot"\n' + _SECRETS_PARSE_LOOP
    r = subprocess.run(["bash", "-n", "-c", snippet],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"snippet has syntax errors: {r.stderr}"


def test_run_sh_no_longer_sources_secrets_env(tmp_path):
    """Regression guard for the shell-injection fix: run.sh must never contain
    a `. "$AP_HOME/.secrets.env"` (or `source`) line — sourcing executes file
    content as shell, which is exactly what a hand-edited or maliciously
    written secret value could exploit."""
    text = (ORBIT / "engine" / "run.sh").read_text()
    assert '. "$AP_HOME/.secrets.env"' not in text
    assert "source \"$AP_HOME/.secrets.env\"" not in text


def test_secrets_parser_never_executes_shell_metacharacters(tmp_path):
    """The loop's secrets loader parses KEY=VALUE lines; it must never
    source/execute the file. A hand-edited line whose value looks like a
    command substitution must land in the environment as a LITERAL string,
    and the embedded command must never actually run."""
    marker = tmp_path / "pwned"
    secrets = tmp_path / ".secrets.env"
    secrets.write_text(f"EVIL=$(touch {marker})\nGOOD=abc-123.def\n")

    script = f'AP_HOME={shlex.quote(str(tmp_path))}\n' + _SECRETS_PARSE_LOOP + \
        '\necho "EVIL_VALUE=[$EVIL]"\necho "GOOD_VALUE=[$GOOD]"\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    assert f"EVIL_VALUE=[$(touch {marker})]" in r.stdout, r.stdout
    assert "GOOD_VALUE=[abc-123.def]" in r.stdout, r.stdout
    assert not marker.exists(), (
        "the secrets parser executed file content instead of treating it as a literal value"
    )


def test_secrets_parser_never_executes_under_bash_3_2(tmp_path):
    """Same as above but pinned to /bin/bash (macOS ships bash 3.2 there) —
    run.sh's `set -uo pipefail` + `read -r ... || [ -n ... ]` construct must
    behave identically on the old bash most macOS machines still default to."""
    if not os.path.exists("/bin/bash"):
        pytest.skip("/bin/bash not present on this system")
    marker = tmp_path / "pwned"
    secrets = tmp_path / ".secrets.env"
    secrets.write_text(f"EVIL=$(touch {marker})\n")

    script = f'AP_HOME={shlex.quote(str(tmp_path))}\n' + _SECRETS_PARSE_LOOP + \
        '\necho "EVIL_VALUE=[$EVIL]"\n'
    r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert f"EVIL_VALUE=[$(touch {marker})]" in r.stdout, r.stdout
    assert not marker.exists()


# Names whose VALUE is a code path: exporting one out of .secrets.env would
# redirect every interpreter the loop shells out to. The dashboard's writer
# refuses them (command_center._SECRETS_NAME_DENY), so a line carrying one can
# only come from a hand-edited file - the loop must drop it anyway.
_RESERVED_LINES = ["PATH=/tmp/evil", "PYTHONPATH=/tmp/evil", "NODE_OPTIONS=--require=/tmp/evil.js",
                   "NODE_PATH=/tmp/evil", "PERL5LIB=/tmp/evil", "RUBYOPT=-r/tmp/evil",
                   "GEM_PATH=/tmp/evil", "CLASSPATH=/tmp/evil.jar", "LD_PRELOAD=/tmp/evil.so",
                   "DYLD_INSERT_LIBRARIES=/tmp/evil.dylib", "BASH_ENV=/tmp/evil.sh"]


@pytest.mark.parametrize("line", _RESERVED_LINES)
def test_secrets_parser_never_exports_a_reserved_name(tmp_path, line):
    """A hand-written reserved-name line in .secrets.env must NOT be exported.
    The value needs no shell metacharacters to be an attack: PATH=/tmp/evil
    alone hijacks every bare python3/git/gh call the loop makes."""
    name, _, value = line.partition("=")
    secrets = tmp_path / ".secrets.env"
    secrets.write_text(f"{line}\nGOOD=abc-123.def\n")

    script = (f'AP_HOME={shlex.quote(str(tmp_path))}\n'
              f'{name}=sentinel-original\n' + _SECRETS_PARSE_LOOP +
              f'\necho "NAME_VALUE=[${name}]"\necho "GOOD_VALUE=[$GOOD]"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "NAME_VALUE=[sentinel-original]" in r.stdout, (
        f"{name} was overwritten from .secrets.env: {r.stdout}")
    assert value not in r.stdout, r.stdout
    # The guard is surgical: the legitimate secret on the next line still lands.
    assert "GOOD_VALUE=[abc-123.def]" in r.stdout, r.stdout


def test_secrets_are_loaded_per_iteration_not_once_at_startup():
    """A token saved in the dashboard's Secrets panel must reach the NEXT
    iteration. Parsing .secrets.env at top-of-file meant waiting for a service
    restart, so the loop lives inside one_iteration() now - before the cycle
    runs, after the gate preflight."""
    text = (ORBIT / "engine" / "run.sh").read_text()
    marker = "# --- secrets parse loop (extracted verbatim by tests/test_secrets.py) ---"
    body = text.split("one_iteration() {", 1)
    assert len(body) == 2, "one_iteration() is gone from run.sh"
    assert marker not in body[0], "the secrets loop is back at top-of-file (loaded once per process)"
    iteration = body[1]
    assert marker in iteration
    assert iteration.index(marker) < iteration.index("prepare_worktree"), \
        "secrets must load before the cycle runs, not after"


def test_doctor_warns_publish_configured_without_atlassian_secrets(tmp_path):
    """Doctor should warn if publish is configured but ATLASSIAN_* secrets are missing."""
    _init_product(tmp_path)
    cfg = tmp_path / ".autopilot" / "config.yaml"
    cfg.write_text(cfg.read_text() + '\npublish:\n  confluence:\n    base_url: "https://wiki.example.com"\n'
                   '    space: "PROD"\n  jira:\n    base_url: "https://jira.example.com"\n'
                   '    project: "ORB"\n    issue_type: "Task"\n')

    env = dict(os.environ, ORBIT_HOME=str(ORBIT))
    env.pop("ATLASSIAN_EMAIL", None)
    env.pop("ATLASSIAN_API_TOKEN", None)

    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)

    assert "publish:" in r.stdout and ("ATLASSIAN_EMAIL" in r.stdout or "ATLASSIAN_API_TOKEN" in r.stdout), \
        f"doctor should warn about missing Atlassian secrets: {r.stdout}"


def test_doctor_quiet_publish_configured_with_atlassian_secrets_in_env(tmp_path):
    """Doctor should not warn if publish is configured and ATLASSIAN_* secrets are in the environment."""
    _init_product(tmp_path)
    cfg = tmp_path / ".autopilot" / "config.yaml"
    cfg.write_text(cfg.read_text() + '\npublish:\n  confluence:\n    base_url: "https://wiki.example.com"\n'
                   '    space: "PROD"\n  jira:\n    base_url: "https://jira.example.com"\n'
                   '    project: "ORB"\n    issue_type: "Task"\n')

    env = dict(os.environ, ORBIT_HOME=str(ORBIT), ATLASSIAN_EMAIL="pm@example.com",
               ATLASSIAN_API_TOKEN="tok123")

    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)

    assert "publish:" not in r.stdout or ("ATLASSIAN_EMAIL" not in r.stdout and "ATLASSIAN_API_TOKEN" not in r.stdout), \
        f"doctor should not warn when Atlassian secrets are in environment: {r.stdout}"
    assert "ALL CHECKS PASSED" in r.stdout, f"doctor should pass: {r.stdout}"


def test_doctor_quiet_publish_configured_with_atlassian_secrets_in_file(tmp_path):
    """Doctor should not warn if publish is configured and ATLASSIAN_* secrets are in .secrets.env."""
    _init_product(tmp_path)
    cfg = tmp_path / ".autopilot" / "config.yaml"
    cfg.write_text(cfg.read_text() + '\npublish:\n  confluence:\n    base_url: "https://wiki.example.com"\n'
                   '    space: "PROD"\n  jira:\n    base_url: "https://jira.example.com"\n'
                   '    project: "ORB"\n    issue_type: "Task"\n')

    secrets = tmp_path / ".autopilot" / ".secrets.env"
    secrets.write_text("ATLASSIAN_EMAIL=pm@example.com\nATLASSIAN_API_TOKEN=tok123\n")

    env = dict(os.environ, ORBIT_HOME=str(ORBIT))
    env.pop("ATLASSIAN_EMAIL", None)
    env.pop("ATLASSIAN_API_TOKEN", None)

    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)

    assert "publish:" not in r.stdout or ("ATLASSIAN_EMAIL" not in r.stdout and "ATLASSIAN_API_TOKEN" not in r.stdout), \
        f"doctor should not warn when Atlassian secrets are in .secrets.env: {r.stdout}"
    assert "ALL CHECKS PASSED" in r.stdout, f"doctor should pass: {r.stdout}"


def test_doctor_informational_atlassian_secrets_without_publish(tmp_path):
    """Doctor should show informational note if ATLASSIAN_* secrets are present but publish unconfigured."""
    _init_product(tmp_path)

    secrets = tmp_path / ".autopilot" / ".secrets.env"
    secrets.write_text("ATLASSIAN_EMAIL=pm@example.com\nATLASSIAN_API_TOKEN=tok123\n")

    env = dict(os.environ, ORBIT_HOME=str(ORBIT))

    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)

    assert "Atlassian secrets are set but publish:" in r.stdout and "unconfigured" in r.stdout, \
        f"doctor should show informational note about unused Atlassian secrets: {r.stdout}"


def test_doctor_warns_publish_configured_with_blank_atlassian_secrets_in_file(tmp_path):
    """A key present in .secrets.env but set to an empty value is 'absent' to
    publish.py's own _auth() (`(env.get(v) or "").strip()`), which treats a
    blank the same as a missing var - doctor's advisory must agree, not pass
    a bare 'is the key present' membership test that would tell an operator
    everything is fine right up until the first real publish attempt."""
    _init_product(tmp_path)
    cfg = tmp_path / ".autopilot" / "config.yaml"
    cfg.write_text(cfg.read_text() + '\npublish:\n  confluence:\n    base_url: "https://wiki.example.com"\n'
                   '    space: "PROD"\n  jira:\n    base_url: "https://jira.example.com"\n'
                   '    project: "ORB"\n    issue_type: "Task"\n')

    secrets = tmp_path / ".autopilot" / ".secrets.env"
    secrets.write_text("ATLASSIAN_EMAIL=\nATLASSIAN_API_TOKEN=   \n")

    env = dict(os.environ, ORBIT_HOME=str(ORBIT))
    env.pop("ATLASSIAN_EMAIL", None)
    env.pop("ATLASSIAN_API_TOKEN", None)

    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)

    assert "publish:" in r.stdout and ("ATLASSIAN_EMAIL" in r.stdout or "ATLASSIAN_API_TOKEN" in r.stdout), \
        f"doctor should warn about blank Atlassian secrets: {r.stdout}"


def test_doctor_quiet_on_blank_env_var_when_secrets_file_has_a_real_value(tmp_path):
    """A blank exported shell variable must not shadow a real value sitting
    in .secrets.env - same non-blank-wins-if-present precedence publish.py's
    _auth() gets from dict.update() (secrets file overwrites the environment
    key), just checked independently here rather than assumed."""
    _init_product(tmp_path)
    cfg = tmp_path / ".autopilot" / "config.yaml"
    cfg.write_text(cfg.read_text() + '\npublish:\n  confluence:\n    base_url: "https://wiki.example.com"\n'
                   '    space: "PROD"\n  jira:\n    base_url: "https://jira.example.com"\n'
                   '    project: "ORB"\n    issue_type: "Task"\n')

    secrets = tmp_path / ".autopilot" / ".secrets.env"
    secrets.write_text("ATLASSIAN_EMAIL=pm@example.com\nATLASSIAN_API_TOKEN=tok123\n")

    env = dict(os.environ, ORBIT_HOME=str(ORBIT), ATLASSIAN_EMAIL="", ATLASSIAN_API_TOKEN="")

    r = subprocess.run([sys.executable, str(ORBIT / "engine" / "doctor.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env)

    assert "publish:" not in r.stdout or ("ATLASSIAN_EMAIL" not in r.stdout and "ATLASSIAN_API_TOKEN" not in r.stdout), \
        f"doctor should not warn when a real value is in .secrets.env, even with a blank env var: {r.stdout}"
    assert "ALL CHECKS PASSED" in r.stdout, f"doctor should pass: {r.stdout}"
