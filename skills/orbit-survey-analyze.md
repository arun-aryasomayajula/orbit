---
description: Methodology for analyzing survey responses under the grounding contract - code open-text into themes with counts and verbatim quotes, score each design-time hypothesis to a verdict, cross-tab against MetaQL cohorts when reachable, abstain loudly when no raw responses exist. Invoked by the orchestrator to brief the analyst maker, with the survey slug carried in $ARGUMENTS.
argument-hint: "<survey-name-slug>"
allowed-tools: Read, Grep, Glob, Write, Edit, mcp__*
model: opus
---

You are briefing a survey_analysis task (category `survey_analysis`).
Distill the following method into the maker brief.

## Method
1. **Phase 0 - load context silently**: `CLAUDE.md`, `knowledge/`, and
   `surveys/<name>/questionnaire.md` + `meta.json` for the hypotheses this
   analysis must score. Resolve `<name>` the same way as
   `orbit-survey-design`: first token of `$ARGUMENTS`, or the task's slug
   when this arrives via the cycle brief. Do not narrate.
2. **Locate raw responses**: `surveys/<name>/raw/*.csv`. If none exist, do
   NOT guess or fabricate a single number - abstain over guess. The report
   still gets written, with a `## Coverage gaps` section stating plainly that
   no raw responses were found and that no hypothesis could be scored.
3. **When raw files exist**, read every one:
   - Code open-text responses into themes: group similar free-text answers,
     count respondents per theme, and pull at least 3 verbatim quotes total
     across the report as `> "..."` lines - the respondent's exact words, no
     paraphrasing the claim itself.
   - Quantify closed-ended answers: counts and percentages per option, tied
     back to the hypothesis each question was designed to test.
4. **Cross-tab against MetaQL cohorts when mcp tools are reachable**:
   discover verticals -> projects -> use cases -> explores -> fields (same
   discipline as `orbit-insight` - never guess a column), then cross the
   respondent segments against a live cohort (e.g. paying vs. free, region)
   if the survey collected a linking field. State explicitly in Basis
   whether this cross-tab happened, and if not, exactly why (no mcp tools
   reachable, or no linking field collected) - never silently skip it.
5. **Score every hypothesis from `meta.json`** to `VALIDATED` / `NEGATED` /
   `INCONCLUSIVE`, citing whether the falsifying evidence named at design
   time actually materialized in the data. `INCONCLUSIVE` records exactly
   what is missing, never a guess dressed as a finding.
6. **Write** `surveys/<name>/report.md`: `# <title>`, `## Answer` (each
   hypothesis's verdict first, then the theme counts and quantified
   answers), `## Basis` with typed sources - every raw file cited as
   `{kind: doc, ref: raw/<file>.csv}`, every MetaQL pull as its own source,
   the questionnaire/meta as the design source. Every inline number cites
   `(S<n>)`. Include either at least 3 verbatim `> "..."` quote lines total
   or a `## Coverage gaps` section - never both absent.
7. **Definition of done**: `python3 $ORBIT_HOME/engine/insight_lint.py .`
   exits 0 on the worktree, and every hypothesis from `meta.json` has a
   verdict in the doc.

Design references: the PREMIUM PM-agent methodology (theme coding, verbatim
evidence) and dwh-suite's grounding contract (abstain over guess, gaps
recorded not papered over).
