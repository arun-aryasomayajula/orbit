#!/usr/bin/env python3
"""Publish approved artifacts out of an Orbit product workspace: an approved
PRD to a Confluence page, a lint-clean finding to a Jira issue.

REST, not MCP (spec decision 10 + the M5 ledger ruling): the office network
that these workspaces run on blocks mcp.atlassian.com, and the PM's tokens
already live in `.autopilot/.secrets.env`. So this module speaks plain
urllib to the two REST APIs, reading `ATLASSIAN_EMAIL`/`ATLASSIAN_API_TOKEN`
via engine/secrets_env.py (the secrets file wins over an inherited env var).
Mirroring engine/mcp.py's posture: a token is never a value this module
prints, logs, or puts in a URL - and no response body ever reaches stdout or
stderr either, because an Atlassian error body can echo the request that
carried the Authorization header. HTTP failures surface as the status code
and nothing else.

Publishing is GATED and IDEMPOTENT, never a duplicate-maker:
  - a PRD publishes only from marker `status: done` (the human sign-off gate
    in prd_flow), and re-publishing UPDATES the page recorded in the marker's
    `published.confluence` block;
  - a finding publishes only if it passes insight_lint on its own, and
    re-publishing NO-OPS, printing the key recorded in the doc's
    `<!-- published: jira KEY -->` line (per the ledger ruling: commenting on
    every re-run would spam the issue).

Marker writes: this module owns the marker's `published` sub-block and
nothing else. It takes prd_flow.marker_lock() - the SAME lock prd_flow's own
read-modify-writes take, not a private one next to it - re-reads the marker,
sets that one key, and writes it back through prd_flow's atomic writer.
Stamps, status and approvals are never touched, and prd_flow.verify() is
unaffected (it hashes artifacts, not the marker). The finding's idempotence
record is written the same way: locked, re-read inside the lock, replaced
atomically, so a concurrent editor's changes are never truncated away.

Config: the profile's `publish:` block.
  publish:
    confluence: {base_url: ..., space: ..., parent_id: ...}   # parent optional
    jira:       {base_url: ..., project: ..., issue_type: ...}

CLI:
  publish.py prd <target> <run>              -> prints the page url
  publish.py finding <target> <insight-stem> -> prints the issue key
Exit codes: 0 ok, 2 bad/missing config or a malformed run/stem argument,
3 missing secrets, 4 unapproved or ungated artifact, 5 HTTP failure (status
code only), 6 the local record could not be written - the page or issue
EXISTS remotely and the message says where, so an operator can record it by
hand rather than re-running into a duplicate.
"""
import base64
import http.client
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import insight_lint
import locks
import prd_flow
import secrets_env

HTTP_TIMEOUT = 30
LOCK_TIMEOUT = 30
EMAIL_VAR = "ATLASSIAN_EMAIL"
TOKEN_VAR = "ATLASSIAN_API_TOKEN"
# The idempotence record appended to a published finding. Deliberately an HTML
# comment: insight_lint reads structure (title, ## Answer, ## Basis), citations
# and the TBD/TODO markers - none of which an HTML comment can trip - so a
# published doc still passes the gate that let it publish (pinned in tests).
JIRA_RECORD = "<!-- published: jira {key} -->"
# Anchored to a whole line: the record is something THIS module wrote on a
# line of its own. Matching it mid-prose would let a doc that merely quotes
# the syntax (a runbook, this module's own docs) claim to be published and
# silently skip filing.
JIRA_RECORD_RE = re.compile(
    r"^[ \t]*<!--[ \t]*published:[ \t]*jira[ \t]+([A-Za-z][A-Za-z0-9_]*-\d+)[ \t]*-->[ \t]*$",
    re.M)
BASIS_EXCERPT_LINES = 12
BASIS_EXCERPT_CHARS = 1200
# Publishing reaches the network with a bearer credential attached, so plain
# http is refused outright: it would put the base64 of email:token on the
# wire in clear. Loopback is the one exception - a stub server in a test has
# no network to be sniffed on, and demanding a TLS cert there buys nothing.
# config.py validates this too (Task 5); this is the last line of defence,
# the one that runs even when a config was hand-edited past the validator.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# An insight stem becomes a FILENAME under insights/. Same shape the dashboard
# serves artifacts under (command_center._ARTIFACT_NAME_RE): leading
# alphanumeric, then alphanumerics, dots, dashes, underscores. No separator,
# no leading dot, so "..", "../x" and "/etc/passwd" are all refused before any
# path is built from it. Kept as a local copy rather than an import -
# command_center is a server module with import-time side effects, and this is
# a one-line shape, not a dependency worth taking.
STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PublishError(RuntimeError):
    """Base for everything main() turns into an exit code."""
    code = 1


class ConfigError(PublishError):
    code = 2


class SecretsError(PublishError):
    code = 3


class GateError(PublishError):
    """The artifact is not in a publishable state (unapproved, missing, or
    failing its lint gate). Never a transport or a config problem."""
    code = 4


class HttpError(PublishError):
    """A REST call failed. Carries the status code ONLY - see the module
    docstring on why response bodies never travel with it."""
    code = 5

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class LockError(PublishError):
    """The remote write SUCCEEDED and the local record of it did not, because
    another writer held the lock.

    Its own exit code (6), never GateError's 4: 4 says "nothing was
    published, fix the artifact and re-run", and a re-run here creates a
    SECOND page or issue. Everything an operator needs to record the result
    by hand is in the message."""
    code = 6


# --------------------------------------------------------------------------
# markdown -> Confluence storage format
# --------------------------------------------------------------------------

_OL_ITEM = re.compile(r"\d{1,9}[.)] ")
# C0 controls (plus DEL) are dropped, not escaped. Tab and newline stay -
# they are the only two a code block legitimately carries. The rest are
# invisible in a diff and in a review, which is exactly what makes them worth
# removing: a NUL or an ESC smuggled through a PRD is either a parser exploit
# downstream or a rendering surprise, never content anybody meant to publish.
_CONTROLS = {c: None for c in list(range(32)) + [127] if c not in (9, 10)}
# Schemes a link may carry into a wiki page. Everything else - javascript:,
# data:, vbscript:, file: - renders as literal text instead: this module
# escapes markup rather than sanitising it, and a link is the one place where
# "escape the text" alone would still hand an attacker a live scheme.
_SAFE_SCHEMES = ("http://", "https://", "mailto:")


def _esc(s):
    return (s.translate(_CONTROLS)
             .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _attr(s):
    return _esc(s).replace('"', "&quot;")


def _safe_href(href):
    """True if `href` may become a real <a href>. Absolute links must use an
    allowlisted scheme; a relative link (no scheme at all before the first
    /, ? or #) is fine - it stays inside the wiki."""
    low = href.lower()
    if low.startswith(_SAFE_SCHEMES):
        return True
    head = low.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return ":" not in head


class _Finder:
    """`str.find` with a first-occurrence memo. The inline scanner walks left
    to right, so a needle that was not found once is never found later, and a
    hit at index j is still the first hit for any start <= j. Without the memo
    a line of unmatched openers ("[[[[[...") re-scans the tail once per
    opener; with it the scan stays linear. No regex is involved anywhere in
    the inline pass, so there is no backtracking to blow up either."""

    def __init__(self, text):
        self.text = text
        self._memo = {}

    def find(self, needle, start):
        got = self._memo.get(needle)
        if got is not None and (got == -1 or got >= start):
            return got
        got = self.text.find(needle, start)
        self._memo[needle] = got
        return got


def _word_edge(text, i):
    """True if position i can open an `_emphasis_` run - i.e. it is not in the
    middle of a word. Without this, snake_case_identifiers in PRD prose come
    out italicised."""
    return i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")


def _inline(text):
    """Inline markdown -> storage XHTML: `code`, [links](url), **strong**,
    *em* / _em_. Everything else, raw HTML very much included, is escaped
    literally."""
    f = _Finder(text)
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "`":
            j = f.find("`", i + 1)
            if j != -1:
                out.append("<code>" + _esc(text[i + 1:j]) + "</code>")
                i = j + 1
                continue
        elif c == "[":
            j = f.find("]", i + 1)
            if j != -1 and text[j + 1:j + 2] == "(":
                k = f.find(")", j + 2)
                # An unsafe scheme is not an error and not a stripped link: it
                # falls through to the literal-text path below, so the reader
                # sees exactly what the markdown said and nothing is clickable.
                href = text[j + 2:k].translate(_CONTROLS).strip() if k != -1 else ""
                if k != -1 and _safe_href(href):
                    out.append('<a href="%s">%s</a>'
                               % (_attr(href), _inline(text[i + 1:j])))
                    i = k + 1
                    continue
        elif c == "*" and text[i + 1:i + 2] == "*":
            j = f.find("**", i + 2)
            if j > i + 2:
                out.append("<strong>" + _inline(text[i + 2:j]) + "</strong>")
                i = j + 2
                continue
        elif c == "*":
            j = f.find("*", i + 1)
            if j > i + 1:
                out.append("<em>" + _inline(text[i + 1:j]) + "</em>")
                i = j + 1
                continue
        elif c == "_" and _word_edge(text, i):
            j = f.find("_", i + 1)
            if j > i + 1 and (j + 1 >= n or not text[j + 1].isalnum()):
                out.append("<em>" + _inline(text[i + 1:j]) + "</em>")
                i = j + 1
                continue
        out.append(_esc(c))
        i += 1
    return "".join(out)


def _heading_level(line):
    """1-4 for a heading line, else 0. Deeper runs of hashes are not headings
    Confluence has, so they fall through to a paragraph and print literally."""
    k = 0
    while k < len(line) and line[k] == "#":
        k += 1
    return k if 1 <= k <= 4 and line[k:k + 1] == " " else 0


def _heading(line, level):
    return "<h%d>%s</h%d>" % (level, _inline(line[level + 1:].strip()), level)


def _list_item(line):
    """(kind, content) for a list line, else None."""
    if line[:2] in ("- ", "* ", "+ "):
        return "ul", line[2:].strip()
    m = _OL_ITEM.match(line)
    if m:
        return "ol", line[m.end():].strip()
    return None


def _is_block_start(line):
    return (not line or line.startswith("```") or line.startswith(">")
            or _heading_level(line) or _list_item(line) is not None)


def render_storage(md_text):
    """Minimal, pure markdown -> Confluence storage format (XHTML).

    Supported: h1-h4, paragraphs, ul/ol lists, blockquotes, fenced code
    blocks (as <pre>), inline code, bold, italic and links. Anything else -
    tables, images, raw HTML, deeper headings - is escaped and rendered as
    literal text rather than guessed at: a PRD that publishes with a stray
    tag showing is fixable, a PRD that publishes with someone's markup
    silently executing in Confluence is not."""
    lines = (md_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("```"):
            i += 1
            body = []
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1  # the closing fence, or one past the end if unterminated
            out.append("<pre>" + _esc("\n".join(body)) + "</pre>")
            continue
        level = _heading_level(line)
        if level:
            out.append(_heading(line, level))
            i += 1
            continue
        if line.startswith(">"):
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote><p>%s</p></blockquote>"
                       % _inline(" ".join(q for q in quote if q)))
            continue
        item = _list_item(line)
        if item is not None:
            kind = item[0]
            items = []
            while i < n:
                nxt = _list_item(lines[i].strip())
                if nxt is None or nxt[0] != kind:
                    break
                items.append("<li>%s</li>" % _inline(nxt[1]))
                i += 1
            out.append("<%s>%s</%s>" % (kind, "".join(items), kind))
            continue
        para = [line]
        i += 1
        while i < n and not _is_block_start(lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>%s</p>" % _inline(" ".join(para)))
    return "\n".join(out)


# --------------------------------------------------------------------------
# config + secrets
# --------------------------------------------------------------------------

def _load_publish_cfg(target):
    """The profile's `publish:` block. Read defensively - Task 5 owns the
    schema validation; here anything missing or the wrong shape is exit 2
    with the key that is wrong."""
    cfg_path = os.path.join(target, ".autopilot", "config.yaml")
    if not os.path.exists(cfg_path):
        raise ConfigError(f"no config at {cfg_path}")
    import yaml
    try:
        with open(cfg_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise ConfigError(f"config at {cfg_path} does not parse: {e}")
    except OSError as e:
        raise ConfigError(f"config at {cfg_path} is unreadable: {e.strerror}")
    if not isinstance(raw, dict):
        raise ConfigError(f"config at {cfg_path} is not a mapping")
    pub = raw.get("publish")
    if not isinstance(pub, dict) or not pub:
        raise ConfigError("no `publish:` block in .autopilot/config.yaml - "
                          "publishing needs confluence/jira base_url and space/project")
    return pub


def _channel(pub, name, required):
    block = pub.get(name)
    if not isinstance(block, dict) or not block:
        raise ConfigError(f"publish.{name} is missing or not a mapping")
    out = {}
    for key in required:
        val = block.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ConfigError(f"publish.{name}.{key} must be a non-empty string, "
                              f"got {val!r}")
        out[key] = val.strip()
    if "base_url" in out:
        out["base_url"] = out["base_url"].rstrip("/")
        _require_secure(name, out["base_url"])
    return block, out


def _require_secure(name, url):
    """https, or http to a loopback host. Anything else is refused before a
    single request is built - see LOOPBACK_HOSTS on why the exception is
    exactly that narrow."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        raise ConfigError(f"publish.{name}.base_url is not a usable URL")
    if parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK_HOSTS):
        return
    raise ConfigError(
        f"publish.{name}.base_url must be https:// - publishing sends an "
        f"Authorization header, and plain http puts the credential on the "
        f"wire in clear (http is allowed only for "
        f"{'/'.join(sorted(LOOPBACK_HOSTS))}); got {_safe_url(url)}")


def _repo_root(target):
    """Where the workspace's artifacts live. `repo:` defaults to the target
    itself (config.py's DEFAULTS), which is what every product workspace
    uses; honoured here so a split target/repo layout still finds insights/."""
    cfg_path = os.path.join(target, ".autopilot", "config.yaml")
    repo = "."
    if os.path.exists(cfg_path):
        import yaml
        try:
            with open(cfg_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            if isinstance(raw, dict) and isinstance(raw.get("repo"), str):
                repo = raw["repo"]
        except (yaml.YAMLError, UnicodeDecodeError, OSError):
            pass
    return os.path.abspath(os.path.join(target, repo))


def _auth(target):
    """(email, token) for Atlassian basic auth. The secrets file wins over an
    inherited environment variable - same precedence prd_flow uses for stage
    children. Errors name the VARIABLE, never a value."""
    env = dict(os.environ)
    env.update(secrets_env.read(target))
    missing = [v for v in (EMAIL_VAR, TOKEN_VAR) if not (env.get(v) or "").strip()]
    if missing:
        raise SecretsError(
            f"missing {' and '.join(missing)} - set them in "
            f"{secrets_env.path(target)} (0600) or the environment")
    return env[EMAIL_VAR].strip(), env[TOKEN_VAR].strip()


# --------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------

def _safe_url(url):
    """`url` with any userinfo stripped, for error messages. A base_url someone
    pasted with credentials in it (https://user:token@host/) must not become
    the one place this module leaks a secret."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<url>"
    if not parts.netloc or "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


def _request(method, url, auth, payload=None):
    """One JSON REST call, returning the decoded response (or {}).

    Every failure path is deliberately terse: the status code, the method and
    the credential-stripped URL. Response bodies are read and dropped - an
    Atlassian error body can quote the request that carried the Authorization
    header, so nothing from the wire is ever printed."""
    email, token = auth
    data = json.dumps(payload).encode() if payload is not None else None
    # Building the Request is inside the try on purpose: urllib parses the URL
    # in the constructor and raises there for a scheme it does not know, so a
    # bad base_url would otherwise escape as a raw ValueError.
    try:
        req = urllib.request.Request(url, data=data, method=method)
        basic = base64.b64encode(f"{email}:{token}".encode()).decode()
        req.add_header("Authorization", "Basic " + basic)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except OSError:
            pass
        raise HttpError(f"{method} {_safe_url(url)} failed: HTTP {e.code}", status=e.code)
    except urllib.error.URLError as e:
        # e.reason is the underlying socket/ssl error ("[Errno 61] Connection
        # refused"), which names no URL and carries no credential - the whole
        # reason it is safe to print where a response body is not.
        raise HttpError(f"{method} {_safe_url(url)} failed: "
                        f"{e.reason or type(e).__name__}")
    except OSError as e:
        raise HttpError(f"{method} {_safe_url(url)} failed: {type(e).__name__}")
    except (ValueError, http.client.InvalidURL) as e:
        # Raised BEFORE any socket opens: urllib's "unknown url type" is a
        # ValueError, while http.client's InvalidURL (a space or a control
        # character in the host, a bad port) descends from HTTPException, not
        # from ValueError - catching only one of the two leaves a traceback.
        # Their text quotes the URL, so it is dropped: an operator gets the
        # sanitised URL and the failure kind.
        raise HttpError(f"{method} {_safe_url(url)} failed: "
                        f"malformed request URL ({type(e).__name__})")
    except http.client.HTTPException as e:
        # A protocol-level failure urllib did not wrap (a truncated status
        # line, a server hanging up mid-response). Type name only.
        raise HttpError(f"{method} {_safe_url(url)} failed: {type(e).__name__}")
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise HttpError(f"{method} {_safe_url(url)} returned a non-JSON response")


# --------------------------------------------------------------------------
# Confluence
# --------------------------------------------------------------------------

def _page_url(base_url, page_id, resp):
    links = resp.get("_links") if isinstance(resp.get("_links"), dict) else {}
    webui = links.get("webui")
    if isinstance(webui, str) and webui:
        root = links.get("base") if isinstance(links.get("base"), str) else base_url
        return root.rstrip("/") + webui
    return f"{base_url}/pages/viewpage.action?pageId={page_id}"


def _page_version(base_url, page_id, auth):
    resp = _request("GET", f"{base_url}/rest/api/content/{page_id}?expand=version", auth)
    version = resp.get("version") if isinstance(resp.get("version"), dict) else {}
    number = version.get("number")
    if not isinstance(number, int):
        raise HttpError(f"confluence page {page_id} returned no version number")
    return number


def _create_page(cfg, block, title, storage, auth):
    payload = {
        "type": "page",
        "title": title,
        "space": {"key": cfg["space"]},
        "body": {"storage": {"value": storage, "representation": "storage"}},
    }
    parent = block.get("parent_id")
    if parent not in (None, ""):
        payload["ancestors"] = [{"id": str(parent)}]
    resp = _request("POST", f"{cfg['base_url']}/rest/api/content", auth, payload)
    page_id = resp.get("id")
    if not page_id:
        raise HttpError("confluence create returned no page id")
    return str(page_id), _page_url(cfg["base_url"], page_id, resp)


def _update_page(cfg, page_id, title, storage, auth):
    version = _page_version(cfg["base_url"], page_id, auth) + 1
    payload = {
        "id": str(page_id),
        "type": "page",
        "title": title,
        "space": {"key": cfg["space"]},
        "version": {"number": version},
        "body": {"storage": {"value": storage, "representation": "storage"}},
    }
    resp = _request("PUT", f"{cfg['base_url']}/rest/api/content/{page_id}", auth, payload)
    return str(page_id), _page_url(cfg["base_url"], page_id, resp)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_published(target, run, channel, record):
    """Set the marker's `published.<channel>` and nothing else.

    prd_flow owns this file; the M5 ledger ruling lets publish.py write this
    one sub-block. It does so under prd_flow.marker_lock() - the same flock
    prd_flow's own read-modify-writes take, so a stage stamping from the
    dashboard and a publish landing a page id cannot overwrite each other -
    and re-reads the marker INSIDE the lock, which is why anything that
    landed while we were on the network survives."""
    with locks.hold(prd_flow.marker_lock(target, run), timeout=LOCK_TIMEOUT):
        marker = prd_flow.load_marker(target, run)
        existing = marker.get("published")
        published = dict(existing) if isinstance(existing, dict) else {}
        published[channel] = record
        marker["published"] = published
        prd_flow.write_marker(target, run, marker)
    return marker


def _atomic_write_text(path, text):
    """Replace `path`'s contents in one step (mkstemp + os.replace, the same
    mechanics as prd_flow._atomic_write_json). A plain write truncates first,
    so a crash mid-write leaves a half-written insight doc where a whole one
    used to be."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".publish-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        # mkstemp creates 0600. Carry the doc's own mode across, or publishing
        # a finding would quietly make a repo file owner-only readable.
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _doc_lock(doc):
    """Lock guarding one insight doc's read-modify-write. A dotfile beside the
    doc: insights/ is globbed for `*.md` by insight_lint and the dashboard, so
    a `.<name>.lock` neighbour is invisible to both."""
    return str(doc.with_name("." + doc.name + ".lock"))


def _append_record(doc, key):
    """Append `<!-- published: jira KEY -->` to `doc`.

    Caller must already hold `_doc_lock(doc)` - publish_finding holds that
    lock across its ENTIRE body (re-read, lint, POST, record), not just this
    write, so this function no longer takes the lock itself: `hold()` is not
    reentrant (locks.py's module docstring), and a second acquisition here by
    the same process would self-deadlock.

    Still re-reads `doc` immediately before writing, though: the lock rules
    out another publish.py run racing this one, but not a human or an agent
    hand-editing the file concurrently without going through this module at
    all. If someone else's edit landed a record first, theirs stands (two
    records would be worse than one) and this returns False - the caller has
    an issue nothing points to and must say so, not exit 0.

    The record always lands as the doc's LAST line - command_center's
    _insight_jira_key relies on that to read only the tail of the file
    (cheap) instead of the whole thing, with a full-file fallback for the day
    this contract drifts."""
    text = doc.read_text(encoding="utf-8")
    if JIRA_RECORD_RE.search(text):
        return False
    _atomic_write_text(doc, text.rstrip("\n") + "\n\n" + JIRA_RECORD.format(key=key) + "\n")
    return True


def publish_prd(target, run):
    """Publish research/<run>/04-prd.md to Confluence; return the page url.

    Requires the marker at `status: done` - that status IS the human sign-off
    (prd_flow.approve_prd), and it is the only approval this module trusts.
    Re-running updates the page recorded in `published.confluence` instead of
    creating a second one; a recorded page that no longer exists (someone
    deleted it in Confluence) is re-created rather than left unpublishable."""
    if not prd_flow.RUN_RE.match(run or ""):
        # Before any path is built from it: `run` becomes a DIRECTORY name
        # under research/, and prd_flow owns that shape (RUN_RE) precisely so
        # every caller refuses "../.." in the same way.
        raise ConfigError(
            f"invalid run id {run!r} - lowercase letters, digits and hyphens "
            "only, must start alphanumeric")
    pub = _load_publish_cfg(target)
    block, cfg = _channel(pub, "confluence", ("base_url", "space"))
    try:
        marker = prd_flow.load_marker(target, run)
    except FileNotFoundError:
        raise GateError(f"no PRD run '{run}' in {target}")
    except ValueError as e:
        raise GateError(f"PRD marker for '{run}' does not parse: {e}")
    if marker.get("status") != "done":
        raise GateError(
            f"cannot publish '{run}': status is '{marker.get('status')}', "
            "expected 'done' (the PRD sign-off gate)")
    prd_path = os.path.join(target, "research", run, prd_flow.ARTIFACTS["draft"])
    if not os.path.exists(prd_path):
        raise GateError(f"cannot publish '{run}': no PRD artifact at {prd_path}")
    with open(prd_path, encoding="utf-8") as f:
        storage = render_storage(f.read())
    title = f"{marker.get('title') or run} ({run})"

    auth = _auth(target)
    block_now = marker.get("published")
    recorded = block_now.get("confluence") if isinstance(block_now, dict) else None
    page_id = recorded.get("page_id") if isinstance(recorded, dict) else None
    if page_id:
        try:
            page_id, url = _update_page(cfg, page_id, title, storage, auth)
        except HttpError as e:
            if e.status != 404:
                raise
            page_id, url = _create_page(cfg, block, title, storage, auth)
    else:
        page_id, url = _create_page(cfg, block, title, storage, auth)
    try:
        _record_published(target, run, "confluence",
                          {"page_id": page_id, "url": url, "at": _now()})
    except (locks.LockTimeout, locks.ReentrantLock) as e:
        raise LockError(
            f"the page IS published - id {page_id}, {url} - but the marker "
            f"could not be updated ({e}). Add it by hand under "
            f'"published": {{"confluence": {{"page_id": "{page_id}", '
            f'"url": "{url}"}}}} in '
            f'{os.path.join(target, "research", run, prd_flow.MARKER_NAME)}, or a '
            "re-run will create a SECOND page.")
    return url


# --------------------------------------------------------------------------
# Jira
# --------------------------------------------------------------------------

def _section(text, heading):
    """The body of the `## <heading>` section, up to the next `## `.

    Matches the WHOLE heading line, not a prefix: `## Answers to reviewers`
    is a different section from `## Answer`, and a prefix match would quietly
    file the wrong half of the doc as the finding."""
    want = f"## {heading}"
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        if line.strip() != want:
            continue
        body = []
        for nxt in lines[idx + 1:]:
            if nxt.startswith("## "):
                break
            body.append(nxt)
        return "\n".join(body).strip()
    return ""


def _doc_title(text):
    for line in text.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _basis_excerpt(text):
    basis = _section(text, "Basis")
    lines = [ln for ln in basis.split("\n") if ln.strip()][:BASIS_EXCERPT_LINES]
    excerpt = "\n".join(lines)
    if len(excerpt) > BASIS_EXCERPT_CHARS:
        excerpt = excerpt[:BASIS_EXCERPT_CHARS].rstrip() + " ..."
    return excerpt


def _description(text, rel_path):
    """The issue body: the finding's own Answer, enough Basis to judge it, and
    where the full doc lives. Evidence travels with the ticket - a Jira issue
    that just says "we found something" is the failure mode this avoids."""
    return "\n".join([
        _section(text, "Answer") or "(no Answer section)",
        "",
        "Basis (excerpt):",
        _basis_excerpt(text) or "(no Basis lines)",
        "",
        f"Full finding: {rel_path} (Orbit workspace)",
    ])


def publish_finding(target, stem):
    """File insights/<stem>.md as a Jira issue; return (key, created).

    The gate is insight_lint on this one doc: the grounding contract's
    mechanical half, run solo so a clean finding is not held back by an
    unrelated doc failing in the same workspace. Re-running NO-OPS with the
    recorded key (ledger ruling): the doc carries
    `<!-- published: jira KEY -->` after the first file, and commenting on
    every re-run would spam the issue.

    `_doc_lock(doc)` is held for the WHOLE re-read/no-op-check/lint/POST/
    record sequence, not just the final record write. Only the read of the
    "already filed" record used to be serialized; two concurrent runs could
    each read the doc before either had filed anything, each conclude it was
    unpublished, and each POST its own Jira issue - one of the two orphaned,
    with nothing in the doc pointing to it. Holding the lock across the
    whole body closes that window: whichever run gets there second re-reads
    the doc AFTER the first one's record has landed and takes the no-op path
    instead of ever building a payload. This is one in-process critical
    section around a single HTTP call, not the fork-without-exec hazard
    locks.py's "never hold across a claude invocation" rule warns about (see
    its module docstring) - nothing here forks or execs.

    A lock that cannot be acquired within LOCK_TIMEOUT is a genuinely lost
    race: unlike the old failure mode, NOTHING has been POSTed yet, so there
    is nothing to reconcile - the caller just needs to wait for the other
    writer and retry."""
    if not STEM_RE.match(stem or ""):
        # Before the path is built - see STEM_RE.
        raise ConfigError(
            f"invalid insight stem {stem!r} - letters, digits, dots, dashes "
            "and underscores only, must start alphanumeric, no path separators")
    pub = _load_publish_cfg(target)
    _block, cfg = _channel(pub, "jira", ("base_url", "project", "issue_type"))
    rel_path = os.path.join("insights", f"{stem}.md")
    doc = Path(_repo_root(target)) / rel_path
    if not doc.is_file():
        raise GateError(f"no insight doc at {doc}")
    auth = _auth(target)

    try:
        with locks.hold(_doc_lock(doc), timeout=LOCK_TIMEOUT):
            text = doc.read_text(encoding="utf-8")
            already = JIRA_RECORD_RE.search(text)
            if already:
                return already.group(1), False

            errs = insight_lint.lint_file(doc)
            if errs:
                raise GateError("insight_lint failed - a finding files only once it is "
                                "grounded:\n" + "\n".join(f"  x {e}" for e in errs))

            summary = _doc_title(text) or stem
            payload = {"fields": {
                "project": {"key": cfg["project"]},
                "issuetype": {"name": cfg["issue_type"]},
                "summary": summary,
                "description": _description(text, rel_path),
            }}
            resp = _request("POST", f"{cfg['base_url']}/rest/api/2/issue", auth, payload)
            key = resp.get("key")
            if not key:
                raise HttpError("jira create returned no issue key")
            try:
                if not _append_record(doc, key):
                    raise LockError(
                        f"issue {key} IS filed at {_safe_url(cfg['base_url'])}/browse/{key}, "
                        f"but another writer recorded a different key in {doc} first - "
                        f"{key} is now an issue nothing points to. Close it, or "
                        "reconcile the two by hand.")
            except OSError as e:
                raise LockError(
                    f"issue {key} IS filed at {_safe_url(cfg['base_url'])}/browse/{key}, but "
                    f"{doc} could not be written ({e.strerror}). Append "
                    f"'{JIRA_RECORD.format(key=key)}' to it by hand, or a re-run will "
                    "file a SECOND issue.")
    except locks.LockTimeout as e:
        raise LockError(
            f"could not acquire the lock on {doc} within {LOCK_TIMEOUT}s ({e}) - "
            "another publish is already in progress for this doc. Nothing was "
            "filed by this run; wait for the other one to finish and re-run "
            "rather than retrying blindly, which risks a second issue if the "
            "other run is also still mid-flight.")
    return key, True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cli_prd(rest):
    if len(rest) < 2:
        raise ConfigError("usage: publish.py prd <target> <run>")
    return publish_prd(rest[0], rest[1])


def _cli_finding(rest):
    if len(rest) < 2:
        raise ConfigError("usage: publish.py finding <target> <insight-stem>")
    key, created = publish_finding(rest[0], rest[1])
    if not created:
        print(f"already filed as {key} - no new issue", file=sys.stderr)
    return key


CMDS = {"prd": _cli_prd, "finding": _cli_finding}


def main(argv):
    if not argv or argv[0] not in CMDS:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        print(CMDS[argv[0]](argv[1:]))
    except PublishError as e:
        # The `[<code>]` tag is a stable, parseable contract, not decoration:
        # command_center.py's dashboard reconciles a detached publish run's
        # outcome by reading this exact shape back out of the log (see its
        # _publish_log_reason) rather than pattern-matching free-text
        # messages that are free to change independently of this file.
        print(f"publish[{e.code}]: {e}", file=sys.stderr)
        return e.code
    except (locks.LockTimeout, locks.ReentrantLock) as e:
        # Safety net for a lock taken outside the two paths that wrap their
        # own (both raise LockError with the created id). Nothing was
        # published on this path, but 6 - "the local side is contended" -
        # still beats a traceback.
        print(f"publish[{LockError.code}]: {e}", file=sys.stderr)
        return LockError.code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
