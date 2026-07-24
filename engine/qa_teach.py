#!/usr/bin/env python3
"""qa_teach.py — `orbit teach`: record a browser flow by demonstration into a
committed flow library. The supervised-learning entry point of the QA vision
(Phase 4): a human drives Playwright's own recording browser (`codegen`)
once, states their intent + success criteria, and Orbit commits all three
(intent, raw steps, replay bookkeeping) as one flow under
`$AP_HOME/<qa.flows_dir>/<name>/` — ground truth the replay skill (Task 21)
and heal protocol (Task 26) both consume later.

No agent is present during a `teach` session — this is a deterministic CLI
tool the human runs directly, which is why it does its own qa.services
up/down (via engine/qa_env.py's up_ctx, Task 19) instead of an agent driving
Bash itself the way /orbit-qa-check does.

Flow-file format (BINDING — see .superpowers/sdd/task-20-brief.md):
    $AP_HOME/<flows_dir>/<name>/
        intent.md       frontmatter (name/created/entry_path/tags) + the
                         user's natural-language intent + `## Success
                         criteria` list
        steps.spec.ts    raw codegen output, byte-for-byte — ground truth
        meta.json        {"created_at", "last_run_at": null,
                          "last_verdict": null, "runs": 0, "heals": []}
    steps.json (the normalized, healable form) is NOT written here — the QA
    agent writes it on first successful replay (Task 22).
    $AP_HOME/<flows_dir's parent>/FLOWMAP.md — one line per flow:
        | name | intent one-liner | entry path | last verdict |
    (created with a header on the first flow; re-teaching the same name
    updates its row in place rather than duplicating it.)

Exit codes:
    0   flow written to disk (a human still needs to `git add`/commit it)
    1   the recording/interactive session didn't yield usable material:
        the recorder exited nonzero or wrote no/an empty spec file, qa.services
        never came up, or the human gave an empty intent / zero success
        criteria on stdin. No flow dir is ever left behind on this path.
    2   argument/config shape is wrong before any session was even attempted:
        bad flow-name slug, no `qa:` block configured, the flow name already
        exists, a malformed qa.flows_dir, or no target URL available
        (neither --url nor qa.base_url).

Config (`.autopilot/config.yaml`, see config/schema.yaml):
    qa:
      base_url: "..."      # target URL = --url > qa.base_url; neither -> exit 2
      services: [...]      # brought up/down around the recording session
                            # (qa_env.up() is already a no-op with none configured)
      flows_dir: "qa/flows"  # default; validated by config.py's validate()

Test hook: ORBIT_CODEGEN_CMD replaces the `npx playwright codegen` call
verbatim (same substitution mechanism as ORBIT_QA_CMD/ORBIT_RUNTIME_CMD —
see qa_check.py). ORBIT_CODEGEN_OUTPUT and ORBIT_CODEGEN_URL are exported
into the child's env either way, so an override script can see what was
resolved without parsing argv (an override replaces argv wholesale).

The engine never runs git on the target — teach prints a `git add` hint;
the human commits.

Usage: qa_teach.py <target> <flow-name> [--url URL]
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from backlog_append import SLUG_RE, yq
import qa_env

DEFAULT_FLOWS_DIR = "qa/flows"
FLOWMAP_HEADER = "| name | intent | entry path | last verdict |\n| --- | --- | --- | --- |\n"

USAGE = "usage: qa_teach.py <target> <flow-name> [--url URL]"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(msg: str) -> None:
    print(f"qa_teach: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def _load_cfg(target: Path) -> dict:
    cfg_path = target / ".autopilot" / "config.yaml"
    try:
        return yaml.safe_load(cfg_path.read_text()) or {}
    except OSError:
        return {}


def _bad_flows_dir_reason(fd) -> str | None:
    """Mirrors config.py's validate() check. Unlike the small literal
    defaults each engine script keeps its own copy of (DEFAULT_FLOWS_DIR
    here, DEFAULT_QA_FLOWS_DIR in command_center.py — see qa_check.py's
    _TID_RE for that precedent), THIS shape/escape check is imported (not
    copy-pasted) by qa_run.py (teach-parity: same rejection, exit 2) and by
    command_center.py's _qa_flows_root (silent fallback to the default
    instead — a dashboard poll loop must never error on a bad config value)
    — a path-escape guard must never be allowed to drift between its
    consumers the way a cosmetic default safely can. None = fd is fine to
    use as-is."""
    if not isinstance(fd, str) or not fd.strip():
        return "must be a non-empty string"
    if fd.startswith("/"):
        return "must be a relative path (no leading '/')"
    if ".." in fd.split("/"):
        return "must not contain '..' segments"
    return None


# ---------------------------------------------------------------------------
# recorder (Playwright codegen), foreground
# ---------------------------------------------------------------------------

def _run_codegen(target: Path, url: str, output_path: Path) -> int:
    """Foreground, blocking: the human interacts with the recording browser
    directly. Never redirects stdio (unlike the backgrounded qa.services
    launches or the headless agent calls elsewhere in engine/) — that's what
    "foreground" means here. Returns the recorder's exit code, or 1 if it
    could not even be launched."""
    override = os.environ.get("ORBIT_CODEGEN_CMD")
    if override:
        cmd = shlex.split(override)
    else:
        cmd = ["npx", "playwright", "codegen", "--output", str(output_path), url]
    env = dict(os.environ, ORBIT_CODEGEN_OUTPUT=str(output_path), ORBIT_CODEGEN_URL=url)
    try:
        return subprocess.run(cmd, cwd=str(target), env=env).returncode
    except OSError as e:
        _err(f"could not launch the recorder ({e})")
        return 1


# ---------------------------------------------------------------------------
# interactive prompts — plain input(), EOF-safe
# ---------------------------------------------------------------------------

def _prompt_intent() -> str:
    print("Describe what this flow does, in one line:")
    try:
        return input("> ").strip()
    except EOFError:
        return ""


def _prompt_criteria() -> list[str]:
    print("Success criteria, one per line (blank line to finish):")
    out: list[str] = []
    while True:
        try:
            line = input("- ").strip()
        except EOFError:
            break
        if not line:
            break
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# writing the flow
# ---------------------------------------------------------------------------

def _render_intent_md(name: str, created_at: str, entry_path: str,
                       intent: str, criteria: list[str]) -> str:
    lines = [
        "---",
        f"name: {yq(name)}",
        f"created: {yq(created_at)}",
        f"entry_path: {yq(entry_path)}",
        "tags: []",
        "---",
        "",
        intent,
        "",
        "## Success criteria",
    ]
    lines += [f"- {c}" for c in criteria]
    return "\n".join(lines) + "\n"


def _write_flow(flow_dir: Path, name: str, entry_path: str, intent: str,
                 criteria: list[str], spec_tmp: Path) -> str:
    """Creates flow_dir and its three files. Returns created_at. Raises on
    any failure — caller is responsible for cleaning up flow_dir."""
    flow_dir.mkdir(parents=True)
    created_at = _now_iso()
    (flow_dir / "intent.md").write_text(
        _render_intent_md(name, created_at, entry_path, intent, criteria))
    shutil.copyfile(spec_tmp, flow_dir / "steps.spec.ts")
    meta = {"created_at": created_at, "last_run_at": None, "last_verdict": None,
             "runs": 0, "heals": []}
    (flow_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return created_at


def _cell(s: str) -> str:
    """Markdown-table-safe single cell: neuter '|' and collapse newlines —
    every field folded into a FLOWMAP row is short, human-authored text
    (the one-line intent, a URL), never large or attacker-controlled, so
    this is a display-safety escape, not a security boundary."""
    return " ".join(str(s).split()).replace("|", "\\|")


def _update_flowmap(flowmap_path: Path, name: str, intent: str,
                     entry_path: str, last_verdict: str) -> None:
    """Append a `| name | intent | entry path | last verdict |` row, or
    replace it in place if `name` already has one — idempotent-safe so a
    re-run (or a future re-teach) can never duplicate a flow's row."""
    flowmap_path.parent.mkdir(parents=True, exist_ok=True)
    text = flowmap_path.read_text() if flowmap_path.exists() else ""
    if not text.strip():
        text = FLOWMAP_HEADER
    if not text.endswith("\n"):
        text += "\n"
    row = f"| {name} | {_cell(intent)} | {_cell(entry_path)} | {last_verdict} |\n"
    pat = re.compile(rf"^\|\s*{re.escape(name)}\s*\|.*\|\s*$\n?", re.MULTILINE)
    if pat.search(text):
        text = pat.sub(lambda m: row, text, count=1)   # lambda: never re-interpret
    else:                                              # backslashes (e.g. our own
        text += row                                    # \| cells) as a re.sub template
    flowmap_path.write_text(text)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    if len(argv) < 2:
        _err(USAGE)
        return 2
    target = Path(argv[0]).resolve()
    name = argv[1]
    rest = argv[2:]

    url_arg = None
    i = 0
    while i < len(rest):
        if rest[i] == "--url" and i + 1 < len(rest):
            url_arg = rest[i + 1]
            i += 2
        else:
            _err(f"unrecognized argument {rest[i]!r} ({USAGE})")
            return 2

    if not SLUG_RE.match(name):
        _err(f"{name!r} is not a valid flow name — use a lowercase slug "
             f"(letters/digits/./_/-, starting with a letter or digit)")
        return 2

    ap_home = target / ".autopilot"
    cfg = _load_cfg(target)
    qa_cfg = cfg.get("qa")
    if not isinstance(qa_cfg, dict):
        _err(f"no 'qa:' block configured in {ap_home / 'config.yaml'} — "
             f"see config/schema.yaml's qa: section (services, base_url, "
             f"flows_dir, ...) and add one first")
        return 2

    flows_dir = qa_cfg.get("flows_dir") or DEFAULT_FLOWS_DIR
    bad = _bad_flows_dir_reason(flows_dir)
    if bad:
        _err(f"qa.flows_dir {flows_dir!r} is invalid ({bad})")
        return 2

    flow_dir = ap_home / flows_dir / name
    if flow_dir.exists():
        _err(f"a flow named {name!r} already exists at {flow_dir} — pick a "
             f"new name (re-teaching an existing flow is a later phase)")
        return 2

    url = url_arg or qa_cfg.get("base_url")
    if not url:
        _err("no target URL — pass --url or set qa.base_url in config.yaml")
        return 2

    log_dir = ap_home / "state" / "qa" / "teach-logs"

    with tempfile.TemporaryDirectory(prefix="orbit-teach-") as tmpdir:
        spec_tmp = Path(tmpdir) / "steps.spec.ts"
        try:
            with qa_env.up_ctx(target, qa_cfg, log_dir):
                print(f"Recording {url} — interact in the browser; close its "
                      f"window when the flow is complete.")
                rc = _run_codegen(target, url, spec_tmp)
                if rc != 0:
                    _err(f"recorder exited {rc} — no flow recorded")
                    return 1
                if not spec_tmp.exists() or spec_tmp.stat().st_size == 0:
                    _err("recorder produced no output — no flow recorded")
                    return 1
        except qa_env.QaEnvError as e:
            _err(f"could not bring qa.services up ({e}) — no flow recorded")
            return 1

        intent = _prompt_intent()
        if not intent:
            _err("empty intent — a one-line description is required; no flow recorded")
            return 1
        criteria = _prompt_criteria()
        if not criteria:
            _err("no success criteria given — at least one is required; no flow recorded")
            return 1

        try:
            _write_flow(flow_dir, name, url, intent, criteria, spec_tmp)
        except Exception as e:
            shutil.rmtree(flow_dir, ignore_errors=True)
            _err(f"failed writing the flow ({e}) — cleaned up")
            return 1

    # The flow itself is already fully written and complete at this point —
    # a failure updating the (secondary, regenerable) FLOWMAP index must
    # never masquerade as the teach having failed: no traceback, no
    # misleading nonzero exit that would make a retry hit "already exists"
    # on a flow that in fact succeeded.
    flowmap = (ap_home / flows_dir).parent / "FLOWMAP.md"
    try:
        _update_flowmap(flowmap, name, intent, url, "never run")
    except OSError as e:
        print(f"flow written OK at {flow_dir}; FLOWMAP update failed ({e}) — "
              f"add the row manually")
        return 0

    flow_rel = f".autopilot/{flows_dir}/{name}"
    flowmap_rel = str((Path(".autopilot") / flows_dir).parent / "FLOWMAP.md")
    print(f"taught flow {name!r} — {flow_dir}")
    print(f"  entry: {url}")
    print(f"  criteria: {len(criteria)}")
    print(f"git add {flow_rel}/ {flowmap_rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
