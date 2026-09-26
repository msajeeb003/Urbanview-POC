/**
 * Planning rules (the admin console's "Planning rules" tab, wireframe `adminRules`): the typical
 * planning values of each zone — its current zone parameter set (`/v1/admin/zone-parameters`):
 * land use, FAR (II), site coverage (IZ) %, height and floors — each with the source document and
 * page it was read from and when and by whom it was verified. The zone panel of the public map
 * shows them as the zone's "typical" values; a parcel's own document values take precedence.
 *
 * Saving is a new version of the zone's set (POST for a zone without one, PUT for an edit; the
 * API audits both). The validation mirrors the API's: at least one value, FAR 0–20, coverage
 * 0–100 %, height 0–500 m, floors a whole number, a page only with a document, the verification
 * date not in the future.
 */
import type { ChipTone } from "@/components/admin/parts";
import type { ZoneParameterSet, ZoneParametersIn } from "@/lib/api/types";

import { dayLabel, parseNumber } from "./assumptions";

export interface RuleDraft {
  zoneId: string;
  landUse: string;
  far: string;
  coverage: string;
  height: string;
  floors: string;
  documentId: string;
  page: string;
  sourceNote: string;
  verifiedOn: string;
  verifiedBy: string;
  notes: string;
}

export interface DocumentOption {
  id: number;
  name: string;
  status: string;
}

const text = (value: number | string | null | undefined): string => (value == null ? "" : String(value));

export function ruleDraftFrom(set: ZoneParameterSet | null, zoneId?: number): RuleDraft {
  return {
    zoneId: set ? String(set.zone_id) : zoneId ? String(zoneId) : "",
    landUse: set?.land_use ?? "",
    far: text(set?.max_far),
    coverage: text(set?.max_site_coverage_pct),
    height: text(set?.max_height_m),
    floors: text(set?.max_floors),
    documentId: text(set?.source?.document_id),
    page: text(set?.source?.page),
    sourceNote: set?.source?.note ?? "",
    verifiedOn: set?.verified_on ?? "",
    verifiedBy: set?.verified_by ?? "",
    notes: set?.notes ?? "",
  };
}

export type RuleErrors = Partial<Record<keyof RuleDraft | "values", string>>;

export type RuleResult = { ok: true; value: ZoneParametersIn } | { ok: false; errors: RuleErrors };

function optionalNumber(
  raw: string,
  errors: RuleErrors,
  key: keyof RuleDraft,
  { max, integer = false, label }: { max: number; integer?: boolean; label: string },
): number | null {
  if (raw.trim() === "") return null;
  const value = parseNumber(raw);
  if (!(value >= 0 && value <= max) || (integer && !Number.isInteger(value))) {
    errors[key] = `${label}: ${integer ? "a whole number" : "a number"} from 0 to ${max}`;
    return null;
  }
  return value;
}

/** The API's rules on a draft; `today` is the municipality's local date (verification not later). */
export function checkRule(draft: RuleDraft, today: string): RuleResult {
  const errors: RuleErrors = {};
  const zoneId = Number(draft.zoneId);
  if (!(zoneId > 0)) errors.zoneId = "choose the zone";
  const far = optionalNumber(draft.far, errors, "far", { max: 20, label: "FAR" });
  const coverage = optionalNumber(draft.coverage, errors, "coverage", { max: 100, label: "Coverage %" });
  const height = optionalNumber(draft.height, errors, "height", { max: 500, label: "Height (m)" });
  const floors = optionalNumber(draft.floors, errors, "floors", { max: 100, integer: true, label: "Floors" });
  const landUse = draft.landUse.trim();
  if (landUse.length > 300) errors.landUse = "at most 300 characters";
  if (!landUse && far == null && coverage == null && height == null && floors == null && !errors.far && !errors.coverage && !errors.height && !errors.floors) {
    errors.values = "give at least one of use, FAR, coverage, height or floors";
  }
  const documentId = draft.documentId ? Number(draft.documentId) : null;
  let page: number | null = null;
  if (draft.page.trim() !== "") {
    page = parseNumber(draft.page);
    if (!(Number.isInteger(page) && page >= 1)) errors.page = "a page number from 1";
    else if (documentId == null) errors.page = "a page needs a source document";
  }
  if (draft.verifiedOn) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(draft.verifiedOn)) errors.verifiedOn = "a date";
    else if (draft.verifiedOn > today) errors.verifiedOn = "not in the future";
  }
  if (draft.sourceNote.length > 500) errors.sourceNote = "at most 500 characters";
  if (draft.verifiedBy.length > 200) errors.verifiedBy = "at most 200 characters";
  if (draft.notes.length > 2000) errors.notes = "at most 2,000 characters";
  if (Object.keys(errors).length) return { ok: false, errors };
  return {
    ok: true,
    value: {
      zone_id: zoneId,
      land_use: landUse || null,
      max_far: far,
      max_site_coverage_pct: coverage,
      max_height_m: height,
      max_floors: floors,
      source_document_id: documentId,
      source_page: page,
      source_note: draft.sourceNote.trim() || null,
      verified_on: draft.verifiedOn || null,
      verified_by: draft.verifiedBy.trim() || null,
      notes: draft.notes.trim() || null,
    },
  };
}

export function heightText(set: Pick<ZoneParameterSet, "max_height_m" | "max_floors">): string {
  const parts: string[] = [];
  if (set.max_height_m != null) parts.push(`${set.max_height_m} m`);
  if (set.max_floors != null) parts.push(`${set.max_floors} floor${set.max_floors === 1 ? "" : "s"}`);
  return parts.join(" · ") || "—";
}

export function sourceText(set: Pick<ZoneParameterSet, "source">): string {
  if (!set.source) return "—";
  const name = set.source.document_name ?? `Document ${set.source.document_id}`;
  return set.source.page ? `${name} · p.${set.source.page}` : name;
}

export function verifiedChip(set: Pick<ZoneParameterSet, "verified_on" | "verified_by">): {
  tone: ChipTone;
  label: string;
  detail: string;
} {
  if (!set.verified_on) return { tone: "pend", label: "Unverified", detail: "Not verified against its source yet" };
  return {
    tone: "ok",
    label: "Verified",
    detail: `Verified ${dayLabel(set.verified_on)}${set.verified_by ? ` by ${set.verified_by}` : ""}`,
  };
}

export function percent(value: number | null | undefined): string {
  return value == null ? "—" : `${value}%`;
}

export function far(value: number | null | undefined): string {
  return value == null ? "—" : value.toFixed(1);
}

/** The municipality's local date ("2026-09-26"), for the verification date's upper bound. */
export function localToday(timezone: string | null | undefined, now: Date = new Date()): string {
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: timezone || "UTC",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(now);
  } catch {
    return now.toISOString().slice(0, 10);
  }
}

const VALUE_FIELDS: (keyof RuleDraft)[] = ["landUse", "far", "coverage", "height", "floors", "documentId", "page"];

/** True when a value or its source changed but the verification is the old one: the rule then
 * saves as unverified (an old verification date never vouches for new values). */
export function staleVerification(draft: RuleDraft, rule: ZoneParameterSet | null): boolean {
  if (rule == null || !rule.verified_on) return false;
  const original = ruleDraftFrom(rule);
  const changed = VALUE_FIELDS.some((key) => draft[key].trim() !== original[key].trim());
  return changed && draft.verifiedOn === original.verifiedOn && draft.verifiedBy === original.verifiedBy;
}
