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
 * is left to fit into, which Mapbox would also drop.
 */
export function fitPadding(want: number, width: number, height: number): number {
  return Math.max(0, Math.min(want, Math.floor(Math.min(width, height) / 2) - 1));
}

/**
 * Where a searched place lands when the open legend covers the middle of the map (a small window:
 * the map between the rail and the panel can be narrower than twice the legend, so a pin flown to
 * the centre sat under it). Answers how far right of the centre to put the target, in px: the
 * middle of the strip to the right of the legend; 0 when the legend does not reach the centre (or
 * is minimised, or there is no strip worth using).
 */
export function legendOffset(
  box: { width: number; height: number },
  legend: { right: number; bottom: number } | null,
  margin = 24,
): number {
  if (!legend) return 0;
  const coversCentre = legend.right + margin > box.width / 2 && legend.bottom + margin > box.height / 2;
  if (!coversCentre) return 0;
  const free = box.width - legend.right;
  if (free < 3 * margin) return 0;
  return Math.round(legend.right + free / 2 - box.width / 2);
}
