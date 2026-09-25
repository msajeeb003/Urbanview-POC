import { describe, expect, it } from "vitest";

import { FIELD_DEPENDENCIES, FIELD_ORDER, FORMULA_VERSION, calculate, recalculate } from "../src/index.js";
import type { EditedAssumptions, FieldKey, FixtureFile } from "../src/index.js";
import snapshotJson from "../fixtures/assumption-dependencies.json";
import fixtureJson from "../fixtures/feasibility-cases.json";

const fixtures = fixtureJson as unknown as FixtureFile;

interface DependencySnapshot {
  formula_version: string;
  edits: Record<keyof typeof FIELD_DEPENDENCIES, number>;
  cases: Record<string, Record<string, FieldKey[]>>;
}

const snapshot = snapshotJson as unknown as DependencySnapshot;

/** Which figures change when exactly one assumption is edited (JSON text compared, so byte-level). */
function changedFields(name: string): Record<string, FieldKey[]> {
  const fixtureCase = fixtures.cases.find((c) => c.name === name);
  if (fixtureCase === undefined) throw new Error(`no fixture case ${name}`);
  const baseline = calculate(fixtureCase.inputs).fields;
  const out: Record<string, FieldKey[]> = {};
  for (const [assumption, value] of Object.entries(snapshot.edits)) {
    const edits = { [assumption]: value } as EditedAssumptions;
    const edited = recalculate(fixtureCase.inputs, edits).fields;
    out[assumption] = FIELD_ORDER.filter(
      (key) => JSON.stringify(edited[key]) !== JSON.stringify(baseline[key]),
    );
  }
  return out;
}

describe("fixtures/assumption-dependencies.json (shared snapshot)", () => {
  it("covers every calculate-case of the fixtures, for the current formulas", () => {
    expect(snapshot.formula_version).toBe(FORMULA_VERSION);
    const calculateCases = fixtures.cases.filter((c) => c.edits === null).map((c) => c.name);
    expect(Object.keys(snapshot.cases)).toEqual(calculateCases);
    expect(Object.keys(snapshot.edits).sort()).toEqual(Object.keys(FIELD_DEPENDENCIES).sort());
  });

  for (const name of Object.keys(snapshot.cases)) {
    it(`changing one assumption changes only the snapshot's figures: "${name}"`, () => {
      const changed = changedFields(name);
      expect(changed).toStrictEqual(snapshot.cases[name]);
      // and never a figure the formulas do not link to that assumption
      for (const [assumption, keys] of Object.entries(changed)) {
        const allowed: readonly FieldKey[] =
          FIELD_DEPENDENCIES[assumption as keyof typeof FIELD_DEPENDENCIES];
        for (const key of keys) expect(allowed).toContain(key);
      }
    });
  }
});
