#!/usr/bin/env python3
"""backlog_append.py — the ONE way engine code adds tasks to backlog.yaml.

backlog.yaml is human-curated and comment-preserving, so it is never
round-tripped through a YAML dumper. Everything that feeds it (intake, epic
decomposition, signal adapters) appends rendered task blocks as raw text,
idempotently by id, and only ever as `status: proposed` + `autopilot: human` —
a machine may suggest work, only a human may queue it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

import locks

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


def _lock_path(backlog: Path) -> Path:
    """<AP_STATE>/.backlog.lock for whichever backlog.yaml `backlog` points at -
    the ONE lock every backlog.yaml writer (this module's append_tasks/
    append_raw_blocks/set_task_field, autopromote.py, autoclose.py,
    qa_check.py, command_center.py) takes around its whole read-modify-write,
    so none of them can ever lose another's update.

    backlog_append is a low-level utility - many callers hand it a bare
    backlog.yaml path with no AP_HOME/AP_STATE env set at all (intake, epic
    decomposition, and this module's own tests all do), so this must never
    require AP_HOME to resolve a lock path. AP_STATE (if set) wins - it's the
    authoritative runtime-state dir config.py's shellenv exports for a real
    run and could in principle point somewhere other than the backlog's own
    parent; falling back to `<backlog's directory>/state` matches every real
    target's layout (AP_HOME/backlog.yaml sits next to AP_HOME/state/) and is
    exactly how every other writer resolves AP_STATE off AP_HOME too.
    """
    st = os.environ.get("AP_STATE")
    state_dir = Path(st) if st else Path(backlog).parent / "state"
    return state_dir / ".backlog.lock"


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:64].rstrip("-")


def yq(s: str) -> str:
    """Quote a scalar for a YAML double-quoted string (one line)."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def block_literal(s: str, indent: str) -> str:
    """Render a multi-line string as a YAML literal block at the given indent."""
    body = "\n".join(indent + ln for ln in str(s).splitlines())
    return "|\n" + body


def existing_ids(backlog: Path) -> set[str]:
    try:
        tasks = (yaml.safe_load(backlog.read_text()) or {}).get("tasks") or []
        return {str(t.get("id")) for t in tasks}
    except Exception:
        return set()


def render_proposed_task(t: dict, source: str) -> str:
    """One backlog task block from a proposal dict.

    Honored keys: id, title, category, priority, context, evidence,
    acceptance_criteria, epic. Status/autopilot are NOT honored — proposals are
    always `proposed`/`human`; promotion is a human act on the dashboard.
    """
    lines = [
        f"  - id: {t['id']}",
        f"    title: {yq(t['title'])}",
        f"    category: {t['category']}",
        f"    autopilot: human",
        f"    status: proposed",
        f"    priority: {t.get('priority') or 'medium'}",
        f"    source: {source}",
    ]
    if t.get("epic"):
        lines.append(f"    epic: {t['epic']}")
    ctx = (t.get("context") or "").strip()
    ev = (t.get("evidence") or "").strip()
    if ev:
        ctx = (ctx + "\n\nEVIDENCE:\n" + ev).strip()
    if ctx:
        lines.append("    context: " + block_literal(ctx, "      "))
    acc = [a for a in (t.get("acceptance_criteria") or []) if isinstance(a, str) and a.strip()]
    for i, a in enumerate(acc):
        if i == 0:
            lines.append("    acceptance_criteria:")
        lines.append(f"      - {yq(a)}")
    return "\n".join(lines) + "\n"


def validate_proposal(t: dict, known_categories: set[str]) -> str | None:
    """Return a rejection reason, or None if the proposal is mergeable."""
    tid = str(t.get("id") or "").strip()
    if not SLUG_RE.match(tid):
        return f"bad id {tid!r} (want a lowercase slug)"
    if not (t.get("title") or "").strip():
        return "no title"
    if (t.get("category") or "") not in known_categories:
        return f"unknown category {t.get('category')!r}"
    return None


def set_task_field(backlog: Path, tid: str, field: str, value: str) -> bool:
    """Flip one scalar field on one task's block, comment-preserving.

    Same block-level text-edit discipline as the dashboard: locate the `- id:`
    line, bound the block at the next `- id:`, substitute (or insert) the field
    line. Never a YAML round-trip. → False if the task isn't in the backlog.

    Holds the shared backlog lock (see `_lock_path`) around the whole
    read-modify-write - one of the five backlog.yaml writers autopilot must
    never let race another.
    """
    with locks.hold(_lock_path(backlog)):
        text = backlog.read_text()
        start = re.search(rf"^(\s*)- id:\s*{re.escape(tid)}\s*(?:#.*)?$", text, re.MULTILINE)
        if not start:
            return False
        nxt = re.search(r"^\s*- id:\s*\S", text[start.end():], re.MULTILINE)
        end = start.end() + (nxt.start() if nxt else len(text) - start.end())
        block = text[start.end():end]
        pat = re.compile(rf"^(\s*){re.escape(field)}:.*$", re.MULTILINE)
        if pat.search(block):
            # A callable replacement — never a template string. `value` is
            # untrusted-shaped (e.g. QA findings folded via json.dumps, which can
            # contain backslash-escapes like `—` for an em dash): fed through
            # re.sub's TEMPLATE parser (as `rf"\g<1>{field}: {value}"` used to),
            # `\u` isn't a recognized replacement escape and raises `re.error: bad
            # escape \u`. A lambda's return value is spliced in literally, with no
            # backslash reinterpretation, so `value` is always treated as data.
            block = pat.sub(lambda m: f"{m.group(1)}{field}: {value}", block, count=1)
        else:
            block = f"\n{start.group(1)}  {field}: {value}" + block
        backlog.write_text(text[:start.end()] + block + text[end:])
        return True


def _strip_inline_comment(raw: str) -> str:
    """Best-effort trailing-`# comment` stripper for one YAML scalar's raw
    text (no full tokenizer — this repo's fields are simple bare/quoted
    scalars, never flow collections).

    Unquoted scalar (e.g. `2  # note`): a YAML comment starts at a `#` that
    is preceded by whitespace (or is the whole value) — strip from there.
    Quoted scalar (single or double): a `#` inside the quotes is CONTENT,
    not a comment (`qa_findings: "drove the flow — see #3"` must keep the
    `#3`) — scan past the matching closing quote (honoring `\"` escapes in
    double-quoted, `''` escapes in single-quoted) and drop only what follows
    it.
    """
    s = raw.strip()
    if not s or s[0] not in ('"', "'"):
        m = re.search(r"(?:^|\s)#.*$", s)
        return s[:m.start()].rstrip() if m else s
    quote = s[0]
    i = 1
    while i < len(s):
        c = s[i]
        if c == quote:
            if quote == '"':
                nbackslash = 0
                j = i - 1
                while j >= 0 and s[j] == "\\":
                    nbackslash += 1
                    j -= 1
                if nbackslash % 2 == 1:          # escaped quote (\") — not the close
                    i += 1
                    continue
            elif i + 1 < len(s) and s[i + 1] == "'":   # '' inside a single-quoted scalar
                i += 2
                continue
            return s[:i + 1]                      # closing quote — trailing text dropped
        i += 1
    return s        # no closing quote found (malformed) — return everything, best-effort


def get_task_field(backlog: Path, tid: str, field: str) -> str | None:
    """Read one scalar field's raw (un-parsed) value off one task's block.

    Sibling reader to `set_task_field` — same block-location discipline
    (locate `- id:`, bound at the next `- id:`), no YAML round-trip. Strips
    a trailing inline `# comment` (see `_strip_inline_comment`) so a
    hand-annotated field (`qa_bounces: 2  # bumped by hand`) still reads
    cleanly. Returns the raw text after `field:` (still quoted/escaped if it
    was written quoted — callers that need the Python value should
    json/yaml-decode it themselves). → None if the task isn't in the
    backlog, or the field isn't present on its block.
    """
    text = backlog.read_text()
    start = re.search(rf"^(\s*)- id:\s*{re.escape(tid)}\s*(?:#.*)?$", text, re.MULTILINE)
    if not start:
        return None
    nxt = re.search(r"^\s*- id:\s*\S", text[start.end():], re.MULTILINE)
    end = start.end() + (nxt.start() if nxt else len(text) - start.end())
    block = text[start.end():end]
    pat = re.compile(rf"^\s*{re.escape(field)}:(.*)$", re.MULTILINE)
    m = pat.search(block)
    return _strip_inline_comment(m.group(1)) if m else None


def append_tasks(backlog: Path, proposals: list[dict], source: str,
                 header: str, dry_run: bool = False) -> tuple[list[str], list[tuple[str, str]]]:
    """Append valid, new proposals to backlog.yaml. → (added_ids, [(id, why-skipped)]).

    Holds the shared backlog lock around the WHOLE read-modify-write - the
    existing_ids() read through the final write - so two concurrent callers
    (two intake runs, an intake racing an epic decomposition, ...) can never
    both read the same "have" set and each append, silently duplicating or
    losing an id. Calls `_append_raw_blocks_unlocked` (not the public,
    locking `append_raw_blocks`) for the actual write - locks.hold() is not
    reentrant, so nesting a second acquisition of the SAME lock inside this
    one would self-deadlock (see locks.py's module docstring).
    """
    from backlog_lint import KNOWN_CATEGORIES
    with locks.hold(_lock_path(backlog)):
        have = existing_ids(backlog)
        added, skipped, blocks = [], [], []
        for t in proposals:
            tid = str(t.get("id") or "").strip()
            why = validate_proposal(t, KNOWN_CATEGORIES)
            if why:
                skipped.append((tid or "(no id)", why))
            elif tid in have or tid in set(added):
                skipped.append((tid, "already in backlog"))
            else:
                added.append(tid)
                blocks.append(render_proposed_task(t, source))
        if added and not dry_run:
            _append_raw_blocks_unlocked(backlog, f"{header} — triage before promoting", blocks)
    return added, skipped


def append_raw_blocks(backlog: Path, header: str, blocks: list[str]) -> None:
    """Append pre-rendered task blocks under a comment header.

    Adapters that render their own blocks (custom gate hints etc.) come through
    here so the empty-backlog handling lives once: a scaffolded backlog says
    `tasks: []` (flow-style), under which appended block items would be invalid
    YAML — open the list in place first.

    This is the PUBLIC, locking entry point - holds the shared backlog lock
    around the read-modify-write. `append_tasks` (above) needs the same write
    under a lock it already holds, so it calls the unlocked variant directly
    instead of nesting a second acquisition through here.
    """
    with locks.hold(_lock_path(backlog)):
        _append_raw_blocks_unlocked(backlog, header, blocks)


def _append_raw_blocks_unlocked(backlog: Path, header: str, blocks: list[str]) -> None:
    """The actual read-modify-write behind `append_raw_blocks` - split out so
    `append_tasks` can reuse it without nesting a lock (locks.hold() is not
    reentrant - see locks.py's module docstring). Never call this directly
    from outside this module; go through `append_raw_blocks` or `append_tasks`
    so the write is always lock-protected."""
    text = backlog.read_text() if backlog.exists() else "tasks:\n"
    text = re.sub(r"^(tasks:)\s*\[\s*\](\s*(?:#.*)?)$", r"\1\2", text,
                  count=1, flags=re.MULTILINE)
    if not text.endswith("\n"):
        text += "\n"
    text += f"\n  # ── {header} ──\n"
    text += "".join(b if b.endswith("\n") else b + "\n" for b in blocks)
    backlog.write_text(text)
