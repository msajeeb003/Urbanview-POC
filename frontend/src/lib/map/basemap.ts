/**
 * The base map over Mapbox's light style. The wireframe's palette (land #EEF1F5, white major and
 * cream minor roads, a soft river, nothing under the zones) was the starting point; the client's
 * review of 2026-10-12 asked for existing buildings and a clearer difference between streets,
 * green areas and water, so the light style's own buildings, parks and land cover stay visible in
 * quiet tones and the water is a clear blue. It restyles the loaded base style's own layers by
 * id; nothing is added. Labels of places, streets and water stay (a real map needs them to find a
 * site); points of interest, airports, footpaths and steps go, as in the mock.
 *
 * Only for Mapbox's light style, the default: a custom `NEXT_PUBLIC_MAPBOX_STYLE` is taken as it
 * was designed. Pure (`wireframeBasemap` lists the changes), tested without a map.
 */

export const LIGHT_STYLE = "mapbox://styles/mapbox/light-v11";

export const BASEMAP_COLOURS = {
  land: "#EEF1F5",
  water: "#B7D2E4",
  waterOpacity: 1,
  waterLine: "#99BBD2",
  majorRoad: "#FFFFFF",
  minorRoad: "#F9F6EF",
  roadCase: "#D8D3C8",
  rail: "#D5DAE2",
  building: "#DDD9D1",
  green: "#D7E5CC",
  greenLight: "#E2ECDA",
} as const;

/** Road classes the mock draws as white major roads; every other road is a cream minor one. */
const MAJOR_CLASSES = [
  "motorway",
  "motorway_link",
  "trunk",
  "trunk_link",
  "primary",
  "primary_link",
  "secondary",
  "secondary_link",
  "tertiary",
  "tertiary_link",
];

/** Land-use classes drawn as green areas (parks, pitches, grass, cemeteries, woods, farmland). */
const GREEN_CLASSES = ["park", "pitch", "grass", "garden", "cemetery", "recreation_ground", "wood", "scrub", "farmland", "glacier"];

export interface BaseLayer {
  id: string;
  type: string;
}

export type BaseChange =
  | { id: string; hide: true }
  | { id: string; paint: Record<string, unknown> };

const HIDE = /^(aeroway|hillshade|poi-label|airport-label|transit)/;
const FOOTWAY = /^(road|bridge|tunnel)-(path|steps)/;

/** The changes that turn the light style's layers into UrbanView's base map. */
export function wireframeBasemap(layers: readonly BaseLayer[]): BaseChange[] {
  const c = BASEMAP_COLOURS;
  const roadColour = ["match", ["get", "class"], MAJOR_CLASSES, c.majorRoad, c.minorRoad];
  const greenOrLand = ["match", ["get", "class"], GREEN_CLASSES, c.green, c.land];
  const changes: BaseChange[] = [];
  for (const { id, type } of layers) {
    if (type === "background") changes.push({ id, paint: { "background-color": c.land } });
    else if (HIDE.test(id) || FOOTWAY.test(id)) changes.push({ id, hide: true });
    else if (id === "landuse" && type === "fill") changes.push({ id, paint: { "fill-color": greenOrLand, "fill-opacity": 1 } });
    else if (/^(national-park|landcover)/.test(id) && type === "fill")
      changes.push({ id, paint: { "fill-color": c.greenLight, "fill-opacity": 0.8 } });
    else if (/^land-structure/.test(id)) changes.push({ id, hide: true });
    else if (id === "building" && type === "fill")
      changes.push({ id, paint: { "fill-color": c.building, "fill-opacity": 0.75, "fill-outline-color": c.roadCase } });
    else if (/^building/.test(id) && type === "line") changes.push({ id, paint: { "line-color": c.roadCase } });
    else if (id === "water" && type === "fill")
      changes.push({ id, paint: { "fill-color": c.water, "fill-opacity": c.waterOpacity } });
    else if (id === "waterway" && type === "line") changes.push({ id, paint: { "line-color": c.waterLine } });
    else if (type === "line" && /^(road|bridge|tunnel)-simple$/.test(id))
      changes.push({ id, paint: { "line-color": roadColour } });
    else if (type === "line" && /^(road|bridge|tunnel)-case-simple$/.test(id))
      changes.push({ id, paint: { "line-color": c.roadCase } });
    else if (type === "line" && /^(road|bridge|tunnel)-pedestrian$/.test(id))
      changes.push({ id, paint: { "line-color": c.minorRoad } });
    else if (type === "line" && /-rail$/.test(id)) changes.push({ id, paint: { "line-color": c.rail } });
  }
  return changes;
}
