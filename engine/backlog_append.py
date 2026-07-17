#!/usr/bin/env python3
"""backlog_append.py — the ONE way engine code adds tasks to backlog.yaml.

backlog.yaml is human-curated and comment-preserving, so it is never
round-tripped through a YAML dumper. Everything that feeds it (intake, epic
decomposition, signal adapters) appends rendered task blocks as raw text,
idempotently by id, and only ever as `status: proposed` + `autopilot: human` —
a machine may suggest work, only a human may queue it.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


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
    """
    text = backlog.read_text()
    start = re.search(rf"^(\s*)- id:\s*{re.escape(tid)}\s*(?:#.*)?$", text, re.MULTILINE)
    if not start:
        return False
    nxt = re.search(r"^\s*- id:\s*\S", text[start.end():], re.MULTILINE)
    end = start.end() + (nxt.start() if nxt else len(text) - start.end())
    block = text[start.end():end]
    pat = re.compile(rf"^(\s*){re.escape(field)}:.*$", re.MULTILINE)
    if pat.search(block):
        block = pat.sub(rf"\g<1>{field}: {value}", block, count=1)
    else:
        block = f"\n{start.group(1)}  {field}: {value}" + block
    backlog.write_text(text[:start.end()] + block + text[end:])
    return True


def append_tasks(backlog: Path, proposals: list[dict], source: str,
                 header: str, dry_run: bool = False) -> tuple[list[str], list[tuple[str, str]]]:
    """Append valid, new proposals to backlog.yaml. → (added_ids, [(id, why-skipped)])."""
    from backlog_lint import KNOWN_CATEGORIES
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
        append_raw_blocks(backlog, f"{header} — triage before promoting", blocks)
    return added, skipped


def append_raw_blocks(backlog: Path, header: str, blocks: list[str]) -> None:
    """Append pre-rendered task blocks under a comment header.

    Adapters that render their own blocks (custom gate hints etc.) come through
    here so the empty-backlog handling lives once: a scaffolded backlog says
    `tasks: []` (flow-style), under which appended block items would be invalid
    YAML — open the list in place first.
    """
    text = backlog.read_text() if backlog.exists() else "tasks:\n"
    text = re.sub(r"^(tasks:)\s*\[\s*\](\s*(?:#.*)?)$", r"\1\2", text,
                  count=1, flags=re.MULTILINE)
    if not text.endswith("\n"):
        text += "\n"
    text += f"\n  # ── {header} ──\n"
    text += "".join(b if b.endswith("\n") else b + "\n" for b in blocks)
    backlog.write_text(text)
