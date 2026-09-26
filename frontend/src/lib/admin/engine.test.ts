import { describe, expect, it } from "vitest";

import { ENGINE_CHANGELOG, FIELD_ORDER } from "@urbanview/feasibility-engine";

import { engineFooter, FORMULA_ROWS, inputRows, proposalChip, proposalProblem } from "./engine";

describe("formulas", () => {
  it("name only the engine's own figures, all but the total cost", () => {
    const keys = FORMULA_ROWS.map((r) => r.key);
    expect(keys.every((k) => (FIELD_ORDER as readonly string[]).includes(k))).toBe(true);
    expect(FIELD_ORDER.filter((k) => !keys.includes(k))).toEqual(["total_cost"]);
    expect(FORMULA_ROWS).toHaveLength(9);
  });

  it("has a changelog to show", () => {
    expect(ENGINE_CHANGELOG.length).toBeGreaterThan(0);
  });
});

describe("input data", () => {
  it("names the profile's sources per dataset", () => {
    const rows = inputRows([
      { id: "eregistri", kind: "planning", name: "eRegistri (lamp.gov.me)", url: "", provides: "" },
      { id: "ekatastar", kind: "cadastre", name: "eKatastar", url: "", provides: "" },
      { id: "realitica", kind: "market", name: "Realitica", url: "", provides: "" },
      { id: "monstat", kind: "market", name: "Monstat", url: "", provides: "" },
      { id: "site", kind: "reference", name: "site-check", url: "", provides: "" },
    ]);
    expect(rows.map((r) => r.name)).toEqual(["Adopted planning documents", "Cadastre", "Market sources"]);
    expect(rows[0].provides).toMatch(/\(eRegistri\)$/);
    expect(rows[2].provides).toMatch(/\(Realitica, Monstat\)$/);
    expect(inputRows(null)[1].provides).toBe("parcel geometry, area and ownership status");
  });
});

describe("proposals", () => {
  it("label and check them", () => {
    expect(proposalChip({ status: "new" })).toEqual({ tone: "pend", label: "New" });
    expect(proposalChip({ status: "pending" }).label).toBe("Pending");
    const empty = { name: "", expression: "", source: "", provides: "" };
    expect(proposalProblem("formula", empty)).toBe("Name the output.");
    expect(proposalProblem("formula", { ...empty, name: "Parking" })).toBe("Give the expression.");
    expect(proposalProblem("data_input", { ...empty, name: "Costs" })).toBe("Say what it provides.");
    expect(proposalProblem("formula", { ...empty, name: "Parking", expression: "GFA ÷ 60" })).toBeNull();
  });

  it("writes the footer the wireframe has, without claiming a proposal changes the engine", () => {
    expect(
      engineFooter({ engineVersion: "1.0.0", formulaVersion: "poc-1", formulas: 9, inputs: 3, updated: "25 Sep 2026", proposals: 1 }),
    ).toBe(
      "Engine v1.0.0 · formula version poc-1 · 9 formulas · 3 input sources · last updated 25 Sep 2026. Proposed formulas and " +
        "datasets are recorded for the client's review (1 waiting): the calculation changes only with a new formula version the " +
        "client has validated.",
    );
  });
});
