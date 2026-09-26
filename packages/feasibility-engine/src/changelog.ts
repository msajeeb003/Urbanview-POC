import { ENGINE_VERSION, FORMULA_VERSION } from "./version.js";

/**
 * Release notes of the engine, newest first: what each `ENGINE_VERSION` changed and under which
 * `FORMULA_VERSION`. Release metadata only (the admin console's Calculation engine page shows
 * it); no calculation reads it. A test keeps the newest entry equal to the version constants, so
 * a release cannot ship without its note.
 */
export interface EngineRelease {
  engine_version: string;
  formula_version: string;
  /** ISO date of the release (the engine's last change under this version). */
  date: string;
  changes: readonly string[];
}

export const ENGINE_CHANGELOG: readonly [EngineRelease, ...EngineRelease[]] = [
  {
    engine_version: "1.0.0",
    formula_version: "poc-1",
    date: "2026-09-25",
    changes: [
      "The client's formulas: gross floor area, max coverage area, saleable area, land value, design & documentation, construction cost, total cost, market value, potential profit and return on investment.",
      "Low / expected / high ranges by pessimistic pairing (pessimistic-pairing-v1): cost rows at their own bounds, revenue at the price bounds, profit low = revenue low − total cost high, ROI low = profit low ÷ total cost high.",
      "The planned-first calculation basis (selectCalculationBasis): the planned urban parcel area, the cadastral area only as a fallback.",
      "Rounding on outputs only: 2 decimals, half away from zero, identical in this package and its Python copy (backend/core/engine/shared.py).",
      "Held to fixtures/feasibility-cases.json and assumption-dependencies.json; the client has not yet validated the fixtures (P0 gate 3).",
    ],
  },
];

/** The date of the newest release: the engine's "last updated". */
export const ENGINE_UPDATED: string = ENGINE_CHANGELOG[0].date;

/** True when the newest note describes the running version (checked by the package's tests). */
export function changelogIsCurrent(): boolean {
  const latest = ENGINE_CHANGELOG[0];
  return latest.engine_version === ENGINE_VERSION && latest.formula_version === FORMULA_VERSION;
}
