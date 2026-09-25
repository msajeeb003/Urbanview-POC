import {
  FORMULA_VERSION,
  calculate,
  expectedOf,
  recalculate,
  selectCalculationBasis,
} from "@urbanview/feasibility-engine";
import type { FixtureFile } from "@urbanview/feasibility-engine";
import fixtureJson from "@urbanview/feasibility-engine/fixtures/feasibility-cases.json";
import { describe, expect, it } from "vitest";

// The public map runs the same shared engine as the server (backend/core/engine/shared.py): this
// proves the app's own toolchain resolves the workspace package and reproduces every shared
// fixture byte for byte, so a visitor's recalculation equals the server's figures.
const fixtures = fixtureJson as unknown as FixtureFile;

describe("@urbanview/feasibility-engine in the frontend", () => {
  it("is the formula version the fixtures pin", () => {
    expect(fixtures.formula_version).toBe(FORMULA_VERSION);
  });

  for (const fixtureCase of fixtures.cases) {
    it(`reproduces "${fixtureCase.name}" byte for byte`, () => {
      const result =
        fixtureCase.edits === null
          ? calculate(fixtureCase.inputs)
          : recalculate(fixtureCase.inputs, fixtureCase.edits);
      expect(JSON.stringify(expectedOf(result))).toBe(JSON.stringify(fixtureCase.expected));
    });
  }

  it("applies the planned-first calculation basis", () => {
    expect(selectCalculationBasis({ planned_area: 959.6, cadastral_area: 1370.9 })).toEqual({
      plot_area: 959.6,
      calculation_basis: "urban",
    });
  });
});
