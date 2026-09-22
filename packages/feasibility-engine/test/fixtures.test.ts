import { describe, expect, it } from "vitest";

import { FIELD_ORDER, FORMULA_VERSION, calculate, expectedOf, recalculate } from "../src/index.js";
import type { FixtureFile } from "../src/index.js";
import fixtureJson from "../fixtures/feasibility-cases.json";

const fixtures = fixtureJson as unknown as FixtureFile;

describe("fixtures/feasibility-cases.json (the client's contract)", () => {
  it("is versioned with the formulas the engine implements", () => {
    expect(fixtures.formula_version).toBe(FORMULA_VERSION);
    expect(fixtures.cases.length).toBeGreaterThan(0);
  });

  for (const fixtureCase of fixtures.cases) {
    it(`reproduces "${fixtureCase.name}" exactly`, () => {
      const result =
        fixtureCase.edits === null
          ? calculate(fixtureCase.inputs)
          : recalculate(fixtureCase.inputs, fixtureCase.edits);
      // Deep equality with no tolerance: the numbers must match to the cent.
      expect(expectedOf(result)).toStrictEqual(fixtureCase.expected);
      // JSON round trip is byte-identical, so the server (Python) and browser see the same text.
      expect(JSON.stringify(expectedOf(result))).toBe(JSON.stringify(fixtureCase.expected));
      expect(Object.keys(result.fields)).toEqual([...FIELD_ORDER]);
    });
  }

  it("keeps every range ordered low <= expected <= high", () => {
    for (const fixtureCase of fixtures.cases) {
      for (const field of Object.values(fixtureCase.expected.fields)) {
        if (field.status === "ok") {
          expect(field.low).toBeLessThanOrEqual(field.expected as number);
          expect(field.expected).toBeLessThanOrEqual(field.high as number);
          if (field.range_kind === "deterministic") {
            expect(field.low).toBe(field.expected);
            expect(field.high).toBe(field.expected);
          }
        } else {
          expect(field.low).toBeNull();
          expect(field.expected).toBeNull();
          expect(field.high).toBeNull();
          expect(field.reason).not.toBeNull();
        }
      }
    }
  });
});
