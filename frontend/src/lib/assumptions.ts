/**
 * The visitor's assumption sandbox (Group 2 "◐ Test your own assumptions"): what may be edited,
 * the slider bounds, validation, and the live recalculation. The formulas are the client's and
 * live only in the shared engine package: this module maps the panel's keys onto the engine's
 * (`engine.edit_keys`, `engine.field_keys` from the payload) and calls `recalculate`, nothing else.
 *
 * - Edits are kept in panel units: construction and sale price in €/m², the saleable share 0–1.
 *   Only edited keys are stored; the others stay the zone's defaults from the payload.
 * - Bounds: the wireframe's sliders (config below; widened to include a zone's default), inside
 *   the server's own limits (`EditedAssumptions` of `POST /v1/feasibility`: prices in (0, 100 000],
 *   share in (0, 1]). An edit outside them is shown as an error and not calculated.
 * - No edits → the payload's figures exactly (what "Reset to defaults" returns to).
 */
import { recalculate, type EditedAssumptions, type EngineInputs, type EngineResult } from "@urbanview/feasibility-engine";

import type { UrbanPanel } from "./api/types";

export type EditKey = "construction_cost_eur_m2" | "sale_price_eur_m2" | "saleable_share";
export type AssumptionEdits = Partial<Record<EditKey, number>>;

type Feasibility = NonNullable<UrbanPanel["feasibility"]>;
export type Figure = Feasibility["fields"][number];
type Engine = NonNullable<UrbanPanel["engine"]>;

export interface Slider {
  key: EditKey;
  label: string;
  /** Slider bounds in the slider's unit (€/m², or % for the share). */
  min: number;
  max: number;
  step: number;
}

/** The wireframe's three sliders (`marketHTML`): bounds are configuration, not formulas. */
export const SLIDERS: Slider[] = [
  { key: "construction_cost_eur_m2", label: "Construction €/m²", min: 500, max: 1200, step: 1 },
  { key: "sale_price_eur_m2", label: "Sale price €/m²", min: 1200, max: 3600, step: 1 },
  { key: "saleable_share", label: "Saleable %", min: 55, max: 85, step: 1 },
];

/** The server's limits for the three edits (`api/schemas/feasibility.py`, `EditedAssumptions`). */
const SERVER_LIMITS: Record<EditKey, { gt: number; le: number }> = {
  construction_cost_eur_m2: { gt: 0, le: 100_000 },
  sale_price_eur_m2: { gt: 0, le: 100_000 },
  saleable_share: { gt: 0, le: 1 },
};

/** A panel value in the slider's unit (the share as a whole percent). */
export function toSlider(key: EditKey, value: number): number {
  return key === "saleable_share" ? Math.round(value * 100) : value;
}

export function fromSlider(key: EditKey, value: number): number {
  return key === "saleable_share" ? value / 100 : value;
}

/** The slider's bounds, widened to include the zone's default so it can always be shown. */
export function sliderBounds(slider: Slider, defaultValue: number | null | undefined): { min: number; max: number } {
  if (defaultValue == null) return { min: slider.min, max: slider.max };
  const d = toSlider(slider.key, defaultValue);
  return { min: Math.min(slider.min, Math.floor(d)), max: Math.max(slider.max, Math.ceil(d)) };
}

/** Why an edit cannot be used, or null. */
export function editError(key: EditKey, value: number, bounds: { min: number; max: number }): string | null {
  const limit = SERVER_LIMITS[key];
  const v = toSlider(key, value);
  const outside = !Number.isFinite(value) || value <= limit.gt || value > limit.le || v < bounds.min || v > bounds.max;
  if (!outside) return null;
  const unit = key === "saleable_share" ? "%" : " €/m²";
  return `Out of range: use ${bounds.min}–${bounds.max}${unit}.`;
}

/** Panel keys → the engine's `recalculate` edit keys (from the payload's `engine.edit_keys`). */
export function toEngineEdits(edits: AssumptionEdits, editKeys: Record<string, string>): EditedAssumptions {
  const out: Record<string, number> = {};
  for (const [key, value] of Object.entries(edits)) {
    const engineKey = editKeys[key];
    if (engineKey && value != null) out[engineKey] = value;
  }
  return out as EditedAssumptions;
}

/** The body of `POST /v1/feasibility` for the same edits (its own field names). */
export function toRequestAssumptions(edits: AssumptionEdits) {
  return {
    construction_cost_per_m2: edits.construction_cost_eur_m2 ?? null,
    selling_price_per_m2: edits.sale_price_eur_m2 ?? null,
    saleable_share: edits.saleable_share ?? null,
  };
}

export function hasEdits(edits: AssumptionEdits): boolean {
  return Object.values(edits).some((v) => v != null);
}

/**
 * The feasibility figures for these edits: the payload's own when there are none, else the shared
 * engine's `recalculate(engine.inputs, edits)` mapped back onto the payload's figure keys. A figure
 * the engine cannot calculate keeps the payload's wording of the reason when it had the same one.
 */
export function recalculateFeasibility(
  payload: Feasibility,
  engine: Engine,
  edits: AssumptionEdits,
): { fields: Figure[]; cost_rows: Figure[]; result: EngineResult | null } {
  if (!hasEdits(edits)) return { fields: payload.fields, cost_rows: payload.cost_rows, result: null };
  const result = recalculate(engine.inputs as unknown as EngineInputs, toEngineEdits(edits, engine.edit_keys));
  const map = (figure: Figure): Figure => {
    const computed = result.fields[engine.field_keys[figure.key] as keyof EngineResult["fields"]];
    if (!computed) return figure;
    const ok = computed.status === "ok";
    return {
      ...figure,
      status: computed.status,
      low: computed.low,
      expected: computed.expected,
      high: computed.high,
      reason_code: ok ? null : (computed.reason ?? figure.reason_code),
      reason_en: ok ? null : figure.status === "cannot_calculate" ? figure.reason_en : null,
      reason_me: ok ? null : figure.status === "cannot_calculate" ? figure.reason_me : null,
    };
  };
  return { fields: payload.fields.map(map), cost_rows: payload.cost_rows.map(map), result };
}

/** Figures that differ between two feasibility views (the browser's and the server's), for a warning. */
export function differences(a: Figure[], b: Figure[]): string[] {
  const byKey = new Map(b.map((f) => [f.key, f]));
  const out: string[] = [];
  for (const f of a) {
    const g = byKey.get(f.key);
    if (!g) continue;
    if (f.status !== g.status || f.low !== g.low || f.expected !== g.expected || f.high !== g.high) {
      out.push(`${f.key}: browser ${f.low}/${f.expected}/${f.high} vs server ${g.low}/${g.expected}/${g.high}`);
    }
  }
  return out;
}

/** The zone's defaults for the three editable assumptions, from the payload's `assumptions`. */
export function defaultsOf(assumptions: UrbanPanel["assumptions"]): Record<EditKey, number | null> {
  return {
    construction_cost_eur_m2: assumptions?.construction_cost_eur_m2 ?? null,
    sale_price_eur_m2: assumptions?.sale_price_eur_m2 ?? null,
    saleable_share: assumptions?.saleable_share ?? null,
  };
}

/** Errors of the edits that are outside their bounds (an edit with an error is not calculated). */
export function editErrors(
  edits: AssumptionEdits,
  defaults: Record<EditKey, number | null>,
): Partial<Record<EditKey, string>> {
  const errors: Partial<Record<EditKey, string>> = {};
  for (const slider of SLIDERS) {
    const value = edits[slider.key];
    if (value == null) continue;
    const error = editError(slider.key, value, sliderBounds(slider, defaults[slider.key]));
    if (error) errors[slider.key] = error;
  }
  return errors;
}
