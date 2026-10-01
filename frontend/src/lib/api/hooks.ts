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
  MunicipalityProfile,
  OrderIn,
  PanelQuery,
  TilesCurrent,
} from "./types";

export const queryKeys = {
  municipality: ["municipality"] as const,
  locate: (lat: number, lng: number) => ["locate", round6(lat), round6(lng)] as const,
  locateParcel: (ko: string, number: string, sub?: string | null) =>
    ["locate-parcel", ko.toLowerCase(), number, sub ?? null] as const,
  geocode: (q: string) => ["geocode", q.trim().toLowerCase()] as const,
  urbanParcels: (number: string) => ["urban-parcels", number.trim().toLowerCase().replace(/\s+/g, "")] as const,
  panel: (query: PanelQuery) => ["panel", query] as const,
  parcelPanel: (parcelId: number) => ["parcel-panel", parcelId] as const,
  sourceValue: (valueId: number) => ["source-value", valueId] as const,
  sourcePage: (documentId: number, page: number) => ["source-page", documentId, page] as const,
  tilesCurrent: ["tiles-current"] as const,
  zones: ["zones"] as const,
  orderPricing: ["order-pricing"] as const,
  order: (reference: string) => ["order", reference] as const,
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

/** Planned parcels with a typed urban parcel number (`UP 40`); idle for an empty number. */
export function useUrbanParcels(number: string) {
  const trimmed = number.trim();
  return useQuery({
    queryKey: queryKeys.urbanParcels(trimmed),
    queryFn: ({ signal }) => api.locateUrbanParcel(trimmed, { signal }),
    enabled: trimmed.length > 0,
    staleTime: 5 * 60_000,
    placeholderData: (previous) => previous,
    retry: retryTransient,
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

/** A publish reaches maps already open within this time (a reload at once). */
const POINTER_REFRESH_MS = 5 * 60_000;

/** When to read the tile pointer again: before the signed archive URL expires, at most 5 min. */
export function pointerRefreshMs(expiresAt: string | null | undefined): number {
  return expiresAt ? Math.min(freshUntil(expiresAt, 2 * 60_000), POINTER_REFRESH_MS) : POINTER_REFRESH_MS;
}

export function useTilesCurrent(initialData?: TilesCurrent | null) {
  return useQuery<TilesCurrent>({
    queryKey: queryKeys.tilesCurrent,
    initialData: initialData ?? undefined,
    queryFn: ({ signal }) => api.tilesCurrent({ signal }),
    staleTime: (q) => pointerRefreshMs(q.state.data?.expires_at),
    refetchInterval: (q) => {
      const ms = pointerRefreshMs(q.state.data?.expires_at);
      return ms > 0 ? ms : false;
    },
    refetchOnWindowFocus: true,
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

export function useOrder(reference: string | null) {
  return useQuery({
    queryKey: reference ? queryKeys.order(reference) : ["order", "idle"],
    queryFn: ({ signal }) => api.order(reference!, { signal }),
    enabled: !!reference,
    staleTime: 30_000,
    // the public order page: a payment staff recorded shows when the visitor comes back to the tab
    refetchOnWindowFocus: true,
    retry: retryTransient,
  });
}
