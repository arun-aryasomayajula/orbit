# Orbit runbook — from zero to an unattended loop on Claude Code

This is the operational sequence: every step from a bare machine to a background
service shipping review branches, with a verification checkpoint after each step.
For *what the profile fields mean*, see [SETUP.md](SETUP.md) and
[`config/schema.yaml`](../config/schema.yaml); for *how the system works inside*,
see [ARCHITECTURE.md](ARCHITECTURE.md). This doc is only the "do this, then check
that" path.

## 0. The mental model (30 seconds)

Orbit is a wrapper around **Claude Code running headless** (`claude -p /orbit-cycle`).
Each cycle it checks out a detached worktree of your base branch, lets an
orchestrator agent work exactly one backlog task through build → check →
verify-spec → commit, then the wrapper (never the agent) pushes that commit to
`autopilot/task-<id>` for you to review. Your Claude Code install, plan, and login
are what power it — Orbit adds the queue, the safety rails, and the service.

## 1. Prerequisites

| need | check | fix |
|------|-------|-----|
| Claude Code installed & logged in | `claude --version`, then `claude -p "say ok"` prints a response | install from [claude.com/claude-code](https://claude.com/claude-code), run `claude` once interactively to log in |
| headless mode actually works | `claude -p "say ok"` completes **without prompting** | if it hangs on a permission/login prompt, resolve it interactively first — the loop can't answer prompts |
| Python 3 + pyyaml | `python3 -c "import yaml"` | `python3 -m pip install pyyaml` |
| git remote access | `git -C <your-repo> fetch` works non-interactively | set up SSH keys / credential helper — the wrapper fetches and pushes every cycle |
| your repo's tests pass | run your test command by hand | fix first; Orbit's checker will (correctly) fail every task against a red baseline |

**Plan/usage note:** the loop makes real Claude Code calls (an Opus orchestrator by
default, routed makers/checkers per task). `max_tasks_per_day` and
`interval_seconds` in the config are your budget throttles; `orbit status` shows
today's spend. On a usage-limit error the wrapper backs off and retries — it does
not crash.

## 2. Install the engine (once per machine)

```bash
git clone https://github.com/<you>/orbit ~/orbit
```

Nothing to build. The engine is scripts + markdown; it is installed *into a target
repo* in the next step.

## 3. Onboard a target repo

```bash
cd <your-repo>          # must be a git repo with an origin remote
~/orbit/install.sh .
```

What this does, in order:

1. **Scaffolds `<your-repo>/.autopilot/`** — `config.yaml` (with auto-detected
   gates), `backlog.yaml`, `tracks/` seeded from the engine templates, `state/`.
2. **Installs the Claude Code integration** — copies `/orbit-cycle` into
   `.claude/commands/` and the maker/checker/verifier agents into `.claude/agents/`
   of the target. This is the whole Claude Code footprint: a slash command plus
   agent definitions, driven headless. (After updating the engine later, re-copy
   them with `orbit sync .`.)
3. **Links `orbit`** into `~/.local/bin` (make sure that's on your PATH).
4. **Runs `doctor`** and reports anything mis-wired.
5. **Offers to install the background service** — say **No** for now; go
   unattended only after you've watched one supervised cycle (step 6).

Checkpoint: `.autopilot/` exists, `.claude/commands/orbit-cycle.md` exists,
`orbit` resolves on your PATH.

## 4. Review the profile — the two things auto-detection can't nail

Open `.autopilot/config.yaml`:

- **`gates:`** — the single most important block: the commands the checker runs to
  prove your repo is healthy. Run **each gate command by hand from its `cwd`** and
  confirm it passes before trusting the loop with it. Add what auto-detection
  missed (typecheck, lint). If a gate needs a live service (postgres, docker),
  list it under `needs:` — the loop skips cycles while a need is down instead of
  failing tasks against it.
- **`base_branch`** — the branch every cycle forks from and that review branches
  target. Never pushed to directly.

Every other field is optional with sane defaults — full reference in
[SETUP.md](SETUP.md) §3 and [`config/schema.yaml`](../config/schema.yaml).

Then seed **`.autopilot/backlog.yaml`** with 1–3 small, real tasks. Task anatomy
and the category/status/autopilot semantics are in [SETUP.md](SETUP.md) §5. Two
hard rules the loop enforces:

- **No acceptance criteria → the task is not workable.** Write "done when…" lines
  a verifier could judge a diff against.
- Only `status: queued` + `autopilot: allow` tasks in a `workable_categories`
  category are ever auto-picked.

Optional but high-leverage: replace the placeholder lines in `.autopilot/tracks/`
with your repo's real gotchas ([SETUP.md](SETUP.md) §4). Tracks are the difference
between generically-correct code and code that fits your repo.

## 5. Validate the wiring

```bash
orbit doctor .
```

Read-only: validates config, router, tracks, skills, and dry-runs the routing for
your backlog. Fix everything it flags before running a cycle — doctor failures are
exactly the things that would make an unattended cycle waste a run.

## 6. First supervised cycle

```bash
orbit run .
```

Watch one full cycle in the foreground. What you should see, in order: queue
refresh → task claim → routed maker/checker rounds → verifier verdict → one commit
→ wrapper push. Afterwards, verify the artifacts:

```bash
git fetch && git branch -r | grep autopilot/     # the review branch exists
git log origin/autopilot/task-<id> -1            # one atomic commit, your trailer
cat .autopilot/state/reviews/task-<id>-notes.md  # verifier verdict + real gate output
cat .autopilot/state/STATE.md                    # run log + lesson recorded
orbit status .                                   # ledger shows the task as worked
```

If the run **escalated** instead of committing, that's the system working — read
`.autopilot/state/NEEDS_YOU.md` for why. Common on a first run: a gate that
doesn't actually pass, or a task whose category isn't workable.

Review the branch as you would any PR. Merge it (or reject it) so the loop's
pre-cycle reconcile can auto-close the task.

## 7. Go unattended

```bash
orbit install .
```

Installs a **launchd** agent (macOS) or **systemd --user** unit (Linux) that runs
the loop with KeepAlive — it survives crashes, sleep, and usage-limit backoff.
Service stdout/stderr land in the log directory the installer prints
(`launchd.out.log` / `launchd.err.log` on macOS; `journalctl --user` on Linux),
and per-cycle logs in `.autopilot/state/logs/`.

Start the dashboard when you want eyes on it:

```bash
eval "$(python3 ~/orbit/engine/config.py shellenv <your-repo>)"
python3 ~/orbit/engine/command_center.py     # http://127.0.0.1:8787  (PORT=... to change)
```

It reads state and edits the backlog; the loop picks changes up next cycle, so
it never disturbs a running task. What every section and button means:
[OPERATOR-GUIDE.md](OPERATOR-GUIDE.md).

## 8. Day-2 operations

| situation | do |
|-----------|----|
| stop the loop now | `orbit pause .` (in-flight cycle finishes; nothing new starts) — `orbit resume .` to continue |
| what's it doing / spent today? | `orbit status .`, dashboard, `.autopilot/state/logs/` |
| robot stopped and asked for help | `.autopilot/state/NEEDS_YOU.md` or the dashboard Inbox — answer or park |
| updated the engine (`git -C ~/orbit pull`) | `orbit sync <repo>` in every target, so the running agent gets the new command + agents |
| a merged ship regressed | dashboard **Revert** on the ship (a `git revert`, never `--force`) |
| queue drained | add tasks, or enable auto-promote / adapters ([SETUP.md](SETUP.md) §5–6) |
| stop everything permanently | `orbit pause .`, then unload the launchd plist / disable the systemd unit the installer created |

## 9. Troubleshooting

| symptom | likely cause → fix |
|---------|--------------------|
| `orbit: command not found` | `~/.local/bin` not on PATH — add it, or call `~/orbit/bin/orbit` directly |
| doctor: gate command fails | the command doesn't pass by hand either — fix the repo or the command; never weaken a gate to green |
| cycle exits immediately, "no safe tasks" | backlog empty, tasks not `queued`/`allow`, category not workable, or ids already in the ledger — check `orbit status .` and the backlog |
| every task escalates on the same gate | a `needs:` service is down, or the gate is flaky — the loop is refusing to ship against a red bar, which is correct |
| `claude -p` hangs under the service | first-run permission/login prompt — run `claude` interactively once as the same user, then restart the service |
| usage-limit errors in the log | expected; the wrapper backs off and retries. Lower `max_tasks_per_day` / raise `interval_seconds` to fit your plan |
| engine changes not taking effect | targets run their own copy of the command/agents — `orbit sync <repo>` |
| task re-picked forever as NO-OP | its branch merged outside the loop's knowledge — the pre-cycle auto-close reconcile handles it on next cycle; if not, check the ledger entry has a sha/branch |
