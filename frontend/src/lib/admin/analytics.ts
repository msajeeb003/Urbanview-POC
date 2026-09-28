/**
 * The rules of the Analytics page (`/admin/analytics`, the pilot scope's A7 "Analytics and audit":
 * funnel, orders, districts, repeat usage, intent counts), kept pure and unit-tested: the date
 * range of the GET form, the words of the funnel steps, the district rows (searches outside
 * coverage count for the district they were made in; the API places them by their point), and
 * the percentages as the page prints them. The figures are `GET /v1/admin/analytics`'s, never
 * recomputed here.
 */
import type { ChipTone } from "@/components/admin/parts";
import type { AnalyticsDashboard } from "@/lib/api/types";
import { formatDate } from "@/lib/format";

export type District = AnalyticsDashboard["districts"][number];
export type FunnelStep = AnalyticsDashboard["funnel"]["steps"][number];

/** The API's default window when the form sends no dates. */
export const DEFAULT_DAYS = 30;

const STEP_LABELS: Record<string, string> = {
  map_loaded: "Map loaded",
  searched_or_selected: "Searched or picked a parcel",
  panel_viewed: "Opened a panel",
  financials_viewed: "Saw the financials",
  order_started: "Started an order",
  checkout_completed: "Placed an order",
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

/** `2.3`, `—` for null. */
export function ratioText(value: number | null | undefined): string {
  return value == null || Number.isNaN(value) ? "—" : value.toFixed(1);
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

/** The district's name, or what a row without a zone stands for. */
export function districtName(d: Pick<District, "zone_id" | "zone_name">): string {
  if (d.zone_id == null) return "Outside every district";
  return d.zone_name ?? `Zone #${d.zone_id}`;
}

/** Coverage of a district: an adopted, live plan, or none yet (the S6 demand the pilot measures). */
export function districtChip(d: Pick<District, "zone_id" | "covered">): { tone: ChipTone; label: string } | null {
  if (d.zone_id == null || d.covered == null) return null;
  return d.covered ? { tone: "ok", label: "Covered" } : { tone: "rev", label: "No adopted plan" };
}

/** Searches made where no adopted plan covers the point, summed over the districts. */
export function uncoveredDemand(districts: readonly District[]): { searches: number; districts: number } {
  let searches = 0;
  let count = 0;
  for (const d of districts) {
    const n = d.uncovered_searches ?? 0;
    if (n > 0) {
      searches += n;
      if (d.zone_id != null) count += 1;
    }
  }
  return { searches, districts: count };
}
