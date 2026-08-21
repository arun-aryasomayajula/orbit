# Orbit Product M2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship milestone M2: the PRD pipeline (stamp-chained stages with deterministic evals and human gates), secrets-file token delivery, and the first wave of the PM dashboard (PRD pipeline view, mcp settings card, secrets panel, insight quick-add).

**Architecture:** `engine/prd_flow.py` is the controller and sole writer of stage stamps (dwh-suite's mechanism, Orbit-native shape, modeled on `epic_plan.py`'s detached-Popen dashboard pattern). `engine/prd_evals.py` holds Tier-A deterministic checks. Stage skills are headless claude calls with the workspace's MCP config. Secrets live in `.autopilot/.secrets.env`, sourced by `run.sh`. Dashboard work follows the mapped conventions: flat `do_*` functions + `elif` routes in `command_center.py`, `band`/`setcard` idioms in `cc_shell.html`, `test_shell_parity` satisfied, `sig()` updated.

**Tech Stack:** Python 3 stdlib + PyYAML, Bash, vanilla JS in `cc_shell.html`, pytest.

**Spec:** `docs/superpowers/specs/2026-08-20-orbit-product-teams-design.md` (sections 4, 12, decisions 9 and 10).

## Global Constraints

- Python 3 stdlib + PyYAML only. No new dependencies.
- Secrets: values live only in `<target>/.autopilot/.secrets.env` (mode 0600, gitignored). The dashboard never echoes a stored secret value back to the client (masked previews only). Generated MCP configs keep `${VAR}` references.
- Only `engine/prd_flow.py` writes stage stamps and the run marker. Skills write artifacts and stop; a hand-edited artifact must be mechanically detectable (sha mismatch).
- Every claude invocation in prd_flow honors an `ORBIT_PRD_CMD` env override (the test seam, same pattern as `ORBIT_QA_CMD` in qa_check.py) and passes the workspace's MCP config via `engine/mcp.py`.
- Tests hermetic in the existing styles: fresh-import `_mod`/`cc` fixture for command_center routes; env seams instead of real claude; tmp_path workspaces.
- New POST route path literals MUST appear in `cc_shell.html` (tests/test_shell_parity.py enforces this).
- New `/api/state` keys must be added to `sig()` in cc_shell.html or the panel never re-renders.
- Follow the "absent unless configured" discipline: a target with no `research/` dir gets no `prd` key in /api/state.
- Conventional commits, NO Co-Authored-By trailer. Engine never names a specific project.
- Deviation from spec section 4 (ruled by controller, recorded here): PRD stages are dashboard/CLI-driven detached runs (the epic precedent), not loop queue tasks, with `--chain` covering unattended multi-stage runs; loop-filed stage tasks are deferred to M3.

## File Structure

| File | Responsibility |
|---|---|
| `engine/prd_evals.py` (create) | Tier-A deterministic checks per stage artifact. Pure functions. |
| `engine/prd_flow.py` (create) | Run marker + stamp chain + gates + stage execution (headless claude) + revise cascade. CLI. |
| `skills/orbit-prd-intake.md`, `-research.md`, `-synthesis.md`, `-writer.md`, `-review.md` (create) | The five stage skills. |
| `tracks/prd-grounding.md` (create) | The shared grounding contract, briefed into every stage. |
| `engine/backlog_lint.py` + `engine/command_center.py:192` (modify) | Add `insight` to KNOWN_CATEGORIES and the dashboard SAFE set (M1 gap). |
| `engine/run.sh` (modify) | Source `.autopilot/.secrets.env` at iteration start. |
| `install/init.py` (modify) | Product template: gitignore `.secrets.env`, write `.secrets.env.example`. |
| `engine/doctor.py` (modify) | token_env check also consults `.secrets.env`. |
| `engine/command_center.py` (modify) | `/api/secrets` GET/POST, `/prd-action` POST, `/prd-artifact` GET, `prd_pipeline_state()` into build_state, mcp passthrough in config editor. |
| `engine/cc_shell.html` (modify) | Secrets card + mcp card in Settings; insight quick-add; `renderPrd()` band; `sig()` update. |
| `config/schema.yaml`, `README.md` (modify) | Document the `prd` flow and secrets file. |
| Tests: `tests/test_prd_evals.py`, `tests/test_prd_flow.py`, `tests/test_secrets.py`, `tests/test_prd_dashboard.py` (create); extend `tests/test_init_product.py`. |

## Stage artifact contracts (used by Tasks 1-4; exact keys)

- `research/<run>/01-intake.json`: `{run_id, title, pm, goal, scope_in: [..nonempty], scope_out: [..], data_targets: [..], code_targets: [..], pii_touch: bool}`
- `research/<run>/02-research.json`: `{run_id, sources: [{id: "S<n>", kind: metaql|jira|confluence|scrape|doc|code, ref, date, summary}], findings: [{claim, source_ids: ["S<n>"..]}], coverage_gaps: [..]}`
- `research/<run>/03-synthesis.json`: `{run_id, requirements: [{id: "R<n>", text, source_ids: [..]}], gaps: [..], open_questions: [..]}`
- `research/<run>/04-prd.md`: headings `## Problem`, `## Goals`, `## Non-goals`, `## Requirements`, `## Acceptance criteria`, `## Open questions`, `## Basis`; inline `(S<n>)` citations.
- `research/<run>/prd-run.json` (marker, prd_flow-owned): `{run_id, title, created, status: "<stage>"|"awaiting-scope-approval"|"awaiting-signoff"|"done", stages: {<stage>: {sha, prev_sha, stamped_at, evals: {ok: bool, errors: [..]}}}, approvals: {scope: {at}|null, prd: {at}|null}}`
- Stage order: `intake` → gate `approve-scope` → `research` → `synthesis` → `draft` (writes 04-prd.md) → `review` (appends an `## Review audit trail` section to 04-prd.md + writes `05-review.json {verdicts: [..], ok: bool}`) → gate `approve-prd`.

---

### Task 1: M1 category-gap fix (`insight` in lint + dashboard SAFE)

**Files:**
- Modify: `engine/backlog_lint.py:25-31`, `engine/command_center.py:192`
- Test: `tests/test_init_product.py` (extend)

**Interfaces:**
- Produces: `backlog_lint.KNOWN_CATEGORIES` contains `"insight"`; command_center's `SAFE` set contains `"insight"` so `/task-new` with `queue=1` sets `autopilot: allow` for insight tasks.

- [ ] **Step 1: Failing test**

```python
def test_insight_known_to_lint_and_dashboard():
    import importlib.util
    def _load(name):
        spec = importlib.util.spec_from_file_location(name, str(ORBIT / "engine" / f"{name}.py"))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
    import os
    os.environ.setdefault("AP_HOME", "/tmp/x")  # backlog_lint has no AP_HOME need; guard anyway
    bl = _load("backlog_lint")
    assert "insight" in bl.KNOWN_CATEGORIES
```

Also assert dashboard SAFE via the existing `cc` fixture pattern in `tests/test_prd_dashboard.py` later (Task 8 covers `/task-new` insight+queue=1); this task's test pins the lint set only.

- [ ] **Step 2: Run (fails) → fix**

In `engine/backlog_lint.py` KNOWN_CATEGORIES add `"insight",` with comment `# product workspace analysis (safe; gated by insight_lint)`. In `engine/command_center.py:192` change to `SAFE = {"testing", "documentation", "dependencies", "code_quality", "developer_experience", "insight"}`.

- [ ] **Step 3: Run full suite; commit** `fix(insight): register the insight category with backlog lint and the dashboard SAFE set`

---

### Task 2: `engine/prd_evals.py` — Tier-A checks

**Files:** Create `engine/prd_evals.py`, `tests/test_prd_evals.py`.

**Interfaces:**
- Produces: `check_intake(d: dict) -> list[str]`, `check_research(d: dict) -> list[str]`, `check_synthesis(d: dict, research: dict) -> list[str]`, `check_prd_md(text: str, research: dict) -> list[str]` — each returns error strings, empty = pass. `TESTABLE = re.compile(r"\d|%|>=|<=|\bpass\b|\bfail\b")` for acceptance-criteria lint.

- [ ] **Step 1: Failing tests** (one happy + one violation per checker; key cases)

```python
# tests/test_prd_evals.py
import sys
from pathlib import Path
ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import prd_evals  # noqa: E402

GOOD_RESEARCH = {"run_id": "r1",
    "sources": [{"id": "S1", "kind": "metaql", "ref": "q", "date": "2026-08-21", "summary": "x"}],
    "findings": [{"claim": "c", "source_ids": ["S1"]}], "coverage_gaps": []}

def test_intake_requires_nonempty_scope_and_bool_pii():
    bad = {"run_id": "r1", "title": "t", "pm": "p", "goal": "g",
           "scope_in": [], "scope_out": [], "data_targets": [], "code_targets": [], "pii_touch": "yes"}
    errs = prd_evals.check_intake(bad)
    assert any("scope_in" in e for e in errs) and any("pii_touch" in e for e in errs)

def test_research_findings_must_cite_resolving_sources():
    bad = dict(GOOD_RESEARCH, findings=[{"claim": "c", "source_ids": ["S9"]}])
    assert any("S9" in e for e in prd_evals.check_research(bad))
    assert prd_evals.check_research(GOOD_RESEARCH) == []

def test_synthesis_requirements_resolve_against_research():
    good = {"run_id": "r1", "requirements": [{"id": "R1", "text": "x", "source_ids": ["S1"]}],
            "gaps": [], "open_questions": []}
    assert prd_evals.check_synthesis(good, GOOD_RESEARCH) == []
    bad = dict(good, requirements=[{"id": "R1", "text": "x", "source_ids": ["S2"]}])
    assert any("S2" in e for e in prd_evals.check_synthesis(bad, GOOD_RESEARCH))

GOOD_PRD = ("# T\n\n## Problem\np (S1)\n\n## Goals\ng\n\n## Non-goals\nn\n\n## Requirements\n- R1 (S1)\n\n"
            "## Acceptance criteria\n- recall >= 95% on the validation set\n\n## Open questions\n- q\n\n## Basis\n- S1: metaql q\n")

def test_prd_md_headings_citations_testable_criteria():
    assert prd_evals.check_prd_md(GOOD_PRD, GOOD_RESEARCH) == []
    assert any("S7" in e for e in prd_evals.check_prd_md(GOOD_PRD.replace("(S1)", "(S7)"), GOOD_RESEARCH))
    woolly = GOOD_PRD.replace("- recall >= 95% on the validation set", "- works well for users")
    assert any("testable" in e.lower() for e in prd_evals.check_prd_md(woolly, GOOD_RESEARCH))
    assert any("Non-goals" in e for e in prd_evals.check_prd_md(GOOD_PRD.replace("## Non-goals\nn\n\n", ""), GOOD_RESEARCH))
    assert any("TODO" in e for e in prd_evals.check_prd_md(GOOD_PRD + "\nTODO x\n", GOOD_RESEARCH))
```

- [ ] **Step 2: Implement** (shape; full checks per the stage contracts above)

```python
#!/usr/bin/env python3
"""Tier-A deterministic evals for PRD stage artifacts (spec section 4).
Pure functions: dict/text in, error strings out. prd_flow.py records the
verdicts; nothing here writes files. Design reference: dwh-suite prd_evals."""
import re

SID = re.compile(r"S\d+$")
CITE = re.compile(r"\(S(\d+)\)")
TESTABLE = re.compile(r"\d|%|>=|<=|\bpass\b|\bfail\b")
PRD_HEADINGS = ["## Problem", "## Goals", "## Non-goals", "## Requirements",
                "## Acceptance criteria", "## Open questions", "## Basis"]


def _need(d, key, typ, errs, where):
    v = d.get(key)
    if not isinstance(v, typ) or (typ in (str, list) and not v and key not in OPTIONAL_EMPTY):
        errs.append(f"{where}: '{key}' missing/empty/wrong type")
    return v

OPTIONAL_EMPTY = {"scope_out", "data_targets", "code_targets", "coverage_gaps", "gaps", "open_questions"}


def check_intake(d: dict) -> list:
    errs = []
    for k, t in (("run_id", str), ("title", str), ("pm", str), ("goal", str),
                 ("scope_in", list), ("scope_out", list), ("data_targets", list), ("code_targets", list)):
        _need(d, k, t, errs, "intake")
    if not isinstance(d.get("pii_touch"), bool):
        errs.append("intake: 'pii_touch' must be a boolean")
    return errs


def _source_ids(research: dict) -> set:
    return {s.get("id") for s in research.get("sources") or [] if isinstance(s, dict)}


def check_research(d: dict) -> list:
    errs = []
    srcs = d.get("sources") or []
    if not srcs:
        errs.append("research: no sources — grounding contract requires at least one typed source")
    ids = []
    for s in srcs:
        sid = s.get("id", "")
        if not SID.match(sid):
            errs.append(f"research: source id {sid!r} must match S<n>")
        ids.append(sid)
        for k in ("kind", "ref", "date", "summary"):
            if not s.get(k):
                errs.append(f"research: source {sid} missing '{k}'")
    if len(ids) != len(set(ids)):
        errs.append("research: duplicate source ids")
    known = set(ids)
    for f in d.get("findings") or []:
        for sid in f.get("source_ids") or []:
            if sid not in known:
                errs.append(f"research: finding cites unknown source {sid}")
    return errs


def check_synthesis(d: dict, research: dict) -> list:
    errs = []
    reqs = d.get("requirements") or []
    if not reqs:
        errs.append("synthesis: no requirements")
    known = _source_ids(research)
    for r in reqs:
        if not re.match(r"R\d+$", r.get("id", "")):
            errs.append(f"synthesis: requirement id {r.get('id')!r} must match R<n>")
        if not r.get("text"):
            errs.append(f"synthesis: requirement {r.get('id')} has no text")
        for sid in r.get("source_ids") or []:
            if sid not in known:
                errs.append(f"synthesis: requirement {r.get('id')} cites unknown source {sid}")
    return errs


def check_prd_md(text: str, research: dict) -> list:
    errs = []
    for h in PRD_HEADINGS:
        if h not in text:
            errs.append(f"prd: missing '{h}' section")
    known = {s.lstrip("S") for s in _source_ids(research)}
    for n in set(CITE.findall(text)):
        if n not in known:
            errs.append(f"prd: citation (S{n}) has no matching research source")
    if "## Acceptance criteria" in text:
        body = text.split("## Acceptance criteria", 1)[1].split("\n## ", 1)[0]
        for line in body.splitlines():
            if line.startswith("- ") and not TESTABLE.search(line):
                errs.append(f"prd: acceptance criterion not testable (needs a number/threshold/pass-fail): {line[:60]!r}")
    for marker in ("TBD", "TODO"):
        if marker in text:
            errs.append(f"prd: contains '{marker}'")
    return errs
```

- [ ] **Step 3: Run tests → green; full suite; commit** `feat(prd): Tier-A deterministic evals for PRD stage artifacts`

---

### Task 3: `engine/prd_flow.py` — marker, stamp chain, gates (no claude yet)

**Files:** Create `engine/prd_flow.py`, `tests/test_prd_flow.py`.

**Interfaces:**
- Produces: CLI `prd_flow.py <cmd> <target> <run_id> [args]` with cmds `new <title>`, `status` (prints JSON of marker), `record <stage>` (internal but CLI-exposed for tests), `approve-scope`, `approve-prd`, `revise <stage>`. Python API: `load_marker(target, run)`, `record_stage(target, run, stage) -> dict` (computes sha over the artifact file, chains prev_sha, runs the stage's Tier-A eval, writes marker; refuses out-of-order stages and missing gates), `STAGES = ["intake", "research", "synthesis", "draft", "review"]`, `ARTIFACTS = {"intake": "01-intake.json", "research": "02-research.json", "synthesis": "03-synthesis.json", "draft": "04-prd.md", "review": "05-review.json"}`.
- Gate rules encoded in `next_action(marker) -> str`: after `intake` recorded → `awaiting-scope-approval` until `approvals.scope` set; after `review` recorded → `awaiting-signoff`; `approve-prd` → `done`.
- `revise <stage>`: drops that stage's stamp and every later stage's, resets approvals that follow it, status back to that stage.
- Tamper detection: `record_stage` stores sha; `verify(target, run) -> list[str]` recomputes all shas and reports mismatches (dashboard shows them).

- [ ] **Step 1: Failing tests** (core behaviors)

```python
# tests/test_prd_flow.py
import json, sys
from pathlib import Path
ORBIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORBIT / "engine"))
import prd_flow  # noqa: E402

GOOD_INTAKE = {"run_id": "r1", "title": "t", "pm": "p", "goal": "g", "scope_in": ["a"],
               "scope_out": [], "data_targets": [], "code_targets": [], "pii_touch": False}

def _ws(tmp_path):
    (tmp_path / "research").mkdir()
    return tmp_path

def test_new_creates_marker_and_dir(tmp_path):
    ws = _ws(tmp_path)
    prd_flow.new(str(ws), "r1", "My PRD")
    m = prd_flow.load_marker(str(ws), "r1")
    assert m["status"] == "intake" and m["title"] == "My PRD"

def test_record_intake_stamps_and_gates(tmp_path):
    ws = _ws(tmp_path); prd_flow.new(str(ws), "r1", "T")
    (ws / "research" / "r1" / "01-intake.json").write_text(json.dumps(GOOD_INTAKE))
    m = prd_flow.record_stage(str(ws), "r1", "intake")
    assert m["stages"]["intake"]["evals"]["ok"] is True
    assert m["stages"]["intake"]["sha"] and m["stages"]["intake"]["prev_sha"] is None
    assert m["status"] == "awaiting-scope-approval"

def test_research_refused_before_scope_approval(tmp_path):
    ws = _ws(tmp_path); prd_flow.new(str(ws), "r1", "T")
    (ws / "research" / "r1" / "01-intake.json").write_text(json.dumps(GOOD_INTAKE))
    prd_flow.record_stage(str(ws), "r1", "intake")
    import pytest
    with pytest.raises(prd_flow.GateError):
        prd_flow.record_stage(str(ws), "r1", "research")
    prd_flow.approve_scope(str(ws), "r1")
    assert prd_flow.load_marker(str(ws), "r1")["status"] == "research"

def test_stamp_chain_and_tamper_detection(tmp_path):
    ws = _ws(tmp_path); prd_flow.new(str(ws), "r1", "T")
    f = ws / "research" / "r1" / "01-intake.json"
    f.write_text(json.dumps(GOOD_INTAKE))
    prd_flow.record_stage(str(ws), "r1", "intake")
    f.write_text(json.dumps(dict(GOOD_INTAKE, goal="forged")))
    assert any("intake" in e for e in prd_flow.verify(str(ws), "r1"))

def test_failed_evals_still_stamp_but_block_advance(tmp_path):
    ws = _ws(tmp_path); prd_flow.new(str(ws), "r1", "T")
    bad = dict(GOOD_INTAKE, scope_in=[])
    (ws / "research" / "r1" / "01-intake.json").write_text(json.dumps(bad))
    m = prd_flow.record_stage(str(ws), "r1", "intake")
    assert m["stages"]["intake"]["evals"]["ok"] is False
    assert m["status"] == "intake"   # not advanced

def test_revise_cascades(tmp_path):
    ws = _ws(tmp_path); prd_flow.new(str(ws), "r1", "T")
    (ws / "research" / "r1" / "01-intake.json").write_text(json.dumps(GOOD_INTAKE))
    prd_flow.record_stage(str(ws), "r1", "intake"); prd_flow.approve_scope(str(ws), "r1")
    (ws / "research" / "r1" / "02-research.json").write_text(json.dumps(
        {"run_id": "r1", "sources": [{"id": "S1", "kind": "doc", "ref": "x", "date": "d", "summary": "s"}],
         "findings": [], "coverage_gaps": []}))
    prd_flow.record_stage(str(ws), "r1", "research")
    prd_flow.revise(str(ws), "r1", "research")
    m = prd_flow.load_marker(str(ws), "r1")
    assert "research" not in m["stages"] and m["status"] == "research"
    assert "intake" in m["stages"]
```

- [ ] **Step 2: Implement.** Marker I/O is atomic (mkstemp+replace). `record_stage` rules: stage must be the marker's current `status` (or the stage that `status` names); artifact file must exist; sha = sha256 of file bytes; `prev_sha` = previous stage's sha (None for intake); evals dispatched per stage (`draft` and `review` need the research dict loaded for citation checks; `review` evals: `05-review.json` has `verdicts` list and boolean `ok`); on evals ok → advance status (`intake`→`awaiting-scope-approval`, `research`→`synthesis`, `synthesis`→`draft`, `draft`→`review`, `review`→`awaiting-signoff`); on evals fail → stamp recorded with `ok: false`, status unchanged. `GateError(RuntimeError)` for order/gate violations. `approve_scope`/`approve_prd` set `approvals` and advance (`awaiting-scope-approval`→`research`, `awaiting-signoff`→`done`).

- [ ] **Step 3: Run tests → green; full suite; commit** `feat(prd): prd_flow controller — marker, stamp chain, gates, revise cascade`

---

### Task 4: prd_flow stage execution (headless claude) + `--chain`

**Files:** Modify `engine/prd_flow.py`; extend `tests/test_prd_flow.py`.

**Interfaces:**
- Produces: `run_stage(target, run, stage, chain=False)` — builds the claude command `[claude, --print, f"/orbit-prd-{skill} {run}", --permission-mode, acceptEdits, --settings <mcp.write_settings(target)>, --model, <cfg prd.model or claude-opus-4-8>, --add-dir, <ORBIT_HOME>]` plus `--mcp-config <mcp.write_build(target)> --strict-mcp-config` when the profile declares `mcp:` (reuse `engine/mcp.py` imports); skill name per stage: intake→`orbit-prd-intake`, research→`orbit-prd-research`, synthesis→`orbit-prd-synthesis`, draft→`orbit-prd-writer`, review→`orbit-prd-review`. Honors `ORBIT_PRD_CMD` (shlex-split override; stage/run/target exported as `PRD_STAGE`/`PRD_RUN`/`PRD_TARGET` env for the fake). cwd = target. Timeout from cfg `prd.timeout_seconds` default 3600. After the subprocess exits 0, calls `record_stage`; with `chain=True`, keeps running successive stages while no gate blocks and evals pass (so approve-scope → one chained detached run completes research→synthesis→draft→review).
- CLI grows `run <target> <run> <stage> [--chain]`.

- [ ] **Step 1: Failing tests.** Use a fake `ORBIT_PRD_CMD` pointing at a tiny python script (written to tmp_path by the test) that reads `PRD_STAGE`/`PRD_RUN` and writes a valid artifact for that stage into `research/<run>/` (valid per Task 2 contracts; draft writes GOOD_PRD-style md; review writes `{"verdicts": [], "ok": true}` and appends `## Review audit trail\n- none\n` to 04-prd.md). Tests: (a) `run_stage(..., "intake")` invokes the fake and records the stamp; (b) after `approve_scope`, `run_stage(..., "research", chain=True)` drives the marker to `awaiting-signoff` in one call; (c) a fake that writes an eval-failing artifact stops the chain with status unchanged and `evals.ok is False`.

- [ ] **Step 2: Implement; run tests → green; full suite; commit** `feat(prd): headless stage execution with mcp wiring and chain mode`

---

### Task 5: Stage skills + grounding track

**Files:** Create `skills/orbit-prd-intake.md`, `skills/orbit-prd-research.md`, `skills/orbit-prd-synthesis.md`, `skills/orbit-prd-writer.md`, `skills/orbit-prd-review.md`, `tracks/prd-grounding.md`. Extend `tests/test_init_product.py` with one test asserting the five skills land in a `--product` scaffold's `.claude/commands/`.

Each skill follows the repo's frontmatter (`description`, `argument-hint: "<run-id>"`, `allowed-tools`, `model: opus`; research also gets Bash for engine-side reads). Shared body rules (state in each, tuned per stage): read `tracks/prd-grounding.md` (target-first, engine fallback) and the prior stage artifacts under `research/$ARGUMENTS/`; write ONLY your stage's artifact file per the exact contract keys (copy the contract block from this plan's "Stage artifact contracts" into each skill so it is self-contained); never mark yourself done — the controller stamps; abstain over guess: unknowns go to `coverage_gaps`/`open_questions`, never invented; stay in your lane (intake does not design; research does not invent requirements — every finding cites source_ids; synthesis must advance beyond research, not paraphrase; writer adds no fact absent upstream and cites `(S<n>)` inline; acceptance criteria carry a number/threshold/pass-fail). Research skill specifically: ground against real systems via the available MCP tools (MetaQL discovery order; Jira/Confluence reads when available; repo reads for code_targets), one typed source entry per tool result used. Review skill: two lenses (CEO: scope challenge + completeness 0-10; feasibility: dependencies, rollout, instrumentation), auto-decide with every decision logged into the `## Review audit trail` section it appends to `04-prd.md`, plus `05-review.json {verdicts: [{lens, finding, decision, rationale}...], ok: <false only for contradictions the PM must resolve>}`. `tracks/prd-grounding.md`: the grounding contract distilled (no fact without a source, cite inline, abstain over guess, stay in your lane, controller owns the verdict, pii_touch defers compliance to a human). Credit dwh-suite and gstack (autoplan) as design references in the track's footer.

- [ ] Write files → test green → full suite → commit `feat(prd): five stage skills and the grounding contract track`

---

### Task 6: Secrets file — run.sh sourcing, template, doctor

**Files:** Modify `engine/run.sh` (after the config shellenv eval near the top), `install/init.py` (product branch), `engine/doctor.py`. Create `tests/test_secrets.py`.

**Interfaces:**
- run.sh sources `$AP_HOME/.secrets.env` when present:

```bash
# Secrets (spec decision 10): PM tokens live in $AP_HOME/.secrets.env (0600,
# gitignored). Sourced here so interactive runs and launchd services see the
# same environment.
if [ -f "$AP_HOME/.secrets.env" ]; then
  set -a; . "$AP_HOME/.secrets.env"; set +a
fi
```

- init.py `--product`: append `.secrets.env` to the `.autopilot/.gitignore` it writes, and write `.autopilot/.secrets.env.example` (`# METAQL_TOKEN=...` lines, comment explaining the dashboard Settings > Secrets panel writes the real file).
- doctor: `_secrets_env(target) -> dict` parsing simple `KEY=VALUE` lines from `.autopilot/.secrets.env` (ignore comments/blank); the token_env warning fires only when the var is in neither `os.environ` nor the secrets file; warn separately if the secrets file exists with mode looser than 0600.

- [ ] **Step 1: Failing tests**: (a) doctor stays quiet when METAQL_TOKEN is only in `.secrets.env`; (b) doctor warns on loose permissions (chmod 0644); (c) product scaffold's `.gitignore` covers `.secrets.env` and the example file exists; (d) `bash -n` both bashes.
- [ ] **Step 2: Implement → green; full suite; commit** `feat(secrets): .autopilot/.secrets.env sourced by the loop, scaffolded, doctor-aware`

---

### Task 7: Dashboard — `/api/secrets` + Settings secrets card

**Files:** Modify `engine/command_center.py`, `engine/cc_shell.html`. Create `tests/test_prd_dashboard.py` (holds all M2 dashboard tests; use the standard fresh-import `cc` fixture).

**Interfaces:**
- `GET /api/secrets` → JSON `{"secrets": [{"name": "METAQL_TOKEN", "preview": "sk...xy"}]}` (preview = first 2 + "..." + last 2 of the value, or "set" when shorter than 6). GET is host-checked like other read routes but MUST NOT return full values.
- `POST /api/secrets` (JSON body, same pattern as `/qa-services-*`): `{"set": {"NAME": "value"}, "delete": ["NAME"]}` → `do_save_secrets(payload) -> str`; validates names against `^[A-Z][A-Z0-9_]*$`, rewrites `.autopilot/.secrets.env` atomically with `os.open(..., 0o600)`, returns "Saved N secret(s)". Values never logged.
- cc_shell.html: a `setcard` "Secrets" in the Settings view: masked list + name/value inputs + Add and per-row Delete buttons wired via `postJSON('/api/secrets', ...)`; note text "used for MCP tokens; the loop sources this file". Reference both route literals (parity test).

- [ ] Tests: save→file mode 0600 and content; GET masks; bad name rejected; delete removes. Then implement, shell parity + full suite green, commit `feat(dashboard): secrets panel writing .autopilot/.secrets.env`

---

### Task 8: Dashboard — mcp card in config editor + insight quick-add

**Files:** Modify `engine/command_center.py` (`config_payload` meta + `_validate_config` passthrough for `mcp`), `engine/cc_shell.html` (`loadSettings`/`saveSettings` + board button + modal5 prefill). Extend `tests/test_prd_dashboard.py`.

**Interfaces:**
- Config editor: `mcp` joins the JSON-textarea card family exactly like `gates`/`qa` (`loadSettings` renders `JSON.stringify(cfg.mcp || {}, null, 2)`; `saveSettings` parses and includes it). Server: `_validate_config` accepts an `mcp` dict (deep validation stays in `config.py validate`, which already covers `mcp:` and runs post-write with rollback).
- Insight quick-add: a "New insight" button in the board header that opens the existing task modal pre-filled: category `insight` (and category select gains `insight` automatically from `KNOWN_CATEGORIES` after Task 1), acceptance criteria template line `insights/ doc passes insight_lint (Answer + Basis, every number cited)`, `queue=1` default on. Pure JS prefill — reuses `/task-new`; no new route.

- [ ] Tests: POST `/api/config` with an `mcp` block persists and survives `config.py validate`; POST with an invalid mcp block (http w/o url) rolls back; `/task-new` with `category=insight&queue=1` lands `status: queued, autopilot: allow` in backlog.yaml. Implement, parity + suite green, commit `feat(dashboard): mcp config card and insight quick-add`

---

### Task 9: Dashboard — PRD pipeline view

**Files:** Modify `engine/command_center.py`, `engine/cc_shell.html`. Extend `tests/test_prd_dashboard.py`.

**Interfaces:**
- `prd_pipeline_state() -> list` helper (called from `build_state`, key `prd`, present only when `<repo>/research/` exists — the absent-unless-configured discipline): one entry per `research/*/prd-run.json`: `{run_id, title, status, stages: [{name, ok, stamped_at}], tamper: [..from prd_flow.verify..], next_actions: [..legal actions..]}`. Import prd_flow the way the module imports backlog_lint (path-appended import).
- `POST /prd-action` (form: `run`, `action`): legality mirrored dashboard-side like `_EPIC_ACTIONS`; actions: `new` (also takes `title`; calls `prd_flow.new` synchronously), `approve-scope`, `approve-prd` (synchronous), `run-intake`, `run-research-chain` (detached `Popen([python3, prd_flow.py, run, REPO, run, <stage>, --chain], start_new_session=True)` logging to `$AP_STATE/logs/prd-<run>-<stage>.log` — the epic pattern), `revise-<stage>`.
- `GET /prd-artifact?run=<r>&file=<name>`: text/plain, confined: `file` must be one of the six known artifact names, `run` must match `^[a-z0-9][a-z0-9-]*$`, resolved path must stay under `<repo>/research/` (realpath check).
- cc_shell.html: `renderPrd()` band in the Deep space view under epics (`#prdBand` div next to `#epicsBand`): per run — title, status badge, stage dots (green ok / red failed / grey pending), buttons from `next_actions` (confirm + `post('/prd-action', {run, action})`), artifact links via `openText('/prd-artifact?...')`, tamper warning line when non-empty. "New PRD" button prompts for run id + title. Add `prd` to `sig()`'s key list and `renderPrd()` to `renderViews()`.

- [ ] Tests: state empty without research/ (key absent); with a seeded marker the entry shape is right; `/prd-action approve-scope` flips the marker; `run-research-chain` spawns Popen with `--chain` argv + `start_new_session` (monkeypatched); `/prd-artifact` refuses `file=../../etc/passwd` and unknown names. Implement, parity + `sig()` + suite green, commit `feat(dashboard): PRD pipeline view with gates, stage runs, and artifact viewer`

---

### Task 10: E2E smoke + docs

**Files:** Modify `config/schema.yaml` (document the optional `prd:` block: `model`, `timeout_seconds`), `README.md` (one paragraph: PRD pipeline exists, dashboard-driven, spec pointer). No placeholder code — smoke is commands.

- [ ] **Smoke** (scratch dir under the session scratchpad, not /tmp): git init; `install/init.py <dir> <orbit-home> --product`; write a fake `ORBIT_PRD_CMD` script that emits valid artifacts per stage; then `prd_flow.py new/run intake/approve-scope/run research --chain/approve-prd`; assert marker `done`, `verify` clean; tamper `02-research.json` and assert `verify` reports it. Then start command_center against the workspace (the `_start_cc_server` pattern from tests/test_command_center_generate.py) and GET `/api/state` — assert the `prd` key shape. Clean up scratch.
- [ ] Full suite; commit `docs: prd block reference and README pointer for the PRD pipeline`

---

## Self-review notes

- Spec coverage: section 4 fully (stamps, controller-owns-verdict, Tier-A, gates, grounding contract, revise cascade; Tier-B arrives as the `review` stage's audit-trail pass); decision 10 fully (Task 6-7); section 12's M2 slice (Tasks 7-9). The loop-task deviation is ruled in Global Constraints.
- Type consistency: `record_stage/load_marker/verify/new/approve_scope/approve_prd/run_stage` names used identically in Tasks 3, 4, 9, 10; artifact filenames come from one `ARTIFACTS` dict.
- Known integration risks for implementers: cc_shell parity test needs every new POST literal in the HTML; `sig()` needs the `prd` key; command_center imports prd_flow via the engine-path import used for backlog_lint (`:45`); config editor saves lose YAML comments (pre-existing, accepted).
- Line anchors (command_center routes :3695-3824, config editor :2927-3057, epic pattern :2780-2809, build_state :3270, cc_shell VIEWS :902 / sig :2659 / renderViews :2664) are from the 2026-08-21 exploration; re-locate by content.
