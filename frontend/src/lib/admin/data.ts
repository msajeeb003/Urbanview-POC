/**
 * The rules of the "Data sources" screen (`/admin/data`), kept out of the components so they can
 * be tested: the public data sources the platform reads, what an uploaded file is (by extension),
 * the words and chips of document status, pipeline state, extraction and jobs, when the screen
 * polls, the list filters, and the plain-language sentences for refused actions.
 *
 * Vocabulary is the API's: document status adopted / in_progress / superseded; pipeline state
 * no_files → not_extracted → processing → ready_for_review → reviewed → published (failed when a
 * file's extraction failed); a file's role says what reads it (text = the AI extraction,
 * drawing = the geometry job, both).
 */
import type { ChipTone } from "@/components/admin/parts";
import type {
  AdminDocument,
  AdminDocumentFile,
  AdminJob,
  DocumentState,
  DocumentStatus,
  ExtractionState,
  FileRole,
  JobStateFilter,
} from "@/lib/api/types";

// --- the public sources (wireframe `adminData`) -----------------------------------------------

export interface DataSource {
  source: string;
  provides: string;
  format: string;
}

/** The wireframe's rows: where UrbanView's public official data comes from. */
export const DATA_SOURCES: readonly DataSource[] = [
  { source: "eRegistri (lamp.gov.me)", provides: "Adopted planning documents", format: "PDF" },
  { source: "mondarchitects.com/site-check", provides: "Zone & document structure", format: "Web" },
  { source: "eKatastar", provides: "Ownership & legal burdens", format: "API" },
  { source: "eMapa", provides: "Spatial & cadastral data", format: "GIS" },
  { source: "Geoportal UZN", provides: "Cadastral parcels", format: "GIS" },
  { source: "Realitica / Estitor", provides: "Market listings", format: "Web" },
  { source: "Monstat", provides: "Prices & cost indices", format: "Table" },
];

// --- uploads ------------------------------------------------------------------------------------

export type UploadKind = "planning_document" | "gis" | "cadastral_extract";

export const UPLOAD_KINDS: readonly { value: UploadKind; label: string }[] = [
  { value: "planning_document", label: "Planning document (PDF)" },
  { value: "gis", label: "GIS file" },
  { value: "cadastral_extract", label: "Cadastral extract" },
];

const KIND_BY_EXTENSION: Record<string, UploadKind> = {
  pdf: "planning_document",
  zip: "gis",
  geojson: "gis",
  json: "gis",
  gpkg: "gis",
  dxf: "gis",
  kml: "gis",
  kmz: "gis",
  csv: "cadastral_extract",
  txt: "cadastral_extract",
  xlsx: "cadastral_extract",
  xls: "cadastral_extract",
};

/** What the API accepts per kind (`api/services/admin.py` KIND_SPECS), for the file pickers. */
export const ACCEPT_ANY = Object.keys(KIND_BY_EXTENSION)
  .map((ext) => `.${ext}`)
  .join(",");
export const ACCEPT_PDF = ".pdf,application/pdf";

export function extensionOf(filename: string): string {
  const base = filename.replace(/\\/g, "/").split("/").pop() ?? "";
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
}

/** The kind a dropped file most likely is; null when the API would refuse every kind. */
export function guessKind(filename: string): UploadKind | null {
  return KIND_BY_EXTENSION[extensionOf(filename)] ?? null;
}

export function isPdf(filename: string): boolean {
  return extensionOf(filename) === "pdf";
}

export const FILE_ROLES: readonly { value: FileRole; label: string; hint: string }[] = [
  { value: "text", label: "Text", hint: "Read by the AI extraction (parameter tables, rules)" },
  { value: "drawing", label: "Drawing", hint: "Read by the geometry job (plan sheets)" },
  { value: "both", label: "Both", hint: "Text and drawings in one PDF" },
];

export function roleLabel(role: FileRole): string {
  return FILE_ROLES.find((r) => r.value === role)?.label ?? role;
}

/** Roles a file of this kind may take: GIS files are read by the geometry job only. */
export function rolesFor(kind: string): FileRole[] {
  return kind === "gis" ? ["drawing"] : ["text", "drawing", "both"];
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// --- documents ------------------------------------------------------------------------------------

export const DOCUMENT_STATUSES: readonly { value: DocumentStatus; label: string; tone: ChipTone }[] = [
  { value: "adopted", label: "Adopted", tone: "ok" },
  { value: "in_progress", label: "In progress", tone: "pend" },
  { value: "superseded", label: "Superseded", tone: "rev" },
];

export function statusChip(status: DocumentStatus): { tone: ChipTone; label: string } {
  const found = DOCUMENT_STATUSES.find((s) => s.value === status);
  return found ? { tone: found.tone, label: found.label } : { tone: "rev", label: status };
}

export const DOCUMENT_STATES: readonly { value: DocumentState; label: string; tone: ChipTone }[] = [
  { value: "no_files", label: "No files", tone: "rev" },
  { value: "not_extracted", label: "Not extracted", tone: "rev" },
  { value: "processing", label: "Processing", tone: "pend" },
  { value: "ready_for_review", label: "Ready for review", tone: "pend" },
  { value: "failed", label: "Needs attention", tone: "pend" },
  { value: "reviewed", label: "Reviewed", tone: "ok" },
  { value: "published", label: "Published", tone: "ok" },
];

export function stateChip(state: DocumentState): { tone: ChipTone; label: string } {
  const found = DOCUMENT_STATES.find((s) => s.value === state);
  return found ? { tone: found.tone, label: found.label } : { tone: "rev", label: state };
}

export const JOB_STATE_FILTERS: readonly { value: JobStateFilter; label: string }[] = [
  { value: "queued", label: "Queued" },
  { value: "running", label: "Running" },
  { value: "succeeded", label: "Succeeded" },
  { value: "failed", label: "Failed" },
];

/** Document type choices: the spec's DUP / PUP / PGR first, then the profile's other types. */
export function documentTypeOptions(
  types: Record<string, string> | undefined,
  typesEn: Record<string, string> | undefined,
): { value: string; label: string }[] {
  const keys = Object.keys({ ...(types ?? {}), ...(typesEn ?? {}) });
  const first = ["DUP", "PUP", "PGR"];
  const ordered = [...first.filter((k) => keys.includes(k) || !keys.length), ...keys.filter((k) => !first.includes(k)).sort()];
  return ordered.map((key) => {
    const name = typesEn?.[key] ?? types?.[key];
    return { value: key, label: !name ? key : name.startsWith(key) ? name : `${key} — ${name}` };
  });
}

// --- files: extraction and geometry -----------------------------------------------------------------

const ACTIVE_EXTRACTION: readonly ExtractionState[] = ["queued", "extracting", "retrying"];
const ACTIVE_JOB = ["queued", "running", "retrying"];

export function extractionActive(file: AdminDocumentFile): boolean {
  return ACTIVE_EXTRACTION.includes(file.extraction_state ?? "none");
}

export function jobActive(job: AdminJob | null | undefined): boolean {
  return !!job && ACTIVE_JOB.includes(job.status);
}

/** The screen keeps polling while any file is being extracted or its geometry job runs. */
export function documentActive(document: AdminDocument): boolean {
  return (document.files ?? []).some((f) => extractionActive(f) || jobActive(f.geometry_job));
}

export function anyActive(documents: readonly AdminDocument[]): boolean {
  return documents.some(documentActive);
}

export interface Pill {
  tone: ChipTone;
  label: string;
  /** Mono detail after the chip: attempts, progress, cost. */
  detail?: string;
  /** Why it failed, in the API's words. */
  reason?: string | null;
  /** Not a state but "does not apply" (a drawing is not extracted, a text file has no geometry). */
  muted?: boolean;
}

export function formatCost(eur: number | null | undefined): string | null {
  if (eur == null) return null;
  if (eur > 0 && eur < 0.01) return "< €0.01";
  return `€${eur.toFixed(2)}`;
}

function attempts(job: AdminJob | null | undefined): string | null {
  if (!job || job.attempts <= 1) return null;
  return `attempt ${job.attempts}/${job.max_attempts}`;
}

function join(...parts: (string | null | undefined)[]): string | undefined {
  const kept = parts.filter(Boolean);
  return kept.length ? kept.join(" · ") : undefined;
}

/** The file's extraction: queued → extracting → ready for review / failed with reason. */
export function extractionPill(file: AdminDocumentFile): Pill {
  const state = file.extraction_state ?? "none";
  const run = file.extraction;
  const job = file.extraction_job;
  const cost = formatCost(run?.estimated_cost_eur ?? job?.cost?.estimated_cost_eur);
  switch (state) {
    case "queued":
      return { tone: "pend", label: "Queued", detail: join(attempts(job)) };
    case "extracting":
      return {
        tone: "pend",
        label: "Extracting",
        detail: join(run && run.chunks_total ? `${run.chunks_done}/${run.chunks_total} chunks` : null, attempts(job)),
      };
    case "retrying":
      return { tone: "pend", label: "Retrying", detail: join(attempts(job)), reason: job?.error ?? null };
    case "ready_for_review":
      return {
        tone: "ok",
        label: "Ready for review",
        detail: join(`${file.items?.total ?? run?.items_written ?? 0} items`, cost),
      };
    case "failed":
      return {
        tone: "pend",
        label: "Failed",
        detail: join(job ? `${job.attempts}/${job.max_attempts} attempts` : null, cost),
        reason: file.extraction_error ?? run?.error ?? job?.error ?? null,
      };
    default:
      return file.role === "drawing"
        ? { tone: "rev", label: "— drawing", muted: true }
        : { tone: "rev", label: "Not extracted" };
  }
}

/** A job's status in the table's words (the geometry column). */
export function jobPill(job: AdminJob | null | undefined, role?: FileRole): Pill {
  if (!job) return role === "text" ? { tone: "rev", label: "— text file", muted: true } : { tone: "rev", label: "Not run" };
  const cost = formatCost(job.cost?.estimated_cost_eur);
  switch (job.status) {
    case "queued":
      return { tone: "pend", label: "Queued" };
    case "running":
      return { tone: "pend", label: "Running", detail: join(attempts(job)) };
    case "retrying":
      return { tone: "pend", label: "Retrying", detail: join(attempts(job)), reason: job.error };
    case "succeeded":
      return { tone: "ok", label: "Succeeded", detail: join(cost) };
    case "cancelled":
      return { tone: "rev", label: "Cancelled" };
    default:
      return {
        tone: "pend",
        label: "Failed",
        detail: `${job.attempts}/${job.max_attempts} attempts`,
        reason: job.error,
      };
  }
}

export function removeBlockerText(reason: string | null | undefined): string | null {
  switch (reason) {
    case "items_accepted":
      return "Items read from this file were approved; it stays with the document.";
    case "values_published":
      return "Published values cite this file; it stays with the document.";
    case "extraction_active":
      return "The file is being extracted; remove it once the job has finished.";
    default:
      return null;
  }
}

/** The review queue filtered to one document (and file): the AI review queue tab. */
export function reviewHref(documentId: number, fileId?: number): string {
  return fileId ? `/admin/review?document=${documentId}&file=${fileId}` : `/admin/review?document=${documentId}`;
}

export function documentHref(documentId: number): string {
  return `/admin/data/documents/${documentId}`;
}

// --- list filters -------------------------------------------------------------------------------------

export interface DocumentFilters {
  zone: number | null;
  status: DocumentStatus | null;
  state: DocumentState | null;
  job: JobStateFilter | null;
  q: string;
  page: number;
}

export const PAGE_SIZE = 50;

type Params = Record<string, string | string[] | undefined>;

function first(value: string | string[] | undefined): string {
  return (Array.isArray(value) ? value[0] : value)?.trim() ?? "";
}

function oneOf<T extends string>(value: string, allowed: readonly { value: T }[]): T | null {
  return allowed.some((a) => a.value === value) ? (value as T) : null;
}

export function parseFilters(params: Params): DocumentFilters {
  const zone = Number(first(params.zone));
  const page = Number(first(params.page));
  return {
    zone: Number.isInteger(zone) && zone > 0 ? zone : null,
    status: oneOf(first(params.status), DOCUMENT_STATUSES),
    state: oneOf(first(params.state), DOCUMENT_STATES),
    job: oneOf(first(params.job), JOB_STATE_FILTERS),
    q: first(params.q).slice(0, 200),
    page: Number.isInteger(page) && page > 1 ? page : 1,
  };
}

/** The API query of `GET /v1/admin/documents` for these filters. */
export function filterQuery(filters: DocumentFilters): Record<string, string | number | undefined> {
  return {
    zone_id: filters.zone ?? undefined,
    status: filters.status ?? undefined,
    state: filters.state ?? undefined,
    job_state: filters.job ?? undefined,
    q: filters.q || undefined,
    limit: PAGE_SIZE,
    offset: (filters.page - 1) * PAGE_SIZE,
  };
}

export function hasFilters(filters: DocumentFilters): boolean {
  return !!(filters.zone || filters.status || filters.state || filters.job || filters.q);
}

/** The screen's URL for these filters (and page). */
export function filtersHref(filters: DocumentFilters, page = 1): string {
  const params = new URLSearchParams();
  if (filters.zone) params.set("zone", String(filters.zone));
  if (filters.status) params.set("status", filters.status);
  if (filters.state) params.set("state", filters.state);
  if (filters.job) params.set("job", filters.job);
  if (filters.q) params.set("q", filters.q);
  if (page > 1) params.set("page", String(page));
  const query = params.toString();
  return query ? `/admin/data?${query}` : "/admin/data";
}

/** "No documents yet" or, with filters, a sentence that says what matched nothing. */
export function emptyDocumentsText(filters: DocumentFilters): string {
  if (!hasFilters(filters)) return "No planning documents are registered yet. Upload the PDFs, then register the document under its zone.";
  return "No document matches these filters. Clear them to see every document.";
}

// --- refusals in plain language -------------------------------------------------------------------------

export interface ApiProblem {
  status: number;
  code?: string;
  message?: string;
  details?: unknown;
}

function reasonOf(details: unknown): string | null {
  if (details && typeof details === "object" && "reason" in details) {
    const reason = (details as { reason?: unknown }).reason;
    return typeof reason === "string" ? reason : null;
  }
  return null;
}

/** One sentence for a refused call; the API's own message when it is already plain. */
export function explainProblem(problem: ApiProblem): string {
  const reason = reasonOf(problem.details);
  switch (reason) {
    case "no_coverage_geometry":
      return "This document has no coverage area yet: run the geometry job first.";
    case "not_current_version":
      return "Only the current version of a document can be changed.";
    case "drawing_file":
      return "This file is a drawing: set its role to text or both to extract it.";
    case "no_file":
      return "The document has no PDF to extract yet: upload one first.";
    case "items_accepted":
    case "values_published":
    case "extraction_active":
      return removeBlockerText(reason) ?? "The file cannot be removed.";
    default:
      break;
  }
  if (problem.status === 0) return "The data service did not answer. Try again in a moment.";
  if (problem.status === 413) return "The file is larger than the upload limit.";
  if (problem.status === 403) return "Your role cannot do this.";
  if (problem.status >= 500) return "The data service had a problem. Try again in a moment.";
  return problem.message || "The request was refused.";
}

/** Field errors of a 422 (`details: [{loc: ["body", "name"], msg}]`), keyed by the last loc part. */
export function fieldErrors(details: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (!Array.isArray(details)) return out;
  for (const entry of details) {
    if (!entry || typeof entry !== "object") continue;
    const loc = (entry as { loc?: unknown }).loc;
    const msg = (entry as { msg?: unknown }).msg;
    if (!Array.isArray(loc) || typeof msg !== "string") continue;
    const path = loc.filter((p) => p !== "body");
    const key = path[0] === "files" ? "files" : String(path[path.length - 1] ?? "form");
    if (!out[key]) out[key] = msg.replace(/^Value error, /, "");
  }
  return out;
}

/** The zone select's options: the zone index, by name. */
export function zoneOptions(zones: readonly { id: number; name: string }[] | null | undefined): { id: number; name: string }[] {
  return [...(zones ?? [])].map((z) => ({ id: z.id, name: z.name })).sort((a, b) => a.name.localeCompare(b.name));
}
