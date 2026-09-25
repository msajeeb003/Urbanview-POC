import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_LAYER_STATE } from "@/lib/layers";
import { UNCOVERED_FLASH_MS, highlightOf, useShell } from "@/lib/store";
import { readParcelParam, withParcelParam } from "@/lib/url-state";

import { geometryCentre, pickFeature } from "./pick";
import { UrbanViewTileProvider, parseSourceUrl, type ArchiveReader, type TilePointer } from "./pmtiles-provider";
import { HIT_LAYERS, LAYER_GROUPS, NONE, UV_LAYERS, hatchImage, highlightFilters, visibleLayerIds } from "./style";
import { hasArchive, sourceUrl } from "./tiles";

/** Source-layers of the publish catalogue (backend/jobs/publish_layers.py LAYERS). */
const CATALOGUE = new Set([
  "zones",
  "zone_labels",
  "document_coverage",
  "urban_blocks",
  "urban_parcels",
  "cadastral_parcels",
  "public_ownership",
  "legal_burdens",
  "land_use",
  "traffic_network",
  "block_cells",
  "zone_cells",
]);

const square = (x: number, y: number, s = 1) => ({
  type: "Polygon",
  coordinates: [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]],
});

describe("pickFeature", () => {
  const cad = { layer: { id: HIT_LAYERS.cadastral }, properties: { id: 7, zone_id: 2, primary_urban_parcel_id: 31 }, geometry: square(0, 0, 2) };
  const urban = { layer: { id: HIT_LAYERS.urban }, properties: { id: 31, zone_id: 2 }, geometry: square(0.5, 0.5) };
  const doc = { layer: { id: HIT_LAYERS.document }, properties: { id: 4, zone_id: 2 }, geometry: square(0, 0, 10) };

  it("prefers the cadastral parcel, then the planned parcel, then the plan area", () => {
    // queryRenderedFeatures returns top-most first: planned parcel above cadastral above plan area
    expect(pickFeature([urban, cad, doc])).toMatchObject({ type: "cadastral", id: 7, zoneId: 2, linkedUrbanId: 31 });
    expect(pickFeature([urban, doc])).toMatchObject({ type: "urban", id: 31, linkedUrbanId: null });
    expect(pickFeature([doc])).toMatchObject({ type: "document", id: 4 });
    expect(pickFeature([])).toBeNull();
  });

  it("ignores features without a usable id and layers it does not know", () => {
    expect(pickFeature([{ layer: { id: HIT_LAYERS.cadastral }, properties: { id: "7" } }, doc])).toMatchObject({ type: "document" });
    expect(pickFeature([{ layer: { id: "road-label" }, properties: { id: 1 } }])).toBeNull();
  });

  it("puts the pin at the area centroid of the largest polygon", () => {
    expect(pickFeature([cad])!.centre).toEqual({ lng: 1, lat: 1 });
    const multi = { type: "MultiPolygon", coordinates: [square(0, 0, 1).coordinates, square(10, 10, 4).coordinates] };
    expect(geometryCentre(multi)).toEqual({ lng: 12, lat: 12 });
    expect(geometryCentre({ type: "LineString", coordinates: [] })).toBeNull();
  });
});

describe("style", () => {
  it("draws only catalogue source-layers with unique layer ids", () => {
    const ids = UV_LAYERS.map((l) => l.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const l of UV_LAYERS) expect(CATALOGUE.has(l["source-layer"]), l.id).toBe(true);
    for (const id of Object.values(HIT_LAYERS)) expect(ids).toContain(id);
    const grouped = Object.values(LAYER_GROUPS).flat();
    expect(new Set(grouped)).toEqual(new Set(ids)); // every layer follows exactly one rail toggle
  });

  it("colours zones by type at 24 % and mutes the uncovered ones", () => {
    const fill = UV_LAYERS.find((l) => l.id === "uv-zones-fill")!;
    expect(fill.paint!["fill-opacity"]).toBe(0.24);
    expect(JSON.stringify(fill.paint!["fill-color"])).toContain("#8A7A8E");
    expect(fill.filter).toEqual(["!=", ["get", "covered"], false]);
    expect(UV_LAYERS.find((l) => l.id === "uv-zones-nodata-hatch")!.filter).toEqual(["==", ["get", "covered"], false]);
    expect(UV_LAYERS.find((l) => l.id === "uv-zones-label")!["source-layer"]).toBe("zone_labels");
  });

  it("follows the rail: dependencies and the paid layer", () => {
    const base = visibleLayerIds(DEFAULT_LAYER_STATE, { marketUnlocked: false });
    expect(base.has("uv-zones-fill") && base.has("uv-cad-fill") && base.has("uv-urban-line") && base.has("uv-doc-line")).toBe(true);
    expect(base.has("uv-landuse-fill")).toBe(false);
    const owner = { ...DEFAULT_LAYER_STATE, owner: true };
    expect(visibleLayerIds(owner, { marketUnlocked: false }).has("uv-owner-fill")).toBe(true);
    expect(visibleLayerIds({ ...owner, cadastre: false }, { marketUnlocked: false }).has("uv-owner-fill")).toBe(false);
    const price = { ...DEFAULT_LAYER_STATE, heatMkt: true };
    expect(visibleLayerIds(price, { marketUnlocked: false }).has("uv-heatmkt-fill")).toBe(false);
    expect(visibleLayerIds(price, { marketUnlocked: true }).has("uv-heatmkt-fill")).toBe(true);
  });

  it("highlights the selection, its planned parcel, and a hover that is not the selection", () => {
    const sel = highlightOf({ kind: "feature", type: "cadastral", id: 7, zoneId: 2, linkedUrbanId: 31, via: "click" });
    expect(sel).toEqual({ cadastral: 7, urban: 31, document: null });
    const f = highlightFilters(sel, { cadastral: 7, urban: null, document: 4 });
    expect(f["uv-cad-sel-line"]).toEqual(["==", ["get", "id"], 7]);
    expect(f["uv-urban-sel-line"]).toEqual(["==", ["get", "id"], 31]);
    expect(f["uv-cad-hover-line"]).toEqual(NONE);
    expect(f["uv-doc-hover-line"]).toEqual(["==", ["get", "id"], 4]);
    expect(highlightOf({ kind: "point", point: { lng: 1, lat: 2 }, via: "click" })).toEqual({ cadastral: null, urban: null, document: null });
  });

  it("builds the no-data hatch image", () => {
    const img = hatchImage(8);
    expect(img.data.length).toBe(8 * 8 * 4);
    expect(img.data[3]).toBe(160); // (0, 0) is on the diagonal
  });
});

describe("tile pointer", () => {
  const tiles = {
    status: "published",
    data_version: "v7",
    version_id: 7,
    archive_url: "https://s3.example/podgorica/tiles/7/v7.pmtiles?X-Amz-Signature=abc&x=1",
    expires_at: "2026-09-24T12:00:00Z",
  } as never;

  it("carries the page's pointer in the source url fragment", () => {
    expect(hasArchive(tiles)).toBe(true);
    expect(hasArchive({ status: "unpublished", data_version: "unpublished", archive_url: null } as never)).toBe(false);
    const url = sourceUrl(tiles, "http://api:8000");
    expect(url.startsWith("http://api:8000/v1/tiles/current#")).toBe(true);
    const parsed = parseSourceUrl(url);
    expect(parsed.pointerUrl).toBe("http://api:8000/v1/tiles/current");
    expect(parsed.initial).toEqual({
      status: "published",
      archive_url: (tiles as { archive_url: string }).archive_url,
      expires_at: "2026-09-24T12:00:00Z",
      version_id: 7,
    });
    expect(parseSourceUrl("http://api/v1/tiles/current").initial).toBeNull();
  });
});

describe("UrbanViewTileProvider", () => {
  const T0 = Date.parse("2026-09-24T10:00:00Z");
  function reader(name: string, calls: string[], opts: { failFirst?: boolean } = {}): ArchiveReader {
    let failed = false;
    return {
      getHeader: async () => ({ minZoom: 8, maxZoom: 16, minLon: 19.1, minLat: 42.3, maxLon: 19.4, maxLat: 42.5 }),
      getMetadata: async () => ({ vector_layers: [{ id: "zones" }] }),
      getZxy: async (z, x, y) => {
        calls.push(`${name}:${z}/${x}/${y}`);
        if (opts.failFirst && !failed) {
          failed = true;
          throw new Error("403");
        }
        return z === 13 ? { data: new ArrayBuffer(4) } : undefined;
      },
    };
  }
  const pointer = (url: string, expiresMs: number): TilePointer => ({
    status: "published",
    archive_url: url,
    expires_at: new Date(expiresMs).toISOString(),
    version_id: 7,
  });

  it("answers TileJSON from the archive and empty tiles for missing ones", async () => {
    const calls: string[] = [];
    const deps = { fetchPointer: vi.fn(), open: vi.fn(() => reader("a", calls)), now: () => T0 };
    const p = new UrbanViewTileProvider({ url: `http://api/v1/tiles/current#archive=${encodeURIComponent("https://s3/a.pmtiles")}` }, deps);
    const tj = await p.load();
    expect(tj).toMatchObject({ minzoom: 8, maxzoom: 16, bounds: [19.1, 42.3, 19.4, 42.5], vector_layers: [{ id: "zones" }] });
    expect((tj.tiles as string[])[0]).toContain("{z}/{x}/{y}");
    const signal = new AbortController().signal;
    expect((await p.loadTile({ z: 13, x: 1, y: 2 }, { signal })).data).toBeInstanceOf(ArrayBuffer);
    expect((await p.loadTile({ z: 9, x: 1, y: 2 }, { signal })).data).toBeNull();
    expect(deps.fetchPointer).not.toHaveBeenCalled(); // the fragment pointer was enough
    expect(deps.open).toHaveBeenCalledTimes(1);
  });

  it("re-reads the pointer before the signed link expires", async () => {
    let now = T0;
    const calls: string[] = [];
    const deps = {
      fetchPointer: vi.fn(async () => pointer("https://s3/fresh.pmtiles", now + 3_600_000)),
      open: vi.fn((url: string) => reader(url.includes("fresh") ? "fresh" : "old", calls)),
      now: () => now,
    };
    const p = new UrbanViewTileProvider(
      { url: `http://api/v1/tiles/current#archive=${encodeURIComponent("https://s3/old.pmtiles")}&expires=${encodeURIComponent(new Date(T0 + 120_000).toISOString())}&version=7` },
      deps,
    );
    const signal = new AbortController().signal;
    await p.loadTile({ z: 13, x: 0, y: 0 }, { signal });
    now = T0 + 90_000; // within the one-minute margin of expiry
    await p.loadTile({ z: 13, x: 0, y: 1 }, { signal });
    expect(calls).toEqual(["old:13/0/0", "fresh:13/0/1"]);
    expect(deps.fetchPointer).toHaveBeenCalledWith("http://api/v1/tiles/current");
  });

  it("retries once on a fresh link when a range request fails", async () => {
    const calls: string[] = [];
    const deps = {
      fetchPointer: vi.fn(async () => pointer("https://s3/fresh.pmtiles", T0 + 3_600_000)),
      open: vi.fn((url: string) => reader(url.includes("fresh") ? "fresh" : "old", calls, { failFirst: !url.includes("fresh") })),
      now: () => T0,
    };
    const p = new UrbanViewTileProvider({ url: `http://api/v1/tiles/current#archive=${encodeURIComponent("https://s3/old.pmtiles")}` }, deps);
    const res = await p.loadTile({ z: 13, x: 4, y: 4 }, { signal: new AbortController().signal });
    expect(res.data).toBeInstanceOf(ArrayBuffer);
    expect(calls).toEqual(["old:13/4/4", "fresh:13/4/4"]);
  });

  it("fetches the pointer when the page gave none", async () => {
    const deps = {
      fetchPointer: vi.fn(async () => pointer("https://s3/a.pmtiles", T0 + 3_600_000)),
      open: vi.fn(() => reader("a", [])),
      now: () => T0,
    };
    await new UrbanViewTileProvider({ url: "http://api/v1/tiles/current" }, deps).load();
    expect(deps.fetchPointer).toHaveBeenCalledTimes(1);
  });
});

describe("?parcel= links", () => {
  it("reads a positive integer Parcel ID only", () => {
    expect(readParcelParam("?parcel=1042")).toBe(1042);
    expect(readParcelParam("?x=1&parcel=7")).toBe(7);
    for (const bad of ["", "?parcel=", "?parcel=0", "?parcel=-3", "?parcel=12a", "?parcel=1.5"]) expect(readParcelParam(bad)).toBeNull();
  });

  it("sets and clears the parameter, keeping others", () => {
    expect(withParcelParam("http://x/?a=1", 9)).toBe("http://x/?a=1&parcel=9");
    expect(withParcelParam("http://x/?a=1&parcel=9", null)).toBe("http://x/?a=1");
  });
});

describe("outside-coverage flash", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("shows the pill with the panel closed for 2.6 s, then brings the panel back", () => {
    const s = useShell.getState();
    s.flashUncovered("outside_municipality");
    expect(useShell.getState()).toMatchObject({ coverWarn: true, coverReason: "outside_municipality", panelHidden: true });
    vi.advanceTimersByTime(UNCOVERED_FLASH_MS - 1);
    expect(useShell.getState().coverWarn).toBe(true);
    vi.advanceTimersByTime(1);
    expect(useShell.getState()).toMatchObject({ coverWarn: false, panelHidden: false });
  });

  it("a new selection cancels a running flash", () => {
    const s = useShell.getState();
    s.flashUncovered("no_adopted_plan");
    s.clearSelection();
    vi.advanceTimersByTime(UNCOVERED_FLASH_MS);
    expect(useShell.getState()).toMatchObject({ coverWarn: false, panelHidden: false, selection: null });
  });
});
