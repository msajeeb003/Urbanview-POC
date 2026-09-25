import { calculate } from "@urbanview/feasibility-engine";
import type { FixtureFile } from "@urbanview/feasibility-engine";
import fixtureJson from "@urbanview/feasibility-engine/fixtures/feasibility-cases.json";
import { describe, expect, it } from "vitest";

import { formatEur, formatPct } from "@/lib/format";

import { LOCKED_PARAMS, cannotText, markerPosition } from "./market-section";

const fixtures = fixtureJson as unknown as FixtureFile;

/**
 * Group 2 of UP 12 (Centar) as `GET /v1/panel?type=urban&id=1` serves it (sample data, captured
 * 2026-09-28). The section renders these numbers as they come; the test holds them to the shared
 * fixture and to the engine the browser runs, so panel, fixtures and engine agree.
 */
const UP12_PAYLOAD: Record<string, { low: number; expected: number; high: number }> = {
  saleable_area_m2: { low: 2149.5, expected: 2149.5, high: 2149.5 },
  construction_cost_eur: { low: 2271104.51, expected: 2640819.2, high: 3036942.08 },
  revenue_eur: { low: 4529004.93, expected: 5266284.8, high: 6056227.52 },
  profit_eur: { low: -315535.67, expected: 1053640.8, high: 2433353.68 },
  roi_pct: { low: -6.51, expected: 25.01, high: 67.17 },
  land_value_eur: { low: 1114095.6, expected: 1295460.0, high: 1489779.0 },
  design_documentation_eur: { low: 237673.73, expected: 276364.8, high: 317819.52 },
};

/** Panel key → engine key (`core/engine/feasibility.py`, the panel adapter). */
const ENGINE_KEY: Record<string, string> = {
  saleable_area_m2: "saleable_area",
  construction_cost_eur: "construction_costs",
  revenue_eur: "market_value",
  profit_eur: "potential_profit",
  roi_pct: "roi_pct",
  land_value_eur: "land_value",
  design_documentation_eur: "design_and_documentation_costs",
};

describe("Group 2 figures", () => {
  const fixture = fixtures.cases.find((c) => c.name === "up12_centar_rates")!;

  it("are the shared fixture's and the browser engine's numbers", () => {
    const result = calculate(fixture.inputs) as unknown as { fields: Record<string, { low: number; expected: number; high: number }> };
    const expected = fixture.expected as unknown as { fields: Record<string, { low: number; expected: number; high: number }> };
    for (const [panelKey, range] of Object.entries(UP12_PAYLOAD)) {
      const engineKey = ENGINE_KEY[panelKey];
      const pick = (f: { low: number; expected: number; high: number }) => ({ low: f.low, expected: f.expected, high: f.high });
      expect(pick(expected.fields[engineKey]), panelKey).toEqual(range);
      expect(pick(result.fields[engineKey]), panelKey).toEqual(range);
    }
  });

  it("render as the mock does, ranges never single money figures", () => {
    expect(formatEur(1295460)).toBe("€1,295,460");
    expect(formatEur(-315535.67)).toBe("−€315,536");
    expect(formatEur(0.4)).toBe("€0");
    expect(formatPct(25.01)).toBe("25%");
    expect(formatPct(-6.51)).toBe("−7%");
  });

  it("place the expected marker between low and high", () => {
    expect(markerPosition(UP12_PAYLOAD.construction_cost_eur)).toBe(48);
    expect(markerPosition(UP12_PAYLOAD.profit_eur)).toBe(50);
    expect(markerPosition({ low: 5, expected: 5, high: 5 })).toBe(50);
    expect(markerPosition({ low: null, expected: null, high: null })).toBe(50);
  });

  it("say why a figure cannot be calculated", () => {
    const base = {
      key: "revenue_eur",
      label_en: "Market value (revenue)",
      label_me: "Tržišna vrijednost",
      unit: "€",
      range_kind: "range" as const,
      status: "cannot_calculate" as const,
      low: null,
      expected: null,
      high: null,
      reason_code: "no_market_data",
      reason_params: { zone_name: "Centar" },
      reason_me: "nema tržišnih podataka za zonu Centar",
    };
    expect(cannotText({ ...base, reason_en: "no market data for zone Centar" })).toBe(
      "cannot calculate — no market data for zone Centar",
    );
    expect(cannotText({ ...base, reason_en: null })).toBe("cannot calculate");
  });

  it("lists the seven locked parameters with definitions and units", () => {
    expect(LOCKED_PARAMS.map(([name, , unit]) => `${name} ${unit}`)).toEqual([
      "Estimated land value €",
      "Construction cost €",
      "Design & documentation €",
      "Estimated market value €",
      "Estimated saleable area m²",
      "Potential profit €",
      "Return on investment %",
    ]);
  });
});
