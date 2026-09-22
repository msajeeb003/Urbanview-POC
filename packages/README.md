# packages/

Shared TypeScript packages (npm workspaces from the root `package.json`; `npm install` at the
repo root).

| Package | Purpose |
|---|---|
| `feasibility-engine/` (`@urbanview/feasibility-engine`) | The one shared feasibility formula engine: the client's formulas with low / expected / high ranges, pure and deterministic, ESM + CJS. Used by the Next.js apps for live recalculation; the backend runs a Python copy (`backend/core/engine/shared.py`) held to the same fixtures. |

`feasibility-engine/fixtures/feasibility-cases.json` is the contract: input → expected output
cases that both engines must reproduce exactly (`packages/feasibility-engine/test` and
`backend/tests/test_feasibility_shared.py`). It is `client_validated: false` until the client
confirms the numbers (P0 week-1 gate 3); extend it with the client's worked examples rather than
regenerating it from an engine. See `feasibility-engine/README.md` for the formulas, the range
derivation and the rounding rule.
