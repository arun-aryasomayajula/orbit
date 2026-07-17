# SDLC expansion — intake, epics, PRs, signals, dashboard

Status: in progress · 2026-07-17

## Scope (agreed with operator)

IN: repo intake (zero-day onboarding incl. test bootstrap), planning tier (epics),
PR raising from the wrapper, generalized signal adapters, dashboard support.
OUT: CI integration (completely), auto-merge (manual merge stays, as today),
release automation (manual for now).

**Invariant that survives every slice:** the agent never pushes, never merges,
never opens PRs itself — provider actions are wrapper-only; every phase
transition that ships or approves anything is a human click.

## Unknowns / blindspot pass

- `gh` availability & auth varies per machine → PR raising is **opt-in config,
  degrade-to-log** — a missing/unauthed `gh` must never fail a cycle.
- Intake on a big repo can be expensive → single headless call, own timeout,
  writes *proposals* only (`status: proposed`, `autopilot: human`) — never queued work.
- backlog.yaml is comment-preserving, edited by block-level text append (the
  logwatch pattern) → intake/decompose merge via the same append pattern, one
  shared helper, never a YAML round-trip.
- The dashboard (command_center.py + cc_shell.html) is the largest surface →
  smallest possible additive changes: one link, one section, reuse existing
  action plumbing.
- Epic state must be invisible to the loop → `epic` is a known category but
  never emittable; the cycle never sees one.

## Design per slice

### 1. PR raising (wrapper)
- config: `pull_requests: "off" | "github"` (default `off`).
- run.sh, after a successful push + review packet: if `github`, `gh pr create
  --head <branch> --base <base_branch> --title <commit subject> --body-file
  <review packet>`. On success `ledger.py pr <id> <url>`; on any failure log+continue.
- `ledger.py pr <id> <url>` — new verb; `show`/dashboard read `pr_url`.
- doctor: when `pull_requests: github`, check `gh` resolves + `gh auth status`.

### 2. Intake (`orbit intake <target>`)
- `engine/intake.py` orchestrates; `skills/orbit-intake.md` is the agent prompt
  (installed as `/orbit-intake` by init/sync).
- Deterministic part (no model): run each configured gate, record pass/fail;
  if **no gates configured**, inject the test-bootstrap proposal (a `testing`
  task: establish a smoke suite + gate).
- Agent part (one headless call, read-only settings): survey the repo → fill
  `.autopilot/tracks/` placeholders + write `state/intake/proposals.json`
  (candidate tasks from TODOs, flaky/failing tests, lint debt, doc gaps —
  each with `evidence`).
- intake.py merges proposals into backlog.yaml as `proposed`/`human` blocks
  (evidence → `context`), then prints a summary. Re-run = idempotent (skips ids
  already present).

### 3. Planning tier (epics)
- New category `epic` (linted known; never emitted to the queue).
- `engine/epic_plan.py`: `plan <id>` — headless planner writes
  `.autopilot/specs/<id>.md`, backlog status → `spec_ready`; `approve <id>` —
  human action, status → `approved`; `decompose <id>` — headless decomposer
  reads the approved spec, emits child tasks JSON, merged into backlog as
  `proposed` with `epic: <id>`; epic status → `decomposed`.
- Statuses: `proposed → planning → spec_ready → approved → decomposing → decomposed`
  (failure returns to the previous stable state with a note).
- Runs detached like feature_build.py, registry in `state/epics.json` so the
  dashboard can render progress.

### 4. Signals (generalized adapters)
- Resolution order for `sources:` entries: `$AP_HOME/adapters/<name>_to_backlog.py`
  (target-local) first, then `$ORBIT_HOME/adapters/`.
- Contract (documented in SETUP.md): adapter runs with `AP_HOME`/`AP_STATE`
  exported and cwd = target repo root; it appends `status: proposed` tasks to
  `$AP_HOME/backlog.yaml` itself, idempotently, and must never touch queued tasks.
- Shipped example adapters updated to resolve paths from `AP_HOME` (their old
  file-relative paths were broken outside the original host repo).

### 5. Dashboard
- Ships/branches: show the ledger `pr_url` as an "PR ↗" link when present.
- Proposed cards: render `evidence`/`source` so intake/signal leads are triageable.
- Epics: a small section listing epic tasks with stage-appropriate action
  (Plan → Approve spec → Decompose) wired to epic_plan.py via the existing
  POST-action plumbing.

### 6. Docs, doctor, tests
- SETUP.md (profile: pull_requests, adapters contract, epics), ARCHITECTURE.md
  (new components), RUNBOOK.md (intake as step 3.5, PR mode), README (SDLC
  framing), schema.yaml (new fields).
- doctor: `gh` check (above); fix latent `RATCHET_HOME` NameError (doctor.py:91).
- pytest green after every slice; new tests per engine module touched.
