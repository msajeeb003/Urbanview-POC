import { describe, expect, it } from "vitest";

import {
  DEFAULT_SALEABLE_SHARE,
  ENGINE_VERSION,
  EngineInputError,
  FIELD_DEPENDENCIES,
  FIELD_ORDER,
  FORMULA_VERSION,
  RANGE_DERIVATION,
  boundsOf,
  calculate,
  recalculate,
  selectCalculationBasis,
  withEditedExpected,
} from "../src/index.js";
import type { EngineInputs, FieldKey, MarketInputs, RangedInput } from "../src/index.js";

const multiplier = (expected: number, low = 0.86, high = 1.15): RangedInput => ({
  expected,
  bounds: { kind: "multiplier", low, high },
});
const absolute = (expected: number, low: number, high: number): RangedInput => ({
  expected,
  bounds: { kind: "absolute", low, high },
});

const CENTAR: MarketInputs = {
  market_value_per_m2: multiplier(2450),
  construction_cost: { per_m2: multiplier(860) },
  land_value: { per_m2: multiplier(1350) },
  design_and_documentation_costs: { per_m2: multiplier(90) },
};

const UP12: EngineInputs = {
  planning: { plot_area: 959.6, calculation_basis: "urban", far: 3.2, site_coverage_pct: 55 },
  market: CENTAR,
};

const okKeys = (inputs: EngineInputs): FieldKey[] =>
  FIELD_ORDER.filter((key) => calculate(inputs).fields[key].status === "ok");

describe("formulas", () => {
  it("computes the seven fields and the cost rows for a complete parcel", () => {
    const result = calculate(UP12);
    const f = result.fields;
    expect(f.max_gfa.expected).toBe(3070.72); // 3.2 × 959.6
    expect(f.max_coverage_area.expected).toBe(527.78); // 959.6 × 0.55
    expect(f.saleable_area.expected).toBe(2149.5); // 3070.72 × 0.70 = 2149.504
    expect(f.construction_costs.expected).toBe(2640819.2); // 3070.72 × 860
    expect(f.land_value.expected).toBe(1295460); // 959.6 × 1350
    expect(f.design_and_documentation_costs.expected).toBe(276364.8); // 3070.72 × 90
    expect(f.total_cost.expected).toBe(4212644);
    expect(f.market_value.expected).toBe(5266284.8); // 2149.504 × 2450
    expect(f.potential_profit.expected).toBe(1053640.8);
    expect(f.roi_pct.expected).toBe(25.01); // 1,053,640.8 / 4,212,644 × 100
    expect(result.engine_version).toBe(ENGINE_VERSION);
    expect(result.formula_version).toBe(FORMULA_VERSION);
    expect(result.range_derivation).toBe(RANGE_DERIVATION);
    expect(result.assumptions_used.saleable_share).toEqual({ value: DEFAULT_SALEABLE_SHARE, source: "default" });
  });

  it("derives ranges pessimistically: profit low pairs revenue low with costs high", () => {
    const f = calculate(UP12).fields;
    expect(f.construction_costs.low).toBe(2271104.51); // 3070.72 × 739.6
    expect(f.construction_costs.high).toBe(3036942.08); // 3070.72 × 989
    expect(f.market_value.low).toBe(4529004.93); // 2149.504 × 2107
    expect(f.total_cost.high).toBe(4844540.6); // 1,489,779 + 317,819.52 + 3,036,942.08
    expect(f.potential_profit.low).toBe(-315535.67); // 4,529,004.928 − 4,844,540.6
    expect(f.potential_profit.high).toBe(2433353.68); // 6,056,227.52 − 3,622,873.84
    expect(f.roi_pct.low).toBeCloseTo(-6.51, 2);
    expect(f.roi_pct.high).toBeCloseTo(67.17, 2);
    expect(f.roi_pct.low).toBeLessThan(f.roi_pct.expected as number);
    expect(f.roi_pct.expected).toBeLessThan(f.roi_pct.high as number);
  });

  const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

  it("is deterministic: the same inputs give byte-identical JSON", () => {
    const a = JSON.stringify(calculate(UP12));
    const b = JSON.stringify(calculate(clone(UP12)));
    expect(a).toBe(b);
  });

  it("does not mutate its inputs", () => {
    const inputs = clone(UP12);
    recalculate(inputs, { saleable_share: 0.8, construction_cost_per_m2: 700, market_value_per_m2: 2600 });
    expect(inputs).toStrictEqual(UP12);
  });
});

describe("missing inputs never produce a made-up number", () => {
  it("no plot area: everything is area_unknown", () => {
    const result = calculate({ ...UP12, planning: { ...UP12.planning, plot_area: null } });
    for (const key of FIELD_ORDER) {
      expect(result.fields[key]).toMatchObject({ status: "cannot_calculate", reason: "area_unknown" });
    }
    expect(result.assumptions_used.plot_area).toBeNull();
  });

  it("FAR not stated: GFA and everything derived from it, but not coverage or land value", () => {
    const inputs = { ...UP12, planning: { ...UP12.planning, far: null } };
    const f = calculate(inputs).fields;
    expect(f.max_gfa.reason).toBe("far_not_stated");
    for (const key of ["saleable_area", "construction_costs", "design_and_documentation_costs", "total_cost", "market_value", "potential_profit", "roi_pct"] as const) {
      expect(f[key].reason).toBe("requires_gfa");
    }
    expect(okKeys(inputs)).toEqual(["max_coverage_area", "land_value"]);
  });

  it("coverage not stated: only the coverage area", () => {
    const inputs = { ...UP12, planning: { ...UP12.planning, site_coverage_pct: null } };
    const f = calculate(inputs).fields;
    expect(f.max_coverage_area.reason).toBe("coverage_not_stated");
    expect(okKeys(inputs)).toEqual(FIELD_ORDER.filter((k) => k !== "max_coverage_area"));
  });

  it("no market row: the money figures carry the caller's reason, the areas still compute", () => {
    const inputs: EngineInputs = { ...UP12, market: null, market_missing_reason: "no_market_data" };
    const result = calculate(inputs);
    expect(okKeys(inputs)).toEqual(["max_gfa", "max_coverage_area", "saleable_area"]);
    expect(result.fields.land_value.reason).toBe("no_market_data");
    expect(result.fields.roi_pct.reason).toBe("no_market_data");
    expect(result.assumptions_used.market_missing_reason).toBe("no_market_data");
    expect(result.assumptions_used.market_value_per_m2).toBeNull();
    expect(result.assumptions_used.land_value).toBeNull();
  });

  it("no market row and no reason given: zone unknown", () => {
    const result = calculate({ ...UP12, market: null });
    expect(result.fields.market_value.reason).toBe("no_market_data_zone_unknown");
    expect(result.assumptions_used.market_missing_reason).toBe("no_market_data_zone_unknown");
  });

  it("FAR missing takes precedence over a missing market for GFA-derived figures", () => {
    const f = calculate({
      ...UP12,
      planning: { ...UP12.planning, far: null },
      market: null,
      market_missing_reason: "no_market_data",
    }).fields;
    expect(f.construction_costs.reason).toBe("requires_gfa");
    expect(f.land_value.reason).toBe("no_market_data");
  });

  it("a zero total cost makes ROI incalculable while the other figures are 0", () => {
    const f = calculate({ ...UP12, planning: { ...UP12.planning, plot_area: 0 } }).fields;
    expect(f.roi_pct).toMatchObject({ status: "cannot_calculate", reason: "total_cost_zero" });
    expect(f.total_cost.expected).toBe(0);
    expect(f.potential_profit.expected).toBe(0);
  });

  it("a stated FAR of 0 is a real 0, not a missing value", () => {
    const f = calculate({ ...UP12, planning: { ...UP12.planning, far: 0 } }).fields;
    expect(f.max_gfa.status).toBe("ok");
    expect(f.max_gfa.expected).toBe(0);
    expect(f.roi_pct.expected).toBe(-100); // only the land value is a cost
  });
});

describe("recalculate: user edits over the defaults", () => {
  it("changing an assumption changes only the figures that depend on it", () => {
    const baseline = calculate(UP12).fields;
    const edits = {
      saleable_share: 0.8,
      construction_cost_per_m2: 700,
      market_value_per_m2: 2600,
    } as const;
    for (const [assumption, dependants] of Object.entries(FIELD_DEPENDENCIES) as [
      keyof typeof edits,
      readonly FieldKey[],
    ][]) {
      const edited = recalculate(UP12, { [assumption]: edits[assumption] }).fields;
      const changed = FIELD_ORDER.filter(
        (key) => JSON.stringify(edited[key]) !== JSON.stringify(baseline[key]),
      );
      expect(changed).toEqual([...dependants]);
    }
  });

  it("reports the edited values and their source", () => {
    const result = recalculate(UP12, { saleable_share: 0.8, construction_cost_per_m2: 700, market_value_per_m2: 2600 });
    const used = result.assumptions_used;
    expect(used.saleable_share).toEqual({ value: 0.8, source: "user" });
    expect(used.construction_cost).toEqual({ low: 602, expected: 700, high: 805, source: "user", basis: "per_m2_gfa" });
    expect(used.market_value_per_m2).toEqual({ low: 2236, expected: 2600, high: 2990, source: "user" });
    expect(used.land_value).toEqual({ low: 1161, expected: 1350, high: 1552.5, source: "market", basis: "per_m2_plot" });
    expect(result.fields.saleable_area.expected).toBe(2456.58); // 3070.72 × 0.8 = 2456.576
  });

  it("undefined edits are ignored and null returns to the market value", () => {
    const baseline = calculate(UP12);
    expect(recalculate(UP12, { construction_cost_per_m2: undefined })).toStrictEqual(baseline);
    expect(recalculate(UP12, { construction_cost_per_m2: null, market_value_per_m2: null })).toStrictEqual(baseline);
  });

  it("caller defaults in inputs.assumptions behave like edits, but are reported as defaults for the share", () => {
    const withDefaults: EngineInputs = {
      ...UP12,
      assumptions: { saleable_share: 0.75, construction_cost_per_m2: 900, market_value_per_m2: 2500 },
    };
    const result = calculate(withDefaults);
    expect(result.assumptions_used.saleable_share).toEqual({ value: 0.75, source: "default" });
    expect(result.assumptions_used.construction_cost?.source).toBe("user");
    expect(result.assumptions_used.market_value_per_m2?.expected).toBe(2500);
    // an explicit edit still wins over the caller default
    expect(recalculate(withDefaults, { saleable_share: 0.6 }).assumptions_used.saleable_share).toEqual({
      value: 0.6,
      source: "user",
    });
  });

  it("edits do not rescue a missing market row", () => {
    const result = recalculate(
      { ...UP12, market: null, market_missing_reason: "no_market_data" },
      { construction_cost_per_m2: 700, market_value_per_m2: 2600 },
    );
    expect(result.fields.construction_costs.reason).toBe("no_market_data");
    expect(result.assumptions_used.construction_cost).toEqual({ low: 700, expected: 700, high: 700, source: "user", basis: "per_m2_gfa" });
    expect(result.assumptions_used.market_value_per_m2).toEqual({ low: 2600, expected: 2600, high: 2600, source: "user" });
  });

  it("an edit over a total construction cost switches it to a per-m² rate with no spread", () => {
    const market: MarketInputs = { ...CENTAR, construction_cost: { total: absolute(1800000, 1650000, 2000000) } };
    const result = recalculate({ ...UP12, market }, { construction_cost_per_m2: 750 });
    expect(result.assumptions_used.construction_cost).toEqual({ low: 750, expected: 750, high: 750, source: "user", basis: "per_m2_gfa" });
    expect(result.fields.construction_costs.expected).toBe(2303040); // 3070.72 × 750
  });
});

describe("bounds helpers", () => {
  it("boundsOf applies multipliers or returns absolute bounds", () => {
    expect(boundsOf(multiplier(100, 0.9, 1.1))).toEqual({ low: 90, expected: 100, high: 110.00000000000001 });
    expect(boundsOf(absolute(100, 80, 130))).toEqual({ low: 80, expected: 100, high: 130 });
  });

  it("withEditedExpected keeps multipliers, scales absolute bounds proportionally, and flattens when nothing is known", () => {
    expect(withEditedExpected(multiplier(100, 0.9, 1.1), 200)).toEqual(multiplier(200, 0.9, 1.1));
    expect(withEditedExpected(absolute(100, 80, 130), 200)).toEqual(absolute(200, 160, 260));
    expect(withEditedExpected(absolute(0, 0, 0), 200)).toEqual(absolute(200, 200, 200));
    expect(withEditedExpected(null, 200)).toEqual(absolute(200, 200, 200));
  });
});

describe("both parcel areas: the calculation basis", () => {
  it("uses the planned urban parcel area whenever the plan defines one", () => {
    expect(selectCalculationBasis({ planned_area: 959.6, cadastral_area: 1370.9 })).toStrictEqual({
      plot_area: 959.6,
      calculation_basis: "urban",
    });
    expect(selectCalculationBasis({ planned_area: 0, cadastral_area: 1370.9 })).toStrictEqual({
      plot_area: 0,
      calculation_basis: "urban",
    });
  });

  it("falls back to the cadastral area, and to an unknown area with neither", () => {
    expect(selectCalculationBasis({ planned_area: null, cadastral_area: 1370.9 })).toStrictEqual({
      plot_area: 1370.9,
      calculation_basis: "cadastral",
    });
    expect(selectCalculationBasis({ planned_area: null, cadastral_area: null })).toStrictEqual({
      plot_area: null,
      calculation_basis: "cadastral",
    });
  });

  it("rejects a negative or non-finite area on either side", () => {
    expect(() => selectCalculationBasis({ planned_area: -1, cadastral_area: 10 })).toThrow(EngineInputError);
    expect(() => selectCalculationBasis({ planned_area: 10, cadastral_area: Number.NaN })).toThrow(EngineInputError);
  });

  it("feeds calculate; the two areas travel as context and never change a figure", () => {
    const planning = {
      ...UP12.planning,
      ...selectCalculationBasis({ planned_area: 959.6, cadastral_area: 1370.9 }),
    };
    const withContext = calculate({
      ...UP12,
      planning: { ...planning, planned_area: 959.6, cadastral_area: 1370.9, max_height_m: null, land_use: "Residential" },
    });
    expect(JSON.stringify(withContext)).toBe(JSON.stringify(calculate(UP12)));
    const nulls = calculate({ ...UP12, planning: { ...UP12.planning, planned_area: null, cadastral_area: null } });
    expect(JSON.stringify(nulls)).toBe(JSON.stringify(calculate(UP12)));
  });
});

describe("invalid inputs are rejected (caller bugs, not data gaps)", () => {
  const cases: [string, () => unknown][] = [
    ["negative plot area", () => calculate({ ...UP12, planning: { ...UP12.planning, plot_area: -1 } })],
    ["NaN FAR", () => calculate({ ...UP12, planning: { ...UP12.planning, far: Number.NaN } })],
    ["coverage above 100", () => calculate({ ...UP12, planning: { ...UP12.planning, site_coverage_pct: 101 } })],
    ["negative coverage", () => calculate({ ...UP12, planning: { ...UP12.planning, site_coverage_pct: -5 } })],
    ["bad basis", () => calculate({ ...UP12, planning: { ...UP12.planning, calculation_basis: "plot" as never } })],
    ["negative planned area", () => calculate({ ...UP12, planning: { ...UP12.planning, planned_area: -5 } })],
    ["infinite cadastral area", () => calculate({ ...UP12, planning: { ...UP12.planning, cadastral_area: Number.POSITIVE_INFINITY } })],
    ["saleable share 0", () => recalculate(UP12, { saleable_share: 0 })],
    ["saleable share above 1", () => recalculate(UP12, { saleable_share: 1.2 })],
    ["negative edit", () => recalculate(UP12, { construction_cost_per_m2: -1 })],
    ["infinite price", () => calculate({ ...UP12, market: { ...CENTAR, market_value_per_m2: multiplier(Number.POSITIVE_INFINITY) } })],
    ["multiplier low above 1", () => calculate({ ...UP12, market: { ...CENTAR, market_value_per_m2: multiplier(2450, 1.2, 1.3) } })],
    ["multiplier high below 1", () => calculate({ ...UP12, market: { ...CENTAR, market_value_per_m2: multiplier(2450, 0.8, 0.9) } })],
    ["absolute bounds not bracketing expected", () => calculate({ ...UP12, market: { ...CENTAR, market_value_per_m2: absolute(2450, 2500, 2600) } })],
    ["unknown bounds kind", () => calculate({ ...UP12, market: { ...CENTAR, market_value_per_m2: { expected: 1, bounds: { kind: "range", low: 1, high: 1 } as never } } })],
    ["cost with neither per_m2 nor total", () => calculate({ ...UP12, market: { ...CENTAR, land_value: {} as never } })],
    ["negative cost total", () => calculate({ ...UP12, market: { ...CENTAR, land_value: { total: absolute(-5, -5, -5) } } })],
  ];
  for (const [name, run] of cases) {
    it(name, () => {
      expect(run).toThrow(EngineInputError);
    });
  }
});
