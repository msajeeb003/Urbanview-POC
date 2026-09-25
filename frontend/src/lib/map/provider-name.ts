/** Shared by the page (registration, source spec) and the worker module; no imports on purpose. */
export const PROVIDER_NAME = "urbanview-pmtiles";
/** Tile URL template handed back in the TileJSON; the provider only reads z / x / y from it. */
export const TILE_TEMPLATE = `${PROVIDER_NAME}://tile/{z}/{x}/{y}`;
