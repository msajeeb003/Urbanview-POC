/**
 * The feasibility formulas (client-owned) and the range derivation.
 *
 * Formulas — evaluated in exactly this order so the Python engine's doubles match bit for bit:
 *
 *   max_gfa                 = far × plot_area
 *   max_coverage_area       = plot_area × (site_coverage_pct / 100)
 *   saleable_area           = max_gfa × saleable_share                       (0.70 by default)
 *   construction_costs      = max_gfa × construction_cost_per_m2  | construction total
 *   land_value              = plot_area × land_value_per_m2       | land total
 *   design_and_documentation_costs = max_gfa × design_per_m2      | design total
 *   total_cost              = land_value + design_and_documentation_costs + construction_costs
 *   market_value (revenue)  = saleable_area × market_value_per_m2
 *   potential_profit        = market_value − total_cost
 *   roi_pct                 = potential_profit / total_cost × 100
 *
 * Range derivation ("pessimistic-pairing-v1"):
 *   The admin supplies bounds per market input (multiplier or absolute). Each money input therefore
 *   has its own low / expected / high value; the three areas depend on no market input and are
 *   `deterministic` (low = expected = high). For every output, `low` is its smallest plausible
 *   value and `high` its largest within those bounds:
 *   - cost rows: the cost at its own low / high bound (sum of lows and sum of highs for the total);
 *   - market_value: revenue at the low / high selling price;
 *   - potential_profit: low = market_value.low − total_cost.high, high = market_value.high −
 *     total_cost.low (revenue low paired with costs high, and vice versa);
 *   - roi_pct: low = potential_profit.low / total_cost.high, high = potential_profit.high /
 *     total_cost.low (ROI = revenue / cost − 1 is monotone, so these are its true extremes).
 *   Consequently every range satisfies low ≤ expected ≤ high.
 *
 * Missing inputs never produce a made-up number: the affected figures are `cannot_calculate` with
 * a reason code, in this precedence: no plot area → everything `area_unknown`; FAR not stated →
 * max_gfa `far_not_stated` and everything derived from GFA `requires_gfa`; coverage not stated →
 * only max_coverage_area `coverage_not_stated`; no market row → the money figures carry the
 * market reason (land value needs the plot area and the market row but no FAR, so it reports the
 * market reason even when the FAR is missing); total cost 0 → roi_pct `total_cost_zero`.
 */

import { DECIMALS, roundHalfAwayFromZero } from "./rounding.js";
import type {
  AssumptionsUsed,
  CostInput,
  EditedAssumptions,
  EngineInputs,
  EngineResult,
  FieldKey,
  FieldRange,
  MarketMissingReason,
  RangedInput,
  ReasonCode,
  Unit,
  UsedCost,
  UsedValue,
  ValueSource,
} from "./types.js";
import { ENGINE_VERSION, FORMULA_VERSION, RANGE_DERIVATION } from "./version.js";

export const DEFAULT_SALEABLE_SHARE = 0.7;

/** Output order for tables: the seven fields of the panel, with the cost rows in between. */
export const FIELD_ORDER: readonly FieldKey[] = [
  "max_gfa",
  "max_coverage_area",
  "saleable_area",
  "construction_costs",
  "land_value",
  "design_and_documentation_costs",
  "total_cost",
  "market_value",
  "potential_profit",
  "roi_pct",
];

export const FIELD_UNITS: Readonly<Record<FieldKey, Unit>> = {
  max_gfa: "m2",
  max_coverage_area: "m2",
  saleable_area: "m2",
  construction_costs: "EUR",
  land_value: "EUR",
  design_and_documentation_costs: "EUR",
  total_cost: "EUR",
  market_value: "EUR",
  potential_profit: "EUR",
  roi_pct: "%",
};

const DETERMINISTIC: ReadonlySet<FieldKey> = new Set(["max_gfa", "max_coverage_area", "saleable_area"]);

/** Which figures depend on which assumption (used by the UI to highlight what an edit changes). */
export const FIELD_DEPENDENCIES: Readonly<Record<keyof EditedAssumptions, readonly FieldKey[]>> = {
  saleable_share: ["saleable_area", "market_value", "potential_profit", "roi_pct"],
  construction_cost_per_m2: ["construction_costs", "total_cost", "potential_profit", "roi_pct"],
  market_value_per_m2: ["market_value", "potential_profit", "roi_pct"],
};

/** Thrown for inputs that are invalid rather than missing (a caller bug, not a data gap). */
export class EngineInputError extends Error {
  override readonly name = "EngineInputError";
}

interface Triplet {
  low: number;
  expected: number;
  high: number;
}

// ---------------------------------------------------------------------------------------------
// validation

function assert(condition: boolean, message: string): asserts condition {
  if (!condition) {
    throw new EngineInputError(message);
  }
}

function assertFinite(value: number, label: string): void {
  assert(typeof value === "number" && Number.isFinite(value), `${label} must be a finite number`);
}

function assertNonNegative(value: number, label: string): void {
  assertFinite(value, label);
  assert(value >= 0, `${label} must not be negative`);
}

function validateRanged(input: RangedInput, label: string): void {
  assertNonNegative(input.expected, `${label}.expected`);
  const { bounds } = input;
  assertFinite(bounds.low, `${label}.bounds.low`);
  assertFinite(bounds.high, `${label}.bounds.high`);
  if (bounds.kind === "multiplier") {
    assert(bounds.low >= 0 && bounds.low <= 1, `${label}.bounds.low must be within [0, 1] for a multiplier`);
    assert(bounds.high >= 1, `${label}.bounds.high must be at least 1 for a multiplier`);
  } else {
    assert(bounds.kind === "absolute", `${label}.bounds.kind must be "multiplier" or "absolute"`);
    assert(
      bounds.low <= input.expected && input.expected <= bounds.high,
      `${label} absolute bounds must satisfy low <= expected <= high`,
    );
  }
}

function validateCost(input: CostInput, label: string): void {
  if (input.per_m2 !== undefined) {
    validateRanged(input.per_m2, `${label}.per_m2`);
  } else {
    assert(input.total !== undefined, `${label} needs per_m2 or total`);
    validateRanged(input.total, `${label}.total`);
  }
}

function validateInputs(inputs: EngineInputs, edits: EditedAssumptions): void {
  const { planning, market } = inputs;
  assert(
    planning.calculation_basis === "urban" || planning.calculation_basis === "cadastral",
    'planning.calculation_basis must be "urban" or "cadastral"',
  );
  if (planning.plot_area !== null) assertNonNegative(planning.plot_area, "planning.plot_area");
  if (planning.far !== null) assertNonNegative(planning.far, "planning.far");
  if (planning.site_coverage_pct !== null) {
    assertNonNegative(planning.site_coverage_pct, "planning.site_coverage_pct");
    assert(planning.site_coverage_pct <= 100, "planning.site_coverage_pct must be at most 100");
  }
  if (market !== null) {
    validateRanged(market.market_value_per_m2, "market.market_value_per_m2");
    validateCost(market.construction_cost, "market.construction_cost");
    validateCost(market.land_value, "market.land_value");
    validateCost(market.design_and_documentation_costs, "market.design_and_documentation_costs");
  }
  const share = resolveSaleableShare(inputs, edits).value;
  assertFinite(share, "saleable_share");
  assert(share > 0 && share <= 1, "saleable_share must be within (0, 1]");
  for (const key of ["construction_cost_per_m2", "market_value_per_m2"] as const) {
    const edited = edits[key];
    if (edited !== undefined && edited !== null) assertNonNegative(edited, `edits.${key}`);
  }
}

// ---------------------------------------------------------------------------------------------
// range derivation helpers

/** The low / expected / high of one market input from its bounds (unrounded). */
export function boundsOf(input: RangedInput): Triplet {
  const { expected, bounds } = input;
  if (bounds.kind === "multiplier") {
    return { low: expected * bounds.low, expected, high: expected * bounds.high };
  }
  return { low: bounds.low, expected, high: bounds.high };
}

/**
 * A user edit replaces the expected value. Multiplier bounds simply apply to the new value;
 * absolute bounds keep their proportion to the original expected value (low × new/old), so an
 * admin spread of −9 % / +14 % stays −9 % / +14 % after the edit.
 */
export function withEditedExpected(original: RangedInput | null, edited: number): RangedInput {
  if (original === null || (original.bounds.kind === "absolute" && original.expected === 0)) {
    return { expected: edited, bounds: { kind: "absolute", low: edited, high: edited } };
  }
  if (original.bounds.kind === "multiplier") {
    return { expected: edited, bounds: original.bounds };
  }
  const { low, high } = original.bounds;
  return {
    expected: edited,
    bounds: {
      kind: "absolute",
      low: (low * edited) / original.expected,
      high: (high * edited) / original.expected,
    },
  };
}

function resolveSaleableShare(inputs: EngineInputs, edits: EditedAssumptions): AssumptionsUsed["saleable_share"] {
  if (edits.saleable_share !== undefined) {
    return { value: edits.saleable_share, source: "user" };
  }
  return { value: inputs.assumptions?.saleable_share ?? DEFAULT_SALEABLE_SHARE, source: "default" };
}

interface ResolvedMarket {
  price: RangedInput | null;
  priceSource: ValueSource | null;
  construction: CostInput | null;
  constructionSource: ValueSource | null;
  land: CostInput | null;
  design: CostInput | null;
}

/** Merge the caller defaults and the user edits over the market inputs. */
function resolveMarket(inputs: EngineInputs, edits: EditedAssumptions): ResolvedMarket {
  const market = inputs.market;
  const resolved: ResolvedMarket = {
    price: market?.market_value_per_m2 ?? null,
    priceSource: market ? "market" : null,
    construction: market?.construction_cost ?? null,
    constructionSource: market ? "market" : null,
    land: market?.land_value ?? null,
    design: market?.design_and_documentation_costs ?? null,
  };
  const editedPrice = edits.market_value_per_m2 ?? inputs.assumptions?.market_value_per_m2 ?? null;
  if (editedPrice !== null) {
    resolved.price = withEditedExpected(resolved.price, editedPrice);
    resolved.priceSource = "user";
  }
  const editedConstruction =
    edits.construction_cost_per_m2 ?? inputs.assumptions?.construction_cost_per_m2 ?? null;
  if (editedConstruction !== null) {
    const perM2 = resolved.construction?.per_m2 ?? null;
    resolved.construction = { per_m2: withEditedExpected(perM2, editedConstruction) };
    resolved.constructionSource = "user";
  }
  return resolved;
}

// ---------------------------------------------------------------------------------------------
// output helpers

function round(value: number, unit: Unit): number {
  return roundHalfAwayFromZero(value, DECIMALS[unit]);
}

function ok(key: FieldKey, triplet: Triplet): FieldRange {
  const unit = FIELD_UNITS[key];
  return {
    key,
    unit,
    range_kind: DETERMINISTIC.has(key) ? "deterministic" : "range",
    status: "ok",
    reason: null,
    low: round(triplet.low, unit),
    expected: round(triplet.expected, unit),
    high: round(triplet.high, unit),
  };
}

function cannot(key: FieldKey, reason: ReasonCode): FieldRange {
  return {
    key,
    unit: FIELD_UNITS[key],
    range_kind: DETERMINISTIC.has(key) ? "deterministic" : "range",
    status: "cannot_calculate",
    reason,
    low: null,
    expected: null,
    high: null,
  };
}

function flat(value: number): Triplet {
  return { low: value, expected: value, high: value };
}

/** The cost's low / expected / high in EUR (per-m² inputs multiplied by their base area). */
function costTriplet(input: CostInput, baseArea: number): Triplet {
  if (input.per_m2 !== undefined) {
    const rate = boundsOf(input.per_m2);
    return { low: baseArea * rate.low, expected: baseArea * rate.expected, high: baseArea * rate.high };
  }
  return boundsOf(input.total as RangedInput);
}

function usedValue(triplet: Triplet, source: ValueSource): UsedValue {
  return {
    low: round(triplet.low, "EUR"),
    expected: round(triplet.expected, "EUR"),
    high: round(triplet.high, "EUR"),
    source,
  };
}

function usedCost(input: CostInput | null, source: ValueSource | null, perM2Basis: UsedCost["basis"]): UsedCost | null {
  if (input === null || source === null) {
    return null;
  }
  if (input.per_m2 !== undefined) {
    return { ...usedValue(boundsOf(input.per_m2), source), basis: perM2Basis };
  }
  return { ...usedValue(boundsOf(input.total as RangedInput), source), basis: "total" };
}

// ---------------------------------------------------------------------------------------------
// the engine

function compute(inputs: EngineInputs, edits: EditedAssumptions): EngineResult {
  validateInputs(inputs, edits);
  const { planning } = inputs;
  const saleableShare = resolveSaleableShare(inputs, edits);
  const market = resolveMarket(inputs, edits);
  const marketReason: MarketMissingReason | null =
    inputs.market === null ? (inputs.market_missing_reason ?? "no_market_data_zone_unknown") : null;

  // Blocking reasons, most fundamental first; the first that applies to a figure wins.
  const areaReason: ReasonCode | null = planning.plot_area === null ? "area_unknown" : null;
  const farMissing = planning.far === null;
  const gfaReason = areaReason ?? (farMissing ? "far_not_stated" : null);
  const needsGfa = areaReason ?? (farMissing ? "requires_gfa" : null);
  const coverageReason = areaReason ?? (planning.site_coverage_pct === null ? "coverage_not_stated" : null);
  const landReason = areaReason ?? marketReason;
  const moneyReason = needsGfa ?? marketReason;

  const area = planning.plot_area ?? 0;
  const gfa = gfaReason ? 0 : (planning.far as number) * area;
  const coverageArea = coverageReason ? 0 : area * ((planning.site_coverage_pct as number) / 100);
  const saleable = needsGfa ? 0 : gfa * saleableShare.value;

  const fields = {} as Record<FieldKey, FieldRange>;
  fields.max_gfa = gfaReason ? cannot("max_gfa", gfaReason) : ok("max_gfa", flat(gfa));
  fields.max_coverage_area = coverageReason
    ? cannot("max_coverage_area", coverageReason)
    : ok("max_coverage_area", flat(coverageArea));
  fields.saleable_area = needsGfa ? cannot("saleable_area", needsGfa) : ok("saleable_area", flat(saleable));

  // Past these checks the market inputs are present (a null market sets marketReason).
  fields.land_value = landReason
    ? cannot("land_value", landReason)
    : ok("land_value", costTriplet(market.land as CostInput, area));

  if (moneyReason) {
    for (const key of [
      "construction_costs",
      "design_and_documentation_costs",
      "total_cost",
      "market_value",
      "potential_profit",
      "roi_pct",
    ] as const) {
      fields[key] = cannot(key, moneyReason);
    }
  } else {
    const construction = costTriplet(market.construction as CostInput, gfa);
    const design = costTriplet(market.design as CostInput, gfa);
    const land = costTriplet(market.land as CostInput, area);
    const total: Triplet = {
      low: land.low + design.low + construction.low,
      expected: land.expected + design.expected + construction.expected,
      high: land.high + design.high + construction.high,
    };
    const price = boundsOf(market.price as RangedInput);
    const revenue: Triplet = {
      low: saleable * price.low,
      expected: saleable * price.expected,
      high: saleable * price.high,
    };
    const profit: Triplet = {
      low: revenue.low - total.high,
      expected: revenue.expected - total.expected,
      high: revenue.high - total.low,
    };
    fields.construction_costs = ok("construction_costs", construction);
    fields.design_and_documentation_costs = ok("design_and_documentation_costs", design);
    fields.total_cost = ok("total_cost", total);
    fields.market_value = ok("market_value", revenue);
    fields.potential_profit = ok("potential_profit", profit);
    fields.roi_pct =
      total.expected === 0 || total.low === 0 || total.high === 0
        ? cannot("roi_pct", "total_cost_zero")
        : ok("roi_pct", {
            low: (profit.low / total.high) * 100,
            expected: (profit.expected / total.expected) * 100,
            high: (profit.high / total.low) * 100,
          });
  }

  const assumptionsUsed: AssumptionsUsed = {
    calculation_basis: planning.calculation_basis,
    plot_area: planning.plot_area,
    saleable_share: saleableShare,
    market_value_per_m2: market.price ? usedValue(boundsOf(market.price), market.priceSource as ValueSource) : null,
    construction_cost: usedCost(market.construction, market.constructionSource, "per_m2_gfa"),
    land_value: usedCost(market.land, market.land ? "market" : null, "per_m2_plot"),
    design_and_documentation_costs: usedCost(market.design, market.design ? "market" : null, "per_m2_gfa"),
    market_missing_reason: marketReason,
  };

  // Rebuild in FIELD_ORDER so JSON output is stable regardless of the branches taken above.
  const ordered = {} as Record<FieldKey, FieldRange>;
  for (const key of FIELD_ORDER) {
    ordered[key] = fields[key];
  }
  return {
    engine_version: ENGINE_VERSION,
    formula_version: FORMULA_VERSION,
    range_derivation: RANGE_DERIVATION,
    calculation_basis: planning.calculation_basis,
    fields: ordered,
    assumptions_used: assumptionsUsed,
  };
}

/** Run the formulas with the defaults (saleable share 0.70, market rates). Pure and deterministic. */
export function calculate(inputs: EngineInputs): EngineResult {
  return compute(inputs, {});
}

/**
 * Live recalculation: merge the visitor's edits (construction cost per m², selling price per m²,
 * saleable share) over the defaults and recompute. Edits are validated like inputs; `undefined`
 * entries are ignored, `null` means "back to the market value".
 */
export function recalculate(inputs: EngineInputs, editedAssumptions: EditedAssumptions): EngineResult {
  const edits: EditedAssumptions = {};
  if (editedAssumptions.saleable_share !== undefined) edits.saleable_share = editedAssumptions.saleable_share;
  if (editedAssumptions.construction_cost_per_m2 !== undefined) {
    edits.construction_cost_per_m2 = editedAssumptions.construction_cost_per_m2;
  }
  if (editedAssumptions.market_value_per_m2 !== undefined) {
    edits.market_value_per_m2 = editedAssumptions.market_value_per_m2;
  }
  return compute(inputs, edits);
}

/** The fixture view of a result: everything except the engine (package) version. */
export function expectedOf(result: EngineResult): Omit<EngineResult, "engine_version"> {
  const { engine_version: _ignored, ...rest } = result;
  return rest;
}
