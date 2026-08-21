# Orbit Product M3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship milestone M3: the analysis workflow family (funnel, hypothesis), metric watches, surveys, the minimal browse/scrape layer, PM dashboard wave 2 (watch/survey status, artifact browser, New-PRD modal), and the stage-agent permission narrowing deferred from M2.

**Architecture:** Everything rides M1/M2 machinery: new skills + router categories + the insight_lint gate for analysis and surveys; a signal adapter for watches (proposed-only, AUTO_PROMOTE for unattended); a small stdlib-first `engine/browse.py` with optional Playwright; dashboard additions follow the established command_center/cc_shell conventions.

**Tech Stack:** Python 3 stdlib + PyYAML (Playwright optional), Bash, vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-08-20-orbit-product-teams-design.md` (sections 5, 6, 8, 12 as amended 2026-08-21).

## Global Constraints

- Python 3 stdlib + PyYAML only in required paths; `playwright` strictly optional (every browse feature degrades or errors cleanly without it, and no test may require it).
- Signal adapters append `status: proposed` tasks ONLY, idempotently, via `engine/backlog_append.py` (the documented contract in backlog_to_tasks.py's run_source_adapters).
- All PM-facing artifacts keep the grounding gate: insight_lint (extended) fails any Answer/Basis violation in `insights/*.md` AND `surveys/*/report.md`.
- Dashboard rules from M2 hold: POST literals in cc_shell.html (shell-parity), new /api/state keys added to `sig()`, absent-unless-configured state keys, escJsAttr on disk-derived strings, secrets never echoed.
- Conventional commits, NO Co-Authored-By trailer. No em dashes in new text. Engine never names a specific project.
- Tests hermetic in the existing styles; the suite is currently 899 green — it stays green after every task.
- Serial execution: one implementer at a time (M2's commit-sweep lesson).

## File Structure

| File | Responsibility |
|---|---|
| `engine/browse.py` (create) | `fetch <url>` (Playwright-rendered when available, urllib+html-to-text fallback), `screenshot <url> <out>` (Playwright only), `rerun <scraper-name>` (re-run a saved extractor). |
| `skills/orbit-scrape.md` (create) | Read-only scrape contract producing typed `scrape` sources; persists proven extractors under `.autopilot/scrapers/`. |
| `skills/orbit-analyze-funnel.md`, `skills/orbit-hypothesis.md` (create) | PREMIUM-methodology analysis skills, output under `insights/`, gated by insight_lint. |
| `skills/orbit-survey-design.md`, `skills/orbit-survey-analyze.md` (create) | Survey questionnaire design and response analysis, output under `surveys/<name>/`. |
| `adapters/metric_watch_to_backlog.py` (create) | Files one proposed insight task per watch per cadence; state in `$AP_STATE/watches.json`. |
| `engine/insight_lint.py` (modify) | Also lints `surveys/*/report.md`; adds a verbatim-quote rule for survey reports. |
| `engine/backlog_lint.py`, `engine/command_center.py` SAFE, `router/router.yaml`, `install/init.py` product template (modify) | New categories: `funnel`, `hypothesis`, `survey_design`, `survey_analysis`. |
| `engine/config.py` (modify) | Validate the `watches:` block. |
| `engine/prd_flow.py` (modify) | Drop `--add-dir ORBIT_HOME` from stage invocations (posture narrowing); `install/init.py` copies `tracks/prd-grounding.md` into the workspace's `.autopilot/tracks/`. |
| `engine/command_center.py`, `engine/cc_shell.html` (modify) | Watches + surveys status, product artifact browser band, New-PRD modal form, parked-minors batch. |
| `config/schema.yaml`, `README.md` (modify) | `watches:` and scraper docs. |
| Tests: `tests/test_browse.py`, `tests/test_metric_watch.py`, extend `tests/test_insight_lint.py`, `tests/test_init_product.py`, `tests/test_prd_dashboard.py`, `tests/test_prd_flow.py`. |

---

### Task 1: `engine/browse.py` — minimal fetch/screenshot/rerun

**Interfaces:**
- `fetch(url, timeout=30) -> str`: if `playwright` importable, render via `sync_playwright` chromium headless and return `page.inner_text("body")`; else `urllib.request` + a small stdlib HTMLParser-based text extractor (strip script/style, blocks to newlines). CLI `browse.py fetch <url>` prints it.
- `screenshot(url, out_path)`: Playwright only; without it, exit 3 with `screenshot requires the optional playwright dependency (pip install playwright && playwright install chromium)`.
- Scraper store: `.autopilot/scrapers/<name>.json` = `{"name", "url", "note", "created"}`; `rerun <target> <name>` loads it and prints `fetch(url)`. `save <target> <name> <url> [--note ...]` writes it (name slug-validated with prd_flow-style regex).
- URL guard: http/https only; refuse file:// and other schemes (exit 2).
- Tests (no network, no playwright): the HTML-to-text extractor on a fixture string (script/style stripped, headings/paragraphs on their own lines); scheme guard; scraper save/rerun round-trip with fetch monkeypatched; screenshot-without-playwright exit code. TDD, then commit `feat(browse): minimal fetch/screenshot driver with saved extractors`.

### Task 2: `skills/orbit-scrape.md`

Read-only contract modeled on gstack's scrape (credited): refuse any mutating intent (form-fill, login, purchase — the skill states this and stops); use `python3 $ORBIT_HOME/engine/browse.py fetch <url>` via Bash; output is a typed source block the caller can cite (`{kind: "scrape", ref: <url>, date, summary}`) plus extracted facts with quotes; on a proven repeatable extraction, persist it via `browse.py save` and mention the saved name. Frontmatter: `allowed-tools: Read, Grep, Glob, Write, Bash`, `model: sonnet`. Also: `skills/orbit-prd-research.md` gains one line permitting `python3 $ORBIT_HOME/engine/browse.py fetch` for `scrape`-kind sources (its allowed-tools must then include Bash again — scope Bash back in for research ONLY with a comment that browse.py fetch is its sanctioned use; the M2 review's Bash concern is mitigated by the posture narrowing in Task 8). Test: product scaffold contains orbit-scrape.md. Commit `feat(scrape): read-only scrape skill over the browse driver`.

### Task 3: analysis skills — funnel + hypothesis

- `skills/orbit-analyze-funnel.md`: PREMIUM discovery-phase method (full-funnel pull for a cohort via MetaQL discovery order; conversion between stages; biggest ABSOLUTE drop ranked; 3-5 hypotheses each with data signal and estimated impact). Output `insights/<task-id>-funnel-<slug>.md` with `# title`, `## Answer` (funnel table + ranked hypotheses), `## Basis` (typed S-sources). Frontmatter like orbit-insight: `allowed-tools: Read, Grep, Glob, Write, Edit, mcp__*`, `model: opus`.
- `skills/orbit-hypothesis.md`: PREMIUM execution-phase method (one hypothesis; triangulate MetaQL + knowledge/ + scrapes; VALIDATED/NEGATED/INCONCLUSIVE verdict; INR or affected-user impact mandatory when data supports it; recommended fix). Output `insights/<task-id>-hypothesis-<slug>.md`, same doc contract.
- Categories `funnel` and `hypothesis`: add to backlog_lint KNOWN_CATEGORIES, command_center SAFE, backlog_to_tasks SAFE_CATEGORIES, router categories (analyst maker, opus/high, respective skill, discipline lines), goldens by_category → always, product template `workable_categories: [insight, funnel, hypothesis, survey_design, survey_analysis, documentation]` (one template edit shared with Task 5).
- Tests: categories known to lint + emitter + dashboard SAFE; scaffold contains both skills. Commit `feat(analysis): funnel and hypothesis workflows`.

### Task 4: insight_lint extension — surveys

- `main(repo)` also globs `surveys/*/report.md` with the same five rules, plus one survey-only rule: the report must contain at least three verbatim respondent quotes (lines matching `^> `) OR an explicit `## Coverage gaps` section explaining why not (abstain-over-guess).
- `lint_file` gains a `survey=False` flag for the extra rule; CLI behavior otherwise unchanged (exit 0 when neither dir exists).
- Tests: good survey report passes; quote-less report without gaps section fails; insights/ behavior unchanged. Commit `feat(insight): grounding lint covers survey reports`.

### Task 5: survey skills

- `skills/orbit-survey-design.md`: goals → hypotheses (challenged: each hypothesis names the evidence that would falsify it) → questionnaire (`surveys/<name>/questionnaire.md`: sections, question types, bias notes) → `surveys/<name>/meta.json` `{name, goal, hypotheses: [..], created}`. No data access needed; `allowed-tools: Read, Grep, Glob, Write, Edit`, model opus.
- `skills/orbit-survey-analyze.md`: reads `surveys/<name>/raw/*.csv` (fail loudly into the doc's Coverage gaps if none); codes open-text responses into themes with counts and verbatim quotes (`> "..."` lines); cross-tabs against MetaQL cohorts when mcp tools are available; writes `surveys/<name>/report.md` (`# `, `## Answer`, `## Basis` with S-sources incl. `{kind: doc, ref: raw/<file>.csv}`, quotes). `allowed-tools: Read, Grep, Glob, Write, Edit, mcp__*`, model opus.
- Categories `survey_design`, `survey_analysis` wired everywhere as in Task 3.
- Tests: scaffold contains both; categories known. Commit `feat(survey): design and analyze workflows under the grounding gate`.

### Task 6: `adapters/metric_watch_to_backlog.py` + `watches:` config

- Profile block: `watches: [{name, question, cadence_hours (default 168), category (default insight)}]`; `config.py validate` checks shape (name slug, question non-empty, cadence positive int, category in {insight, funnel, hypothesis}).
- Adapter (contract per run_source_adapters): reads config + `$AP_STATE/watches.json` (`{name: last_filed_iso}`); for each watch past cadence, appends ONE proposed task via backlog_append (`id: watch-<name>-<YYYYMMDD>`, `title: question`, `category`, acceptance criterion `insights/ doc passes insight_lint (Answer + Basis, every number cited)`, context naming the watch) and updates state. Idempotent: existing id → skip, state still advances. Never queues, never mutates existing tasks.
- Product template: `sources: [backlog, metric_watch]` plus a commented `watches:` example.
- Tests: first run files, second run within cadence files nothing, past cadence files a new dated id; malformed watches rejected by validate; adapter tolerates missing state file. Commit `feat(watches): cadence-based metric watch adapter`.

### Task 7: dashboard wave 2 — watches, surveys, artifact browser

- `build_state` additions (absent unless configured): `watches` (config entries + last_filed from state + the proposed/queued task id if present on the backlog) and `surveys` (each surveys/* dir: name, has_questionnaire, raw_count, has_report). New helper functions, keys added to `sig()`.
- Product artifacts band in Deep space (`#artifactsBand`): three groups — Insights (insights/*.md), PRDs (research/*/04-prd.md via existing /prd-artifact), Survey reports (surveys/*/report.md) — each row a name + `openText(...)` view button. New confined GET `/product-artifact?kind=insight|survey&name=...` (kind-scoped dir allowlist, slug regex, realpath check) for the two families /prd-artifact doesn't cover.
- Tests: state shapes; confinement rejects traversal/unknown kinds; parity. Commit `feat(dashboard): watch and survey status plus the product artifact browser`.

### Task 8: PRD stage posture narrowing (M2 deferral)

- `install/init.py --product`: copy `tracks/prd-grounding.md` into `<target>/.autopilot/tracks/` (idempotent).
- `engine/prd_flow.py` `_build_cmd`: drop `--add-dir ORBIT_HOME` (stages read the track from the workspace copy; nothing else in the engine dir is needed). The five prd skills' track-path line changes to `$AP_HOME/tracks/prd-grounding.md` only (no engine fallback).
- Tests: init copies the track; _build_cmd argv contains no --add-dir; existing prd tests stay green. Commit `fix(prd): stage agents lose engine-dir access; grounding track ships into the workspace`.

### Task 9: dashboard polish — New-PRD modal + parked minors batch

- Replace the three chained prompt()s with a modal form (reuse the modal5 pattern or add a dedicated small modal): run id (slug-validated inline), title, brief textarea, optional PM override; submits the existing /prd-action new.
- Parked minors, all in one commit: secrets preview → fixed `••••` + length (update its test); `_atomic_write_secrets` temp via tempfile.mkstemp + unlink-on-failure; insight_lint TODO/TBD word-boundary regex (`\bTODO\b`); `do_save_config` docstring mentions mcp; delete-nonexistent secret message honest ("removed 0"); awaiting-signoff gains `revise-synthesis`.
- Tests updated alongside each. Commit `fix(dashboard): New PRD modal and the parked-minors batch`.

### Task 10: E2E smoke + docs

- Smoke (scratchpad dir): scaffold product workspace; seed a fake survey (`surveys/pilot/raw/responses.csv` with a few rows) and run the survey-analyze skill via a fake `ORBIT_QA_RUN_CMD`-style stub? No — survey skills run through orbit-cycle; instead smoke the mechanical layers: metric_watch adapter files a task (run backlog_to_tasks with sources), insight_lint passes/fails survey fixtures, browse.py fetch on a local `data:`-free fixture (feed the extractor function directly), dashboard state keys appear (module-call pattern). Real-claude spot check: run ONE analysis skill headless against a scratch workspace with a seeded knowledge note (no MCP): `claude --print "/orbit-analyze-funnel ..."`? Analysis skills are cycle-routed, not argument-invoked — instead verify the funnel skill file loads as a command and do a real-claude run of orbit-survey-design (self-contained, no data needed): create task-shaped invocation via a direct `claude --print "/orbit-survey-design pilot"` in the scaffold and assert questionnaire.md + meta.json appear and pass lint. Capture outputs in the report; clean up.
- Docs: schema.yaml `watches:` + scrapers note; README one paragraph (analysis/surveys/watches). Commit `docs: M3 reference and README pointers`.

---

## Self-review notes
- Spec coverage: section 5 (Tasks 3, 6), section 6 (Tasks 4, 5), section 8 as amended (Tasks 1, 2), section 12 additions (Tasks 7, 9), M2 deferrals (Tasks 8, 9).
- Consistent vocab: category names appear identically in backlog_lint, command_center SAFE, backlog_to_tasks SAFE_CATEGORIES, router, and the product template; the plan text is the single list.
- Survey report and insight docs share one lint; the verbatim-quote rule applies only to surveys.
- Execution is strictly serial; the M2 support docs (dashboard-notes.md) carry over into the M3 SDD workspace.
