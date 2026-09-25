/**
 * Glue between the tile pointer (`GET /v1/tiles/current`) and Mapbox GL: registers the PMTiles
 * provider module once and builds the vector source for the current published version.
 */
import { apiBaseUrl, buildUrl } from "@/lib/api/client";
import type { TilesCurrent } from "@/lib/api/types";

import { PROVIDER_NAME } from "./provider-name";

export const SOURCE_ID = "uv";
/** Served from `public/` (built by `scripts/build-map-provider.mjs`). */
export const PROVIDER_MODULE_PATH = "/map/urbanview-pmtiles.js";

let registered = false;

/** `mapboxgl.addTileProvider` is experimental in Mapbox GL 3.x; register before any map exists. */
export function registerTileProvider(mapboxgl: { addTileProvider?: (name: string, url: string) => void }): boolean {
  if (registered) return true;
  if (typeof mapboxgl.addTileProvider !== "function") return false;
  mapboxgl.addTileProvider(PROVIDER_NAME, new URL(PROVIDER_MODULE_PATH, window.location.href).href);
  registered = true;
  return true;
}

/** Whether the pointer names an archive the map can draw. */
export function hasArchive(tiles: TilesCurrent | null | undefined): tiles is TilesCurrent & { archive_url: string } {
  return !!tiles && tiles.status === "published" && !!tiles.archive_url;
}

/**
 * Source `url` for the provider: the pointer endpoint, with the pointer the page already holds in
 * the fragment so workers start on it without asking the API again.
 */
export function sourceUrl(tiles: TilesCurrent & { archive_url: string }, baseUrl = apiBaseUrl()): string {
  const params = new URLSearchParams({ archive: tiles.archive_url });
  if (tiles.expires_at) params.set("expires", tiles.expires_at);
  if (tiles.version_id != null) params.set("version", String(tiles.version_id));
  return `${buildUrl("/v1/tiles/current", undefined, baseUrl)}#${params.toString()}`;
}

export function vectorSource(tiles: TilesCurrent & { archive_url: string }) {
  return {
    type: "vector" as const,
    url: sourceUrl(tiles),
    provider: PROVIDER_NAME,
  };
}
