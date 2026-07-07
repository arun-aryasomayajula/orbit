# Track: frontend-react
Loaded when: the task touches the frontend source.
Generic template — see examples/frontend-react.md for a filled-in version; add YOUR repo's facts.

## Hard rules (universal)
- **Match the repo's toolchain and test runner exactly** (CRA vs Vite; Jest vs Vitest) — don't
  swap or "modernize" as a side effect. Read package.json scripts first.
- Every outbound request needs a timeout + abort on unmount (a hung request wedges the UI).
- Use a ref for values that must not re-trigger effects / accumulate across renders; sync derived
  state to changing inputs with an effect rather than a once-captured initial value.
- Handle every interaction state: loading, error, empty, success.

## Playbook (add repo specifics)
- Fill in: CSS convention/prefix, API response wrapper shape, shared UI primitives, state container.

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
