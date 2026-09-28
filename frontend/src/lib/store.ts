"use client";

/**
 * Shell state: what the wireframe's global `STATE` held (rail, layers, selection pin, AI panel,
 * admin view, modal, toast) plus the phone bottom-sheet position. Components read slices with
 * selectors; the map registers a small controller so chrome buttons can drive the camera.
 */
import type { ReactNode } from "react";
import { create } from "zustand";

import type { AssumptionEdits, EditKey } from "./assumptions";
import { DEFAULT_CHOROPLETH, DEFAULT_LAYER_STATE, type ChoroplethState, type LayerId } from "./layers";
import { EMPTY_DRAFT, type OrderDraft, type OrderTarget } from "./order-form";


export interface LngLat {
  lng: number;
  lat: number;
}

export type FeatureType = "cadastral" | "urban" | "document";

/**
 * What the visitor last looked up; panels read its resolution from the query cache.
 * - `point`: a map click on no feature, or an address suggestion (resolved through `/v1/locate`)
 * - `parcel`: a KO + parcel-number search (resolved through `/v1/locate/parcel`)
 * - `feature`: a selected map feature: cadastral parcel (`id` = Parcel ID), planned (urban)
 *   parcel, or planning-document coverage area; `linkedUrbanId` is the cadastral parcel's primary
 *   planned parcel, highlighted with it.
 * - `zone`: a zone picked in the search (framed) or clicked on the map; its panel is
 *   `/v1/zones/{id}/panel` and the map outlines it.
 */
export type Selection =
  | { kind: "point"; point: LngLat; via: "click" | "address" }
  | { kind: "parcel"; ref: { ko: string; number: string; sub: string | null } }
  | { kind: "zone"; id: number; name: string; via: "search" | "click" }
  | {
      kind: "feature";
      type: FeatureType;
      id: number;
      zoneId: number | null;
      linkedUrbanId: number | null;
      via: "click" | "search" | "link";
    };

export type SheetState = "peek" | "half" | "full";

/** The next bottom-sheet position when its handle is tapped: peek → half → full → peek. */
export function nextSheet(sheet: SheetState): SheetState {
  return sheet === "peek" ? "half" : sheet === "half" ? "full" : "peek";
}

/** Why the "Outside current coverage" pill is showing. */
export type UncoveredReason = "no_adopted_plan" | "outside_municipality";

/** A camera target set before the map exists (a `?parcel=` link), applied when it is ready. */
export interface CameraFocus {
  bbox?: [number, number, number, number] | null;
  point?: LngLat | null;
}

/** What the map highlights for a selection (ids per clickable source-layer). */
export function highlightOf(selection: Selection | null): {
  cadastral: number | null;
  urban: number | null;
  document: number | null;
  zone: number | null;
} {
  const none = { cadastral: null, urban: null, document: null, zone: null };
  if (selection?.kind === "zone") return { ...none, zone: selection.id };
  if (!selection || selection.kind !== "feature") return none;
  return {
    ...none,
    cadastral: selection.type === "cadastral" ? selection.id : null,
    urban: selection.type === "urban" ? selection.id : selection.type === "cadastral" ? selection.linkedUrbanId : null,
    document: selection.type === "document" ? selection.id : null,
  };
}

/** How long the outside-coverage pill stays up (the wireframe's 2.6 s). */
export const UNCOVERED_FLASH_MS = 2600;

export interface ModalSpec {
  content: ReactNode;
  wide?: boolean;
  /** Extra class on `.modal`. */
  className?: string;
  /** Accessible name for the dialog (the modal's heading text). */
  label: string;
}

export interface MapController {
  zoomIn(): void;
  zoomOut(): void;
  /** Back to the municipality framing. */
  reset(): void;
  flyTo(point: LngLat, zoom?: number): void;
  fitBounds(bbox: [number, number, number, number]): void;
}

interface ShellState {
  railOpen: boolean;
  legendMin: boolean;
  layers: Record<LayerId, boolean>;
  /** Field shown by each choropleth card (block-cell parameter, zone-cell sale-rate level). */
  choropleth: ChoroplethState;
  /**
   * The visitor's edited assumptions (only the edited keys), in memory only: moving to another
   * parcel keeps them and an order can carry them; a reload returns to the defaults.
   */
  assumptionEdits: AssumptionEdits;
  /**
   * The parcel an order would be for: the parcel panel on screen registers it (`ParcelCtas`), so
   * every order button (panel, "Choose your access", the methodology's last step) orders what
   * the visitor is looking at. Null without a parcel panel.
   */
  orderTarget: OrderTarget | null;
  /**
   * What the visitor typed in the order form, kept in memory only (personal data: never in
   * storage) while the form is closed for the methodology or after a failed request; cleared
   * once an order is placed.
   */
  orderDraft: OrderDraft;

  view: "map" | "admin";
  aiOpen: boolean;
  /** A question a panel CTA put into the assistant's input (`id` changes per request). */
  aiDraft: { id: number; text: string } | null;

  /** Panel removed from the layout (outside-coverage state). */
  panelHidden: boolean;
  /** Phone / tablet bottom sheet: its header only, half the screen (where a selection opens), or expanded. */
  sheet: SheetState;

  selection: Selection | null;
  pin: LngLat | null;
  coords: LngLat | null;
  coverWarn: boolean;
  coverReason: UncoveredReason;
  focus: CameraFocus | null;
  /** Data version of the tiles on the map (`/v1/tiles/current`), `"unpublished"` before a publish. */
  dataVersion: string | null;

  zoomLabel: string;
  scale: { label: string; widthPx: number };
  /**
   * The map's zoom level, null without a map (no token, or before it exists). The rail and the
   * legend compare it with each layer's published zoom range (`layerState`).
   */
  zoom: number | null;

  /** Last toast; `visible` flips off after 2.6 s but the text stays for the fade-out. */
  toast: { id: number; message: string; visible: boolean } | null;
  modal: ModalSpec | null;
  map: MapController | null;

  setRailOpen(open: boolean): void;
  toggleLegend(): void;
  /** Replace the whole layer state (a `?layers=` link, or a toggle that switched two cards). */
  setLayers(layers: Record<LayerId, boolean>): void;
  setChoropleth(kind: keyof ChoroplethState, value: string): void;
  setView(view: "map" | "admin"): void;
  setAiOpen(open: boolean): void;
  /** Set one edited assumption (`undefined` = back to the zone's default). */
  setAssumptionEdit(key: EditKey, value: number | undefined): void;
  resetAssumptions(): void;
  setOrderTarget(target: OrderTarget | null): void;
  setOrderDraft(patch: Partial<OrderDraft>): void;
  clearOrderDraft(): void;
  /** Open the assistant with a question typed in for the visitor (not sent). */
  openAiWith(text: string): void;
  setPanelHidden(hidden: boolean): void;
  setSheet(sheet: SheetState): void;
  setSelection(selection: Selection | null): void;
  dropPin(point: LngLat | null): void;
  setCoords(point: LngLat | null): void;
  setCoverWarn(on: boolean): void;
  /** Uncovered location: pill for 2.6 s with the panel closed, then the panel comes back. */
  flashUncovered(reason: UncoveredReason): void;
  setFocus(focus: CameraFocus | null): void;
  setDataVersion(version: string | null): void;
  setCamera(zoomLabel: string, scale: { label: string; widthPx: number }, zoom: number | null): void;
  showToast(message: string): void;
  hideToast(id: number): void;
  openModal(spec: ModalSpec): void;
  closeModal(): void;
  registerMap(controller: MapController | null): void;
  clearSelection(): void;
}

let toastSeq = 0;

let uncoveredTimer: ReturnType<typeof setTimeout> | null = null;

export const useShell = create<ShellState>()((set) => ({
  railOpen: true,
  legendMin: false,
  layers: { ...DEFAULT_LAYER_STATE },
  choropleth: { ...DEFAULT_CHOROPLETH },
  assumptionEdits: {},
  orderTarget: null,
  orderDraft: EMPTY_DRAFT,

  view: "map",
  aiOpen: false,
  aiDraft: null,

  panelHidden: false,
  sheet: "peek",

  selection: null,
  pin: null,
  coords: null,
  coverWarn: false,
  coverReason: "no_adopted_plan",
  focus: null,
  dataVersion: null,

  zoomLabel: "1.0×",
  scale: { label: "250 m", widthPx: 80 },
  zoom: null,

  toast: null,
  modal: null,
  map: null,

  setRailOpen: (railOpen) => set({ railOpen }),
  toggleLegend: () => set((s) => ({ legendMin: !s.legendMin })),
  setLayers: (layers) => set({ layers }),
  setChoropleth: (kind, value) => set((s) => ({ choropleth: { ...s.choropleth, [kind]: value } })),
  setView: (view) => set({ view }),
  setAiOpen: (aiOpen) => set({ aiOpen }),
  setAssumptionEdit: (key, value) =>
    set((s) => {
      const next = { ...s.assumptionEdits };
      if (value === undefined) delete next[key];
      else next[key] = value;
      return { assumptionEdits: next };
    }),
  resetAssumptions: () => set({ assumptionEdits: {} }),
  setOrderTarget: (orderTarget) => set({ orderTarget }),
  setOrderDraft: (patch) => set((s) => ({ orderDraft: { ...s.orderDraft, ...patch } })),
  clearOrderDraft: () => set({ orderDraft: EMPTY_DRAFT }),
  openAiWith: (text) => set((s) => ({ aiOpen: true, aiDraft: { id: (s.aiDraft?.id ?? 0) + 1, text } })),
  setPanelHidden: (panelHidden) => set({ panelHidden }),
  setSheet: (sheet) => set({ sheet }),
  setSelection: (selection) => set({ selection }),
  dropPin: (pin) => set((s) => ({ pin, coords: pin ?? s.coords })),
  setCoords: (coords) => set({ coords }),
  setCoverWarn: (coverWarn) => {
    if (!coverWarn && uncoveredTimer) {
      clearTimeout(uncoveredTimer);
      uncoveredTimer = null;
    }
    set({ coverWarn });
  },
  flashUncovered: (reason) => {
    if (uncoveredTimer) clearTimeout(uncoveredTimer);
    set({ coverWarn: true, coverReason: reason, panelHidden: true });
    uncoveredTimer = setTimeout(() => {
      uncoveredTimer = null;
      set({ coverWarn: false, panelHidden: false });
    }, UNCOVERED_FLASH_MS);
  },
  setFocus: (focus) => set({ focus }),
  setDataVersion: (dataVersion) => set({ dataVersion }),
  setCamera: (zoomLabel, scale, zoom) => set({ zoomLabel, scale, zoom }),
  showToast: (message) => set({ toast: { id: ++toastSeq, message, visible: true } }),
  hideToast: (id) => set((s) => (s.toast?.id === id ? { toast: { ...s.toast, visible: false } } : {})),
  openModal: (modal) => set({ modal }),
  closeModal: () => set({ modal: null }),
  registerMap: (map) => set({ map }),
  clearSelection: () => {
    if (uncoveredTimer) clearTimeout(uncoveredTimer);
    uncoveredTimer = null;
    set({ selection: null, pin: null, coverWarn: false, panelHidden: false, sheet: "peek" });
  },
}));
