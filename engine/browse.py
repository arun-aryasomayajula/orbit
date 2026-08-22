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
call, works without it installed OR installed-but-broken - a launch or
navigation failure degrades to the urllib path after one stderr line. Only
screenshot() actually needs it - there is no meaningful stdlib fallback for
rendering a page visually - so the CLI exits 3 with an actionable install
message when it's missing.

Fetches are bounded and public-only: at most MAX_FETCH_BYTES of body text,
and only from a host that resolves entirely to public addresses (see
_check_public_host - the SSRF guard, re-run after redirects).

CLI:
  browse.py fetch <url>
  browse.py screenshot <url> <out>
  browse.py save <target> <name> <url> [--note <text>]
  browse.py rerun <target> <name>

Exit codes:
  2  bad URL scheme, bad scraper name, refused address, unsaved scraper
     name, or missing/invalid CLI arguments
  3  screenshot requested but the playwright package isn't installed
  4  the network or the host failed (DNS, connection, timeout, HTTP error)

Design reference: gstack's /browse skill (its fetch+screenshot QA driver).
"""
import html.parser
import ipaddress
import json
import os
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ALLOWED_SCHEMES = ("http", "https")
SCRAPERS_DIRNAME = os.path.join(".autopilot", "scrapers")
# Same shape as prd_flow.RUN_RE: lowercase letters/digits/hyphens, must start
# alphanumeric. A scraper name becomes a JSON file name under scrapers/, so
# the same restraint on what can land on disk applies here. fullmatch (not
# match) so a trailing newline - "$" matches just before a final "\n" - is
# rejected rather than silently accepted as part of the anchored slug.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
USER_AGENT = "orbit-browse/1.0"
# Hard ceiling on how much of a response body ever reaches a model's context
# (or a scraper's caller). A page that streams gigabytes, or lies about its
# Content-Length, costs at most this much memory and this many tokens.
MAX_FETCH_BYTES = 2_000_000

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


def _address_class(addr):
    """Name the disallowed class `addr` belongs to, or None if it's public.

    Ordered most-specific-first, since the classes overlap: 169.254.169.254
    (the cloud metadata endpoint) is both link-local and private, and
    "link-local" is the more useful name to print."""
    if addr.is_unspecified:
        return "unspecified"
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link-local"
    if addr.is_multicast:
        return "multicast"
    if addr.is_reserved:
        return "reserved"
    if addr.is_private:
        return "private"
    return None


def _check_public_host(url):
    """Refuse a URL whose host resolves to any non-public address.

    The SSRF guard. Left to itself, `fetch` is a confused deputy: whoever
    supplies the URL (a task contract, a page that redirects, an operator
    pasting a link) gets an HTTP GET issued from inside the network the loop
    runs in - reaching a localhost admin port, a private-subnet service, or
    the cloud metadata endpoint at 169.254.169.254 (credentials, in one GET).

    So resolve the host up front and refuse if ANY resolved address is
    disallowed (see _address_class), not just the first: a hostname that
    answers with one public and one loopback address must not be fetchable by
    whichever the connect happens to pick. Re-run after redirects, because a
    public URL can 302 straight into the private range.

    Honest about its limit: this is a resolve-then-connect check, so a DNS
    answer that changes between the two (DNS rebinding) can still slip past.
    Closing that needs connect-time pinning, which urllib does not expose;
    the scheme guard, this check, and MAX_FETCH_BYTES are the defenses in
    depth we have.
    """
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        raise BrowseError(f"no host in URL {url!r}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise BrowseError(f"cannot resolve host {host!r} - {e}") from e
    for info in infos:
        raw = info[4][0]
        try:
            # An IPv6 answer can carry a zone id ("fe80::1%lo0"), which
            # ip_address rejects - the address itself is what we classify.
            addr = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            raise BrowseError(f"cannot classify resolved address {raw!r} for host {host!r}")
        klass = _address_class(addr)
        if klass:
            raise BrowseError(
                f"refusing private/loopback address for host {host!r}: "
                f"{addr} is {klass} - browse only fetches public addresses")
    return url


def _sync_playwright():
    """Import hook, isolated so tests can force the "not installed" path
    (monkeypatching this) without depending on whether playwright actually
    is installed in whatever environment runs the suite."""
    from playwright.sync_api import sync_playwright
    return sync_playwright


def _fetch_playwright(sync_playwright, url, timeout):
    """The rendered path: headless Chromium, then the body's visible text
    capped at MAX_FETCH_BYTES. Chromium follows redirects itself, so the
    landing URL gets the same scheme + address check the urllib path
    applies to resp.url."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, timeout=timeout * 1000)
            _check_url(page.url)
            _check_public_host(page.url)
            return page.inner_text("body")[:MAX_FETCH_BYTES]
        finally:
            browser.close()


def fetch(url, timeout=30):
    """Return the URL's visible text, at most MAX_FETCH_BYTES of it.

    Renders with headless Chromium when playwright is importable AND working
    (JS-rendered pages need it); otherwise a plain HTTP GET run through
    html_to_text(). The host must resolve to public addresses only, before
    the fetch and again after any redirect (see _check_public_host)."""
    _check_url(url)
    _check_public_host(url)
    try:
        sync_playwright = _sync_playwright()
    except ImportError:
        sync_playwright = None
    if sync_playwright is not None:
        try:
            return _fetch_playwright(sync_playwright, url, timeout)
        except BrowseError:
            # A refused address or scheme is a REFUSAL, not a broken renderer -
            # falling back would just re-issue the same forbidden fetch.
            raise
        except Exception as e:
            # Everything else (chromium not downloaded, sandbox denied, a
            # navigation timeout, a crashed browser) is a degraded renderer,
            # not a dead command: say so once and take the stdlib path.
            # Class name only - a playwright error body is multi-line and can
            # echo back the URL, query string and all.
            print(f"browse: playwright render failed ({type(e).__name__}) "
                  "- falling back to urllib", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # CPython's redirect handler follows a 3xx to non-http(s) targets
        # (e.g. ftp://) even though _check_url above only guarded the
        # ORIGINAL url - close that residual by re-checking the scheme the
        # response actually landed on before trusting its bytes. Same for the
        # address: a public URL is free to 302 into the private range.
        _check_url(resp.url)
        _check_public_host(resp.url)
        raw = resp.read(MAX_FETCH_BYTES)
        charset = resp.headers.get_content_charset() or "utf-8"
    return html_to_text(raw.decode(charset, errors="replace"))


def screenshot(url, out_path, timeout=30):
    """Save a full-page PNG screenshot to out_path. Playwright-only.

    No _check_public_host here, deliberately: a screenshot goes to a PNG on
    disk for a human to look at, not into a model's context, and its whole
    point during QA is the app under test on localhost. fetch()'s guard exists
    because fetch()'s OUTPUT is what a URL-supplier could exfiltrate."""
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
    if not NAME_RE.fullmatch(name or ""):
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
    """Read a saved scraper record. A name that was never saved is a CALLER
    mistake (a typo, or `rerun` before `save`), not a network or filesystem
    failure - so it surfaces as BrowseError with the fix in it, rather than a
    raw traceback or an exit code that reads as "the site is down"."""
    try:
        with open(_scraper_path(target, name)) as f:
            return json.load(f)
    except FileNotFoundError as e:
        raise BrowseError(
            f"no scraper named {name!r}; run save first "
            f"(browse.py save <target> {name} <url>)") from e
    except ValueError as e:
        raise BrowseError(f"scraper {name!r} is corrupt ({e}) - re-save it") from e


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
    except (OSError, urllib.error.URLError) as e:
        # The site, the DNS, or the socket - not the caller. One line, its own
        # exit code, no traceback: a skill's Bash call can tell "this URL is
        # unreachable right now" (retry, or record a coverage gap) apart from
        # "this URL is not allowed" (exit 2, never retry). URLError is itself
        # an OSError subclass; both are named so the intent survives a reader.
        print(f"network error fetching: {e}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
