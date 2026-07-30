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
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "engine" / "knowledge.py"
DOCTOR = ROOT / "engine" / "doctor.py"

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

VALID_GRAPH = json.dumps({
    "version": 1,
    "generated_at": "2026-07-22T00:00:00Z",
    "base_sha": "deadbeefcafe",
    "nodes": [
        {"id": "GET /health", "type": "endpoint", "label": "GET /health",
         "file": "routes.py:10", "meta": {"method": "GET"}},
        {"id": "routes.py:health", "type": "function", "label": "health",
         "file": "routes.py:12"},
        {"id": "table:users", "type": "table", "label": "users",
         "file": "migrations/0001_init.sql:3"},
        {"id": "flow:checkout", "type": "flow", "label": "checkout flow", "file": None},
    ],
    "edges": [
        {"source": "GET /health", "target": "routes.py:health", "type": "handles"},
        {"source": "routes.py:health", "target": "table:users", "type": "reads"},
        {"source": "flow:checkout", "target": "GET /health", "type": "step", "order": 1},
    ],
})

ALL_DOCS_WITH_GRAPH = ALL_DOCS + ["graph.json"]


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


def _full_files_with_graph(graph=VALID_GRAPH, **kw):
    """_full_files() plus a graph.json — for graph.json-specific validation tests."""
    files = _full_files(**kw)
    files["graph.json"] = graph
    return files


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


# --- graph.json ---------------------------------------------------------
# graph.json is OPTIONAL (validated only if present under knowledge/) — the
# absent case is covered implicitly by every test above (none of them write
# one) plus test_build_absent_graph_json_still_valid below for clarity.

def _graph_manifest(**extra_kwargs):
    return _full_manifest(docs=ALL_DOCS_WITH_GRAPH,
                          extra={"files": {"routes.py": ["endpoints.md", "graph.json"]}},
                          **extra_kwargs)


def _run_with_graph(t, tmp_path, graph_text):
    spec = _spec(tmp_path, "spec.json", _full_files_with_graph(graph=graph_text), _graph_manifest())
    return run(t, _agent_script(tmp_path), spec)


def test_build_succeeds_with_a_valid_graph_json(tmp_path):
    t = _target(tmp_path)
    r = _run_with_graph(t, tmp_path, VALID_GRAPH)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (t / ".autopilot" / "knowledge" / "graph.json").exists()


def test_build_absent_graph_json_still_valid(tmp_path):
    # existing behavior unchanged: no graph.json written at all → still valid
    t = _target(tmp_path)
    spec = _spec(tmp_path, "spec.json", _full_files(), _full_manifest())
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (t / ".autopilot" / "knowledge" / "graph.json").exists()


def test_build_fails_when_graph_json_is_invalid_json_syntax(tmp_path):
    t = _target(tmp_path)
    r = _run_with_graph(t, tmp_path, "{not valid json")
    assert r.returncode == 1
    assert "VALIDATE" in r.stdout
    assert "graph.json" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_graph_json_is_not_an_object(tmp_path):
    t = _target(tmp_path)
    r = _run_with_graph(t, tmp_path, json.dumps([1, 2, 3]))
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "object" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_graph_json_missing_nodes(tmp_path):
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z", "edges": []})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "nodes" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_graph_json_missing_edges(tmp_path):
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z",
                        "nodes": [{"id": "table:users", "type": "table", "label": "users"}]})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "edges" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_graph_json_node_has_no_id(tmp_path):
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z",
                        "nodes": [{"type": "table", "label": "users"}], "edges": []})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "id" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_graph_json_node_has_bad_type(tmp_path):
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z",
                        "nodes": [{"id": "table:users", "type": "bogus", "label": "users"}],
                        "edges": []})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "type" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_graph_json_node_id_not_unique(tmp_path):
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z",
                        "nodes": [{"id": "table:users", "type": "table", "label": "users"},
                                  {"id": "table:users", "type": "table", "label": "again"}],
                        "edges": []})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "unique" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_graph_json_edge_source_not_a_node_id(tmp_path):
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z",
                        "nodes": [{"id": "table:users", "type": "table", "label": "users"}],
                        "edges": [{"source": "table:ghost", "target": "table:users",
                                   "type": "relates"}]})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "ghost" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_graph_json_edge_target_not_a_node_id(tmp_path):
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z",
                        "nodes": [{"id": "table:users", "type": "table", "label": "users"}],
                        "edges": [{"source": "table:users", "target": "table:ghost",
                                   "type": "relates"}]})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "ghost" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_graph_json_edge_has_bad_type(tmp_path):
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z",
                        "nodes": [{"id": "table:a", "type": "table", "label": "a"},
                                  {"id": "table:b", "type": "table", "label": "b"}],
                        "edges": [{"source": "table:a", "target": "table:b", "type": "bogus"}]})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "type" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


# --- graph.json: unhashable field values must VALIDATE, never crash --------
# `x not in some_set` HASHES x — a node/edge field that's legal JSON (a list
# or object) but an illegal graph.json value used to raise an unhandled
# TypeError instead of reporting a VALIDATE line, which (with no done-marker
# printed) left the dashboard's Run view stuck on "running" indefinitely.

def test_build_fails_when_graph_json_node_id_is_a_list(tmp_path):
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z",
                        "nodes": [{"id": ["not", "a", "string"], "type": "table", "label": "x"}],
                        "edges": []})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_graph_json_node_type_is_a_dict(tmp_path):
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z",
                        "nodes": [{"id": "table:users", "type": {"x": 1}, "label": "users"}],
                        "edges": []})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_graph_json_edge_source_is_a_list(tmp_path):
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z",
                        "nodes": [{"id": "table:users", "type": "table", "label": "users"}],
                        "edges": [{"source": ["a"], "target": "table:users", "type": "relates"}]})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_graph_json_edge_target_is_a_dict(tmp_path):
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z",
                        "nodes": [{"id": "table:users", "type": "table", "label": "users"}],
                        "edges": [{"source": "table:users", "target": {"x": 1}, "type": "relates"}]})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_graph_json_edge_type_is_a_list(tmp_path):
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z",
                        "nodes": [{"id": "table:a", "type": "table", "label": "a"},
                                  {"id": "table:b", "type": "table", "label": "b"}],
                        "edges": [{"source": "table:a", "target": "table:b", "type": ["bogus"]}]})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_build_fails_when_step_edge_order_is_a_string(tmp_path):
    # A stringified order passes the OLD build unvalidated but the UI nulls
    # it (typeof e.order==='number' check), silently losing flow ordering.
    t = _target(tmp_path)
    graph = json.dumps({"version": 1, "generated_at": "2026-07-22T00:00:00Z",
                        "nodes": [{"id": "flow:checkout", "type": "flow", "label": "checkout"},
                                  {"id": "GET /health", "type": "endpoint", "label": "GET /health"}],
                        "edges": [{"source": "flow:checkout", "target": "GET /health",
                                   "type": "step", "order": "1"}]})
    r = _run_with_graph(t, tmp_path, graph)
    assert r.returncode == 1
    assert "graph.json" in (r.stdout + r.stderr)
    assert "order" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


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


# === Fix A: sync the /orbit-knowledge command into the target on every build ===
# A target onboarded before the graph.json feature landed in skills/orbit-
# knowledge.md has a STALE .claude/commands/orbit-knowledge.md — its builds
# silently never produce graph.json. run_survey_agent() now (re)copies
# $ORBIT_HOME/skills/orbit-knowledge.md over that file before every survey
# call (build AND refresh). In these tests ORBIT_HOME is not overridden, so
# it resolves to this repo's own root (engine/knowledge.py's ENGINE.parent) —
# the real skills/orbit-knowledge.md is the thing being synced.

REAL_ORBIT_KNOWLEDGE_MD = (ROOT / "skills" / "orbit-knowledge.md").read_text()


def test_build_syncs_orbit_knowledge_command_into_target(tmp_path):
    t = _target(tmp_path)
    assert not (t / ".claude").exists()          # no .claude/ at all before the build
    spec = _spec(tmp_path, "spec.json", _full_files(), _full_manifest())
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 0, r.stdout + r.stderr
    synced = t / ".claude" / "commands" / "orbit-knowledge.md"
    assert synced.exists()
    assert synced.read_text() == REAL_ORBIT_KNOWLEDGE_MD
    assert "synced the /orbit-knowledge command" in r.stdout


def test_build_overwrites_a_stale_orbit_knowledge_command(tmp_path):
    t = _target(tmp_path)
    cmd_dir = t / ".claude" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "orbit-knowledge.md").write_text("stale copy — predates graph.json\n")
    spec = _spec(tmp_path, "spec.json", _full_files(), _full_manifest())
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (cmd_dir / "orbit-knowledge.md").read_text() == REAL_ORBIT_KNOWLEDGE_MD


def test_refresh_also_syncs_orbit_knowledge_command(tmp_path):
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"]})
    synced = t / ".claude" / "commands" / "orbit-knowledge.md"
    assert synced.read_text() == REAL_ORBIT_KNOWLEDGE_MD    # the build above already synced it
    synced.write_text("stale again\n")                      # simulate drift since that build

    sha = _commit_file(t, "routes.py", "print('changed')\n")
    _write_markers(t, [_marker("task-sync", "2026-07-15T00:00:00Z", sha=sha)])
    refresh_spec = _spec(tmp_path, "refresh-spec.json", _full_files(), _full_manifest())
    r = run_sub(t, "refresh", env_extra={
        "ORBIT_KNOWLEDGE_CMD": _agent_script(tmp_path),
        "FAKE_SPEC": str(refresh_spec),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert synced.read_text() == REAL_ORBIT_KNOWLEDGE_MD


def test_build_sync_failure_warns_and_does_not_abort(tmp_path):
    # a file sitting where the .claude/commands/ directory needs to go makes
    # the mkdir(parents=True) fail — the build must WARN, not abort
    t = _target(tmp_path)
    (t / ".claude").mkdir()
    (t / ".claude" / "commands").write_text("not a directory")
    spec = _spec(tmp_path, "spec.json", _full_files(), _full_manifest())
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" in (r.stdout + r.stderr)


# === Fix B: elapsed heartbeat while the real survey call is in flight ========
# The heartbeat only wraps the REAL `claude` path (no ORBIT_KNOWLEDGE_CMD
# override) — a test fake invoked via the override returns effectively
# instantly and would never emit a tick regardless. To exercise the real
# path without a real `claude` binary, `_fake_claude_dir` drops an executable
# named `claude` on PATH instead of using ORBIT_KNOWLEDGE_CMD, so
# run_survey_agent takes its `shutil.which("claude")` branch — which is
# exactly the branch the heartbeat thread wraps.

def _fake_claude_dir(tmp_path, sleep_secs=0.0):
    """A directory containing an executable `claude` that behaves like
    _agent_script (reads $FAKE_SPEC, writes knowledge/ + manifest.json),
    after first sleeping `sleep_secs` — stands in for the real `claude`
    binary on PATH so the non-override code path (the one the heartbeat
    wraps) can be exercised in a test."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import base64, json, os, sys, time
        from pathlib import Path
        time.sleep({sleep_secs})
        spec = json.loads(Path(os.environ["FAKE_SPEC"]).read_text())
        kdir = Path(os.environ["AP_HOME"]) / "knowledge"
        kdir.mkdir(parents=True, exist_ok=True)
        for rel, content in spec.get("files", {{}}).items():
            p = kdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        for rel, b64 in spec.get("binary_files", {{}}).items():
            p = kdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(b64))
        if "manifest" in spec:
            (kdir / "manifest.json").write_text(json.dumps(spec["manifest"]))
        log = os.environ.get("FAKE_CALL_LOG")
        if log:
            with open(log, "a") as f:
                f.write(json.dumps({{"argv": sys.argv[1:],
                                     "skill": os.environ.get("ORBIT_KNOWLEDGE_SKILL")}}) + "\\n")
        """))
    script.chmod(0o755)
    return bin_dir


def test_build_fast_fake_produces_no_heartbeat(tmp_path):
    # normal fast build via the ORBIT_KNOWLEDGE_CMD override: no regression —
    # still exits 0 + validates, and (returning effectively instantly) never
    # ticks a heartbeat line
    t = _target(tmp_path)
    spec = _spec(tmp_path, "spec.json", _full_files(), _full_manifest())
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "elapsed" not in r.stdout


def test_build_heartbeat_ticks_on_slow_real_path(tmp_path):
    t = _target(tmp_path)
    bin_dir = _fake_claude_dir(tmp_path, sleep_secs=0.5)
    spec = _spec(tmp_path, "spec.json", _full_files(), _full_manifest())
    env = dict(os.environ)
    env.pop("ORBIT_KNOWLEDGE_CMD", None)
    env.update(
        FAKE_SPEC=str(spec),
        ORBIT_KNOWLEDGE_HEARTBEAT_SECS="0.05",
        PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
    )
    r = subprocess.run([sys.executable, str(KNOWLEDGE), str(t), "build"],
                      capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "elapsed" in r.stdout


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


def test_autorefresh_no_config_exits_0_with_warn(tmp_path):
    # no .autopilot/ at all — autorefresh's contract is unconditional exit 0,
    # unlike build/status/refresh, which correctly usage-error (exit 2) here
    r = run_sub(tmp_path, "autorefresh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" in (r.stdout + r.stderr)


def test_autorefresh_malformed_config_exits_0_with_warn(tmp_path):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "config.yaml").write_text("repo: [unterminated\n")   # invalid YAML
    r = run_sub(tmp_path, "autorefresh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" in (r.stdout + r.stderr)


def test_status_no_config_still_exits_2(tmp_path):
    # unchanged: status is NOT autorefresh — a missing target is a genuine
    # usage error, same as build's existing test_build_exits_2_without_autopilot
    r = run_sub(tmp_path, "status")
    assert r.returncode == 2, r.stdout + r.stderr


def test_autorefresh_stale_after_merges_zero_still_acts(tmp_path):
    # stale_after_merges: 0 must NOT fall back to the default of 3 — `0 or
    # DEFAULT` would silently turn "refresh on any staleness" into "refresh at
    # 3+", which is the opposite of what an operator asking for 0 wants.
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"routes.py": ["endpoints.md"]})
    _configure_knowledge(t, refresh="auto", stale_after_merges=0)
    sha = _commit_file(t, "routes.py", "print('changed')\n")
    _write_markers(t, [_marker("task-16", "2026-07-15T00:00:00Z", sha=sha)])

    call_log = tmp_path / "calls.jsonl"
    refresh_spec = _spec(tmp_path, "refresh-spec.json", _full_files(), _full_manifest())
    r = run_sub(t, "autorefresh", env_extra={
        "ORBIT_KNOWLEDGE_CMD": _agent_script(tmp_path),
        "FAKE_SPEC": str(refresh_spec),
        "FAKE_CALL_LOG": str(call_log),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert call_log.exists() and len(call_log.read_text().splitlines()) == 1


def test_status_unreachable_sha_and_traversal_patch_warns_with_reason(tmp_path):
    # a patch path that escapes diffs/ must be distinguishable in the WARN
    # line from a merely-missing patch — Minor B from the task-4 review
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"src/*.py": ["conventions.md"]})
    (t / ".autopilot" / "state" / "diffs").mkdir(parents=True)
    outside = t / ".autopilot" / "state" / "secret.patch"
    outside.write_text("+++ b/src/app.py\n")
    _write_markers(t, [_marker("task-17", "2026-07-15T00:00:00Z",
                               sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                               patch="../secret.patch")])

    r = run_sub(t, "status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" in r.stdout
    assert "traversal" in r.stdout or "escapes" in r.stdout


# === Task 5: doctor's knowledge-base check (read-only — no doctor test conventions
# existed yet, so these are subprocess smoke cases per the task-5 brief) =====
#
# NOTE: assertions anchor on the check-line's own "✓ "/"✗ " prefix (after
# stripping leading whitespace) rather than a loose "knowledge" substring —
# pytest's tmp_path directory is derived from the test function name (e.g.
# .../test_doctor_valid_knowledge_manifest_shows_ok_line0/...) and doctor
# prints that path inline in unrelated ✓ lines (config.yaml present, etc.),
# which would otherwise false-positive the match.

def _run_doctor(target):
    return subprocess.run([sys.executable, str(DOCTOR), str(target)],
                          capture_output=True, text=True, env=dict(os.environ))


def _check_lines(stdout, mark):
    return [ln.strip() for ln in stdout.splitlines() if ln.strip().startswith(mark)]


def test_doctor_valid_manifest_shows_ok_line(tmp_path):
    # (test name deliberately avoids the word "knowledge" — see the note above
    # _run_doctor: tmp_path's directory is derived from the test id and would
    # otherwise leak into unrelated ✓ lines' path details.)
    t = _target(tmp_path)
    kdir = t / ".autopilot" / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "INDEX.md").write_text("# index\n")
    (kdir / "manifest.json").write_text(json.dumps({
        "version": 1, "generated_at": "2026-07-01T00:00:00Z",
        "base_sha": "deadbeef", "docs": ["INDEX.md"], "files": {}}))
    r = _run_doctor(t)
    assert any(ln.startswith("✓ knowledge base") for ln in _check_lines(r.stdout, "✓")), r.stdout
    assert not any("knowledge" in ln for ln in _check_lines(r.stdout, "✗")), r.stdout


def test_doctor_broken_manifest_shows_fail_line(tmp_path):
    t = _target(tmp_path)
    kdir = t / ".autopilot" / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "manifest.json").write_text("{not valid json")
    r = _run_doctor(t)
    assert any("knowledge" in ln for ln in _check_lines(r.stdout, "✗")), r.stdout


def test_doctor_manifest_lists_missing_doc_shows_fail_line(tmp_path):
    t = _target(tmp_path)
    kdir = t / ".autopilot" / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "manifest.json").write_text(json.dumps({
        "version": 1, "generated_at": "2026-07-01T00:00:00Z",
        "base_sha": "deadbeef", "docs": ["missing.md"], "files": {}}))
    r = _run_doctor(t)
    assert any("knowledge" in ln for ln in _check_lines(r.stdout, "✗")), r.stdout


def test_doctor_refresh_auto_without_knowledge_dir_fails(tmp_path):
    t = _target(tmp_path)
    _configure_knowledge(t, refresh="auto")
    r = _run_doctor(t)
    assert any("knowledge.refresh: auto" in ln for ln in _check_lines(r.stdout, "✗")), r.stdout


def test_doctor_feature_unused_prints_nothing_new(tmp_path):
    # feature unused (no kb dir, refresh not "auto") → zero behavior change:
    # doctor emits no ✓/✗/informational knowledge line, and the run stays
    # exactly as green/red as it was before this task.
    # (test name deliberately avoids the word "knowledge" — tmp_path's
    # directory is derived from the test id, and pytest_id substrings leak
    # into unrelated ✓ lines' path details, e.g. "config.yaml present <path>".)
    t = _target(tmp_path)
    r = _run_doctor(t)
    assert not any("knowledge" in ln for ln in _check_lines(r.stdout, "✓")
                   + _check_lines(r.stdout, "✗") + _check_lines(r.stdout, "·"))


def test_doctor_reports_stale_docs_informationally(tmp_path):
    t = _target(tmp_path)
    kdir = t / ".autopilot" / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "INDEX.md").write_text("# index\n")
    (kdir / "manifest.json").write_text(json.dumps({
        "version": 1, "generated_at": "2026-07-01T00:00:00Z",
        "base_sha": "deadbeef", "docs": ["INDEX.md"], "files": {}}))
    (t / ".autopilot" / "state" / "knowledge-staleness.json").write_text(json.dumps({
        "checked_at": "2026-07-22T00:00:00Z", "generated_at": "2026-07-01T00:00:00Z",
        "stale_docs": ["INDEX.md"], "merges_considered": 2}))
    r = _run_doctor(t)
    assert "knowledge: 1 doc(s) stale — orbit knowledge <target> refresh" in r.stdout, r.stdout


# === Task 11: doctor's plan wiring checks (read-only surfacing of the `plan:`
# config block — engine/config.py's `validate` already checks enforce/categories/
# model *shapes*; doctor additionally confirms the planner agent resolves and
# that plan.categories names real router categories, a check config.py can't
# make because it never reads router.yaml) =================================

def _configure_plan(t, categories=None, enforce=None, model=None):
    lines = ['repo: "."\n', 'base_branch: "main"\n', 'gates:\n', '  test: {cmd: "true", cwd: "."}\n',
             'plan:\n']
    if categories is not None:
        lines.append(f'  categories: {json.dumps(categories)}\n')
    if enforce is not None:
        lines.append(f'  enforce: "{enforce}"\n')
    if model is not None:
        lines.append(f'  model: "{model}"\n')
    (t / ".autopilot" / "config.yaml").write_text("".join(lines))


def test_doctor_valid_plan_block_shows_ok_lines(tmp_path):
    t = _target(tmp_path)
    _configure_plan(t, categories=["bug", "feature"], enforce="verifier")
    r = _run_doctor(t)
    oks = _check_lines(r.stdout, "✓")
    assert any(ln.startswith("✓ plan: agents/planner.md resolves") for ln in oks), r.stdout
    assert any(ln.startswith('✓ plan.enforce is "verifier" or "block"') for ln in oks), r.stdout
    assert any(ln.startswith("✓ plan.categories are known router categories") for ln in oks), r.stdout
    assert not any(ln.startswith("✗ plan") for ln in _check_lines(r.stdout, "✗")), r.stdout


def test_doctor_plan_unknown_category_shows_fail_line(tmp_path):
    t = _target(tmp_path)
    _configure_plan(t, categories=["bug", "not_a_real_category"])
    r = _run_doctor(t)
    fails = _check_lines(r.stdout, "✗")
    assert any(ln.startswith("✗ plan.categories are known router categories")
               and "not_a_real_category" in ln for ln in fails), r.stdout


def test_doctor_plan_bad_enforce_value_shows_fail_line(tmp_path):
    t = _target(tmp_path)
    _configure_plan(t, enforce="sometimes")
    r = _run_doctor(t)
    fails = _check_lines(r.stdout, "✗")
    assert any(ln.startswith('✗ plan.enforce is "verifier" or "block"') for ln in fails), r.stdout


def test_doctor_no_task_scope_config_prints_nothing_new(tmp_path):
    # feature unused (no `plan:` block) → zero behavior change: doctor emits no
    # ✓/✗/informational plan line. (Test name deliberately avoids the literal
    # word "plan" — tmp_path's directory is derived from the test id and would
    # otherwise leak into unrelated ✓ lines' path details, e.g. "config.yaml
    # present <path>", the same false-positive risk noted above for "knowledge".)
    t = _target(tmp_path)
    r = _run_doctor(t)
    assert not any("plan" in ln.lower() for ln in _check_lines(r.stdout, "✓")
                   + _check_lines(r.stdout, "✗") + _check_lines(r.stdout, "·"))


# === Final whole-branch review fixes ========================================

def test_status_sha_flag_injection_is_rejected_and_falls_back_with_warning(tmp_path):
    # Finding 1 (security): a merge marker's `sha` is agent-written data
    # (stored verbatim from the maker's ledger call) that used to reach
    # `git show --name-only --format= <sha>` completely unguarded. Confirmed
    # exploit absent the fix: `git show --name-only --format= --output=X <any
    # sha>` exits 0 and writes file X — i.e. an agent-controlled "sha" of
    # `--output=<path>` makes the engine write an arbitrary file. This marker
    # carries exactly that payload (an absolute --output= path); the file
    # must never be created, and the marker must be handled exactly like an
    # unreachable sha — patch fallback attempted (none given here), then
    # WARN + skip, exit 0, fresh — with the rejection reason named in the
    # WARN line.
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"src/*.py": ["conventions.md"]})
    pwned = tmp_path / "pwned.txt"
    _write_markers(t, [_marker("task-inj", "2026-07-15T00:00:00Z",
                               sha=f"--output={pwned}", patch="")])

    r = run_sub(t, "status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not pwned.exists(), "git argument injection via marker sha wrote a file"
    assert "WARN" in r.stdout
    assert "not a valid git sha" in r.stdout
    staleness = json.loads((t / ".autopilot" / "state" / "knowledge-staleness.json").read_text())
    assert staleness["stale_docs"] == []


def test_status_sha_flag_injection_falls_back_to_patch_file_when_present(tmp_path):
    # Same payload as above, but a valid patch backup exists — the invalid
    # sha must fall through to the existing patch-file fallback (not just
    # skip), exactly like today's unreachable-sha-with-patch case.
    t = _target(tmp_path)
    _build_kb(tmp_path, t, "2026-07-10T00:00:00Z", {"src/*.py": ["conventions.md"]})
    diffs = t / ".autopilot" / "state" / "diffs"
    diffs.mkdir(parents=True)
    patch = diffs / "cycle-inj.patch"
    patch.write_text(
        "diff --git a/src/app.py b/src/app.py\n"
        "index 111..222 100644\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-x = 0\n"
        "+x = 1\n"
    )
    pwned = tmp_path / "pwned2.txt"
    _write_markers(t, [_marker("task-inj-2", "2026-07-15T00:00:00Z",
                               sha=f"--output={pwned}", patch=str(patch))])

    r = run_sub(t, "status")
    assert r.returncode == 4, r.stdout + r.stderr
    assert not pwned.exists()
    assert "conventions.md" in r.stdout


def test_build_rejects_doc_path_containing_null_byte(tmp_path):
    # Finding 2 (minor): _unsafe_doc_reason's `.resolve()` used to be guarded
    # by `except OSError` only — a null byte in a manifest docs[] entry
    # raises ValueError instead, which escaped as a raw traceback (an
    # accidental fail-closed, not a deliberate one). Must now be a clean
    # VALIDATE: rejection, no traceback.
    t = _target(tmp_path)
    docs = list(ALL_DOCS) + ["bad\x00name.md"]
    spec = _spec(tmp_path, "spec.json", _full_files(), _full_manifest(docs=docs))
    r = run(t, _agent_script(tmp_path), spec)
    assert r.returncode == 1
    assert "VALIDATE" in r.stdout
    assert "Traceback" not in r.stderr


def test_endpoints_header_constant_matches_the_skill_doc():
    # Finding 3 (minor): ENDPOINTS_HEADER is hand-duplicated from
    # skills/orbit-knowledge.md with no coupling test — drift between the two
    # would be a silent false-pass (validate() keeps accepting whatever
    # knowledge.py hardcodes even if the skill's prescribed table header
    # changes out from under it). Import the real constant rather than
    # re-duplicating the literal here.
    spec = importlib.util.spec_from_file_location("knowledge", KNOWLEDGE)
    knowledge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(knowledge)

    skill_text = (ROOT / "skills" / "orbit-knowledge.md").read_text()
    assert knowledge.ENDPOINTS_HEADER in skill_text
