# packages/ — shared formula engine (reserved)

`packages/formula-engine` will hold the **shared TypeScript formula engine** used by the public map,
the admin app and the backend (build plan: "shared TypeScript formula engine"). Until it lands:

- `packages/formula-engine/fixtures/*.json` — client-validated input/output fixtures for the
  feasibility formulas (P0 week-1 gate 3: *formula engine fixtures validated by the client before
  any UI is built on them*). The Python side must pass the same fixtures (parity test) before any
  endpoint returns financial figures.
- `fixtures/feasibility.json` — the feasibility cases for formula version `poc-1`
  (`{"formula_version", "client_validated": false, "cases": [{name, notes, inputs, expected}]}`).
  Each `expected` is the exact shape of the Python engine's `FeasibilityResult.to_dict()` (7
  `fields`, 4 `cost_rows`, `assumptions_used`, `formula_version`); `notes` states the hand
  arithmetic behind the numbers. Cases: full inputs (sample UP 12: area 959.6 m², FAR 3.2,
  coverage 55 %, Centar rates 1350 / 860 / 90 / 2450 €/m², factors 0.86 / 1.15), missing FAR,
  missing coverage, no market data (with and without a known zone), user overrides (saleable
  share 0.8, construction 700, sale 2600), overrides without market data, area 0 (total cost 0 →
  ROI cannot be calculated), area unknown, and the FAR-before-market reason precedence.
  Rounding is applied to outputs only: areas 1 decimal, euros whole, ROI 1 decimal, half away
  from zero on the decimal value. A `null` input means "unknown / not stated"; `0` is a real 0.
- Python engine: `backend/core/engine/feasibility.py` (pure, deterministic, no municipality
  knowledge; emits reason codes, the API layer owns the labels). Parity test:
  `backend/tests/test_feasibility.py` runs every case with a tolerance of 0.5 for euros and 0.05
  for areas / ROI, and additionally asserts the dependency rules and that every range satisfies
  low ≤ expected ≤ high. The TypeScript engine will be held to the same file and tolerances.
- Formulas (client-owned, deterministic, no LLM in the arithmetic):
  - Max GFA = FAR × plot area
  - Max coverage area = site coverage % × plot area
  - Potential profit = (Max GFA × 0.70 × market value per m²) − (land value + design & documentation costs + construction costs)
  - ROI % = potential profit / (land value + design & documentation costs + construction costs) × 100
  - 70% saleable share is a visible, user-editable assumption; construction cost and selling price are also editable.
  - Every financial output is a low / expected / high range: rate-based figures use the market
    row's low / high factors; profit low = revenue low − total cost high (high symmetrically) and
    ROI follows the same pessimistic pairing.
- The engine takes numbers in and gives numbers out. It has no knowledge of any municipality;
  terminology and reference prices come from configuration and reviewed data.
