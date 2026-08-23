"""engine/publish.py: markdown->storage rendering, and the Confluence
adapter driven against a local stub server (never the network).

The stub deliberately answers errors with a body containing the token and a
marker string, so the "no bodies, no tokens in output" contract is pinned by
capturing stdout/stderr rather than by reading the source.
"""
import base64
import hashlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import prd_flow  # noqa: E402
import publish  # noqa: E402

EMAIL = "pm@example.com"
TOKEN = "tok-super-secret"
SECRET_BODY = "SECRET-BODY-MARKER"

# --------------------------------------------------------------------------
# render_storage
# --------------------------------------------------------------------------

RENDER_CASES = [
    ("h1", "# Title", "<h1>Title</h1>"),
    ("h4", "#### Deep", "<h4>Deep</h4>"),
    ("h5 is not a heading", "##### Deeper", "<p>##### Deeper</p>"),
    ("paragraph joins lines", "one\ntwo", "<p>one two</p>"),
    ("ul", "- a\n- b", "<ul><li>a</li><li>b</li></ul>"),
    ("ol", "1. a\n2. b", "<ol><li>a</li><li>b</li></ol>"),
    ("list kinds do not merge", "- a\n1. b",
     "<ul><li>a</li></ul>\n<ol><li>b</li></ol>"),
    ("bold italic code", "**b** and *i* and `c`",
     "<p><strong>b</strong> and <em>i</em> and <code>c</code></p>"),
    ("underscore italic", "_i_ here", "<p><em>i</em> here</p>"),
    ("snake_case is not italic", "call user_id_map now",
     "<p>call user_id_map now</p>"),
    ("link escapes its href", "[Orbit](https://x/y?a=1&b=2)",
     '<p><a href="https://x/y?a=1&amp;b=2">Orbit</a></p>'),
    ("blockquote", "> quoted\n> more", "<blockquote><p>quoted more</p></blockquote>"),
    ("fence", "```\nx = 1 < 2\n```", "<pre>x = 1 &lt; 2</pre>"),
    ("fence with language", "```python\nprint(1)\n```", "<pre>print(1)</pre>"),
    ("raw html is escaped literally", "<script>alert(1)</script>",
     "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"),
    ("ampersand escaped", "a & b", "<p>a &amp; b</p>"),
    ("inline code escapes tags", "`<b>`", "<p><code>&lt;b&gt;</code></p>"),
    ("unmatched emphasis stays literal", "a * b", "<p>a * b</p>"),
    ("unmatched bracket stays literal", "see [here", "<p>see [here</p>"),
    ("empty input", "", ""),
]


@pytest.mark.parametrize("name,md,expected",
                         RENDER_CASES, ids=[c[0] for c in RENDER_CASES])
def test_render_storage(name, md, expected):
    assert publish.render_storage(md) == expected


def test_render_storage_escapes_html_inside_every_construct():
    """The one rule that matters for a page that ships to a wiki: nothing the
    markdown carries can become live markup."""
    out = publish.render_storage(
        "# <b>t</b>\n\n- <i>li</i>\n\n> <em>q</em>\n\n[<u>l</u>](javascript:x)\n")
    assert "<b>" not in out and "<i>" not in out and "<em>q" not in out
    assert "&lt;b&gt;" in out and "&lt;i&gt;li&lt;/i&gt;" in out
    assert '<a href="javascript:x">&lt;u&gt;l&lt;/u&gt;</a>' in out


def test_render_storage_is_linear_on_degenerate_input():
    """Pure and linear-ish by construction (a memoised left-to-right scan, no
    regex on the inline path). 20k unmatched openers must not melt."""
    start = time.monotonic()
    publish.render_storage("[" * 20000 + "\n" + "*" * 20000)
    assert time.monotonic() - start < 2.0


def test_render_storage_does_not_mutate_its_input():
    md = "# T\n\n- a\n"
    publish.render_storage(md)
    assert md == "# T\n\n- a\n"


# --------------------------------------------------------------------------
# stub Atlassian
# --------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep pytest output clean
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _record(self, method, raw):
        self.server.requests.append({
            "method": method,
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "json": json.loads(raw) if raw else None,
        })

    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _maybe_fail(self):
        if self.server.fail_status is None:
            return False
        # An error body that echoes the credential, exactly like a real
        # gateway page can: if any of this reaches a terminal, the test fails.
        self._send(self.server.fail_status,
                   {"message": f"{SECRET_BODY} basic {EMAIL}:{TOKEN}"})
        return True

    def do_POST(self):
        raw = self._body()
        self._record("POST", raw)
        if self._maybe_fail():
            return
        if self.path == "/rest/api/content":
            self.server.page_seq += 1
            pid = str(self.server.page_seq)
            self.server.pages[pid] = {"version": 1, "body": json.loads(raw)}
            return self._send(200, {"id": pid, "_links": {
                "base": self.server.base_url, "webui": f"/spaces/PS/pages/{pid}"}})
        self._send(404, {"message": "no such endpoint"})

    def do_GET(self):
        self._record("GET", b"")
        if self._maybe_fail():
            return
        pid = self.path.split("/rest/api/content/", 1)[-1].split("?", 1)[0]
        page = self.server.pages.get(pid)
        if page is None:
            return self._send(404, {"message": f"{SECRET_BODY} no page {pid}"})
        self._send(200, {"id": pid, "version": {"number": page["version"]}})

    def do_PUT(self):
        raw = self._body()
        self._record("PUT", raw)
        if self._maybe_fail():
            return
        pid = self.path.split("/rest/api/content/", 1)[-1]
        if pid not in self.server.pages:
            return self._send(404, {"message": "no page"})
        self.server.pages[pid] = {"version": json.loads(raw)["version"]["number"],
                                  "body": json.loads(raw)}
        self._send(200, {"id": pid, "_links": {
            "base": self.server.base_url, "webui": f"/spaces/PS/pages/{pid}"}})


@pytest.fixture
def stub():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.requests, srv.pages = [], {}
    srv.page_seq = 0
    srv.fail_status = None
    srv.base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    # poll fast: serve_forever's 0.5s default is pure teardown latency, paid
    # once per test in this module.
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


# --------------------------------------------------------------------------
# workspace fixtures
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_ambient_tokens(monkeypatch):
    monkeypatch.delenv(publish.EMAIL_VAR, raising=False)
    monkeypatch.delenv(publish.TOKEN_VAR, raising=False)


def _ws(tmp_path, stub=None, publish_block=True, secrets=True):
    ws = tmp_path / "workspace"
    (ws / ".autopilot").mkdir(parents=True)
    (ws / "research").mkdir()
    cfg = {"repo": "."}
    if publish_block:
        cfg["publish"] = {
            "confluence": {"base_url": stub.base_url, "space": "PS"},
        }
    (ws / ".autopilot" / "config.yaml").write_text(yaml.safe_dump(cfg))
    if secrets:
        (ws / ".autopilot" / ".secrets.env").write_text(
            f"# tokens\n{publish.EMAIL_VAR}={EMAIL}\n{publish.TOKEN_VAR}={TOKEN}\n")
    return ws


PRD_MD = """# Checkout PRD

## Problem

Card OTP drops **62%** of carts.

- one
- two
"""


def _done_run(ws, run="r1", title="Checkout PRD", md=PRD_MD, status="done"):
    prd_flow.new(str(ws), run, title)
    art = ws / "research" / run / "04-prd.md"
    art.write_text(md)
    marker = prd_flow.load_marker(str(ws), run)
    marker["stages"]["draft"] = {
        "sha": hashlib.sha256(art.read_bytes()).hexdigest(),
        "prev_sha": None, "stamped_at": "2026-08-22T00:00:00Z",
        "evals": {"ok": True, "errors": []}}
    marker["status"] = status
    if status == "done":
        marker["approvals"]["prd"] = {"at": "2026-08-22T00:00:00Z"}
    prd_flow.write_marker(str(ws), run, marker)
    return marker


def _assert_no_secrets(captured):
    blob = captured.out + captured.err
    assert TOKEN not in blob
    assert SECRET_BODY not in blob
    assert base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode() not in blob


# --------------------------------------------------------------------------
# Confluence
# --------------------------------------------------------------------------

def test_prd_creates_then_updates_the_same_page(tmp_path, stub):
    ws = _ws(tmp_path, stub)
    _done_run(ws)

    url = publish.publish_prd(str(ws), "r1")
    assert url == f"{stub.base_url}/spaces/PS/pages/1"
    first = prd_flow.load_marker(str(ws), "r1")["published"]["confluence"]
    assert first["page_id"] == "1" and first["url"] == url and first["at"]

    posts = [r for r in stub.requests if r["method"] == "POST"]
    assert len(posts) == 1
    body = posts[0]["json"]
    assert body["title"] == "Checkout PRD (r1)"
    assert body["space"] == {"key": "PS"}
    assert body["body"]["storage"]["representation"] == "storage"
    assert "<h1>Checkout PRD</h1>" in body["body"]["storage"]["value"]
    assert "<strong>62%</strong>" in body["body"]["storage"]["value"]
    assert "ancestors" not in body

    # re-run updates page 1 rather than creating a second one
    again = publish.publish_prd(str(ws), "r1")
    assert again == url
    assert len([r for r in stub.requests if r["method"] == "POST"]) == 1
    puts = [r for r in stub.requests if r["method"] == "PUT"]
    assert len(puts) == 1 and puts[0]["path"] == "/rest/api/content/1"
    assert puts[0]["json"]["version"] == {"number": 2}
    assert prd_flow.load_marker(str(ws), "r1")["published"]["confluence"]["page_id"] == "1"


def test_parent_id_becomes_an_ancestor(tmp_path, stub):
    ws = _ws(tmp_path, stub)
    cfg = yaml.safe_load((ws / ".autopilot" / "config.yaml").read_text())
    cfg["publish"]["confluence"]["parent_id"] = 4242
    (ws / ".autopilot" / "config.yaml").write_text(yaml.safe_dump(cfg))
    _done_run(ws)
    publish.publish_prd(str(ws), "r1")
    assert stub.requests[0]["json"]["ancestors"] == [{"id": "4242"}]


def test_auth_header_is_basic_base64_of_email_and_token(tmp_path, stub):
    ws = _ws(tmp_path, stub)
    _done_run(ws)
    publish.publish_prd(str(ws), "r1")
    expected = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    assert stub.requests[0]["auth"] == f"Basic {expected}"
    assert stub.requests[0]["content_type"] == "application/json"


def test_secrets_file_wins_over_the_environment(tmp_path, stub, monkeypatch):
    """Precedence matches prd_flow's stage children: the workspace's own
    .secrets.env beats whatever the shell happened to export."""
    monkeypatch.setenv(publish.EMAIL_VAR, "stale@example.com")
    monkeypatch.setenv(publish.TOKEN_VAR, "stale-token")
    ws = _ws(tmp_path, stub)
    _done_run(ws)
    publish.publish_prd(str(ws), "r1")
    expected = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    assert stub.requests[0]["auth"] == f"Basic {expected}"


def test_environment_is_the_fallback_when_no_secrets_file(tmp_path, stub, monkeypatch):
    monkeypatch.setenv(publish.EMAIL_VAR, EMAIL)
    monkeypatch.setenv(publish.TOKEN_VAR, TOKEN)
    ws = _ws(tmp_path, stub, secrets=False)
    _done_run(ws)
    publish.publish_prd(str(ws), "r1")
    expected = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    assert stub.requests[0]["auth"] == f"Basic {expected}"


def test_deleted_page_is_recreated_not_left_stuck(tmp_path, stub):
    ws = _ws(tmp_path, stub)
    _done_run(ws)
    publish.publish_prd(str(ws), "r1")
    stub.pages.clear()  # somebody deleted the page in Confluence
    url = publish.publish_prd(str(ws), "r1")
    assert url.endswith("/pages/2")
    assert prd_flow.load_marker(str(ws), "r1")["published"]["confluence"]["page_id"] == "2"


def test_http_failure_exits_5_with_the_status_and_nothing_else(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub)
    _done_run(ws)
    stub.fail_status = 500
    assert publish.main(["prd", str(ws), "r1"]) == 5
    cap = capsys.readouterr()
    assert "HTTP 500" in cap.err
    _assert_no_secrets(cap)
    assert "published" not in prd_flow.load_marker(str(ws), "r1")


def test_missing_secrets_exits_3_naming_only_the_variables(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub, secrets=False)
    _done_run(ws)
    assert publish.main(["prd", str(ws), "r1"]) == 3
    cap = capsys.readouterr()
    assert publish.EMAIL_VAR in cap.err and publish.TOKEN_VAR in cap.err
    assert not stub.requests


def test_unapproved_run_exits_4_without_touching_the_network(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub)
    _done_run(ws, status="review")
    assert publish.main(["prd", str(ws), "r1"]) == 4
    assert "expected 'done'" in capsys.readouterr().err
    assert not stub.requests


def test_missing_publish_config_exits_2(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub, publish_block=False)
    _done_run(ws)
    assert publish.main(["prd", str(ws), "r1"]) == 2
    assert "publish:" in capsys.readouterr().err
    assert not stub.requests


def test_malformed_confluence_config_exits_2(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub)
    cfg = yaml.safe_load((ws / ".autopilot" / "config.yaml").read_text())
    cfg["publish"]["confluence"].pop("space")
    (ws / ".autopilot" / "config.yaml").write_text(yaml.safe_dump(cfg))
    _done_run(ws)
    assert publish.main(["prd", str(ws), "r1"]) == 2
    assert "publish.confluence.space" in capsys.readouterr().err


def test_unknown_run_exits_4(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub)
    assert publish.main(["prd", str(ws), "nope"]) == 4
    assert "no PRD run" in capsys.readouterr().err


def test_success_prints_the_url_and_no_credential(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub)
    _done_run(ws)
    assert publish.main(["prd", str(ws), "r1"]) == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == f"{stub.base_url}/spaces/PS/pages/1"
    _assert_no_secrets(cap)


def test_publish_writes_only_the_published_block(tmp_path, stub):
    """The ledger ruling: publish.py owns `published` and nothing else in the
    marker prd_flow owns - and verify() hashes artifacts, so publishing can
    never make a run look tampered with."""
    ws = _ws(tmp_path, stub)
    _done_run(ws)
    before = json.loads((ws / "research" / "r1" / "prd-run.json").read_text())
    assert prd_flow.verify(str(ws), "r1") == []

    publish.publish_prd(str(ws), "r1")

    after = json.loads((ws / "research" / "r1" / "prd-run.json").read_text())
    assert set(after) - set(before) == {"published"}
    for key in before:
        assert after[key] == before[key], key
    assert prd_flow.verify(str(ws), "r1") == []


def test_url_in_errors_never_carries_pasted_credentials():
    assert publish._safe_url("https://u:p@wiki.example.com/rest/api/content") == \
        "https://wiki.example.com/rest/api/content"


def test_no_command_prints_usage_and_exits_2(capsys):
    assert publish.main([]) == 2
    assert "publish.py prd" in capsys.readouterr().err
