#!/usr/bin/env python3
"""Feature-agent build engine — dedicated worktree agents for features too big
for the one-commit loop.

Each build carves an isolated git worktree from origin/<base_branch> (the loop
branch), runs Claude Code headless to implement ONE scoped task end-to-end
(build → test → commit), then pushes the work to its own branch for review. The
dashboard's "Merge into loop" button (POST /merge-to-loop) lands it later.

State lives in <repo>/.autopilot/state/feature_builds.json ({"builds":[...]}),
the same file the command center's renderAgents() reads. Each build row:
  {id, title, branch, status(running|pushed|failed), started, finished, sha, note}

CLI:
  feature_build.py start <id> "<title>" [branch]   # register + spawn detached worker
  feature_build.py run   <id> "<title>" [branch]   # the worker (usually auto-spawned)
  feature_build.py done  <id> [--sha S --branch B]  # manual: mark pushed
  feature_build.py fail  <id> [--note N]            # manual: mark failed
  feature_build.py list                             # dump the registry
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

# ── path model (identical resolution to command_center.py) ──────────────────
ENGINE = Path(__file__).resolve().parent
ORBIT_HOME = Path(os.environ.get("ORBIT_HOME") or ENGINE.parent)


def _ap_home() -> Path:
    """Resolve AP_HOME (the target repo's .autopilot dir); fail loud if unset."""
    home = os.environ.get("AP_HOME")
    if not home:
        raise SystemExit("AP_HOME unset — run inside an orbit target repo.")
    return Path(home)


AP_HOME = _ap_home()
AP_STATE = Path(os.environ.get("AP_STATE") or AP_HOME / "state")
REPO = Path(os.environ.get("ORBIT_REPO") or AP_HOME.parent)
BACKLOG = AP_HOME / "backlog.yaml"
FEATURE_BUILDS = AP_STATE / "feature_builds.json"
LOCK = AP_STATE / "feature_builds.lock"
LOGDIR = AP_STATE / "logs"

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")


def _cfg() -> dict:
    """Load the target repo's .autopilot/config.yaml (base branch, model, etc.)."""
    try:
        return yaml.safe_load((AP_HOME / "config.yaml").read_text()) or {}
    except Exception:
        return {}


def _base_branch() -> str:
    """The trunk to build on — env override, then config.yaml, then loud main."""
    for var in ("ORBIT_BASE_BRANCH", "AP_BASE_BRANCH"):
        if os.environ.get(var):
            return os.environ[var]
    return str(_cfg().get("base_branch") or "main")


def _model() -> str:
    """Model for the agent — env override, then config, then Opus."""
    return os.environ.get("AP_MODEL") or os.environ.get("ORBIT_MODEL") \
        or str(_cfg().get("model") or "claude-opus-4-8")


def _perm() -> str:
    """Permission mode for the headless agent."""
    return os.environ.get("AP_PERM") or os.environ.get("ORBIT_PERM") \
        or str(_cfg().get("permission_mode") or "acceptEdits")


def _timeout() -> int:
    """Wall-clock budget for a feature build (generous — bigger than one cycle)."""
    for var in ("AP_FEATURE_TIMEOUT", "ORBIT_FEATURE_TIMEOUT"):
        if os.environ.get(var):
            return int(os.environ[var])
    return 7200  # 2h


def _claude_bin() -> str:
    """Resolve the claude binary the same way run.sh does."""
    env = os.environ.get("ORBIT_CLAUDE_BIN")
    if env and env != "auto":
        return env
    from shutil import which
    return which("claude") or str(Path.home() / ".local/bin/claude")


def _now() -> int:
    """Current epoch seconds (int) — matches the dashboard's relSince()."""
    return int(time.time())


# ── registry (locked read-modify-write) ─────────────────────────────────────
@contextlib.contextmanager
def _locked():
    """Exclusive file lock around the JSON registry so concurrent builds don't
    clobber each other's rows."""
    AP_STATE.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _load() -> list:
    """Read the builds array (empty if the file is missing/corrupt)."""
    if not FEATURE_BUILDS.exists():
        return []
    try:
        return json.loads(FEATURE_BUILDS.read_text()).get("builds", []) or []
    except Exception:
        return []


def _save(builds: list) -> None:
    """Atomically write the builds array back to the registry."""
    tmp = FEATURE_BUILDS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"builds": builds}, indent=2) + "\n")
    tmp.replace(FEATURE_BUILDS)


def _upsert(bid: str, **fields) -> None:
    """Insert or update the build row for `bid`, merging `fields` in, under lock."""
    with _locked():
        builds = _load()
        for b in builds:
            if b.get("id") == bid:
                b.update(fields)
                break
        else:
            builds.append({"id": bid, **fields})
        _save(builds)


# ── task contract ────────────────────────────────────────────────────────────
def _contract(bid: str, title: str) -> str:
    """Pull the task's plain summary + acceptance criteria + context from the
    backlog so the agent gets the full contract, not just a title."""
    try:
        tasks = (yaml.safe_load(BACKLOG.read_text()) or {}).get("tasks", []) or []
    except Exception:
        tasks = []
    t = next((x for x in tasks if str(x.get("id")) == bid), None)
    if not t:
        return f"TASK {bid}: {title}\n(no backlog entry found — implement from the title.)"
    lines = [f"TASK {bid}: {t.get('title') or title}"]
    if t.get("plain"):
        lines.append(f"\nWhat it is: {t['plain']}")
    if t.get("context"):
        lines.append(f"\nContext:\n{t['context']}")
    ac = t.get("acceptance_criteria") or []
    if ac:
        lines.append("\nAcceptance criteria (all must hold):")
        lines += [f"  - {a}" for a in ac]
    fh = t.get("files_hint") or []
    if fh:
        lines.append("\nLikely files: " + ", ".join(fh))
    return "\n".join(lines)


def _prompt(bid: str, title: str, wt: Path, base: str) -> str:
    """Build the headless agent's instruction: implement the whole feature in the
    isolated worktree, commit it, and stop — the harness pushes for review."""
    return f"""You are a feature agent working in an isolated git worktree at {wt},
branched from {base}. Implement ONE feature end-to-end.

{_contract(bid, title)}

Rules:
- Implement the FULL feature so every acceptance criterion holds. Multiple commits are fine.
- Match the existing code style — read neighbouring files before writing new code.
- Add or update tests in the same change; run the project's lint + tests before you finish.
- Never commit secrets or .env files; never force-push; do not run destructive git commands.
- Commit your work with clear `<type>: <what>` messages. Do NOT push — the harness pushes your branch for review.
- If you hit a decision you genuinely cannot resolve, write one line `BLOCKED: <why>` to {wt}/.feature-blocked and stop.
- When done, make sure `git status` is clean (everything committed).
"""


# ── git helpers ──────────────────────────────────────────────────────────────
def _git(repo, *args, **kw):
    """Run a git command against `repo`, capturing output."""
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, **kw)


def _remove_worktree(wt: Path) -> None:
    """Best-effort reclaim of a build worktree (safe to call if it never existed)."""
    with contextlib.suppress(Exception):
        _git(REPO, "worktree", "remove", "--force", str(wt))
        _git(REPO, "worktree", "prune")


# ── commands ─────────────────────────────────────────────────────────────────
def _stopped() -> bool:
    """True when Orbit is paused (the loop's STOP kill-switch is present)."""
    return (AP_STATE / "STOP").exists()


def _already_merged(bid: str) -> bool:
    """True if this task's recorded commit is already an ancestor of the base
    branch — its fix shipped, so a fresh build would only duplicate it."""
    led = AP_STATE / "ledger.json"
    if not led.exists():
        return False
    try:
        e = (json.loads(led.read_text()).get("entries", {}) or {}).get(bid) or {}
    except Exception:
        return False
    target = e.get("sha") or e.get("remote_ref") or (f"origin/{e['branch']}" if e.get("branch") else None)
    if not target:
        return False
    base_ref = f"origin/{_base_branch()}"
    return subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", target, base_ref],
                          capture_output=True).returncode == 0


def cmd_start(bid: str, title: str, branch: str) -> str:
    """Register a running build and spawn the detached worker; return fast."""
    if not _ID_RE.match(bid):
        raise SystemExit(f"invalid id: {bid}")
    if not _BRANCH_RE.match(branch):
        raise SystemExit(f"invalid branch: {branch}")
    # Pause guard: feature agents spend tokens + push branches, so they must
    # honour the same kill-switch as the loop — nothing runs while Orbit is paused.
    if _stopped():
        return "Orbit is paused (STOP present) — feature agents don't run while paused. Resume the loop first."
    # Dedup guards: never rebuild work that is already building, already pushed for
    # review, or already merged on the base branch (the redundant-rebuild bug).
    for b in _load():
        if b.get("id") == bid and b.get("status") == "running":
            return f"'{bid}' is already building — see the Feature agents tab."
        if b.get("id") == bid and b.get("status") == "pushed" and not b.get("merged"):
            return (f"'{bid}' already has a pushed build ({b.get('branch')}) awaiting review — "
                    f"merge or reject it first, don't rebuild it.")
    if _already_merged(bid):
        return f"'{bid}' is already merged on the base branch — nothing to build."
    _upsert(bid, title=title, branch=branch, status="running",
            started=_now(), finished=None, sha=None, note="preparing worktree")
    LOGDIR.mkdir(parents=True, exist_ok=True)
    logf = open(LOGDIR / f"feature-{bid}.log", "a")
    subprocess.Popen(
        [sys.executable, str(ENGINE / "feature_build.py"), "run", bid, title, branch],
        stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,  # survive the HTTP request that launched us
    )
    return f"🚀 Feature agent started for '{bid}' → will push origin/{branch} for review."


def cmd_run(bid: str, title: str, branch: str) -> None:
    """The worker: worktree → headless build → commit → push → update registry."""
    base = _base_branch()
    base_ref = f"origin/{base}"
    wt = Path(f"{REPO}-feat-{re.sub(r'[^A-Za-z0-9._-]', '-', bid)}")
    try:
        _upsert(bid, note="fetching + creating worktree")
        _git(REPO, "fetch", "origin", "--quiet")
        # Fresh worktree on the base ref (force-replace any stale one at this path).
        if wt.exists():
            _git(REPO, "worktree", "remove", "--force", str(wt))
        add = _git(REPO, "worktree", "add", "--force", "--detach", str(wt), base_ref)
        if add.returncode != 0:
            _upsert(bid, status="failed", finished=_now(),
                    note=f"worktree add failed: {add.stderr.strip()[:200]}")
            return

        _upsert(bid, note="agent building")
        prompt = _prompt(bid, title, wt, base)
        settings = ORBIT_HOME / "config" / "orbit.settings.json"
        argv = [_claude_bin(), "--print", prompt,
                "--permission-mode", _perm(), "--model", _model(),
                "--add-dir", str(REPO), "--add-dir", str(ORBIT_HOME),
                "--output-format", "stream-json", "--verbose"]
        if settings.exists():
            argv[3:3] = ["--settings", str(settings)]
        try:
            subprocess.run(argv, cwd=str(wt), stdin=subprocess.DEVNULL,
                           timeout=_timeout())
        except subprocess.TimeoutExpired:
            _upsert(bid, note="agent hit the time budget — checking for commits")

        # How much did the agent commit above the base?
        ahead = _git(wt, "rev-list", "--count", f"{base_ref}..HEAD").stdout.strip() or "0"
        blocked = (wt / ".feature-blocked")
        if int(ahead or 0) == 0:
            note = "agent reported BLOCKED: " + blocked.read_text().strip()[:200] \
                if blocked.exists() else "no changes committed"
            _upsert(bid, status="failed", finished=_now(), note=note)
            _remove_worktree(wt)  # nothing committed — the worktree is disposable
            return

        # Push the work to its own branch for review (never force).
        _upsert(bid, note=f"pushing {ahead} commit(s)")
        push = _git(wt, "push", "origin", f"HEAD:refs/heads/{branch}")
        if push.returncode != 0:
            _upsert(bid, status="failed", finished=_now(),
                    note=f"push failed: {push.stderr.strip()[:200]}")
            return
        sha = (_git(wt, "rev-parse", "--short", "HEAD").stdout or "").strip()
        _upsert(bid, status="pushed", finished=_now(), sha=sha,
                note=f"{ahead} commit(s) on origin/{branch} — review, then Merge into loop")
        # Best-effort review packet (same helper the loop uses).
        with contextlib.suppress(Exception):
            subprocess.run([sys.executable, str(ENGINE / "review_packet.py"),
                            bid, str(wt), branch, base], timeout=120)
        # Work is safely on origin — reclaim the local worktree.
        _remove_worktree(wt)
    except Exception as e:  # never leave a build stuck on "running"
        _upsert(bid, status="failed", finished=_now(), note=f"error: {str(e)[:200]}")
        _remove_worktree(wt)


def cmd_done(bid: str, sha: str, branch: str) -> str:
    """Manually mark a build pushed (for externally-driven agents)."""
    fields = {"status": "pushed", "finished": _now()}
    if sha:
        fields["sha"] = sha
    if branch:
        fields["branch"] = branch
    _upsert(bid, **fields)
    return f"marked '{bid}' pushed."


def cmd_fail(bid: str, note: str) -> str:
    """Manually mark a build failed."""
    _upsert(bid, status="failed", finished=_now(), note=note or "failed")
    return f"marked '{bid}' failed."


def main():
    ap = argparse.ArgumentParser(description="Orbit feature-agent build engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("start", "run"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("title")
        p.add_argument("branch", nargs="?", default="")
    pd = sub.add_parser("done"); pd.add_argument("id"); pd.add_argument("--sha", default=""); pd.add_argument("--branch", default="")
    pf = sub.add_parser("fail"); pf.add_argument("id"); pf.add_argument("--note", default="")
    sub.add_parser("list")
    a = ap.parse_args()

    if a.cmd in ("start", "run"):
        branch = a.branch or f"feature/{a.id}"
        if a.cmd == "start":
            print(cmd_start(a.id, a.title, branch))
        else:
            cmd_run(a.id, a.title, branch)
    elif a.cmd == "done":
        print(cmd_done(a.id, a.sha, a.branch))
    elif a.cmd == "fail":
        print(cmd_fail(a.id, a.note))
    elif a.cmd == "list":
        print(json.dumps({"builds": _load()}, indent=2))


if __name__ == "__main__":
    main()
