# Track: deps
Loaded when: category `dependencies` — version bumps, lockfiles, CVE remediation.

## Hard rules (universal)
- **A bump is not done until the FULL gate is green** — deps break things far from the changed line.
- **No major-version jumps unattended** — a breaking major is a human call; escalate.
- Don't touch auth/payment/security-sensitive deps without the security track + human review.
- Respect the existing toolchain — don't change the build system as a side effect of a bump.

## Playbook (add repo specifics)
- Prefer an override pinning the transitive dep over a wide parent upgrade. One dep/CVE per commit.
- Read the changelog for the version delta; grep the repo's import sites for renamed/removed exports.

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
