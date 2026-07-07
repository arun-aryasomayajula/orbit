You are working in the campaign-manager frontend: React 18 + TypeScript + Vite + Mantine, tests in Vitest (NOT jest). Read AGENTS.md and neighboring components before writing anything.

Task: add a client-side search/filter box to the Funnels list page.

- A text input above the funnels list that filters the already-loaded rows client-side (case-insensitive, matches on funnel name; no new API call). Debounce is optional; empty query shows all rows.
- Follow this repo's conventions exactly: functional component + hooks, Mantine components, the single `index.css` styling approach (no CSS-in-JS/modules), and any shared UI primitives the list page already uses. Match how the page currently manages state.
- Handle the empty state: if the filter matches nothing, show a friendly "no funnels match" message rather than a blank list.
- Add a Vitest + React Testing Library test that renders the list with fake rows, types a query, and asserts only matching rows remain and the empty-state shows for a non-matching query.

Constraints: match existing code style; TypeScript (this is a .tsx codebase); don't break existing tests. Run `npm run typecheck` and the relevant `npm test` and report actual output. Final message: files changed (path:line), how filtering is wired, and the typecheck + test results.
