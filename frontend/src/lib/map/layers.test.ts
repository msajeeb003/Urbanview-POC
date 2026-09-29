import { describe, expect, it } from "vitest";

import type { TilesCurrent } from "@/lib/api/types";
import {
  DEFAULT_CHOROPLETH,
  DEFAULT_LAYER_STATE,
  LAYERS,
  layerById,
  layerMinZoom,
  layerState,
  layerStatesKey,
  legendGroups,
  parseLayerStates,
  toggleLayer,
  type LayerView,
  type LegendContext,
} from "@/lib/layers";
import { formatLayersParam, parseLayersParam, withParcelParam } from "@/lib/url-state";

import {
  NOT_SALEABLE_COLOUR,
  gradientColours,
  paramScheme,
  priceScheme,
  type MetricClasses,
} from "./classes";
import { fitOptions, fitPadding } from "./camera";
import { LAYER_GROUPS, NONE, UV_LAYERS, choroplethStyle, uvLayers } from "./style";

/** Minimal evaluator for the colour expressions the schemes build (step / case / to-number / get). */
function evaluate(expr: unknown, props: Record<string, number>): unknown {
  if (!Array.isArray(expr)) return expr;
  const [op, ...args] = expr as [string, ...unknown[]];
  switch (op) {
    case "get":
      return props[args[0] as string];
    case "to-number":
      return Number(evaluate(args[0], props) ?? 0);
    case "<=":
      return (evaluate(args[0], props) as number) <= (evaluate(args[1], props) as number);
    case "case":
      return evaluate(args[0], props) ? evaluate(args[1], props) : evaluate(args[2], props);
    case "step": {
      const v = evaluate(args[0], props) as number;
      let out = args[1];
      for (let i = 2; i < args.length; i += 2) if (v >= (args[i] as number)) out = args[i + 1];
      return out;
    }
    default:
      throw new Error(`unsupported ${op}`);
  }
}

const farClasses: MetricClasses = {
  layer: "far",
  source_layer: "heat_far",
  decimals: 2,
  method: "quantile",
  unit: null,
  breaks: [1.22, 1.68, 2.22, 2.72],
  min: 0.8,
  max: 3.2,
  count: 4,
  null_count: 1,
  zero_class: false,
};

describe("registry", () => {
  it("has the wireframe's cards in rail order, minus the out-of-POC ones, each with map layers or the base style", () => {
    // no planned traffic (MVP layer), no ownership / restitution (bulk cadastral access not confirmed)
    // the POC plan's seven layers each toggle; only the plan areas and the base map are core
    expect(LAYERS.map((l) => l.id)).toEqual(["docareas", "base", "zones", "blocks", "cadastre", "planned", "landuse", "heatFAR", "heatMkt"]);
    expect(LAYERS.filter((l) => l.core).map((l) => l.id)).toEqual(["docareas", "base"]);
    for (const l of LAYERS) if (l.id !== "base") expect(LAYER_GROUPS[l.id].length, l.id).toBeGreaterThan(0);
    // cadastral and planned parcels are separate layers on separate source-layers
    const src = (id: "cadastre" | "planned") => new Set(UV_LAYERS.filter((s) => LAYER_GROUPS[id].includes(s.id)).map((s) => s["source-layer"]));
    expect(src("cadastre")).toEqual(new Set(["cadastral_parcels"]));
    expect(src("planned")).toEqual(new Set(["urban_parcels"]));
  });

  it("starts each map layer at its catalogue zoom", () => {
    expect(UV_LAYERS.find((l) => l.id === "uv-cad-fill")!.minzoom).toBe(13);
    expect(UV_LAYERS.find((l) => l.id === "uv-heat-far-fill")!.minzoom).toBe(10);
    expect(UV_LAYERS.find((l) => l.id === "uv-heat-height-nodata")!.minzoom).toBe(10);
    expect(UV_LAYERS.find((l) => l.id === "uv-heatmkt-fill")!.minzoom).toBe(8); // heat_sale_price: 8 in the catalogue
    expect(UV_LAYERS.find((l) => l.id === "uv-blocks-label")!.minzoom).toBe(15);
  });

  it("starts each map layer where the published archive has its source-layer, as the rail says", () => {
    const tiles = pointer({ cadastral_parcels: [14, 3], urban_blocks: [11, 2], heat_sale_price: [8, 2] });
    const specs = uvLayers(tiles);
    const at = (id: string) => specs.find((l) => l.id === id)!.minzoom;
    expect(at("uv-cad-fill")).toBe(14); // a clamped build wins over the catalogue's 13
    expect(at("uv-cad-sel-line")).toBe(14); // hover and selection layers follow their source-layer
    expect(at("uv-blocks-line")).toBe(11); // blocks draw from their own range, not the zones card's 8
    expect(at("uv-blocks-label")).toBe(15); // a label's own later start stays
    expect(at("uv-urban-fill")).toBe(13); // not listed: the catalogue's value
    expect(layerMinZoom(layerById("cadastre"), tiles)).toBe(14);
    expect(layerMinZoom(layerById("zones"), tiles)).toBe(8); // zones from 8 although blocks start at 11
    expect(layerMinZoom(layerById("base"), tiles)).toBe(0);
  });

  it("toggles every card on its own (both heatmaps may be on) and never a core layer", () => {
    const withFar = toggleLayer(DEFAULT_LAYER_STATE, "heatFAR");
    expect(withFar.on).toBe(true);
    const withPrice = toggleLayer(withFar.layers, "heatMkt");
    expect(withPrice.layers.heatFAR && withPrice.layers.heatMkt).toBe(true);
    expect(toggleLayer(DEFAULT_LAYER_STATE, "zones")).toMatchObject({ on: false, layers: { zones: false, blocks: true } });
    expect(toggleLayer(DEFAULT_LAYER_STATE, "blocks")).toMatchObject({ on: false, layers: { zones: true, blocks: false } });
    expect(toggleLayer(DEFAULT_LAYER_STATE, "docareas").layers).toBe(DEFAULT_LAYER_STATE);
    expect(toggleLayer(DEFAULT_LAYER_STATE, "landuse").layers.heatFAR).toBe(false); // others untouched
  });
});

/** A published pointer listing `id: [min_zoom, features]` per source-layer. */
function pointer(layers: Record<string, [number, number]>): TilesCurrent {
  return {
    status: "published",
    data_version: "v1",
    version_id: 1,
    archive_url: "https://files.example/tiles.pmtiles",
    layers: Object.entries(layers).map(([id, [min_zoom, features]]) => ({ id, geometry_type: "polygon", min_zoom, max_zoom: 16, features, available: true })),
    heatmaps_refreshing: false,
  };
}

describe("layer state: what the map draws for each card", () => {
  const view = (over: Partial<LayerView> = {}): LayerView => ({
    layers: { ...DEFAULT_LAYER_STATE },
    zoom: 14,
    ...over,
  });
  const tiles = pointer({
    zones: [8, 2],
    document_coverage: [9, 3],
    cadastral_parcels: [13, 7],
    urban_parcels: [13, 6],
    land_use: [10, 0],
    heat_far: [10, 2],
    heat_sale_price: [8, 2],
  });

  it("shows the default layers closer in, and says zoom in for parcels at the city framing", () => {
    expect(layerState("cadastre", view(), tiles)).toBe("shown");
    expect(layerState("planned", view(), tiles)).toBe("shown");
    const city = view({ zoom: 10.64 });
    expect(layerState("cadastre", city, tiles)).toBe("zoom_in");
    expect(layerState("planned", city, tiles)).toBe("zoom_in");
    expect(layerState("zones", city, tiles)).toBe("shown");
    expect(layerState("docareas", city, tiles)).toBe("shown");
    expect(layerState("cadastre", view({ zoom: 13 }), tiles)).toBe("shown"); // Mapbox draws at zoom >= minzoom
    expect(layerState("cadastre", view({ zoom: null }), tiles)).toBe("shown"); // no map: no zoom reason
  });

  it("says off or no data before it looks at the zoom", () => {
    expect(layerState("landuse", view(), tiles)).toBe("off");
    expect(layerState("landuse", view({ layers: { ...DEFAULT_LAYER_STATE, landuse: true }, zoom: 9.5 }), tiles)).toBe("no_data");
    const price = { ...DEFAULT_LAYER_STATE, heatMkt: true };
    expect(layerState("heatMkt", view({ layers: price, zoom: 9 }), tiles)).toBe("shown"); // zone cells from 8, no lock
    const far = { ...DEFAULT_LAYER_STATE, heatFAR: true };
    expect(layerState("heatFAR", view({ layers: far, zoom: 9.5 }), tiles)).toBe("zoom_in");
  });

  it("round-trips every card's state through one selector string", () => {
    const key = layerStatesKey(view({ zoom: 10.64 }), tiles);
    const states = parseLayerStates(key);
    expect(Object.keys(states)).toEqual(LAYERS.map((l) => l.id));
    expect(states).toMatchObject({ zones: "shown", cadastre: "zoom_in", landuse: "off", heatMkt: "off" });
    expect(layerStatesKey(view({ zoom: 10.7 }), tiles)).toBe(key); // no change inside a zoom band
  });
});

describe("camera fits", () => {
  it("never sends maxZoom: undefined (Mapbox's fit turns NaN and is dropped)", () => {
    expect(fitOptions(24)).toEqual({ padding: 24 });
    expect("maxZoom" in fitOptions(24)).toBe(false);
    expect(fitOptions(96, 18)).toEqual({ padding: 96, maxZoom: 18 });
  });

  it("keeps room to fit into in a small box", () => {
    expect(fitPadding(24, 842, 838)).toBe(24);
    expect(fitPadding(96, 375, 150)).toBe(74);
    expect(fitPadding(24, 0, 0)).toBe(0);
  });
});

describe("legend", () => {
  const ctx = (over: Partial<LegendContext> = {}): LegendContext => ({
    layers: { ...DEFAULT_LAYER_STATE },
    choropleth: { ...DEFAULT_CHOROPLETH },
    classes: null,
    ...over,
  });

  it("lists the default layers with the wireframe's rows", () => {
    const groups = legendGroups(ctx());
    expect(groups.map((g) => g.title)).toEqual(["Urban zones", "Urban blocks", "Cadastral parcels", "Urban parcels"]);
    expect(groups[0].rows.map((r) => r.label)).toEqual(["Residential", "Commercial", "Mixed use", "Public / institutional", "Green / recreation"]);
    expect(groups[1].rows.map((r) => r.label)).toEqual(["Urban block boundary"]);
    expect(groups[2].rows[0].label).toBe("Parcel outline");
    expect(groups[3].rows[0].label).toBe("Parcel — click to open");
  });

  it("falls back to the wireframe's FAR gradient without served classes", () => {
    const g = legendGroups(ctx({ layers: { ...DEFAULT_LAYER_STATE, heatFAR: true } })).find((x) => x.title === "FAR intensity")!;
    expect(g.unit).toBe("floor area ratio");
    expect(g.rows).toEqual([{ mark: { kind: "grad", stops: "#F1E7D6,#B5613B" }, label: "Low → high" }]);
  });

  it("lists the served classes and a no-data row, and follows the selected field", () => {
    const floors = { ...farClasses, layer: "height", source_layer: "heat_height", unit: "floors", breaks: [], min: 12, max: 12, null_count: 0, decimals: 0 };
    const classes = { far: farClasses, height: floors };
    const far = legendGroups(ctx({ layers: { ...DEFAULT_LAYER_STATE, heatFAR: true }, classes })).find((x) => x.title === "FAR intensity")!;
    expect(far.rows.map((r) => r.label)).toEqual(["0.8 – 1.22", "1.22 – 1.68", "1.68 – 2.22", "2.22 – 2.72", "2.72 – 3.2", "No data"]);
    expect(far.rows.at(-1)!.mark).toEqual({ kind: "hatch" });
    const height = legendGroups(
      ctx({ layers: { ...DEFAULT_LAYER_STATE, heatFAR: true }, classes, choropleth: { param: "max_floors", price: "expected" } }),
    ).find((x) => x.title === "Building height")!;
    expect(height.rows.map((r) => r.label)).toEqual(["12"]);
  });

  it("says why a layer that is on draws nothing: zoom in (rows kept) or no data yet (no rows)", () => {
    const layers = { ...DEFAULT_LAYER_STATE, landuse: true };
    const groups = legendGroups(ctx({ layers, states: { cadastre: "zoom_in", landuse: "no_data" } }));
    const cad = groups.find((g) => g.title === "Cadastral parcels")!;
    expect(cad.note).toBe("zoom in to see");
    expect(cad.rows[0].label).toBe("Parcel outline");
    expect(groups.find((g) => g.title === "Land use")).toEqual({ title: "Land use", note: "no data yet", rows: [] });
    expect(groups.find((g) => g.title === "Urban parcels")!.note).toBeUndefined();
  });

  it("shows the price bands when the price heatmap is on (no paywall)", () => {
    const layers = { ...DEFAULT_LAYER_STATE, heatMkt: true };
    const g = legendGroups(ctx({ layers })).find((x) => x.title === "Price heatmap")!;
    expect(g.unit).toBe("€/m² land");
    expect(g.rows.map((r) => r.label)).toEqual(["not saleable", "under €1,300", "€1,300 – 1,700", "€1,700 – 2,100", "€2,100 and above"]);
  });
});

describe("choropleth colours match the legend", () => {
  it("parameter classes: every cell gets the colour of the legend row the API banded it in", () => {
    const scheme = paramScheme("max_far", farClasses);
    [0, 1, 2, 3, 4].forEach((band) => expect(evaluate(scheme.fillColor, { band, value: 1 })).toBe(scheme.rows[band].colour));
    expect(scheme.rows[0].colour).toBe(gradientColours(5)[0]);
    expect(scheme.rows.at(-1)!.colour).toBe("#B4744A");
    const style = choroplethStyle({ param: "max_far", price: "expected" }, { far: farClasses });
    expect(style["uv-heat-far-fill"].filter).toEqual(["has", "value"]);
    expect(style["uv-heat-far-nodata"].filter).toEqual(["!", ["has", "value"]]);
    // the other fields' layers draw nothing while FAR is selected
    expect(style["uv-heat-height-fill"].filter).toEqual(NONE);
    expect(style["uv-heat-gfa-nodata"].filter).toEqual(NONE);
  });

  it("parameter fallback without classes: the gradient on the value", () => {
    const scheme = paramScheme("max_far", null);
    expect(scheme.classed).toBe(false);
    expect(JSON.stringify(scheme.fillColor)).toContain('"get","value"');
  });

  it("price bands: 0 is not saleable, the bands follow the served breaks", () => {
    const scheme = priceScheme("low", { ...farClasses, layer: "sale_price", method: "fixed", breaks: [1300, 1700, 2100], zero_class: true });
    expect(scheme.column).toBe("low");
    const at = (band: number) => evaluate(scheme.fillColor, { band_low: band });
    expect(at(0)).toBe(NOT_SALEABLE_COLOUR);
    expect([at(1), at(2), at(3), at(4)]).toEqual(scheme.rows.slice(1).map((r) => r.colour));
    // without served classes: the wireframe's bands on the value itself
    const fallback = priceScheme("expected", null);
    const on = (v: number) => evaluate(fallback.fillColor, { value: v });
    expect(on(0)).toBe(NOT_SALEABLE_COLOUR);
    expect([on(900), on(1300), on(1800), on(2450)]).toEqual(fallback.rows.slice(1).map((r) => r.colour));
    const style = choroplethStyle({ param: "max_gfa_m2", price: "high" }, null);
    expect(style["uv-heatmkt-fill"].filter).toEqual(["has", "high"]);
    expect(style["uv-heat-gfa-nodata"].filter).toEqual(["!", ["has", "value"]]);
  });
});

describe("?layers= links", () => {
  it("has no parameter for the default view and round-trips any other", () => {
    expect(formatLayersParam(DEFAULT_LAYER_STATE, DEFAULT_CHOROPLETH)).toBeNull();
    const layers = { ...DEFAULT_LAYER_STATE, cadastre: false, landuse: true, heatFAR: true };
    const value = formatLayersParam(layers, { param: "max_gfa_m2", price: "expected" })!;
    expect(value).toBe("zones,blocks,planned,landuse,heatFAR:gfa"); // zones and blocks toggle too now
    const parsed = parseLayersParam(`?layers=${value}`)!;
    expect(parsed.layers).toEqual(layers);
    expect(parsed.choropleth.param).toBe("max_gfa_m2");
  });

  it("keeps core layers on, ignores unknown ids and allows both heatmaps", () => {
    const parsed = parseLayersParam("?layers=heatMkt:high,zones,bogus,heatFAR:far")!;
    expect(parsed.layers.zones && parsed.layers.docareas && parsed.layers.base).toBe(true);
    expect(parsed.layers.blocks).toBe(false); // listed layers only
    expect(parsed.layers.heatMkt && parsed.layers.heatFAR).toBe(true);
    expect(parsed.choropleth.price).toBe("high");
    expect(parseLayersParam("?layers=none")!.layers.cadastre).toBe(false);
    expect(parseLayersParam("?layers=bogus")).toBeNull();
    expect(parseLayersParam("?x=1")).toBeNull();
  });

  it("writes readable URLs next to ?parcel=", () => {
    expect(withParcelParam("http://x/?layers=planned,heatFAR:far", 7)).toBe("http://x/?layers=planned,heatFAR:far&parcel=7");
  });
});
