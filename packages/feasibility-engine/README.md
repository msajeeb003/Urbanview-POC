# @urbanview/feasibility-engine

The one shared implementation of the client's feasibility formulas. Pure, deterministic
TypeScript (no I/O, no randomness, no LLM, no runtime dependencies), built as ESM + CJS with
strict types. The Next.js apps use it for live recalculation; the FastAPI backend ships a Python
copy (`backend/core/engine/feasibility.py`) that is held to the same fixtures, so the two can
never drift.

```ts
import { calculate, recalculate, FORMULA_VERSION } from "@urbanview/feasibility-engine";

const result = calculate({
  planning: { plot_area: 959.6, calculation_basis: "urban", far: 3.2, site_coverage_pct: 55 },
  market: {
    market_value_per_m2: { expected: 2450, bounds: { kind: "multiplier", low: 0.86, high: 1.15 } },
    construction_cost: { per_m2: { expected: 860, bounds: { kind: "multiplier", low: 0.86, high: 1.15 } } },
    land_value: { per_m2: { expected: 1350, bounds: { kind: "multiplier", low: 0.86, high: 1.15 } } },
    design_and_documentation_costs: { per_m2: { expected: 90, bounds: { kind: "multiplier", low: 0.86, high: 1.15 } } },
  },
});
result.fields.roi_pct; // { key, unit: "%", range_kind: "range", status: "ok", reason: null, low: -6.53, expected: 25.01, high: 67.17 }

// the visitor edits an assumption: only the dependent figures change
const edited = recalculate(inputs, { saleable_share: 0.8, market_value_per_m2: 2600 });
edited.assumptions_used; // every number actually used, with its source ("market" | "user" | "default")
```

## Formulas (client-owned; `FORMULA_VERSION = "poc-1"`)

| output | formula |
|---|---|
| max_gfa | far × plot_area |
| max_coverage_area | plot_area × site_coverage_pct / 100 |
| saleable_area | max_gfa × saleable_share (default 0.70) |
| construction_costs | max_gfa × construction cost per m², or the total given |
| land_value | plot_area × land value per m², or the total given |
| design_and_documentation_costs | max_gfa × design rate per m², or the total given |
| total_cost | land_value + design_and_documentation_costs + construction_costs |
| market_value (revenue) | saleable_area × market_value_per_m2 |
| potential_profit | market_value − total_cost |
| roi_pct | potential_profit / total_cost × 100 |

`plot_area` is the planned urban parcel area, or the cadastral area as fallback; the caller says
which with `calculation_basis`, and the result echoes it. `selectCalculationBasis({ planned_area,
cadastral_area })` applies that rule to both areas (`{ plot_area, calculation_basis }`); the two
areas, `max_height_m`, `max_floors` and `land_use` may travel in `planning` as validated context
that no formula uses.

## Ranges (`range_derivation = "pessimistic-pairing-v1"`)

Bounds come from the admin assumptions, per market input, as multipliers (`low ≤ 1 ≤ high`) or
absolute values — nothing is hardcoded. Each output's `low` / `high` are its smallest / largest
plausible values within those bounds: cost rows at their own bounds, revenue at the price bounds,
profit low = revenue low − cost high (and high symmetrically), ROI low = profit low / cost high.
The three areas depend on no market input and are `range_kind: "deterministic"`
(low = expected = high). Every range satisfies low ≤ expected ≤ high. The rules are documented
in `src/engine.ts` next to the code.

## Missing data

`null` means "not stated in the plan" (0 is a real 0). Figures that cannot be computed come back
as `status: "cannot_calculate"` with a reason code (`area_unknown`, `far_not_stated`,
`coverage_not_stated`, `requires_gfa`, `no_market_data`, `no_market_data_zone_unknown`,
`total_cost_zero`); the UI owns the texts. Invalid inputs (negative areas, bounds that do not
bracket the value, a saleable share outside (0, 1]) throw `EngineInputError`.

## Fixtures are the contract

`fixtures/feasibility-cases.json` holds input → expected-output cases. Both engines must
reproduce `expected` exactly (deep equality, no tolerance; `expectedOf(result)` drops only the
package version). The file is currently `client_validated: false`: engineering cases derived from
the client's formulas, with the hand arithmetic in each case's `notes`. Replace or extend them
with the client's worked examples and flip the flag once the client confirms the numbers. Never
regenerate the file from the engine.

Rounding: outputs and displayed assumption bounds are rounded to 2 decimals, half away from zero
on the shortest decimal representation of the double (`1.005 → 1.01`), never intermediate
values; `-0` becomes `0`. The Python engine uses the identical rule (`repr` + `ROUND_HALF_UP`).

## The two engines cannot drift

- **Fixtures** (`fixtures/feasibility-cases.json`): both engines, exact to the cent and
  byte-identical as JSON (`test/fixtures.test.ts`, `backend/tests/test_feasibility_shared.py`,
  and `frontend/src/lib/feasibility.test.ts`, which runs them through the package as the public
  map imports it).
- **Dependency snapshot** (`fixtures/assumption-dependencies.json`): for every calculate-case,
  which figures change when one assumption is edited; every other figure must stay
  byte-identical. Both engines are held to it (`test/dependencies.test.ts`, the Python test).
- **Generated inputs** (`backend/tests/test_feasibility_cross_engine.py`): 680 seeded input sets
  over the whole input space, including invalid ones, through the built bundle and the Python
  copy; every JSON text must be identical. It skips with a reason when the bundle is missing or
  older than `src/`.

The Next.js apps depend on the workspace package (`"@urbanview/feasibility-engine": "*"`);
`npm install` at the repo root builds `dist/` (root `prepare` script).

## Develop

```bash
npm install                 # from the repo root (npm workspaces)
npm run -w @urbanview/feasibility-engine check   # typecheck, tests with 100% coverage, ESM+CJS build
```
