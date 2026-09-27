/**
 * Camera options for the map's fits (the landing view, reset, a searched parcel or zone). Pure
 * functions, tested without Mapbox.
 */

/**
 * Options for fitting bounds. `maxZoom` is only set when given: Mapbox spreads the options over
 * its defaults, so an explicit `maxZoom: undefined` makes the fitted zoom NaN and the fit is
 * dropped with "Map cannot fit within canvas": the map then opened on a corner of its max bounds
 * (over Skadar Lake) instead of the city extent S1 asks for.
 */
export function fitOptions(padding: number, maxZoom?: number): { padding: number; maxZoom?: number } {
  return maxZoom == null ? { padding } : { padding, maxZoom };
}

/**
 * The padding to fit with in a `width` × `height` px box: `want`, but never so much that nothing
 * is left to fit into (a phone's map above the bottom sheet), which Mapbox would also drop.
 */
export function fitPadding(want: number, width: number, height: number): number {
  return Math.max(0, Math.min(want, Math.floor(Math.min(width, height) / 2) - 1));
}
