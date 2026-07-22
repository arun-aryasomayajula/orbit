# `orbit knowledge build` — ONE headless survey call (/orbit-knowledge), then
# deterministic post-validation of what the agent wrote under knowledge/.
# Every check here drives the real knowledge.py as a subprocess with
# ORBIT_KNOWLEDGE_CMD swapped for a fake agent script — no mocking of the
# thing under test. `refresh|status|autorefresh` are Task 4's; they must only
# be recognized-but-unimplemented here.
#
# Run: python3.11 -m pytest tests/test_knowledge.py -v   (from orbit/)
import json
import os
import subprocess
import sys
import textwrap
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
    named by $FAKE_SPEC. `files`: {relpath: content}. `manifest`: dict, or omit the
    key entirely to skip writing manifest.json altogether."""
    script = tmp_path / "agent.py"
    script.write_text(textwrap.dedent("""\
        import json, os
        from pathlib import Path
        spec = json.loads(Path(os.environ["FAKE_SPEC"]).read_text())
        kdir = Path(os.environ["AP_HOME"]) / "knowledge"
        kdir.mkdir(parents=True, exist_ok=True)
        for rel, content in spec.get("files", {}).items():
            p = kdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        if "manifest" in spec:
            (kdir / "manifest.json").write_text(json.dumps(spec["manifest"]))
        """))
    return f"{sys.executable} {script}"


NO_MANIFEST = object()   # sentinel: skip writing manifest.json at all (not "write it as null")


def _spec(tmp_path, name, files, manifest):
    """Write the JSON spec the fake agent reads. Pass `manifest=NO_MANIFEST` to
    make the fake agent write every doc but never write manifest.json."""
    doc = {"files": files}
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


def _full_files(endpoints=VALID_ENDPOINTS, er=VALID_ER, index=VALID_INDEX):
    return {
        "INDEX.md": index,
        "endpoints.md": endpoints,
        "er.mmd": er,
        "mindmap.mmd": VALID_MINDMAP,
        "flows/checkout.md": VALID_FLOW,
        "conventions.md": VALID_CONVENTIONS,
    }


def run(target, agent_cmd, spec_path, extra_args=()):
    env = dict(os.environ, ORBIT_KNOWLEDGE_CMD=agent_cmd, FAKE_SPEC=str(spec_path))
    return subprocess.run(
        [sys.executable, str(KNOWLEDGE), str(target), "build", *extra_args],
        capture_output=True, text=True, env=env)


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


def test_unimplemented_subcommands_exit_2(tmp_path):
    t = _target(tmp_path)
    for sub in ("refresh", "status", "autorefresh"):
        r = subprocess.run([sys.executable, str(KNOWLEDGE), str(t), sub],
                          capture_output=True, text=True, env=dict(os.environ))
        assert r.returncode == 2, f"{sub}: {r.stdout + r.stderr}"
        assert "Task 4" in (r.stdout + r.stderr)


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
