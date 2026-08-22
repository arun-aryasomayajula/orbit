# Orbit Product M4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship milestone M4: slot-loop concurrency (safe shared state + N loops per workspace), the tabbed launcher as the loop process manager with workspace creation, per-slot dashboard visibility, and the M3 deferrals (watches editor, malformed-watches test, real-model runs of the gated skills).

**Architecture:** Per the amended spec section 9: N independent `run.sh` processes ("slots") with namespaced worktrees/markers/logs; shared state made safe in Python once (flock library, exclusive wrapper-side claims handed into `/orbit-cycle <tid>`, pid-aware reaping, slot-0 housekeeping, locked spend/count). The launcher spawns/stops slot processes per tab; `concurrency: 1` default preserves today's behavior byte-for-byte.

**Tech Stack:** Python 3 stdlib + PyYAML, Bash, vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-08-20-orbit-product-teams-design.md` (sections 9-10 as amended 2026-08-22, section 12; decision 9).

## Global Constraints

- `concurrency: 1` (the default) must be behaviorally identical to today for every existing deployment (launchd services, `orbit run`): same file names for slot 0 where the dashboard reads them (slot 0 keeps the legacy `.current-task-id`/`.cycle-candidate`/`.idle-reason` names; higher slots add a `.N` suffix), same log line shapes plus a `[slot N]` tag, same STOP semantics (no new claims; in-flight cycles finish).
- All new locking uses one shared helper (`engine/locks.py`); no ad-hoc lock files. Locks are flock-based, stale-tolerant (owner pid recorded; a lock whose owner is dead is breakable), and never held across a claude invocation.
- The agent-side pick path must keep working when `/orbit-cycle` is invoked with no argument (manual runs, old deployments) - argument-given mode skips picking AND skips claiming (the wrapper already claimed).
- Reaping never escalates a task whose recorded worker pid is alive.
- Dashboard/board semantics: "running" means any slot has a live cycle; kill-task targets one slot's process tree, never `pgrep` machine-wide.
- Launcher-spawned children get `ORBIT_LAUNCHD_LABEL` per project (fixes the existing multi-project restart-loop bug).
- Serial implementers only where files overlap; scoped `git add` always. Conventional commits, NO trailers. Plain dashes. stdlib+PyYAML only. Tests hermetic; suite baseline 998 stays green after every task.

## File Structure

| File | Responsibility |
|---|---|
| `engine/locks.py` (create) | flock context manager + stale-owner breaking; used by ledger, backlog writers, spend. |
| `engine/spend.py` (create) | Locked `add <state> <date> <cost>` / `count <state> <date> [--check-cap N]` (atomic read-modify-write + cap check inside the lock). |
| `engine/ledger.py`, `engine/lifecycle.py` (modify) | `claim --exclusive` under flock recording `worker_pid`/`slot`; claim-over-in_progress illegal without --force; `reap` skips live-pid tasks. |
| `engine/backlog_append.py`, `engine/autopromote.py`, `engine/autoclose.py`, `engine/qa_check.py` set_task_field, `engine/command_center.py` `_write_blocks` (modify) | All backlog.yaml writers take the shared backlog lock; autopromote's write becomes tmp+replace; refill target = slot count. |
| `engine/run.sh` (modify) | `ORBIT_SLOT` (default 0) + `ORBIT_SLOTS` (total, default from config concurrency); per-slot WT/markers/log+patch names (`cycle-<tid>-<ts>`); wrapper picks + exclusively claims + passes tid to `/orbit-cycle <tid>` (on claim race: next candidate); spend/count via `spend.py`; housekeeping gated on slot 0; worker pid file `$STATE/workers/<slot>.pid` (+ current tid) written/cleaned per cycle. |
| `skills/orbit-cycle.md` (modify) | Optional `$ARGUMENTS` task id: use verbatim, skip pick AND claim; record-keeping steps unchanged. |
| `engine/qa_check.py` (modify) | Services-using QA gate serializes behind a per-workspace lock (blocking wait with generous timeout, then the existing unable-to-run semantics). |
| `engine/command_center.py`, `engine/cc_shell.html` (modify) | `workers` state (per-slot pid/tid/started from pid files + markers), board `live` from workers, `do_killtask` targets the owning slot's process group, probe_runtime per-slot logs; Settings gains `concurrency`; watches editor card (add/remove/edit cadence - writes via /api/config); malformed-watches regression test. |
| `engine/launcher.py`, `engine/launcher_shell.html` (modify) | Tab strip UI; per-tab loop Start/Stop (spawn N detached `run.sh` slots with `ORBIT_SLOT/ORBIT_SLOTS`, pid files; stop = STOP file + terminate slot pids); real `_loop_running` from pid files; `/_launcher/create` (mkdir + git init + init.py [--product] + register); pass `ORBIT_LAUNCHD_LABEL` to children. |
| `engine/config.py`, `config/schema.yaml`, `install/init.py` (modify) | `concurrency` field (positive int, default 1) validated + documented + in the product template (commented). |
| Tests: `tests/test_locks.py`, `tests/test_spend.py`, `tests/test_slot_claims.py`, extend `tests/test_launcher.py`, `tests/test_prd_dashboard.py`, `tests/test_metric_watch.py` (malformed-config), `tests/test_autopromote.py`. |

---

### Task 1: `engine/locks.py` + `engine/spend.py`

**Interfaces:** `locks.hold(path, timeout=30, break_stale=True)` context manager - creates `<path>` with owner pid json, flocks it, breaks it when the owner pid is dead and break_stale; raises `locks.LockTimeout` otherwise. `spend.py` CLI: `add <state_dir> <date> <cost_float>` (locked accumulate), `count <state_dir> <date>` prints int, `bump-count <state_dir> <date> --cap N` exits 0 and increments iff count < N else exits 3 WITHOUT incrementing (the cap check and increment are one critical section). Files keep today's names (`.spend-<date>`, `.count-<date>`) so `bin/orbit` and the dashboard keep reading them.
Tests: two processes contend (multiprocessing) - no lost updates over 50 increments; stale lock (dead pid) broken; cap boundary (exactly N) refuses the N+1th; LockTimeout on a live-owner lock. TDD; commit `feat(locks): shared flock helper and atomic spend/count accounting`.

### Task 2: exclusive claims + pid-aware reap

- ledger.py `claim` gains `--exclusive --pid P --slot S`: under `locks.hold(ledger.lock)`, refuse (exit 5, nothing written) when the task is already present in a non-reclaimable state; record `worker_pid`/`slot` on the entry. lifecycle.py: claim-over-in_progress becomes illegal by default (`--force` keeps working; update its tests).
- `reap`: before escalating an in_progress/committed entry older than the timeout, check its `worker_pid` - alive (os.kill(pid,0) or /proc-free psutil-less check) → skip with a log line. Entries without a pid (legacy) keep today's behavior.
- Tests: two concurrent exclusive claims on one tid - exactly one wins (multiprocessing); reap skips live-pid, escalates dead-pid and pid-less; force still overrides. Commit `feat(ledger): exclusive claims with worker identity; reap only the dead`.

### Task 3: backlog write lock everywhere

- All five backlog.yaml writers acquire `locks.hold(AP_STATE/.backlog.lock)` around their read-modify-write; autopromote's bare `write_text` becomes tmp+replace under the lock; autopromote's refill condition becomes `count_pickable() < slots` (slots from env `ORBIT_SLOTS` default 1, passed by run.sh).
- Tests: concurrent backlog_append from two processes loses nothing (50 tasks each); autopromote refills to the slot count. Commit `fix(backlog): one lock for every backlog.yaml writer`.

### Task 4: run.sh slot plumbing

- Top: `SLOT="${ORBIT_SLOT:-0}"`, `SLOTS="${ORBIT_SLOTS:-<config concurrency via shellenv>}"`; config.py shellenv emits `ORBIT_CONCURRENCY`. WT becomes `${AP_WORKTREE:-${REPO}-orbit-wt-${SLOT}}` with a compat note (slot 0 name changes - acceptable: worktrees are disposable; prepare_worktree recreates).
- Markers: slot 0 keeps legacy names; slot N>0 uses `.current-task-id.$SLOT` etc. Log/patch: `cycle-<tid>-<ts>.log/.patch` (tid known pre-cycle now). Log lines gain a `[slot $SLOT]` prefix via the existing log() helper.
- Worker pid file `$STATE/workers/$SLOT.pid` (json: pid, tid, started) written before run_cycle, removed after; trap cleans it.
- Claim-then-run: after housekeeping (slot 0) / directly (slot N), loop over `next_pickable_id` candidates: `ledger.py claim --exclusive <tid> --pid $$ --slot $SLOT` - exit 5 → try next candidate; success → `run_cycle "$out" "$tid"` invoking `/orbit-cycle $tid`. No candidates → EMPTY as today.
- Spend/count: `record_spend` calls `spend.py add`; the daily-cap check becomes `spend.py bump-count ... --cap $MAX_TASKS` per successful cycle (exit 3 → LIMIT).
- Housekeeping block (reap/autoclose/knowledge/regression/queue-regen/autopromote) wrapped in `if [ "$SLOT" = 0 ]`. `bash -n` both bashes; the fd-detachment watchdog constraint untouched.
- Tests: extend the hermetic seams - a fake-claude run at SLOT=1 writes suffixed markers and skips housekeeping (assert queue.json untouched); claim-race candidate walk (seed a ledger where candidate 1 is claimed). Commit `feat(loop): slot-namespaced cycles with wrapper-side exclusive claims`.

### Task 5: orbit-cycle argument mode

- skills/orbit-cycle.md: `$ARGUMENTS` may carry the task id. Given: use it verbatim, SKIP the pick and SKIP `ledger.py claim` (the wrapper claimed; note the marker write stays). Absent: today's behavior exactly. One sentence in the header explains why (slot loops claim first).
- Test: none automatable beyond prose - covered by Task 10's smoke. Commit `feat(cycle): accept a pre-claimed task id`.

### Task 6: QA services serialization

- qa_check.py: when the qa config declares `services`, wrap the up→agent→down span in `locks.hold(AP_STATE/qa/.services.lock, timeout=qa timeout+60)`; LockTimeout → the existing unable-to-run path (ships, never fails the cycle). Comment names the fixed-ports rationale.
- Test: lock held → second qa_check with services takes the unable path (monkeypatch timeout small). Commit `fix(qa): one services consumer at a time per workspace`.

### Task 7: dashboard per-slot visibility

- command_center.py: `workers_state()` from `$STATE/workers/*.pid` (+ liveness) merged into build_state (key `workers`, absent when the dir is empty AND concurrency==1); board `live` = any live worker; `do_killtask(tid)` finds the slot owning tid and TERMs that pid's process group only; probe_runtime's newest-log heuristic keys on the slot's own pid file's tid instead. sig() + a small workers strip in the Dev session band. Settings gains a `concurrency` numeric field (validated server-side positive int).
- Tests: workers shape; killtask targets the right pid (monkeypatch os.killpg); concurrency round-trips through /api/config. Commit `feat(dashboard): per-slot workers, targeted kill, concurrency setting`.

### Task 8: watches editor + malformed-watches regression test (M3 deferrals)

- cc_shell.html Settings: a structured watches card (rows: name/question/cadence/category with add/remove) reading cfg.watches and writing through the existing /api/config save (mcp-card family but field-structured, not raw JSON). Server: `_validate_config` accepts list-shape (deep validation stays in config.py).
- tests/test_prd_dashboard.py: watches round-trip via do_save_config; tests/test_metric_watch.py: `watches_state` tolerates non-dict entries (the T7 deferred pin).
- Commit `feat(dashboard): watches editor; pin malformed-watches tolerance`.

### Task 9: launcher - tabs, loop control, create

- launcher.py: `LOOPS: dict[path -> {slot: proc}]`; `/_launcher/loop-start {path}` reads the project's concurrency and spawns slots 0..N-1 of `engine/run.sh <path>` detached (`start_new_session=True`, env ORBIT_SLOT/ORBIT_SLOTS/ORBIT_LAUNCHD_LABEL, log to `$STATE/logs/loop-<slot>.out`); `/_launcher/loop-stop {path}` touches STOP then TERMs the tracked pids (and any `$STATE/workers/*.pid` leftovers) after a grace period, removes STOP-file? No - leaves STOP so launchd doesn't restart; document. `_loop_running` becomes pid-file-based (workers dir + tracked LOOPS). `/_launcher/create {path, name, product: bool}`: mkdir -p, `git init`, `install/init.py path ORBIT_HOME [--product]`, register, spawn dashboard child. atexit: kill dashboard children as today; deliberately DO NOT kill loop processes (comment why: loops are long-lived work; stop is explicit).
- spawn_child passes `ORBIT_LAUNCHD_LABEL=com.orbit.orbit-<slug>` (matching install_service.sh's naming).
- launcher_shell.html: tab strip across the top (one tab per registered project, active highlighted, click = /open + proxy flip), per-tab Start/Stop loop buttons + running badge from the real state, a "New workspace" button (name, path, product checkbox → /_launcher/create).
- Tests (tests/test_launcher.py style, StubHandler/fake run.sh via env override `ORBIT_LOOP_CMD` for spawn testing): loop-start spawns N with the right env; loop-stop TERMs and leaves STOP; create scaffolds + registers; label passed to children. Commit `feat(launcher): tabs, per-tab loop control, workspace creation`.

### Task 10: E2E smoke + docs + real-model runs

- Concurrency smoke (scratchpad): product workspace, concurrency 2, fake claude (env CLAUDE_BIN pointing at a script that reads the tid argument, writes a trivial insight + commits); two slots via `ORBIT_SLOT=0/1 ORBIT_SLOTS=2 bash engine/run.sh <ws>` short-circuited to one iteration each (existing test seams / ORBIT_ONE_SHOT if present, else run with STOP pre-armed after first iteration); assert: both tasks claimed exactly once each (ledger), no lost spend/count, slot-1 skipped housekeeping, markers suffixed.
- Real-model runs (M3 deferral): in a scaffold, run `/orbit-analyze-funnel` and `/orbit-survey-analyze` once each via real claude against seeded fixtures (a knowledge note + a raw CSV; no MCP) and assert their outputs pass insight_lint. Capture outputs; failures are findings, not blockers to record honestly.
- Docs: schema.yaml `concurrency` + launcher section note in README (tabs, loop control, create). Commit `docs: M4 reference; concurrency and launcher pointers`.

---

## Self-review notes
- Spec coverage: section 9 (Tasks 1-6), section 10 + decision 9 (Tasks 7-9), M3 deferrals (Tasks 8, 10).
- The compat constraint (concurrency 1 identical) is enforced by slot-0 legacy naming and default SLOTS=1; reviewers should treat any behavioral diff at N=1 as Critical.
- Known consciously-deferred items: notify batching, interleaved orbit.log stats pairing (worker tag added, pairing fix deferred), git fetch contention (tolerated today by feature builds), multi-launcher single-port assumption.
