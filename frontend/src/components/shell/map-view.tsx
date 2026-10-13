"use client";

/**
 * S1 "Map — Landing": Mapbox GL JS with UrbanView's PMTiles layers in the wireframe's map area.
 *
 * - Mapbox GL is imported lazily (its own chunk), only when `NEXT_PUBLIC_MAPBOX_TOKEN` is set;
 *   without a token the wireframe's map background and the chrome still render.
 * - The camera opens on the municipality profile's bounds, the city extent (or a `?parcel=` link's
 *   parcel). 2D only: plain Web Mercator (the base style's globe is overridden) and no rotation or
 *   pitch. 1.0× is the city framing of the map's box; the zoom goes from 0.4× to parcel detail, and
 *   the camera stays inside the field the 0.4× view shows, centred on the city, so zooming all the
 *   way out ends on the whole city, centred (the wireframe's rule). Framing, zoom range and field
 *   are measured again whenever the box changes (rail, panel, window).
 * - `/v1/tiles/current` names the published archive; the PMTiles provider module
 *   (`lib/map/pmtiles-provider.ts`, registered with `mapboxgl.addTileProvider`) reads it in the
 *   map workers. A new `version_id` recreates the source, so a publish swaps the tiles without a
 *   deploy. Without a published archive only the base map shows: no error, nothing drawn. Every
 *   layer starts at the zoom its data is built from (the pointer's per-layer range, `uvLayers`).
 * - The map follows the rail exactly. Once the style has loaded, every change of the layers, the
 *   choropleth fields and the selection is applied the moment it happens,
 *   also while tiles are still loading (Mapbox takes layout, filter and paint changes then; the
 *   state at `style.load` is applied by its handler). The zoom goes to the store, so a card and
 *   its legend group say "zoom in to see" for a layer that is on but not drawn at this zoom.
 *   The layers of a card exist on the map only while the card is on (`syncLayers`): every tile
 *   is processed once per style layer, so the twelve heatmap layers of a card that is off cost
 *   nothing (the client's review of 2026-10-12: "navigation could be faster"). Highlights are
 *   feature-state flags, never filters (`syncHighlight`), and tiles draw without a cross-fade.
 * - `?fps=1` shows a frame-rate meter in the map's corner and exposes the map as
 *   `window.__uvMap`, for a measurement on the visitor's own machine; nothing else changes.
 * - Click: the top-priority feature under the pointer is selected (cadastral parcel, planned
 *   parcel, urban block, planning-document coverage; `lib/map/pick.ts`), a click on nothing drops
 *   a pin and asks `/v1/locate`. A drag never selects (`clickTolerance` 3 px). Hover outlines
 *   parcels, blocks and plan areas and turns the cursor into a pointer. Wheel and trackpad zoom
 *   faster than Mapbox's defaults; hit tests pause while the camera moves.
 * - With Mapbox's light style (the default) the base map is restyled to the wireframe's (land,
 *   white major and cream minor roads, the river; no buildings, land cover or points of interest:
 *   `lib/map/basemap.ts`) before UrbanView's layers go on top.
 * - `map_loaded` is emitted once the style and the first tiles have loaded (`idle`).
 */
import "mapbox-gl/dist/mapbox-gl.css";

import type { Map as MapboxMap, MapMouseEvent, Marker } from "mapbox-gl";
import { useEffect, useRef } from "react";

import { getTracker } from "@/lib/analytics/react";
import { useTilesCurrent } from "@/lib/api/hooks";
import type { MunicipalityProfile, TilesCurrent } from "@/lib/api/types";
import { formatZoomFactor, scaleBar } from "@/lib/format";
import { sourceLayerEmpty, sourceLayerMissing } from "@/lib/layers";
import { LIGHT_STYLE, wireframeBasemap } from "@/lib/map/basemap";
import { fitOptions, fitPadding, legendOffset } from "@/lib/map/camera";
import { pickFeature, type PickType } from "@/lib/map/pick";
import {
  HATCH_IMAGE,
  HIT_LAYERS,
  UV_LAYERS,
  choroplethStyle,
  hatchImage,
  highlightStates,
  insertBefore,
  uvLayers,
  visibleLayerIds,
  type FeatureFlags,
  type Highlight,
} from "@/lib/map/style";
import { SOURCE_ID, hasArchive, registerTileProvider, vectorSource } from "@/lib/map/tiles";
import { apiBaseUrl } from "@/lib/api/client";
import { useSelection } from "@/lib/selection";
import { highlightOf, useShell } from "@/lib/store";

const TOKEN = process.env.NEXT_PUBLIC_MAPBOX_TOKEN ?? "";
const STYLE = process.env.NEXT_PUBLIC_MAPBOX_STYLE || LIGHT_STYLE;
const FIT_PADDING = 24;
/** Padding around a searched parcel or zone. */
const FOCUS_PADDING = FIT_PADDING * 4;
/** The wireframe's zoom range around the city framing: 0.4× … and parcel detail. */
const MIN_ZOOM_FACTOR = 0.4;
const MAX_ZOOM = 19;
const FOCUS_MAX_ZOOM = 18;
/** A hair over the 0.4× view, so rounding never pushes the zoomed-out camera off the field. */
const FIELD_SLACK = 1 + 1e-6;

/** The wireframe's selection pin (terracotta drop, white ring and dot), tip at the bottom. */
const PIN_SVG =
  '<svg width="30" height="39" viewBox="-13 -27 26 34" aria-hidden="true">' +
  '<path d="M0,-26 c-7,0 -12,5 -12,12 c0,9 12,20 12,20 c0,0 12,-11 12,-20 c0,-7 -5,-12 -12,-12 z" fill="#B5613B" stroke="#ffffff" stroke-width="1.5"/>' +
  '<circle cx="0" cy="-14" r="3.6" fill="#ffffff"/></svg>';

type Bbox = [number, number, number, number];
type LngLatBounds = [[number, number], [number, number]];
const NO_HOVER: Highlight = { cadastral: null, urban: null, document: null, block: null, zone: null };
/** The feature-state flags each map currently carries (`syncHighlight`). */
const FLAGGED = new WeakMap<MapboxMap, Map<string, FeatureFlags>>();
const PRECONNECTED = new Set<string>();

// Start fetching Mapbox GL as soon as this module is evaluated (hydration), not at effect time.
const mapboxModule = TOKEN && typeof window !== "undefined" ? import("mapbox-gl") : null;

/** Open the connection to a host before its first request (the API, the tile archive's host). */
function preconnect(href: string | null | undefined) {
  if (!href || typeof document === "undefined") return;
  let origin: string;
  try {
    origin = new URL(href, window.location.href).origin;
  } catch {
    return;
  }
  if (origin === window.location.origin || PRECONNECTED.has(origin)) return;
  PRECONNECTED.add(origin);
  const link = document.createElement("link");
  link.rel = "preconnect";
  link.href = origin;
  link.crossOrigin = "anonymous";
  document.head.appendChild(link);
}

const toBounds = (b: Bbox): LngLatBounds => [
  [b[0], b[1]],
  [b[2], b[3]],
];

/** The wireframe's colours on the light style's own layers (a custom style is left as designed). */
function applyBasemap(map: MapboxMap) {
  if (STYLE !== LIGHT_STYLE) return;
  for (const change of wireframeBasemap(map.getStyle()?.layers ?? [])) {
    if (!map.getLayer(change.id)) continue;
    try {
      if ("hide" in change) map.setLayoutProperty(change.id, "visibility", "none");
      else for (const [prop, value] of Object.entries(change.paint)) map.setPaintProperty(change.id, prop as never, value as never);
    } catch (err) {
      // a base style that differs from the one this was written for keeps its own look there
      if (process.env.NODE_ENV !== "production") console.warn("[map] base map layer left as is", change.id, err);
    }
  }
}

/** The two places UrbanView's layers go in the base style: under its labels, or above its buildings. */
function anchorsOf(map: MapboxMap): { labels: string | undefined; aboveBuildings: string | undefined } {
  const baseLayers = (map.getStyle()?.layers ?? []).filter((l) => !l.id.startsWith("uv-"));
  const labels = baseLayers.find((l) => l.type === "symbol")?.id;
  // right above the base style's buildings (under its roads); without a `building` layer, under the labels
  const buildingIndex = baseLayers.findIndex((l) => l.id === "building");
  const aboveBuildings = buildingIndex >= 0 ? (baseLayers[buildingIndex + 1]?.id ?? labels) : labels;
  return { labels, aboveBuildings };
}

/** Add (or replace) the UrbanView source and the layers the rail shows, for the current archive. */
function installTiles(map: MapboxMap, tiles: TilesCurrent | undefined): number | null {
  for (const l of [...UV_LAYERS].reverse()) if (map.getLayer(l.id)) map.removeLayer(l.id);
  if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
  FLAGGED.delete(map); // a new source carries no feature state
  if (!hasArchive(tiles)) return null; // unpublished: base map only, nothing drawn
  // `provider` is a Mapbox GL 3.x source option its bundled style spec does not list yet
  map.addSource(SOURCE_ID, vectorSource(tiles) as unknown as Parameters<MapboxMap["addSource"]>[1]);
  syncLayers(map, tiles);
  // the plan's ground covers the base map inside the plans: its water (the Morača inside Stara
  // Varoš) comes back over it, under the plan's footprints
  const { aboveBuildings } = anchorsOf(map);
  if (map.getLayer("uv-plan-ground") && aboveBuildings) {
    const overGround = map.getLayer("uv-plan-buildings") ? "uv-plan-buildings" : aboveBuildings;
    for (const id of ["water", "waterway"]) if (map.getLayer(id)) map.moveLayer(id, overGround);
  }
  return tiles.version_id ?? null;
}

/**
 * The map carries exactly the layers the rail shows: a card switched off takes its layers off the
 * map (Mapbox then does no work for them in any tile), a card switched on adds them back in
 * catalogue order (`insertBefore`), so the drawing order never changes.
 */
function syncLayers(map: MapboxMap, tiles: TilesCurrent | undefined) {
  if (!map.getSource(SOURCE_ID)) return;
  const visible = visibleLayerIds(useShell.getState().layers);
  for (const l of [...UV_LAYERS].reverse()) if (map.getLayer(l.id) && !visible.has(l.id)) map.removeLayer(l.id);
  // not in the archive (a version published before the layer existed, or nothing published for it)
  const order = uvLayers(tiles).filter((l) => !sourceLayerMissing(l["source-layer"], tiles) && !sourceLayerEmpty(l["source-layer"], tiles));
  const anchors = anchorsOf(map);
  const present = (id: string) => !!map.getLayer(id);
  order.forEach((l, i) => {
    if (!visible.has(l.id) || present(l.id)) return;
    map.addLayer(l as unknown as Parameters<MapboxMap["addLayer"]>[0], insertBefore(order, i, present, anchors));
  });
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

/** Flag the selected and hovered features (feature-state), unflag the rest: a paint change only. */
function syncHighlight(map: MapboxMap, hover: Highlight) {
  if (!map.getSource(SOURCE_ID)) return;
  const want = highlightStates(highlightOf(useShell.getState().selection), hover);
  const have = FLAGGED.get(map) ?? new Map<string, FeatureFlags>();
  for (const [key, f] of have) {
    if (!want[key]) map.removeFeatureState({ source: SOURCE_ID, sourceLayer: f.sourceLayer, id: f.id });
  }
  const next = new Map<string, FeatureFlags>();
  for (const [key, f] of Object.entries(want)) {
    const prev = have.get(key);
    if (!prev || prev.hover !== f.hover || prev.selected !== f.selected) {
      map.setFeatureState({ source: SOURCE_ID, sourceLayer: f.sourceLayer, id: f.id }, { hover: f.hover, selected: f.selected });
    }
    next.set(key, f);
  }
  FLAGGED.set(map, next);
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
  /** The archive version on the map; `undefined` until the style has loaded (no layers yet). */
  const installedVersion = useRef<number | null | undefined>(undefined);
  const { data: tiles } = useTilesCurrent(initialTiles);
  const tilesRef = useRef(tiles);
  const { selectPoint, selectFeature } = useSelection();
  const handlers = useRef({ selectPoint, selectFeature });

  useEffect(() => {
    handlers.current = { selectPoint, selectFeature };
  }, [selectPoint, selectFeature]);

  // Tile pointer: remember it, swap the source when a publish changed it.
  useEffect(() => {
    tilesRef.current = tiles;
    const map = mapRef.current;
    // before `style.load` there is nothing to swap: its handler installs this pointer
    if (!map || installedVersion.current === undefined) return;
    const next = hasArchive(tiles) ? (tiles.version_id ?? null) : null;
    if (next !== installedVersion.current) {
      installedVersion.current = installTiles(map, tiles);
      syncHighlight(map, NO_HOVER);
    }
    syncChoropleth(map, tiles);
  }, [tiles]);

  // the archive's host is known from the pointer: connect before the first tile request
  useEffect(() => {
    if (hasArchive(tiles)) preconnect(tiles.archive_url);
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

    preconnect(apiBaseUrl());
    void (async () => {
      const mapboxgl = (await mapboxModule).default;
      if (cancelled) return;
      mapboxgl.accessToken = TOKEN;
      const providerReady = registerTileProvider(mapboxgl as unknown as { addTileProvider?: (n: string, u: string) => void });
      if (!providerReady && process.env.NODE_ENV !== "production") {
        console.warn("[map] this Mapbox GL build has no addTileProvider: UrbanView layers cannot load.");
      }

      const [w, s, e, n] = bounds;
      const cityBounds = toBounds(bounds);
      // the city's centre as a fit centres it: in Mercator units, between the corners
      const nw = mapboxgl.MercatorCoordinate.fromLngLat({ lng: w, lat: n });
      const se = mapboxgl.MercatorCoordinate.fromLngLat({ lng: e, lat: s });
      const cityX = (nw.x + se.x) / 2;
      const cityY = (nw.y + se.y) / 2;
      const focus = useShell.getState().focus;
      const startBounds = focus?.bbox ? toBounds(focus.bbox) : cityBounds;
      const startMaxZoom = focus?.bbox ? FOCUS_MAX_ZOOM : undefined;
      /** The box has a size (a fit into a 0 × 0 box is dropped). */
      const hasRoom = () => container.clientWidth > 2 && container.clientHeight > 2;
      const padFor = (want: number) => fitPadding(want, container.clientWidth, container.clientHeight);

      const map = new mapboxgl.Map({
        container,
        style: STYLE,
        bounds: startBounds,
        fitBoundsOptions: fitOptions(padFor(FIT_PADDING), startMaxZoom),
        // 2D: flat Mercator at every zoom, and the camera kept inside its field (`measureFraming`);
        // the style's globe would only constrain the centre, and not before the style has loaded
        projection: "mercator",
        maxZoom: MAX_ZOOM,
        dragRotate: false,
        pitchWithRotate: false,
        touchPitch: false,
        clickTolerance: 3, // a drag of more than 3 px is a pan, never a selection
        fadeDuration: 0, // a loaded tile is drawn at once, not faded in over 300 ms
        attributionControl: false,
        // top-right is the only corner the wireframe chrome leaves free
        logoPosition: "top-right",
      });
      mapRef.current = map;
      if (focus) useShell.getState().setFocus(null);
      map.touchZoomRotate.disableRotation();
      map.keyboard.disableRotation();
      // snappier zoom than Mapbox's defaults (a wheel notch 1/450 of a level, a trackpad pixel
      // 1/100): the client's review of 2026-10-12 found zooming slow and heavy
      map.scrollZoom.setWheelZoomRate(1 / 200);
      map.scrollZoom.setZoomRate(1 / 60);
      map.addControl(new mapboxgl.AttributionControl({ compact: true }), "top-right");

      /** The style has loaded: UrbanView's layers exist and take changes (set once, in `style.load`). */
      let ready = false;
      /**
       * The first view is the start bounds (S1: the city extent). The constructor fits them when the
       * box already has its size; otherwise the first resize that gives it one does.
       */
      let framed = hasRoom();

      // The zoom label is relative to the city framing (1.0× = the whole municipality in the box).
      let baseZoom = map.getZoom();
      /**
       * The city framing of the box as it is now, the lowest zoom (0.4× of it) and the field the
       * camera stays in: what the 0.4× view shows around the city's centre. At the lowest zoom the
       * view is that field, so the city sits in its middle; closer in, panning ends at its edges.
       */
      const measureFraming = () => {
        if (!hasRoom()) return;
        const zoom = map.cameraForBounds(cityBounds, { padding: padFor(FIT_PADDING) })?.zoom;
        if (zoom == null || !Number.isFinite(zoom)) return;
        baseZoom = zoom;
        const minZoom = Math.max(0, baseZoom + Math.log2(MIN_ZOOM_FACTOR));
        const world = 512 * Math.pow(2, minZoom); // px across the world at that zoom
        const halfW = (container.clientWidth / 2 / world) * FIELD_SLACK;
        const halfH = (container.clientHeight / 2 / world) * FIELD_SLACK;
        const fieldSw = new mapboxgl.MercatorCoordinate(cityX - halfW, cityY + halfH).toLngLat();
        const fieldNe = new mapboxgl.MercatorCoordinate(cityX + halfW, cityY - halfH).toLngLat();
        map.setMinZoom(minZoom);
        map.setMaxBounds([
          [fieldSw.lng, fieldSw.lat],
          [fieldNe.lng, fieldNe.lat],
        ]);
      };
      // the store (and with it the chrome, the rail and the legend) hears about the camera only
      // when something it shows changed: a pan at one zoom re-renders nothing
      let lastCamera = "";
      const updateCamera = () => {
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(() => {
          const zoom = map.getZoom();
          const label = formatZoomFactor(zoom, baseZoom);
          const scale = scaleBar(map.getCenter().lat, zoom);
          const key = `${label}|${scale.label}|${scale.widthPx}|${zoom.toFixed(2)}`;
          if (key === lastCamera) return;
          lastCamera = key;
          useShell.getState().setCamera(label, scale, zoom);
        });
      };
      measureFraming();
      map.on("move", updateCamera);
      updateCamera();

      // Mapbox only follows window resizes; the map's box also changes when the rail collapses or
      // opens or the panel hides (outside coverage): redraw at the new size.
      // Resizing wipes the drawing surface and Mapbox repaints a frame later, which shows as a
      // blink; paint the new size in the same frame (Mapbox's own frame function, when present).
      const paintNow = (map as unknown as { _render?: (now: number) => void })._render;
      const resizeObserver = new ResizeObserver(() => {
        map.resize();
        if (!framed && hasRoom()) {
          map.fitBounds(startBounds, { ...fitOptions(padFor(FIT_PADDING), startMaxZoom), duration: 0 });
          framed = true;
        }
        measureFraming();
        updateCamera();
        if (ready && typeof paintNow === "function") paintNow.call(map, performance.now());
      });
      resizeObserver.observe(container);
      unsubscribers.push(() => resizeObserver.disconnect());

      let hover: Highlight = NO_HOVER;
      /** Apply the rail, the choropleth fields and the selection as they are now. */
      const syncAll = () => {
        syncLayers(map, tilesRef.current);
        syncChoropleth(map, tilesRef.current);
        syncHighlight(map, hover);
      };

      // `?fps=1`: a frame-rate meter in the map's corner and the map on `window`, for a
      // measurement on the visitor's own machine (the client's review of 2026-10-12)
      let fpsRaf = 0;
      if (new URLSearchParams(window.location.search).get("fps") === "1") {
        (window as unknown as { __uvMap?: MapboxMap }).__uvMap = map;
        const meter = document.createElement("div");
        meter.className = "uv-fps";
        container.appendChild(meter);
        const recent: number[] = [];
        let lastTick = performance.now();
        const tick = (now: number) => {
          recent.push(now - lastTick);
          lastTick = now;
          while (recent.length > 60) recent.shift();
          const mean = recent.reduce((a, b) => a + b, 0) / recent.length;
          const worst = Math.max(...recent);
          meter.textContent = `${Math.round(1000 / mean)} fps · worst ${Math.round(worst)} ms · tiles ${map.areTilesLoaded() ? "ready" : "loading"}`;
          fpsRaf = requestAnimationFrame(tick);
        };
        fpsRaf = requestAnimationFrame(tick);
        unsubscribers.push(() => {
          cancelAnimationFrame(fpsRaf);
          meter.remove();
          delete (window as unknown as { __uvMap?: MapboxMap }).__uvMap;
        });
      }
      /**
       * Apply one change; should Mapbox refuse it (a style that is not done loading), apply the
       * whole state again once the map is idle, so the map never stays behind the rail.
       */
      const apply = (sync: () => void) => {
        try {
          sync();
        } catch (err) {
          if (process.env.NODE_ENV !== "production") console.warn("[map] layer update deferred", err);
          map.once("idle", syncAll);
        }
      };

      // `style.load`, not `load`: UrbanView's source goes on as soon as the style is parsed, so its
      // tiles download alongside the base map's instead of after all of them (first render)
      map.on("style.load", () => {
        const img = hatchImage();
        if (!map.hasImage(HATCH_IMAGE)) map.addImage(HATCH_IMAGE, img);
        applyBasemap(map);
        installedVersion.current = installTiles(map, tilesRef.current);
        ready = true;
        syncAll();
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
          if (map.isMoving()) return; // no hit tests while panning or zooming: the frame budget is the camera's
          const layers = queryableHitLayers(map);
          const picked = layers.length ? pickFeature(map.queryRenderedFeatures(ev.point, { layers }) as never) : null;
          map.getCanvas().style.cursor = picked ? "pointer" : "";
          const next: Highlight = { ...NO_HOVER };
          if (picked) next[picked.type as PickType] = picked.id;
          if (
            next.cadastral !== hover.cadastral ||
            next.urban !== hover.urban ||
            next.block !== hover.block ||
            next.document !== hover.document
          ) {
            hover = next;
            apply(() => syncHighlight(map, hover));
          }
        });
      });
      map.getCanvas().addEventListener("mouseleave", () => {
        cancelAnimationFrame(hoverRaf);
        map.getCanvas().style.cursor = "";
        if (hover !== NO_HOVER) {
          hover = NO_HOVER;
          apply(() => syncHighlight(map, hover));
        }
      });

      /** How far right of the centre a flown-to place goes so the open legend does not cover it. */
      const legendShift = () => {
        const el = container.parentElement?.querySelector<HTMLElement>("#legend");
        if (!el) return 0;
        const box = container.getBoundingClientRect();
        const rect = el.getBoundingClientRect();
        return legendOffset(
          { width: box.width, height: box.height },
          { right: rect.right - box.left, bottom: rect.bottom - box.top },
        );
      };

      useShell.getState().registerMap({
        zoomIn: () => map.zoomIn({ duration: 220 }),
        zoomOut: () => map.zoomOut({ duration: 220 }),
        reset: () => map.fitBounds(cityBounds, fitOptions(padFor(FIT_PADDING))),
        flyTo: (p, zoom = 17) =>
          map.flyTo({
            center: [p.lng, p.lat],
            zoom: Math.max(map.getZoom(), zoom),
            essential: true,
            // on a small window the legend covers the map's middle: land beside it, not under it
            offset: [legendShift(), 0],
          }),
        fitBounds: (bbox) => map.fitBounds(toBounds(bbox), fitOptions(padFor(FOCUS_PADDING), FOCUS_MAX_ZOOM)),
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
          // Before `style.load` there are no layers: its handler applies the state as it is then.
          // After it, every change is applied at once, whether or not tiles are still loading.
          if (ready) {
            if (st.layers !== prev.layers) {
              // a card switched on adds its layers with their default paint: the choropleth
              // fields and classes go on them right after
              apply(() => {
                syncLayers(map, tilesRef.current);
                syncChoropleth(map, tilesRef.current);
              });
            }
            if (st.choropleth !== prev.choropleth) apply(() => syncChoropleth(map, tilesRef.current));
            if (st.selection !== prev.selection) apply(() => syncHighlight(map, hover));
          }
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
      useShell.setState({ zoom: null });
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
