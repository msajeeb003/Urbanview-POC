/**
 * PMTiles for Mapbox GL JS. Mapbox GL has no `addProtocol` (that is MapLibre); since 3.x it loads
 * custom tile sources through `mapboxgl.addTileProvider(name, moduleUrl)`: every map worker imports
 * the module and constructs its default export with the source options, then calls `load()` for
 * the TileJSON and `loadTile()` per tile. This file is that module. It is bundled on its own (no
 * app code, worker-safe APIs only) by `scripts/build-map-provider.mjs` into
 * `public/map/urbanview-pmtiles.js`; `src/lib/map/tiles.ts` registers it.
 *
 * The source `url` is the API's tile pointer (`/v1/tiles/current`), with the pointer the page
 * already fetched carried in the fragment (`#archive=…&expires=…&version=…`) so workers do not ask
 * again. Archive URLs are short-lived signed links: when one is about to expire, or a range request
 * fails, the provider re-reads the pointer and continues on the fresh link. A new publish shows up
 * as a new `version_id`; the page then recreates the source, so tiles of two versions never mix.
 */
import { PMTiles } from "pmtiles";

import { TILE_TEMPLATE } from "./provider-name";

export { PROVIDER_NAME, TILE_TEMPLATE } from "./provider-name";
const REFRESH_MARGIN_MS = 60_000;

export interface TilePointer {
  status: string;
  archive_url: string | null;
  expires_at: string | null;
  version_id: number | null;
}

export interface ArchiveReader {
  getHeader(): Promise<{ minZoom: number; maxZoom: number; minLon: number; minLat: number; maxLon: number; maxLat: number }>;
  getMetadata(): Promise<unknown>;
  getZxy(z: number, x: number, y: number, signal?: AbortSignal): Promise<{ data: ArrayBuffer; cacheControl?: string; expires?: string } | undefined>;
}

export interface ProviderDeps {
  fetchPointer(url: string): Promise<TilePointer>;
  open(archiveUrl: string): ArchiveReader;
  now(): number;
}

const defaultDeps: ProviderDeps = {
  async fetchPointer(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
    if (!res.ok) throw new Error(`tile pointer answered ${res.status}`);
    return (await res.json()) as TilePointer;
  },
  open: (archiveUrl) => new PMTiles(archiveUrl),
  now: () => Date.now(),
};

/** Source url = pointer URL + `#archive=&expires=&version=` (see `tiles.ts`). */
export function parseSourceUrl(url: string): { pointerUrl: string; initial: TilePointer | null } {
  const hash = url.indexOf("#");
  if (hash < 0) return { pointerUrl: url, initial: null };
  const pointerUrl = url.slice(0, hash);
  const params = new URLSearchParams(url.slice(hash + 1));
  const archive = params.get("archive");
  if (!archive) return { pointerUrl, initial: null };
  const version = params.get("version");
  return {
    pointerUrl,
    initial: {
      status: "published",
      archive_url: archive,
      expires_at: params.get("expires"),
      version_id: version ? Number(version) : null,
    },
  };
}

interface Current {
  reader: ArchiveReader;
  expiresAt: number;
  versionId: number | null;
}

export class UrbanViewTileProvider {
  private readonly pointerUrl: string;
  private initial: TilePointer | null;
  private current: Promise<Current> | null = null;
  private readonly deps: ProviderDeps;

  constructor(options: { url: string }, deps: ProviderDeps = defaultDeps) {
    const { pointerUrl, initial } = parseSourceUrl(options.url);
    this.pointerUrl = pointerUrl;
    this.initial = initial;
    this.deps = deps;
  }

  private open(pointer: TilePointer): Current {
    if (!pointer.archive_url) throw new Error("no published tile archive");
    return {
      reader: this.deps.open(pointer.archive_url),
      expiresAt: pointer.expires_at ? Date.parse(pointer.expires_at) : Number.POSITIVE_INFINITY,
      versionId: pointer.version_id,
    };
  }

  /** The archive to read now; refreshes the signed link shortly before it expires. */
  private async archive(forceRefresh = false): Promise<Current> {
    if (!forceRefresh && this.current) {
      const cur = await this.current;
      if (cur.expiresAt - REFRESH_MARGIN_MS > this.deps.now()) return cur;
    }
    const initial = this.initial;
    this.initial = null;
    const usable =
      initial && !forceRefresh && (!initial.expires_at || Date.parse(initial.expires_at) - REFRESH_MARGIN_MS > this.deps.now());
    this.current = (usable ? Promise.resolve(initial) : this.deps.fetchPointer(this.pointerUrl)).then((p) => this.open(p));
    return this.current;
  }

  async load(): Promise<Record<string, unknown>> {
    const { reader } = await this.archive();
    const header = await reader.getHeader();
    const metadata = ((await reader.getMetadata()) ?? {}) as { vector_layers?: unknown[]; attribution?: string };
    return {
      tilejson: "3.0.0",
      tiles: [TILE_TEMPLATE],
      minzoom: header.minZoom,
      maxzoom: header.maxZoom,
      bounds: [header.minLon, header.minLat, header.maxLon, header.maxLat],
      vector_layers: metadata.vector_layers ?? [],
      ...(metadata.attribution ? { attribution: metadata.attribution } : {}),
    };
  }

  async loadTile(
    tile: { z: number; x: number; y: number },
    options: { signal: AbortSignal },
  ): Promise<{ data: ArrayBuffer | null; cacheControl?: string; expires?: string }> {
    let cur = await this.archive();
    let res;
    try {
      res = await cur.reader.getZxy(tile.z, tile.x, tile.y, options.signal);
    } catch (error) {
      if (options.signal.aborted) throw error;
      // most likely the signed link expired early or was revoked: read the pointer once more
      cur = await this.archive(true);
      res = await cur.reader.getZxy(tile.z, tile.x, tile.y, options.signal);
    }
    // an absent tile is empty ocean, not an error: nothing is drawn there
    return res ? { data: res.data, cacheControl: res.cacheControl, expires: res.expires } : { data: null };
  }
}

export default UrbanViewTileProvider;
