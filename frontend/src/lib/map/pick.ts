/**
 * What a click on the map selects. `queryRenderedFeatures` on the three hit layers returns the
 * features under the pointer; the selection priority is cadastral parcel, then planned (urban)
 * parcel, then planning-document coverage area. Pure functions, tested without a map.
 */
import type { LngLat } from "@/lib/store";

import { HIT_LAYERS } from "./style";

export type PickType = keyof typeof HIT_LAYERS;

export interface RenderedFeature {
  layer?: { id: string } | null;
  properties?: Record<string, unknown> | null;
  geometry?: { type: string; coordinates: unknown } | null;
}

export interface Pick {
  type: PickType;
  id: number;
  zoneId: number | null;
  /** Primary planned parcel of a cadastral parcel (tile property), highlighted with it. */
  linkedUrbanId: number | null;
  /** Centre of the rendered geometry: where the pin goes until the API gives the centroid. */
  centre: LngLat | null;
  properties: Record<string, unknown>;
}

const PRIORITY: PickType[] = ["cadastral", "urban", "document"];
const LAYER_TO_TYPE = new Map<string, PickType>(Object.entries(HIT_LAYERS).map(([t, l]) => [l, t as PickType]));

const asInt = (v: unknown): number | null => (typeof v === "number" && Number.isInteger(v) && v > 0 ? v : null);

export function pickFeature(features: readonly RenderedFeature[]): Pick | null {
  for (const type of PRIORITY) {
    const f = features.find((x) => x.layer && LAYER_TO_TYPE.get(x.layer.id) === type);
    const props = (f?.properties ?? {}) as Record<string, unknown>;
    const id = asInt(props.id);
    if (!f || id == null) continue;
    return {
      type,
      id,
      zoneId: asInt(props.zone_id),
      linkedUrbanId: type === "cadastral" ? asInt(props.primary_urban_parcel_id) : null,
      centre: geometryCentre(f.geometry ?? null),
      properties: props,
    };
  }
  return null;
}

/**
 * Area-weighted centroid of the largest polygon of a (Multi)Polygon, falling back to the bounding
 * box centre. Rendered geometry may be clipped at tile edges, so this is only the first guess.
 */
export function geometryCentre(geometry: { type: string; coordinates: unknown } | null): LngLat | null {
  if (!geometry) return null;
  const polygons =
    geometry.type === "Polygon"
      ? [geometry.coordinates as number[][][]]
      : geometry.type === "MultiPolygon"
        ? (geometry.coordinates as number[][][][])
        : null;
  if (!polygons || polygons.length === 0) return null;
  let best: { area: number; lng: number; lat: number } | null = null;
  for (const poly of polygons) {
    const ring = poly[0];
    if (!ring || ring.length < 3) continue;
    let a = 0;
    let cx = 0;
    let cy = 0;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [x0, y0] = ring[j];
      const [x1, y1] = ring[i];
      const cross = x0 * y1 - x1 * y0;
      a += cross;
      cx += (x0 + x1) * cross;
      cy += (y0 + y1) * cross;
    }
    if (a === 0) {
      const xs = ring.map((p) => p[0]);
      const ys = ring.map((p) => p[1]);
      const c = { area: 0, lng: (Math.min(...xs) + Math.max(...xs)) / 2, lat: (Math.min(...ys) + Math.max(...ys)) / 2 };
      if (!best) best = c;
      continue;
    }
    const area = Math.abs(a / 2);
    if (!best || area > best.area) best = { area, lng: cx / (3 * a), lat: cy / (3 * a) };
  }
  return best ? { lng: best.lng, lat: best.lat } : null;
}
