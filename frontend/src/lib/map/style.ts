/**
 * UrbanView's layers over the Mapbox base style, drawn with the wireframe's values
 * (`docs/specs/frontend-design.md` §5 "Map styling"): zone fills by zone type at 24 % with a
 * coloured boundary and a centred label, dashed planning-document coverage, cadastral parcels
 * tinted by zone type, dashed planned parcels, block boundaries, the context overlays, and one
 * hover and one selection layer per clickable source-layer, drawn for the flagged feature only
 * (feature-state, `highlightStates`: a paint change, never a tile re-layout).
 *
 * Source-layers are the publish catalogue's (`backend/jobs/publish_layers.py`). Outside coverage the
 * map is the base map alone (BRD S6): zones and cadastral parcels with `covered: false` are neither
 * filled, outlined, labelled, hovered nor selected, and the archive carries no heatmap cell or
 * block there. Inside coverage a heatmap cell without a value gets the "no data yet" hatch.
 */
import type { TilesCurrent } from "@/lib/api/types";
import { BASEMAP_COLOURS } from "@/lib/map/basemap";
import {
  LAND_USE_OTHER_COLOUR,
  ZONE_TYPES,
  layerById,
  servedMinZoom,
  type ChoroplethState,
  type LayerId,
} from "@/lib/layers";

import {
  PARAM_METRICS,
  hasValue,
  heatSourceLayer,
  noValue,
  paramScheme,
  priceScheme,
  type CellClasses,
} from "./classes";

import { SOURCE_ID } from "./tiles";

type Expr = unknown[];
export interface LayerSpec {
  id: string;
  type: "fill" | "line" | "symbol";
  source: string;
  "source-layer": string;
  minzoom?: number;
  filter?: Expr;
  layout?: Record<string, unknown>;
  paint?: Record<string, unknown>;
  /**
   * Where the layer goes in the base style: under its labels (the default: above the roads) or
   * right above its buildings (`above-buildings`: under the roads, so the streets stay on top).
   */
  placement?: "above-buildings";
}

const NEUTRAL = "#B3A894";
const BRAND = "#B5613B";
const BRAND_DARK = "#8F472A";
const INK = "#2A2118";
export const HATCH_IMAGE = "uv-no-data";
const LABEL_FONT = ["DIN Pro Medium", "Arial Unicode MS Regular"];
const LABEL_FONT_BOLD = ["DIN Pro Bold", "Arial Unicode MS Bold"];

/** Zone-type colour of a feature (`zone_type` property), neutral when unclassified. */
export const zoneColour: Expr = [
  "match",
  ["get", "zone_type"],
  ...ZONE_TYPES.flatMap((z) => [z.key, z.hex]),
  NEUTRAL,
];

/**
 * Only covered zones and cadastral parcels are drawn: outside coverage the map shows the base map
 * alone (POC plan). Features published before the `covered` flag existed count as covered.
 */
const COVERED: Expr = ["!=", ["get", "covered"], false];

/** Nothing matches: the filter of a heatmap pair whose field is not the selected one. */
export const NONE: Expr = ["==", ["get", "id"], -1];

/**
 * Highlights are feature-state flags, not filters (2026-10-13, the client's review: "navigation
 * could be smoother"). A filter change makes Mapbox re-process the layer in every loaded tile on
 * each crossing of the cursor; a feature-state change touches the paint buffers only. The hover
 * and selection layers are always present and draw every feature at opacity 0 except the flagged
 * one.
 */
const FLAG = (flag: "hover" | "selected"): Expr => ["boolean", ["feature-state", flag], false];
/** Drawn for the hovered feature, unless it is the selected one (the selection layers show it). */
const hovered = (value: number): Expr => ["case", ["all", FLAG("hover"), ["!", FLAG("selected")]], value, 0];
/** Drawn for the selected feature. */
const selected = (value: number): Expr => ["case", FLAG("selected"), value, 0];


function layer(id: string, type: LayerSpec["type"], sourceLayer: string, rest: Partial<LayerSpec>): LayerSpec {
  return { id, type, source: SOURCE_ID, "source-layer": sourceLayer, ...rest };
}

/** The base map's buildings appear from this zoom (Mapbox's light style). */
export const BUILDINGS_MIN_ZOOM = 15;

/** Bottom to top. Line dash lengths are in line widths (the wireframe's SVG dashes ÷ stroke width). */
const BASE_LAYERS: readonly LayerSpec[] = [
  // The plan's own ground (2026-10-13, the client's review): inside a plan whose drawing states its
  // existing buildings, the base map's OpenStreetMap buildings (2-3 m off the planned parcels, no
  // rigid shift fits them all) are painted over in the land colour and the plan's own footprints
  // drawn instead, in the base map's building colours, so parcels and buildings come from one
  // drawing and match. Both sit right above the base style's `building` layer (`placement`), so the
  // streets and labels stay on top and water is moved back over the ground (`installTiles`).
  layer("uv-plan-ground", "fill", "plan_buildings", {
    minzoom: BUILDINGS_MIN_ZOOM,
    filter: ["==", ["get", "kind"], "ground"],
    placement: "above-buildings",
    paint: { "fill-color": BASEMAP_COLOURS.land, "fill-opacity": 1 },
  }),
  layer("uv-plan-buildings", "fill", "plan_buildings", {
    minzoom: BUILDINGS_MIN_ZOOM,
    filter: ["==", ["get", "kind"], "building"],
    placement: "above-buildings",
    paint: { "fill-color": BASEMAP_COLOURS.building, "fill-opacity": 0.75, "fill-outline-color": BASEMAP_COLOURS.roadCase },
  }),
  // zones (covered only): type colour at 24 %, boundary at 40 %
  layer("uv-zones-fill", "fill", "zones", { filter: COVERED, paint: { "fill-color": zoneColour, "fill-opacity": 0.24 } }),
  layer("uv-zones-line", "line", "zones", {
    filter: COVERED,
    paint: { "line-color": zoneColour, "line-width": 1, "line-opacity": 0.4 },
  }),
  // context overlays (off by default)
  layer("uv-landuse-fill", "fill", "land_use", {
    paint: {
      "fill-color": ["match", ["get", "category"], ...ZONE_TYPES.flatMap((z) => [z.key, z.hex]), LAND_USE_OTHER_COLOUR],
      "fill-opacity": 0.32,
    },
  }),
  // choropleths: colours and filters are set from the served classes (`choroplethStyle`); cells
  // without a value get the "no data" hatch, never the lowest class
  // one source-layer per planning field; only the selected field's pair is drawn (filters)
  ...PARAM_METRICS.flatMap((m) => [
    layer(`uv-heat-${m.layer}-fill`, "fill", heatSourceLayer(m.layer), {
      filter: m.key === "max_far" ? hasValue("value") : NONE,
      paint: { "fill-color": paramScheme(m.key, null).fillColor, "fill-opacity": 0.78 },
    }),
    layer(`uv-heat-${m.layer}-nodata`, "fill", heatSourceLayer(m.layer), {
      filter: m.key === "max_far" ? noValue("value") : NONE,
      paint: { "fill-pattern": HATCH_IMAGE, "fill-opacity": 0.8 },
    }),
  ]),
  layer("uv-heatmkt-fill", "fill", heatSourceLayer("sale_price"), {
    filter: hasValue("value"),
    paint: { "fill-color": priceScheme("expected", null).fillColor, "fill-opacity": 1 },
  }),
  layer("uv-heatmkt-nodata", "fill", heatSourceLayer("sale_price"), {
    filter: noValue("value"),
    paint: { "fill-pattern": HATCH_IMAGE, "fill-opacity": 0.8 },
  }),
  // planning-document coverage (core, selectable): invisible fill as the click target
  layer("uv-doc-fill", "fill", "document_coverage", { paint: { "fill-color": BRAND, "fill-opacity": 0 } }),
  layer("uv-doc-line", "line", "document_coverage", {
    paint: { "line-color": NEUTRAL, "line-width": 2.8, "line-dasharray": [3.57, 1.79], "line-opacity": 0.5 },
  }),
  // cadastral parcels: tinted by their zone's type, hairline outline
  layer("uv-cad-fill", "fill", "cadastral_parcels", {
    filter: COVERED,
    paint: { "fill-color": zoneColour, "fill-opacity": 0.5 },
  }),
  layer("uv-cad-line", "line", "cadastral_parcels", {
    filter: COVERED,
    paint: { "line-color": "rgba(42,33,24,0.2)", "line-width": 0.6 },
  }),
  // planned (urban) parcels: dashed brand-dark outline, invisible fill as the click target
  layer("uv-urban-fill", "fill", "urban_parcels", { paint: { "fill-color": BRAND, "fill-opacity": 0 } }),
  layer("uv-urban-line", "line", "urban_parcels", {
    paint: { "line-color": BRAND_DARK, "line-width": 1.6, "line-dasharray": [2.5, 1.875] },
  }),
  // urban blocks (their own card): invisible fill as the click target (the block panel), the
  // dotted boundary
  layer("uv-blocks-fill", "fill", "urban_blocks", { paint: { "fill-color": NEUTRAL, "fill-opacity": 0 } }),
  layer("uv-blocks-line", "line", "urban_blocks", {
    paint: { "line-color": NEUTRAL, "line-width": 1, "line-dasharray": [3, 2.5], "line-opacity": 0.45 },
  }),
  // hover (the flagged feature only; an uncovered cadastral parcel is never outlined, S6)
  layer("uv-doc-hover-fill", "fill", "document_coverage", { paint: { "fill-color": BRAND, "fill-opacity": hovered(0.06) } }),
  layer("uv-doc-hover-line", "line", "document_coverage", { paint: { "line-color": BRAND_DARK, "line-width": 2.8, "line-opacity": hovered(1) } }),
  layer("uv-cad-hover-line", "line", "cadastral_parcels", { filter: COVERED, paint: { "line-color": INK, "line-width": 1.2, "line-opacity": hovered(1) } }),
  layer("uv-blocks-hover-line", "line", "urban_blocks", {
    paint: { "line-color": INK, "line-width": 1.4, "line-dasharray": [3, 2.5], "line-opacity": hovered(0.7) },
  }),
  layer("uv-urban-hover-fill", "fill", "urban_parcels", { paint: { "fill-color": BRAND, "fill-opacity": hovered(0.14) } }),
  layer("uv-urban-hover-line", "line", "urban_parcels", { paint: { "line-color": BRAND_DARK, "line-width": 2.6, "line-opacity": hovered(1) } }),
  // selection (the flagged feature only)
  layer("uv-zones-sel-line", "line", "zones", { paint: { "line-color": BRAND, "line-width": 2.8, "line-opacity": selected(1) } }),
  layer("uv-blocks-sel-line", "line", "urban_blocks", { paint: { "line-color": BRAND, "line-width": 2.6, "line-opacity": selected(1) } }),
  layer("uv-doc-sel-fill", "fill", "document_coverage", { paint: { "fill-color": BRAND, "fill-opacity": selected(0.07) } }),
  layer("uv-doc-sel-line", "line", "document_coverage", { paint: { "line-color": BRAND, "line-width": 3.4, "line-opacity": selected(1) } }),
  layer("uv-cad-sel-glow", "line", "cadastral_parcels", {
    filter: COVERED,
    paint: { "line-color": BRAND, "line-width": 6, "line-blur": 4, "line-opacity": selected(0.4) },
  }),
  layer("uv-cad-sel-line", "line", "cadastral_parcels", { filter: COVERED, paint: { "line-color": BRAND, "line-width": 2.2, "line-opacity": selected(1) } }),
  layer("uv-urban-sel-fill", "fill", "urban_parcels", { paint: { "fill-color": BRAND, "fill-opacity": selected(0.22) } }),
  layer("uv-urban-sel-glow", "line", "urban_parcels", {
    paint: { "line-color": BRAND, "line-width": 6, "line-blur": 4, "line-opacity": selected(0.4) },
  }),
  layer("uv-urban-sel-line", "line", "urban_parcels", { paint: { "line-color": BRAND, "line-width": 2.8, "line-opacity": selected(1) } }),
  // labels on top
  layer("uv-blocks-label", "symbol", "urban_blocks", {
    minzoom: 15,
    layout: { "text-field": ["get", "block_ref"], "text-font": LABEL_FONT_BOLD, "text-size": 10, "text-allow-overlap": false },
    paint: { "text-color": "rgba(42,33,24,0.5)", "text-halo-color": "rgba(255,255,255,0.6)", "text-halo-width": 1 },
  }),
  layer("uv-zones-label", "symbol", "zone_labels", {
    filter: COVERED,
    layout: {
      "text-field": ["get", "name"],
      "text-font": LABEL_FONT,
      "text-size": 13,
      "text-allow-overlap": false,
    },
    paint: { "text-color": "rgba(42,33,24,0.5)", "text-halo-color": "rgba(255,255,255,0.6)", "text-halo-width": 1 },
  }),
];

/** Layers the click handler queries, in selection priority: cadastral, planned, block, document, zone. */
export const HIT_LAYERS = {
  cadastral: "uv-cad-fill",
  urban: "uv-urban-fill",
  block: "uv-blocks-fill",
  document: "uv-doc-fill",
  zone: "uv-zones-fill",
} as const;

/** Map layers each rail toggle shows or hides. Core layers are always visible. */
export const LAYER_GROUPS: Record<LayerId, readonly string[]> = {
  docareas: ["uv-doc-fill", "uv-doc-line", "uv-doc-hover-fill", "uv-doc-hover-line", "uv-doc-sel-fill", "uv-doc-sel-line"],
  base: ["uv-plan-ground", "uv-plan-buildings"],
  zones: ["uv-zones-fill", "uv-zones-line", "uv-zones-sel-line", "uv-zones-label"],
  blocks: ["uv-blocks-fill", "uv-blocks-line", "uv-blocks-hover-line", "uv-blocks-sel-line", "uv-blocks-label"],
  cadastre: ["uv-cad-fill", "uv-cad-line", "uv-cad-hover-line", "uv-cad-sel-glow", "uv-cad-sel-line"],
  planned: ["uv-urban-fill", "uv-urban-line", "uv-urban-hover-fill", "uv-urban-hover-line", "uv-urban-sel-fill", "uv-urban-sel-glow", "uv-urban-sel-line"],
  landuse: ["uv-landuse-fill"],
  heatFAR: PARAM_METRICS.flatMap((m) => [`uv-heat-${m.layer}-fill`, `uv-heat-${m.layer}-nodata`]),
  heatMkt: ["uv-heatmkt-fill", "uv-heatmkt-nodata"],
};

const OWNER = new Map<string, LayerId>(
  (Object.entries(LAYER_GROUPS) as [LayerId, readonly string[]][]).flatMap(([id, ls]) => ls.map((l) => [l, id] as const)),
);

/**
 * UrbanView's map layers, bottom to top, for a published version: each starts at the later of its
 * own minimum zoom (labels) and the zoom its source-layer is built from — the tile pointer's
 * per-layer range when it lists the source-layer, else the registry entry's (`LAYERS[].minZoom`,
 * the catalogue's range). Nothing is in the tiles below it, and the rail says "zoom in to see" with
 * the same number (`layerMinZoom`), so card and map never disagree.
 */
export function uvLayers(tiles?: TilesCurrent | null): LayerSpec[] {
  return BASE_LAYERS.map((l) => {
    const owner = OWNER.get(l.id);
    const built = servedMinZoom(l["source-layer"], tiles) ?? (owner ? layerById(owner).minZoom : 0);
    const minzoom = Math.max(l.minzoom ?? 0, built);
    return minzoom > 0 ? { ...l, minzoom } : l;
  });
}

/** The layers with the catalogue's zoom ranges (no pointer). */
export const UV_LAYERS: readonly LayerSpec[] = uvLayers(null);

/** Which UrbanView layers are visible for the rail state. */
export function visibleLayerIds(layers: Record<LayerId, boolean>): Set<string> {
  const on = new Set<string>();
  for (const [id, mapLayers] of Object.entries(LAYER_GROUPS) as [LayerId, readonly string[]][]) {
    if (layers[id]) mapLayers.forEach((l) => on.add(l));
  }
  return on;
}

/**
 * Paint and filters of the two choropleths for the selected fields and the served classes:
 * applied with `setPaintProperty` / `setFilter`, never by reloading the source.
 */
export function choroplethStyle(
  choropleth: ChoroplethState,
  classes: CellClasses | null | undefined,
): Record<string, { filter: Expr; paint?: Record<string, unknown> }> {
  const out: Record<string, { filter: Expr; paint?: Record<string, unknown> }> = {};
  for (const m of PARAM_METRICS) {
    if (m.key !== choropleth.param) {
      out[`uv-heat-${m.layer}-fill`] = { filter: NONE };
      out[`uv-heat-${m.layer}-nodata`] = { filter: NONE };
      continue;
    }
    const param = paramScheme(m.key, classes?.[m.layer]);
    out[`uv-heat-${m.layer}-fill`] = { filter: hasValue(param.column), paint: { "fill-color": param.fillColor } };
    out[`uv-heat-${m.layer}-nodata`] = { filter: noValue(param.column) };
  }
  const price = priceScheme(choropleth.price, classes?.sale_price);
  out["uv-heatmkt-fill"] = { filter: hasValue(price.column), paint: { "fill-color": price.fillColor } };
  out["uv-heatmkt-nodata"] = { filter: noValue(price.column) };
  return out;
}

export interface Highlight {
  cadastral: number | null;
  urban: number | null;
  document: number | null;
  block: number | null;
  /** A selected zone gets an outline; zones have no hover outline (they cover whole quarters). */
  zone: number | null;
}

/** The source-layer each highlight kind flags its feature in. */
export const HIGHLIGHT_SOURCE_LAYERS: Record<keyof Highlight, string> = {
  cadastral: "cadastral_parcels",
  urban: "urban_parcels",
  document: "document_coverage",
  block: "urban_blocks",
  zone: "zones",
};

export interface FeatureFlags {
  sourceLayer: string;
  id: number;
  hover: boolean;
  selected: boolean;
}

/**
 * The feature-state flags the current highlight needs, keyed `<source-layer>:<id>`: the selected
 * features (a cadastral selection also flags its planned parcel) and the hovered ones. The map
 * sets exactly these and removes the flags of every feature no longer listed (`syncHighlight`).
 */
export function highlightStates(selected: Highlight, hover: Highlight): Record<string, FeatureFlags> {
  const out: Record<string, FeatureFlags> = {};
  const flag = (kind: keyof Highlight, id: number | null, which: "hover" | "selected") => {
    if (id == null) return;
    const sourceLayer = HIGHLIGHT_SOURCE_LAYERS[kind];
    const key = `${sourceLayer}:${id}`;
    const cur = out[key] ?? { sourceLayer, id, hover: false, selected: false };
    cur[which] = true;
    out[key] = cur;
  };
  for (const kind of Object.keys(HIGHLIGHT_SOURCE_LAYERS) as (keyof Highlight)[]) {
    flag(kind, selected[kind], "selected");
    flag(kind, hover[kind], "hover");
  }
  return out;
}

/**
 * Where a layer goes when it is added later than its neighbours (a card switched on): before the
 * first layer after it in catalogue order that is on the map and sits in the same band (under the
 * base map's labels, or above its buildings); with none, at that band's own anchor. Our own label
 * layers go on top (`undefined`). Returns the layer id to insert before, or `undefined` for the
 * top.
 */
export function insertBefore(
  order: readonly LayerSpec[],
  index: number,
  present: (id: string) => boolean,
  anchors: { labels: string | undefined; aboveBuildings: string | undefined },
): string | undefined {
  const l = order[index];
  if (l.type === "symbol") return undefined;
  const band = l.placement ?? "labels";
  for (let j = index + 1; j < order.length; j++) {
    const m = order[j];
    if (m.type === "symbol" || (m.placement ?? "labels") !== band) continue;
    if (present(m.id)) return m.id;
  }
  return band === "above-buildings" ? anchors.aboveBuildings : anchors.labels;
}

/** 8 px diagonal hatch in the wireframe's muted outline colour: the "no data yet" pattern. */
export function hatchImage(size = 8): { width: number; height: number; data: Uint8Array } {
  const data = new Uint8Array(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      if ((x + y) % size === 0 || (x + y) % size === size - 1) {
        const i = (y * size + x) * 4;
        data[i] = 0xb3;
        data[i + 1] = 0xa8;
        data[i + 2] = 0x94;
        data[i + 3] = 160;
      }
    }
  }
  return { width: size, height: size, data };
}
