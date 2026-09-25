"use client";

/**
 * Every way of finding a location ends in one store selection (`useShell().selection`) that the
 * map highlights and the panel reads:
 *
 * - **map click on a feature** (`selectFeature`): top-priority feature under the pointer
 *   (cadastral parcel, planned parcel, planning-document coverage); the parcel is highlighted at
 *   once (with its primary planned parcel), pinned at its centre, then `/v1/locate` at the click
 *   confirms zone, planned-parcel link and coverage;
 * - **map click on nothing** or an **address suggestion** (`selectPoint`): pin, `/v1/locate`; a
 *   parcel found there becomes the selection;
 * - **KO + parcel number** (`selectParcel`): `/v1/locate/parcel`, fly to the parcel;
 * - **zone suggestion** (`selectZone`): frame the zone and select it (its panel; a zone without
 *   an adopted plan shows the S6 pill first);
 * - **`?parcel=` link** (`selectParcelById`): `/v1/parcels/{id}/panel`, land on the parcel.
 *
 * Uncovered locations are not errors: the "Outside current coverage" pill shows for 2.6 s and the
 * panel stays closed. The search calls answer an outcome so the search box can say inline that a
 * parcel reference matched nothing (the previous selection stays).
 * Analytics: `search_performed { search_kind, matched }` per search or map click (plus `result`
 * address | zone | parcel for typed searches and `recent: true` for a recent search re-run),
 * `parcel_selected` per selected parcel (`parcel_id` or `urban_parcel_id`, `parcel_type`,
 * `zone_id`, `via`).
 */
import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { getTracker, useTrack } from "@/lib/analytics/react";
import { ApiError } from "@/lib/api/client";
import { api } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/hooks";
import { tNow } from "@/lib/i18n";
import type { LocationResolution, ParcelPanel } from "@/lib/api/types";
import type { Pick } from "@/lib/map/pick";
import type { ParcelRef, ZoneRef } from "@/lib/search";
import { useShell, type LngLat, type Selection, type UncoveredReason } from "@/lib/store";

export type SearchKind = "click" | "address" | "parcel_number";

/** A search re-run from the recent list says so in its event. */
export interface SearchOptions {
  recent?: boolean;
}

export type PointOutcome = "parcel" | "no_parcel" | "uncovered" | "failed" | "superseded";
export type ParcelOutcome = "found" | "not_found" | "failed" | "superseded";

const FLY_ZOOM = 17;

type FeatureSelection = Extract<Selection, { kind: "feature" }>;

/** Cache key of a point / parcel-reference selection's resolution (shared with the hooks). */
export function selectionQueryKey(sel: Extract<Selection, { kind: "point" | "parcel" }>) {
  return sel.kind === "point"
    ? queryKeys.locate(sel.point.lat, sel.point.lng)
    : queryKeys.locateParcel(sel.ref.ko, sel.ref.number, sel.ref.sub);
}

function uncoveredReason(res: { coverage: { reason?: string | null } }): UncoveredReason {
  return res.coverage.reason === "outside_municipality" ? "outside_municipality" : "no_adopted_plan";
}

/** `search_performed` properties: never the query text, only what kind of search it was and whether it matched. */
function searchProps(
  kind: SearchKind,
  matched: boolean,
  result: "address" | "zone" | "parcel" | null,
  opts?: SearchOptions,
) {
  return {
    search_kind: kind,
    matched,
    ...(result ? { result } : {}),
    ...(opts?.recent ? { recent: true } : {}),
  };
}

/** `parcel_selected` for a parcel selection; documents are not parcels and emit nothing. */
function emitParcelSelected(sel: FeatureSelection): void {
  if (sel.type === "document") return;
  getTracker().track("parcel_selected", {
    ...(sel.type === "cadastral" ? { parcel_id: sel.id } : { urban_parcel_id: sel.id }),
    parcel_type: sel.type,
    zone_id: sel.zoneId ?? undefined,
    via: sel.via,
  });
}

/** A resolution that found a cadastral parcel becomes a cadastral feature selection. */
function cadastralSelection(res: LocationResolution, via: FeatureSelection["via"]): FeatureSelection | null {
  if (!res.cadastral_parcel) return null;
  return {
    kind: "feature",
    type: "cadastral",
    id: res.cadastral_parcel.parcel_id,
    zoneId: res.zone?.id ?? null,
    linkedUrbanId: res.urban_parcel?.id ?? null,
    via,
  };
}

/** Show a selection's panel, or the S6 state when the location is uncovered. */
function showCoverage(covered: boolean, res: { coverage: { reason?: string | null } }): void {
  const s = useShell.getState();
  if (!covered) {
    s.flashUncovered(uncoveredReason(res));
    return;
  }
  s.setCoverWarn(false);
  s.setPanelHidden(false);
  s.setSheet("half");
}

export function useSelection() {
  const qc = useQueryClient();
  const track = useTrack();

  const locate = useCallback(
    (point: LngLat) =>
      qc.fetchQuery({
        queryKey: queryKeys.locate(point.lat, point.lng),
        queryFn: ({ signal }) => api.locate(point, { signal }),
        staleTime: 60_000,
      }),
    [qc],
  );

  /** Map click on no feature, or an address suggestion. */
  const selectPoint = useCallback(
    async (point: LngLat, via: "click" | "address", opts?: SearchOptions): Promise<PointOutcome> => {
      // an address pick matched a location; a click matches when a parcel is under it
      if (via === "address") track("search_performed", searchProps("address", true, "address", opts));
      const s = useShell.getState();
      const sel: Selection = { kind: "point", point, via };
      s.setSelection(sel);
      s.dropPin(point);
      if (via === "address") s.map?.flyTo(point, FLY_ZOOM);
      let res: LocationResolution;
      try {
        res = await locate(point);
      } catch (error) {
        if (via === "click") track("search_performed", searchProps("click", false, null));
        reportLookupFailure(error);
        return "failed";
      }
      if (via === "click") track("search_performed", searchProps("click", !!res.cadastral_parcel, null));
      if (useShell.getState().selection !== sel) return "superseded"; // a newer selection won
      const parcel = cadastralSelection(res, via === "click" ? "click" : "search");
      if (parcel && res.covered) {
        s.setSelection(parcel);
        s.dropPin(res.cadastral_parcel!.centroid);
        emitParcelSelected(parcel);
      }
      // covered but no parcel here: the selection stays a point and the panel says so
      showCoverage(res.covered, res);
      return !res.covered ? "uncovered" : parcel ? "parcel" : "no_parcel";
    },
    [locate, track],
  );

  /** Map click on a feature (see `pickFeature`). */
  const selectFeature = useCallback(
    async (pick: Pick, clickPoint: LngLat) => {
      track("search_performed", searchProps("click", true, null));
      const s = useShell.getState();
      const sel: FeatureSelection = {
        kind: "feature",
        type: pick.type,
        id: pick.id,
        zoneId: pick.zoneId,
        linkedUrbanId: pick.linkedUrbanId,
        via: "click",
      };
      s.setSelection(sel);
      s.setCoverWarn(false);
      if (pick.type === "document") {
        s.dropPin(null); // the wireframe clears the pin for a plan area
        s.setCoords(clickPoint);
        s.setPanelHidden(false);
        s.setSheet("half");
        return;
      }
      s.dropPin(pick.centre ?? clickPoint);
      if (pick.type === "urban") {
        // planned parcels exist in the tiles only for adopted, live plans: always covered
        emitParcelSelected(sel);
        s.setPanelHidden(false);
        s.setSheet("half");
        return;
      }
      s.setPanelHidden(false);
      s.setSheet("half");
      let res: LocationResolution | null = null;
      try {
        res = await locate(clickPoint);
      } catch (error) {
        reportLookupFailure(error);
      }
      if (useShell.getState().selection !== sel) return;
      let confirmed = sel;
      if (res?.cadastral_parcel?.parcel_id === sel.id) {
        confirmed = {
          ...sel,
          zoneId: res.zone?.id ?? sel.zoneId,
          linkedUrbanId: res.urban_parcel?.id ?? sel.linkedUrbanId,
        };
        s.setSelection(confirmed);
        s.dropPin(res.cadastral_parcel.centroid);
      }
      emitParcelSelected(confirmed);
      if (res) showCoverage(res.covered, res);
    },
    [locate, track],
  );

  /**
   * Cadastral municipality + parcel number from the search box. `not_found` leaves the previous
   * selection (and what the map shows for it) in place; the search box says so inline.
   */
  const selectParcel = useCallback(
    async (ref: ParcelRef, opts?: SearchOptions): Promise<ParcelOutcome> => {
      const s = useShell.getState();
      const previous = s.selection;
      const sel: Selection = { kind: "parcel", ref };
      s.setSelection(sel);
      let res: LocationResolution;
      try {
        res = await qc.fetchQuery({
          queryKey: selectionQueryKey(sel),
          queryFn: ({ signal }) => api.locateParcel(ref, { signal }),
          staleTime: 60_000,
        });
      } catch (error) {
        if (useShell.getState().selection === sel) s.setSelection(previous);
        reportLookupFailure(error);
        return "failed";
      }
      const parcel = cadastralSelection(res, "search");
      track("search_performed", searchProps("parcel_number", !!parcel, "parcel", opts));
      if (useShell.getState().selection !== sel) return "superseded";
      if (!parcel) {
        s.setSelection(previous);
        return "not_found";
      }
      s.setSelection(parcel);
      s.dropPin(res.cadastral_parcel!.centroid);
      s.map?.flyTo(res.cadastral_parcel!.centroid, FLY_ZOOM);
      emitParcelSelected(parcel);
      showCoverage(res.covered, res);
      return "found";
    },
    [qc, track],
  );

  /**
   * A zone from the search: the map frames it and the zone becomes the selection (its panel). A
   * zone without an adopted plan first shows the S6 pill with the panel closed; when the panel
   * comes back it says "no adopted plan" rather than listing nothing.
   */
  const selectZone = useCallback(
    (zone: ZoneRef, opts?: SearchOptions): "covered" | "uncovered" => {
      track("search_performed", searchProps("address", true, "zone", opts));
      const s = useShell.getState();
      if (s.map) s.map.fitBounds(zone.bbox);
      else s.setFocus({ bbox: zone.bbox });
      s.setSelection({ kind: "zone", id: zone.id, name: zone.name, via: "search" });
      s.dropPin(null);
      showCoverage(zone.covered, { coverage: { reason: zone.covered ? null : "no_adopted_plan" } });
      return zone.covered ? "covered" : "uncovered";
    },
    [track],
  );

  /**
   * Moving between the two separate objects from a panel: "Open urban parcel →" on a cadastral
   * panel, "← cadastral parcel #…" on an urban one. The other object becomes the selection (and
   * the map highlight); `parcel_selected` says `via: "panel"`. The camera stays: both lie on the
   * same ground.
   */
  const selectLinkedParcel = useCallback(
    (target: { type: "cadastral" | "urban"; id: number; zoneId: number | null; linkedUrbanId?: number | null }) => {
      const s = useShell.getState();
      const sel: FeatureSelection = {
        kind: "feature",
        type: target.type,
        id: target.id,
        zoneId: target.zoneId,
        linkedUrbanId: target.linkedUrbanId ?? null,
        via: "click",
      };
      s.setSelection(sel);
      s.setPanelHidden(false);
      getTracker().track("parcel_selected", {
        ...(target.type === "cadastral" ? { parcel_id: target.id } : { urban_parcel_id: target.id }),
        parcel_type: target.type,
        zone_id: target.zoneId ?? undefined,
        via: "panel",
      });
    },
    [],
  );

  /**
   * `?parcel=<Parcel ID>` link: land on that parcel, selected and centred. Answers `not_found` for
   * an id that does not exist (the caller drops the parameter) and `failed` for trouble reaching
   * the API (the link stays so a reload can succeed).
   */
  const selectParcelById = useCallback(
    async (id: number): Promise<"ok" | "not_found" | "failed"> => {
      let panel: ParcelPanel;
      try {
        panel = await qc.fetchQuery({
          queryKey: queryKeys.parcelPanel(id),
          queryFn: ({ signal }) => api.parcelPanel(id, { signal }),
          staleTime: 60_000,
        });
      } catch (error) {
        // a stale link returns quietly to the map
        if (error instanceof ApiError && error.isNotFound) return "not_found";
        reportLookupFailure(error);
        return "failed";
      }
      const s = useShell.getState();
      const links = panel.header.calculation_basis.links ?? [];
      const primary = links.find((l) => l.primary) ?? links[0];
      const sel: FeatureSelection = {
        kind: "feature",
        type: "cadastral",
        id,
        zoneId: panel.header.zone?.id ?? null,
        linkedUrbanId: primary?.urban_parcel_id ?? null,
        via: "link",
      };
      s.setSelection(sel);
      s.dropPin(panel.centroid);
      const bbox = (panel.bbox?.length === 4 ? panel.bbox : null) as [number, number, number, number] | null;
      if (s.map) {
        if (bbox) s.map.fitBounds(bbox);
        else s.map.flyTo(panel.centroid, FLY_ZOOM);
      } else {
        s.setFocus({ bbox, point: panel.centroid });
      }
      emitParcelSelected(sel);
      showCoverage(panel.covered, { coverage: { reason: panel.covered ? null : "no_adopted_plan" } });
      return "ok";
    },
    [qc],
  );

  return { selectPoint, selectFeature, selectParcel, selectZone, selectLinkedParcel, selectParcelById };
}

function reportLookupFailure(error: unknown) {
  // Location lookups never produce an error screen; a transient failure is a quiet toast.
  if (error instanceof ApiError && error.isTransient) {
    useShell.getState().showToast(tNow("toast.serviceDown"));
  } else if (process.env.NODE_ENV !== "production") {
    console.warn("[selection] lookup failed", error);
  }
}
