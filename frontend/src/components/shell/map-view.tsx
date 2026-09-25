"use client";

/**
 * S1 "Map — Landing": Mapbox GL JS with UrbanView's PMTiles layers in the wireframe's map area.
 *
 * - Mapbox GL is imported lazily (its own chunk), only when `NEXT_PUBLIC_MAPBOX_TOKEN` is set;
 *   without a token the wireframe's map background and the chrome still render.
 * - The camera opens on the municipality profile's bounds (or a `?parcel=` link's parcel), zoom and
 *   pan are held to the city (padded bounds, 0.4× … 19), 2D only.
 * - `/v1/tiles/current` names the published archive; the PMTiles provider module
 *   (`lib/map/pmtiles-provider.ts`, registered with `mapboxgl.addTileProvider`) reads it in the
 *   map workers. A new `version_id` recreates the source, so a publish swaps the tiles without a
 *   deploy. Without a published archive only the base map shows: no error, nothing drawn.
 * - Click: the top-priority feature under the pointer is selected (cadastral parcel, planned
 *   parcel, planning-document coverage; `lib/map/pick.ts`), a click on nothing drops a pin and
 *   asks `/v1/locate`. A drag never selects (`clickTolerance` 3 px). Hover outlines parcels and
 *   plan areas and turns the cursor into a pointer.
 * - `map_loaded` is emitted once the style and the first tiles have loaded (`idle`).
 */
import "mapbox-gl/dist/mapbox-gl.css";

import type { Map as MapboxMap, MapMouseEvent, Marker } from "mapbox-gl";
import { useEffect, useRef } from "react";

import { getTracker } from "@/lib/analytics/react";
import { useTilesCurrent } from "@/lib/api/hooks";
import type { MunicipalityProfile, TilesCurrent } from "@/lib/api/types";
import { formatZoomFactor, scaleBar } from "@/lib/format";
import { pickFeature, type PickType } from "@/lib/map/pick";
import {
  HATCH_IMAGE,
  HIT_LAYERS,
  UV_LAYERS,
  choroplethStyle,
  hatchImage,
  highlightFilters,
  visibleLayerIds,
  type Highlight,
} from "@/lib/map/style";
import { SOURCE_ID, hasArchive, registerTileProvider, vectorSource } from "@/lib/map/tiles";
import { useSelection } from "@/lib/selection";
import { highlightOf, useShell } from "@/lib/store";

const TOKEN = process.env.NEXT_PUBLIC_MAPBOX_TOKEN ?? "";
const STYLE = process.env.NEXT_PUBLIC_MAPBOX_STYLE || "mapbox://styles/mapbox/light-v11";
const FIT_PADDING = 24;
/** The wireframe's zoom range around the city framing: 0.4× … and parcel detail. */
const MIN_ZOOM_FACTOR = 0.4;
const MAX_ZOOM = 19;
const FOCUS_MAX_ZOOM = 18;
/** How far outside the municipality bounds the camera may go, as a share of their size. */
const MAX_BOUNDS_PAD = 0.25;

/** The wireframe's selection pin (terracotta drop, white ring and dot), tip at the bottom. */
const PIN_SVG =
  '<svg width="30" height="39" viewBox="-13 -27 26 34" aria-hidden="true">' +
  '<path d="M0,-26 c-7,0 -12,5 -12,12 c0,9 12,20 12,20 c0,0 12,-11 12,-20 c0,-7 -5,-12 -12,-12 z" fill="#B5613B" stroke="#ffffff" stroke-width="1.5"/>' +
  '<circle cx="0" cy="-14" r="3.6" fill="#ffffff"/></svg>';

type Bbox = [number, number, number, number];
const NO_HOVER: Highlight = { cadastral: null, urban: null, document: null };

// Start fetching Mapbox GL as soon as this module is evaluated (hydration), not at effect time.
const mapboxModule = TOKEN && typeof window !== "undefined" ? import("mapbox-gl") : null;

/** Add (or replace) the UrbanView source and layers for the current published archive. */
function installTiles(map: MapboxMap, tiles: TilesCurrent | undefined): number | null {
  for (const l of [...UV_LAYERS].reverse()) if (map.getLayer(l.id)) map.removeLayer(l.id);
  if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
  if (!hasArchive(tiles)) return null; // unpublished: base map only, nothing drawn
  // `provider` is a Mapbox GL 3.x source option its bundled style spec does not list yet
  map.addSource(SOURCE_ID, vectorSource(tiles) as unknown as Parameters<MapboxMap["addSource"]>[1]);
  const beforeId = map.getStyle()?.layers?.find((l) => l.type === "symbol")?.id;
  for (const l of UV_LAYERS) {
    // UrbanView's fills and lines go under the base map's labels; our own labels on top
    map.addLayer(l as unknown as Parameters<MapboxMap["addLayer"]>[0], l.type === "symbol" ? undefined : beforeId);
  }
  return tiles.version_id ?? null;
}

function syncVisibility(map: MapboxMap) {
  const s = useShell.getState();
  const visible = visibleLayerIds(s.layers, { marketUnlocked: s.marketUnlocked });
  for (const l of UV_LAYERS) {
    if (!map.getLayer(l.id)) continue;
    map.setLayoutProperty(l.id, "visibility", visible.has(l.id) ? "visible" : "none");
  }
}

/** Choropleth colours and filters for the selected fields and the served classes (no reload). */
function syncChoropleth(map: MapboxMap, tiles: TilesCurrent | undefined) {
  const styles = choroplethStyle(useShell.getState().choropleth, tiles?.cell_classes);
  for (const [id, { filter, paint }] of Object.entries(styles)) {
    if (!map.getLayer(id)) continue;
    map.setFilter(id, filter as Parameters<MapboxMap["setFilter"]>[1]);
    for (const [prop, value] of Object.entries(paint ?? {})) {
      map.setPaintProperty(id, prop as never, value as never);
    }
  }
}

function syncHighlight(map: MapboxMap, hover: Highlight) {
  const filters = highlightFilters(highlightOf(useShell.getState().selection), hover);
  for (const [id, filter] of Object.entries(filters)) {
    if (map.getLayer(id)) map.setFilter(id, filter as Parameters<MapboxMap["setFilter"]>[1]);
  }
}

/** Hit layers present and visible right now (querying a missing layer throws). */
function queryableHitLayers(map: MapboxMap): string[] {
  return Object.values(HIT_LAYERS).filter((id) => map.getLayer(id) && map.getLayoutProperty(id, "visibility") !== "none");
}

export function MapView({
  profile,
  initialTiles,
}: {
  profile: MunicipalityProfile | undefined;
  initialTiles?: TilesCurrent | null;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapboxMap | null>(null);
  const installedVersion = useRef<number | null | undefined>(undefined);
  const { data: tiles } = useTilesCurrent(initialTiles);
  const tilesRef = useRef(tiles);
  const { selectPoint, selectFeature } = useSelection();
  const handlers = useRef({ selectPoint, selectFeature });

  useEffect(() => {
    handlers.current = { selectPoint, selectFeature };
  }, [selectPoint, selectFeature]);

  // Tile pointer: remember it, show its data version, swap the source when a publish changed it.
  useEffect(() => {
    tilesRef.current = tiles;
    useShell.getState().setDataVersion(tiles?.data_version ?? null);
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded() || installedVersion.current === undefined) return;
    const next = hasArchive(tiles) ? (tiles.version_id ?? null) : null;
    if (next !== installedVersion.current) {
      installedVersion.current = installTiles(map, tiles);
      syncVisibility(map);
      syncHighlight(map, NO_HOVER);
    }
    syncChoropleth(map, tiles);
  }, [tiles]);

  // No Mapbox token: the map area is the wireframe background; the page still counts as loaded.
  useEffect(() => {
    if (TOKEN) return;
    if (process.env.NODE_ENV !== "production") {
      console.info("[map] NEXT_PUBLIC_MAPBOX_TOKEN is not set: the map area shows the wireframe background only.");
    }
    getTracker().trackOnce("map_loaded", "map_loaded", { renderer: "none" });
  }, []);

  const profileId = profile?.id;
  const bounds = profile?.bounds as Bbox | undefined;

  useEffect(() => {
    if (!TOKEN || !mapboxModule || !profileId || !bounds || !containerRef.current) return;
    let cancelled = false;
    let raf = 0;
    let hoverRaf = 0;
    let marker: Marker | null = null;
    const unsubscribers: (() => void)[] = [];
    const container = containerRef.current;
    const started = typeof performance !== "undefined" ? performance.now() : 0;

    void (async () => {
      const mapboxgl = (await mapboxModule).default;
      if (cancelled) return;
      mapboxgl.accessToken = TOKEN;
      const providerReady = registerTileProvider(mapboxgl as unknown as { addTileProvider?: (n: string, u: string) => void });
      if (!providerReady && process.env.NODE_ENV !== "production") {
        console.warn("[map] this Mapbox GL build has no addTileProvider: UrbanView layers cannot load.");
      }

      const [w, s, e, n] = bounds;
      const padX = (e - w) * MAX_BOUNDS_PAD;
      const padY = (n - s) * MAX_BOUNDS_PAD;
      const cityBounds: [[number, number], [number, number]] = [
        [w, s],
        [e, n],
      ];
      const focus = useShell.getState().focus;
      const startBounds: [[number, number], [number, number]] = focus?.bbox
        ? [
            [focus.bbox[0], focus.bbox[1]],
            [focus.bbox[2], focus.bbox[3]],
          ]
        : cityBounds;

      const map = new mapboxgl.Map({
        container,
        style: STYLE,
        bounds: startBounds,
        fitBoundsOptions: { padding: FIT_PADDING, maxZoom: focus?.bbox ? FOCUS_MAX_ZOOM : undefined },
        maxBounds: [
          [w - padX, s - padY],
          [e + padX, n + padY],
        ],
        maxZoom: MAX_ZOOM,
        dragRotate: false,
        pitchWithRotate: false,
        touchPitch: false,
        clickTolerance: 3, // a drag of more than 3 px is a pan, never a selection
        attributionControl: false,
        // top-right is the only corner the wireframe chrome leaves free
        logoPosition: "top-right",
      });
      mapRef.current = map;
      if (focus) useShell.getState().setFocus(null);
      // Mapbox only follows window resizes; the map's box also changes when the rail collapses or
      // opens, the panel hides (outside coverage) or the bottom sheet moves: redraw at the new size.
      let resizeRaf = 0;
      const resizeObserver = new ResizeObserver(() => {
        cancelAnimationFrame(resizeRaf);
        resizeRaf = requestAnimationFrame(() => map.resize());
      });
      resizeObserver.observe(container);
      unsubscribers.push(() => {
        cancelAnimationFrame(resizeRaf);
        resizeObserver.disconnect();
      });
      map.touchZoomRotate.disableRotation();
      map.keyboard.disableRotation();
      map.addControl(new mapboxgl.AttributionControl({ compact: true }), "top-right");

      // The zoom label is relative to the city framing (1.0× = whole municipality).
      const baseZoom = map.cameraForBounds(cityBounds, { padding: FIT_PADDING })?.zoom ?? map.getZoom();
      map.setMinZoom(Math.max(0, baseZoom + Math.log2(MIN_ZOOM_FACTOR)));

      const updateCamera = () => {
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(() => {
          const zoom = map.getZoom();
          useShell.getState().setCamera(formatZoomFactor(zoom, baseZoom), scaleBar(map.getCenter().lat, zoom));
        });
      };
      map.on("move", updateCamera);
      updateCamera();

      let hover: Highlight = NO_HOVER;
      map.on("load", () => {
        const img = hatchImage();
        if (!map.hasImage(HATCH_IMAGE)) map.addImage(HATCH_IMAGE, img);
        installedVersion.current = installTiles(map, tilesRef.current);
        syncVisibility(map);
        syncChoropleth(map, tilesRef.current);
        syncHighlight(map, hover);
        map.once("idle", () => {
          const loadMs = Math.round((typeof performance !== "undefined" ? performance.now() : 0) - started);
          getTracker().trackOnce("map_loaded", "map_loaded", { load_ms: loadMs, renderer: "mapbox" });
        });
      });
      map.on("error", (ev) => {
        // tile and provider trouble never becomes a visible error state on the public map
        if (process.env.NODE_ENV !== "production") console.warn("[map]", ev.error);
      });

      map.on("click", (ev: MapMouseEvent) => {
        const layers = queryableHitLayers(map);
        const picked = layers.length ? pickFeature(map.queryRenderedFeatures(ev.point, { layers }) as never) : null;
        const point = { lng: ev.lngLat.lng, lat: ev.lngLat.lat };
        if (picked) void handlers.current.selectFeature(picked, point);
        else void handlers.current.selectPoint(point, "click");
      });

      map.on("mousemove", (ev: MapMouseEvent) => {
        cancelAnimationFrame(hoverRaf);
        hoverRaf = requestAnimationFrame(() => {
          const layers = queryableHitLayers(map);
          const picked = layers.length ? pickFeature(map.queryRenderedFeatures(ev.point, { layers }) as never) : null;
          map.getCanvas().style.cursor = picked ? "pointer" : "";
          const next: Highlight = { ...NO_HOVER };
          if (picked) next[picked.type as PickType] = picked.id;
          if (next.cadastral !== hover.cadastral || next.urban !== hover.urban || next.document !== hover.document) {
            hover = next;
            syncHighlight(map, hover);
          }
        });
      });
      map.getCanvas().addEventListener("mouseleave", () => {
        cancelAnimationFrame(hoverRaf);
        map.getCanvas().style.cursor = "";
        if (hover !== NO_HOVER) {
          hover = NO_HOVER;
          syncHighlight(map, hover);
        }
      });

      useShell.getState().registerMap({
        zoomIn: () => map.zoomIn(),
        zoomOut: () => map.zoomOut(),
        reset: () => map.fitBounds(cityBounds, { padding: FIT_PADDING }),
        flyTo: (p, zoom = 17) => map.flyTo({ center: [p.lng, p.lat], zoom: Math.max(map.getZoom(), zoom), essential: true }),
        fitBounds: (bbox) =>
          map.fitBounds(
            [
              [bbox[0], bbox[1]],
              [bbox[2], bbox[3]],
            ],
            { padding: FIT_PADDING * 4, maxZoom: FOCUS_MAX_ZOOM },
          ),
      });

      const el = document.createElement("div");
      el.className = "uv-pin";
      el.innerHTML = PIN_SVG;
      marker = new mapboxgl.Marker({ element: el, anchor: "bottom" });
      const syncPin = (pin: { lng: number; lat: number } | null) => {
        if (pin) marker!.setLngLat([pin.lng, pin.lat]).addTo(map);
        else marker!.remove();
      };
      syncPin(useShell.getState().pin);
      unsubscribers.push(
        useShell.subscribe((st, prev) => {
          if (st.pin !== prev.pin) syncPin(st.pin);
          if (st.selection !== prev.selection && map.isStyleLoaded()) syncHighlight(map, hover);
          if ((st.layers !== prev.layers || st.marketUnlocked !== prev.marketUnlocked) && map.isStyleLoaded()) syncVisibility(map);
          if (st.choropleth !== prev.choropleth && map.isStyleLoaded()) syncChoropleth(map, tilesRef.current);
          if (st.focus && st.focus !== prev.focus) {
            const f = st.focus;
            useShell.getState().setFocus(null);
            if (f.bbox) useShell.getState().map?.fitBounds(f.bbox);
            else if (f.point) useShell.getState().map?.flyTo(f.point);
          }
        }),
      );
    })();

    return () => {
      cancelled = true;
      cancelAnimationFrame(raf);
      cancelAnimationFrame(hoverRaf);
      unsubscribers.forEach((u) => u());
      useShell.getState().registerMap(null);
      marker?.remove();
      mapRef.current?.remove();
      mapRef.current = null;
      installedVersion.current = undefined;
    };
    // bounds is a fresh array per profile object; the profile id decides re-creation
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profileId]);

  return <div id="map" ref={containerRef} />;
}
