"use client";

/**
 * The application frame, element for element the wireframe's `renderApp`:
 *
 *   .app
 *     .topbar                      (62 px)
 *     .main
 *       .rail                      (206 px, collapsible)
 *       .mapwrap > #map + .mapchrome
 *       .panel                     (392 px; bottom sheet at ≤ 860 px)
 *       .aifab / .aipanel          (anchored right 412 / bottom 20)
 *       .admin                     (overlay over the whole row)
 *   #overlay (modal) · #toast
 *
 * No login wall, splash or tour: the map is interactive on first paint. `map_loaded` is emitted by
 * the map once its style and tiles have loaded.
 */
import { useEffect, useRef } from "react";

import { useMunicipality } from "@/lib/api/hooks";
import type { MunicipalityProfile, TilesCurrent } from "@/lib/api/types";
import { tNow } from "@/lib/i18n";
import { useSelection } from "@/lib/selection";
import { useShell } from "@/lib/store";
import { parseLayersParam, readParcelParam, syncLayersParam, syncParcelParam } from "@/lib/url-state";

import { AdminOverlay } from "./admin-overlay";
import { AiFab, AiPanel } from "./ai-assistant";
import { ModalHost, ToastHost } from "./hosts";
import { InfoPanel } from "./info-panel";
import { LayerRail } from "./layer-rail";
import { MapChrome } from "./map-chrome";
import { MapView } from "./map-view";
import { Topbar } from "./topbar";

const INTRO_TOAST_MS = 900;
let linkOpened = false;

/**
 * The view in the address bar. `?layers=`: opening a link restores the layers (and choropleth
 * fields) it lists, then the parameter follows the rail. `?parcel=<Parcel ID>`: opening the page
 * with it lands on that parcel, selected and centred; the parameter then follows the selected
 * cadastral parcel (cleared for anything else).
 */
function useUrlSync() {
  const { selectParcelById } = useSelection();
  useEffect(() => {
    const view = parseLayersParam(window.location.search);
    if (view) {
      const s = useShell.getState();
      s.setLayers(view.layers);
      s.setChoropleth("param", view.choropleth.param);
      s.setChoropleth("price", view.choropleth.price);
    }
    const unsubscribeLayers = useShell.subscribe((st, prev) => {
      if (st.layers !== prev.layers || st.choropleth !== prev.choropleth) syncLayersParam(st.layers, st.choropleth);
    });
    const id = readParcelParam(window.location.search);
    if (id != null && !linkOpened) {
      linkOpened = true; // once per page load, also under strict-mode double effects
      void selectParcelById(id).then((outcome) => {
        if (outcome === "not_found") syncParcelParam(null);
      });
    }
    const unsubscribeParcel = useShell.subscribe((st, prev) => {
      if (st.selection === prev.selection) return;
      const sel = st.selection;
      if (sel?.kind === "feature" && sel.type === "cadastral") syncParcelParam(sel.id);
      else if (sel?.kind !== "parcel") syncParcelParam(null); // a pending search keeps the link
    });
    return () => {
      unsubscribeLayers();
      unsubscribeParcel();
    };
  }, [selectParcelById]);
}

export function AppShell({
  initialProfile,
  initialTiles,
}: {
  initialProfile: MunicipalityProfile | null;
  initialTiles?: TilesCurrent | null;
}) {
  const { data: profile } = useMunicipality(initialProfile);
  const searchRef = useRef<HTMLInputElement>(null);

  useUrlSync();

  useEffect(() => {
    const t = setTimeout(() => useShell.getState().showToast(tNow("toast.intro")), INTRO_TOAST_MS);
    return () => clearTimeout(t);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        useShell.getState().setView("map");
        searchRef.current?.focus();
        searchRef.current?.select();
      }
      // Esc: the search box closes its list itself; Radix closes an open modal.
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Phones and tablets start with the rail collapsed (it opens as a drawer over the map).
  useEffect(() => {
    if (window.matchMedia("(max-width: 860px)").matches) useShell.getState().setRailOpen(false);
  }, []);

  return (
    <>
      <div className="app" id="app">
        <Topbar ref={searchRef} />
        <div className="main">
          <LayerRail />
          <div className="mapwrap">
            <MapView profile={profile} initialTiles={initialTiles} />
            <MapChrome />
          </div>
          <InfoPanel />
          <AiFab />
          <AiPanel />
          <AdminOverlay />
        </div>
      </div>
      <ModalHost />
      <ToastHost />
    </>
  );
}
