/**
 * View state in the address bar, so a link reproduces what the sender saw:
 *
 * - `?parcel=<Parcel ID>`: the selected cadastral parcel (UrbanView's cadastral parcel id);
 * - `?layers=<id>[:<field>],…`: the toggleable layers that are on, in rail ids, with the field a
 *   choropleth shows (`heatFAR:far|coverage|height|gfa`, `heatMkt:low|expected|high`). Core
 *   layers are always on and never listed. The default view has no `layers` parameter at all.
 *
 * Written with `history.replaceState` (no navigation, no history entry per click); read once when
 * the page loads.
 */
import {
  DEFAULT_CHOROPLETH,
  DEFAULT_LAYER_STATE,
  LAYERS,
  type ChoroplethState,
  type LayerId,
} from "@/lib/layers";
import type { ParamMetric, PriceMetric } from "@/lib/map/classes";

export const PARCEL_PARAM = "parcel";
export const LAYERS_PARAM = "layers";

const PARAM_CODES: Record<ParamMetric, string> = {
  max_far: "far",
  max_site_coverage_pct: "coverage",
  max_height_m: "height",
  max_gfa_m2: "gfa",
};
const PARAM_FROM_CODE = Object.fromEntries(Object.entries(PARAM_CODES).map(([k, v]) => [v, k])) as Record<string, ParamMetric>;
const PRICE_CODES: readonly PriceMetric[] = ["low", "expected", "high"];
const TOGGLEABLE = LAYERS.filter((l) => !l.core).map((l) => l.id);

/** The Parcel ID in a query string, or null when absent or not a positive integer. */
export function readParcelParam(search: string): number | null {
  const raw = new URLSearchParams(search).get(PARCEL_PARAM);
  if (!raw || !/^\d{1,18}$/.test(raw)) return null;
  const id = Number(raw);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

/** The same URL with `?parcel=` set to the id, or removed for null. Other parameters stay. */
export function withParcelParam(href: string, id: number | null): string {
  return withParam(href, PARCEL_PARAM, id == null ? null : String(id));
}

export function withParam(href: string, name: string, value: string | null): string {
  const url = new URL(href);
  if (value == null) url.searchParams.delete(name);
  else url.searchParams.set(name, value);
  return url.toString().replace(/%2C/gi, ",").replace(/%3A/gi, ":");
}

function replaceUrl(next: string): void {
  if (next !== window.location.href) window.history.replaceState(window.history.state, "", next);
}

export function syncParcelParam(id: number | null): void {
  if (typeof window === "undefined") return;
  replaceUrl(withParcelParam(window.location.href, id));
}

/** `layers` value for a view, or null for the default view (no parameter). */
export function formatLayersParam(layers: Record<LayerId, boolean>, choropleth: ChoroplethState): string | null {
  const isDefault =
    TOGGLEABLE.every((id) => layers[id] === DEFAULT_LAYER_STATE[id]) &&
    (!layers.heatFAR || choropleth.param === DEFAULT_CHOROPLETH.param) &&
    (!layers.heatMkt || choropleth.price === DEFAULT_CHOROPLETH.price);
  if (isDefault) return null;
  const on = TOGGLEABLE.filter((id) => layers[id]).map((id) =>
    id === "heatFAR" ? `${id}:${PARAM_CODES[choropleth.param]}` : id === "heatMkt" ? `${id}:${choropleth.price}` : id,
  );
  return on.length ? on.join(",") : "none";
}

/**
 * The view a `layers` value describes, or null when absent / unreadable. Unknown ids and fields are
 * ignored; core layers stay on whatever the link says.
 */
export function parseLayersParam(search: string): { layers: Record<LayerId, boolean>; choropleth: ChoroplethState } | null {
  const raw = new URLSearchParams(search).get(LAYERS_PARAM);
  if (raw == null) return null;
  const layers = { ...DEFAULT_LAYER_STATE };
  for (const id of TOGGLEABLE) layers[id] = false;
  const choropleth = { ...DEFAULT_CHOROPLETH };
  let known = raw.trim() === "none";
  for (const token of raw.split(",")) {
    const [id, field] = token.trim().split(":");
    if (!(TOGGLEABLE as string[]).includes(id)) continue;
    known = true;
    layers[id as LayerId] = true;
    if (id === "heatFAR" && field && PARAM_FROM_CODE[field]) choropleth.param = PARAM_FROM_CODE[field];
    if (id === "heatMkt" && field && (PRICE_CODES as string[]).includes(field)) choropleth.price = field as PriceMetric;
  }
  if (!known) return null;
  // one choropleth at a time, as on the rail: the first listed wins
  if (layers.heatFAR && layers.heatMkt) {
    const first = raw.split(",").map((t) => t.trim().split(":")[0]).find((t) => t === "heatFAR" || t === "heatMkt");
    layers[first === "heatFAR" ? "heatMkt" : "heatFAR"] = false;
  }
  return { layers, choropleth };
}

export function syncLayersParam(layers: Record<LayerId, boolean>, choropleth: ChoroplethState): void {
  if (typeof window === "undefined") return;
  replaceUrl(withParam(window.location.href, LAYERS_PARAM, formatLayersParam(layers, choropleth)));
}
