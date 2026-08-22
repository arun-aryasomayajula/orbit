# Orbit Product M5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the final milestone M5: Figma read-side sources, the design-review workflow, Confluence/Jira publish adapters with dashboard buttons, multi-slot service units, slot-aware metrics + notify dedupe, doctor/publish hardening, the accumulated polish batch, and the closing docs sweep.

**Architecture:** Publishing is engine-side REST (`engine/publish.py`, urllib + tokens from `.autopilot/.secrets.env`, per the PREMIUM guide's REST-over-MCP guidance for office networks), gated on recorded human approval and idempotent via ids recorded back into the artifact/marker. Figma arrives as a typed source kind through the existing `mcp:` block - no new engine surface. Everything else extends established seams.

**Tech Stack:** Python 3 stdlib + PyYAML, Bash, vanilla JS, pytest (HTTP tested against local stub servers).

**Spec:** `docs/superpowers/specs/2026-08-20-orbit-product-teams-design.md` (sections 7, 11, 12, 13's M5 line; decisions 9-10).

## Global Constraints

- Publish adapters run ONLY on artifacts with recorded approval (PRD marker `status: done`; insights that passed lint) and are idempotent: the created page/issue id is recorded (marker `published` block / doc frontmatter comment) and re-publish updates rather than duplicates.
- Tokens only from env/secrets (`ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN`); never logged, never in URLs; publish.py never prints response bodies that could echo them.
- All HTTP in tests goes to local stub servers (http.server); no network in the suite.
- N=1 and single-slot service deployments keep working untouched; multi-slot units are opt-in via `concurrency` + re-running `orbit install`.
- Scoped `git add`; conventional commits, NO trailers; plain dashes; suite baseline 1210 stays green after every task.

## File Structure

| File | Responsibility |
|---|---|
| `engine/publish.py` (create) | Markdown→Confluence-storage rendering (stdlib), Confluence page create/update, Jira issue create, idempotence records, CLI (`prd <target> <run>`, `finding <target> <insight-stem>`). |
| `engine/prd_evals.py`, `skills/orbit-prd-research.md` (modify) | `figma` joins the source-kind enum; research skill cites Figma frames via workspace MCP when declared. |
| `skills/orbit-design-review.md` (create) + category wiring | Design audit workflow (Figma frames via MCP or live pages via browse.py), output under insights/, lint-gated. |
| `engine/config.py`, `config/schema.yaml`, `install/init.py` (modify) | `publish:` block (confluence: base_url/space/parent_id?; jira: base_url/project/issue_type) validated + documented + commented in the product template. |
| `engine/doctor.py` (modify) | publish secrets/config advisories. |
| `engine/command_center.py`, `engine/cc_shell.html` (modify) | Publish button on done PRD runs (detached publish.py, epic pattern), File-Jira button on insight artifacts (`/artifact-action`), published ids/urls rendered. |
| `install/install_service.sh`, `install/launchd.plist.tmpl`, `install/systemd.service.tmpl`, `engine/doctor.py` (modify) | Multi-slot units (`com.orbit.orbit-<slug>-slot<N>` with ORBIT_SLOT/ORBIT_SLOTS) from `concurrency`; uninstall/upgrade handling; doctor slot-count check; the M4 doc caveats replaced with the real thing. |
| `engine/command_center.py`, `engine/run.sh`, `engine/notify.py` (modify) | Slot-tagged turnaround pairing (restore the metric at N>1); notify dedupe window. |
| Polish batch: `engine/cc_shell.html`, `engine/command_center.py` (modify) | Hero status line slot-aware; workers strip placed beside the board's live indicator; esc/escJsAttr consistency; watch rows get stable ids. |
| `README.md`, `docs/SETUP.md`, spec (modify) | SETUP.md caught up (mcp/secrets/product/concurrency - it drifted since M2); README M5 pointers; spec marked shipped. |
| Tests: `tests/test_publish.py` (create); extend doctor/dashboard/service/notify tests. |

---

### Task 1: Figma as a first-class source kind
- engine/prd_evals.py KINDS gains `figma`; skills/orbit-prd-research.md gains a short Figma-sources paragraph (cite frames as `{kind: figma, ref: <file/frame url>, ...}` when the workspace's mcp: declares a figma server; abstain otherwise); install/init.py product template's commented mcp: example gains a figma stdio/http stanza; schema.yaml mentions it. Tests: evals accept kind figma; scaffold text present. Commit `feat(figma): figma frames are typed research sources`.

### Task 2: `skills/orbit-design-review.md` + `design_review` category
- Skill: audits a named screen/flow against design rubrics (hierarchy, spacing, consistency, interaction states, accessibility flags as critical - the M1-referenced gstack design-review criteria, credited) using Figma frames via mcp__* when declared, else live pages via `python3 $ORBIT_HOME/engine/browse.py fetch`; abstains into Coverage gaps without either. Output `insights/<task-id>-design-<slug>.md` (# / ## Answer with severity-bucketed findings / ## Basis with figma/scrape sources), insight_lint-gated. Frontmatter like orbit-hypothesis (`Read, Grep, Glob, Write, Edit, mcp__*, Bash` with the browse.py-only Bash comment), model opus.
- Category `design_review` wired everywhere (KNOWN_CATEGORIES, both SAFE sets, router → analyst + this skill, goldens always, product template workable list). Tests: vocab + scaffold presence. Commit `feat(design): design-review workflow under the grounding gate`.

### Task 3: `engine/publish.py` core + Confluence
- `render_storage(md_text) -> str`: minimal markdown→XHTML (headings, paragraphs, lists, bold/italic/code, links, blockquotes; everything else escaped literally). Pure function, heavily tested.
- Confluence REST (urllib): find-or-create by recorded id; `publish_prd(target, run)`: require marker status done, render 04-prd.md, create/update page in `publish.confluence.space` (title = PRD title + run id), record `{page_id, url, at}` into the marker's `published.confluence`; re-run updates the same page. Auth basic from ATLASSIAN_EMAIL/TOKEN via secrets_env/os.environ. Clear errors (missing config exit 2, missing secrets exit 3, unapproved exit 4, HTTP failure exit 5 with status only - no bodies).
- Tests: render cases; a local stub Confluence (http.server recording requests) - create then update path, idempotence, auth header shape (base64 of email:token), no token in any log output. Commit `feat(publish): approved PRDs publish to Confluence`.

### Task 4: publish.py Jira findings
- `publish_finding(target, insight_stem)`: require the doc passes insight_lint solo; create a Jira issue (project/issue_type from config; summary = doc title; description = Answer section + a Basis excerpt + a workspace path pointer); record `<!-- published: jira <KEY> -->` at the doc's end (lint-neutral - verify against insight_lint rules); re-run comments instead of duplicating (or no-ops with the key printed - pick, document). Stub-Jira tests mirroring Task 3. Commit `feat(publish): validated findings file to Jira`.

### Task 5: publish config + doctor
- config.py validate `publish:` block (shapes above); schema.yaml + product template commented block; doctor advisories: publish configured but ATLASSIAN_* secrets absent (this-shell + secrets file, name only); secrets present but publish unconfigured (informational). Tests via the established validate/doctor patterns. Commit `feat(config): publish block validated and doctored`.

### Task 6: dashboard publish surfaces
- `/prd-action` gains `publish` (legal only from done; detached publish.py, epic logging pattern); prd state exposes `published` (url shown as a link in the PRD band). New POST `/artifact-action` (form: kind=insight, name, action=file-jira) → detached publish.py finding; artifact browser rows gain the button + show the recorded key. Parity + tests (legality, Popen argv, confinement reuse). Commit `feat(dashboard): publish buttons on approved artifacts`.

### Task 7: multi-slot service units
- install_service.sh reads concurrency (config.py shellenv) and installs units slot0..N-1 (`com.orbit.orbit-<slug>-slot<N>`, env ORBIT_SLOT/ORBIT_SLOTS baked into each plist/unit); slot 0's unit keeps the legacy label for upgrade continuity (existing installs = slot 0); uninstall removes all matching; re-install reconciles (adds/removes to match current concurrency). doctor: warn when installed slot units != concurrency. Replace the M4 "slot 0 only" caveats in schema/init/README with the real behavior. Tests: unit-file generation content (no launchctl in tests - generate-only mode flag or function-level tests); doctor check. Commit `feat(install): service units per slot`.

### Task 8: slot-aware metrics + notify dedupe
- command_center's turnaround pairing keys start/terminal lines by the `[slot N]` tag (restore the metric at N>1; remove the suppress-when->1 from M4's F9). notify.py: drop an identical title+body within a 60s window (state file under $AP_STATE, best-effort, never blocks). Tests: pairing with interleaved two-slot log fixture; dedupe window. Commit `feat(metrics): slot-tagged turnaround; notify dedupe`.

### Task 9: polish batch + parked-minors sweep
- Hero "In orbit" line slot-aware (n cycles live / idle); workers strip moves beside the board's live indicator; esc→escJsAttr on the Abort&skip control; watch rows carry stable ids; loop-stop identity check already landed (verify); sweep the M2-M4 ledgers' stays-deferred lists and implement any remaining one-liners (do_save_config docstring done? preview format done? - verify, implement stragglers, list what stays parked in the report). Tests where behavior changes. Commit `fix(dashboard): slot-aware hero, polish batch, parked-minor sweep`.

### Task 10: E2E smoke + closing docs
- Smoke (scratchpad): full publish path against local stub Confluence+Jira (scaffold → fake done PRD run via ORBIT_PRD_CMD → publish → assert page/issue recorded + idempotent re-publish); multi-slot unit generation for concurrency 2 (generate-only); real-model `/orbit-design-review pilot-screen` with NO figma configured (must abstain into gaps and still lint clean).
- Docs: SETUP.md caught up (product workspaces, mcp:, secrets, concurrency, publish - the M2-era drift closed); README M5 paragraph; spec: mark M1-M5 shipped with dates, move remaining ideas (Keychain, web chat, Figma write-side, PRD stages on the loop queue) to an explicit "Future" section. Commit `docs: M5 reference; spec marked shipped`.

---

## Self-review notes
- Spec coverage: section 7 (Tasks 1-2), section 11 (Tasks 3-6), section 13 M5 line (Tasks 5, 7, 10), decision 9 (Task 6's buttons), M4 deferrals (Tasks 7-9).
- Publish safety: approval-gating + idempotence are Global Constraints; the stub-server test pattern keeps the suite offline.
- Known consciously-deferred (list travels to the Future section): Keychain secret delivery, embedded web chat, Figma write-side, PRD stages as loop queue items, DNS-rebinding close, per-slot orbit.log split.
