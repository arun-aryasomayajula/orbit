#!/usr/bin/env python3
"""browse.py - minimal fetch/screenshot driver with saved extractors.

Fetches a URL's visible text (a real Playwright-driven headless Chromium
render when it's installed - JS-rendered pages need one - else a plain
urllib GET run through a stdlib HTML-to-text extractor) and, when
Playwright is available, saves a full-page screenshot. Fetches worth
repeating are stashed as named "scrapers" under
`<target>/.autopilot/scrapers/<name>.json` so a later `rerun` re-fetches
the same URL without re-typing it.

playwright is strictly OPTIONAL: importing this module, and every fetch()
call, works without it installed. Only screenshot() actually needs it -
there is no meaningful stdlib fallback for rendering a page visually - so
the CLI exits 3 with an actionable install message when it's missing.

CLI:
  browse.py fetch <url>
  browse.py screenshot <url> <out>
  browse.py save <target> <name> <url> [--note <text>]
  browse.py rerun <target> <name>

Exit codes:
  2  bad URL scheme, bad scraper name, or missing/invalid CLI arguments
  3  screenshot requested but the playwright package isn't installed

Design reference: gstack's /browse skill (its fetch+screenshot QA driver).
"""
import html.parser
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ALLOWED_SCHEMES = ("http", "https")
SCRAPERS_DIRNAME = os.path.join(".autopilot", "scrapers")
# Same shape as prd_flow.RUN_RE: lowercase letters/digits/hyphens, must start
# alphanumeric. A scraper name becomes a JSON file name under scrapers/, so
# the same restraint on what can land on disk applies here.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "tr", "br", "section", "article"}
SKIP_TAGS = {"script", "style", "noscript"}


class BrowseError(ValueError):
    """Bad URL scheme, bad scraper name, or other caller-input problem."""


class PlaywrightMissing(RuntimeError):
    """screenshot() needs the optional playwright dependency and it isn't
    installed."""


class _TextExtractor(html.parser.HTMLParser):
    """script/style/noscript content is dropped entirely; block elements
    (see BLOCK_TAGS) turn into line breaks; everything else's text is kept
    as-is and normalized once at the end (see html_to_text)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
        elif self._skip_depth == 0 and tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self):
        raw = "".join(self._chunks)
        lines = [line.strip() for line in raw.splitlines()]
        out = []
        blank = True  # starts True so leading blank lines are dropped too
        for line in lines:
            if line:
                out.append(line)
                blank = False
            elif not blank:
                out.append("")
                blank = True
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out)


def html_to_text(markup):
    """Stdlib fallback extractor: strip script/style/noscript, turn block
    elements into newlines, collapse runs of blank lines. Entity handling
    comes from HTMLParser's convert_charrefs default."""
    extractor = _TextExtractor()
    extractor.feed(markup)
    extractor.close()
    return extractor.text()


def _check_url(url):
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BrowseError(
            f"unsupported URL scheme {scheme!r} - only http/https are allowed")
    return url


def _sync_playwright():
    """Import hook, isolated so tests can force the "not installed" path
    (monkeypatching this) without depending on whether playwright actually
    is installed in whatever environment runs the suite."""
    from playwright.sync_api import sync_playwright
    return sync_playwright


def fetch(url, timeout=30):
    """Return the URL's visible text. Renders with headless Chromium when
    playwright is installed (JS-rendered pages need it); otherwise a plain
    HTTP GET run through html_to_text()."""
    _check_url(url)
    try:
        sync_playwright = _sync_playwright()
    except ImportError:
        sync_playwright = None
    if sync_playwright is not None:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(url, timeout=timeout * 1000)
                return page.inner_text("body")
            finally:
                browser.close()
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    return html_to_text(raw.decode(charset, errors="replace"))


def screenshot(url, out_path, timeout=30):
    """Save a full-page PNG screenshot to out_path. Playwright-only."""
    _check_url(url)
    try:
        sync_playwright = _sync_playwright()
    except ImportError as e:
        raise PlaywrightMissing(
            "screenshot requires the optional playwright dependency "
            "(pip install playwright && playwright install chromium)") from e
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, timeout=timeout * 1000)
            page.screenshot(path=out_path, full_page=True)
        finally:
            browser.close()


def _scrapers_dir(target):
    return os.path.join(target, SCRAPERS_DIRNAME)


def _scraper_path(target, name):
    return os.path.join(_scrapers_dir(target), f"{name}.json")


def _atomic_write_json(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".browse-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(target, name, url, note=""):
    """Save a named scraper - {name, url, note, created} - to
    `<target>/.autopilot/scrapers/<name>.json` (atomic write; dir created on
    first save). `name` is slug-validated the same way prd_flow validates a
    run id, since it becomes a file name on disk."""
    if not NAME_RE.match(name or ""):
        raise BrowseError(
            f"invalid scraper name {name!r} - lowercase letters, digits and "
            "hyphens only, must start alphanumeric")
    _check_url(url)
    record = {
        "name": name,
        "url": url,
        "note": note or "",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _atomic_write_json(_scraper_path(target, name), record)
    return record


def load_scraper(target, name):
    with open(_scraper_path(target, name)) as f:
        return json.load(f)


def rerun(target, name, timeout=30):
    """Load a saved scraper and fetch() its URL again."""
    record = load_scraper(target, name)
    return fetch(record["url"], timeout=timeout)


def _cli_fetch(rest):
    print(fetch(rest[0]))


def _cli_screenshot(rest):
    screenshot(rest[0], rest[1])


def _cli_save(rest):
    target, name, url = rest[0], rest[1], rest[2]
    note = rest[rest.index("--note") + 1] if "--note" in rest else ""
    print(json.dumps(save(target, name, url, note=note), indent=2))


def _cli_rerun(rest):
    print(rerun(rest[0], rest[1]))


CMDS = {
    "fetch": _cli_fetch,
    "screenshot": _cli_screenshot,
    "save": _cli_save,
    "rerun": _cli_rerun,
}


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd not in CMDS:
        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    try:
        CMDS[cmd](rest)
    except PlaywrightMissing as e:
        print(str(e), file=sys.stderr)
        return 3
    except BrowseError as e:
        print(str(e), file=sys.stderr)
        return 2
    except IndexError:
        print(f"missing argument for '{cmd}' - see browse.py with no args", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
