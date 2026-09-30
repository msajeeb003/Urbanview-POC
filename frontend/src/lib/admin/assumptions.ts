/**
 * Financial assumptions (the admin console's "Financial assumptions" tab): the rules the screen
 * follows, kept out of the components so they are unit-tested.
 *
 * - Every zone has a history of versions (`GET /v1/admin/assumptions?include_history=true`). The
 *   API says which one applies today (`status: live`: the latest `applies_from` on or before the
 *   municipality's today, the newest on a tie), which are `scheduled` (dated later), `superseded`
 *   or `retired`. Nothing here decides that; the screen shows it.
 * - The table edits a draft per zone, started from the live version: the four rates (€/m²; land
 *   per m² of parcel, construction and design per m² GFA, sale per m² saleable), optional
 *   absolute low / high bounds per rate, the range ± (the factors every rate without bounds is
 *   widened with), the saleable share (blank = the product's 70 %), the source note. Saving sends
 *   every changed zone as a new version applying from one date (`POST /v1/admin/assumptions/batch`,
 *   all or nothing).
 * - The validation mirrors the API's: positive figures, both bounds or neither, low ≤ expected ≤
 *   high, a source, the date today or later.
 */
import type { ChipTone } from "@/components/admin/parts";
import type {
  AssumptionSet,
  AssumptionSetIn,
  AssumptionStatus,
  RateRange,
} from "@/lib/api/types";

export const RATES = ["land", "build", "design", "sale"] as const;
export type RateKey = (typeof RATES)[number];

export const RATE_LABELS: Record<RateKey, string> = {
  land: "Land €/m²",
  build: "Construction €/m²",
  design: "Design & documentation €/m²",
  sale: "Sale €/m²",
};
export const RATE_BASIS: Record<RateKey, string> = {
  land: "per m² of parcel area",
  build: "per m² of gross floor area",
  design: "per m² of gross floor area",
  sale: "per m² of saleable area",
};
export const DEFAULT_SALEABLE_PCT = 70;

export interface RateDraft {
  expected: string;
  /** Absolute bounds; both blank = the range ± applies. */
  low: string;
  high: string;
}

export interface SetDraft {
  rates: Record<RateKey, RateDraft>;
  /** Range ±: "14" = low factor 0.86; "15" = high factor 1.15. */
  lowPct: string;
  highPct: string;
  /** Blank = the product default (70 %). */
  saleablePct: string;
  source: string;
  notes: string;
}

export interface ZoneRow {
  zoneId: number;
  name: string;
  /** What the public panel uses today. */
  live: AssumptionSet | null;
  /** Dated after today, soonest first. */
  scheduled: AssumptionSet[];
  /** Every version, newest first. */
  history: AssumptionSet[];
}

export interface ZoneOption {
  id: number;
  name: string;
}

// --- numbers ---------------------------------------------------------------------------------

/** A typed number: spaces ignored, a decimal comma accepted ("1350,5"); blank / junk = NaN. */
export function parseNumber(text: string): number {
  const cleaned = text.replace(/[\s  ]/g, "").replace(",", ".");
  if (cleaned === "" || !/^-?\d*\.?\d+$/.test(cleaned)) return Number.NaN;
  return Number(cleaned);
}

const round = (value: number, digits = 2): number => {
  const f = 10 ** digits;
  return Math.round(value * f) / f;
};

const plain = (value: number): string => String(round(value, 4));

export function euros(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${value < 0 ? "−" : ""}€${Math.abs(value).toLocaleString("en-GB", { maximumFractionDigits: 2 })}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "26 Sep 2026" (fixed month names: the same text on the server and in every browser). */
export function dayLabel(iso: string | null | undefined): string {
  if (!iso) return "—";
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!match) return iso;
  const [, year, month, day] = match;
  return `${Number(day)} ${MONTHS[Number(month) - 1] ?? month} ${year}`;
}

export function daysBetween(fromIso: string, toIso: string): number {
  const a = Date.parse(`${fromIso.slice(0, 10)}T00:00:00Z`);
  const b = Date.parse(`${toIso.slice(0, 10)}T00:00:00Z`);
  return Math.round((b - a) / 86_400_000);
}

// --- rows ------------------------------------------------------------------------------------

export function zoneRows(zones: ZoneOption[], sets: AssumptionSet[]): ZoneRow[] {
  const byZone = new Map<number, AssumptionSet[]>();
  for (const set of sets) {
    if (set.zone_id == null) continue; // the municipality-wide row holds import factors only
    const list = byZone.get(set.zone_id) ?? [];
    list.push(set);
    byZone.set(set.zone_id, list);
  }
  const names = new Map(zones.map((z) => [z.id, z.name]));
  for (const [id, list] of byZone) if (!names.has(id)) names.set(id, list[0]?.zone_name ?? `Zone ${id}`);
  return [...names.entries()]
    .map(([zoneId, name]) => {
      const history = [...(byZone.get(zoneId) ?? [])].sort((a, b) => b.version - a.version || b.id - a.id);
      return {
        zoneId,
        name,
        live: history.find((s) => s.status === "live") ?? null,
        scheduled: history
          .filter((s) => s.status === "scheduled")
          .sort((a, b) => a.applies_from.localeCompare(b.applies_from) || a.version - b.version),
        history,
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name, "en"));
}

export function statusChip(status: AssumptionStatus): { tone: ChipTone; label: string } {
  switch (status) {
    case "live":
      return { tone: "ok", label: "Live" };
    case "scheduled":
      return { tone: "pend", label: "Scheduled" };
    case "retired":
      return { tone: "rev", label: "Retired" };
    default:
      return { tone: "rev", label: "Superseded" };
  }
}

/** "−14% / +15%", or "±14%" when both sides are equal. */
export function rangeText(lowFactor: number, highFactor: number): string {
  const low = round((1 - lowFactor) * 100, 2);
  const high = round((highFactor - 1) * 100, 2);
  return low === high ? `±${low}%` : `−${low}% / +${high}%`;
}

/** "€1,200 – €1,500" for absolute bounds, else null (the range ± applies). */
export function boundsText(range: RateRange): string | null {
  return range.kind === "absolute" ? `${euros(range.low)} – ${euros(range.high)}` : null;
}

// --- drafts ----------------------------------------------------------------------------------

export function emptyDraft(): SetDraft {
  const rate = (): RateDraft => ({ expected: "", low: "", high: "" });
  return {
    rates: { land: rate(), build: rate(), design: rate(), sale: rate() },
    lowPct: "14",
    highPct: "15",
    saleablePct: "",
    source: "",
    notes: "",
  };
}

export function draftFrom(set: AssumptionSet | null): SetDraft {
  if (set == null) return emptyDraft();
  const rate = (range: RateRange): RateDraft => ({
    expected: plain(range.expected),
    low: range.kind === "absolute" ? plain(range.low) : "",
    high: range.kind === "absolute" ? plain(range.high) : "",
  });
  return {
    rates: {
      land: rate(set.land_rate),
      build: rate(set.build_rate),
      design: rate(set.design_rate),
      sale: rate(set.sale_rate),
    },
    lowPct: plain(round((1 - set.range_low_factor) * 100, 4)),
    highPct: plain(round((set.range_high_factor - 1) * 100, 4)),
    saleablePct: set.saleable_share == null ? "" : plain(round(set.saleable_share * 100, 4)),
    source: set.source ?? "",
    notes: set.notes ?? "",
  };
}

export type DraftErrors = Partial<Record<string, string>>;

export type DraftResult =
  | { ok: true; value: Omit<AssumptionSetIn, "zone_id"> }
  | { ok: false; errors: DraftErrors };

/** The API's rules on a draft: field key -> message ("land.expected", "land.bounds", "lowPct" …). */
export function checkDraft(draft: SetDraft): DraftResult {
  const errors: DraftErrors = {};
  const rates = {} as Record<RateKey, { expected: number; low?: number | null; high?: number | null }>;
  for (const key of RATES) {
    const raw = draft.rates[key];
    const expected = parseNumber(raw.expected);
    if (!(expected > 0 && expected <= 1_000_000)) {
      errors[`${key}.expected`] = raw.expected.trim() ? "a positive figure up to 1,000,000" : "required";
      continue;
    }
    const lowBlank = raw.low.trim() === "";
    const highBlank = raw.high.trim() === "";
    if (lowBlank && highBlank) {
      rates[key] = { expected };
      continue;
    }
    const low = parseNumber(raw.low);
    const high = parseNumber(raw.high);
    if (lowBlank || highBlank) errors[`${key}.bounds`] = "give both bounds, or neither";
    else if (!(low > 0) || !(high > 0)) errors[`${key}.bounds`] = "bounds are positive figures";
    else if (!(low <= expected && expected <= high)) errors[`${key}.bounds`] = "low ≤ expected ≤ high";
    else rates[key] = { expected, low, high };
  }
  const lowPct = parseNumber(draft.lowPct);
  if (!(lowPct >= 0 && lowPct < 100)) errors.lowPct = "0 to 99 %";
  const highPct = parseNumber(draft.highPct);
  if (!(highPct >= 0 && highPct <= 400)) errors.highPct = "0 to 400 %";
  let saleable: number | null = null;
  if (draft.saleablePct.trim() !== "") {
    const pct = parseNumber(draft.saleablePct);
    if (!(pct > 0 && pct <= 100)) errors.saleablePct = "above 0, at most 100 %";
    else saleable = round(pct / 100, 4);
  }
  const source = draft.source.trim();
  if (!source) errors.source = "say where the figures come from";
  else if (source.length > 200) errors.source = "at most 200 characters";
  if (draft.notes.length > 2000) errors.notes = "at most 2,000 characters";
  if (Object.keys(errors).length) return { ok: false, errors };
  return {
    ok: true,
    value: {
      land_rate: rates.land,
      build_rate: rates.build,
      design_rate: rates.design,
      sale_rate: rates.sale,
      range_low_factor: round(1 - lowPct / 100, 4),
      range_high_factor: round(1 + highPct / 100, 4),
      saleable_share: saleable,
      source,
      notes: draft.notes.trim() || null,
    },
  };
}

/** True when the draft says something other than the version it started from. */
export function isChanged(draft: SetDraft, base: AssumptionSet | null): boolean {
  const start = draftFrom(base);
  const norm = (d: SetDraft) => {
    const checked = checkDraft(d);
    return checked.ok ? JSON.stringify(checked.value) : JSON.stringify(d);
  };
  if (base == null) {
    const empty = emptyDraft();
    return JSON.stringify(draft) !== JSON.stringify(empty);
  }
  return norm(draft) !== norm(start);
}

/** "applies today" / "scheduled: from 15 Oct 2026 (in 19 days)" for the chosen date. */
export function applyNote(date: string, today: string): string {
  const days = daysBetween(today, date);
  if (days <= 0) return "Applies today: the public panel shows the new figures on its next load.";
  return `Scheduled: the panel keeps today's figures until ${dayLabel(date)} (in ${days} day${days === 1 ? "" : "s"}).`;
}

export function dateProblem(date: string, today: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return "Choose the date the figures apply from.";
  if (daysBetween(today, date) < 0) return `A set applies from today (${dayLabel(today)}) or a later date.`;
  if (daysBetween(today, date) > 5 * 366) return "The date is more than five years ahead.";
  return null;
}

// --- history ---------------------------------------------------------------------------------

export interface DiffLine {
  label: string;
  before: string;
  after: string;
}

function rateSummary(range: RateRange): string {
  const bounds = boundsText(range);
  return bounds ? `${euros(range.expected)} (${bounds})` : euros(range.expected);
}

/** What changed from one version to the next (the history's "view diff"). */
export function diffVersions(before: AssumptionSet | null, after: AssumptionSet): DiffLine[] {
  const lines: DiffLine[] = [];
  const push = (label: string, a: string, b: string) => {
    if (a !== b) lines.push({ label, before: a, after: b });
  };
  for (const key of RATES) {
    const field = `${key}_rate` as const;
    push(RATE_LABELS[key], before ? rateSummary(before[field]) : "—", rateSummary(after[field]));
  }
  push(
    "Range ±",
    before ? rangeText(before.range_low_factor, before.range_high_factor) : "—",
    rangeText(after.range_low_factor, after.range_high_factor),
  );
  const share = (s: AssumptionSet | null) =>
    s == null ? "—" : s.saleable_share == null ? `${DEFAULT_SALEABLE_PCT}% (default)` : `${round(s.saleable_share * 100, 2)}%`;
  push("Saleable share", share(before), share(after));
  push("Source", before?.source ?? "—", after.source ?? "—");
  push("Applies from", before ? dayLabel(before.applies_from) : "—", dayLabel(after.applies_from));
  push("Notes", before?.notes ?? "—", after.notes ?? "—");
  return lines;
}

/** The version before `set` in its zone's history (by version number), or null. */
export function previousVersion(history: AssumptionSet[], set: AssumptionSet): AssumptionSet | null {
  return (
    history
      .filter((s) => s.version < set.version)
      .sort((a, b) => b.version - a.version)[0] ?? null
  );
}
