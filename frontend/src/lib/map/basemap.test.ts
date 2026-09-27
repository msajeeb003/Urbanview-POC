import { describe, expect, it } from "vitest";

import { BASEMAP_COLOURS, wireframeBasemap } from "./basemap";

/** The layers of Mapbox's light-v11 style (id and type, in style order). */
const LIGHT_V11 = [
  ["land", "background"],
  ["national-park", "fill"],
  ["landuse", "fill"],
  ["waterway", "line"],
  ["water", "fill"],
  ["land-structure-polygon", "fill"],
  ["land-structure-line", "line"],
  ["aeroway-polygon", "fill"],
  ["building", "fill"],
  ["tunnel-path-trail", "line"],
  ["tunnel-simple", "line"],
  ["road-path", "line"],
  ["road-steps", "line"],
  ["road-pedestrian", "line"],
  ["road-simple", "line"],
  ["road-rail", "line"],
  ["bridge-case-simple", "line"],
  ["bridge-simple", "line"],
  ["admin-0-boundary", "line"],
  ["road-label-simple", "symbol"],
  ["poi-label", "symbol"],
  ["airport-label", "symbol"],
  ["settlement-major-label", "symbol"],
].map(([id, type]) => ({ id, type }));

describe("wireframe base map over the light style", () => {
  const changes = new Map(wireframeBasemap(LIGHT_V11).map((c) => [c.id, c]));
  const paint = (id: string) => {
    const c = changes.get(id);
    return c && "paint" in c ? c.paint : undefined;
  };

  it("paints the land, the river and the roads in the mock's colours", () => {
    expect(paint("land")).toEqual({ "background-color": BASEMAP_COLOURS.land });
    expect(paint("water")).toEqual({ "fill-color": "#BCCEC8", "fill-opacity": 0.85 });
    expect(paint("waterway")).toEqual({ "line-color": "#A4B9B2" });
    for (const id of ["road-simple", "bridge-simple", "tunnel-simple"]) {
      const colour = paint(id)?.["line-color"] as unknown[];
      expect(colour[0]).toBe("match");
      expect(colour.at(-2)).toBe("#FFFFFF"); // major classes white
      expect(colour.at(-1)).toBe("#F2EBDC"); // everything else cream
      expect(colour[2]).toContain("primary");
    }
    expect(paint("road-pedestrian")).toEqual({ "line-color": "#F2EBDC" });
  });

  it("hides buildings, land cover, points of interest and footpaths; keeps place and street labels", () => {
    for (const id of ["building", "landuse", "national-park", "land-structure-polygon", "aeroway-polygon", "poi-label", "airport-label", "road-path", "road-steps", "tunnel-path-trail"]) {
      expect(changes.get(id), id).toEqual({ id, hide: true });
    }
    for (const id of ["road-label-simple", "settlement-major-label", "admin-0-boundary"]) {
      expect(changes.has(id), id).toBe(false);
    }
  });
});
