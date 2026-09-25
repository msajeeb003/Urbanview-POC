/**
 * One typed function per public API route. Hooks (`hooks.ts`) and imperative callers (the
 * analytics tracker, click handlers) both go through these, so paths and shapes live in one place.
 */
import { apiGet, apiPost, apiRequest, type RequestOptions } from "./client";
import type {
  EventBatch,
  FeasibilityRequest,
  FeasibilityResponse,
  GeocodeResponse,
  GeocodeStatus,
  IngestResult,
  LocationResolution,
  MunicipalityProfile,
  OrderCreated,
  OrderIn,
  OrderPricing,
  OrderStatusPublic,
  Panel,
  PanelQuery,
  ParcelPanel,
  SourcePage,
  TilesCurrent,
  ZoneIndex,
} from "./types";

type Init = Pick<RequestOptions, "signal" | "timeoutMs" | "keepalive" | "fetchImpl" | "baseUrl">;

export const api = {
  municipality: (init?: Init) => apiGet<MunicipalityProfile>("/v1/municipality", undefined, init),

  /** Point lookup. Outside coverage is a 200 with `covered: false`, never an error. */
  locate: (point: { lat: number; lng: number }, init?: Init) =>
    apiGet<LocationResolution>("/v1/locate", { lat: point.lat, lng: point.lng }, init),

  /** Parcel reference lookup; `ko` (cadastral municipality) is mandatory. */
  locateParcel: (ref: { ko: string; number: string; sub?: string | null }, init?: Init) =>
    apiGet<LocationResolution>("/v1/locate/parcel", { ko: ref.ko, number: ref.number, sub: ref.sub }, init),

  /** Free-text search suggestions plus the `X-Geocode-Status` header (why a list is empty). */
  geocode: async (q: string, init?: Init) => {
    const res = await apiRequest<GeocodeResponse>("/v1/geocode", { ...init, query: { q } });
    return { ...res.data, status: (res.headers.get("X-Geocode-Status") as GeocodeStatus | null) ?? null };
  },

  panel: (query: PanelQuery, init?: Init) => apiGet<Panel>("/v1/panel", { ...query }, init),

  /** Display-shaped parcel panel by Parcel ID (cadastral parcel id); carries centroid and bbox. */
  parcelPanel: (parcelId: number, init?: Init) =>
    apiGet<ParcelPanel>(`/v1/parcels/${parcelId}/panel`, undefined, init),

  feasibility: (body: FeasibilityRequest, init?: Init) =>
    apiPost<FeasibilityResponse>("/v1/feasibility", body, init),

  /** One short-lived signed link to the cited page of a planning value. */
  sourceValue: (valueId: number, init?: Init) => apiGet<SourcePage>(`/v1/source/value/${valueId}`, undefined, init),

  sourcePage: (documentId: number, page: number, init?: Init) =>
    apiGet<SourcePage>(`/v1/source/${documentId}/page/${page}`, undefined, init),

  events: (batch: EventBatch, init?: Init) => apiPost<IngestResult>("/v1/events", batch, init),

  tilesCurrent: (init?: Init) => apiGet<TilesCurrent>("/v1/tiles/current", undefined, init),

  /** Every zone with its type, coverage, bbox and a simplified outline (the search box matches it). */
  zones: (init?: Init) => apiGet<ZoneIndex>("/v1/zones", undefined, init),

  createOrder: (body: OrderIn, init?: Init) => apiPost<OrderCreated>("/v1/orders", body, init),

  /** The configured price tiers (the panel shows a parcel's price from its `basis_area_m2`). */
  orderPricing: (init?: Init) => apiGet<OrderPricing>("/v1/orders/pricing", undefined, init),

  orderStatus: (reference: string, init?: Init) =>
    apiGet<OrderStatusPublic>(`/v1/orders/${encodeURIComponent(reference)}/status`, undefined, init),
};

export type Api = typeof api;
