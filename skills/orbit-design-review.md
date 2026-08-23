---
description: Methodology for auditing one named screen or flow against design rubrics (hierarchy, spacing, consistency, interaction states, accessibility) under the grounding contract - Figma frames via mcp__* when the workspace's mcp: declares figma, else live pages via browse.py fetch; abstains into Coverage gaps with neither. Invoked by the orchestrator to brief the analyst maker.
argument-hint: "<screen-or-flow-name>"
allowed-tools: Read, Grep, Glob, Write, Edit, mcp__*, Bash(python3:*) # sanctioned use: python3 $ORBIT_HOME/engine/browse.py fetch (scrape sources)
model: opus
---

You are briefing a design_review task (category `design_review`). Distill
the following method into the maker brief.

## Method
1. **Phase 0 - load context silently**: `CLAUDE.md`, `knowledge/`, and the
   workspace's `.autopilot/config.yaml` `mcp:` block to see whether a `figma`
   server is declared. Resolve `<name>` the same way as `orbit-survey-design`:
   first token of `$ARGUMENTS`, or the task's slug when this arrives via the
   cycle brief. Do not narrate.
2. **Pick the one visual source the task allows** - never both, never neither
   silently:
   - If the workspace's `mcp:` declares a `figma` server, pull the named
     screen/flow's frames via `mcp__figma__*` (e.g. `get_screenshot`,
     `get_design_context`, `get_metadata`).
   - Else, if the task names a live URL, fetch it with
     `python3 $ORBIT_HOME/engine/browse.py fetch <url>` through Bash - the
     only sanctioned network access in this skill.
   - Else, **abstain**: write the doc anyway, exactly like
     `orbit-survey-analyze` does when no raw responses exist. Basis still
     needs at least one typed source line - cite the Phase-0 reads
     themselves (e.g. `- S1: doc CLAUDE.md` / `- S2: doc knowledge/<note>.md`)
     so the doc never has an empty Basis. Explain the abstention in prose
     inside `## Answer`, and add a `## Coverage gaps` section stating
     plainly that no Figma server is configured and no URL was given, so no
     rubric could be scored. Never guess at a screen you cannot see.
3. **Audit against every rubric** - each is a distinct section in the doc,
   never folded together:
   - **Hierarchy**: one clear focal point, natural eye flow, above-the-fold
     content readable at a glance.
   - **Spacing**: a consistent scale/grid, alignment, rhythm between related
     vs. distinct sections.
   - **Consistency**: the same component styled identically everywhere it
     repeats; typography and color coherent across the screen/flow.
   - **Interaction states**: evaluate and name all four lifecycle states -
     **loading**, **error**, **empty**, **success** - explicitly, one by one,
     even when a state could not be observed (say so rather than skip it).
     Also check hover/focus/active/disabled where visible.
   - **Accessibility**: contrast (4.5:1 body text, 3:1 large text/UI),
     focus-visible rings, touch targets >= 44px, no color-only encoding.
     **Every accessibility finding is CRITICAL severity, unconditionally** -
     never downgraded because the visual impact looks small.
4. **Bucket every finding by severity** - CRITICAL, MAJOR, MINOR - worst
   first. Each finding cites the source it was seen in `(S<n>)`.
5. **Write** `insights/<task-id>-design-<slug>.md`: `# title`, `## Answer`
   with severity-bucketed findings (CRITICAL first), `## Basis` with typed
   sources - a Figma frame as `{kind: figma, ref: <frame url>, date, summary}`,
   a scraped page as `{kind: scrape, ref: <url>, date, summary}`, or, on the
   true-abstain path, the Phase-0 doc reads as `{kind: doc, ref: <path>, date,
   summary}`. `## Basis` is NEVER empty - even a full abstention cites the
   context docs actually read. Every inline claim cites `(S<n>)`.
6. **Basis lines MUST start `- S<n>: `** at column 0 (no leading spaces, no
   bullet nesting) - the lint gate matches this exact shape. Literally:

   ```
   ## Basis
   - S1: figma frame https://figma.com/file/.../node-id=1:2, 2026-08-22
   - S2: scrape https://staging.example.com/pilot-screen, 2026-08-22
   ```

   or, on the true-abstain path (no figma server, no URL):

   ```
   ## Basis
   - S1: doc CLAUDE.md, 2026-08-22
   - S2: doc knowledge/design-notes.md, 2026-08-22
   ```
7. **Definition of done**: `python3 $ORBIT_HOME/engine/insight_lint.py .`
   exits 0 on the worktree - which requires `## Basis` to carry at least one
   typed source line even when abstaining - and, whenever a visual source was
   actually reachable, the doc names all four interaction states (loading,
   error, empty, success) even where one was not observed.

Design references: the PREMIUM PM-agent methodology (severity-bucketed,
evidence-cited findings) and dwh-suite's grounding contract (abstain over
guess, gaps recorded not papered over; `orbit-survey-analyze`'s pattern of
still citing sources on a no-data abstention). The rubric areas - hierarchy,
spacing, consistency, interaction states, accessibility - are credited to
gstack's `/design-review` skill
(`~/.claude/skills/gstack/design-review/SKILL.md`), scoped down here to a
single named screen/flow with no fix loop. Escalating every accessibility
finding to CRITICAL severity is Orbit's own rule, not gstack's (gstack grades
accessibility on the same A-F scale as its other categories) - it comes from
the user's design principles (accessibility is not optional).
