/**
 * The wireframe's base map over Mapbox's light style (`docs/specs/frontend-design.md` §5 "Map
 * styling": land #EEF1F5, major roads white, minor roads #F2EBDC, the river #BCCEC8 at .85 with a
 * #A4B9B2 line, and no buildings or land cover under the zones). It restyles the loaded base
 * style's own layers by id; nothing is added. Labels of places, streets and water stay (a real map
 * needs them to find a site); points of interest, airports, footpaths and steps go, as in the mock.
 *
 * Only for Mapbox's light style, the default: a custom `NEXT_PUBLIC_MAPBOX_STYLE` is taken as it
 * was designed. Pure (`wireframeBasemap` lists the changes), tested without a map.
 */

export const LIGHT_STYLE = "mapbox://styles/mapbox/light-v11";

export const BASEMAP_COLOURS = {
  land: "#EEF1F5",
  water: "#BCCEC8",
  waterOpacity: 0.85,
  waterLine: "#A4B9B2",
  majorRoad: "#FFFFFF",
  minorRoad: "#F2EBDC",
  rail: "#DCE1E8",
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

export interface BaseLayer {
  id: string;
  type: string;
}

export type BaseChange =
  | { id: string; hide: true }
  | { id: string; paint: Record<string, unknown> };

const HIDE = /^(landuse|landcover|national-park|land-structure|aeroway|building|hillshade|poi-label|airport-label|transit)/;
const FOOTWAY = /^(road|bridge|tunnel)-(path|steps)/;

/** The changes that turn the light style's layers into the wireframe's base map. */
export function wireframeBasemap(layers: readonly BaseLayer[]): BaseChange[] {
  const c = BASEMAP_COLOURS;
  const roadColour = ["match", ["get", "class"], MAJOR_CLASSES, c.majorRoad, c.minorRoad];
  const changes: BaseChange[] = [];
  for (const { id, type } of layers) {
    if (type === "background") changes.push({ id, paint: { "background-color": c.land } });
    else if (HIDE.test(id) || FOOTWAY.test(id)) changes.push({ id, hide: true });
    else if (id === "water" && type === "fill")
      changes.push({ id, paint: { "fill-color": c.water, "fill-opacity": c.waterOpacity } });
    else if (id === "waterway" && type === "line") changes.push({ id, paint: { "line-color": c.waterLine } });
    else if (type === "line" && /^(road|bridge|tunnel)-simple$/.test(id))
      changes.push({ id, paint: { "line-color": roadColour } });
    else if (type === "line" && /^(road|bridge|tunnel)-pedestrian$|^bridge-case-simple$/.test(id))
      changes.push({ id, paint: { "line-color": c.minorRoad } });
    else if (type === "line" && /-rail$/.test(id)) changes.push({ id, paint: { "line-color": c.rail } });
  }
  return changes;
}
