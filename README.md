# Orbit

**An autonomous coding loop for Claude Code that works on any git repo.**

Point Orbit at a repository, give it a backlog, and it works one task at a time —
around the clock, unattended, without ever touching your main branch. Each finished
task lands as **one atomic commit on its own review branch**. A human reviews and
merges every ship; Orbit never merges anything itself.

```
one cycle:  pick task ─▶ route (maker · model · skill · tracks) ─▶ build ⇄ check ─▶ verify-spec ─▶ commit ─▶ push review branch ─▶ you review
```

One notch forward per cycle, never backward, every change independently reviewable.

## Why this exists

Running a coding agent unattended fails in predictable ways: it invents work, ships
half-verified changes, piles unrelated edits into one diff, or quietly pushes to main.
Orbit is the harness that removes those failure modes:

- **A human-curated backlog is the only source of work.** No task in the queue → the
  loop exits cleanly. It never invents work.
- **Your own test/lint commands (the "gates") are the definition of done.** A checker
  agent must show real passing output — assertions of "all green" are rejected.
- **A separate verifier agent judges the diff against the task's acceptance criteria**
  before anything is committed. Tasks without acceptance criteria are hard-gated out.
- **The agent cannot push.** Only the wrapper script pushes, always to a per-task
  branch, never `--force`, never the base branch.
- **Anything requiring judgment escalates to you** — auth, payments, migrations,
  secrets, CI config, and architecture decisions are refused, not attempted.

## How it stays repo-agnostic

One boundary splits everything: the **engine** (this repo, generic, never names a
project) vs. the **profile** (everything project-specific, living in the target
repo's `.autopilot/` directory).

```
orbit/  (engine — install once)        <your-repo>/.autopilot/  (profile — per repo)
├── engine/    loop + helpers            ├── config.yaml   repo, base branch, GATES, model
├── agents/    builder/checker/verifier  ├── router.yaml   optional routing override
├── skills/    the /orbit-cycle command  ├── tracks/       your repo's playbooks
├── router/    default routing           ├── backlog.yaml  the task queue
├── tracks/    generic templates         └── state/        ledger, queue, reviews (gitignored)
├── goldens/   graded output exemplars
├── adapters/  opt-in task sources
└── install/   launchd + systemd service
```

The one block that makes Orbit work on *your* repo is **`gates:`** in
`.autopilot/config.yaml` — the commands that prove your repo is healthy (tests,
lint, typecheck). `install.sh` auto-detects a starter set from your stack; you
confirm it. Everything else has sensible defaults.

## Quickstart

```bash
# 1. install the engine (once per machine)
git clone https://github.com/<you>/orbit ~/orbit

# 2. onboard a repo
cd ~/code/my-project
~/orbit/install.sh .        # scaffolds .autopilot/, auto-detects gates, installs /orbit-cycle, links `orbit`

# 3. review the two things auto-detection can't nail
$EDITOR .autopilot/config.yaml    # confirm gates: commands actually pass locally
$EDITOR .autopilot/backlog.yaml   # add tasks (with acceptance criteria — required)

# 4. validate, try one cycle in the foreground, then go unattended
orbit doctor .              # read-only wiring check + routing dry-run
orbit run .                 # watch it work one task
orbit install .             # background service (launchd on macOS, systemd on Linux)
```

Full walkthrough with prerequisites, first-cycle verification, and troubleshooting:
**[docs/RUNBOOK.md](docs/RUNBOOK.md)**.

## Commands

`orbit <verb> <target-repo>` — a thin dispatcher (`bin/orbit`):

| verb | does |
|------|------|
| `init` | scaffold `.autopilot/` (same as `install.sh`) |
| `doctor` | validate config + router + tracks + skills; dry-run routing (read-only) |
| `run` | run the loop in the foreground |
| `install` | install the background service |
| `sync` | re-copy the engine's command + agents into the target (after an engine `git pull`) |
| `pause` / `resume` | kill switch (touch / remove the STOP file) |
| `status` | queue + ledger + today's spend |

## The dashboard

`engine/command_center.py` serves a live control panel (stdlib-only, default
`http://127.0.0.1:8787`): watch the in-flight task, reorder or promote backlog
items, answer escalations, review finished branches, merge/reject/revert ships,
and manage `autopilot/*` branches — all without disturbing the running loop.
What each section means and how to operate it: **[docs/OPERATOR-GUIDE.md](docs/OPERATOR-GUIDE.md)**.

## Requirements

- **[Claude Code](https://claude.com/claude-code)** (`claude`) on PATH — Orbit drives
  it headless (`claude -p /orbit-cycle`).
- Python 3 with `pyyaml`. Git.
- A test/lint command for your repo (the gates).
- For the background service: launchd (macOS) or systemd-user (Linux). On Windows,
  run `orbit run` under your own supervisor.

## Safety posture

- The agent runs **push-denied and destructive-git-denied**
  (`config/orbit.settings.json`); only the wrapper pushes, and unattended deletes
  escalate instead of executing.
- Auth / payments / migrations / secrets / CI edits are refused unless a task is
  explicitly `forced: true` **and** code-fixable.
- Every cycle leaves a backup patch (`state/diffs/`) and a review packet
  (`state/reviews/`), so every ship is auditable and every merge revertible.

## Documentation

| doc | read it for |
|-----|-------------|
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | step-by-step setup with Claude Code, first cycle, going unattended, troubleshooting |
| [docs/SETUP.md](docs/SETUP.md) | the full `.autopilot/` profile reference (config, tracks, backlog, adapters) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | how the pieces fit: the cycle, routing, skills vs tracks, state |
| [docs/OPERATOR-GUIDE.md](docs/OPERATOR-GUIDE.md) | the dashboard, plain-words — what each section is and what you do there |
| [config/schema.yaml](config/schema.yaml) | every config field, documented at the source of truth |

## License

See [LICENSE](LICENSE).
