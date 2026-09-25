/**
 * Input and output types of the shared feasibility engine.
 *
 * Everything is plain JSON-compatible data: the same objects travel through the API payload, the
 * fixtures (`fixtures/feasibility-cases.json`) and the Python engine.
 */

/** Which area the calculation is based on (BRD: planned urban parcel first, cadastral only as fallback). */
export type CalculationBasis = "urban" | "cadastral";

/** Why a figure cannot be calculated. Codes only: the UI owns the (bilingual) texts. */
export type ReasonCode =
  | "area_unknown"
  | "far_not_stated"
  | "coverage_not_stated"
  | "requires_gfa"
  | "no_market_data"
  | "no_market_data_zone_unknown"
  | "total_cost_zero";

/** The two market-related reasons a caller may supply when `market` is null. */
export type MarketMissingReason = "no_market_data" | "no_market_data_zone_unknown";

/**
 * Range bounds supplied by the admin assumptions (never hardcoded here).
 * - `multiplier`: low = expected × low, high = expected × high (low ≤ 1 ≤ high).
 * - `absolute`: the bounds themselves, in the input's unit (low ≤ expected ≤ high).
 */
export type RangeBounds =
  | { kind: "multiplier"; low: number; high: number }
  | { kind: "absolute"; low: number; high: number };

/** A market input with its expected value and the bounds that give its low / high. */
export interface RangedInput {
  expected: number;
  bounds: RangeBounds;
}

/**
 * A cost given either per square metre (land: per m² of plot area; construction and design &
 * documentation: per m² of GFA) or as a total in EUR.
 */
export type CostInput = { per_m2: RangedInput; total?: undefined } | { total: RangedInput; per_m2?: undefined };

/** Current market inputs for the parcel's zone (admin financial assumptions). */
export interface MarketInputs {
  /** Selling price per m² of saleable area (revenue side). */
  market_value_per_m2: RangedInput;
  /** Construction cost (cost side). */
  construction_cost: CostInput;
  /** Land value (cost side). */
  land_value: CostInput;
  /** Design and documentation costs, "other costs" in the UI (cost side). */
  design_and_documentation_costs: CostInput;
}

/** Planning parameters of the selected parcel. `null` means "not stated in the plan"; 0 is a real 0. */
export interface PlanningInputs {
  /** The calculation-basis area in m²: the planned urban parcel, or the cadastral parcel as fallback. */
  plot_area: number | null;
  calculation_basis: CalculationBasis;
  /** Floor area ratio (II). */
  far: number | null;
  /** Maximum site coverage (IZ) in percent, 0–100. */
  site_coverage_pct: number | null;
  /**
   * Context: both areas of the parcel as the visitor sees them (`selectCalculationBasis` picks
   * `plot_area` and `calculation_basis` from them). Validated, not used by any formula.
   */
  planned_area?: number | null;
  cadastral_area?: number | null;
  /** Context echoed for the UI; not used by any formula. */
  max_height_m?: number | null;
  max_floors?: string | null;
  land_use?: string | null;
}

/** The assumptions a visitor may edit ("assumptions visible"). */
export interface EditableAssumptions {
  /** Share of GFA that is saleable; the client's default is 0.70. */
  saleable_share: number;
  /** Construction cost per m² of GFA; `null` = use the market input. */
  construction_cost_per_m2: number | null;
  /** Selling price per m² of saleable area; `null` = use the market input. */
  market_value_per_m2: number | null;
}

/** User edits merged over the defaults by `recalculate`; `undefined` entries are ignored. */
export type EditedAssumptions = { [K in keyof EditableAssumptions]?: EditableAssumptions[K] | undefined };

/** Both areas of a parcel; `null` = not available (no planned parcel, no cadastral record). */
export interface ParcelAreas {
  /** Area of the planned urban parcel the cadastral parcel lies in (the plan's parcel). */
  planned_area: number | null;
  /** Area of the cadastral parcel. */
  cadastral_area: number | null;
}

/** The area the formulas use and which one it is. */
export interface CalculationArea {
  plot_area: number | null;
  calculation_basis: CalculationBasis;
}

export interface EngineInputs {
  planning: PlanningInputs;
  /** `null` when the zone has no current market row; then say why with `market_missing_reason`. */
  market: MarketInputs | null;
  market_missing_reason?: MarketMissingReason;
  /** Caller-side defaults (e.g. from the API payload); the engine's own default is saleable 0.70. */
  assumptions?: Partial<EditableAssumptions>;
}

export type FieldKey =
  | "max_gfa"
  | "max_coverage_area"
  | "saleable_area"
  | "construction_costs"
  | "land_value"
  | "design_and_documentation_costs"
  | "total_cost"
  | "market_value"
  | "potential_profit"
  | "roi_pct";

export type Unit = "m2" | "EUR" | "%";

/** One figure as a range. Numbers when `status === "ok"`, all `null` otherwise. */
export interface FieldRange {
  key: FieldKey;
  unit: Unit;
  /** `deterministic` figures depend on no market input, so low = expected = high. */
  range_kind: "deterministic" | "range";
  status: "ok" | "cannot_calculate";
  reason: ReasonCode | null;
  low: number | null;
  expected: number | null;
  high: number | null;
}

export type ValueSource = "market" | "user";

/** A market input as actually used, with its low / expected / high (rounded for display). */
export interface UsedValue {
  low: number;
  expected: number;
  high: number;
  source: ValueSource;
}

export interface UsedCost extends UsedValue {
  basis: "per_m2_gfa" | "per_m2_plot" | "total";
}

/** "Assumptions visible": every number the calculation used, and where it came from. */
export interface AssumptionsUsed {
  calculation_basis: CalculationBasis;
  plot_area: number | null;
  saleable_share: { value: number; source: "default" | "user" };
  market_value_per_m2: UsedValue | null;
  construction_cost: UsedCost | null;
  land_value: UsedCost | null;
  design_and_documentation_costs: UsedCost | null;
  market_missing_reason: MarketMissingReason | null;
}

export interface EngineResult {
  engine_version: string;
  formula_version: string;
  range_derivation: string;
  calculation_basis: CalculationBasis;
  fields: Record<FieldKey, FieldRange>;
  assumptions_used: AssumptionsUsed;
}

/** The result without `engine_version`: what the fixtures pin down. */
export type ExpectedResult = Omit<EngineResult, "engine_version">;

/** Shape of `fixtures/feasibility-cases.json`. */
export interface FixtureFile {
  formula_version: string;
  client_validated: boolean;
  cases: FixtureCase[];
}

export interface FixtureCase {
  name: string;
  description: string;
  notes: string;
  inputs: EngineInputs;
  edits: EditedAssumptions | null;
  expected: ExpectedResult;
}
