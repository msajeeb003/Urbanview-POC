/**
 * UrbanView's layers over the Mapbox base style, drawn with the wireframe's values
 * (`docs/specs/frontend-design.md` §5 "Map styling"): zone fills by zone type at 24 % with a
 * coloured boundary and a centred label, dashed planning-document coverage, cadastral parcels
 * tinted by zone type, dashed planned parcels, block boundaries, the context overlays, and one
 * hover and one selection layer per clickable source-layer (filters set by `highlightFilters`).
 *
 * Source-layers are the publish catalogue's (`backend/jobs/publish_layers.py`). Outside coverage the
 * map is the base map alone (BRD S6): zones and cadastral parcels with `covered: false` are neither
 * filled, outlined, labelled, hovered nor selected, and the archive carries no heatmap cell or
 * block there. Inside coverage a heatmap cell without a value gets the "no data yet" hatch.
 */
import type { TilesCurrent } from "@/lib/api/types";
import { ZONE_TYPES, layerById, servedMinZoom, type ChoroplethState, type LayerId } from "@/lib/layers";

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

/** Nothing matches: the initial filter of hover / selection layers. */
export const NONE: Expr = ["==", ["get", "id"], -1];
const byId = (id: number | null): Expr => (id == null ? NONE : ["==", ["get", "id"], id]);
/** A covered feature by id: an uncovered cadastral parcel is never outlined (S6). */
const coveredById = (id: number | null): Expr => (id == null ? NONE : ["all", byId(id), COVERED]);


function layer(id: string, type: LayerSpec["type"], sourceLayer: string, rest: Partial<LayerSpec>): LayerSpec {
  return { id, type, source: SOURCE_ID, "source-layer": sourceLayer, ...rest };
}

/** Bottom to top. Line dash lengths are in line widths (the wireframe's SVG dashes ÷ stroke width). */
const BASE_LAYERS: readonly LayerSpec[] = [
  // zones (covered only): type colour at 24 %, boundary at 40 %
  layer("uv-zones-fill", "fill", "zones", { filter: COVERED, paint: { "fill-color": zoneColour, "fill-opacity": 0.24 } }),
  layer("uv-zones-line", "line", "zones", {
    filter: COVERED,
    paint: { "line-color": zoneColour, "line-width": 1, "line-opacity": 0.4 },
  }),
  // context overlays (off by default)
  layer("uv-landuse-fill", "fill", "land_use", {
    paint: {
      "fill-color": ["match", ["get", "category"], ...ZONE_TYPES.flatMap((z) => [z.key, z.hex]), "#b98a5a"],
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
  // urban blocks (their own card)
  layer("uv-blocks-line", "line", "urban_blocks", {
    paint: { "line-color": NEUTRAL, "line-width": 1, "line-dasharray": [3, 2.5], "line-opacity": 0.45 },
  }),
  // hover
  layer("uv-doc-hover-fill", "fill", "document_coverage", { filter: NONE, paint: { "fill-color": BRAND, "fill-opacity": 0.06 } }),
  layer("uv-doc-hover-line", "line", "document_coverage", { filter: NONE, paint: { "line-color": BRAND_DARK, "line-width": 2.8 } }),
  layer("uv-cad-hover-line", "line", "cadastral_parcels", { filter: NONE, paint: { "line-color": INK, "line-width": 1.2 } }),
  layer("uv-urban-hover-fill", "fill", "urban_parcels", { filter: NONE, paint: { "fill-color": BRAND, "fill-opacity": 0.14 } }),
  layer("uv-urban-hover-line", "line", "urban_parcels", { filter: NONE, paint: { "line-color": BRAND_DARK, "line-width": 2.6 } }),
  // selection
  layer("uv-zones-sel-line", "line", "zones", { filter: NONE, paint: { "line-color": BRAND, "line-width": 2.8 } }),
  layer("uv-doc-sel-fill", "fill", "document_coverage", { filter: NONE, paint: { "fill-color": BRAND, "fill-opacity": 0.07 } }),
  layer("uv-doc-sel-line", "line", "document_coverage", { filter: NONE, paint: { "line-color": BRAND, "line-width": 3.4 } }),
  layer("uv-cad-sel-glow", "line", "cadastral_parcels", {
    filter: NONE,
    paint: { "line-color": BRAND, "line-width": 6, "line-blur": 4, "line-opacity": 0.4 },
  }),
  layer("uv-cad-sel-line", "line", "cadastral_parcels", { filter: NONE, paint: { "line-color": BRAND, "line-width": 2.2 } }),
  layer("uv-urban-sel-fill", "fill", "urban_parcels", { filter: NONE, paint: { "fill-color": BRAND, "fill-opacity": 0.22 } }),
  layer("uv-urban-sel-glow", "line", "urban_parcels", {
    filter: NONE,
    paint: { "line-color": BRAND, "line-width": 6, "line-blur": 4, "line-opacity": 0.4 },
  }),
  layer("uv-urban-sel-line", "line", "urban_parcels", { filter: NONE, paint: { "line-color": BRAND, "line-width": 2.8 } }),
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

/** Layers the click handler queries, in selection priority: cadastral, planned, document, zone. */
export const HIT_LAYERS = {
  cadastral: "uv-cad-fill",
  urban: "uv-urban-fill",
  document: "uv-doc-fill",
  zone: "uv-zones-fill",
} as const;

/** Map layers each rail toggle shows or hides. Core layers are always visible. */
export const LAYER_GROUPS: Record<LayerId, readonly string[]> = {
  docareas: ["uv-doc-fill", "uv-doc-line", "uv-doc-hover-fill", "uv-doc-hover-line", "uv-doc-sel-fill", "uv-doc-sel-line"],
  base: [],
  zones: ["uv-zones-fill", "uv-zones-line", "uv-zones-sel-line", "uv-zones-label"],
  blocks: ["uv-blocks-line", "uv-blocks-label"],
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
  /** A selected zone gets an outline; zones have no hover outline (they cover whole quarters). */
  zone: number | null;
}

/** Filters of the hover and selection layers for the current highlight. */
export function highlightFilters(selected: Highlight, hover: Highlight): Record<string, Expr> {
  return {
    "uv-zones-sel-line": byId(selected.zone),
    "uv-doc-sel-fill": byId(selected.document),
    "uv-doc-sel-line": byId(selected.document),
    "uv-cad-sel-glow": coveredById(selected.cadastral),
    "uv-cad-sel-line": coveredById(selected.cadastral),
    "uv-urban-sel-fill": byId(selected.urban),
    "uv-urban-sel-glow": byId(selected.urban),
    "uv-urban-sel-line": byId(selected.urban),
    "uv-doc-hover-fill": byId(hover.document === selected.document ? null : hover.document),
    "uv-doc-hover-line": byId(hover.document === selected.document ? null : hover.document),
    "uv-cad-hover-line": coveredById(hover.cadastral === selected.cadastral ? null : hover.cadastral),
    "uv-urban-hover-fill": byId(hover.urban === selected.urban ? null : hover.urban),
    "uv-urban-hover-line": byId(hover.urban === selected.urban ? null : hover.urban),
  };
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
