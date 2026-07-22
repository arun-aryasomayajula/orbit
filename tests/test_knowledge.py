# `orbit knowledge build` — ONE headless survey call (/orbit-knowledge), then
# deterministic post-validation of what the agent wrote under knowledge/.
# Every check here drives the real knowledge.py as a subprocess with
# ORBIT_KNOWLEDGE_CMD swapped for a fake agent script — no mocking of the
# thing under test. `status`/`refresh`/`autorefresh` (Task 4) are staleness
# over merge markers + git — see the fixtures below (`_build_kb`, `_commit_file`,
# `_write_markers`) reused across all three.
#
# Run: python3.11 -m pytest tests/test_knowledge.py -v   (from orbit/)
import base64
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "engine" / "knowledge.py"

VALID_INDEX = "# Fake Project\n\nA tiny fake project used by tests.\n\n" + "\n".join(
    f"line {i}" for i in range(10)
) + "\n\ngenerated: 2026-07-22 @ deadbeef\n"

VALID_ENDPOINTS = textwrap.dedent("""\
    ## routes.py

    | method + path | handler (file:symbol) | calls | tables read | tables written | auth? |
    |---|---|---|---|---|---|
    | GET /health | routes.py:health | ? | ? | ? | no |
    """)

NO_ROW_ENDPOINTS = textwrap.dedent("""\
    ## routes.py

    | method + path | handler (file:symbol) | calls | tables read | tables written | auth? |
    |---|---|---|---|---|---|
    """)

VALID_ER = textwrap.dedent("""\
    erDiagram
        USER ||--o{ ORDER : places
    %% evidence:
    %% USER -> migrations/0001_init.sql:3
    """)

BAD_ER = textwrap.dedent("""\
    graph TD
        A --> B
    """)

VALID_MINDMAP = textwrap.dedent("""\
    mindmap
      root((fake))
        Domain
          routes.py
    """)

VALID_FLOW = "# checkout flow\n\n1. POST /checkout -> routes.py:checkout\n"
VALID_CONVENTIONS = "## naming\n\n**STANDARD** snake_case (routes.py:1)\n"

ALL_DOCS = ["INDEX.md", "endpoints.md", "er.mmd", "mindmap.mmd",
            "flows/checkout.md", "conventions.md"]


def _target(tmp_path):
    """tmp_path fake target: .autopilot/{config.yaml,state/} + a real 1-commit git repo."""
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text(
        'repo: "."\nbase_branch: "main"\ngates:\n  test: {cmd: "true", cwd: "."}\n')
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("fake repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _head_sha(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _agent_script(tmp_path):
    """Fake survey agent: writes knowledge/ files + manifest.json from a JSON spec
    named by $FAKE_SPEC. `files`: {relpath: content} (text). `binary_files`:
    {relpath: base64} for content that must land as raw, possibly non-UTF8,
    bytes. `manifest`: dict, or omit the key entirely to skip writing
    manifest.json altogether.

    If $FAKE_CALL_LOG is set, also appends one JSON line per invocation —
    {"argv": sys.argv[1:], "skill": $ORBIT_KNOWLEDGE_SKILL} — so refresh tests
    can assert whether/how the agent was invoked without mocking knowledge.py
    itself."""
    script = tmp_path / "agent.py"
    script.write_text(textwrap.dedent("""\
        import base64, json, os, sys
        from pathlib import Path
        spec = json.loads(Path(os.environ["FAKE_SPEC"]).read_text())
        kdir = Path(os.environ["AP_HOME"]) / "knowledge"
        kdir.mkdir(parents=True, exist_ok=True)
        for rel, content in spec.get("files", {}).items():
            p = kdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        for rel, b64 in spec.get("binary_files", {}).items():
            p = kdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(b64))
        if "manifest" in spec:
            (kdir / "manifest.json").write_text(json.dumps(spec["manifest"]))
        log = os.environ.get("FAKE_CALL_LOG")
        if log:
            with open(log, "a") as f:
                f.write(json.dumps({"argv": sys.argv[1:],
                                     "skill": os.environ.get("ORBIT_KNOWLEDGE_SKILL")}) + "\\n")
        """))
    return f"{sys.executable} {script}"


NO_MANIFEST = object()   # sentinel: skip writing manifest.json at all (not "write it as null")


def _spec(tmp_path, name, files, manifest, binary_files=None):
    """Write the JSON spec the fake agent reads. Pass `manifest=NO_MANIFEST` to
    make the fake agent write every doc but never write manifest.json."""
    doc = {"files": files}
    if binary_files:
        doc["binary_files"] = binary_files
    if manifest is not NO_MANIFEST:
        doc["manifest"] = manifest
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return p


def _full_manifest(docs=None, base_sha="deadbeefcafe", extra=None):
    m = {
        "version": 1,
        "generated_at": "2026-07-22T00:00:00Z",
        "docs": docs if docs is not None else list(ALL_DOCS),
        "files": {"routes.py": ["endpoints.md"]},
    }
    if base_sha is not None:
        m["base_sha"] = base_sha
    if extra:
        m.update(extra)
    return m


def _full_files(endpoints=VALID_ENDPOINTS, er=VALID_ER, index=VALID_INDEX, mindmap=VALID_MINDMAP):
    return {
        "INDEX.md": index,
        "endpoints.md": endpoints,
        "er.mmd": er,
        "mindmap.mmd": mindmap,
        "flows/checkout.md": VALID_FLOW,
        "conventions.md": VALID_CONVENTIONS,
    }


def run(target, agent_cmd, spec_path, extra_args=()):
    env = dict(os.environ, ORBIT_KNOWLEDGE_CMD=agent_cmd, FAKE_SPEC=str(spec_path))
    return subprocess.run(
        [sys.executable, str(KNOWLEDGE), str(target), "build", *extra_args],
        capture_output=True, text=True, env=env)


# --- Task 4 fixtures: staleness over merge markers + git -------------------

def run_sub(target, subcmd, extra_args=(), env_extra=None):
    """Run knowledge.py <target> <subcmd> — for status/refresh/autorefresh,
    which (unlike build) don't always need ORBIT_KNOWLEDGE_CMD/FAKE_SPEC."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(KNOWLEDGE), str(target), subcmd, *extra_args],
        capture_output=True, text=True, env=env)


def _build_kb(tmp_path, t, generated_at, files_map):
    """Build a real, valid knowledge base under t/.autopilot/knowledge/ whose
    manifest has a specific generated_at + files map — the fixed point every
    staleness test measures merges against."""
    manifest = _full_manifest(extra={"generated_at": generated_at, "files": files_map})
    spec = _spec(tmp_path, f"spec-{abs(hash(generated_at))}.json", _full_files(), manifest)
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 0, r.stdout + r.stderr
    return t


def _commit_file(repo, relpath, content, message="change"):
    """A real commit touching `relpath` in the fixture's tiny git repo — returns
    its sha, reachable via `git show` exactly like a real merged ship."""
    p = repo / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", relpath], cwd=repo, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True)
    return _head_sha(repo)


def _write_markers(t, markers):
    (t / ".autopilot" / "state" / "merge_markers.jsonl").write_text(
        "".join(json.dumps(m) + "\n" for m in markers))


def _marker(task_id, at, sha="", branch="", pr_url="", patch=""):
    return {"task_id": task_id, "at": at, "sha": sha, "branch": branch,
            "pr_url": pr_url, "patch": patch}


def _configure_knowledge(t, refresh=None, stale_after_merges=None):
    lines = ['repo: "."\n', 'base_branch: "main"\n', 'gates:\n', '  test: {cmd: "true", cwd: "."}\n']
    if refresh is not None or stale_after_merges is not None:
        lines.append('knowledge:\n')
        if refresh is not None:
            lines.append(f'  refresh: "{refresh}"\n')
        if stale_after_merges is not None:
            lines.append(f'  stale_after_merges: {stale_after_merges}\n')
    (t / ".autopilot" / "config.yaml").write_text("".join(lines))


def test_build_succeeds_with_a_complete_valid_knowledge_dir(tmp_path):
    t = _target(tmp_path)
    spec = _spec(tmp_path, "spec.json", _full_files(), _full_manifest())
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 0, r.stdout + r.stderr
    for doc in ALL_DOCS:
        assert (t / ".autopilot" / "knowledge" / doc).exists()


def test_build_fails_when_manifest_missing(tmp_path):
    t = _target(tmp_path)
    spec = _spec(tmp_path, "spec.json", _full_files(), manifest=NO_MANIFEST)
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 1
    assert "manifest" in (r.stdout + r.stderr).lower()


def test_build_fails_when_manifest_lists_missing_doc(tmp_path):
    t = _target(tmp_path)
    docs = list(ALL_DOCS) + ["ghost.md"]
    spec = _spec(tmp_path, "spec.json", _full_files(), _full_manifest(docs=docs))
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 1
    assert "ghost.md" in (r.stdout + r.stderr)


def test_build_fails_when_er_mmd_wrong_first_line(tmp_path):
    t = _target(tmp_path)
    spec = _spec(tmp_path, "spec.json", _full_files(er=BAD_ER), _full_manifest())
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 1
    assert "er.mmd" in (r.stdout + r.stderr)


def test_build_fails_when_mindmap_mmd_wrong_first_line(tmp_path):
    t = _target(tmp_path)
    spec = _spec(tmp_path, "spec.json", _full_files(mindmap="graph TD\n  A --> B\n"), _full_manifest())
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 1
    assert "mindmap.mmd" in (r.stdout + r.stderr)


def test_build_rejects_absolute_path_in_manifest_docs(tmp_path):
    t = _target(tmp_path)
    docs = list(ALL_DOCS) + ["/etc/passwd"]
    spec = _spec(tmp_path, "spec.json", _full_files(), _full_manifest(docs=docs))
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 1
    assert "/etc/passwd" in (r.stdout + r.stderr)
    assert "VALIDATE" in r.stdout


def test_build_rejects_path_traversal_in_manifest_docs(tmp_path):
    t = _target(tmp_path)
    # a real file just outside knowledge/ — proves containment, not just
    # nonexistence, is what rejects this entry
    (t / ".autopilot" / "state").mkdir(parents=True, exist_ok=True)
    (t / ".autopilot" / "state" / "secret.txt").write_text("do not surface this")
    docs = list(ALL_DOCS) + ["../state/secret.txt"]
    spec = _spec(tmp_path, "spec.json", _full_files(), _full_manifest(docs=docs))
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 1
    assert "../state/secret.txt" in (r.stdout + r.stderr)
    assert "traversal" in (r.stdout + r.stderr) or "escapes" in (r.stdout + r.stderr)


def test_build_reports_clean_validate_line_for_unreadable_er_mmd(tmp_path):
    t = _target(tmp_path)
    files = _full_files()
    del files["er.mmd"]                              # written as raw bytes below instead
    bad_bytes = base64.b64encode(b"\xff\xfeInvalidUTF8\n").decode()
    spec = _spec(tmp_path, "spec.json", files, _full_manifest(),
                binary_files={"er.mmd": bad_bytes})
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 1
    assert "VALIDATE" in r.stdout
    assert "er.mmd" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr                # no raw traceback — a clean failure line


def test_build_fails_when_endpoints_has_no_data_rows(tmp_path):
    t = _target(tmp_path)
    spec = _spec(tmp_path, "spec.json", _full_files(endpoints=NO_ROW_ENDPOINTS), _full_manifest())
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 1
    assert "endpoints.md" in (r.stdout + r.stderr)


def test_build_stamps_missing_base_sha(tmp_path):
    t = _target(tmp_path)
    spec = _spec(tmp_path, "spec.json", _full_files(), _full_manifest(base_sha=None))
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 0, r.stdout + r.stderr
    manifest = json.loads((t / ".autopilot" / "knowledge" / "manifest.json").read_text())
    assert manifest["base_sha"] == _head_sha(t)


def test_build_fails_when_index_too_long(tmp_path):
    t = _target(tmp_path)
    long_index = "\n".join(f"line {i}" for i in range(85)) + "\n"
    spec = _spec(tmp_path, "spec.json", _full_files(index=long_index), _full_manifest())
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 1
    assert "INDEX.md" in (r.stdout + r.stderr)


def test_build_exits_2_without_autopilot(tmp_path):
    # no .autopilot/ at all — must fail before ever touching the agent hook
    r = subprocess.run([sys.executable, str(KNOWLEDGE), str(tmp_path), "build"],
                      capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 2


def test_unknown_subcommand_exits_2(tmp_path):
    t = _target(tmp_path)
    r = run_sub(t, "bogus")
    assert r.returncode == 2, r.stdout + r.stderr


def test_agent_failure_exits_1(tmp_path):
    t = _target(tmp_path)
    script = tmp_path / "fail_agent.py"
    script.write_text("import sys\nsys.exit(1)\n")
    r = run(t, f"{sys.executable} {script}", tmp_path / "unused-spec.json")
    assert r.returncode == 1


def test_no_gates_config_still_resolves_repo_for_base_sha(tmp_path):
    # sanity: repo defaults to "." (the target itself) — the fixture's git repo
    t = _target(tmp_path)
    spec = _spec(tmp_path, "spec.json", _full_files(), _full_manifest(base_sha=""))
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 0, r.stdout + r.stderr
    manifest = json.loads((t / ".autopilot" / "knowledge" / "manifest.json").read_text())
    assert manifest["base_sha"] == _head_sha(t)


# === Task 4: status ==========================================================

def test_status_no_manifest_exits_0(tmp_path):
    t = _target(tmp_path)
    r = run_sub(t, "status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no knowledge base" in (r.stdout + r.stderr).lower()


def test_status_marker_touching_exact_path_is_stale(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"]})
    sha = _commit_file(t, "routes.py", "print('changed')\n")
    _write_markers(t, [_marker("task-1", "2026-07-15T00:00:00Z", sha=sha)])

    r = run_sub(t, "status")
    assert r.returncode == 4, r.stdout + r.stderr
    assert "endpoints.md" in r.stdout

    staleness = json.loads((t / ".autopilot" / "state" / "knowledge-staleness.json").read_text())
    assert staleness["generated_at"] == "2026-07-10T00:00:00Z"
    assert staleness["stale_docs"] == ["endpoints.md"]
    assert staleness["merges_considered"] == 1
    assert "checked_at" in staleness


def test_status_dir_prefix_key_matches(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"migrations/": ["INDEX.md"]})
    sha = _commit_file(t, "migrations/0002_add.py", "op\n")
    _write_markers(t, [_marker("task-2", "2026-07-15T00:00:00Z", sha=sha)])

    r = run_sub(t, "status")
    assert r.returncode == 4, r.stdout + r.stderr
    assert "INDEX.md" in r.stdout


def test_status_glob_key_matches(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"src/*.py": ["conventions.md"]})
    sha = _commit_file(t, "src/app.py", "x = 1\n")
    _write_markers(t, [_marker("task-3", "2026-07-15T00:00:00Z", sha=sha)])

    r = run_sub(t, "status")
    assert r.returncode == 4, r.stdout + r.stderr
    assert "conventions.md" in r.stdout


def test_status_marker_older_than_generated_at_is_fresh(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"]})
    sha = _commit_file(t, "routes.py", "print('changed')\n")
    _write_markers(t, [_marker("task-4", "2026-07-01T00:00:00Z", sha=sha)])

    r = run_sub(t, "status")
    assert r.returncode == 0, r.stdout + r.stderr
    staleness = json.loads((t / ".autopilot" / "state" / "knowledge-staleness.json").read_text())
    assert staleness["stale_docs"] == []
    assert staleness["merges_considered"] == 0


def test_status_unreachable_sha_falls_back_to_patch_file(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"src/*.py": ["conventions.md"]})
    diffs = t / ".autopilot" / "state" / "diffs"
    diffs.mkdir(parents=True)
    patch = diffs / "cycle-x.patch"
    patch.write_text(
        "diff --git a/src/app.py b/src/app.py\n"
        "index 111..222 100644\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-x = 0\n"
        "+x = 1\n"
    )
    _write_markers(t, [_marker("task-5", "2026-07-15T00:00:00Z",
                               sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                               patch=str(patch))])

    r = run_sub(t, "status")
    assert r.returncode == 4, r.stdout + r.stderr
    assert "conventions.md" in r.stdout


def test_status_unreachable_sha_and_no_patch_skips_with_warning_and_fresh(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"src/*.py": ["conventions.md"]})
    _write_markers(t, [_marker("task-6", "2026-07-15T00:00:00Z",
                               sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                               patch="")])

    r = run_sub(t, "status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" in r.stdout
    staleness = json.loads((t / ".autopilot" / "state" / "knowledge-staleness.json").read_text())
    assert staleness["stale_docs"] == []


# === Task 4: refresh ==========================================================

def test_refresh_fresh_skips_agent(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"]})
    call_log = tmp_path / "calls.jsonl"

    r = run_sub(t, "refresh", env_extra={
        "ORBIT_KNOWLEDGE_CMD": _agent_script(tmp_path),
        "FAKE_SPEC": str(tmp_path / "unused-spec.json"),
        "FAKE_CALL_LOG": str(call_log),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fresh" in r.stdout.lower()
    assert not call_log.exists()


def test_refresh_stale_invokes_agent_with_refresh_skill_and_writes_scope(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"],
                                                    "src/*.py": ["conventions.md"]})
    sha = _commit_file(t, "routes.py", "print('changed')\n")
    _write_markers(t, [_marker("task-7", "2026-07-15T00:00:00Z", sha=sha)])

    call_log = tmp_path / "calls.jsonl"
    refresh_spec = _spec(tmp_path, "refresh-spec.json", _full_files(), _full_manifest())
    r = run_sub(t, "refresh", env_extra={
        "ORBIT_KNOWLEDGE_CMD": _agent_script(tmp_path),
        "FAKE_SPEC": str(refresh_spec),
        "FAKE_CALL_LOG": str(call_log),
    })
    assert r.returncode == 0, r.stdout + r.stderr

    scope = json.loads((t / ".autopilot" / "state" / "knowledge" / "refresh-scope.json").read_text())
    assert scope["stale_docs"] == ["endpoints.md"]
    assert scope["changed_files"] == ["routes.py"]

    calls = [json.loads(ln) for ln in call_log.read_text().splitlines()]
    assert len(calls) == 1
    assert "refresh" in calls[0]["argv"] or calls[0]["skill"] == "refresh"


def test_refresh_stale_agent_failure_exits_1(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"]})
    sha = _commit_file(t, "routes.py", "print('changed')\n")
    _write_markers(t, [_marker("task-8", "2026-07-15T00:00:00Z", sha=sha)])

    script = tmp_path / "fail_agent.py"
    script.write_text("import sys\nsys.exit(1)\n")
    r = run_sub(t, "refresh", env_extra={"ORBIT_KNOWLEDGE_CMD": f"{sys.executable} {script}"})
    assert r.returncode == 1


def test_refresh_stale_post_validation_still_enforced(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"]})
    sha = _commit_file(t, "routes.py", "print('changed')\n")
    _write_markers(t, [_marker("task-9", "2026-07-15T00:00:00Z", sha=sha)])

    # the agent writes an invalid knowledge base this time (manifest lists a
    # doc that was never written) — refresh must fail exactly like build would
    broken_spec = _spec(tmp_path, "broken-spec.json", _full_files(),
                        _full_manifest(docs=list(ALL_DOCS) + ["ghost.md"]))
    r = run_sub(t, "refresh", env_extra={
        "ORBIT_KNOWLEDGE_CMD": _agent_script(tmp_path),
        "FAKE_SPEC": str(broken_spec),
    })
    assert r.returncode == 1
    assert "ghost.md" in (r.stdout + r.stderr)


def test_refresh_no_manifest_exits_0_without_agent(tmp_path):
    t = _target(tmp_path)
    call_log = tmp_path / "calls.jsonl"
    r = run_sub(t, "refresh", env_extra={
        "ORBIT_KNOWLEDGE_CMD": _agent_script(tmp_path),
        "FAKE_SPEC": str(tmp_path / "unused-spec.json"),
        "FAKE_CALL_LOG": str(call_log),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert not call_log.exists()


# === Task 4: autorefresh ======================================================

def test_autorefresh_manual_mode_is_noop(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"]})
    _configure_knowledge(t, refresh="manual", stale_after_merges=1)
    sha = _commit_file(t, "routes.py", "print('changed')\n")
    _write_markers(t, [_marker("task-10", "2026-07-15T00:00:00Z", sha=sha)])

    call_log = tmp_path / "calls.jsonl"
    r = run_sub(t, "autorefresh", env_extra={
        "ORBIT_KNOWLEDGE_CMD": _agent_script(tmp_path),
        "FAKE_SPEC": str(tmp_path / "unused-spec.json"),
        "FAKE_CALL_LOG": str(call_log),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert not call_log.exists()


def test_autorefresh_absent_knowledge_block_is_noop(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"]})
    sha = _commit_file(t, "routes.py", "print('changed')\n")
    _write_markers(t, [_marker("task-11", "2026-07-15T00:00:00Z", sha=sha)])

    call_log = tmp_path / "calls.jsonl"
    r = run_sub(t, "autorefresh", env_extra={
        "ORBIT_KNOWLEDGE_CMD": _agent_script(tmp_path),
        "FAKE_SPEC": str(tmp_path / "unused-spec.json"),
        "FAKE_CALL_LOG": str(call_log),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert not call_log.exists()


def test_autorefresh_below_threshold_is_noop(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"]})
    _configure_knowledge(t, refresh="auto", stale_after_merges=2)
    sha = _commit_file(t, "routes.py", "print('changed')\n")
    _write_markers(t, [_marker("task-12", "2026-07-15T00:00:00Z", sha=sha)])  # 1 stale doc < 2

    call_log = tmp_path / "calls.jsonl"
    r = run_sub(t, "autorefresh", env_extra={
        "ORBIT_KNOWLEDGE_CMD": _agent_script(tmp_path),
        "FAKE_SPEC": str(tmp_path / "unused-spec.json"),
        "FAKE_CALL_LOG": str(call_log),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert not call_log.exists()


def test_autorefresh_at_threshold_invokes_refresh_and_touches_marker(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"]})
    _configure_knowledge(t, refresh="auto", stale_after_merges=1)
    sha = _commit_file(t, "routes.py", "print('changed')\n")
    _write_markers(t, [_marker("task-13", "2026-07-15T00:00:00Z", sha=sha)])

    call_log = tmp_path / "calls.jsonl"
    refresh_spec = _spec(tmp_path, "refresh-spec.json", _full_files(), _full_manifest())
    r = run_sub(t, "autorefresh", env_extra={
        "ORBIT_KNOWLEDGE_CMD": _agent_script(tmp_path),
        "FAKE_SPEC": str(refresh_spec),
        "FAKE_CALL_LOG": str(call_log),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert call_log.exists() and len(call_log.read_text().splitlines()) == 1
    marker = t / ".autopilot" / "state" / ".knowledge-refreshed"
    assert marker.exists()


def test_autorefresh_recent_marker_skips_even_if_stale(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"]})
    _configure_knowledge(t, refresh="auto", stale_after_merges=1)
    sha = _commit_file(t, "routes.py", "print('changed')\n")
    _write_markers(t, [_marker("task-14", "2026-07-15T00:00:00Z", sha=sha)])
    marker = t / ".autopilot" / "state" / ".knowledge-refreshed"
    marker.write_text("2026-07-21T00:00:00Z\n")   # < 24h old (checked against wall-clock mtime)

    call_log = tmp_path / "calls.jsonl"
    r = run_sub(t, "autorefresh", env_extra={
        "ORBIT_KNOWLEDGE_CMD": _agent_script(tmp_path),
        "FAKE_SPEC": str(tmp_path / "unused-spec.json"),
        "FAKE_CALL_LOG": str(call_log),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert not call_log.exists()


def test_autorefresh_stale_marker_over_24h_still_acts(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"]})
    _configure_knowledge(t, refresh="auto", stale_after_merges=1)
    sha = _commit_file(t, "routes.py", "print('changed')\n")
    _write_markers(t, [_marker("task-15", "2026-07-15T00:00:00Z", sha=sha)])
    marker = t / ".autopilot" / "state" / ".knowledge-refreshed"
    marker.write_text("old\n")
    old = time.time() - 25 * 3600   # well over 24h before "now"
    os.utime(marker, (old, old))

    call_log = tmp_path / "calls.jsonl"
    refresh_spec = _spec(tmp_path, "refresh-spec.json", _full_files(), _full_manifest())
    r = run_sub(t, "autorefresh", env_extra={
        "ORBIT_KNOWLEDGE_CMD": _agent_script(tmp_path),
        "FAKE_SPEC": str(refresh_spec),
        "FAKE_CALL_LOG": str(call_log),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert call_log.exists() and len(call_log.read_text().splitlines()) == 1
