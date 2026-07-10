# Branch-Management Tab Implementation Plan

> **ARCHIVED build-history (shipped 2026-07-10).** This is the TDD build ladder kept for provenance — the exact code/tests each task landed. The durable why-and-invariants record is the sibling `2026-07-10-branch-management-tab-design.md`; the code is the source of truth for how.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an orbit dashboard "Branches" tab that reconciles the ledger against every `autopilot/*` branch on origin (awaiting-review / merged / orphan / rejected) with guarded per-branch and bulk delete.

**Architecture:** All new logic lives in `engine/command_center.py` (the dashboard process) and `engine/cc_shell.html` (its inline SPA). Merge status is derived from local git ancestry against the loop's base branch — no Bitbucket API. A pure `branch_reconcile()` function is unit-tested in Python; the UI is verified by curl + browser.

**Tech Stack:** Python 3 stdlib (`subprocess`, `http.server`, `yaml`), vanilla JS in a single HTML file, pytest.

**Design spec (rationale record):** `docs/specs/done/2026-07-10-branch-management-tab-design.md`

## Global Constraints

- Merge target / base branch is resolved from config, currently `feature/funnelhub-auto-loop`. Never hardcode a branch name in new logic — read `BASE_BRANCH` / `PREFIX`.
- Deletable branches are **only** those under the `autopilot/` prefix (`PREFIX`). Any other ref is refused server-side.
- Bulk delete may only touch branches the server itself classified as `merged` or `rejected`. Never trust a category from the client.
- No force-push, no changes to how branches are created, no Bitbucket API.
- POST endpoints require the per-process CSRF token (`X-CC-Token`) and same-origin — follow the existing `do_POST` pattern exactly.
- Commit after each task. Run from the `orbit/` directory. Tests: `python3 -m pytest tests/ -v`.

---

### Task 1: Harden `BASE_BRANCH` resolution (Part A)

Today `command_center.py:77` is `BASE_BRANCH = os.environ.get("ORBIT_BASE_BRANCH") or os.environ.get("AP_BASE_BRANCH", "main")` — a silent fall back to `main` if the env is ever missing, which would make every merge badge wrong. Read the target config as the source of truth and make the fallback loud.

**Files:**
- Modify: `engine/command_center.py:77`
- Test: `tests/test_base_branch.py` (create)

**Interfaces:**
- Produces: `_resolve_base_branch() -> str`; module constant `BASE_BRANCH: str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_base_branch.py`:

```python
# Verifies BASE_BRANCH resolution precedence: env override → target config.yaml
# → loud 'main' fallback. Guards against the silent-main regression.
import importlib, sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine"


def _import_cc(tmp_path, monkeypatch, *, env=None, config_base=None):
    ap_home = tmp_path / ".autopilot"
    (ap_home / "state").mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(ap_home / "state"))
    for var in ("ORBIT_BASE_BRANCH", "AP_BASE_BRANCH"):
        monkeypatch.delenv(var, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    if config_base is not None:
        (ap_home / "config.yaml").write_text(f"base_branch: {config_base}\n")
    sys.path.insert(0, str(ENGINE))
    sys.modules.pop("command_center", None)
    mod = importlib.import_module("command_center")
    importlib.reload(mod)
    sys.path.remove(str(ENGINE))
    return mod


def test_env_override_wins(tmp_path, monkeypatch):
    mod = _import_cc(tmp_path, monkeypatch,
                     env={"ORBIT_BASE_BRANCH": "feature/x"}, config_base="main")
    assert mod._resolve_base_branch() == "feature/x"


def test_config_used_when_no_env(tmp_path, monkeypatch):
    mod = _import_cc(tmp_path, monkeypatch, config_base="feature/funnelhub-auto-loop")
    assert mod._resolve_base_branch() == "feature/funnelhub-auto-loop"


def test_loud_fallback_when_nothing_set(tmp_path, monkeypatch, capsys):
    mod = _import_cc(tmp_path, monkeypatch)
    assert mod._resolve_base_branch() == "main"
    assert "base_branch unresolved" in capsys.readouterr().err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_base_branch.py -v`
Expected: FAIL with `AttributeError: module 'command_center' has no attribute '_resolve_base_branch'`

- [ ] **Step 3: Write the implementation**

In `engine/command_center.py`, replace the single line at `:77`
(`BASE_BRANCH = os.environ.get("ORBIT_BASE_BRANCH") or os.environ.get("AP_BASE_BRANCH", "main")`) with:

```python
def _resolve_base_branch() -> str:
    # Trunk to build on + measure "merged?" against. Precedence: explicit env
    # override (set by run.sh / launchd) → the target's config.yaml (source of
    # truth, same value run.sh uses) → a LOUD 'main' fallback (never silent —
    # a wrong base makes every merge badge lie).
    for var in ("ORBIT_BASE_BRANCH", "AP_BASE_BRANCH"):
        v = os.environ.get(var)
        if v:
            return v
    try:
        cfg = yaml.safe_load((AP_HOME / "config.yaml").read_text()) or {}
        if cfg.get("base_branch"):
            return str(cfg["base_branch"])
    except Exception:
        pass
    print("WARNING: base_branch unresolved (no ORBIT_BASE_BRANCH/AP_BASE_BRANCH env, "
          "no base_branch in config.yaml) — merge detection will be WRONG. "
          "Defaulting to 'main'.", file=sys.stderr)
    return "main"


BASE_BRANCH = _resolve_base_branch()
```

(`yaml`, `os`, `sys`, `AP_HOME` are already imported/defined above line 77.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_base_branch.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add engine/command_center.py tests/test_base_branch.py
git commit -m "fix: resolve BASE_BRANCH from config, no silent main fallback"
```

---

### Task 2: Backend plumbing — prefix, fetch/prune, branch enumeration, trunk ancestry

Add the git-facing helpers the reconciler needs. These shell out, so they are covered by the reconciler's pure tests (Task 3) rather than unit-tested directly; this task's deliverable is verified by an import + smoke assertion.

**Files:**
- Modify: `engine/command_center.py` (add constants + helpers near `merged_map`, ~line 536)
- Test: none new (smoke-checked in Step 3; exercised via Task 3/4)

**Interfaces:**
- Consumes: `REPO`, `BASE_BRANCH`, `subprocess`, `time` (all already defined).
- Produces:
  - `PREFIX: str` — branch prefix, `os.environ.get("ORBIT_BRANCH_PREFIX", "autopilot")`.
  - `remote_branches() -> list[tuple[str, str, int]]` — `(name, tip_sha, committer_unix_ts)` for every `origin/<PREFIX>/*` ref, where `name` is like `autopilot/task-x` (the `origin/` stripped). Runs a cached `git fetch --prune` first (60s TTL).
  - `trunk_ancestry() -> set[str]` — full SHAs reachable from `origin/<BASE_BRANCH>` (60s TTL).
  - `bust_branch_caches() -> None` — clears both caches (called after a delete).

- [ ] **Step 1: Write the implementation**

In `engine/command_center.py`, just above `def merged_map()` (~line 539), add:

```python
PREFIX = os.environ.get("ORBIT_BRANCH_PREFIX", "autopilot")

_FETCH_CACHE = {"t": 0.0}
_BRANCHES_CACHE = {"t": 0.0, "v": []}
_ANCESTRY_CACHE = {"t": 0.0, "v": set()}


def _git(*args, timeout=15):
    # Thin wrapper: git in the target repo, captured, never raises on non-zero.
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True, timeout=timeout)


def _fetch_prune():
    # Refresh remote-tracking refs (and drop refs deleted on origin) so branch
    # status is accurate even while the loop is paused. Cached 60s. --prune
    # never deletes REMOTE branches, only stale local tracking refs.
    now = time.time()
    if now - _FETCH_CACHE["t"] < 60:
        return
    try:
        _git("fetch", "--prune", "origin", "--quiet")
    except Exception:
        pass
    _FETCH_CACHE["t"] = now


def remote_branches() -> list:
    # [(name, tip_sha, committer_ts)] for origin/<PREFIX>/* — from local
    # remote-tracking refs (fresh via _fetch_prune), one for-each-ref call.
    now = time.time()
    if now - _BRANCHES_CACHE["t"] < 60:
        return _BRANCHES_CACHE["v"]
    _fetch_prune()
    out = []
    try:
        r = _git("for-each-ref", "--format=%(refname:short) %(objectname) %(committerdate:unix)",
                 f"refs/remotes/origin/{PREFIX}/")
        for ln in r.stdout.splitlines():
            parts = ln.split()
            if len(parts) != 3:
                continue
            short, sha, ts = parts  # short = "origin/autopilot/task-x"
            name = short[len("origin/"):] if short.startswith("origin/") else short
            try:
                out.append((name, sha, int(ts)))
            except ValueError:
                continue
    except Exception:
        out = []
    _BRANCHES_CACHE.update(t=now, v=out)
    return out


def trunk_ancestry() -> set:
    # Full SHAs reachable from origin/<BASE_BRANCH>. A branch tip in this set is
    # merged. Same technique as merged_map, shared here. Cached 60s.
    now = time.time()
    if now - _ANCESTRY_CACHE["t"] < 60:
        return _ANCESTRY_CACHE["v"]
    s = set()
    try:
        r = _git("rev-list", "-8000", f"origin/{BASE_BRANCH}")
        s = set(r.stdout.split())
    except Exception:
        s = set()
    _ANCESTRY_CACHE.update(t=now, v=s)
    return s


def bust_branch_caches():
    _FETCH_CACHE["t"] = 0.0
    _BRANCHES_CACHE["t"] = 0.0
    _ANCESTRY_CACHE["t"] = 0.0
```

- [ ] **Step 2: Smoke-check it imports and runs**

Run:
```bash
AP_HOME=/Users/arunaryasomayajula/master/cdp-metaql/.autopilot \
AP_STATE=/Users/arunaryasomayajula/master/cdp-metaql/.autopilot/state \
ORBIT_BASE_BRANCH=feature/funnelhub-auto-loop \
python3 -c "import sys; sys.path.insert(0,'engine'); import command_center as c; print('branches:', len(c.remote_branches())); print('ancestry shas:', len(c.trunk_ancestry()))"
```
Expected: prints a branch count (~16) and a non-zero ancestry count, no traceback.

- [ ] **Step 3: Commit**

```bash
git add engine/command_center.py
git commit -m "feat: git plumbing for branch reconciliation (fetch/prune, enumerate, ancestry)"
```

---

### Task 3: `branch_reconcile()` — the pure reconciler

The heart of the feature: join git reality to the ledger and categorize. Pure and injectable so it is fully unit-tested without git or network.

**Files:**
- Modify: `engine/command_center.py` (add after `trunk_ancestry`)
- Test: `tests/test_branch_reconcile.py` (create)

**Interfaces:**
- Consumes: `PREFIX`, `remote_branches()` output shape, `load_ledger()` shape.
- Produces:
  `branch_reconcile(branches, ancestry, ledger, now_ts) -> list[dict]`
  - `branches`: `list[(name, tip_sha, committer_ts)]`
  - `ancestry`: `set[str]` of trunk SHAs
  - `ledger`: `dict[task_id -> entry]` (entry has `state`, `sha`, `remote_ref`, `branch`)
  - `now_ts`: `int` epoch seconds
  - Returns rows with keys: `branch, task_id, tip, merged, ledger_state, is_current_ref, is_orphan, age_days, category`. `category ∈ {"awaiting","merged","rejected","orphan"}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_branch_reconcile.py`:

```python
# Pure-function tests for branch_reconcile: category assignment, merge detection,
# orphan/current-ref logic, age. No git, no network — inputs are injected.
import importlib, sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"


def _cc(tmp_path, monkeypatch):
    ap_home = tmp_path / ".autopilot"
    (ap_home / "state").mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(ap_home / "state"))
    monkeypatch.setenv("ORBIT_BASE_BRANCH", "feature/trunk")
    sys.path.insert(0, str(ENGINE))
    sys.modules.pop("command_center", None)
    mod = importlib.import_module("command_center")
    importlib.reload(mod)
    sys.path.remove(str(ENGINE))
    return mod


def test_categories(tmp_path, monkeypatch):
    cc = _cc(tmp_path, monkeypatch)
    NOW = 1_000_000
    DAY = 86400
    branches = [
        ("autopilot/task-await", "sha_await", NOW - 2 * DAY),   # pushed, not merged
        ("autopilot/task-done",  "sha_done",  NOW - 5 * DAY),   # tip in ancestry
        ("autopilot/task-rej",   "sha_rej",   NOW - 1 * DAY),   # rejected in ledger
        ("autopilot/task-orph",  "sha_orph",  NOW - 9 * DAY),   # no ledger entry
        ("autopilot/task-dup-20260101T010101", "sha_dup", NOW), # dup: ledger points elsewhere
    ]
    ancestry = {"sha_done"}
    ledger = {
        "task-await": {"state": "pushed",   "sha": "sha_await", "remote_ref": "origin/autopilot/task-await"},
        "task-done":  {"state": "pushed",   "sha": "sha_done",  "remote_ref": "origin/autopilot/task-done"},
        "task-rej":   {"state": "rejected", "sha": "sha_rej",   "remote_ref": "origin/autopilot/task-rej"},
        "task-dup":   {"state": "pushed",   "sha": "sha_new",   "remote_ref": "origin/autopilot/task-dup"},
    }
    rows = {r["branch"]: r for r in cc.branch_reconcile(branches, ancestry, ledger, NOW)}

    assert rows["autopilot/task-await"]["category"] == "awaiting"
    assert rows["autopilot/task-await"]["age_days"] == 2

    assert rows["autopilot/task-done"]["category"] == "merged"    # ancestry beats pushed state
    assert rows["autopilot/task-done"]["merged"] is True

    assert rows["autopilot/task-rej"]["category"] == "rejected"

    assert rows["autopilot/task-orph"]["category"] == "orphan"
    assert rows["autopilot/task-orph"]["is_orphan"] is True

    dup = rows["autopilot/task-dup-20260101T010101"]
    assert dup["task_id"] == "dup"           # timestamp suffix stripped
    assert dup["is_current_ref"] is False     # ledger's current ref is the clean branch
    assert dup["category"] == "orphan"        # superseded branch → orphan bucket


def test_marked_merged_without_ancestry(tmp_path, monkeypatch):
    cc = _cc(tmp_path, monkeypatch)
    branches = [("autopilot/task-m", "sha_m", 1_000_000)]
    ledger = {"task-m": {"state": "merged", "sha": "sha_m", "remote_ref": "origin/autopilot/task-m"}}
    rows = cc.branch_reconcile(branches, set(), ledger, 1_000_000)
    assert rows[0]["category"] == "merged"
    assert rows[0]["merged"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_branch_reconcile.py -v`
Expected: FAIL with `AttributeError: ... has no attribute 'branch_reconcile'`

- [ ] **Step 3: Write the implementation**

In `engine/command_center.py`, add after `trunk_ancestry()`:

```python
import re as _re  # (re is already imported at top; this line is a no-op guard — omit if re present)

_TS_SUFFIX = re.compile(r"-\d{8}T\d{6}$")


def _task_id_from_branch(name: str) -> str:
    # "autopilot/task-foo-bar" -> "foo-bar"; strips a trailing -<TIMESTAMP> re-run suffix.
    base = name[len(PREFIX) + 1:] if name.startswith(PREFIX + "/") else name
    if base.startswith("task-"):
        base = base[len("task-"):]
    return _TS_SUFFIX.sub("", base)


def branch_reconcile(branches, ancestry, ledger, now_ts) -> list:
    # Join origin/<PREFIX>/* branches to the ledger and categorize each.
    # Pure: all inputs injected. Category order: awaiting > merged > rejected > orphan.
    rows = []
    for name, tip, ts in branches:
        tid = _task_id_from_branch(name)
        entry = ledger.get(tid, {})
        entry_ref = (entry.get("remote_ref") or "").removeprefix("origin/") or entry.get("branch") or ""
        is_current_ref = bool(entry) and entry_ref == name
        state = entry.get("state") if is_current_ref else None
        merged = (tip in ancestry) or (is_current_ref and state == "merged")
        is_orphan = (not entry) or (not is_current_ref)

        if state == "pushed" and not merged:
            category = "awaiting"
        elif merged:
            category = "merged"
        elif state == "rejected":
            category = "rejected"
        else:
            category = "orphan"

        rows.append({
            "branch": name,
            "task_id": tid,
            "tip": tip,
            "merged": merged,
            "ledger_state": state,
            "is_current_ref": is_current_ref,
            "is_orphan": is_orphan,
            "age_days": max(0, (now_ts - ts) // 86400),
            "category": category,
        })
    return rows
```

(If `re` is already imported at the top of the module — it is, used by `_TID_RE` — delete the `import re as _re` guard line and keep only `_TS_SUFFIX = re.compile(...)`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_branch_reconcile.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add engine/command_center.py tests/test_branch_reconcile.py
git commit -m "feat: pure branch_reconcile — categorize autopilot branches vs ledger"
```

---

### Task 4: Wire branches into `/api/state`

Expose the reconciled rows (decorated with `has_packet` + `pr_url`) to the SPA.

**Files:**
- Modify: `engine/command_center.py` — inside `build_state()` return dict (~line 1124, where `feature_builds` is added)
- Test: `tests/test_state_branches.py` (create)

**Interfaces:**
- Consumes: `remote_branches()`, `trunk_ancestry()`, `load_ledger()`, `branch_reconcile()`, `REVIEWS`, `BB_PR_NEW`, `BASE_BRANCH`.
- Produces: `build_state()["branches"]` — the reconciled rows plus `has_packet: bool` and `pr_url: str|None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_state_branches.py`:

```python
# build_state must expose a "branches" list built from the reconciler, decorated
# with has_packet + pr_url. remote_branches/trunk_ancestry are monkeypatched so
# the test needs no git.
import importlib, sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"


def _cc(tmp_path, monkeypatch):
    ap_home = tmp_path / ".autopilot"
    state = ap_home / "state"
    (state / "reviews").mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(state))
    monkeypatch.setenv("ORBIT_BASE_BRANCH", "feature/trunk")
    monkeypatch.setenv("ORBIT_BB_REPO", "team/repo")
    (ap_home / "backlog.yaml").write_text("version: 1\ntasks: []\n")
    (state / "ledger.json").write_text(
        '{"entries": {"await": {"state": "pushed", "sha": "s1",'
        ' "remote_ref": "origin/autopilot/task-await"}}}')
    (state / "reviews" / "task-await.md").write_text("packet")
    sys.path.insert(0, str(ENGINE))
    sys.modules.pop("command_center", None)
    mod = importlib.import_module("command_center")
    importlib.reload(mod)
    sys.path.remove(str(ENGINE))
    return mod


def test_state_has_branches(tmp_path, monkeypatch):
    cc = _cc(tmp_path, monkeypatch)
    monkeypatch.setattr(cc, "remote_branches",
                        lambda: [("autopilot/task-await", "s1", 1_000_000)])
    monkeypatch.setattr(cc, "trunk_ancestry", lambda: set())
    st = cc.build_state()
    assert "branches" in st
    b = next(x for x in st["branches"] if x["branch"] == "autopilot/task-await")
    assert b["category"] == "awaiting"
    assert b["has_packet"] is True
    assert b["pr_url"] and "pull-requests/new" in b["pr_url"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_state_branches.py -v`
Expected: FAIL — `KeyError: 'branches'` (or assertion on missing key).

- [ ] **Step 3: Write the implementation**

In `engine/command_center.py`, inside `build_state()`, just before the `return {`
that includes `"feature_builds": feature_builds_annotated(),` (~line 1124), add:

```python
    # Reconciled autopilot/* branches for the Branches tab. Decorate the pure
    # rows with filesystem/URL facts build_state owns.
    import time as _t
    _now = int(_t.time())
    _branch_rows = branch_reconcile(remote_branches(), trunk_ancestry(), load_ledger(), _now)
    for _b in _branch_rows:
        _b["has_packet"] = (REVIEWS / f"task-{_b['task_id']}.md").exists()
        _b["pr_url"] = (
            f"{BB_PR_NEW}?source={urllib.parse.quote(_b['branch'], safe='')}"
            f"&dest={urllib.parse.quote(BASE_BRANCH, safe='')}"
            if BB_PR_NEW and not _b["merged"] and _b["category"] != "rejected"
            else None
        )
```

Then add one key to the returned dict (next to `"feature_builds": feature_builds_annotated(),`):

```python
               "branches": _branch_rows,
```

(`urllib.parse` is already imported.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_state_branches.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest tests/ -v`
Expected: all pass (base_branch + reconcile + state_branches + existing move test).

- [ ] **Step 6: Commit**

```bash
git add engine/command_center.py tests/test_state_branches.py
git commit -m "feat: expose reconciled branches in /api/state"
```

---

### Task 5: Delete endpoints + guardrails

Single delete (any `autopilot/*`, UI-confirmed) and bulk delete (server-verified `merged`/`rejected` only).

**Files:**
- Modify: `engine/command_center.py` — add `do_delete_branch` + `do_delete_branches_bulk` (near `do_merge_to_loop`, ~line 753) and two routes in `do_POST` (~line 1780)
- Test: `tests/test_delete_branch.py` (create)

**Interfaces:**
- Consumes: `PREFIX`, `remote_branches`, `trunk_ancestry`, `load_ledger`, `branch_reconcile`, `_git`, `bust_branch_caches`.
- Produces:
  - `do_delete_branch(branch: str) -> str`
  - `do_delete_branches_bulk(kind: str) -> str` (`kind ∈ {"merged","rejected"}`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_delete_branch.py`:

```python
# Guardrails: only autopilot/* deletable; bulk only touches server-classified
# merged/rejected; awaiting/orphan never deleted in bulk. git push is faked.
import importlib, sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"


def _cc(tmp_path, monkeypatch):
    ap_home = tmp_path / ".autopilot"
    (ap_home / "state").mkdir(parents=True)
    monkeypatch.setenv("AP_HOME", str(ap_home))
    monkeypatch.setenv("AP_STATE", str(ap_home / "state"))
    monkeypatch.setenv("ORBIT_BASE_BRANCH", "feature/trunk")
    sys.path.insert(0, str(ENGINE))
    sys.modules.pop("command_center", None)
    mod = importlib.import_module("command_center")
    importlib.reload(mod)
    sys.path.remove(str(ENGINE))
    return mod


class _FakeGit:
    def __init__(self): self.deleted = []
    def __call__(self, *args, **kw):
        import types
        if args[:2] == ("push", "origin") and "--delete" in args:
            self.deleted.append(args[-1])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def test_refuses_non_autopilot(tmp_path, monkeypatch):
    cc = _cc(tmp_path, monkeypatch)
    fg = _FakeGit(); monkeypatch.setattr(cc, "_git", fg)
    msg = cc.do_delete_branch("main")
    assert "refused" in msg.lower()
    assert fg.deleted == []


def test_single_delete_autopilot(tmp_path, monkeypatch):
    cc = _cc(tmp_path, monkeypatch)
    fg = _FakeGit(); monkeypatch.setattr(cc, "_git", fg)
    msg = cc.do_delete_branch("autopilot/task-x")
    assert "deleted" in msg.lower()
    assert fg.deleted == ["autopilot/task-x"]


def test_bulk_merged_only_touches_merged(tmp_path, monkeypatch):
    cc = _cc(tmp_path, monkeypatch)
    fg = _FakeGit(); monkeypatch.setattr(cc, "_git", fg)
    monkeypatch.setattr(cc, "remote_branches", lambda: [
        ("autopilot/task-m", "sha_m", 1_000_000),   # merged (in ancestry)
        ("autopilot/task-a", "sha_a", 1_000_000),   # awaiting
    ])
    monkeypatch.setattr(cc, "trunk_ancestry", lambda: {"sha_m"})
    monkeypatch.setattr(cc, "load_ledger", lambda: {
        "a": {"state": "pushed", "sha": "sha_a", "remote_ref": "origin/autopilot/task-a"},
    })
    msg = cc.do_delete_branches_bulk("merged")
    assert fg.deleted == ["autopilot/task-m"]   # awaiting NOT deleted
    assert "1" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_delete_branch.py -v`
Expected: FAIL — `has no attribute 'do_delete_branch'`

- [ ] **Step 3: Write the implementation**

In `engine/command_center.py`, after `do_merge_to_loop` (~line 800), add:

```python
def do_delete_branch(branch: str) -> str:
    # Delete a single remote branch. HARD guardrail: only <PREFIX>/* refs — named
    # / team branches can never be deleted from this UI. Per-branch delete is
    # allowed for ANY category (the UI gates unmerged ones behind a confirm).
    if not branch.startswith(PREFIX + "/"):
        return f"Refused: '{branch}' is not under '{PREFIX}/' — only orbit task branches are deletable here."
    r = _git("push", "origin", "--delete", branch)
    bust_branch_caches()
    if r.returncode != 0:
        return f"Delete of '{branch}' FAILED:\n{r.stderr.strip()}"
    return f"Deleted origin/{branch}."


def do_delete_branches_bulk(kind: str) -> str:
    # Bulk delete only branches the SERVER classifies as `kind` (merged|rejected).
    # Never trusts the client; awaiting-review and orphan branches are never
    # touched here — those are per-branch, confirmed deletes only.
    if kind not in ("merged", "rejected"):
        return f"Refused: bulk delete only supports merged|rejected (got '{kind}')."
    import time as _t
    rows = branch_reconcile(remote_branches(), trunk_ancestry(), load_ledger(), int(_t.time()))
    targets = [r["branch"] for r in rows
               if r["category"] == kind and r["branch"].startswith(PREFIX + "/")]
    deleted, failed = [], []
    for b in targets:
        if _git("push", "origin", "--delete", b).returncode == 0:
            deleted.append(b)
        else:
            failed.append(b)
    bust_branch_caches()
    tail = f" ({len(failed)} failed)" if failed else ""
    return f"Deleted {len(deleted)} {kind} branch(es){tail}."
```

Then add two routes in `do_POST` (before the final `else:` at ~line 1785):

```python
            elif path == "/delete-branch":
                branch = (data.get("branch", [""])[0]).strip()
                if not re.match(r"^[A-Za-z0-9._/-]{1,120}$", branch):
                    self._send(b'{"ok":false,"msg":"invalid branch"}', "application/json", 400); return
                msg = do_delete_branch(branch)
            elif path == "/delete-branches-bulk":
                msg = do_delete_branches_bulk((data.get("kind", [""])[0]).strip())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_delete_branch.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add engine/command_center.py tests/test_delete_branch.py
git commit -m "feat: guarded single + bulk delete of autopilot branches"
```

---

### Task 6: The Branches tab (frontend) + operator-guide docs

Add the nav entry, the view, the render function, and delete wiring to the inline SPA, then document it. No JS test harness exists for the dashboard (it is server-rendered HTML + vanilla JS), so this task is verified by curl + a manual browser pass.

**Files:**
- Modify: `engine/cc_shell.html` — `VIEWS` (line 492), nav block (~line 339), view sections (~line 401), draw dispatch (line 1084) + badge (line 689), and a new `renderBranches()` (after `renderShips`, ~line 810)
- Modify: `docs/OPERATOR-GUIDE.md`

**Interfaces:**
- Consumes: `S.branches` from `/api/state` (Task 4); `post(path, obj)` helper (line ~1306); `esc()`, `nb()` helpers.

- [ ] **Step 1: Add the tab to the view registry + nav + badge**

In `engine/cc_shell.html`:

Line 492 — add `'branches'`:
```javascript
const VIEWS=['overview','inbox','queue','ships','branches','agents','insights'];
```

Nav block — add after the Ships button (line 339):
```html
    <button class="nav" data-view="branches" onclick="go('branches')"><span class="ico">⎇</span>Branches<span class="nb" id="nb-branches">0</span></button>
```

Keyboard shortcut cap (line ~504 `if(i>=1&&i<=6)`) — bump to 7 so the new view is reachable:
```javascript
  const i=parseInt(e.key,10); if(i>=1&&i<=7) go(VIEWS[i-1]);
```

Badge (after line 689 `nb('nb-ships',...)`):
```javascript
  nb('nb-branches',(r.branches||[]).filter(b=>b.category==='awaiting'||b.category==='orphan').length,false);
```

- [ ] **Step 2: Add the view container + row styles**

After the ships `<section>` (ends ~line 403), add:
```html
  <section class="view" id="view-branches">
    <h2>Branches <span class=aside>every autopilot/* branch on origin, reconciled against the ledger</span></h2>
    <div id="branchesBody"></div>
  </section>
```

Add these rules to the `<style>` block (anywhere inside it, e.g. after the `.chip` rules ~line 202) — the row/section layout the render function above relies on:
```css
.bsec{margin:14px 0}
.bsec-h{display:flex;align-items:center;gap:8px;margin:0 0 6px;font-size:13px}
.brow{display:flex;align-items:center;gap:8px;padding:7px 10px;border:1px solid var(--line);
  border-radius:8px;margin:4px 0;background:var(--card);font-size:12.5px}
.brow code{font:600 11.5px var(--mono);color:var(--ink)}
.brow .spacer{flex:1}
```

- [ ] **Step 3: Add `renderBranches()` and delete helpers**

After `renderShips()` (~line 810), add. NOTE: this reuses the dashboard's existing
`badge(text, cls)` helper (classes `ok|warn|stop|muted`), `openText(...)`, `post(...)`,
and `rejectTask(id)` — the same primitives the Ships `card()` uses. It deliberately does
**not** call `/merge-to-loop` (that endpoint's allowlist only accepts feature-agent builds
from `feature_builds.json` and refuses task branches); merges are recorded via `/mark`
exactly like Ships, and the real merge happens via the PR or is auto-detected by ancestry.

```javascript
/* ── branches view ──────────────────────────────────────────────────────── */
function branchRow(b){
  const st = b.merged?badge('merged','ok')
    : b.category==='rejected'?badge('rejected','stop')
    : b.category==='awaiting'?badge('awaiting review','warn')
    : badge('orphan','muted');
  let acts='';
  if(b.has_packet) acts+=`<button class="btn btn-blue btn-mini" onclick="openText('Review packet — ${esc(b.task_id)}','/review?id=${encodeURIComponent(b.task_id)}',false)">Review</button>`;
  if(b.pr_url) acts+=`<a class="btn btn-blue btn-mini" href="${esc(b.pr_url)}" target="_blank" rel="noopener">Open PR ↗</a>`;
  if(b.category==='awaiting'){
    acts+=`<button class="btn btn-go btn-mini" title="record that this landed (feeds merge-rate)" onclick="post('/mark',{id:'${esc(b.task_id)}',outcome:'merged'})">Mark merged</button>`;
    acts+=`<button class="btn btn-stop btn-mini" onclick="rejectTask('${esc(b.task_id)}')">Reject</button>`;
  }
  acts+=`<button class="btn btn-stop btn-mini" onclick="delBranch('${esc(b.branch)}',${b.merged||b.category==='rejected'})">Delete</button>`;
  return `<div class=brow><code>${esc(b.branch)}</code> ${st} <span class=aside>${b.age_days}d</span><span class=spacer></span>${acts}</div>`;
}
function renderBranches(){
  const bs=(S&&S.branches)||[];
  const groups=[['awaiting','Awaiting review',null],
                ['merged','Merged','merged'],
                ['orphan','Orphans',null],
                ['rejected','Rejected','rejected']];
  let html='';
  for(const [cat,label,bulk] of groups){
    const rows=bs.filter(b=>b.category===cat);
    if(!rows.length) continue;
    const bulkBtn = bulk?`<button class="btn btn-stop btn-mini" onclick="delBulk('${bulk}',${rows.length})">Delete all ${bulk} (${rows.length})</button>`:'';
    html+=`<div class=bsec><div class=bsec-h><b>${label}</b> <span class=nb>${rows.length}</span>${bulkBtn}</div>${rows.map(branchRow).join('')}</div>`;
  }
  document.getElementById('branchesBody').innerHTML = html ||
    `<div class=empty>No autopilot branches on origin.</div>`;
}
function delBranch(branch,safe){
  const warn = safe?'' : '\n\nThis branch is NOT merged — its work may be unreviewed. ';
  if(confirm(`Delete origin/${branch}?${warn}`)) post('/delete-branch',{branch:branch});
}
function delBulk(kind,n){
  if(confirm(`Delete all ${n} ${kind} branches from origin? This cannot be undone.`)) post('/delete-branches-bulk',{kind:kind});
}
```

- [ ] **Step 4: Wire `renderBranches()` into the draw dispatch**

Line 1084 — add `renderBranches();`:
```javascript
  renderStats();renderInboxAll();renderFilters();renderQueue();renderShips();renderBranches();renderAgents();
```

- [ ] **Step 5: Restart the dashboard and verify via curl**

Run:
```bash
launchctl kickstart -k "gui/$(id -u)/com.orbit.dashboard-cdp-metaql"
sleep 2
curl -s http://127.0.0.1:8787/ | grep -c 'data-view="branches"'
curl -s http://127.0.0.1:8787/api/state | python3 -c "import json,sys; print('branches in state:', len(json.load(sys.stdin).get('branches',[])))"
```
Expected: the grep prints `1` (nav button present); the state prints a branch count (~16).

- [ ] **Step 6: Manual browser pass**

Open `http://127.0.0.1:8787`, click **Branches**. Confirm: four sections render with correct counts; a merged branch shows the "merged" pill and a working "Delete all merged"; an awaiting branch shows Review/Open PR/Merge; deleting a branch removes it within one poll (~3s); deleting an unmerged branch shows the extra warning in the confirm.

- [ ] **Step 7: Update the operator guide**

In `docs/OPERATOR-GUIDE.md`, add a "Branches tab" subsection: what the four buckets mean (awaiting-review / merged / orphan / rejected), that merge status is local git ancestry against the base branch (fetched every ~60s, accurate even when paused), the delete guardrails (autopilot/* only; bulk = merged/rejected only; unmerged is per-branch confirm), and that re-run timestamp branches appear as orphans.

- [ ] **Step 8: Commit**

```bash
git add engine/cc_shell.html docs/OPERATOR-GUIDE.md
git commit -m "feat: Branches tab — reconcile + delete autopilot branches from the dashboard"
```

---

## Self-Review Notes

- **Spec coverage:** Part A → Task 1. Part B (reconciler + freshness + ancestry) → Tasks 2–3. Part C (tab) → Task 6. Part D (delete + guardrails) → Task 5. `/api/state` exposure → Task 4. Docs → Task 6 Step 7. Testing → Tasks 1,3,4,5 (Python) + Task 6 (curl/manual).
- **Correction vs spec:** the spec's "Jest/RTL harness" line is inaccurate for the dashboard (no JS test harness exists); this plan verifies UI via curl + manual browser and unit-tests the logic in Python. Update the spec's Testing section to match when convenient.
- **Type consistency:** `branch_reconcile(branches, ancestry, ledger, now_ts)` and the row keys (`branch, task_id, tip, merged, ledger_state, is_current_ref, is_orphan, age_days, category`) are used identically in Tasks 3, 4, 5, 6. `do_delete_branch(branch)` / `do_delete_branches_bulk(kind)` match their routes and tests.
- **Reused frontend primitives:** `badge(text, cls)` (classes `ok|warn|stop|muted`), `openText(title,url,isDiff)`, `post(path,obj)`, `rejectTask(id)`, `esc()`, `nb()` — all pre-existing in `cc_shell.html` (verified against the Ships `card()`). The branch tab does NOT use `/merge-to-loop` (feature-build allowlist only); it records merges via `/mark` and relies on ancestry auto-detection, matching Ships.
- **CSS:** `.brow/.bsec/.bsec-h/.spacer` are added in Task 6 Step 2; `.aside`, `.empty`, `.btn*`, `.nb`, `.chip` already exist.
