# Orbit

**An autonomous coding loop for Claude Code — repo-agnostic.** Point it at any git repo and it
works one task at a time from a backlog: pick → route → build → check → verify-spec → atomic
commit → push to its own review branch. One notch forward per cycle, never backward, every change
independently reviewable. It never pushes to your base branch and never merges — a human reviews
each ship.

```
one cycle:  pick task ─▶ router (maker·model·skill·tracks) ─▶ build ⇄ checker ─▶ verifier ─▶ commit ─▶ wrapper pushes ─▶ you review
```

## Why it's not tied to any one repo

The **engine** (this repo) is generic and never names a project. Everything project-specific lives
in the **target repo's `.autopilot/` profile**:

```
orbit/ (engine)                     <your-repo>/.autopilot/ (profile)
├── engine/    loop + helpers         ├── config.yaml   ← repo, base_branch, GATES, model, sources
├── agents/    maker/checker/verifier ├── router.yaml   ← optional override of the engine default
├── skills/    the /orbit-cycle     ├── tracks/       ← this repo's playbooks
├── router/    default routing        ├── backlog.yaml  ← the task queue
├── tracks/    generic templates      └── state/        ← ledger, queue, reviews (gitignored)
├── adapters/  opt-in task sources
└── install/   launchd + systemd
```

The single line that makes it work anywhere is the **`gates:` block** in `config.yaml` — it tells
the checker how to verify *your* repo (its test/lint/typecheck commands). `install.sh` auto-detects
a starter set from your stack.

## Setup on any system

```bash
git clone <orbit-repo> orbit
cd <your-project>
~/path/to/orbit/install.sh .        # scaffolds .autopilot/, installs /orbit-cycle, links `orbit`, offers the service
orbit doctor .                       # validate wiring (read-only, no cycle)
orbit run .                          # run one loop in the foreground to try it
orbit install .                      # install the background service (launchd on macOS, systemd on Linux)
```

Two things to do by hand after `install.sh`: **review `.autopilot/config.yaml` gates** (auto-detection
is a starting point) and **add tasks** to `.autopilot/backlog.yaml`.

## Commands (`orbit <verb> <target>`)

| verb | does |
|------|------|
| `init` | scaffold `.autopilot/` (via install.sh) |
| `doctor` | validate config + router + tracks + skills; dry-run routing (read-only) |
| `run` | run the loop in the foreground |
| `install` | install the background service |
| `pause` / `resume` | touch / remove the STOP file |
| `status` | queue + ledger + today's spend |

## Requirements

- **Claude Code** (`claude`) on PATH — Orbit drives it headless (`claude -p /orbit-cycle`).
- Python 3 + `pyyaml`. Git. A test/lint command for your repo (the gates).
- Background service: launchd (macOS) or systemd-user (Linux). Windows: run `orbit run` under a supervisor.

## Safety posture

- The agent is **push-denied and destructive-git-denied** (`config/orbit.settings.json`); only the
  wrapper pushes, always to `<branch_prefix>/task-<id>`, never `--force`, never the base branch.
- Deletes require permission → denied unattended → the cycle escalates instead.
- Refuses auth/payments/migrations/secrets/CI edits unless a task is explicitly `forced: true` and code-fixable.
- Every cycle captures a backup patch under `state/diffs/` and a review packet under `state/reviews/`.

See `docs/ARCHITECTURE.md` for how the pieces fit and `docs/SETUP.md` for the full profile reference.
