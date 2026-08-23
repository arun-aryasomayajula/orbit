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
nothing else. It takes a lock next to the marker, re-reads it, sets that one
key, and writes it back through prd_flow's own atomic writer - stamps,
status and approvals are never touched, and prd_flow.verify() is unaffected
(it hashes artifacts, not the marker).

Config: the profile's `publish:` block.
  publish:
    confluence: {base_url: ..., space: ..., parent_id: ...}   # parent optional
    jira:       {base_url: ..., project: ..., issue_type: ...}

CLI:
  publish.py prd <target> <run>              -> prints the page url
  publish.py finding <target> <insight-stem> -> prints the issue key
Exit codes: 0 ok, 2 missing/malformed config, 3 missing secrets,
4 unapproved or ungated artifact, 5 HTTP failure (status code only).
"""
import base64
import json
import os
import re
import sys
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
JIRA_RECORD_RE = re.compile(r"<!--\s*published:\s*jira\s+([A-Za-z][A-Za-z0-9_]*-\d+)\s*-->")
BASIS_EXCERPT_LINES = 12
BASIS_EXCERPT_CHARS = 1200


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


# --------------------------------------------------------------------------
# markdown -> Confluence storage format
# --------------------------------------------------------------------------

_OL_ITEM = re.compile(r"\d{1,9}[.)] ")


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(s):
    return _esc(s).replace('"', "&quot;")


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
                if k != -1:
                    out.append('<a href="%s">%s</a>'
                               % (_attr(text[j + 2:k].strip()), _inline(text[i + 1:j])))
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
        raw = yaml.safe_load(open(cfg_path)) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"config at {cfg_path} does not parse: {e}")
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
    return block, out


def _repo_root(target):
    """Where the workspace's artifacts live. `repo:` defaults to the target
    itself (config.py's DEFAULTS), which is what every product workspace
    uses; honoured here so a split target/repo layout still finds insights/."""
    cfg_path = os.path.join(target, ".autopilot", "config.yaml")
    repo = "."
    if os.path.exists(cfg_path):
        import yaml
        try:
            raw = yaml.safe_load(open(cfg_path)) or {}
            if isinstance(raw, dict) and isinstance(raw.get("repo"), str):
                repo = raw["repo"]
        except yaml.YAMLError:
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
    req = urllib.request.Request(url, data=data, method=method)
    basic = base64.b64encode(f"{email}:{token}".encode()).decode()
    req.add_header("Authorization", "Basic " + basic)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except OSError:
            pass
        raise HttpError(f"{method} {_safe_url(url)} failed: HTTP {e.code}", status=e.code)
    except urllib.error.URLError as e:
        raise HttpError(f"{method} {_safe_url(url)} failed: {type(e.reason).__name__}")
    except OSError as e:
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
    one sub-block through its own locked read-modify-write. The lock (a real
    flock, engine/locks.py) makes the read-modify-write safe against a
    concurrent prd_flow write; the re-read inside it is why a stamp or a
    status change that landed while we were on the network survives."""
    lock = os.path.join(target, "research", run, ".publish.lock")
    with locks.hold(lock, timeout=LOCK_TIMEOUT):
        marker = prd_flow.load_marker(target, run)
        existing = marker.get("published")
        published = dict(existing) if isinstance(existing, dict) else {}
        published[channel] = record
        marker["published"] = published
        prd_flow.write_marker(target, run, marker)
    return marker


def publish_prd(target, run):
    """Publish research/<run>/04-prd.md to Confluence; return the page url.

    Requires the marker at `status: done` - that status IS the human sign-off
    (prd_flow.approve_prd), and it is the only approval this module trusts.
    Re-running updates the page recorded in `published.confluence` instead of
    creating a second one; a recorded page that no longer exists (someone
    deleted it in Confluence) is re-created rather than left unpublishable."""
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
    _record_published(target, run, "confluence",
                      {"page_id": page_id, "url": url, "at": _now()})
    return url


# --------------------------------------------------------------------------
# Jira
# --------------------------------------------------------------------------

def _section(text, heading):
    """The body of a `## <heading>` section, up to the next `## `."""
    marker = f"\n## {heading}"
    body = ("\n" + text).split(marker, 1)
    if len(body) < 2:
        return ""
    rest = body[1].split("\n", 1)[1] if "\n" in body[1] else ""
    return rest.split("\n## ", 1)[0].strip()


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
    every re-run would spam the issue."""
    pub = _load_publish_cfg(target)
    _block, cfg = _channel(pub, "jira", ("base_url", "project", "issue_type"))
    rel_path = os.path.join("insights", f"{stem}.md")
    doc = Path(_repo_root(target)) / rel_path
    if not doc.is_file():
        raise GateError(f"no insight doc at {doc}")
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
    auth = _auth(target)
    resp = _request("POST", f"{cfg['base_url']}/rest/api/2/issue", auth, payload)
    key = resp.get("key")
    if not key:
        raise HttpError("jira create returned no issue key")
    record = JIRA_RECORD.format(key=key)
    doc.write_text(text.rstrip("\n") + "\n\n" + record + "\n", encoding="utf-8")
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
        print(f"publish: {e}", file=sys.stderr)
        return e.code
    except locks.LockTimeout as e:
        print(f"publish: {e}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
