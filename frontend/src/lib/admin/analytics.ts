/**
 * The rules of the Analytics page (`/admin/analytics`, the pilot scope's A7 "Analytics and audit":
 * funnel, orders, districts, repeat usage, intent counts), kept pure and unit-tested: the date
 * range of the GET form, the words of the funnel steps, the zone rows (searches outside coverage
 * count for the district they were made in; the API places them by their point), and the
 * percentages as the page prints them. The figures are `GET /v1/admin/analytics`'s, never
 * recomputed here.
 */
import type { ChipTone } from "@/components/admin/parts";
import type { AnalyticsDashboard } from "@/lib/api/types";
import { formatDate } from "@/lib/format";

export type ZoneHits = AnalyticsDashboard["top_zones"][number];
export type UncoveredHit = AnalyticsDashboard["uncovered_hits"][number];
export type FunnelStep = AnalyticsDashboard["funnel"]["steps"][number];
export type OrderStatusCount = AnalyticsDashboard["orders"]["by_status"][number];

/** The API's default window when the form sends no dates. */
export const DEFAULT_DAYS = 30;

const STEP_LABELS: Record<string, string> = {
  map_loaded: "Map loaded",
  parcel_resolved: "Picked a parcel",
  panel_opened: "Opened its panel",
  order_started: "Started an order",
  order_submitted: "Placed an order",
  paid: "Paid",
};

export function stepLabel(step: string): string {
  return STEP_LABELS[step] ?? step.replace(/_/g, " ");
}

/** `42.5%`, `—` for null (the API sends percentages with 1 decimal). */
export function pctText(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${Number.isInteger(value) ? value : value.toFixed(1)}%`;
}

/** `1 session`, `3 sessions` (thousands grouped). */
export function count(n: number, singular: string, plural = `${singular}s`): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? singular : plural}`;
}

/** `42.460° N · 19.281° E` for a position of the uncovered hits. */
export function positionText(hit: Pick<UncoveredHit, "lat" | "lng">): string {
  const lat = `${Math.abs(hit.lat).toFixed(3)}° ${hit.lat < 0 ? "S" : "N"}`;
  const lng = `${Math.abs(hit.lng).toFixed(3)}° ${hit.lng < 0 ? "W" : "E"}`;
  return `${lat} · ${lng}`;
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export interface AnalyticsRange {
  /** `YYYY-MM-DD`, inclusive; empty = the API's default window. */
  from: string;
  /** `YYYY-MM-DD`, inclusive (the API's `to` is exclusive: the day after is sent). */
  to: string;
}

type Params = Record<string, string | string[] | undefined>;

const first = (value: string | string[] | undefined) => ((Array.isArray(value) ? value[0] : value) ?? "").trim();

/** The form's dates, dropped when malformed or reversed. */
export function parseRange(params: Params): AnalyticsRange {
  const from = first(params.from);
  const to = first(params.to);
  const ok = (d: string) => DATE_RE.test(d) && !Number.isNaN(Date.parse(`${d}T00:00:00Z`));
  const range = { from: ok(from) ? from : "", to: ok(to) ? to : "" };
  if (range.from && range.to && range.from > range.to) return { from: "", to: "" };
  return range;
}

/** The query `GET /v1/admin/analytics` takes: `from` inclusive, `to` exclusive (the next day). */
export function rangeQuery(range: AnalyticsRange): { from?: string; to?: string } {
  const query: { from?: string; to?: string } = {};
  if (range.from) query.from = range.from;
  if (range.to) {
    const next = new Date(Date.parse(`${range.to}T00:00:00Z`) + 86_400_000);
    query.to = next.toISOString().slice(0, 10);
  }
  return query;
}

/** "1 Sep 2026 – 30 Sep 2026" from the API's range (`to` exclusive). */
export function rangeLabel(range: AnalyticsDashboard["range"]): string {
  const lastDay = new Date(Date.parse(range.to) - 1).toISOString();
  return `${formatDate(range.from) ?? "—"} – ${formatDate(lastDay) ?? "—"}`;
}

/**
 * The zone's name, or what a row without one stands for. The row without a zone holds the
 * searches and picks no district could be recorded for: the event carried no position (a search
 * that found nothing, events from before positions were sent) or its point lies in no district.
 * It is not "outside coverage" (those searches are counted in the district of their point). A
 * zone id that names no zone any more is a district removed since (the first sample's two).
 */
export function districtName(d: Pick<ZoneHits, "zone_id" | "zone_name">): string {
  if (d.zone_id == null) return "No district recorded";
  return d.zone_name ?? `Removed district #${d.zone_id}`;
}

/** What such a row means, in a sentence (its tooltip); null for an ordinary district. */
export function districtNote(d: Pick<ZoneHits, "zone_id" | "zone_name">): string | null {
  if (d.zone_id == null)
    return "Searches and picks without a recorded position (for example a search that found nothing), or outside every district.";
  return d.zone_name == null ? "This district no longer exists; the events were recorded while it did." : null;
}

/** Coverage of a zone: an adopted, live plan, or none yet (the S6 demand the pilot measures). */
export function districtChip(d: Pick<ZoneHits, "zone_id" | "covered">): { tone: ChipTone; label: string } | null {
  if (d.zone_id == null || d.covered == null) return null;
  return d.covered ? { tone: "ok", label: "Covered" } : { tone: "rev", label: "No adopted plan" };
}
