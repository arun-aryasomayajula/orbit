# Orbit PM UX rethink

## Purpose

Orbit has strong product capabilities: grounded insights, PRD workflows,
surveys, watches, publishing, and human gates. The PM experience does not yet
make those capabilities easy to understand. Product mode currently changes
labels on an engineering control surface, so a PM still has to interpret agent
status, repository concepts, queue mechanics, and inconsistent names before
they can act.

This document defines the direction for a product-first Orbit experience. It
keeps the local-first model, human decision gates, and orbit visual identity.

## The problem

The current Mission Control is organized around the autonomous engineering
loop. Product work appears as bands inside that loop:

- A PM can see **Dawn**, **Decisions**, **Studio**, and **Atlas** in the rail,
  then arrive at views that still use names such as **Deep space** and
  **Knowledge**.
- A decision can be in the inbox, on a PRD card, in a calibration card, or in
  the Dawn summary. The place to act depends on which subsystem produced it.
- The first-run flow asks for an absolute filesystem path, then recommends
  intake and a knowledge build before the PM creates a product artifact.
- Status and telemetry expose code churn, branches, loop slots, watchdog
  timing, and raw queue state alongside the PM-facing summary.

The result is a product that is powerful but difficult to explain in a few
minutes.

## Success metrics

Each phase below carries its own exit criteria; these are the program-level targets.

- Time from launcher to a first visible product artifact (an insight task queued or a PRD run created): under 10 minutes, with no terminal use and no JSON editing.
- Zero engineering-only views reachable in product mode without an explicit Diagnostics action: not by keyboard shortcut, not by persisted navigation state, not by stale links.
- 100% of pending human gates (escalations, ship reviews, PRD stage decisions, calibration approvals) appear in the Decisions view, sourced from one server-owned list.
- Zero native browser dialogs (`alert`, `confirm`, `prompt`) anywhere in Mission Control; every confirmation runs through Orbit's own dialog with dialog semantics.
- Explanatory copy (subtitles, section labels, hints) meets WCAG AA contrast; primary actions have a touch target of at least 44px.

## Product principles

### Orbit proposes; people decide

Every automated output must make the remaining human judgment clear. A decision
card needs a recommendation, evidence, expected impact, available actions, and
an honest urgency signal. Orbit must not auto-merge, auto-publish, or silently
turn a proposal into committed product work.

### Product nouns are functional; orbit language is atmospheric

Keep the space visual system: the dark sky, orbmark, starfield, restrained
motion, and short status language such as “Clear skies.” Use familiar product
language to organize work. Theme should add character without becoming a
translation layer.

### Show value before configuration

A new PM should produce a visible product artifact before they need to
understand integrations, folders, queues, or agent settings. Connections and
advanced controls should remain available, but they should not block the first
question or first PRD.

### A PM should never need to hunt for a decision

Every pending approval, sign-off, review, escalation, and publish request
belongs in one first-class Decisions destination. The same item can appear as a
summary on Today, but its action lives in Decisions.

### Diagnostics are optional

Branches, code churn, slot state, QA flows, raw logs, and agent controls are
useful diagnostics. They should not define the primary PM experience. Keep them
behind an explicit Engineering or Diagnostics entry, with product-mode routes
and keyboard navigation protected accordingly.

## Target information architecture

### Today

Answers: **What changed? What needs me?**

Today is the daily landing point after the PM has completed first-run setup. It
contains:

- a short current-state summary;
- decisions waiting for the PM;
- work in flight;
- artifacts that landed since the last visit;
- a compact setup nudge only when something material remains incomplete.

Today should not show raw loop metrics or code-centric statistics.

### Decisions

Answers: **What is blocked on my judgment?**

This is the control surface for human gates. It collects:

- agent escalations;
- PRD stage decisions and sign-off;
- drafts ready for review;
- publish approvals;
- calibration or learning approvals, if they are relevant to product work.

Each card should explain the context and offer a small set of reversible,
well-labeled actions. All confirmations and reason-collecting rejections use
Orbit's own dialog UI; native browser dialogs are banned outright (they also
block automated QA).

Decisions is a server-owned data model, not a UI-side aggregation.
`command_center.build_state` emits one `decisions` list with a uniform shape
(id, source, kind, title, detail, urgency), composed from escalations, ships
awaiting review, PRD stage gates, and calibration candidates.
Today and Decisions both render from that one list, so the two surfaces cannot
drift apart the way the old per-subsystem aggregations did.

### Work

Answers: **What are we learning or making?**

Work is a unified list and reader for PM-owned objects:

- PRDs;
- insights;
- surveys;
- watches;
- experiments or hypotheses when supported.

The PM filters by object type and state instead of scrolling across an unrelated
stack of bands. A selected object opens in a readable document surface, with
evidence and publishing state close to the content.

### Library

Answers: **What can I read, reuse, or share?**

Library contains finished and reference material:

- published PRDs;
- insight and survey reports;
- reusable product knowledge;
- optional technical knowledge for a PM who needs it.

Product documents should be rendered as documents, not exposed as raw text.
The architecture graph is a specialist tool, not the default Library view.

### Setup

Answers: **What is connected, and how does Orbit behave?**

Setup is progressive:

1. product identity and first outcome;
2. data connections;
3. publishing connections;
4. watches and cadence;
5. advanced agent and diagnostic settings.

Structured forms should be the normal experience. JSON is an Advanced
disclosure for users who need capabilities not yet represented by a form.

## First ten minutes

### 1. Create a workspace

Ask for a product name and optional description. Generate a safe local path by
default, with path selection in an Advanced disclosure. The launcher should
explain that Orbit drafts grounded product work and waits for human decisions.

The workspace mode is an explicit marker, never an inference.
`orbit init --product` writes `mode: product` into `.autopilot/config.yaml`, and
`is_product_workspace` reads that marker first.
The directory/gate heuristics survive only as a fallback for workspaces created
before the marker existed; an explicit `mode: engineering` always wins over them.

### 2. Land in Setup

A brand-new workspace should not land on an empty Today page. Setup should
offer one concrete first action:

- ask a product question; or
- start a PRD.

Either path should create a visible item in Work immediately.

### 3. Produce the first artifact

Orbit should show that item as in flight with an understandable status. The PM
should be able to open the draft or report without leaving Mission Control.

### 4. Connect optional services

Data sources and Confluence/Jira publishing should be presented after the first
outcome. A PM can defer them without being blocked.

### 5. Make Today the home view

Once a workspace contains product work, Today becomes the default landing page.
It should make the next action obvious without requiring a guide.

## Interaction and accessibility requirements

These are per-phase requirements, not a final phase.
Every surface a phase touches ships meeting them; Phase 5 is the audit and
sweep of what remains, not the first appearance of accessibility.

Product mode must meet these behavioral requirements:

- Every view has a visible page heading and one consistent name across rail,
  heading, help, empty state, and status copy.
- Product-mode keyboard shortcuts cannot navigate to hidden engineering views.
  Persisted navigation state must be validated against the active mode.
- Refresh polling must not replace a focused action or reset the user’s reading
  position. Prefer targeted updates or preserve focus before rendering.
- Primary actions must provide a touch target of at least 44px.
- Explanatory copy must meet WCAG AA contrast, including subtitles and section
  labels.
- Dialogs need dialog semantics, focus management, focus restore, and explicit
  confirmation copy.
- Live status, toasts, and decision count changes need appropriate accessible
  announcements.

## Delivery order

Ruling on the pre-existing uncommitted product-mode work (the
`applyProductChrome` rename layer): it is folded into Phase 1 and replaced,
not shipped as a bridge.

### Phase 1: Make product mode coherent

Replace the product-mode post-render rename layer with source-owned product
copy. Align page headings, empty states, guide content, keyboard navigation,
persisted view state, and status metrics. Hide diagnostics as complete routes,
not only as rail buttons.

Exit criteria: the server injects the workspace mode into the shell at serve
time; render functions emit mode-correct copy directly (no post-render DOM
patching anywhere); keyboard digits map only to the active mode's views; a
persisted engineering view falls back to Today in product mode; a test asserts
each of these.

### Phase 2: Create Decisions

Make Decisions a real view that aggregates every pending human gate. Keep Today
as a summary and remove duplicated actions from other surfaces.

Exit criteria: `build_state` emits the unified `decisions` list in product
mode; the Decisions view and Today's summary render from that list alone; a
test asserts every gate kind (escalation, review, PRD stage, calibration)
appears in it.

### Phase 3: Rebuild first-run

Use a name-first launcher and a Setup landing flow. Help the PM create a first
question or PRD before asking for integration details.

Exit criteria: the launcher creates a product workspace from a name alone
(path auto-derived, editable under Advanced); a brand-new product workspace
lands on Setup with "ask a question" and "start a PRD" as the two first
actions; integrations are never a blocker for either.

### Phase 4: Recompose Work and Library

Replace the current stacked Studio bands with product-object filters and a
document reader. Keep documents, reports, and reference material in Library.

Exit criteria: Work lists PRDs, insights, surveys, and watches behind type
filters with a reader pane; Library renders finished documents as documents
(not raw text); the architecture graph is reachable but not the default.

### Phase 5: Accessibility and resilience

Improve contrast, heading structure, dialog behavior, target sizes, responsive
layout, and non-destructive polling.

Exit criteria: zero native browser dialogs remain in the shell; modals have
dialog semantics with focus save/restore; polling never replaces a focused
control or resets a reader's scroll position; explanatory copy passes WCAG AA
contrast; primary actions are at least 44px tall.

## Explicit non-goals

- Rewriting Mission Control in React.
- Replacing the orbit visual identity with generic SaaS styling.
- Building full replicas of Jira, Confluence, Linear, Productboard, or an
  analytics platform.
- Weakening human gates to make the experience appear more autonomous.

## Implementation touchpoints

`engine/cc_shell.html` owns the Mission Control composition, navigation,
product-mode rendering, and accessibility behavior. `engine/launcher_shell.html`
owns the first-run create/import experience. `engine/command_center.py` owns
the product-mode state and the state payload that should support the target
views.

Render discipline for the single-file shell: the server substitutes the
workspace mode into the template at serve time (alongside `__BRANCH__` and
`__TOKEN__`), the rail and view headings are authored per mode in one place,
and every view is one named render function that reads state and emits final
copy. Post-render DOM patching layers are banned; that is the failure mode this
document exists to remove. `engine/command_center.py` additionally owns
`is_product_workspace` (mode marker first, heuristics as legacy fallback) and
the `decisions` aggregation. `install/init.py` writes the `mode: product`
marker.
