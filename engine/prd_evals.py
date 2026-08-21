#!/usr/bin/env python3
"""Tier-A deterministic evals for PRD stage artifacts (spec section 4).
Pure functions: dict/text in, error strings out. prd_flow.py records the
verdicts; nothing here writes files. Design reference: dwh-suite prd_evals."""
import re

SID = re.compile(r"S\d+$")
CITE = re.compile(r"\(S(\d+)\)")
TESTABLE = re.compile(r"\d|%|>=|<=|\bpass\b|\bfail\b")
PRD_HEADINGS = ["## Problem", "## Goals", "## Non-goals", "## Requirements",
                "## Acceptance criteria", "## Open questions", "## Basis"]
KINDS = {"metaql", "jira", "confluence", "scrape", "doc", "code"}


def _need(d, key, typ, errs, where):
    v = d.get(key)
    if not isinstance(v, typ) or (typ in (str, list) and not v and key not in OPTIONAL_EMPTY):
        errs.append(f"{where}: '{key}' missing/empty/wrong type")
    return v

OPTIONAL_EMPTY = {"scope_out", "data_targets", "code_targets"}


def check_intake(d: dict) -> list:
    errs = []
    for k, t in (("run_id", str), ("title", str), ("pm", str), ("goal", str),
                 ("scope_in", list), ("scope_out", list), ("data_targets", list), ("code_targets", list)):
        _need(d, k, t, errs, "intake")
    if not isinstance(d.get("pii_touch"), bool):
        errs.append("intake: 'pii_touch' must be a boolean")
    return errs


def _source_ids(research: dict) -> set:
    return {s.get("id") for s in research.get("sources") or [] if isinstance(s, dict)}


def check_research(d: dict) -> list:
    errs = []
    if not isinstance(d.get("run_id"), str) or not d.get("run_id"):
        errs.append("research: 'run_id' missing/empty/wrong type")
    srcs = d.get("sources") or []
    if not srcs:
        errs.append("research: no sources — grounding contract requires at least one typed source")
    ids = []
    for s in srcs:
        sid = s.get("id", "")
        if not SID.match(sid):
            errs.append(f"research: source id {sid!r} must match S<n>")
        ids.append(sid)
        kind = s.get("kind", "")
        if kind and kind not in KINDS:
            errs.append(f"research: source {sid} kind {kind!r} not in {sorted(KINDS)}")
        for k in ("kind", "ref", "date", "summary"):
            if not s.get(k):
                errs.append(f"research: source {sid} missing '{k}'")
    if len(ids) != len(set(ids)):
        errs.append("research: duplicate source ids")
    if not isinstance(d.get("coverage_gaps"), (type(None), list)):
        errs.append("research: 'coverage_gaps' must be a list when present")
    known = set(ids)
    for f in d.get("findings") or []:
        if not isinstance(f, dict) or not f.get("claim"):
            errs.append("research: finding must have non-empty 'claim'")
        for sid in f.get("source_ids") or []:
            if sid not in known:
                errs.append(f"research: finding cites unknown source {sid}")
    return errs


def check_synthesis(d: dict, research: dict) -> list:
    errs = []
    if not isinstance(d.get("run_id"), str) or not d.get("run_id"):
        errs.append("synthesis: 'run_id' missing/empty/wrong type")
    reqs = d.get("requirements") or []
    if not reqs:
        errs.append("synthesis: no requirements")
    if not isinstance(d.get("gaps"), (type(None), list)):
        errs.append("synthesis: 'gaps' must be a list when present")
    if not isinstance(d.get("open_questions"), (type(None), list)):
        errs.append("synthesis: 'open_questions' must be a list when present")
    known = _source_ids(research)
    for r in reqs:
        if not re.match(r"R\d+$", r.get("id", "")):
            errs.append(f"synthesis: requirement id {r.get('id')!r} must match R<n>")
        if not r.get("text"):
            errs.append(f"synthesis: requirement {r.get('id')} has no text")
        for sid in r.get("source_ids") or []:
            if sid not in known:
                errs.append(f"synthesis: requirement {r.get('id')} cites unknown source {sid}")
    return errs


def check_prd_md(text: str, research: dict) -> list:
    errs = []
    for h in PRD_HEADINGS:
        if h not in text:
            errs.append(f"prd: missing '{h}' section")
    known = {s.lstrip("S") for s in _source_ids(research)}
    for n in set(CITE.findall(text)):
        if n not in known:
            errs.append(f"prd: citation (S{n}) has no matching research source")
    if "## Acceptance criteria" in text:
        body = text.split("## Acceptance criteria", 1)[1].split("\n## ", 1)[0]
        for line in body.splitlines():
            if line.startswith("- ") and not TESTABLE.search(line):
                errs.append(f"prd: acceptance criterion not testable (needs a number/threshold/pass-fail): {line[:60]!r}")
    for marker in ("TBD", "TODO"):
        if marker in text:
            errs.append(f"prd: contains '{marker}'")
    return errs
