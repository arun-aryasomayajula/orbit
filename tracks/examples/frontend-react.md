# Track: frontend-react
Loaded when: the task touches `frontend/src`. CLAUDE.md's React guardrails
(useRef for non-render values, effect-synced derived state) already apply —
this track holds what CLAUDE.md does NOT.

## Hard rules (repo-specific — violations have shipped bugs or burned cycles)
- This is CRA (react-scripts), NOT Vite: no `import.meta`, tests run through
  react-scripts (see the testing track), builds via `npm run build`.
- All FunnelHub CSS classes use the `fh-` prefix; styles live under
  `frontend/src/styles/funnelhub/`. API responses unwrap `{ result: data }`.
- Every axios call passes `{ signal, timeout }` — a hung request with neither
  never resolves and wedges the UI.
- New Druid/FunnelHub error call sites copy the WHOLE `extractErrorMessage`
  helper (502 envelope is `{error, message, upstream_status}`) — partial
  copies mis-render upstream failures.
- `SessionContext.js` (~1800 lines) is the single source of truth: 2s-debounce
  auto-save, `pickNewerSession()` picks the newer of localStorage vs PG (PG
  wins ties). ANY session change must be re-tested for multi-tab behavior.
- Routing is React Router v7; FunnelHub pages use flat `/funnelhub/*` paths
  via the `useWorkspacePath` hook — don't hand-build workspace URLs.
- Console noise that is NOT a bug: AG Charts Enterprise trial-license spam
  (renders anyway); "no data" reports are often a COLLAPSED `<details>`
  disclosure, not missing data.

## Playbook (distilled patterns that fit this repo)
- Prefer lifting/colocating state over adding a state library; this codebase
  uses Contexts (Session, Workspace) + hooks (`useFunnelBuilderState.js`).
- Server state ≠ UI state: results/caches flow through `funnelhubApi.js` /
  `api.js`; don't mirror them into component state you must then invalidate.
- Derive during render instead of syncing copies with effects when possible.
- Pattern files: `frontend/src/contexts/SessionContext.js`,
  `frontend/src/funnelhub/useFunnelBuilderState.js`,
  `frontend/src/services/funnelhubApi.js`.

## Learned here (cycle-appended, newest last — one line each, with date)
- (none yet)
