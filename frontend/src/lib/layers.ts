/**
 * The layer registry: one entry per rail card, in the wireframe's rail order and groups
 * (`docs/wireframe/wireframe.js` → `LAYERS`). Each entry says which source-layers of the published
 * PMTiles archive it draws (`backend/jobs/publish_layers.py` → `LAYERS`), its minimum zoom, its
 * default, its rules (core, dependency, paid, choropleth) and its legend. The map style for each
 * entry is in `lib/map/style.ts` (`LAYER_GROUPS`), keyed by the same ids.
 *
 * Cadastral and urban (planned) parcels are separate layers and are never merged. Names, group
 * titles and legend text shown on screen come from the string table (`layer.<id>`, `group.<id>`,
 * `legend.*`, `zoneType.*`) through the legend context's translator; `name` is the English one.
 */
import type { TilesCurrent } from "@/lib/api/types";
import { translate, type Translate } from "@/lib/i18n/strings";
import {
  paramScheme,
  priceMetric,
  priceScheme,
  type CellClasses,
  type ParamMetric,
  type PriceMetric,
} from "@/lib/map/classes";

export type LayerGroup = "base" | "parcels" | "context" | "feas";

export type Swatch =
  | { kind: "color"; color: string }
  | { kind: "zones" }
  | { kind: "dash" }
  | { kind: "docdash" }
  | { kind: "blockdash" }
  | { kind: "heat1" }
  | { kind: "heat2" };

export type LayerId =
  | "docareas"
  | "base"
  | "zones"
  | "cadastre"
  | "planned"
  | "owner"
  | "restit"
  | "landuse"
  | "heatFAR"
  | "traffic"
  | "heatMkt";

export type LegendMark =
  | { kind: "color"; color: string }
  | { kind: "line"; color: string }
  | { kind: "grad"; stops: string }
  | { kind: "dash" }
  | { kind: "docdash" }
  | { kind: "blockdash" }
  | { kind: "cadsw" }
  | { kind: "hatch" };

export interface LegendGroup {
  title: string;
  unit?: string;
  rows: { mark: LegendMark; label: string }[];
}

export interface ChoroplethState {
  param: ParamMetric;
  price: PriceMetric;
}

export interface LegendContext {
  layers: Record<LayerId, boolean>;
  marketUnlocked: boolean;
  choropleth: ChoroplethState;
  classes: CellClasses | null | undefined;
  /** The shell's translator (English when absent). */
  t?: Translate;
}

export interface LayerDef {
  id: LayerId;
  name: string;
  group: LayerGroup;
  defaultOn: boolean;
  /** Always visible; the card shows the muted check and only toasts on click. */
  core?: boolean;
  /** Needs another layer to be visible (it shades that layer's features). */
  requires?: LayerId;
  /** Part of the market-data tier: locked card until the market entitlement is on. */
  paid?: boolean;
  /** Choropleth card: shows a field selector while on; only one choropleth is on at a time. */
  choropleth?: "param" | "price";
  swatch: Swatch;
  /** Source-layers in the published tile archive; empty for the Mapbox base style. */
  published: string[];
  /** Lowest zoom the archive has features for (the catalogue's range). */
  minZoom: number;
  legend: (ctx: LegendContext) => LegendGroup | null;
}

export const GROUP_LABEL: Record<LayerGroup, string> = {
  base: "Base",
  parcels: "Parcels",
  context: "Context",
  feas: "Feasibility",
};

/** Zone types (wireframe `ZTYPES`): legend rows, zone fills, land-use colours. */
export const ZONE_TYPES = [
  { key: "res", name: "Residential", token: "--z-res", hex: "#B5744A" },
  { key: "com", name: "Commercial", token: "--z-com", hex: "#BE9A44" },
  { key: "mix", name: "Mixed use", token: "--z-mix", hex: "#8A7A8E" },
  { key: "pub", name: "Public / institutional", token: "--z-pub", hex: "#5E8A82" },
  { key: "grn", name: "Green / recreation", token: "--z-grn", hex: "#7C8A4F" },
] as const;

const english: Translate = (key, vars) => translate("en", key, vars);
const tx = (ctx: LegendContext): Translate => ctx.t ?? english;
const title = (ctx: LegendContext, id: LayerId) => tx(ctx)(`layer.${id}`);
const zoneRows = (ctx: LegendContext) =>
  ZONE_TYPES.map((z) => ({ mark: { kind: "color" as const, color: z.hex }, label: tx(ctx)(`zoneType.${z.key}`) }));
const needsCadastre = (ctx: LegendContext) => (ctx.layers.cadastre ? "" : tx(ctx)("legend.needsCadastre"));
const noDataRow = (ctx: LegendContext) => ({ mark: { kind: "hatch" as const }, label: tx(ctx)("legend.noData") });

export const LAYERS: readonly LayerDef[] = [
  {
    id: "docareas",
    name: "Planning documents",
    group: "base",
    defaultOn: true,
    core: true,
    swatch: { kind: "docdash" },
    published: ["document_coverage"],
    minZoom: 9,
    legend: (ctx) => ({ title: title(ctx, "docareas"), rows: [{ mark: { kind: "docdash" }, label: tx(ctx)("legend.coverageArea") }] }),
  },
  {
    id: "base",
    name: "Base map",
    group: "base",
    defaultOn: true,
    core: true,
    swatch: { kind: "color", color: "#c4bdac" },
    published: [],
    minZoom: 0,
    legend: () => null,
  },
  {
    id: "zones",
    name: "Urban zones",
    group: "base",
    defaultOn: true,
    core: true,
    swatch: { kind: "zones" },
    published: ["zones", "urban_blocks", "zone_labels"],
    minZoom: 8,
    legend: (ctx) => ({
      title: title(ctx, "zones"),
      rows: [...zoneRows(ctx), { mark: { kind: "blockdash" }, label: tx(ctx)("legend.blockBoundary") }],
    }),
  },
  {
    id: "cadastre",
    name: "Cadastral parcels",
    group: "parcels",
    defaultOn: true,
    swatch: { kind: "color", color: "#B3A894" },
    published: ["cadastral_parcels"],
    minZoom: 13,
    legend: (ctx) => ({ title: title(ctx, "cadastre"), rows: [{ mark: { kind: "cadsw" }, label: tx(ctx)("legend.parcelOutline") }] }),
  },
  {
    id: "planned",
    name: "Urban parcels",
    group: "parcels",
    defaultOn: true,
    swatch: { kind: "dash" },
    published: ["urban_parcels"],
    minZoom: 13,
    legend: (ctx) => ({ title: title(ctx, "planned"), rows: [{ mark: { kind: "dash" }, label: tx(ctx)("legend.parcelOpen") }] }),
  },
  {
    id: "owner",
    name: "Public ownership",
    group: "parcels",
    defaultOn: false,
    requires: "cadastre",
    swatch: { kind: "color", color: "#4F6D82" },
    published: ["public_ownership"],
    minZoom: 13,
    legend: (ctx) => ({
      title: title(ctx, "owner"),
      rows: [{ mark: { kind: "color", color: "#4F6D82" }, label: `${tx(ctx)("legend.publicOwned")}${needsCadastre(ctx)}` }],
    }),
  },
  {
    id: "restit",
    name: "Restitution / legal",
    group: "parcels",
    defaultOn: false,
    requires: "cadastre",
    swatch: { kind: "color", color: "#9E5568" },
    published: ["legal_burdens"],
    minZoom: 13,
    legend: (ctx) => ({
      title: title(ctx, "restit"),
      rows: [{ mark: { kind: "color", color: "#9E5568" }, label: `${tx(ctx)("legend.legalClaim")}${needsCadastre(ctx)}` }],
    }),
  },
  {
    id: "landuse",
    name: "Land use",
    group: "context",
    defaultOn: false,
    swatch: { kind: "color", color: "#b98a5a" },
    published: ["land_use"],
    minZoom: 10,
    legend: (ctx) => ({ title: title(ctx, "landuse"), rows: zoneRows(ctx) }),
  },
  {
    id: "heatFAR",
    name: "FAR heatmap",
    group: "context",
    defaultOn: false,
    choropleth: "param",
    swatch: { kind: "heat1" },
    published: ["block_cells"],
    minZoom: 10,
    legend: (ctx) => {
      const metric = ctx.choropleth.param;
      const classes = ctx.classes?.block_cells?.[metric];
      const scheme = paramScheme(metric, classes);
      const t = tx(ctx);
      const rows: LegendGroup["rows"] = scheme.classed
        ? scheme.rows.map((r) => ({ mark: { kind: "color", color: r.colour }, label: r.label }))
        : [{ mark: { kind: "grad", stops: "#F1E7D6,#B5613B" }, label: t("legend.lowHigh") }];
      if (classes && classes.null_count > 0) rows.push(noDataRow(ctx));
      return { title: t(`metric.${metric}.legend`), unit: t(`metric.${metric}.unit`), rows };
    },
  },
  {
    id: "traffic",
    name: "Planned traffic",
    group: "context",
    defaultOn: false,
    swatch: { kind: "color", color: "#5b5b5b" },
    published: ["traffic_network"],
    minZoom: 10,
    legend: (ctx) => ({ title: title(ctx, "traffic"), rows: [{ mark: { kind: "line", color: "#5b5b5b" }, label: tx(ctx)("legend.plannedRoute") }] }),
  },
  {
    id: "heatMkt",
    name: "Price heatmap",
    group: "feas",
    defaultOn: false,
    paid: true,
    choropleth: "price",
    swatch: { kind: "heat2" },
    published: ["zone_cells"],
    minZoom: 10,
    legend: (ctx) => {
      if (!ctx.marketUnlocked) return null;
      const served = ctx.classes?.zone_cells?.[priceMetric(ctx.choropleth.price).column];
      const t = tx(ctx);
      const words = { notSaleable: t("legend.notSaleable"), under: t("legend.priceUnder"), above: t("legend.priceAbove") };
      const rows: LegendGroup["rows"] = priceScheme(ctx.choropleth.price, served, words).rows.map((r) => ({
        mark: { kind: "color", color: r.colour },
        label: r.label,
      }));
      if (served && served.null_count > 0) rows.push(noDataRow(ctx));
      return { title: title(ctx, "heatMkt"), unit: t("legend.priceUnit"), rows };
    },
  },
];

export const layerById = (id: LayerId): LayerDef => LAYERS.find((l) => l.id === id)!;

export const DEFAULT_LAYER_STATE = Object.fromEntries(LAYERS.map((l) => [l.id, l.defaultOn])) as Record<LayerId, boolean>;
export const DEFAULT_CHOROPLETH: ChoroplethState = { param: "max_far", price: "expected" };

/** The value `layer_toggled.layer_id` carries: the published layer key. */
export const analyticsLayerId = (l: LayerDef): string => l.published[0] ?? l.id;

/** Whether a card's layer is drawn: on, its requirement on, and (paid) the entitlement held. */
export function isDrawn(id: LayerId, layers: Record<LayerId, boolean>, marketUnlocked: boolean): boolean {
  const l = layerById(id);
  if (!layers[id]) return false;
  if (l.requires && !layers[l.requires]) return false;
  if (l.paid && !marketUnlocked) return false;
  return true;
}

/**
 * Toggle one card. Only one choropleth is on at a time (a readable map): switching one on
 * switches the other off, reported in `switchedOff`. Core layers never change.
 */
export function toggleLayer(
  layers: Record<LayerId, boolean>,
  id: LayerId,
): { layers: Record<LayerId, boolean>; on: boolean; switchedOff: LayerId | null } {
  const def = layerById(id);
  if (def.core) return { layers, on: true, switchedOff: null };
  const on = !layers[id];
  const next = { ...layers, [id]: on };
  let switchedOff: LayerId | null = null;
  if (on && def.choropleth) {
    for (const other of LAYERS) {
      if (other.choropleth && other.id !== id && next[other.id]) {
        next[other.id] = false;
        switchedOff = other.id;
      }
    }
  }
  return { layers: next, on, switchedOff };
}

/**
 * Nothing published for the layer: the pointer lists every one of its source-layers with 0
 * features (a layer missing from the list was left out of the build for the same reason). Only
 * said of a published archive, and never of the base map.
 */
export function hasNoData(layer: LayerDef, tiles: TilesCurrent | undefined): boolean {
  if (!tiles || tiles.status !== "published" || !tiles.archive_url || layer.published.length === 0) return false;
  const counts = new Map((tiles.layers ?? []).map((l) => [l.id, l.features]));
  return layer.published.every((id) => !counts.get(id));
}

/** Legend groups for the drawn layers, in rail order; empty = "No overlays active". */
export function legendGroups(ctx: LegendContext): LegendGroup[] {
  return LAYERS.filter((l) => ctx.layers[l.id] && (!l.paid || ctx.marketUnlocked))
    .map((l) => l.legend(ctx))
    .filter((g): g is LegendGroup => !!g);
}
