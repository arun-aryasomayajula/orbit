# Task 10 contract (plan_gate.py — deterministic scope-fence gate): the opt-in
# `plan.enforce: "block"` backstop run.sh calls right before runtime_check, on
# the SAME ahead>0 commit. Ships (exit 0) unless ALL of: a `plan:` block with
# `enforce: "block"`, a plan file at reviews/task-<id>-plan.md, at least one
# ```yaml fence in it whose parsed content is a dict with a `plan_scope` key
# (the LAST such fence wins — amendments append, never replace), and the
# commit's changed paths (`git show --name-only HEAD`) are ALL present —
# AND some path falls outside edit∪create∪allow_incidental, or inside
# `forbid` (which overrides everything) — only then exit 3, with offending
# paths written to reviews/task-<id>-plan-gate.txt. Cardinal rule: never
# block blind — every missing/unparseable precondition ships (exit 0),
# malformed YAML warns but never crashes. A malformed task id is a wiring
# mistake, not a scope question → exit 2 (usage), checked before any I/O.
#
# Run: python3.11 -m pytest tests/test_plan_gate.py -v   (from orbit/)
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "engine" / "plan_gate.py"


# --- fixtures ----------------------------------------------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _write_files(repo, files):
    for path, content in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        _git(repo, "add", path)


def make_repo(tmp_path, base_files, changed_files):
    """A real git repo: a base commit, then ONE additional commit touching
    `changed_files` — the atomic commit the cycle makes, which is what the
    gate inspects via `git show --name-only HEAD`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "orbit@test")
    _git(repo, "config", "user.name", "orbit")
    _write_files(repo, base_files)
    _git(repo, "commit", "-q", "-m", "base")
    _write_files(repo, changed_files)
    _git(repo, "commit", "-q", "-m", "cycle change")
    return repo


def scope_fence(edit=None, create=None, forbid=None, allow_incidental=None):
    scope = {}
    if edit is not None:
        scope["edit"] = edit
    if create is not None:
        scope["create"] = create
    if forbid is not None:
        scope["forbid"] = forbid
    if allow_incidental is not None:
        scope["allow_incidental"] = allow_incidental
    return "```yaml\n" + yaml.safe_dump({"plan_scope": scope}, sort_keys=False) + "```\n"


def plan_md(*fence_bodies):
    out = "# Plan — task t1\ngenerated: now · category: feature\n\n## File scope\n"
    for i, fence in enumerate(fence_bodies):
        if i > 0:
            out += f"\n## Amendment {i}\n"
        out += fence
    return out


def make_target(tmp_path, plan_body, enforce="block", tid="t1", has_plan_block=True):
    ap = tmp_path / ".autopilot"
    (ap / "state" / "reviews").mkdir(parents=True)
    cfg = 'repo: "."\nbase_branch: "main"\ngates: {t: {cmd: "true"}}\n'
    if has_plan_block:
        cfg += f'plan:\n  enforce: "{enforce}"\n'
    (ap / "config.yaml").write_text(cfg)
    if plan_body is not None:
        (ap / "state" / "reviews" / f"task-{tid}-plan.md").write_text(plan_body)
    return ap


def run_gate(ap_home, worktree, tid="t1"):
    env = dict(os.environ, AP_HOME=str(ap_home), AP_STATE=str(ap_home / "state"))
    return subprocess.run([sys.executable, str(GATE), tid, str(worktree)],
                          capture_output=True, text=True, env=env)


def gate_file_text(ap_home, tid="t1"):
    f = ap_home / "state" / "reviews" / f"task-{tid}-plan-gate.txt"
    return f.read_text() if f.exists() else None


# --- in-scope ships, out-of-scope escalates ----------------------------------

def test_committed_paths_within_fence_ship(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"}, {"src/app.py": "code"})
    ap = make_target(tmp_path, plan_md(scope_fence(edit=["src/app.py"])))
    r = run_gate(ap, repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_extra_path_outside_fence_escalates(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"},
                      {"src/app.py": "code", "extra/oops.py": "oops"})
    ap = make_target(tmp_path, plan_md(scope_fence(edit=["src/app.py"])))
    r = run_gate(ap, repo)
    assert r.returncode == 3, r.stdout + r.stderr
    text = gate_file_text(ap)
    assert text is not None and "extra/oops.py" in text
    assert "src/app.py" not in text


def test_forbid_overrides_edit(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"}, {"src/app.py": "code"})
    ap = make_target(tmp_path, plan_md(scope_fence(edit=["src/app.py"], forbid=["src/app.py"])))
    r = run_gate(ap, repo)
    assert r.returncode == 3, r.stdout + r.stderr
    text = gate_file_text(ap)
    assert text is not None and "src/app.py" in text
    assert "forbid" in text.lower()


def test_dir_prefix_and_glob_entries_match(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"},
                      {"src/nested/app.py": "code", "yarn.lock": "lockfile"})
    ap = make_target(tmp_path, plan_md(
        scope_fence(edit=["src/"], allow_incidental=["*.lock"])))
    r = run_gate(ap, repo)
    assert r.returncode == 0, r.stdout + r.stderr


# --- amendment semantics: LAST plan_scope fence wins -------------------------

def test_amendment_last_fence_wins_allows_new_path(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"}, {"b.py": "code"})
    ap = make_target(tmp_path, plan_md(scope_fence(edit=["a.py"]), scope_fence(edit=["b.py"])))
    r = run_gate(ap, repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_amendment_last_fence_wins_drops_old_path(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"}, {"a.py": "code"})
    ap = make_target(tmp_path, plan_md(scope_fence(edit=["a.py"]), scope_fence(edit=["b.py"])))
    r = run_gate(ap, repo)
    assert r.returncode == 3, r.stdout + r.stderr
    text = gate_file_text(ap)
    assert text is not None and "a.py" in text


# --- never block blind --------------------------------------------------------

def test_no_plan_file_ships(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"}, {"anything.py": "code"})
    ap = make_target(tmp_path, plan_body=None)
    r = run_gate(ap, repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_plan_config_block_ships(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"}, {"anything.py": "code"})
    ap = make_target(tmp_path, plan_md(scope_fence(edit=["src/app.py"])), has_plan_block=False)
    r = run_gate(ap, repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_enforce_verifier_ships(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"}, {"anything.py": "code"})
    ap = make_target(tmp_path, plan_md(scope_fence(edit=["src/app.py"])), enforce="verifier")
    r = run_gate(ap, repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_invalid_yaml_fence_ships_with_warning(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"}, {"anything.py": "code"})
    bad = "# Plan\n\n## File scope\n```yaml\nplan_scope: [unterminated\n```\n"
    ap = make_target(tmp_path, bad)
    r = run_gate(ap, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" in r.stdout


def test_fence_with_non_list_entry_ships_with_warning(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"}, {"anything.py": "code"})
    bad = "# Plan\n\n## File scope\n```yaml\nplan_scope:\n  edit: \"src/app.py\"\n```\n"
    ap = make_target(tmp_path, bad)
    r = run_gate(ap, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" in r.stdout


def test_non_dict_config_ships_without_traceback(tmp_path):
    """A syntactically-valid config.yaml whose top level isn't a mapping (a
    bare list) must never crash main() with an AttributeError on `.get()` —
    it's treated the same as a missing/corrupt config (feature off)."""
    repo = make_repo(tmp_path, {"README.md": "x"}, {"anything.py": "code"})
    ap = tmp_path / ".autopilot"
    (ap / "state" / "reviews").mkdir(parents=True)
    (ap / "config.yaml").write_text("- some\n- top-level\n- list\n")
    (ap / "state" / "reviews" / "task-t1-plan.md").write_text(
        plan_md(scope_fence(edit=["src/app.py"])))
    r = run_gate(ap, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Traceback" not in r.stderr
    assert "AttributeError" not in r.stderr


def test_malformed_amendment_warns_but_first_valid_fence_still_enforces(tmp_path):
    """Two fences: a valid one, then a botched amendment that fails to parse.
    Enforcement stays on the last VALID fence (the first one here) — but the
    botched amendment must still leave a WARN signal that it didn't take."""
    repo = make_repo(tmp_path, {"README.md": "x"},
                      {"a.py": "code", "b.py": "code"})
    body = plan_md(scope_fence(edit=["a.py"]))
    body += "\n## Amendment 1\n```yaml\nplan_scope: [unterminated\n```\n"
    ap = make_target(tmp_path, body)
    r = run_gate(ap, repo)
    # b.py is outside fence 1's scope → still enforced as a violation.
    assert r.returncode == 3, r.stdout + r.stderr
    text = gate_file_text(ap)
    assert text is not None and "b.py" in text and "a.py" not in text
    # ...and the botched amendment left a WARN even though fence 1 still ran.
    assert "WARN" in r.stdout


def test_git_show_failure_ships_with_warning(tmp_path):
    """An empty git repo (no commits yet) makes `git show HEAD` fail — never
    block blind on a git-layer failure, and never dump a traceback."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    ap = make_target(tmp_path, plan_md(scope_fence(edit=["src/app.py"])))
    r = run_gate(ap, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" in r.stdout
    assert "Traceback" not in r.stderr


# --- usage / task id ----------------------------------------------------------

def test_bad_task_id_exits_2(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x"}, {"anything.py": "code"})
    ap = make_target(tmp_path, plan_md(scope_fence(edit=["src/app.py"])))
    r = run_gate(ap, repo, tid="Not_A-Slug!")
    assert r.returncode == 2, r.stdout + r.stderr
