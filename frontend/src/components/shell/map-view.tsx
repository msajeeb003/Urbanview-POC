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
 *   are measured again whenever the box changes (rail, panel, window, phone sheet).
 * - `/v1/tiles/current` names the published archive; the PMTiles provider module
 *   (`lib/map/pmtiles-provider.ts`, registered with `mapboxgl.addTileProvider`) reads it in the
 *   map workers. A new `version_id` recreates the source, so a publish swaps the tiles without a
 *   deploy. Without a published archive only the base map shows: no error, nothing drawn. Every
 *   layer starts at the zoom its data is built from (the pointer's per-layer range, `uvLayers`).
 * - The map follows the rail exactly. Once the style has loaded, every change of the layers, the
 *   choropleth fields, the market entitlement and the selection is applied the moment it happens,
 *   also while tiles are still loading (Mapbox takes layout, filter and paint changes then; the
 *   state at `load` is applied by the load handler). The zoom goes to the store, so a card and
 *   its legend group say "zoom in to see" for a layer that is on but not drawn at this zoom.
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
import { sourceLayerEmpty } from "@/lib/layers";
import { fitOptions, fitPadding } from "@/lib/map/camera";
import { pickFeature, type PickType } from "@/lib/map/pick";
import {
  HATCH_IMAGE,
  HIT_LAYERS,
  UV_LAYERS,
  choroplethStyle,
  hatchImage,
  highlightFilters,
  uvLayers,
  visibleLayerIds,
  type Highlight,
} from "@/lib/map/style";
import { SOURCE_ID, hasArchive, registerTileProvider, vectorSource } from "@/lib/map/tiles";
import { useSelection } from "@/lib/selection";
import { highlightOf, useShell } from "@/lib/store";

const TOKEN = process.env.NEXT_PUBLIC_MAPBOX_TOKEN ?? "";
const STYLE = process.env.NEXT_PUBLIC_MAPBOX_STYLE || "mapbox://styles/mapbox/light-v11";
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
const NO_HOVER: Highlight = { cadastral: null, urban: null, document: null };

// Start fetching Mapbox GL as soon as this module is evaluated (hydration), not at effect time.
const mapboxModule = TOKEN && typeof window !== "undefined" ? import("mapbox-gl") : null;

const toBounds = (b: Bbox): LngLatBounds => [
  [b[0], b[1]],
  [b[2], b[3]],
];

/** Add (or replace) the UrbanView source and layers for the current published archive. */
function installTiles(map: MapboxMap, tiles: TilesCurrent | undefined): number | null {
  for (const l of [...UV_LAYERS].reverse()) if (map.getLayer(l.id)) map.removeLayer(l.id);
  if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
  if (!hasArchive(tiles)) return null; // unpublished: base map only, nothing drawn
  // `provider` is a Mapbox GL 3.x source option its bundled style spec does not list yet
  map.addSource(SOURCE_ID, vectorSource(tiles) as unknown as Parameters<MapboxMap["addSource"]>[1]);
  const beforeId = map.getStyle()?.layers?.find((l) => l.type === "symbol")?.id;
  for (const l of uvLayers(tiles)) {
    if (sourceLayerEmpty(l["source-layer"], tiles)) continue; // not in the archive: nothing to draw
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
    const want = visible.has(l.id) ? "visible" : "none";
    if (map.getLayoutProperty(l.id, "visibility") !== want) map.setLayoutProperty(l.id, "visibility", want);
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
  /** The archive version on the map; `undefined` until the style has loaded (no layers yet). */
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
    // before the style's `load` there is nothing to swap: the load handler installs this pointer
    if (!map || installedVersion.current === undefined) return;
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
        attributionControl: false,
        // top-right is the only corner the wireframe chrome leaves free
        logoPosition: "top-right",
      });
      mapRef.current = map;
      if (focus) useShell.getState().setFocus(null);
      map.touchZoomRotate.disableRotation();
      map.keyboard.disableRotation();
      map.addControl(new mapboxgl.AttributionControl({ compact: true }), "top-right");

      /** The style has loaded: UrbanView's layers exist and take changes (set once, in `load`). */
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
      const updateCamera = () => {
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(() => {
          const zoom = map.getZoom();
          useShell.getState().setCamera(formatZoomFactor(zoom, baseZoom), scaleBar(map.getCenter().lat, zoom), zoom);
        });
      };
      measureFraming();
      map.on("move", updateCamera);
      updateCamera();

      // Mapbox only follows window resizes; the map's box also changes when the rail collapses or
      // opens, the panel hides (outside coverage) or the bottom sheet moves: redraw at the new size.
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
      /** Apply the rail, the entitlement, the choropleth fields and the selection as they are now. */
      const syncAll = () => {
        syncVisibility(map);
        syncChoropleth(map, tilesRef.current);
        syncHighlight(map, hover);
      };
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

      map.on("load", () => {
        const img = hatchImage();
        if (!map.hasImage(HATCH_IMAGE)) map.addImage(HATCH_IMAGE, img);
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
          const layers = queryableHitLayers(map);
          const picked = layers.length ? pickFeature(map.queryRenderedFeatures(ev.point, { layers }) as never) : null;
          map.getCanvas().style.cursor = picked ? "pointer" : "";
          const next: Highlight = { ...NO_HOVER };
          if (picked) next[picked.type as PickType] = picked.id;
          if (next.cadastral !== hover.cadastral || next.urban !== hover.urban || next.document !== hover.document) {
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

      useShell.getState().registerMap({
        zoomIn: () => map.zoomIn(),
        zoomOut: () => map.zoomOut(),
        reset: () => map.fitBounds(cityBounds, fitOptions(padFor(FIT_PADDING))),
        flyTo: (p, zoom = 17) => map.flyTo({ center: [p.lng, p.lat], zoom: Math.max(map.getZoom(), zoom), essential: true }),
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
          // Before `load` there are no layers: the load handler applies the state as it is then.
          // After it, every change is applied at once, whether or not tiles are still loading.
          if (ready) {
            if (st.layers !== prev.layers || st.marketUnlocked !== prev.marketUnlocked) apply(() => syncVisibility(map));
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
