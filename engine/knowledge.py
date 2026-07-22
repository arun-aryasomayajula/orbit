#!/usr/bin/env python3
"""knowledge.py — headless wrapper for the /orbit-knowledge survey skill: builds
and deterministically validates the project knowledge base under
`$AP_HOME/knowledge/`.

`build` runs ONE headless Claude Code call (the survey skill), then
post-validates what it wrote:
  - `manifest.json` exists, parses, and has `version`, `generated_at`, a
    non-empty `docs` list, and a `files` dict. A missing/empty `base_sha` is
    stamped from `git rev-parse HEAD` of the target's repo and the manifest is
    rewritten (this is not a failure — it's the engine finishing the agent's
    job for it).
  - every `docs` entry exists on disk under `knowledge/`.
  - `INDEX.md` exists and is <= 80 lines.
  - IF `er.mmd` exists, its first non-empty line is exactly `erDiagram`; IF
    `mindmap.mmd` exists, its first non-empty line is exactly `mindmap`
    (conditional: not every repo has a DB or deep enough structure for
    either — the skill decides what to write, the engine validates what's
    there).
  - IF `endpoints.md` exists, it has at least one real markdown-table data
    row (not just the header/separator).

Any validation failure prints one `VALIDATE: <reason>` line per failure,
leaves every file on disk for inspection, and exits 1 — a knowledge base a
human can't trust must not look done. Success prints a one-line summary and
exits 0.

`refresh|status|autorefresh` are recognized subcommands reserved for a later
task; they currently print a message and exit 2 so Task 4 can fill them in
without touching this dispatch.

Test hook: ORBIT_KNOWLEDGE_CMD replaces the claude call (shell, same env/cwd)
— see engine/runtime_check.py for the identical pattern.

Usage: knowledge.py <target> build|refresh|status|autorefresh [--timeout N] [--quiet]
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parent
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or ENGINE.parent)

DEFAULT_TIMEOUT = 1800      # default cap for the survey call
GIT_TIMEOUT = 30

NOT_IMPLEMENTED = ("refresh", "status", "autorefresh")

ENDPOINTS_HEADER = ("| method + path | handler (file:symbol) | calls "
                    "| tables read | tables written | auth? |")


def run_survey_agent(target: Path, cfg: dict, timeout: int, quiet: bool) -> bool:
    """One headless Claude Code call: /orbit-knowledge (full build, no args)."""
    override = os.environ.get("ORBIT_KNOWLEDGE_CMD")
    ap = target / ".autopilot"
    env = dict(os.environ)
    env.update(AP_HOME=str(ap), AP_STATE=str(ap / "state"), ORBIT_HOME=str(ORBIT_HOME))
    if override:
        cmd = shlex.split(override)
    else:
        claude = shutil.which("claude")
        if not claude:
            print("  ✗ claude not on PATH — cannot run the survey agent")
            return False
        cmd = [claude, "--print", "/orbit-knowledge",
               "--permission-mode", cfg.get("permission_mode", "acceptEdits"),
               "--settings", str(ORBIT_HOME / "config" / "orbit.settings.json"),
               "--model", cfg.get("model", "claude-opus-4-8"),
               "--add-dir", str(ORBIT_HOME)]
    if not quiet:
        print(f"  · survey agent: {' '.join(shlex.quote(c) for c in cmd[:3])} … (≤{timeout}s)")
    try:
        p = subprocess.run(cmd, cwd=target, env=env, timeout=timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL)
        return p.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"  ✗ survey agent timed out after {timeout}s")
        return False
    except OSError as e:
        print(f"  ✗ survey agent failed to start: {e}")
        return False


def _git_head_sha(repo: Path) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT)
        return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _first_nonempty_line(path: Path) -> str | None:
    for line in path.read_text().splitlines():
        if line.strip():
            return line.strip()
    return None


def _has_endpoints_data_row(path: Path) -> bool:
    header = re.sub(r"\s+", " ", ENDPOINTS_HEADER)
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        if re.sub(r"\s+", " ", s) == header:
            continue                                 # the header row itself
        if re.fullmatch(r"[|:\-\s]+", s):
            continue                                 # a `|---|---|` separator row
        return True
    return False


def validate(kdir: Path, repo: Path) -> list[str]:
    """Deterministically check what the survey agent wrote. Returns a list of
    human-readable failure reasons (empty = valid). May rewrite manifest.json
    in place to stamp a missing base_sha — that's a repair, not a failure."""
    failures: list[str] = []

    manifest_path = kdir / "manifest.json"
    manifest: dict | None = None
    if not manifest_path.exists():
        failures.append(f"manifest.json missing under {kdir}/")
    else:
        try:
            loaded = json.loads(manifest_path.read_text())
        except (OSError, ValueError) as e:
            failures.append(f"manifest.json is not valid JSON ({e})")
        else:
            if not isinstance(loaded, dict):
                failures.append("manifest.json must be a JSON object")
            else:
                manifest = loaded

    if manifest is not None:
        for key in ("version", "generated_at"):
            if not manifest.get(key):
                failures.append(f"manifest.json missing required field '{key}'")
        docs = manifest.get("docs")
        if not isinstance(docs, list) or not docs:
            failures.append("manifest.json 'docs' must be a non-empty list")
            docs = []
        if not isinstance(manifest.get("files"), dict):
            failures.append("manifest.json 'files' must be a dict")

        if not manifest.get("base_sha"):
            sha = _git_head_sha(repo)
            if sha:
                manifest["base_sha"] = sha
                manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
                print(f"  · manifest.json had no base_sha — stamped {sha[:12]} from {repo}")
            else:
                failures.append(
                    f"manifest.json has no base_sha and `git -C {repo} rev-parse HEAD` failed")

        for doc in docs:
            if not (kdir / doc).exists():
                failures.append(f"manifest.json lists doc '{doc}' but it does not exist under {kdir}/")

    index_path = kdir / "INDEX.md"
    if not index_path.exists():
        failures.append(f"INDEX.md missing under {kdir}/")
    else:
        n = len(index_path.read_text().splitlines())
        if n > 80:
            failures.append(f"INDEX.md is {n} lines, must be <= 80")

    er_path = kdir / "er.mmd"
    if er_path.exists():
        first = _first_nonempty_line(er_path)
        if first != "erDiagram":
            failures.append(f"er.mmd first non-empty line must be exactly 'erDiagram', got {first!r}")

    mindmap_path = kdir / "mindmap.mmd"
    if mindmap_path.exists():
        first = _first_nonempty_line(mindmap_path)
        if first != "mindmap":
            failures.append(f"mindmap.mmd first non-empty line must be exactly 'mindmap', got {first!r}")

    endpoints_path = kdir / "endpoints.md"
    if endpoints_path.exists() and not _has_endpoints_data_row(endpoints_path):
        failures.append("endpoints.md has no data rows (only a header/separator) under "
                         f"{kdir}/")

    return failures


def _usage_exit(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        return 2
    target = Path(args[0]).resolve()
    subcmd = args[1]
    quiet = "--quiet" in argv

    ap = target / ".autopilot"
    cfg_path = ap / "config.yaml"
    if not cfg_path.exists():
        return _usage_exit(f"no {cfg_path} — run `orbit init {target}` first")
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError as e:
        return _usage_exit(f"unreadable config at {cfg_path}: {e}")
    if not isinstance(cfg, dict):
        return _usage_exit(f"unreadable config at {cfg_path}: expected a mapping, got {cfg!r}")

    if subcmd in NOT_IMPLEMENTED:
        return _usage_exit(f"knowledge {subcmd}: not implemented (Task 4)")
    if subcmd != "build":
        print(__doc__, file=sys.stderr)
        return 2

    kb_cfg = cfg.get("knowledge") if isinstance(cfg.get("knowledge"), dict) else {}
    timeout = int(kb_cfg.get("timeout_seconds") or DEFAULT_TIMEOUT)
    for i, a in enumerate(argv):
        if a == "--timeout" and i + 1 < len(argv):
            timeout = int(argv[i + 1])

    kdir = ap / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)

    if not quiet:
        print(f"ORBIT KNOWLEDGE BUILD — {target}\n")

    if not run_survey_agent(target, cfg, timeout, quiet):
        print("  ✗ survey agent failed — knowledge build aborted (nothing further validated)",
              file=sys.stderr)
        return 1

    repo_rel = cfg.get("repo") or "."
    repo = (target / repo_rel).resolve() if not os.path.isabs(repo_rel) else Path(repo_rel)

    failures = validate(kdir, repo)
    if failures:
        for f in failures:
            print(f"VALIDATE: {f}")
        return 1

    docs = json.loads((kdir / "manifest.json").read_text()).get("docs") or []
    print(f"  ✓ knowledge build complete — {len(docs)} docs written to {kdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
