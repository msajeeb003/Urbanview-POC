import { describe, expect, it } from "vitest";

import { DEFAULT_CHOROPLETH, DEFAULT_LAYER_STATE, LAYERS, isDrawn, legendGroups, toggleLayer, type LegendContext } from "@/lib/layers";
import { formatLayersParam, parseLayersParam, withParcelParam } from "@/lib/url-state";

import {
  NOT_SALEABLE_COLOUR,
  gradientColours,
  paramScheme,
  priceScheme,
  type MetricClasses,
} from "./classes";
import { LAYER_GROUPS, UV_LAYERS, choroplethStyle } from "./style";

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
  it("has the wireframe's eleven cards in rail order, each with map layers or the base style", () => {
    expect(LAYERS.map((l) => l.id)).toEqual([
      "docareas", "base", "zones", "cadastre", "planned", "owner", "restit", "landuse", "heatFAR", "traffic", "heatMkt",
    ]);
    expect(LAYERS.filter((l) => l.core).map((l) => l.id)).toEqual(["docareas", "base", "zones"]);
    for (const l of LAYERS) if (l.id !== "base") expect(LAYER_GROUPS[l.id].length, l.id).toBeGreaterThan(0);
    // cadastral and planned parcels are separate layers on separate source-layers
    const src = (id: "cadastre" | "planned") => new Set(UV_LAYERS.filter((s) => LAYER_GROUPS[id].includes(s.id)).map((s) => s["source-layer"]));
    expect(src("cadastre")).toEqual(new Set(["cadastral_parcels"]));
    expect(src("planned")).toEqual(new Set(["urban_parcels"]));
  });

  it("starts each map layer at its catalogue zoom", () => {
    expect(UV_LAYERS.find((l) => l.id === "uv-cad-fill")!.minzoom).toBe(13);
    expect(UV_LAYERS.find((l) => l.id === "uv-heatfar-fill")!.minzoom).toBe(10);
    expect(UV_LAYERS.find((l) => l.id === "uv-blocks-label")!.minzoom).toBe(15);
  });

  it("keeps one choropleth on at a time and never toggles a core layer", () => {
    const withFar = toggleLayer(DEFAULT_LAYER_STATE, "heatFAR");
    expect(withFar).toMatchObject({ on: true, switchedOff: null });
    const withPrice = toggleLayer(withFar.layers, "heatMkt");
    expect(withPrice.switchedOff).toBe("heatFAR");
    expect(withPrice.layers.heatFAR).toBe(false);
    expect(withPrice.layers.heatMkt).toBe(true);
    expect(toggleLayer(DEFAULT_LAYER_STATE, "zones").layers).toBe(DEFAULT_LAYER_STATE);
    expect(toggleLayer(DEFAULT_LAYER_STATE, "landuse").layers.heatFAR).toBe(false); // others untouched
  });

  it("draws dependent and paid layers only when they can show", () => {
    const owner = { ...DEFAULT_LAYER_STATE, owner: true };
    expect(isDrawn("owner", owner, false)).toBe(true);
    expect(isDrawn("owner", { ...owner, cadastre: false }, false)).toBe(false);
    const price = { ...DEFAULT_LAYER_STATE, heatMkt: true };
    expect(isDrawn("heatMkt", price, false)).toBe(false);
    expect(isDrawn("heatMkt", price, true)).toBe(true);
  });
});

describe("legend", () => {
  const ctx = (over: Partial<LegendContext> = {}): LegendContext => ({
    layers: { ...DEFAULT_LAYER_STATE },
    marketUnlocked: false,
    choropleth: { ...DEFAULT_CHOROPLETH },
    classes: null,
    ...over,
  });

  it("lists the default layers with the wireframe's rows", () => {
    const groups = legendGroups(ctx());
    expect(groups.map((g) => g.title)).toEqual(["Planning documents", "Urban zones", "Cadastral parcels", "Urban parcels"]);
    expect(groups[1].rows.map((r) => r.label)).toEqual([
      "Residential", "Commercial", "Mixed use", "Public / institutional", "Green / recreation", "Urban block boundary",
    ]);
    expect(groups[2].rows[0].label).toBe("Parcel outline");
    expect(groups[3].rows[0].label).toBe("Parcel — click to open");
  });

  it("says when an ownership layer needs cadastral parcels", () => {
    const groups = legendGroups(ctx({ layers: { ...DEFAULT_LAYER_STATE, owner: true, cadastre: false } }));
    expect(groups.find((g) => g.title === "Public ownership")!.rows[0].label).toBe("Publicly owned — needs cadastral parcels");
  });

  it("falls back to the wireframe's FAR gradient without served classes", () => {
    const g = legendGroups(ctx({ layers: { ...DEFAULT_LAYER_STATE, heatFAR: true } })).find((x) => x.title === "FAR intensity")!;
    expect(g.unit).toBe("floor area ratio");
    expect(g.rows).toEqual([{ mark: { kind: "grad", stops: "#F1E7D6,#B5613B" }, label: "Low → high" }]);
  });

  it("lists the served classes and a no-data row, and follows the selected field", () => {
    const classes = { block_cells: { max_far: farClasses, max_height_m: { ...farClasses, unit: "m", breaks: [], min: 12, max: 12, null_count: 0 } }, zone_cells: {} };
    const far = legendGroups(ctx({ layers: { ...DEFAULT_LAYER_STATE, heatFAR: true }, classes })).find((x) => x.title === "FAR intensity")!;
    expect(far.rows.map((r) => r.label)).toEqual(["0.8 – 1.22", "1.22 – 1.68", "1.68 – 2.22", "2.22 – 2.72", "2.72 – 3.2", "No data"]);
    expect(far.rows.at(-1)!.mark).toEqual({ kind: "hatch" });
    const height = legendGroups(
      ctx({ layers: { ...DEFAULT_LAYER_STATE, heatFAR: true }, classes, choropleth: { param: "max_height_m", price: "expected" } }),
    ).find((x) => x.title === "Building height")!;
    expect(height.rows.map((r) => r.label)).toEqual(["12"]);
  });

  it("shows the price bands only with the market entitlement", () => {
    const layers = { ...DEFAULT_LAYER_STATE, heatMkt: true };
    expect(legendGroups(ctx({ layers })).some((g) => g.title === "Price heatmap")).toBe(false);
    const g = legendGroups(ctx({ layers, marketUnlocked: true })).find((x) => x.title === "Price heatmap")!;
    expect(g.unit).toBe("€/m² land");
    expect(g.rows.map((r) => r.label)).toEqual(["not saleable", "under €1,300", "€1,300 – 1,700", "€1,700 – 2,100", "€2,100 and above"]);
  });
});

describe("choropleth colours match the legend", () => {
  it("parameter classes: every class's value gets its legend row's colour; nulls are not coloured", () => {
    const scheme = paramScheme("max_far", farClasses);
    const probes = [0.8, 1.3, 2.0, 2.5, 3.2];
    probes.forEach((far, i) => expect(evaluate(scheme.fillColor, { max_far: far })).toBe(scheme.rows[i].colour));
    expect(evaluate(scheme.fillColor, { max_far: 1.22 })).toBe(scheme.rows[1].colour); // a break starts its class
    expect(scheme.rows[0].colour).toBe(gradientColours(5)[0]);
    expect(scheme.rows.at(-1)!.colour).toBe("#B4744A");
    const style = choroplethStyle({ param: "max_far", price: "expected" }, { block_cells: { max_far: farClasses }, zone_cells: {} });
    expect(style["uv-heatfar-fill"].filter).toEqual(["has", "max_far"]);
    expect(style["uv-heatfar-nodata"].filter).toEqual(["!", ["has", "max_far"]]);
  });

  it("price bands: 0 is not saleable, the bands follow the served breaks", () => {
    const scheme = priceScheme("low", { ...farClasses, method: "fixed", breaks: [1300, 1700, 2100], zero_class: true });
    expect(scheme.column).toBe("sale_rate_low_eur_m2");
    const at = (v: number) => evaluate(scheme.fillColor, { sale_rate_low_eur_m2: v });
    expect(at(0)).toBe(NOT_SALEABLE_COLOUR);
    expect([at(900), at(1300), at(1800), at(2450)]).toEqual(scheme.rows.slice(1).map((r) => r.colour));
    const style = choroplethStyle({ param: "max_gfa_m2", price: "high" }, null);
    expect(style["uv-heatmkt-fill"].filter).toEqual(["has", "sale_rate_high_eur_m2"]);
    expect(style["uv-heatfar-nodata"].filter).toEqual(["!", ["has", "max_gfa_m2"]]);
  });
});

describe("?layers= links", () => {
  it("has no parameter for the default view and round-trips any other", () => {
    expect(formatLayersParam(DEFAULT_LAYER_STATE, DEFAULT_CHOROPLETH)).toBeNull();
    const layers = { ...DEFAULT_LAYER_STATE, cadastre: false, landuse: true, heatFAR: true };
    const value = formatLayersParam(layers, { param: "max_gfa_m2", price: "expected" })!;
    expect(value).toBe("planned,landuse,heatFAR:gfa");
    const parsed = parseLayersParam(`?layers=${value}`)!;
    expect(parsed.layers).toEqual(layers);
    expect(parsed.choropleth.param).toBe("max_gfa_m2");
  });

  it("keeps core layers on, ignores unknown ids and keeps one choropleth", () => {
    const parsed = parseLayersParam("?layers=heatMkt:high,zones,bogus,heatFAR:far")!;
    expect(parsed.layers.zones && parsed.layers.docareas && parsed.layers.base).toBe(true);
    expect(parsed.layers.heatMkt).toBe(true);
    expect(parsed.layers.heatFAR).toBe(false);
    expect(parsed.choropleth.price).toBe("high");
    expect(parseLayersParam("?layers=none")!.layers.cadastre).toBe(false);
    expect(parseLayersParam("?layers=bogus")).toBeNull();
    expect(parseLayersParam("?x=1")).toBeNull();
  });

  it("writes readable URLs next to ?parcel=", () => {
    expect(withParcelParam("http://x/?layers=planned,heatFAR:far", 7)).toBe("http://x/?layers=planned,heatFAR:far&parcel=7");
  });
});
