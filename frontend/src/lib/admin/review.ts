/**
 * The rules of the AI review queue (`/admin/review`), kept out of the components so they can be
 * tested: how an item reads in the list (parameter — target, source line, value chip), the status
 * chips, low confidence, which editor a correction gets (a number with its unit, the floor
 * notation, a designation from the document's own wordings, free text), checking a correction,
 * the next pending item, the URL filters, the staged payload's fact lines and the refusals in
 * plain words (a correction the API refuses says which contract rule it broke).
 *
 * Rule behind the screen: 100% of AI-extracted planning values are reviewed before they publish,
 * and textual accuracy matters as much as numerical (a misread planning term changes what a
 * constraint means), so a correction is typed per parameter and always carries a note.
 */
import type { ChipTone } from "@/components/admin/parts";
import type {
  ReviewCounters,
  ReviewEntityType,
  ReviewItem,
  ReviewPayload,
  ReviewSort,
  ReviewStatus,
  ReviewTarget,
  ReviewValue,
} from "@/lib/api/types";

// --- how an item reads ---------------------------------------------------------------------------

function formatNumber(n: number, unit: string | null | undefined): string {
  if (Number.isInteger(n)) return unit ? String(n) : n.toFixed(1); // a ratio reads "2.0"
  return String(Math.round(n * 100) / 100);
}

/** The value chip: "55%", "2.0", "27.5 m", "P+5+Pk", "Stanovanje male gustine". */
export function formatValue(value: ReviewValue | null | undefined, valueType: "text" | "number"): string {
  if (!value) return "—";
  if (valueType === "number" && value.number != null) {
    const n = formatNumber(value.number, value.unit);
    if (!value.unit) return n;
    return value.unit === "%" ? `${n}%` : `${n} ${value.unit}`;
  }
  if (value.text != null && value.text !== "") return value.text;
  if (value.number != null) return formatNumber(value.number, value.unit) + (value.unit ? ` ${value.unit}` : "");
  return "—";
}

const ENTITY_NAMES: Record<ReviewEntityType, string> = {
  urban_parcel: "Urban parcel",
  block: "Block",
  zone: "Zone",
  document: "Whole plan",
};

export function entityName(type: ReviewEntityType): string {
  return ENTITY_NAMES[type] ?? type;
}

/** The target as the plan names it: "UP 12", "Blok VII", "Zona C2", a zone, "whole plan". */
export function targetLabel(target: ReviewTarget): string {
  switch (target.entity_type) {
    case "urban_parcel": {
      const n = target.urban_parcel_number ?? target.label;
      if (!n) return "urban parcel";
      return /^up\b/i.test(n) ? n : `UP ${n}`;
    }
    case "block":
      return target.block_ref ?? target.label ?? "block";
    case "zone":
      return target.zone_name ?? target.label ?? "zone";
    default:
      return "whole plan";
  }
}

/** "Max number of floors — UP 12" */
export function itemTitle(item: ReviewItem): string {
  return `${item.label_en} — ${targetLabel(item.target)}`;
}

/** "DUP Novi Grad · p.14" (the list's source line). */
export function sourceLine(item: ReviewItem): string {
  const page = item.source.page != null ? ` · p.${item.source.page}` : "";
  return `${item.source.document_name}${page}`;
}

export const LOW_CONFIDENCE = 0.7;

export function isLowConfidence(item: ReviewItem): boolean {
  const c = item.source.confidence;
  return (item.flags ?? []).includes("low_confidence") || (c != null && c < LOW_CONFIDENCE);
}

const FLAG_WORDS: Record<string, string> = {
  low_confidence: "low confidence",
  out_of_range: "outside the usual range",
  unit_assumed: "unit assumed",
  bbox_ambiguous: "position on the page uncertain",
  found_under_other_parcel: "found under another parcel",
  target_unmatched: "parcel not matched to a geometry",
  target_staged: "parcel geometry not published yet",
  page_corrected: "page corrected by the checker",
  repeated_in_run: "read twice in the run",
};

export function flagWords(flag: string): string {
  return FLAG_WORDS[flag] ?? flag.replace(/_/g, " ");
}

export function statusChip(item: Pick<ReviewItem, "status" | "published">): { tone: ChipTone; label: string } {
  if (item.published) return { tone: "ok", label: "Published" };
  switch (item.status) {
    case "approved":
      return { tone: "ok", label: "Approved" };
    case "amended":
      return { tone: "ok", label: "Amended" };
    case "rejected":
      return { tone: "rev", label: "Rejected" };
    default:
      return { tone: "pend", label: "Pending" };
  }
}

export function decided(item: ReviewItem): boolean {
  return item.status !== "pending";
}

/** Decisions are open until the item is published or superseded. */
export function canDecide(item: ReviewItem): boolean {
  return !item.published && !item.superseded;
}

// --- corrections ---------------------------------------------------------------------------------

export type Editor =
  | { kind: "number"; unit: string | null }
  | { kind: "floors" }
  | { kind: "choice"; field: string }
  | { kind: "text" };

/** The editor a correction gets: FAR / coverage % / heights / areas are numbers with their unit,
 * the floor count is the plan's notation ("P+5+Pk"), the land-use designation is picked from the
 * wordings the document uses (or typed when it is not among them), other texts are free text. */
export function editorFor(item: ReviewItem): Editor {
  if (item.parameter_key === "max_floors") return { kind: "floors" };
  if (item.parameter_key === "land_use") return { kind: "choice", field: "land_use" };
  if (item.value_type === "number") return { kind: "number", unit: item.extracted.unit ?? item.field_unit ?? null };
  return { kind: "text" };
}

/** Units a number may be corrected in (the canonical one first). */
export function unitChoices(unit: string | null): (string | null)[] {
  if (unit === "%") return ["%"];
  if (unit === "m²" || unit === "m2") return ["m²", "ha"];
  if (unit === "m") return ["m"];
  return [unit];
}

const FLOORS = /^(?:\d+|[A-Za-zČĆŽŠĐčćžšđ]{1,3})(?:\s*\+\s*(?:\d+|[A-Za-zČĆŽŠĐčćžšđ]{1,3}))*$/;

export type Correction = { ok: true; value: number | string; unit: string | null } | { ok: false; error: string };

/** A correction as the API takes it (numbers stay numbers, texts texts), or what is wrong with it. */
export function parseCorrection(editor: Editor, raw: string, unit: string | null): Correction {
  const text = raw.trim();
  if (!text) return { ok: false, error: "Enter the corrected value." };
  switch (editor.kind) {
    case "number": {
      const cleaned = text.replace(/\s/g, "").replace(/%$/, "").replace(",", ".");
      if (!/^\d+(\.\d+)?$/.test(cleaned)) return { ok: false, error: "Enter a number, e.g. 2.5 (a comma works too)." };
      const value = Number(cleaned);
      if (unit === "%" && value > 100) return { ok: false, error: "A percentage is at most 100." };
      return { ok: true, value, unit };
    }
    case "floors": {
      if (!FLOORS.test(text)) return { ok: false, error: 'Use the plan\'s notation, e.g. "P+5+Pk" or "S+P+4".' };
      return { ok: true, value: text.replace(/\s*\+\s*/g, "+"), unit: null };
    }
    default:
      if (text.length > 500) return { ok: false, error: "Keep the value under 500 characters." };
      return { ok: true, value: text, unit: null };
  }
}

/** The text the editor starts from: the value as it would publish now. */
export function editorStart(item: ReviewItem): string {
  const v = item.effective;
  if (item.value_type === "number" && v.number != null) return String(v.number);
  return v.text ?? (v.number != null ? String(v.number) : "");
}

// --- moving through the queue ------------------------------------------------------------------------

/** The next item still to decide after `from` (wrapping round), or null when none is left. */
export function nextPending(items: readonly ReviewItem[], from: number): number | null {
  const n = items.length;
  for (let step = 1; step <= n; step++) {
    const i = (from + step) % n;
    if (items[i].status === "pending" && canDecide(items[i])) return i;
  }
  return null;
}

// --- progress ------------------------------------------------------------------------------------------

export interface Progress {
  reviewed: number;
  total: number;
  pending: number;
  pct: number;
}

export function progressOf(counters: Pick<ReviewCounters, "pending" | "total"> | null | undefined): Progress {
  const total = counters?.total ?? 0;
  const pending = counters?.pending ?? 0;
  const reviewed = total - pending;
  return { reviewed, total, pending, pct: total ? Math.round((reviewed / total) * 100) : 0 };
}

/** Counters after one decision on the client (the server's answer replaces them). */
export function countAfter(counters: ReviewCounters, from: ReviewStatus, to: ReviewStatus): ReviewCounters {
  if (from === to) return counters;
  const next = { ...counters } as ReviewCounters & Record<ReviewStatus, number>;
  (next as Record<string, number>)[from] = Math.max(0, (next as Record<string, number>)[from] - 1);
  (next as Record<string, number>)[to] = (next as Record<string, number>)[to] + 1;
  return next;
}

// --- filters ---------------------------------------------------------------------------------------------

export const REVIEW_STATUSES: readonly { value: ReviewStatus; label: string }[] = [
  { value: "pending", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "amended", label: "Amended" },
  { value: "rejected", label: "Rejected" },
];

export const ENTITY_TYPES: readonly { value: ReviewEntityType; label: string }[] = [
  { value: "urban_parcel", label: "Urban parcel" },
  { value: "block", label: "Block" },
  { value: "document", label: "Whole plan" },
  { value: "zone", label: "Zone" },
];

export const SORTS: readonly { value: ReviewSort; label: string }[] = [
  { value: "pending", label: "Pending first" },
  { value: "page", label: "Page, then parcel" },
  { value: "confidence", label: "Low confidence first" },
];

export interface ReviewFilters {
  document: number | null;
  file: number | null;
  status: ReviewStatus | null;
  zone: number | null;
  entity: ReviewEntityType | null;
  page: number | null;
  sort: ReviewSort;
}

export const QUEUE_PAGE = 200;

type Params = Record<string, string | string[] | undefined>;

function first(value: string | string[] | undefined): string {
  return (Array.isArray(value) ? value[0] : value)?.trim() ?? "";
}

function positive(value: string): number | null {
  const n = Number(value);
  return Number.isInteger(n) && n > 0 ? n : null;
}

function oneOf<T extends string>(value: string, allowed: readonly { value: T }[]): T | null {
  return allowed.some((a) => a.value === value) ? (value as T) : null;
}

export function parseReviewFilters(params: Params): ReviewFilters {
  return {
    document: positive(first(params.document)),
    file: positive(first(params.file)),
    status: oneOf(first(params.status), REVIEW_STATUSES),
    zone: positive(first(params.zone)),
    entity: oneOf(first(params.entity), ENTITY_TYPES),
    page: positive(first(params.page)),
    sort: oneOf(first(params.sort), SORTS) ?? "pending",
  };
}

/** The API query of `GET /v1/admin/review` for these filters. */
export function reviewQuery(filters: ReviewFilters, offset = 0): Record<string, string | number | undefined> {
  return {
    document_id: filters.document ?? undefined,
    file_id: filters.file ?? undefined,
    status: filters.status ?? undefined,
    zone_id: filters.zone ?? undefined,
    entity_type: filters.entity ?? undefined,
    source_page: filters.page ?? undefined,
    sort: filters.sort,
    limit: QUEUE_PAGE,
    offset,
  };
}

export function emptyQueueText(filters: ReviewFilters): string {
  if (filters.status === "pending" || (!filters.status && !filters.page && !filters.entity)) {
    return filters.document
      ? "Nothing waits for review in this document. Extracted values appear here as soon as a file is read."
      : "Nothing waits for review. Extracted values appear here as soon as a document's files are read.";
  }
  return "No item matches these filters.";
}

// --- the staged payload: as printed and what the contract made of it ---------------------------------------

const RULE_WORDS: Record<string, string> = {
  decimal_comma: "decimal comma",
  grouped_number: "thousands grouping",
  ratio_to_percent: "ratio → %",
  ha_to_m2: "ha → m²",
  m2_to_ha: "m² → ha",
};
const STATED_UNITS: Record<string, string> = { percent: "%", ratio: "ratio", m2: "m²", ha: "ha", m: "m", none: "no unit" };

/** The payload as fact lines: as printed, normalised (with the rules), floors, class, table cell. */
export function payloadLines(payload: ReviewPayload | null | undefined): { label: string; text: string }[] {
  if (!payload) return [];
  const lines: { label: string; text: string }[] = [];
  const unit = payload.stated_unit ? ` (${STATED_UNITS[payload.stated_unit] ?? payload.stated_unit})` : "";
  lines.push({ label: "As printed", text: `“${payload.stated_value}”${unit}` });
  const rules = payload.normalisation ?? [];
  if (rules.length) {
    const value = typeof payload.value === "number" ? `${payload.value}${payload.unit ? ` ${payload.unit}` : ""}` : payload.value;
    lines.push({
      label: "Normalised",
      text: `${value} · ${rules.map((r) => RULE_WORDS[r] ?? r.replace(/_/g, " ")).join(", ")}`,
    });
  }
  if (payload.floors) {
    const f = payload.floors;
    lines.push({
      label: "Floors",
      text: `${f.notation}: ${f.below_ground} below ground, ${f.above_ground} above${f.attic ? ` (${f.attic} attic)` : ""}`,
    });
  }
  if (payload.land_use_class) lines.push({ label: "Land-use class", text: payload.land_use_class.replace(/_/g, " ") });
  const t = payload.table;
  if (t && (t.table || t.row || t.column || t.cell)) {
    const parts = [t.table && `table ${t.table}`, t.row && `row ${t.row}`, t.column && `column ${t.column}`, t.cell && `cell ${t.cell}`];
    lines.push({ label: "Table cell", text: parts.filter(Boolean).join(" · ") });
  }
  return lines;
}

// --- refusals in plain words -------------------------------------------------------------------------------

/** A correction the API refused (a 422 from the contract's rules): its rule and sentence. */
export function correctionRefusal(problem: { status: number; details?: unknown }): { code: string; message: string } | null {
  if (problem.status !== 422 || !Array.isArray(problem.details)) return null;
  const first = problem.details[0] as { type?: unknown; msg?: unknown } | undefined;
  if (!first || typeof first.msg !== "string") return null;
  return { code: typeof first.type === "string" ? first.type : "invalid", message: first.msg };
}

export function explainReviewProblem(problem: { status: number; message?: string; details?: unknown }): string {
  const refusal = correctionRefusal(problem);
  if (refusal) return refusal.message;
  const reason =
    problem.details && typeof problem.details === "object" && "reason" in problem.details
      ? String((problem.details as { reason?: unknown }).reason)
      : null;
  if (reason === "superseded") return "A newer reading replaced this item; its decision is closed.";
  if (reason === "published") return "This value is published; its decision is closed.";
  if (reason === "pending_review") return "Publishing waits until no document has pending items and no geometry waits for review.";
  if (problem.status === 0) return "The data service did not answer. Try again in a moment.";
  if (problem.status === 403) return "Your role cannot do this.";
  if (problem.status >= 500) return "The data service had a problem. Try again in a moment.";
  return problem.message || "The request was refused.";
}
