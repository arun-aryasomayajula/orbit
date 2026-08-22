"""Tests for engine/browse.py - fetch/screenshot driver with saved
extractors. No test here may touch the network or require playwright: the
extractor tests run on a fixture string, fetch() is monkeypatched wherever
a scraper round-trip needs it, the playwright paths are forced by
monkeypatching browse._sync_playwright rather than relying on whether the
package happens to be installed, and name resolution is stubbed for every
test (see the autouse _stub_dns fixture) so the SSRF guard never issues a
real DNS query."""
import json
import socket
import sys
from pathlib import Path

import pytest

ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import browse  # noqa: E402

PUBLIC_IP = "93.184.216.34"


def _addrinfo(ip):
    """One getaddrinfo() 5-tuple list, shaped the way the guard reads it."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """Every test resolves to a public address unless it says otherwise.

    fetch() now resolves the host before connecting, so without this the
    suite would depend on real DNS (and on example.com staying public).
    Tests that exercise the guard itself override this per-case."""
    monkeypatch.setattr(browse.socket, "getaddrinfo",
                        lambda host, port, *a, **kw: _addrinfo(PUBLIC_IP))


class _FakeHeaders:
    def get_content_charset(self):
        return None


class _FakeResponse:
    """Stands in for what urllib.request.urlopen() returns: a context
    manager with .url (the URL actually reached, post-redirect), .headers,
    and .read(n) - which honors the byte cap fetch() passes it, the way a
    real HTTPResponse does."""

    def __init__(self, url, body):
        self.url = url
        self.headers = _FakeHeaders()
        self._body = body

    def read(self, amt=None):
        return self._body if amt is None else self._body[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

FIXTURE_HTML = """<html><head>
<style>body { color: red; }</style>
<script>console.log("hi");</script>
</head>
<body>
<h1>Title</h1>
<p>First paragraph.</p>
<p>Second paragraph with <b>bold</b> text.</p>
<div><p>Nested</p></div>
<script>tracked();</script>
</body></html>"""


# --- html_to_text extractor -------------------------------------------------

def test_html_to_text_strips_script_and_style_and_splits_blocks():
    text = browse.html_to_text(FIXTURE_HTML)
    assert "color: red" not in text
    assert "console.log" not in text
    assert "tracked()" not in text
    assert text == (
        "Title\n\n"
        "First paragraph.\n\n"
        "Second paragraph with bold text.\n\n"
        "Nested"
    )


def test_html_to_text_collapses_blank_line_runs():
    # A run of many block-boundary newlines between two paragraphs must
    # collapse to a single blank line, not survive as several.
    text = browse.html_to_text("<div><div><div><p>A</p></div></div></div><p>B</p>")
    assert "\n\n\n" not in text
    assert text == "A\n\nB"


def test_html_to_text_handles_entities_via_convert_charrefs():
    text = browse.html_to_text("<p>Tom &amp; Jerry</p>")
    assert text == "Tom & Jerry"


def test_html_to_text_empty_input_is_empty_string():
    assert browse.html_to_text("") == ""
    assert browse.html_to_text("<script>only script</script>") == ""


# --- urllib fallback: user-agent + post-redirect scheme check --------------

def test_fetch_urllib_fallback_sends_custom_user_agent(monkeypatch):
    monkeypatch.setattr(browse, "_sync_playwright",
                         lambda: (_ for _ in ()).throw(ImportError("no playwright")))
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeResponse("https://example.com/x", b"<p>hi</p>")

    monkeypatch.setattr(browse.urllib.request, "urlopen", fake_urlopen)
    text = browse.fetch("https://example.com/x")
    assert isinstance(captured["req"], browse.urllib.request.Request)
    # Request.add_header() stores the key as key.capitalize() ("User-agent",
    # not "User-Agent") and get_header() does no case-normalization of its
    # own, so the lookup has to use the stored casing.
    assert captured["req"].get_header("User-agent") == browse.USER_AGENT
    assert text == "hi"


def test_fetch_rejects_redirect_to_non_http_scheme(monkeypatch):
    # CPython's redirect handler will itself follow a 3xx to ftp:// - the
    # original-url scheme guard cannot catch that, so fetch() must recheck
    # the scheme the response actually landed on (resp.url) before trusting
    # its bytes.
    monkeypatch.setattr(browse, "_sync_playwright",
                         lambda: (_ for _ in ()).throw(ImportError("no playwright")))
    monkeypatch.setattr(browse.urllib.request, "urlopen",
                         lambda req, timeout=None: _FakeResponse(
                             "ftp://evil.example.com/payload", b"<p>hi</p>"))
    try:
        browse.fetch("https://example.com/x")
        assert False, "expected BrowseError"
    except browse.BrowseError as e:
        assert "ftp" in str(e)


# --- URL scheme guard -------------------------------------------------------

def test_fetch_refuses_file_scheme():
    try:
        browse.fetch("file:///etc/passwd")
        assert False, "expected BrowseError"
    except browse.BrowseError as e:
        assert "file" in str(e)


def test_save_refuses_file_scheme(tmp_path):
    try:
        browse.save(str(tmp_path), "x", "file:///etc/passwd")
        assert False, "expected BrowseError"
    except browse.BrowseError:
        pass


def test_cli_fetch_bad_scheme_exits_2():
    assert browse.main(["fetch", "ftp://example.com"]) == 2


def test_cli_unknown_command_exits_2():
    assert browse.main(["bogus"]) == 2


def test_cli_no_args_exits_2():
    assert browse.main([]) == 2


# --- scraper save/rerun round-trip (fetch monkeypatched, no network) -------

def test_save_writes_atomic_json(tmp_path):
    record = browse.save(str(tmp_path), "my-scraper", "https://example.com/x", note="hi")
    path = tmp_path / ".autopilot" / "scrapers" / "my-scraper.json"
    assert path.is_file()
    on_disk = json.loads(path.read_text())
    assert on_disk == record
    assert on_disk["name"] == "my-scraper"
    assert on_disk["url"] == "https://example.com/x"
    assert on_disk["note"] == "hi"
    assert "created" in on_disk


def test_save_rejects_invalid_name(tmp_path):
    try:
        browse.save(str(tmp_path), "Not-A-Slug", "https://example.com")
        assert False, "expected BrowseError"
    except browse.BrowseError:
        pass
    try:
        browse.save(str(tmp_path), "-leading-hyphen", "https://example.com")
        assert False, "expected BrowseError"
    except browse.BrowseError:
        pass


def test_name_re_rejects_trailing_newline():
    # .match() would have accepted this ("$" matches just before a
    # trailing "\n"); fullmatch must not.
    assert browse.NAME_RE.fullmatch("abc\n") is None


def test_save_rejects_name_with_trailing_newline(tmp_path):
    try:
        browse.save(str(tmp_path), "abc\n", "https://example.com")
        assert False, "expected BrowseError"
    except browse.BrowseError:
        pass


def test_save_then_rerun_round_trip(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(url, timeout=30):
        calls.append(url)
        return f"fetched:{url}"

    monkeypatch.setattr(browse, "fetch", fake_fetch)
    browse.save(str(tmp_path), "acme", "https://example.com/acme")
    result = browse.rerun(str(tmp_path), "acme")
    assert result == "fetched:https://example.com/acme"
    assert calls == ["https://example.com/acme"]


def test_cli_save_then_rerun_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(browse, "fetch", lambda url, timeout=30: f"fetched:{url}")
    rc = browse.main(["save", str(tmp_path), "acme", "https://example.com/acme", "--note", "test note"])
    assert rc == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["note"] == "test note"

    rc = browse.main(["rerun", str(tmp_path), "acme"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "fetched:https://example.com/acme"


# --- screenshot without playwright -------------------------------------------

def test_screenshot_without_playwright_raises(monkeypatch):
    def missing():
        raise ImportError("no playwright")

    monkeypatch.setattr(browse, "_sync_playwright", missing)
    try:
        browse.screenshot("https://example.com", "/tmp/does-not-matter.png")
        assert False, "expected PlaywrightMissing"
    except browse.PlaywrightMissing as e:
        assert "playwright" in str(e)
        assert "pip install playwright" in str(e)


def test_cli_screenshot_without_playwright_exits_3(monkeypatch):
    def missing():
        raise ImportError("no playwright")

    monkeypatch.setattr(browse, "_sync_playwright", missing)
    assert browse.main(["screenshot", "https://example.com", "/tmp/does-not-matter.png"]) == 3


def test_cli_screenshot_bad_scheme_exits_2_before_playwright_check(monkeypatch):
    # The scheme guard runs first, so this must not even reach the
    # playwright import - no monkeypatch needed to keep it off the network.
    assert browse.main(["screenshot", "file:///etc/passwd", "/tmp/x.png"]) == 2


# --- SSRF guard: only public addresses are fetchable ------------------------

def _no_playwright(monkeypatch):
    monkeypatch.setattr(browse, "_sync_playwright",
                        lambda: (_ for _ in ()).throw(ImportError("no playwright")))


def _refuse_or_fail(url):
    try:
        browse.fetch(url)
    except browse.BrowseError as e:
        return str(e)
    raise AssertionError(f"expected BrowseError for {url}")


def test_fetch_refuses_loopback_literal(monkeypatch):
    # A literal address needs no DNS at all - the guard fires on the parsed
    # address, before any socket is opened.
    monkeypatch.setattr(browse.socket, "getaddrinfo",
                        lambda host, port, *a, **kw: _addrinfo("127.0.0.1"))
    msg = _refuse_or_fail("http://127.0.0.1:1/")
    assert "refusing private/loopback address" in msg
    assert "loopback" in msg


def test_fetch_refuses_cloud_metadata_endpoint(monkeypatch):
    monkeypatch.setattr(browse.socket, "getaddrinfo",
                        lambda host, port, *a, **kw: _addrinfo("169.254.169.254"))
    msg = _refuse_or_fail("http://169.254.169.254/latest/meta-data/iam/")
    assert "refusing private/loopback address" in msg
    assert "link-local" in msg          # the more specific class, not "private"


def test_fetch_refuses_hostname_that_resolves_private(monkeypatch):
    """The interesting case: a perfectly public-looking NAME whose A record
    points inside the network."""
    monkeypatch.setattr(browse.socket, "getaddrinfo",
                        lambda host, port, *a, **kw: _addrinfo("10.1.2.3"))
    msg = _refuse_or_fail("https://internal.example.com/admin")
    assert "private" in msg
    assert "internal.example.com" in msg


def test_fetch_refuses_when_any_resolved_address_is_private(monkeypatch):
    """Split-horizon DNS answering with one public and one loopback address:
    refuse, since which one the connect picks is not ours to choose."""
    monkeypatch.setattr(browse.socket, "getaddrinfo",
                        lambda host, port, *a, **kw:
                        _addrinfo(PUBLIC_IP) + _addrinfo("127.0.0.1"))
    assert "loopback" in _refuse_or_fail("https://split.example.com/")


def test_fetch_refuses_redirect_into_the_private_range(monkeypatch):
    """A public URL is free to 302 anywhere - so the address check has to run
    again on the URL the response actually landed on."""
    _no_playwright(monkeypatch)
    seen = []

    def fake_getaddrinfo(host, port, *a, **kw):
        seen.append(host)
        return _addrinfo("127.0.0.1" if host == "localhost" else PUBLIC_IP)

    monkeypatch.setattr(browse.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(browse.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(
                            "http://localhost:8080/admin", b"<p>secrets</p>"))
    assert "loopback" in _refuse_or_fail("https://public.example.com/r")
    assert seen == ["public.example.com", "localhost"]      # checked both ends


def test_fetch_refuses_unresolvable_host(monkeypatch):
    _no_playwright(monkeypatch)

    def boom(host, port, *a, **kw):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(browse.socket, "getaddrinfo", boom)
    assert "cannot resolve host" in _refuse_or_fail("https://nope.invalid/")


def test_cli_fetch_private_address_exits_2(monkeypatch, capsys):
    # No listener needed: the guard fires on the resolved address, so nothing
    # is ever connected to. (Real getaddrinfo returns a literal unchanged and
    # issues no DNS query - it is stubbed here only because the autouse
    # fixture already replaced it.)
    monkeypatch.setattr(browse.socket, "getaddrinfo",
                        lambda host, port, *a, **kw: _addrinfo("127.0.0.1"))
    assert browse.main(["fetch", "http://127.0.0.1:1/"]) == 2
    assert "refusing private/loopback address" in capsys.readouterr().err


def test_address_class_names_public_addresses_none():
    import ipaddress
    assert browse._address_class(ipaddress.ip_address(PUBLIC_IP)) is None
    assert browse._address_class(ipaddress.ip_address("2606:2800:220:1::")) is None


# --- response size cap ------------------------------------------------------

def test_fetch_caps_the_response_at_max_fetch_bytes(monkeypatch):
    _no_playwright(monkeypatch)
    oversized = b"<p>" + b"x" * (browse.MAX_FETCH_BYTES * 2)
    captured = {}

    class _Counting(_FakeResponse):
        def read(self, amt=None):
            captured["amt"] = amt
            return super().read(amt)

    monkeypatch.setattr(browse.urllib.request, "urlopen",
                        lambda req, timeout=None: _Counting("https://example.com/big", oversized))
    text = browse.fetch("https://example.com/big")
    assert captured["amt"] == browse.MAX_FETCH_BYTES
    assert len(text) <= browse.MAX_FETCH_BYTES


def test_playwright_text_is_capped(monkeypatch):
    monkeypatch.setattr(browse, "_sync_playwright",
                        lambda: _fake_playwright("y" * (browse.MAX_FETCH_BYTES * 2)))
    text = browse.fetch("https://example.com/big")
    assert len(text) == browse.MAX_FETCH_BYTES


# --- playwright present but broken: degrade, never die ----------------------

class _FakePage:
    def __init__(self, text, url, goto_error=None):
        self._text = text
        self.url = url
        self._goto_error = goto_error

    def goto(self, url, timeout=None):
        if self._goto_error:
            raise self._goto_error

    def inner_text(self, selector):
        return self._text


class _FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, page, launch_error=None):
        self._page = page
        self._launch_error = launch_error
        self.browser = None

    def launch(self):
        if self._launch_error:
            raise self._launch_error
        self.browser = _FakeBrowser(self._page)
        return self.browser


class _FakePlaywrightCtx:
    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _fake_playwright(text="rendered", url="https://example.com/x",
                     launch_error=None, goto_error=None):
    """A stand-in for playwright.sync_api.sync_playwright: browse calls the
    returned object, so hand back a factory."""
    chromium = _FakeChromium(_FakePage(text, url, goto_error), launch_error)
    return lambda: _FakePlaywrightCtx(chromium)


def test_fetch_uses_playwright_when_it_works(monkeypatch):
    monkeypatch.setattr(browse, "_sync_playwright", lambda: _fake_playwright("rendered body"))
    assert browse.fetch("https://example.com/x") == "rendered body"


def test_fetch_falls_back_when_launch_fails(monkeypatch, capsys):
    """playwright imports fine but chromium was never downloaded (or the
    sandbox refuses it). One stderr line, then the urllib path - not an
    exception out of a command whose whole job is "get me this page"."""
    monkeypatch.setattr(browse, "_sync_playwright",
                        lambda: _fake_playwright(launch_error=RuntimeError("Executable doesn't exist")))
    monkeypatch.setattr(browse.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse("https://example.com/x", b"<p>fallback</p>"))
    assert browse.fetch("https://example.com/x") == "fallback"
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "falling back to urllib" in err[0]
    assert "RuntimeError" in err[0]


def test_fetch_falls_back_when_navigation_fails(monkeypatch, capsys):
    monkeypatch.setattr(browse, "_sync_playwright",
                        lambda: _fake_playwright(goto_error=TimeoutError("navigation timeout")))
    monkeypatch.setattr(browse.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse("https://example.com/x", b"<p>fallback</p>"))
    assert browse.fetch("https://example.com/x") == "fallback"
    assert "falling back to urllib" in capsys.readouterr().err


def test_playwright_refusal_is_not_a_fallback(monkeypatch):
    """A landing URL in the private range is a REFUSAL. Falling back would
    just re-issue the forbidden fetch through urllib."""
    monkeypatch.setattr(browse, "_sync_playwright",
                        lambda: _fake_playwright(url="http://localhost:8080/admin"))

    def fake_getaddrinfo(host, port, *a, **kw):
        return _addrinfo("127.0.0.1" if host == "localhost" else PUBLIC_IP)

    monkeypatch.setattr(browse.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(browse.urllib.request, "urlopen",
                        lambda req, timeout=None: pytest.fail("must not fall back"))
    assert "loopback" in _refuse_or_fail("https://public.example.com/r")


# --- rerun on a name that was never saved -----------------------------------

def test_rerun_unsaved_name_raises_browse_error(tmp_path):
    try:
        browse.rerun(str(tmp_path), "never-saved")
        assert False, "expected BrowseError"
    except browse.BrowseError as e:
        assert "no scraper named 'never-saved'" in str(e)
        assert "run save first" in str(e)


def test_cli_rerun_unsaved_name_exits_2(tmp_path, capsys):
    assert browse.main(["rerun", str(tmp_path), "never-saved"]) == 2
    assert "run save first" in capsys.readouterr().err


def test_cli_rerun_corrupt_scraper_exits_2(tmp_path):
    p = tmp_path / ".autopilot" / "scrapers"
    p.mkdir(parents=True)
    (p / "broken.json").write_text("{not json")
    assert browse.main(["rerun", str(tmp_path), "broken"]) == 2


# --- network failures are their own exit code -------------------------------

def test_cli_fetch_network_error_exits_4(monkeypatch, capsys):
    _no_playwright(monkeypatch)

    def boom(req, timeout=None):
        raise browse.urllib.error.URLError("connection refused")

    monkeypatch.setattr(browse.urllib.request, "urlopen", boom)
    assert browse.main(["fetch", "https://example.com/x"]) == 4
    assert "network error" in capsys.readouterr().err


def test_cli_fetch_http_error_exits_4(monkeypatch):
    _no_playwright(monkeypatch)

    def boom(req, timeout=None):
        raise browse.urllib.error.HTTPError("https://example.com/x", 503, "Down", {}, None)

    monkeypatch.setattr(browse.urllib.request, "urlopen", boom)
    assert browse.main(["fetch", "https://example.com/x"]) == 4


def test_user_agent_carries_no_personal_url():
    assert browse.USER_AGENT == "orbit-browse/1.0"
