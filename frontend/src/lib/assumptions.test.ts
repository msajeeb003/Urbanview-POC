import { calculate, type EngineInputs, type EngineResult, type FixtureFile } from "@urbanview/feasibility-engine";
import fixtureJson from "@urbanview/feasibility-engine/fixtures/feasibility-cases.json";
import { describe, expect, it } from "vitest";

import type { UrbanPanel } from "./api/types";
import {
  SLIDERS,
  defaultsOf,
  differences,
  editErrors,
  fromSlider,
  recalculateFeasibility,
  sliderBounds,
  toEngineEdits,
  toRequestAssumptions,
  toSlider,
  type Figure,
} from "./assumptions";

const fixtures = fixtureJson as unknown as FixtureFile;
const fixture = (name: string) => fixtures.cases.find((c) => c.name === name)!;

/** What the payload's `engine` block carries (`core.engine.feasibility.EDIT_KEYS`, `SHARED_KEY`). */
const EDIT_KEYS = {
  construction_cost_eur_m2: "construction_cost_per_m2",
  saleable_share: "saleable_share",
  sale_price_eur_m2: "market_value_per_m2",
};
const FIELD_KEYS: Record<string, string> = {
  max_gfa_m2: "max_gfa",
  max_coverage_area_m2: "max_coverage_area",
  saleable_area_m2: "saleable_area",
  construction_cost_eur: "construction_costs",
  revenue_eur: "market_value",
  profit_eur: "potential_profit",
  roi_pct: "roi_pct",
  land_value_eur: "land_value",
  design_documentation_eur: "design_and_documentation_costs",
  total_cost_eur: "total_cost",
};
const FIELDS = ["max_gfa_m2", "max_coverage_area_m2", "saleable_area_m2", "construction_cost_eur", "revenue_eur", "profit_eur", "roi_pct"];
const COST_ROWS = ["land_value_eur", "design_documentation_eur", "construction_cost_eur", "total_cost_eur"];

/** A panel payload as the server builds it from these engine inputs (the adapter maps keys only). */
function payloadFor(inputs: EngineInputs, reason_en: string | null = null) {
  const result: EngineResult = calculate(inputs);
  const figure = (key: string): Figure => {
    const f = result.fields[FIELD_KEYS[key] as keyof EngineResult["fields"]];
    return {
      key,
      label_en: key,
      label_me: key,
      unit: f.unit,
      range_kind: f.range_kind,
      status: f.status,
      reason_code: f.reason,
      reason_en: f.status === "ok" ? null : reason_en,
      reason_me: null,
      low: f.low,
      expected: f.expected,
      high: f.high,
    } as Figure;
  };
  const feasibility = { fields: FIELDS.map(figure), cost_rows: COST_ROWS.map(figure) } as NonNullable<UrbanPanel["feasibility"]>;
  const engine = { inputs, edit_keys: EDIT_KEYS, field_keys: FIELD_KEYS } as unknown as NonNullable<UrbanPanel["engine"]>;
  return { feasibility, engine };
}

const pick = (f: Figure | undefined) => (f ? [f.status, f.low, f.expected, f.high] : null);
const byKey = (figures: Figure[]) => new Map(figures.map((f) => [f.key, f]));

describe("recalculateFeasibility (the shared engine in the browser)", () => {
  const edited = fixture("user_edits_over_multiplier_bounds");
  const { feasibility, engine } = payloadFor(edited.inputs);

  it("shows the payload exactly without edits (Reset to defaults)", () => {
    const view = recalculateFeasibility(feasibility, engine, {});
    expect(view.fields).toBe(feasibility.fields);
    expect(view.cost_rows).toBe(feasibility.cost_rows);
    expect(view.result).toBeNull();
  });

  it("reproduces the shared fixture for the visitor's three edits", () => {
    const view = recalculateFeasibility(feasibility, engine, {
      construction_cost_eur_m2: 700,
      sale_price_eur_m2: 2600,
      saleable_share: 0.8,
    });
    const expected = edited.expected as unknown as EngineResult;
    for (const figure of [...view.fields, ...view.cost_rows]) {
      const e = expected.fields[FIELD_KEYS[figure.key] as keyof EngineResult["fields"]];
      expect(pick(figure), figure.key).toEqual([e.status, e.low, e.expected, e.high]);
    }
  });

  it("moves revenue, profit and ROI with the sale price, within 100 ms, and leaves GFA and coverage", () => {
    const before = byKey([...feasibility.fields]);
    const started = performance.now();
    const view = recalculateFeasibility(feasibility, engine, { sale_price_eur_m2: 3000 });
    expect(performance.now() - started).toBeLessThan(100);
    const after = byKey(view.fields);
    for (const key of ["revenue_eur", "profit_eur", "roi_pct"]) {
      expect(after.get(key)!.expected, key).toBeGreaterThan(before.get(key)!.expected!);
    }
    for (const key of ["max_gfa_m2", "max_coverage_area_m2", "saleable_area_m2", "construction_cost_eur"]) {
      expect(pick(after.get(key)), key).toEqual(pick(before.get(key)));
    }
  });

  it("never rescues missing market data; the payload's reason stays", () => {
    const missing = fixture("edits_do_not_rescue_missing_market");
    const payload = payloadFor(missing.inputs, "no market data for zone Centar");
    const view = recalculateFeasibility(payload.feasibility, payload.engine, {
      construction_cost_eur_m2: 700,
      sale_price_eur_m2: 2600,
    });
    const after = byKey(view.fields);
    expect(after.get("revenue_eur")!.status).toBe("cannot_calculate");
    expect(after.get("revenue_eur")!.reason_en).toBe("no market data for zone Centar");
    expect(after.get("max_gfa_m2")!.status).toBe("ok");
  });

  it("maps edits onto the engine's and the server's names", () => {
    const edits = { construction_cost_eur_m2: 700, sale_price_eur_m2: 2600, saleable_share: 0.8 };
    expect(toEngineEdits(edits, EDIT_KEYS)).toEqual({
      construction_cost_per_m2: 700,
      market_value_per_m2: 2600,
      saleable_share: 0.8,
    });
    expect(toRequestAssumptions({ sale_price_eur_m2: 2600 })).toEqual({
      construction_cost_per_m2: null,
      selling_price_per_m2: 2600,
      saleable_share: null,
    });
  });

  it("reports figures that differ between browser and server", () => {
    const shifted = feasibility.fields.map((f) => (f.key === "roi_pct" ? { ...f, expected: (f.expected ?? 0) + 1 } : f));
    expect(differences(feasibility.fields, feasibility.fields)).toEqual([]);
    expect(differences(feasibility.fields, shifted)).toHaveLength(1);
  });
});

describe("sliders and bounds", () => {
  const defaults = defaultsOf({ construction_cost_eur_m2: 860, sale_price_eur_m2: 2450, saleable_share: 0.7 } as UrbanPanel["assumptions"]);

  it("are the wireframe's, widened to include a zone's default", () => {
    expect(SLIDERS.map((s) => [s.key, s.min, s.max])).toEqual([
      ["construction_cost_eur_m2", 500, 1200],
      ["sale_price_eur_m2", 1200, 3600],
      ["saleable_share", 55, 85],
    ]);
    expect(sliderBounds(SLIDERS[1], 4000)).toEqual({ min: 1200, max: 4000 });
    expect(sliderBounds(SLIDERS[0], null)).toEqual({ min: 500, max: 1200 });
    expect(toSlider("saleable_share", 0.7)).toBe(70);
    expect(fromSlider("saleable_share", 70)).toBe(0.7);
  });

  it("flag an edit outside them, which is then not calculated", () => {
    expect(editErrors({ construction_cost_eur_m2: 780, saleable_share: 0.6 }, defaults)).toEqual({});
    expect(editErrors({ sale_price_eur_m2: 9000 }, defaults)).toEqual({
      sale_price_eur_m2: "Out of range: use 1200–3600 €/m².",
    });
    expect(editErrors({ saleable_share: 1.2 }, defaults).saleable_share).toBe("Out of range: use 55–85%.");
    expect(editErrors({ construction_cost_eur_m2: -1 }, defaults).construction_cost_eur_m2).toBeTruthy();
  });
});
