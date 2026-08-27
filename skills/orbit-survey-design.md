---
description: Methodology for designing a grounded survey instrument - a goal turned into falsifiable hypotheses (each naming the evidence that would disprove it) and a bias-audited questionnaire. No data access; this skill only designs the instrument, it never touches responses. Invoked by the orchestrator to brief the analyst maker, with the survey slug carried in $ARGUMENTS.
argument-hint: "<survey-name-slug> [goal]"
allowed-tools: Read, Grep, Glob, Write, Edit
model: opus
---

You are briefing a survey_design task (category `survey_design`). Distill
the following method into the maker brief.

## Method
1. **Phase 0 - load context silently**: `CLAUDE.md`, `knowledge/`, and any
   existing `surveys/<name>/` artifacts (do not silently overwrite a prior
   design - if one exists, say so and treat this as a revision). Resolve
   `<name>`: the first token of `$ARGUMENTS` is the slug; when the task
   instead arrives via the cycle brief, take the slug from the task's
   context. Do not narrate.
2. **Restate the goal** as a specific research question - who it is about,
   what decision it feeds, by when the answer is needed. If the task's goal
   is vague, sharpen it to the most conservative reading and say so in the
   doc.
3. **Generate 3-6 hypotheses about what the survey will find.** Challenge,
   don't validate: every hypothesis MUST name the concrete evidence that
   would FALSIFY it - a specific question's response pattern, an open-text
   theme's absence, a cross-tab against an analytics cohort that comes out flat.
   "Users want X" with no falsifying signal named is not a hypothesis;
   rewrite it until it is.
4. **Draft the questionnaire** in sections tied to the hypotheses:
   - Assign an explicit type per question (single-select, multi-select,
     Likert-5, NPS, open-text, ...). Prefer a closed type wherever a
     hypothesis can be tested numerically; reserve open-text for what a
     checkbox cannot capture (the respondent's own words on "why").
   - For every section, note the concrete bias risk (leading wording, order
     effects, social-desirability, sampling bias in who sees this survey)
     and the specific mitigation taken (randomized option order, neutral
     phrasing, a screener question, an explicit non-response option). A bias
     note that just says "watch for bias" is not a mitigation; name the
     actual risk and the actual fix.
5. **Write** `surveys/<name>/questionnaire.md`: one `#` title, then each
   section as a `##` heading, each question numbered with its type in
   brackets and its section's bias note stated once per section.
6. **Write** `surveys/<name>/meta.json`:
   ```json
   {"name": "<name>", "goal": "<restated goal>",
    "hypotheses": ["<hypothesis text, ending in its falsifying evidence>", ...],
    "created": "<UTC ts>"}
   ```
7. **Definition of done**: both files exist under `surveys/<name>/`; every
   hypothesis in `meta.json` names its own falsifying evidence in its text;
   no `TBD`/`TODO` anywhere in either file.

Design references: dwh-suite's grounding contract (challenge before
validate) and `orbit-hypothesis`'s falsifiable-claim discipline, adapted
from query triangulation to instrument design.
