# Interactive knowledge-graph view (dashboard): the confined GET /vendor/<file>
# route serving the vendored client-side graph lib (cytoscape.js, for the
# Knowledge view's new Graph mode), plus confirming the EXISTING
# /api/knowledge-doc route (read_knowledge_doc — see
# test_command_center_onboarding.py's doc-browser tests) already serves the
# parallel knowledge-agent's new graph.json unchanged, and that build_state()/
# onboarding_state() stay byte-identical when graph.json is absent.
#
# Test level: same convention as test_command_center_qa_routes.py — this
# repo's command_center tests exercise the module function a route handler
# calls directly, never a live HTTP socket (no precedent anywhere in this
# suite for instantiating BaseHTTPRequestHandler without a real connection).
# read_vendor is the exact function do_GET's `/vendor/<file>` branch calls for
# every status code the route can produce (200 for the one allowlisted+
# present file, 404 for everything else — disallowed name, missing file,
# traversal/encoding trick). JS rendering (cytoscape elements, highlight-the-
# chain, type filters) isn't harness-testable from here — server-side only.
#
# Run: python3.11 -m pytest tests/test_command_center_graph.py -v   (from orbit/)
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, ENGINE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    ap = tmp_path / ".autopilot"
    (ap / "state").mkdir(parents=True)
    (ap / "backlog.yaml").write_text("tasks:\n")
    monkeypatch.setenv("AP_HOME", str(ap))
    monkeypatch.setenv("AP_STATE", str(ap / "state"))
    return _mod("command_center")


# ── /vendor/<file> — read_vendor ──────────────────────────────────────────────

def test_vendor_dir_points_at_engine_vendor(cc):
    assert cc._VENDOR_DIR == cc.ENGINE / "vendor"


def test_vendor_allowlist_is_exactly_cytoscape(cc):
    # The whole gate is dict membership — nothing else should ever be
    # servable through this route without a code change.
    assert set(cc._VENDOR_ALLOW) == {"cytoscape.min.js"}
    assert cc._VENDOR_ALLOW["cytoscape.min.js"] == "application/javascript"


def test_vendor_serves_the_real_vendored_cytoscape_file(cc):
    # Exercises the actual file this task ships (not a stub) — a future
    # accidental deletion/truncation of engine/vendor/cytoscape.min.js fails
    # this test too.
    served = cc.read_vendor("cytoscape.min.js")
    assert served is not None
    body, ctype = served
    assert ctype == "application/javascript"
    assert len(body) > 1000
    assert b"Cytoscape" in body


def test_vendor_serves_a_stub_file_when_present(cc, monkeypatch, tmp_path):
    # Independent of whatever's actually vendored on disk: point _VENDOR_DIR
    # at a scratch dir with a tiny stub so this test doesn't depend on network
    # access ever having succeeded in whatever environment runs the suite.
    vdir = tmp_path / "vendor_stub"
    vdir.mkdir()
    (vdir / "cytoscape.min.js").write_text("/* stub cytoscape */\nvar cytoscape={};\n")
    monkeypatch.setattr(cc, "_VENDOR_DIR", vdir)
    served = cc.read_vendor("cytoscape.min.js")
    assert served is not None
    body, ctype = served
    assert ctype == "application/javascript"
    assert b"cytoscape" in body


def test_vendor_rejects_disallowed_filename(cc, monkeypatch, tmp_path):
    # Some OTHER real file sitting in vendor/ (even one that exists on disk)
    # must never be served through this route — allowlist is a fixed dict,
    # not "anything present under vendor/". Uses a scratch dir, never the
    # real engine/vendor/, so the test never leaves stray files in the repo.
    vdir = tmp_path / "vendor_other"
    vdir.mkdir()
    (vdir / "not-allowed.js").write_text("evil() {}")
    monkeypatch.setattr(cc, "_VENDOR_DIR", vdir)
    assert cc.read_vendor("not-allowed.js") is None


def test_vendor_rejects_missing_allowlisted_file(cc, monkeypatch, tmp_path):
    # Shape/name-valid (it's the one allowlist key) but not actually present
    # on disk — must not fall through to a read.
    vdir = tmp_path / "empty_vendor"
    vdir.mkdir()
    monkeypatch.setattr(cc, "_VENDOR_DIR", vdir)
    assert cc.read_vendor("cytoscape.min.js") is None


def test_vendor_rejects_dotdot_traversal(cc):
    assert cc.read_vendor("../command_center.py") is None
    assert cc.read_vendor("sub/../../etc/passwd") is None


def test_vendor_rejects_absolute_path(cc):
    assert cc.read_vendor("/etc/passwd") is None


def test_vendor_rejects_empty_name(cc):
    assert cc.read_vendor("") is None


def test_vendor_rejects_encoded_traversal_string(cc):
    # A literal percent-encoded name (do_GET never unquotes the path segment
    # for this route) is just some other string — the dict-membership gate
    # rejects it exactly like any other non-allowlisted name.
    assert cc.read_vendor("%2e%2e%2fcommand_center.py") is None


def test_vendor_rejects_symlink_escape(cc, monkeypatch, tmp_path):
    # Belt-and-suspenders: even if a symlink inside vendor/ somehow used the
    # allowlisted name, the resolve()+startswith confinement must still catch
    # an escape (same template as read_qa_shot/read_knowledge_doc). Scratch
    # dir, never the real engine/vendor/.
    vdir = tmp_path / "vendor_escape"
    vdir.mkdir()
    outside = tmp_path / "outside.js"
    outside.write_text("secret js")
    (vdir / "cytoscape.min.js").symlink_to(outside)
    monkeypatch.setattr(cc, "_VENDOR_DIR", vdir)
    assert cc.read_vendor("cytoscape.min.js") is None


# ── /api/knowledge-doc already serves graph.json (no new route needed) ───────

_GRAPH_FIXTURE = {
    "version": 1,
    "generated_at": "2026-07-25T00:00:00Z",
    "base_sha": "abc123",
    "nodes": [
        {"id": "GET /users", "type": "endpoint", "label": "GET /users", "file": "app/routes.py", "meta": {}},
        {"id": "app/routes.py:list_users", "type": "function", "label": "list_users",
         "file": "app/routes.py", "meta": {"line": 12}},
        {"id": "table:users", "type": "table", "label": "users", "file": "", "meta": {}},
        {"id": "flow:signup", "type": "flow", "label": "Signup", "file": "", "meta": {}},
    ],
    "edges": [
        {"source": "GET /users", "target": "app/routes.py:list_users", "type": "handles"},
        {"source": "app/routes.py:list_users", "target": "table:users", "type": "reads"},
        {"source": "flow:signup", "target": "GET /users", "type": "step", "order": 1},
    ],
}


def test_knowledge_doc_route_serves_graph_json_fixture(cc):
    kdir = cc.AP_HOME / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "graph.json").write_text(json.dumps(_GRAPH_FIXTURE))

    # Same allowlist path as any other knowledge doc — a LIVE directory
    # listing, so graph.json needs no special-casing in read_knowledge_doc.
    assert "graph.json" in cc._knowledge_docs()
    served = cc.read_knowledge_doc("graph.json")
    assert served is not None
    assert json.loads(served) == _GRAPH_FIXTURE


def test_knowledge_doc_route_404s_when_graph_json_absent(cc):
    kdir = cc.AP_HOME / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "INDEX.md").write_text("# index\n")
    assert "graph.json" not in cc._knowledge_docs()
    assert cc.read_knowledge_doc("graph.json") is None


def test_knowledge_doc_route_rejects_malformed_graph_json_gracefully_at_the_server_layer(cc):
    # The server layer just serves bytes/text — it never parses graph.json
    # (that's the browser's job, and cc_shell.html's fetch handler is
    # responsible for the friendly "malformed graph" error). Confirm the
    # route still serves whatever bytes are on disk rather than erroring,
    # so a corrupt graph.json degrades in the client, not the server.
    kdir = cc.AP_HOME / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "graph.json").write_text("{not valid json,,,")
    served = cc.read_knowledge_doc("graph.json")
    assert served == "{not valid json,,,"


def test_knowledge_doc_route_serves_a_large_graph_json_whole_not_truncated_at_400k_chars(cc):
    # Regression: read_knowledge_doc used to slice the text to `[:400_000]`
    # chars AFTER already gating on `_KDOC_MAX_BYTES` (2 MB) — so a
    # legitimately-sized graph.json between ~400 KB and 2 MB was served
    # truncated (invalid JSON to the client) even though it's on-disk-valid
    # and under the route's own size gate. Build one comfortably in that
    # window and confirm it comes back whole and still parses.
    kdir = cc.AP_HOME / "knowledge"
    kdir.mkdir(parents=True)
    nodes = [{"id": f"table:t{i}", "type": "table", "label": f"t{i}", "file": "", "meta": {}}
             for i in range(12000)]
    graph = {"version": 1, "generated_at": "2026-07-25T00:00:00Z", "nodes": nodes, "edges": []}
    text = json.dumps(graph)
    assert 400_000 < len(text) < cc._KDOC_MAX_BYTES, \
        f"fixture must land strictly between the old cap and the size gate, got {len(text)}"
    (kdir / "graph.json").write_text(text)
    served = cc.read_knowledge_doc("graph.json")
    assert served is not None
    assert len(served) == len(text)
    parsed = json.loads(served)   # would raise on a mid-object truncation
    assert len(parsed["nodes"]) == 12000


# ── build_state() / onboarding_state() stay byte-neutral without graph.json ──

def test_build_state_onboarding_unchanged_when_graph_json_absent(cc):
    st = cc.build_state()
    ob = st["onboarding"]
    assert ob == {
        "has_knowledge": False,
        "knowledge_docs": [],
        "intake_running": False,
        "knowledge_running": False,
        "gates_configured": False,
        "qa_configured": False,
        "plan_configured": False,
    }


def test_build_state_onboarding_lists_graph_json_alongside_markdown_docs_when_present(cc):
    # graph.json is just another file under knowledge/ from build_state's
    # point of view — no special "has_graph" key was added to the payload;
    # the dashboard derives "does a graph exist" client-side from this same
    # knowledge_docs list (keeps this route/payload byte-neutral either way).
    kdir = cc.AP_HOME / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "INDEX.md").write_text("# index\n")
    (kdir / "graph.json").write_text(json.dumps(_GRAPH_FIXTURE))
    ob = cc.build_state()["onboarding"]
    assert set(ob["knowledge_docs"]) == {"INDEX.md", "graph.json"}
    assert ob["has_knowledge"] is True
