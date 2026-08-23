"""engine/publish.py: markdown->storage rendering, and the two REST adapters
driven against local stub servers (never the network).

The stub deliberately answers errors with a body containing the token and a
marker string, so the "no bodies, no tokens in output" contract is pinned by
capturing stdout/stderr rather than by reading the source.
"""
import base64
import contextlib
import hashlib
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import insight_lint  # noqa: E402
import locks  # noqa: E402
import prd_flow  # noqa: E402
import publish  # noqa: E402

EMAIL = "pm@example.com"
TOKEN = "tok-super-secret"
SECRET_BODY = "SECRET-BODY-MARKER"

GOOD_INSIGHT = """# Checkout drop-off is card OTP

## Answer

Card OTP failures explain 62% (S1) of checkout drop-off, worth INR 4.1cr a
month (S2).

## Basis

- S1: metaql checkout_funnel, 2026-08-01..2026-08-15
- S2: finance export, same window
"""

BAD_INSIGHT = """# Half a finding

## Answer

Something happened.
"""


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
    markdown carries can become live markup - and an unsafe href is not a
    link at all, it is text."""
    out = publish.render_storage(
        "# <b>t</b>\n\n- <i>li</i>\n\n> <em>q</em>\n\n[<u>l</u>](javascript:x)\n")
    assert "<b>" not in out and "<i>" not in out and "<em>q" not in out
    assert "&lt;b&gt;" in out and "&lt;i&gt;li&lt;/i&gt;" in out
    assert "<a " not in out
    assert "<p>[&lt;u&gt;l&lt;/u&gt;](javascript:x)</p>" in out


@pytest.mark.parametrize("href", [
    "javascript:alert(1)", "JavaScript:alert(1)", "  javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD4=", "vbscript:x", "file:///etc/passwd",
])
def test_unsafe_hrefs_never_become_links(href):
    out = publish.render_storage(f"[click]({href})")
    assert "<a " not in out and "href" not in out
    assert "click" in out


@pytest.mark.parametrize("href", [
    "https://x.example/y", "http://x.example/y", "mailto:pm@example.com",
    "/wiki/spaces/PS", "relative/page.md", "#anchor",
])
def test_safe_hrefs_stay_links(href):
    out = publish.render_storage(f"[click]({href})")
    assert out == f'<p><a href="{href}">click</a></p>'


def test_c0_controls_are_dropped_but_tabs_and_newlines_survive():
    """Invisible in a diff and in a review, which is exactly why they do not
    travel to a wiki page. Tab and newline are the two a code block needs."""
    out = publish.render_storage("a\x00b\x1bc\x07d")
    assert out == "<p>abcd</p>"
    assert publish.render_storage("```\nx\ty\nz\n```") == "<pre>x\ty\nz</pre>"
    assert "\x00" not in publish.render_storage("# t\x00\n\n- l\x00\n\n> q\x00")


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

    def _maybe_bad_json(self):
        """A 200 that is not JSON at all - a proxy's interstitial, a login
        page. The parse must fail as an HttpError, not a traceback."""
        if not self.server.bad_json:
            return False
        raw = b"<html>gateway login required</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        return True

    def do_POST(self):
        raw = self._body()
        self._record("POST", raw)
        if self._maybe_fail() or self._maybe_bad_json():
            return
        if self.path == "/rest/api/content":
            self.server.page_seq += 1
            pid = str(self.server.page_seq)
            self.server.pages[pid] = {"version": 1, "body": json.loads(raw)}
            return self._send(200, {"id": pid, "_links": {
                "base": self.server.base_url, "webui": f"/spaces/PS/pages/{pid}"}})
        if self.path == "/rest/api/2/issue":
            # post_delay: widens the concurrent-duplicate test's race window -
            # a second, racing process gets a real chance to reach its own
            # lock acquisition attempt WHILE this response is still pending,
            # rather than the whole exchange completing before it even starts.
            if self.server.post_delay:
                time.sleep(self.server.post_delay)
            self.server.issue_seq += 1
            key = f"ORB-{self.server.issue_seq}"
            self.server.issues[key] = json.loads(raw)
            return self._send(201, {"id": "1", "key": key,
                                    "self": f"{self.server.base_url}/rest/api/2/issue/1"})
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
    srv.requests, srv.pages, srv.issues = [], {}, {}
    srv.page_seq = srv.issue_seq = 0
    srv.fail_status = None
    srv.bad_json = False
    srv.post_delay = 0
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
    (ws / "insights").mkdir()
    cfg = {"repo": "."}
    if publish_block:
        cfg["publish"] = {
            "confluence": {"base_url": stub.base_url, "space": "PS"},
            "jira": {"base_url": stub.base_url, "project": "ORB",
                     "issue_type": "Task"},
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


@contextlib.contextmanager
def _held_by_a_live_process(lock_path, tmp_path):
    """Hold `lock_path` from a real, living process for the duration.

    A separate process, not a thread and not a fake: locks.hold breaks a lock
    whose recorded pid is dead, so contention is only real when the holder is
    alive. This is what a dashboard-launched stage stamping the marker looks
    like from publish.py's side."""
    ready = tmp_path / f"held-{abs(hash(str(lock_path)))}"
    code = (
        "import pathlib, sys, time\n"
        f"sys.path.insert(0, {str(ORBIT / 'engine')!r})\n"
        "import locks\n"
        f"with locks.hold({str(lock_path)!r}, timeout=30):\n"
        f"    pathlib.Path({str(ready)!r}).write_text('held')\n"
        "    time.sleep(120)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        for _ in range(500):
            if ready.exists():
                break
            time.sleep(0.02)
        else:
            pytest.fail("the lock holder subprocess never acquired the lock")
        yield
    finally:
        proc.kill()
        proc.wait(timeout=10)


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


# --------------------------------------------------------------------------
# transport and config hardening
# --------------------------------------------------------------------------

def _set_base_url(ws, channel, url):
    cfg = yaml.safe_load((ws / ".autopilot" / "config.yaml").read_text())
    cfg["publish"][channel]["base_url"] = url
    (ws / ".autopilot" / "config.yaml").write_text(yaml.safe_dump(cfg))


@pytest.mark.parametrize("url", [
    "http://wiki.example.com",          # plain http to a real host
    "wiki.example.com",                 # no scheme at all
    "ftp://wiki.example.com",
    "http://127.0.0.1.evil.example",    # loopback-looking, not loopback
])
def test_insecure_base_url_is_refused_before_any_request(tmp_path, stub, capsys, url):
    ws = _ws(tmp_path, stub)
    _set_base_url(ws, "confluence", url)
    _done_run(ws)
    assert publish.main(["prd", str(ws), "r1"]) == 2
    assert "must be https://" in capsys.readouterr().err
    assert not stub.requests


@pytest.mark.parametrize("url", ["https://wiki.example.com", "http://localhost:8080",
                                 "http://127.0.0.1:9", "http://[::1]:9"])
def test_https_and_loopback_pass_the_scheme_check(url):
    publish._require_secure("confluence", url)  # no raise


def test_malformed_url_is_an_http_error_not_a_traceback():
    """http.client raises ValueError (InvalidURL) for a space or a control
    character, and urllib for a scheme it does not know - both before a socket
    opens. Probed directly, since the config gate now rejects most of them
    first."""
    for url in ("https://wiki example.com/rest/api/content",
                "wiki.example.com/rest/api/content",
                "https://wiki.example.com/rest\nx"):
        with pytest.raises(publish.HttpError) as e:
            publish._request("GET", url, (EMAIL, TOKEN))
        assert "malformed request URL" in str(e.value)
        assert TOKEN not in str(e.value)


def test_space_in_base_url_exits_5_cleanly(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub)
    _set_base_url(ws, "confluence", "https://wiki example.com")
    _done_run(ws)
    assert publish.main(["prd", str(ws), "r1"]) == 5
    cap = capsys.readouterr()
    assert "malformed request URL" in cap.err
    assert "Traceback" not in cap.err
    _assert_no_secrets(cap)


def test_unreachable_host_exits_5_with_the_socket_reason(tmp_path, stub, capsys):
    """The URLError branch: the reason names no URL and carries no credential,
    which is why it is safe to print where a response body is not."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    ws = _ws(tmp_path, stub)
    _set_base_url(ws, "confluence", f"http://127.0.0.1:{dead_port}")
    _done_run(ws)
    assert publish.main(["prd", str(ws), "r1"]) == 5
    cap = capsys.readouterr()
    assert "failed:" in cap.err and "Traceback" not in cap.err
    _assert_no_secrets(cap)


def test_non_json_response_exits_5_without_echoing_the_body(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub)
    _done_run(ws)
    stub.bad_json = True
    assert publish.main(["prd", str(ws), "r1"]) == 5
    cap = capsys.readouterr()
    assert "non-JSON" in cap.err
    assert "gateway login required" not in cap.err
    _assert_no_secrets(cap)


@pytest.mark.parametrize("run", ["../../etc", "R1", "a b", "", ".hidden", "a/b"])
def test_bad_run_id_is_refused_before_any_path_is_built(tmp_path, stub, capsys, run):
    ws = _ws(tmp_path, stub)
    assert publish.main(["prd", str(ws), run]) == 2
    assert "invalid run id" in capsys.readouterr().err
    assert not stub.requests


@pytest.mark.parametrize("stem", ["../../etc/passwd", "a/b", "", ".ssh", "a b"])
def test_bad_insight_stem_is_refused_before_any_path_is_built(tmp_path, stub, capsys, stem):
    ws = _ws(tmp_path, stub)
    assert publish.main(["finding", str(ws), stem]) == 2
    assert "invalid insight stem" in capsys.readouterr().err
    assert not stub.requests


# --------------------------------------------------------------------------
# the record could not be written, but the remote write happened (exit 6)
# --------------------------------------------------------------------------

def test_marker_lock_contention_exits_6_naming_the_published_page(
        tmp_path, stub, capsys, monkeypatch):
    """Exit 4 would say "nothing published, fix and re-run" - and a re-run
    here files a SECOND page. 6 says the page exists and where."""
    monkeypatch.setattr(publish, "LOCK_TIMEOUT", 0.3)
    ws = _ws(tmp_path, stub)
    _done_run(ws)
    with _held_by_a_live_process(prd_flow.marker_lock(str(ws), "r1"), tmp_path):
        assert publish.main(["prd", str(ws), "r1"]) == 6
    cap = capsys.readouterr()
    assert "the page IS published" in cap.err
    assert f"{stub.base_url}/spaces/PS/pages/1" in cap.err
    assert "SECOND page" in cap.err
    _assert_no_secrets(cap)
    # the page really was created, and the marker really was left alone
    assert len([r for r in stub.requests if r["method"] == "POST"]) == 1
    assert "published" not in prd_flow.load_marker(str(ws), "r1")


def test_doc_lock_contention_before_any_post_exits_6_without_filing(
        tmp_path, stub, capsys, monkeypatch):
    """publish_finding now holds `_doc_lock(doc)` across its WHOLE body (the
    fix for the concurrent-duplicate-Jira-issue bug), so a contended lock
    blocks BEFORE the POST ever happens - unlike the old failure mode (lock
    only around the final record write), a genuinely lost race here means
    NOTHING was filed. Exit 6 still applies (a re-run right now would race
    the other writer), but the recovery message is "wait and retry", not
    "reconcile a stray issue by hand"."""
    monkeypatch.setattr(publish, "LOCK_TIMEOUT", 0.3)
    ws = _ws(tmp_path, stub)
    doc = _insight(ws)
    with _held_by_a_live_process(publish._doc_lock(doc), tmp_path):
        assert publish.main(["finding", str(ws), "checkout-otp"]) == 6
    cap = capsys.readouterr()
    assert "could not acquire" in cap.err.lower()
    assert not stub.requests, "no issue should ever have been filed while the lock was contended"
    assert "published: jira" not in doc.read_text()
    _assert_no_secrets(cap)


def test_concurrent_publish_finding_creates_exactly_one_issue(tmp_path, stub):
    """The Important-2 regression test: two publish.py `finding` invocations
    racing the SAME doc, with the stub deliberately delaying its response to
    the Jira POST so the second process has a real window to race in while
    the first is still inside its critical section. Before the fix, holding
    _doc_lock only around the final record write let both processes read
    "not yet published" before either had recorded anything, so both filed
    their own issue - one orphaned. After the fix, the lock is held across
    the whole read-check-POST-record sequence, so exactly one issue is ever
    created and the second process takes the no-op path."""
    ws = _ws(tmp_path, stub)
    _insight(ws)
    stub.post_delay = 0.5

    results = []

    def run():
        r = subprocess.run(
            [sys.executable, str(ORBIT / "engine" / "publish.py"), "finding", str(ws), "checkout-otp"],
            capture_output=True, text=True)
        results.append(r)

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start()
    time.sleep(0.1)   # give t1 a real head start onto the lock
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    posts = [r for r in stub.requests if r["method"] == "POST" and r["path"] == "/rest/api/2/issue"]
    assert len(posts) == 1, f"expected exactly one Jira issue created, got {len(posts)}"
    assert [r.returncode for r in results] == [0, 0], [r.stderr for r in results]
    assert sorted(r.stdout.strip() for r in results) == ["ORB-1", "ORB-1"]
    assert any("already filed" in r.stderr for r in results), \
        "the second (or blocked) process must take the recorded-key no-op path"
    doc_text = (ws / "insights" / "checkout-otp.md").read_text()
    assert doc_text.count("<!-- published: jira") == 1


def test_marker_lock_contention_message_never_leaks_pasted_credentials(tmp_path, stub, monkeypatch):
    """The "page IS published" message interpolates `url` (built from
    cfg['base_url']) - an operator who pasted a base_url with embedded
    userinfo must never see it echoed back in this error, same as every
    other output path in this module routes through _safe_url(). Reached
    directly (not through the network + a real contended lock, which
    urllib can't even connect through with userinfo in the host) by
    monkeypatching _create_page (the "remote write succeeded") and
    _record_published (the "then the lock was contended") separately."""
    ws = _ws(tmp_path, stub)
    _set_base_url(ws, "confluence", "https://leaked-user:leaked-pass@wiki.example.com")
    _done_run(ws)
    leaked_url = "https://leaked-user:leaked-pass@wiki.example.com/spaces/PS/pages/1"
    monkeypatch.setattr(publish, "_create_page", lambda cfg, block, title, storage, auth: ("1", leaked_url))

    def boom(*a, **kw):
        raise locks.LockTimeout("contended")

    monkeypatch.setattr(publish, "_record_published", boom)
    with pytest.raises(publish.LockError) as exc:
        publish.publish_prd(str(ws), "r1")
    msg = str(exc.value)
    assert "the page IS published" in msg
    assert "leaked-user" not in msg and "leaked-pass" not in msg


def test_doc_lock_contention_message_never_leaks_pasted_credentials(tmp_path, stub, monkeypatch):
    """Same guarantee for publish_finding's "issue IS filed" LockError
    (the "another writer recorded a different key" branch) - reached
    directly by monkeypatching _request (the successful POST) and
    _append_record (the lost race) rather than a real network round trip."""
    ws = _ws(tmp_path, stub)
    _set_base_url(ws, "jira", "https://leaked-user:leaked-pass@jira.example.com")
    _insight(ws)
    monkeypatch.setattr(publish, "_request", lambda *a, **kw: {"key": "ORB-1"})
    monkeypatch.setattr(publish, "_append_record", lambda doc, key: False)
    with pytest.raises(publish.LockError) as exc:
        publish.publish_finding(str(ws), "checkout-otp")
    msg = str(exc.value)
    assert "ORB-1 IS filed" in msg
    assert "leaked-user" not in msg and "leaked-pass" not in msg


def test_prd_flow_and_publish_share_one_marker_lock(tmp_path, stub, monkeypatch):
    """One lock, not two names guarding the same file: publish.py takes
    prd_flow's, so a stamp landing mid-publish cannot be lost. Held from
    outside, BOTH modules must queue on it."""
    monkeypatch.setattr(publish, "LOCK_TIMEOUT", 0.3)
    monkeypatch.setattr(prd_flow, "MARKER_LOCK_TIMEOUT", 0.3)
    ws = _ws(tmp_path, stub)
    _done_run(ws)
    lock = prd_flow.marker_lock(str(ws), "r1")
    assert lock == str(ws / "research" / "r1" / ".marker.lock")
    with _held_by_a_live_process(lock, tmp_path):
        with pytest.raises(locks.LockTimeout):
            prd_flow.record_stage(str(ws), "r1", "intake")
        with pytest.raises(locks.LockTimeout):
            publish._record_published(str(ws), "r1", "confluence", {"page_id": "9"})


# --------------------------------------------------------------------------
# Jira
# --------------------------------------------------------------------------

def _insight(ws, stem="checkout-otp", text=GOOD_INSIGHT):
    p = ws / "insights" / f"{stem}.md"
    p.write_text(text)
    return p


def test_finding_files_an_issue_with_answer_basis_and_path(tmp_path, stub):
    ws = _ws(tmp_path, stub)
    doc = _insight(ws)
    key, created = publish.publish_finding(str(ws), "checkout-otp")
    assert (key, created) == ("ORB-1", True)

    posts = [r for r in stub.requests if r["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["path"] == "/rest/api/2/issue"
    fields = posts[0]["json"]["fields"]
    assert fields["project"] == {"key": "ORB"}
    assert fields["issuetype"] == {"name": "Task"}
    assert fields["summary"] == "Checkout drop-off is card OTP"
    assert "62% (S1)" in fields["description"]
    assert "S1: metaql checkout_funnel" in fields["description"]
    assert "insights/checkout-otp.md" in fields["description"]
    assert doc.read_text().rstrip().endswith("<!-- published: jira ORB-1 -->")


def test_the_record_comment_keeps_the_doc_lint_clean(tmp_path, stub):
    """The gate that let the finding out must still pass afterwards, or the
    next cycle's insight_lint run fails on a doc publishing itself broke."""
    ws = _ws(tmp_path, stub)
    doc = _insight(ws)
    assert insight_lint.lint_file(doc) == []
    publish.publish_finding(str(ws), "checkout-otp")
    assert insight_lint.lint_file(doc) == []


def test_refiling_no_ops_and_prints_the_recorded_key(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub)
    _insight(ws)
    publish.publish_finding(str(ws), "checkout-otp")
    before = len(stub.requests)

    assert publish.main(["finding", str(ws), "checkout-otp"]) == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == "ORB-1"
    assert "already filed" in cap.err
    assert len(stub.requests) == before
    _assert_no_secrets(cap)


def test_lint_failure_blocks_the_filing(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub)
    doc = _insight(ws, stem="half", text=BAD_INSIGHT)
    assert publish.main(["finding", str(ws), "half"]) == 4
    err = capsys.readouterr().err
    assert "insight_lint failed" in err and "Basis" in err
    assert not stub.requests
    assert "published: jira" not in doc.read_text()


def test_missing_insight_doc_exits_4(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub)
    assert publish.main(["finding", str(ws), "ghost"]) == 4
    assert "no insight doc" in capsys.readouterr().err
    assert not stub.requests


def test_finding_http_failure_exits_5_and_leaves_no_record(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub)
    doc = _insight(ws)
    stub.fail_status = 502
    assert publish.main(["finding", str(ws), "checkout-otp"]) == 5
    cap = capsys.readouterr()
    assert "HTTP 502" in cap.err
    _assert_no_secrets(cap)
    assert "published: jira" not in doc.read_text()


def test_finding_missing_secrets_exits_3(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub, secrets=False)
    _insight(ws)
    assert publish.main(["finding", str(ws), "checkout-otp"]) == 3
    assert publish.TOKEN_VAR in capsys.readouterr().err
    assert not stub.requests


def test_finding_success_prints_only_the_key(tmp_path, stub, capsys):
    ws = _ws(tmp_path, stub)
    _insight(ws)
    assert publish.main(["finding", str(ws), "checkout-otp"]) == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == "ORB-1"
    _assert_no_secrets(cap)


def test_record_write_does_not_lose_a_concurrent_edit(tmp_path, stub, monkeypatch):
    """The doc is a file a human or an agent may be editing right now. The
    record write re-reads INSIDE the lock, so an edit that lands while we are
    on the network survives; a write built on the pre-request text would
    silently throw it away."""
    ws = _ws(tmp_path, stub)
    doc = _insight(ws)
    real_request = publish._request

    def edit_then_request(*a, **kw):
        doc.write_text(doc.read_text() + "\n- S3: added while filing\n")
        return real_request(*a, **kw)

    monkeypatch.setattr(publish, "_request", edit_then_request)
    key, created = publish.publish_finding(str(ws), "checkout-otp")

    text = doc.read_text()
    assert created and "S3: added while filing" in text
    assert text.rstrip().endswith(f"<!-- published: jira {key} -->")
    assert insight_lint.lint_file(doc) == []


def test_a_key_recorded_by_someone_else_first_is_reported_not_swallowed(
        tmp_path, stub, capsys, monkeypatch):
    """Our issue exists and the doc points at someone else's. Exit 0 would
    hide an orphan; the message says which key nothing points to."""
    ws = _ws(tmp_path, stub)
    doc = _insight(ws)
    real_request = publish._request

    def race_then_request(*a, **kw):
        doc.write_text(doc.read_text().rstrip() + "\n\n<!-- published: jira ORB-99 -->\n")
        return real_request(*a, **kw)

    monkeypatch.setattr(publish, "_request", race_then_request)
    assert publish.main(["finding", str(ws), "checkout-otp"]) == 6
    err = capsys.readouterr().err
    assert "ORB-1 IS filed" in err and "nothing points to" in err
    assert "ORB-99" in doc.read_text() and "ORB-1 -->" not in doc.read_text()


def test_record_write_keeps_the_docs_permissions(tmp_path, stub):
    """mkstemp makes 0600 files; a published finding must not turn into one."""
    ws = _ws(tmp_path, stub)
    doc = _insight(ws)
    doc.chmod(0o644)
    publish.publish_finding(str(ws), "checkout-otp")
    assert doc.stat().st_mode & 0o777 == 0o644


def test_record_write_is_atomic(tmp_path, stub, monkeypatch):
    """mkstemp + os.replace: a crash mid-write must not leave a truncated
    insight doc where a whole one used to be."""
    ws = _ws(tmp_path, stub)
    doc = _insight(ws)
    original = doc.read_text()

    def boom(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(publish.os, "replace", boom)
    with pytest.raises(OSError):
        publish._append_record(doc, "ORB-9")
    assert doc.read_text() == original
    assert not list((ws / "insights").glob("*.tmp"))


def test_a_doc_that_merely_quotes_the_record_is_not_published(tmp_path, stub):
    """The regex is anchored to a whole line. A runbook explaining the syntax
    mid-sentence must still file, or documenting the feature disables it."""
    ws = _ws(tmp_path, stub)
    text = GOOD_INSIGHT.replace(
        "Something happened.", "x").rstrip() + (
        "\n\nA published doc carries <!-- published: jira ORB-7 --> at its end.\n")
    _insight(ws, text=text)
    key, created = publish.publish_finding(str(ws), "checkout-otp")
    assert (key, created) == ("ORB-1", True)


def test_section_matches_the_whole_heading_line(tmp_path, stub):
    """`## Answers to reviewers` is not `## Answer`; a prefix match would file
    the wrong half of the doc as the finding."""
    text = ("# T\n\n## Answers to reviewers\n\nnot this (S1)\n\n"
            "## Answer\n\nthis one (S1)\n\n## Basis\n\n- S1: a source\n")
    ws = _ws(tmp_path, stub)
    _insight(ws, text=text)
    publish.publish_finding(str(ws), "checkout-otp")
    desc = stub.requests[0]["json"]["fields"]["description"]
    assert "this one (S1)" in desc and "not this" not in desc


def test_no_command_prints_usage_and_exits_2(capsys):
    assert publish.main([]) == 2
    assert "publish.py prd" in capsys.readouterr().err
