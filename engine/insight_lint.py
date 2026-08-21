#!/usr/bin/env python3
"""Tier-A deterministic lint for insight docs (spec sections 4 and 5): structure
plus citation integrity, the mechanical half of the grounding contract. Used as
the product workspace's gate, so a cycle cannot ship an ungrounded insight.
Exit 0 = clean (or no insights/ directory yet); exit 1 = violations printed.
Design reference: dwh-suite's prd_evals.py Tier-A checks."""
import re, sys
from pathlib import Path

CITE = re.compile(r"\(S(\d+)\)")
SOURCE = re.compile(r"^- S(\d+)[ :—-]", re.M)


def lint_file(path: Path, survey: bool = False) -> list:
    errs = []
    text = path.read_text()
    rel = path.name
    if not text.startswith("# "):
        errs.append(f"{rel}: must start with a '# ' title")
    if "## Answer" not in text:
        errs.append(f"{rel}: missing '## Answer' section")
    if "## Basis" not in text:
        errs.append(f"{rel}: missing '## Basis' section")
        return errs
    basis = text.split("## Basis", 1)[1]
    defined = set(SOURCE.findall(basis))
    if not defined:
        errs.append(f"{rel}: Basis has no typed source lines ('- S1: ...')")
    for n in set(CITE.findall(text)):
        if n not in defined:
            errs.append(f"{rel}: citation (S{n}) has no matching source in Basis")
    for marker in ("TBD", "TODO"):
        if marker in text:
            errs.append(f"{rel}: contains '{marker}' — abstain-over-guess means gaps go in Basis, not markers")
    if survey:
        quote_lines = [line for line in text.split("\n") if line.startswith("> ")]
        has_coverage_gaps = "## Coverage gaps" in text
        if len(quote_lines) < 3 and not has_coverage_gaps:
            errs.append(f"{rel}: must contain at least three verbatim quotes (lines starting '> ') or a '## Coverage gaps' section")
    return errs


def main(repo: str) -> int:
    repo_path = Path(repo)
    errs = []
    insights_dir = repo_path / "insights"
    if insights_dir.is_dir():
        for f in sorted(insights_dir.glob("*.md")):
            errs += lint_file(f)
    surveys_dir = repo_path / "surveys"
    if surveys_dir.is_dir():
        for f in sorted(surveys_dir.glob("*/report.md")):
            errs += lint_file(f, survey=True)
    for e in errs:
        print(f"  ✗ {e}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
