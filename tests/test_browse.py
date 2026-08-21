"""Tests for engine/browse.py - fetch/screenshot driver with saved
extractors. No test here may touch the network or require playwright: the
extractor tests run on a fixture string, fetch() is monkeypatched wherever
a scraper round-trip needs it, and the playwright-missing path is forced by
monkeypatching browse._sync_playwright rather than relying on whether the
package happens to be installed."""
import json
import sys
from pathlib import Path

ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import browse  # noqa: E402

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
