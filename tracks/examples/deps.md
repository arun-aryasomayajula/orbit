# Track: deps
Loaded when: category `dependencies` — version bumps, lockfile changes, CVE remediation.

## Hard rules
- **A bump is not done until the FULL gate is green** — deps break things far from the
  changed line. Run backend pytest AND frontend tests, report real numbers.
- **No major-version jumps unattended.** Patch/minor within the same major only; a major
  bump (breaking API) is a human call — escalate.
- Don't touch `backend/middleware/auth*`, payment/billing deps, or anything security-
  sensitive without the security track + human review.
- Frontend: this is CRA (react-scripts), NOT Vite — respect the existing toolchain; don't
  "modernize" the build as a side effect of a bump.

## Playbook
- Prefer an override/resolution pinning the transitive dep (the rollup 2.79→2.80 CVE
  pattern: override, don't bump the parent) over a wide upgrade.
- Read the changelog for the exact version delta; check for renamed/removed exports the
  repo uses (grep the import sites).
- One dependency (or one CVE) per commit — keep the diff reviewable.
- If a bump forces a code change to compile, that's a feature/bug task riding along —
  note it in the review packet as risk.

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
