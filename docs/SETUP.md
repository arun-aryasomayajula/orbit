# Orbit setup — full profile reference

## 1. Install the engine (once per machine)
```bash
git clone <orbit-repo> ~/orbit
# ensure `claude` is on PATH and `python3 -m pip install pyyaml`
```

## 2. Onboard a target repo
```bash
cd <your-project>
~/orbit/install.sh .
```
This scaffolds `.autopilot/`, auto-detects your gates, installs the `/orbit-cycle` command + agents
into `.claude/`, links the `orbit` convenience script, runs `doctor`, and offers to install the service.

## 3. Review the profile (the two things auto-detection can't nail)
Open `.autopilot/config.yaml`:

- **`gates:`** — the most important block. This is how the checker verifies YOUR repo. Auto-detection
  gives a starting point; confirm the commands actually pass locally and add missing ones
  (typecheck, lint). `needs:` lists services that must be up (postgres, docker, redis, …).
- **`base_branch`** — the loop resets its worktree to `origin/<base_branch>` each cycle and forks
  per-task branches from it. It is never pushed to directly.

Other fields (all optional, sensible defaults): `model`, `permission_mode`, `interval_seconds`,
`max_tasks_per_day`, `cycle_timeout_seconds`, `spec` (a standing doc the loop rereads),
`workable_categories`, `sources`, `env.passthrough`, `branch_prefix`, `commit_trailer`.
Full field docs: `<orbit>/config/schema.yaml`.

## 4. Fill in tracks (optional but high-value)
`.autopilot/tracks/` seeds from the engine's generic templates. Replace the placeholder lines with
YOUR repo's real gotchas and pattern-file pointers (see `<orbit>/tracks/TEMPLATE.md` and the
filled-in `<orbit>/tracks/examples/`). Tracks are what make the maker write code that fits your repo.

## 5. Add tasks
`.autopilot/backlog.yaml` — each task:
```yaml
tasks:
  - id: fix-null-grain-crash
    title: "int(None) crashes the lane on an empty stage"
    category: bug            # bug|feature|refactor|code_quality|testing|documentation|dependencies|developer_experience|security|...
    priority: high           # high|medium|low
    status: queued           # queued (pickable) | proposed (needs promotion) | done
    autopilot: allow         # allow (auto) | human (never auto-ship) | review-only
    acceptance_criteria:
      - "empty stage returns 0, not a crash"
      - "regression test fails before, passes after"
```
Only `workable_categories` with `status: queued` and `autopilot: allow` are auto-picked. Others need
`forced: true` or a human.

## 6. Run
```bash
orbit doctor .      # validate everything, dry-run routing (read-only)
orbit run .         # one loop, foreground — watch it work a task
orbit install .     # background service (launchd/systemd)
orbit pause . / resume .   # kill switch
```

## Opt-in task sources (adapters)
Beyond the native `backlog.yaml`, add source names to `sources:` and Orbit runs the matching
`<orbit>/adapters/<name>_to_backlog.py` each cycle to convert an external source into proposed
tasks. Shipped: `foundry` (maturity tasks), `logwatch` (prod log findings), `qa` (UI-test findings).
These carry their own coupling to specific tools — treat them as examples to adapt.
