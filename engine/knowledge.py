#!/usr/bin/env python3
"""knowledge.py — headless wrapper for the /orbit-knowledge survey skill: builds,
validates, and keeps fresh the project knowledge base under `$AP_HOME/knowledge/`.

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
  - IF `graph.json` exists, it parses as a JSON object with `version` (int),
    `nodes` (list), and `edges` (list); every node has a unique non-empty
    string `id`, a `type` in {endpoint, function, table, flow}, and a string
    `label`; every edge's `source`/`target` reference an existing node id and
    its `type` is in {handles, calls, reads, writes, relates, step}; an
    edge's `order`, if present, must be an int (a stringified number is a
    failure, not a repair — it silently loses flow ordering client-side)
    (optional — validated only when the file is present).

Any validation failure prints one `VALIDATE: <reason>` line per failure,
leaves every file on disk for inspection, and exits 1 — a knowledge base a
human can't trust must not look done. Success prints a one-line summary and
exits 0.

`status` is deterministic Python — no agent call. It reads manifest.json and
`$AP_STATE/merge_markers.jsonl` (dashboard-written on every human merge), maps
each merge landed after the manifest's `generated_at` to changed files (via
`git show`, or a patch-file fallback when the sha is unreachable), and maps
those files to `manifest.files` entries to find which docs are now stale.
Writes `$AP_STATE/knowledge-staleness.json`. Exits 0 fresh, 4 stale — never
mutates knowledge/.

`refresh` computes staleness exactly like `status`; if fresh it exits 0
without running the agent. If stale, it writes
`$AP_STATE/knowledge/refresh-scope.json` (the stale docs + the changed files
that caused them), runs the survey agent with the `refresh` skill argument,
then applies the same post-validation as `build`.

`autorefresh` is the every-cycle loop hook (wired into run.sh in a later
task): acts only when config `knowledge.refresh == "auto"`, a manifest
exists, stale-doc count >= `knowledge.stale_after_merges` (default 3), and
`$AP_STATE/.knowledge-refreshed` is absent or >24h old. It ALWAYS exits 0 —
loop safety over completeness — printing a `WARN:` line on any failure
(including unexpected exceptions) instead of ever propagating one.

Before every survey call (build AND refresh), the target's
`.claude/commands/orbit-knowledge.md` is (re)synced from
`$ORBIT_HOME/skills/orbit-knowledge.md` — that's what `/orbit-knowledge`
actually resolves to in the target repo, and a target onboarded before a
feature landed in this file otherwise runs a stale copy of it forever.
While the real `claude` call is in flight, a heartbeat line ("· still
surveying the repo… (elapsed Ns)") is printed to the wrapper's own stdout
every ORBIT_KNOWLEDGE_HEARTBEAT_SECS (default 20s) so a tail of
knowledge.log shows movement instead of looking frozen for minutes.

Test hook: ORBIT_KNOWLEDGE_CMD replaces the claude call (shell, same env/cwd)
— see engine/qa_check.py's legacy-path agent runner for the identical pattern.

Usage: knowledge.py <target> build|refresh|status|autorefresh [--timeout N] [--quiet]
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parent
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or ENGINE.parent)

DEFAULT_TIMEOUT = 1800      # default cap for the survey call
GIT_TIMEOUT = 30
DEFAULT_STALE_AFTER_MERGES = 3
AUTOREFRESH_COOLDOWN_SECONDS = 24 * 3600
DEFAULT_HEARTBEAT_SECS = 20.0   # elapsed-time heartbeat cadence during the real survey call

ENDPOINTS_HEADER = ("| method + path | handler (file:symbol) | calls "
                    "| tables read | tables written | auth? |")

GRAPH_NODE_TYPES = {"endpoint", "function", "table", "flow"}
GRAPH_EDGE_TYPES = {"handles", "calls", "reads", "writes", "relates", "step"}


def _sync_knowledge_command(target: Path, quiet: bool) -> None:
    """Copy $ORBIT_HOME/skills/orbit-knowledge.md into
    <target>/.claude/commands/orbit-knowledge.md (overwrite), before every
    survey call — build AND refresh, since both funnel through
    run_survey_agent. `/orbit-knowledge` resolves from the TARGET repo's own
    `.claude/commands/`, copied there once at onboarding by install/init.py
    (or `orbit sync`); a target onboarded before a feature (e.g. graph.json)
    landed in this file keeps running the STALE copy forever otherwise —
    confirmed on a real target: 11 docs written, no graph.json, because its
    command file predated the graph.json instruction. Re-syncing this ONE
    command file (not the rest of skills/, which onboarding/`orbit sync`
    already own) on every build/refresh keeps it current with zero extra
    operator steps. Never abort the build over this: a permission error or a
    read-only mount becomes a WARN, not a failure — the survey can still run
    against whatever command version is already there."""
    src = ORBIT_HOME / "skills" / "orbit-knowledge.md"
    dest = target / ".claude" / "commands" / "orbit-knowledge.md"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)
        if not quiet:
            print("  · synced the /orbit-knowledge command into the project (.claude/commands/)")
    except OSError as e:
        print(f"  WARN: could not sync .claude/commands/orbit-knowledge.md ({e})")


def _start_heartbeat(interval: float) -> tuple[threading.Thread, threading.Event]:
    """Daemon thread that prints an elapsed-time line to the wrapper's own
    stdout (which is what knowledge.log captures) every `interval` seconds
    while the headless survey subprocess is in flight. Without this,
    knowledge.log shows the startup banner and the "· survey agent: …" line,
    then nothing for minutes until "complete" — the UI Run view (tailing
    /api/cmdlog?name=knowledge) looks frozen even though the agent is working.
    Not a stream-json parser — just proof of life. Caller must stop_event.set()
    then thread.join() as soon as the subprocess returns, before printing the
    real completion line."""
    stop_event = threading.Event()
    start = time.monotonic()

    def _loop() -> None:
        while not stop_event.wait(interval):
            elapsed = int(time.monotonic() - start)
            print(f"  · still surveying the repo… (elapsed {elapsed}s)")
            sys.stdout.flush()

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return thread, stop_event


def run_survey_agent(target: Path, cfg: dict, timeout: int, quiet: bool,
                      skill: str | None = None) -> bool:
    """One headless Claude Code call: /orbit-knowledge (full build, no args) or
    /orbit-knowledge <skill> (e.g. "refresh") for an incremental pass.
    ORBIT_KNOWLEDGE_SKILL is exported too and `skill` is appended to an
    ORBIT_KNOWLEDGE_CMD override's argv — either lets a test fake tell which
    skill it was invoked with without parsing the real --print string."""
    override = os.environ.get("ORBIT_KNOWLEDGE_CMD")
    ap = target / ".autopilot"
    env = dict(os.environ)
    env.update(AP_HOME=str(ap), AP_STATE=str(ap / "state"), ORBIT_HOME=str(ORBIT_HOME),
               ORBIT_KNOWLEDGE_SKILL=skill or "build")

    _sync_knowledge_command(target, quiet)

    if override:
        cmd = shlex.split(override) + ([skill] if skill else [])
    else:
        claude = shutil.which("claude")
        if not claude:
            print("  ✗ claude not on PATH — cannot run the survey agent")
            return False
        skill_arg = f"/orbit-knowledge {skill}" if skill else "/orbit-knowledge"
        cmd = [claude, "--print", skill_arg,
               "--permission-mode", cfg.get("permission_mode", "acceptEdits"),
               "--settings", str(ORBIT_HOME / "config" / "orbit.settings.json"),
               "--model", cfg.get("model", "claude-opus-4-8"),
               "--add-dir", str(ORBIT_HOME)]
    if not quiet:
        print(f"  · survey agent: {' '.join(shlex.quote(c) for c in cmd[:3])} … (≤{timeout}s)")

    # Heartbeat only on the real `claude` path (no ORBIT_KNOWLEDGE_CMD override)
    # — that's the call that can genuinely run for minutes; a test fake
    # returns effectively instantly and would never emit a tick anyway.
    heartbeat_thread = heartbeat_stop = None
    if not override:
        hb_interval = float(os.environ.get("ORBIT_KNOWLEDGE_HEARTBEAT_SECS", DEFAULT_HEARTBEAT_SECS))
        heartbeat_thread, heartbeat_stop = _start_heartbeat(hb_interval)
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
    finally:
        if heartbeat_thread is not None:
            heartbeat_stop.set()
            heartbeat_thread.join()


def _git_head_sha(repo: Path) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT)
        return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _first_nonempty_line(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def _has_endpoints_data_row(text: str) -> bool:
    header = re.sub(r"\s+", " ", ENDPOINTS_HEADER)
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        if re.sub(r"\s+", " ", s) == header:
            continue                                 # the header row itself
        if re.fullmatch(r"[|:\-\s]+", s):
            continue                                 # a `|---|---|` separator row
        return True
    return False


def _read_text(path: Path) -> tuple[str | None, str | None]:
    """(text, None) on success, (None, reason) on any read/decode failure — turns
    a raw traceback (non-UTF8, permission, gone-between-check-and-read) into a
    clean VALIDATE: line instead."""
    try:
        return path.read_text(), None
    except (OSError, ValueError) as e:            # ValueError covers UnicodeDecodeError
        return None, str(e)


def _unsafe_doc_reason(kdir: Path, doc) -> str | None:
    """None = doc resolves safely under kdir. Otherwise a human-readable reason
    it was rejected — an agent-controlled manifest.json must never be able to
    point validation (or a future reader) at a path outside knowledge/."""
    if not isinstance(doc, str) or not doc:
        return "not a valid relative path"
    if os.path.isabs(doc):
        return "absolute paths are not allowed — must be relative to knowledge/"
    try:
        resolved = (kdir / doc).resolve()
    except (OSError, ValueError) as e:      # ValueError: e.g. an embedded NUL byte
        return f"could not resolve path ({e})"
    if not resolved.is_relative_to(kdir.resolve()):
        return "escapes knowledge/ (path traversal)"
    return None


def _graph_json_failures(graph: dict) -> list[str]:
    """Field-level checks for an already-parsed `graph.json` object. Returns
    human-readable reasons (without the 'graph.json' prefix — the caller adds
    that); empty = valid. Mirrors validate()'s never-crash-on-untrusted-input
    discipline: a survey agent's graph.json is treated exactly like its
    manifest.json — checked, never trusted enough to index into blindly."""
    failures: list[str] = []
    if not isinstance(graph.get("version"), int):
        failures.append("missing/invalid 'version' (must be an int)")

    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        failures.append("'nodes' must be a list")
        nodes = []
    edges = graph.get("edges")
    if not isinstance(edges, list):
        failures.append("'edges' must be a list")
        edges = []

    seen_ids: set = set()
    node_ids: set = set()
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            failures.append(f"node at index {i} is not an object")
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            failures.append(f"node at index {i} has no valid non-empty string 'id'")
        else:
            if node_id in seen_ids:
                failures.append(f"node id {node_id!r} is not unique")
            seen_ids.add(node_id)
            node_ids.add(node_id)
        # A node's 'type' can legally be JSON of any shape (a list/dict is
        # valid JSON, just not a valid type) — `x not in a_set` HASHES x, so
        # checking membership before confirming x is even a string would
        # crash with an unhandled TypeError on that input instead of
        # reporting it as the VALIDATE failure it is.
        node_type = node.get("type")
        if not isinstance(node_type, str):
            failures.append(f"node {node.get('id', i)!r} has non-string 'type' {node_type!r} "
                             f"(must be one of {sorted(GRAPH_NODE_TYPES)})")
        elif node_type not in GRAPH_NODE_TYPES:
            failures.append(f"node {node.get('id', i)!r} has invalid 'type' {node_type!r} "
                             f"(must be one of {sorted(GRAPH_NODE_TYPES)})")
        if not isinstance(node.get("label"), str):
            failures.append(f"node {node.get('id', i)!r} has no string 'label'")

    for i, edge in enumerate(edges):
        if not isinstance(edge, dict):
            failures.append(f"edge at index {i} is not an object")
            continue
        # Same unhashable-value guard as node 'type' above, for 'source'/
        # 'target' (membership-tested against the node_ids set) and 'type'.
        source, target = edge.get("source"), edge.get("target")
        if not isinstance(source, str):
            failures.append(f"edge at index {i} has non-string 'source' {source!r}")
        elif source not in node_ids:
            failures.append(f"edge at index {i} has source {source!r} which is not a known node id")
        if not isinstance(target, str):
            failures.append(f"edge at index {i} has non-string 'target' {target!r}")
        elif target not in node_ids:
            failures.append(f"edge at index {i} has target {target!r} which is not a known node id")
        edge_type = edge.get("type")
        if not isinstance(edge_type, str):
            failures.append(f"edge at index {i} has non-string 'type' {edge_type!r} "
                             f"(must be one of {sorted(GRAPH_EDGE_TYPES)})")
        elif edge_type not in GRAPH_EDGE_TYPES:
            failures.append(f"edge at index {i} has invalid 'type' {edge_type!r} "
                             f"(must be one of {sorted(GRAPH_EDGE_TYPES)})")
        if "order" in edge and not isinstance(edge.get("order"), int):
            failures.append(f"edge at index {i} has non-int 'order' {edge.get('order')!r} "
                             f"(the UI silently drops a non-numeric order, losing flow ordering)")

    return failures


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
            # containment BEFORE any existence check or read — an agent-written
            # manifest.json is untrusted input; it must never make validation
            # (or any future consumer) touch a path outside knowledge/.
            unsafe = _unsafe_doc_reason(kdir, doc)
            if unsafe:
                failures.append(f"manifest.json lists doc '{doc}' which is unsafe ({unsafe}) — rejected")
            elif not (kdir / doc).exists():
                failures.append(f"manifest.json lists doc '{doc}' but it does not exist under {kdir}/")

    index_path = kdir / "INDEX.md"
    if not index_path.exists():
        failures.append(f"INDEX.md missing under {kdir}/")
    else:
        text, err = _read_text(index_path)
        if err:
            failures.append(f"INDEX.md could not be read ({err})")
        else:
            n = len(text.splitlines())
            if n > 80:
                failures.append(f"INDEX.md is {n} lines, must be <= 80")

    er_path = kdir / "er.mmd"
    if er_path.exists():
        text, err = _read_text(er_path)
        if err:
            failures.append(f"er.mmd could not be read ({err})")
        else:
            first = _first_nonempty_line(text)
            if first != "erDiagram":
                failures.append(f"er.mmd first non-empty line must be exactly 'erDiagram', got {first!r}")

    mindmap_path = kdir / "mindmap.mmd"
    if mindmap_path.exists():
        text, err = _read_text(mindmap_path)
        if err:
            failures.append(f"mindmap.mmd could not be read ({err})")
        else:
            first = _first_nonempty_line(text)
            if first != "mindmap":
                failures.append(f"mindmap.mmd first non-empty line must be exactly 'mindmap', got {first!r}")

    endpoints_path = kdir / "endpoints.md"
    if endpoints_path.exists():
        text, err = _read_text(endpoints_path)
        if err:
            failures.append(f"endpoints.md could not be read ({err})")
        elif not _has_endpoints_data_row(text):
            failures.append("endpoints.md has no data rows (only a header/separator) under "
                             f"{kdir}/")

    graph_path = kdir / "graph.json"
    if graph_path.exists():
        text, err = _read_text(graph_path)
        if err:
            failures.append(f"graph.json could not be read ({err})")
        else:
            graph: dict | None = None
            try:
                loaded = json.loads(text)
            except ValueError as e:
                failures.append(f"graph.json is not valid JSON ({e})")
            else:
                if not isinstance(loaded, dict):
                    failures.append("graph.json must be a JSON object")
                else:
                    graph = loaded
            if graph is not None:
                for reason in _graph_json_failures(graph):
                    failures.append(f"graph.json {reason}")

    return failures


def _resolve_repo(target: Path, cfg: dict) -> Path:
    repo_rel = cfg.get("repo") or "."
    return (target / repo_rel).resolve() if not os.path.isabs(repo_rel) else Path(repo_rel)


def cmd_build(target: Path, cfg: dict, ap: Path, timeout: int, quiet: bool) -> int:
    kdir = ap / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)

    if not quiet:
        print(f"ORBIT KNOWLEDGE BUILD — {target}\n")

    if not run_survey_agent(target, cfg, timeout, quiet):
        print("  ✗ survey agent failed — knowledge build aborted (nothing further validated)",
              file=sys.stderr)
        return 1

    repo = _resolve_repo(target, cfg)
    failures = validate(kdir, repo)
    if failures:
        for f in failures:
            print(f"VALIDATE: {f}")
        return 1

    docs = json.loads((kdir / "manifest.json").read_text()).get("docs") or []
    print(f"  ✓ knowledge build complete — {len(docs)} docs written to {kdir}")
    return 0


# --- staleness: status/refresh/autorefresh ----------------------------------
# Deterministic Python over merge markers ($AP_STATE/merge_markers.jsonl,
# written by command_center.do_mark on every human merge) + git — no agent
# call. Shared by `status` (read-only report) and `refresh` (acts on it).

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s) -> "datetime | None":
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_manifest_for_staleness(kdir: Path) -> dict | None:
    """None = no usable manifest (missing, unreadable, or not an object) —
    status/refresh/autorefresh all treat that the same way: nothing to check."""
    manifest_path = kdir / "manifest.json"
    if not manifest_path.exists():
        return None
    text, err = _read_text(manifest_path)
    if err:
        return None
    try:
        loaded = json.loads(text)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _unsafe_patch_reason(ap_state: Path, patch) -> str | None:
    """None = patch resolves safely under $AP_STATE/diffs/ (where the wrapper
    always writes backup patches — see run.sh's DIFFDIR). Mirrors
    `_unsafe_doc_reason`'s containment discipline: a merge marker's `patch`
    field is written by the dashboard from ledger data — untrusted enough that
    a future-corrupted/hand-edited entry must never make us read outside
    diffs/."""
    if not isinstance(patch, str) or not patch:
        return "no patch path recorded"
    diffdir = ap_state / "diffs"
    try:
        resolved = (ap_state / patch).resolve()
        diffdir_resolved = diffdir.resolve()
    except OSError as e:
        return f"could not resolve path ({e})"
    if not resolved.is_relative_to(diffdir_resolved):
        return "escapes diffs/ (path traversal)"
    return None


_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)


def _combine_reasons(primary: str | None, secondary: str | None) -> str | None:
    if primary and secondary:
        return f"{primary}; {secondary}"
    return primary or secondary


def _changed_paths_for_marker(repo: Path, ap_state: Path, sha: str,
                               patch) -> tuple[list[str] | None, str | None]:
    """(paths, None) on success — paths may be empty ("this merge touched
    nothing", not a failure). (None, reason) when BOTH the git lookup and the
    patch-file fallback failed — the caller turns `reason` into its WARN line
    so a rejected/traversal patch reads differently from a merely-missing
    one.

    `sha` is agent-written data (a merge marker's ledger call, verbatim) —
    untrusted enough that it must never reach a git argv unguarded. Before any
    git invocation it's checked against `_SHA_RE`; anything else (e.g. a
    `--output=/path` flag-injection attempt) is rejected outright and treated
    exactly like an unreachable sha — same patch-file fallback, same
    warn-and-skip path, with the rejection reason carried through so the WARN
    says *why*. `--end-of-options` is also inserted ahead of the sha in the
    git argv itself as defense in depth, in case the regex guard is ever
    weakened or bypassed."""
    sha_reason = None
    if sha:
        if _SHA_RE.match(sha):
            try:
                p = subprocess.run(
                    ["git", "-C", str(repo), "show", "--name-only", "--format=",
                     "--end-of-options", sha],
                    capture_output=True, text=True, timeout=GIT_TIMEOUT)
                if p.returncode == 0:
                    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()], None
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            sha_reason = (f"sha {sha!r} is not a valid git sha (must match "
                          "^[0-9a-f]{7,64}$, case-insensitive) — rejected before any git call")
    # git failed (unreachable sha), the sha was rejected as unsafe, or there was
    # no sha at all — fall back to the backup patch.
    reason = _unsafe_patch_reason(ap_state, patch)
    if reason:
        return None, _combine_reasons(sha_reason, reason)
    patch_path = (ap_state / patch).resolve()
    if not patch_path.is_file():
        return None, _combine_reasons(sha_reason, f"patch file not found ({patch_path})")
    text, err = _read_text(patch_path)
    if err:
        return None, _combine_reasons(sha_reason, f"patch file could not be read ({err})")
    return [ln[len("+++ b/"):].strip() for ln in text.splitlines() if ln.startswith("+++ b/")], None


def _map_path_to_docs(path: str, files_map: dict) -> set[str]:
    """manifest.files key match for one changed path: exact, directory prefix
    (key ends with "/"), else fnmatch glob. Every matching key's docs are
    unioned in (a path can legitimately fall under more than one mapping)."""
    docs: set[str] = set()
    for key, val in (files_map or {}).items():
        if not isinstance(key, str):
            continue
        matched = (key == path) or (key.endswith("/") and path.startswith(key)) \
            or fnmatch.fnmatch(path, key)
        if matched:
            if isinstance(val, list):
                docs.update(v for v in val if isinstance(v, str))
            elif isinstance(val, str):
                docs.add(val)
    return docs


def _load_merge_markers(ap_state: Path) -> list[dict]:
    markers_path = ap_state / "merge_markers.jsonl"
    if not markers_path.exists():
        return []
    text, err = _read_text(markers_path)
    if err:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if isinstance(m, dict):
            out.append(m)
    return out


def compute_staleness(manifest: dict, repo: Path, ap_state: Path) -> dict:
    """Deterministic staleness over merge markers + git — shared by `status`
    and `refresh`. Read-only: never touches knowledge/.

    Returns {generated_at, stale_docs (sorted), changed_files (sorted, only
    those that mapped to a doc), merges_considered (markers newer than
    generated_at), causes ({doc: [task_ids]}), warnings ([str])}."""
    generated_at = manifest.get("generated_at")
    gen_dt = _parse_iso(generated_at)
    files_map = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}

    considered = []
    for m in _load_merge_markers(ap_state):
        at_dt = _parse_iso(m.get("at"))
        if gen_dt is None or at_dt is None:
            continue
        try:
            newer = at_dt > gen_dt
        except TypeError:
            continue      # aware/naive mismatch — treat as unparseable, not stale
        if newer:
            considered.append(m)

    stale_docs: set[str] = set()
    causes: dict[str, set[str]] = {}
    changed_files: set[str] = set()
    warnings: list[str] = []

    for m in considered:
        sha = m.get("sha") or ""
        task_id = m.get("task_id") or "?"
        changed, reason = _changed_paths_for_marker(repo, ap_state, sha, m.get("patch"))
        if changed is None:
            warnings.append(
                f"WARN: merge marker for task {task_id} (sha {sha or '(none)'}) is unreachable "
                f"in git and its patch fallback failed ({reason}) — skipped")
            continue
        for path in changed:
            docs = _map_path_to_docs(path, files_map)
            if docs:
                changed_files.add(path)
                for d in docs:
                    stale_docs.add(d)
                    causes.setdefault(d, set()).add(task_id)

    return {
        "generated_at": generated_at,
        "stale_docs": sorted(stale_docs),
        "changed_files": sorted(changed_files),
        "merges_considered": len(considered),
        "causes": {d: sorted(v) for d, v in causes.items()},
        "warnings": warnings,
    }


def _write_staleness_json(ap_state: Path, result: dict) -> None:
    doc = {
        "checked_at": _now_iso(),
        "generated_at": result["generated_at"],
        "stale_docs": result["stale_docs"],
        "merges_considered": result["merges_considered"],
    }
    ap_state.mkdir(parents=True, exist_ok=True)
    (ap_state / "knowledge-staleness.json").write_text(json.dumps(doc, indent=2) + "\n")


def cmd_status(target: Path, cfg: dict, ap: Path, ap_state: Path) -> int:
    """Read-only: never mutates knowledge/. Exit 0 fresh, 4 stale."""
    kdir = ap / "knowledge"
    manifest = _read_manifest_for_staleness(kdir)
    if manifest is None:
        print("no knowledge base (run: orbit knowledge build)")
        return 0

    repo = _resolve_repo(target, cfg)
    result = compute_staleness(manifest, repo, ap_state)
    for w in result["warnings"]:
        print(w)
    _write_staleness_json(ap_state, result)

    if not result["stale_docs"]:
        print(f"knowledge is fresh — {result['merges_considered']} merge(s) since "
              f"{result['generated_at']} touched nothing tracked")
        return 0

    print(f"STALE — {len(result['stale_docs'])} doc(s) out of date:")
    for d in result["stale_docs"]:
        who = ", ".join(result["causes"].get(d, [])) or "an unattributed merge"
        print(f"  - {d}  (caused by: {who})")
    return 4


def cmd_refresh(target: Path, cfg: dict, ap: Path, ap_state: Path, timeout: int, quiet: bool) -> int:
    kdir = ap / "knowledge"
    manifest = _read_manifest_for_staleness(kdir)
    if manifest is None:
        print("no knowledge base (run: orbit knowledge build)")
        return 0

    repo = _resolve_repo(target, cfg)
    result = compute_staleness(manifest, repo, ap_state)
    for w in result["warnings"]:
        print(w)

    if not result["stale_docs"]:
        print("knowledge is fresh")
        return 0

    scope_path = ap_state / "knowledge" / "refresh-scope.json"
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text(json.dumps({
        "stale_docs": result["stale_docs"],
        "changed_files": result["changed_files"],
    }, indent=2) + "\n")

    if not quiet:
        print(f"ORBIT KNOWLEDGE REFRESH — {target}\n")
        print(f"  · {len(result['stale_docs'])} doc(s) stale: {', '.join(result['stale_docs'])}")

    if not run_survey_agent(target, cfg, timeout, quiet, skill="refresh"):
        print("  ✗ survey agent failed — knowledge refresh aborted (nothing further validated)",
              file=sys.stderr)
        return 1

    failures = validate(kdir, repo)
    if failures:
        for f in failures:
            print(f"VALIDATE: {f}")
        return 1

    docs = json.loads((kdir / "manifest.json").read_text()).get("docs") or []
    print(f"  ✓ knowledge refresh complete — {len(docs)} docs written to {kdir}")
    return 0


def _refreshed_recently(marker_path: Path, cooldown_seconds: int = AUTOREFRESH_COOLDOWN_SECONDS) -> bool:
    try:
        return (time.time() - marker_path.stat().st_mtime) < cooldown_seconds
    except OSError:
        return False


def cmd_autorefresh(target: Path, cfg: dict, ap: Path, ap_state: Path, timeout: int, quiet: bool) -> int:
    """The every-cycle loop hook — MUST always exit 0. Loop safety over
    completeness: any failure (expected or not) becomes a WARN: line, never a
    propagated exception or a nonzero exit."""
    try:
        kb_cfg = cfg.get("knowledge") if isinstance(cfg.get("knowledge"), dict) else {}
        if kb_cfg.get("refresh") != "auto":
            return 0

        manifest = _read_manifest_for_staleness(ap / "knowledge")
        if manifest is None:
            return 0

        repo = _resolve_repo(target, cfg)
        result = compute_staleness(manifest, repo, ap_state)
        raw_threshold = kb_cfg.get("stale_after_merges")
        threshold = int(raw_threshold) if raw_threshold is not None else DEFAULT_STALE_AFTER_MERGES
        if len(result["stale_docs"]) < threshold:
            if not quiet:
                print(f"autorefresh: {len(result['stale_docs'])} stale doc(s), below threshold "
                      f"{threshold} — no action")
            return 0

        marker_path = ap_state / ".knowledge-refreshed"
        if _refreshed_recently(marker_path):
            if not quiet:
                print("autorefresh: refreshed within the last 24h — no action")
            return 0

        if not quiet:
            print(f"autorefresh: {len(result['stale_docs'])} stale doc(s) >= threshold "
                  f"{threshold} — refreshing")
        rc = cmd_refresh(target, cfg, ap, ap_state, timeout, quiet)
        if rc == 0:
            ap_state.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(_now_iso() + "\n")
        else:
            print(f"WARN: knowledge autorefresh: refresh exited {rc} — will retry next cycle")
        return 0
    except Exception as e:                          # noqa: BLE001 — loop safety, see docstring
        print(f"WARN: knowledge autorefresh crashed ({e}) — continuing")
        return 0


def _usage_exit(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 2


def _load_config(target: Path) -> tuple[dict | None, str | None]:
    """(cfg, None) on success; (None, message) on any failure — missing
    .autopilot/config.yaml, unparseable YAML, or a non-mapping top level.
    Callers decide how the failure surfaces: a usage error (exit 2) for
    build/status/refresh, or a swallowed WARN for autorefresh, whose
    contract is unconditional exit 0 even when config.yaml is mid-hand-edit."""
    cfg_path = target / ".autopilot" / "config.yaml"
    if not cfg_path.exists():
        return None, f"no {cfg_path} — run `orbit init {target}` first"
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError as e:
        return None, f"unreadable config at {cfg_path}: {e}"
    if not isinstance(cfg, dict):
        return None, f"unreadable config at {cfg_path}: expected a mapping, got {cfg!r}"
    return cfg, None


def _timeout_from(cfg: dict, argv: list[str]) -> int:
    kb_cfg = cfg.get("knowledge") if isinstance(cfg.get("knowledge"), dict) else {}
    timeout = int(kb_cfg.get("timeout_seconds") or DEFAULT_TIMEOUT)
    for i, a in enumerate(argv):
        if a == "--timeout" and i + 1 < len(argv):
            timeout = int(argv[i + 1])
    return timeout


def cmd_autorefresh_entry(target: Path, argv: list[str], quiet: bool) -> int:
    """autorefresh's ALWAYS-exit-0 contract must hold even before
    `cmd_autorefresh` itself runs — a missing or malformed
    `.autopilot/config.yaml` (e.g. a concurrent hand-edit mid-loop) is exactly
    the transient failure this subcommand must survive, unlike build/status/
    refresh, for which the same condition is a genuine usage error (exit 2)."""
    try:
        cfg, err = _load_config(target)
        if err:
            print(f"WARN: knowledge autorefresh: {err} — skipping this cycle")
            return 0
        ap = target / ".autopilot"
        timeout = _timeout_from(cfg, argv)
        return cmd_autorefresh(target, cfg, ap, ap / "state", timeout, quiet)
    except Exception as e:                          # noqa: BLE001 — loop safety, see cmd_autorefresh
        print(f"WARN: knowledge autorefresh crashed ({e}) — continuing")
        return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        return 2
    target = Path(args[0]).resolve()
    subcmd = args[1]
    quiet = "--quiet" in argv

    if subcmd == "autorefresh":
        return cmd_autorefresh_entry(target, argv, quiet)

    cfg, err = _load_config(target)
    if err:
        return _usage_exit(err)

    if subcmd not in ("build", "status", "refresh"):
        print(__doc__, file=sys.stderr)
        return 2

    ap = target / ".autopilot"
    timeout = _timeout_from(cfg, argv)
    ap_state = ap / "state"

    if subcmd == "build":
        return cmd_build(target, cfg, ap, timeout, quiet)
    if subcmd == "status":
        return cmd_status(target, cfg, ap, ap_state)
    return cmd_refresh(target, cfg, ap, ap_state, timeout, quiet)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
