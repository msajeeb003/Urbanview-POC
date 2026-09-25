"use client";

/**
 * React Query hooks over `endpoints.ts`. Keys are stable arrays (`queryKeys`) so other code can
 * prefetch or invalidate. Lookups that should not run yet take `null` and stay idle.
 *
 * Caching rules follow the API: panel and locate answers are cheap and deterministic per data
 * version (1 min stale), geocode suggestions are cached server-side too, signed links (source,
 * tiles) are only reused while they are comfortably inside their expiry.
 */
import { useMutation, useQuery } from "@tanstack/react-query";

import { ApiError } from "./client";
import { api } from "./endpoints";
import type {
  FeasibilityRequest,
  MunicipalityProfile,
  OrderIn,
  PanelQuery,
  SourcePage,
  TilesCurrent,
} from "./types";

export const queryKeys = {
  municipality: ["municipality"] as const,
  locate: (lat: number, lng: number) => ["locate", round6(lat), round6(lng)] as const,
  locateParcel: (ko: string, number: string, sub?: string | null) =>
    ["locate-parcel", ko.toLowerCase(), number, sub ?? null] as const,
  geocode: (q: string) => ["geocode", q.trim().toLowerCase()] as const,
  panel: (query: PanelQuery) => ["panel", query] as const,
  parcelPanel: (parcelId: number) => ["parcel-panel", parcelId] as const,
  sourceValue: (valueId: number) => ["source-value", valueId] as const,
  sourcePage: (documentId: number, page: number) => ["source-page", documentId, page] as const,
  tilesCurrent: ["tiles-current"] as const,
  zones: ["zones"] as const,
  orderPricing: ["order-pricing"] as const,
  orderStatus: (reference: string) => ["order-status", reference] as const,
};

function round6(n: number): number {
  return Math.round(n * 1e6) / 1e6;
}

/** Retry transient failures only; a 4xx will not get better by asking again. */
/** Network trouble and outages are retried by React Query; a 429 already was, by the client. */
function retryTransient(failureCount: number, error: unknown): boolean {
  return error instanceof ApiError && error.isTransient && error.status !== 429 && failureCount < 2;
}

/** Milliseconds a signed link may still be reused (a minute of margin before it dies). */
function freshUntil(expiresAt: string | null | undefined, marginMs = 60_000): number {
  if (!expiresAt) return 0;
  return Math.max(0, new Date(expiresAt).getTime() - Date.now() - marginMs);
}

export function useMunicipality(initialData?: MunicipalityProfile | null) {
  return useQuery({
    queryKey: queryKeys.municipality,
    queryFn: ({ signal }) => api.municipality({ signal }),
    initialData: initialData ?? undefined,
    staleTime: 60 * 60_000,
    retry: retryTransient,
  });
}

export function useLocate(point: { lat: number; lng: number } | null) {
  return useQuery({
    queryKey: point ? queryKeys.locate(point.lat, point.lng) : ["locate", "idle"],
    queryFn: ({ signal }) => api.locate(point!, { signal }),
    enabled: !!point,
    staleTime: 60_000,
    retry: retryTransient,
  });
}

export function useLocateParcel(ref: { ko: string; number: string; sub?: string | null } | null) {
  return useQuery({
    queryKey: ref ? queryKeys.locateParcel(ref.ko, ref.number, ref.sub) : ["locate-parcel", "idle"],
    queryFn: ({ signal }) => api.locateParcel(ref!, { signal }),
    enabled: !!ref && !!ref.ko && !!ref.number,
    staleTime: 60_000,
    retry: retryTransient,
  });
}

/** Pass the debounced query; below `minLength` characters nothing is requested. */
/** The zone index; fetched when the search box first opens (`enabled`), then kept for the visit. */
export function useZones(enabled = true) {
  return useQuery({
    queryKey: queryKeys.zones,
    queryFn: ({ signal }) => api.zones({ signal }),
    enabled,
    staleTime: 10 * 60_000,
    retry: retryTransient,
  });
}

export function useGeocode(q: string, minLength = 2) {
  const trimmed = q.trim();
  return useQuery({
    queryKey: queryKeys.geocode(trimmed),
    queryFn: ({ signal }) => api.geocode(trimmed, { signal }),
    enabled: trimmed.length >= minLength,
    staleTime: 5 * 60_000,
    placeholderData: (previous) => previous,
    retry: false, // the endpoint never dead-ends: failures already answer 200 with results: []
  });
}

export function usePanel(query: PanelQuery | null) {
  return useQuery({
    queryKey: query ? queryKeys.panel(query) : ["panel", "idle"],
    queryFn: ({ signal }) => api.panel(query!, { signal }),
    enabled: !!query,
    staleTime: 60_000,
    placeholderData: (previous, previousQuery) =>
      // keep the previous figures on screen while edited assumptions recalculate
      previousQuery && query && previousQuery.queryKey[1] &&
      (previousQuery.queryKey[1] as PanelQuery).id === query.id &&
      (previousQuery.queryKey[1] as PanelQuery).type === query.type
        ? previous
        : undefined,
    retry: retryTransient,
  });
}

/** Display-shaped panel of a cadastral parcel (`/v1/parcels/{id}/panel`). */
export function useParcelPanel(parcelId: number | null) {
  return useQuery({
    queryKey: parcelId ? queryKeys.parcelPanel(parcelId) : ["parcel-panel", "idle"],
    queryFn: ({ signal }) => api.parcelPanel(parcelId!, { signal }),
    enabled: !!parcelId,
    staleTime: 60_000,
    retry: retryTransient,
  });
}

export function useFeasibility() {
  return useMutation({
    mutationFn: (body: FeasibilityRequest) => api.feasibility(body),
  });
}

export function useSourceValue(valueId: number | null) {
  return useQuery<SourcePage>({
    queryKey: valueId ? queryKeys.sourceValue(valueId) : ["source-value", "idle"],
    queryFn: ({ signal }) => api.sourceValue(valueId!, { signal }),
    enabled: !!valueId,
    staleTime: (q) => freshUntil(q.state.data?.expires_at),
    gcTime: 5 * 60_000,
    retry: retryTransient,
  });
}

export function useSourcePage(documentId: number | null, page: number | null) {
  return useQuery<SourcePage>({
    queryKey: documentId && page ? queryKeys.sourcePage(documentId, page) : ["source-page", "idle"],
    queryFn: ({ signal }) => api.sourcePage(documentId!, page!, { signal }),
    enabled: !!documentId && !!page,
    staleTime: (q) => freshUntil(q.state.data?.expires_at),
    gcTime: 5 * 60_000,
    retry: retryTransient,
  });
}

export function useTilesCurrent(initialData?: TilesCurrent | null) {
  return useQuery<TilesCurrent>({
    queryKey: queryKeys.tilesCurrent,
    initialData: initialData ?? undefined,
    queryFn: ({ signal }) => api.tilesCurrent({ signal }),
    staleTime: (q) => freshUntil(q.state.data?.expires_at, 2 * 60_000),
    refetchInterval: (q) => {
      const ms = freshUntil(q.state.data?.expires_at, 2 * 60_000);
      return ms > 0 ? ms : false; // renew the signed archive URL before it expires
    },
    retry: retryTransient,
  });
}

export function useCreateOrder() {
  return useMutation({
    // generous: the server prices, snapshots the panel and queues the e-mail in one request, and a
    // timeout that fires after the order was stored invites a second one
    mutationFn: (body: OrderIn) => api.createOrder(body, { timeoutMs: 30_000 }),
  });
}

/** Order price tiers (configuration, cached for the visit). */
export function useOrderPricing() {
  return useQuery({
    queryKey: queryKeys.orderPricing,
    queryFn: ({ signal }) => api.orderPricing({ signal }),
    staleTime: 30 * 60_000,
    retry: retryTransient,
  });
}

export function useOrderStatus(reference: string | null) {
  return useQuery({
    queryKey: reference ? queryKeys.orderStatus(reference) : ["order-status", "idle"],
    queryFn: ({ signal }) => api.orderStatus(reference!, { signal }),
    enabled: !!reference,
    staleTime: 30_000,
    // the public order page: a payment staff recorded shows when the visitor comes back to the tab
    refetchOnWindowFocus: true,
    retry: retryTransient,
  });
}

// `/v1/events` is not a hook of its own: analytics go through the batching tracker
// (`@/lib/analytics`), exposed to components as `useTrack()`.
export { useTrack } from "@/lib/analytics/react";
