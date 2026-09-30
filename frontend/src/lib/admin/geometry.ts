/**
 * The rules of the geometry review (`/admin/review/geometry`), kept out of the components so they
 * can be tested: the words for origins, layers, QA and decisions, the queue's filters, the next
 * pending batch, the preview's projection (a batch's GeoJSON onto an SVG box) and the sentence
 * that says what publishing waits for.
 *
 * A draft is one staged geometry batch (one layer of one producing run: a georeferenced plan or
 * GIS drawing, a zone import, a cadastral import), the pilot scope's `staging.geometry_draft`.
 */
import type { ChipTone } from "@/components/admin/parts";
import type { GeometryDraft, GeometryFeatures, GeometryOrigin, GeometryReviewStatus, QaIssue } from "@/lib/api/types";

export const ORIGINS: readonly { value: GeometryOrigin; label: string; note: string }[] = [
  { value: "vector_pdf", label: "Vector plan PDF", note: "drawing layers read from the plan's PDF and georeferenced" },
  { value: "manual_qgis", label: "QGIS drawing", note: "drawn or redrawn in QGIS (scanned sheets, the zones)" },
  { value: "official_gis", label: "Official GIS", note: "an official GIS file: a supplied plan drawing or the cadastre" },
];

export const GEOMETRY_STATUSES: readonly { value: GeometryReviewStatus; label: string }[] = [
  { value: "pending", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
];

/** The staged layers (the API names each draft's layer too: `layer_label`). */
export const GEOMETRY_LAYERS: readonly { value: string; label: string }[] = [
  { value: "urban_parcels", label: "Planned urban parcels" },
  { value: "urban_blocks", label: "Urban blocks" },
  { value: "document_coverage", label: "Plan boundary" },
  { value: "land_use", label: "Land use" },
  { value: "zones", label: "Zones" },
  { value: "cadastral_parcels", label: "Cadastral parcels" },
  { value: "cadastral_municipalities", label: "Cadastral municipalities (KO)" },
];

const DATASET_KINDS: Record<string, string> = {
  georef: "georeferencing",
  zones: "zone import",
  cadastre: "cadastral import",
};

/** The producing run's label: its dataset's, else what the batch records. */
export function runOf(draft: Pick<GeometryDraft, "dataset" | "dataset_version">): string | null {
  return draft.dataset?.version ?? draft.dataset_version ?? null;
}

export function originLabel(origin: GeometryOrigin | null | undefined): string {
  return ORIGINS.find((o) => o.value === origin)?.label ?? "Origin not recorded";
}

/** "Planned urban parcels — DUP-NG12" (the document's short code, else its name or the dataset). */
export function draftTitle(draft: GeometryDraft): string {
  const subject = draft.document?.short_code ?? draft.document?.name ?? runOf(draft) ?? `batch #${draft.id}`;
  return `${draft.layer_label} — ${subject}`;
}

/** "Vector plan PDF · georeferencing geo-12-20261001-1 · 560 features". */
export function draftSource(draft: GeometryDraft): string {
  const parts = [originLabel(draft.origin)];
  if (draft.dataset) parts.push(`${DATASET_KINDS[draft.dataset.kind] ?? draft.dataset.kind} ${draft.dataset.version}`);
  else if (draft.dataset_version) parts.push(draft.dataset_version);
  parts.push(`${draft.feature_count.toLocaleString("en-US")} feature${draft.feature_count === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

export function qaChip(status: GeometryDraft["qa_status"]): { tone: ChipTone; label: string } {
  if (status === "pass") return { tone: "ok", label: "QA passed" };
  if (status === "warn") return { tone: "pend", label: "QA warnings" };
  if (status === "fail") return { tone: "rev", label: "QA fails" };
  return { tone: "pend", label: "Not checked yet" };
}

/** The decision and the batch's life in one chip. */
export function decisionChip(draft: Pick<GeometryDraft, "status" | "review_status">): { tone: ChipTone; label: string } {
  if (draft.status === "superseded") return { tone: "pend", label: "Superseded" };
  if (draft.status === "published") {
    return draft.review_status ? { tone: "ok", label: "Published" } : { tone: "ok", label: "Published before review" };
  }
  if (draft.status === "rejected" || draft.review_status === "rejected") return { tone: "rev", label: "Rejected" };
  if (draft.review_status === "approved") return { tone: "ok", label: "Approved — next publish" };
  return { tone: "pend", label: "Pending" };
}

export function isPending(draft: Pick<GeometryDraft, "status" | "review_status">): boolean {
  return draft.status === "staged" && draft.review_status === "pending";
}

export function issueTone(issue: Pick<QaIssue, "severity">): ChipTone {
  return issue.severity === "error" ? "rev" : "pend";
}

/** The issue's name for the console ("Invalid geometry", "Systematic offset (georeferencing)", …). */
export function issueName(code: string): string {
  const names: Record<string, string> = {
    invalid_geometry: "Invalid geometry",
    empty_geometry: "Empty geometry",
  };
  if (names[code]) return names[code];
  const [kind, rest] = code.includes(".") ? code.split(".", 2) : ["", code];
  const words = rest.replace(/_/g, " ");
  const from = DATASET_KINDS[kind] ? ` (${DATASET_KINDS[kind]})` : "";
  return `${words.charAt(0).toUpperCase()}${words.slice(1)}${from}`;
}

/** The next pending draft after `from` (wrapping to the top), or null. */
export function nextPendingDraft(list: readonly GeometryDraft[], from: number): number | null {
  for (let step = 1; step <= list.length; step += 1) {
    const i = (from + step) % list.length;
    if (isPending(list[i])) return i;
  }
  return null;
}

// --- filters ---------------------------------------------------------------------------------

export interface GeometryFilters {
  status: GeometryReviewStatus | null;
  origin: GeometryOrigin | null;
  layer: string | null;
  document: number | null;
  history: boolean;
}

type Params = Record<string, string | string[] | undefined>;

function first(value: string | string[] | undefined): string {
  return (Array.isArray(value) ? value[0] : value)?.trim() ?? "";
}

export function parseGeometryFilters(params: Params): GeometryFilters {
  const status = first(params.status);
  const origin = first(params.origin);
  const layer = first(params.layer);
  const document = Number(first(params.document));
  return {
    status: GEOMETRY_STATUSES.some((s) => s.value === status) ? (status as GeometryReviewStatus) : null,
    origin: ORIGINS.some((o) => o.value === origin) ? (origin as GeometryOrigin) : null,
    layer: GEOMETRY_LAYERS.some((l) => l.value === layer) ? layer : null,
    document: Number.isInteger(document) && document > 0 ? document : null,
    history: first(params.history) === "1",
  };
}

/** The API query of `GET /v1/admin/geometry` for these filters. */
export function geometryQuery(filters: GeometryFilters): Record<string, string | number | boolean | undefined> {
  return {
    status: filters.status ?? undefined,
    origin: filters.origin ?? undefined,
    layer_id: filters.layer ?? undefined,
    document_id: filters.document ?? undefined,
    include_history: filters.history || undefined,
    limit: 200,
  };
}

export function hasGeometryFilters(filters: GeometryFilters): boolean {
  return Boolean(filters.status || filters.origin || filters.layer || filters.document || filters.history);
}

export function emptyGeometryText(filters: GeometryFilters): string {
  if (hasGeometryFilters(filters)) return "No staged geometry matches these filters.";
  return "No staged geometry waits for review. Georeferenced plans, GIS drawings, zone and cadastral imports appear here when they are staged.";
}

// --- the preview -----------------------------------------------------------------------------

export interface PreviewShape {
  key: string;
  label: string;
  d: string;
  issues: string[];
  area: number | null;
}

type Position = number[];
type Geometry = { type: string; coordinates: unknown } | null;

function rings(geometry: Geometry): { ring: Position[]; closed: boolean }[] {
  if (!geometry) return [];
  const c = geometry.coordinates;
  switch (geometry.type) {
    case "Polygon":
      return (c as Position[][]).map((ring) => ({ ring, closed: true }));
    case "MultiPolygon":
      return (c as Position[][][]).flatMap((poly) => poly.map((ring) => ({ ring, closed: true })));
    case "LineString":
      return [{ ring: c as Position[], closed: false }];
    case "MultiLineString":
      return (c as Position[][]).map((ring) => ({ ring, closed: false }));
    default:
      return [];
  }
}

/**
 * The batch's features on a `width` × `height` SVG box: an equirectangular projection around the
 * batch's middle latitude (east–west shrunk by its cosine, so shapes keep their proportions),
 * fitted with `pad` pixels to spare, north up.
 */
export function projectFeatures(
  data: Pick<GeometryFeatures, "bbox" | "features">,
  width: number,
  height: number,
  pad = 12,
): { shapes: PreviewShape[] } {
  const bbox = data.bbox;
  if (!bbox || bbox.length !== 4) return { shapes: [] };
  const [west, south, east, north] = bbox;
  const k = Math.cos((((south + north) / 2) * Math.PI) / 180);
  const spanX = Math.max((east - west) * k, 1e-9);
  const spanY = Math.max(north - south, 1e-9);
  const scale = Math.min((width - 2 * pad) / spanX, (height - 2 * pad) / spanY);
  const offX = (width - spanX * scale) / 2;
  const offY = (height - spanY * scale) / 2;
  const x = (lng: number) => offX + (lng - west) * k * scale;
  const y = (lat: number) => offY + (north - lat) * scale;
  const round = (n: number) => Math.round(n * 10) / 10;
  const collection = data.features as { features?: { id?: string; geometry: Geometry; properties?: Record<string, unknown> }[] };
  const shapes = (collection.features ?? []).map((f) => {
    const props = f.properties ?? {};
    const d = rings(f.geometry)
      .map(({ ring, closed }) => {
        const pts = ring.map(([lng, lat]) => `${round(x(lng))} ${round(y(lat))}`);
        return pts.length ? `M${pts.join("L")}${closed ? "Z" : ""}` : "";
      })
      .join("");
    return {
      key: String(props.key ?? f.id ?? ""),
      label: String(props.label ?? props.key ?? ""),
      d,
      issues: Array.isArray(props.issues) ? (props.issues as string[]) : [],
      area: typeof props.area_m2 === "number" ? props.area_m2 : null,
    };
  });
  return { shapes };
}

// --- refusals in plain words ---------------------------------------------------------------------

export function explainGeometryProblem(problem: { status: number; message?: string; details?: unknown }): string {
  const reason =
    problem.details && typeof problem.details === "object" && "reason" in problem.details
      ? String((problem.details as { reason?: unknown }).reason)
      : null;
  if (reason === "qa_failed") return "Its checks fail (invalid or empty features): reject it, fix the geometry and stage it again.";
  if (reason === "rejected") return "It was rejected: fix the geometry and stage it again (a new batch).";
  if (reason === "published") return "It is published; changes go through a new import.";
  if (reason === "superseded") return "A newer run of the same geometry replaced it; review that one.";
  if (problem.status === 0) return "The data service did not answer. Try again in a moment.";
  if (problem.status === 403) return "Your role cannot do this.";
  if (problem.status === 422) return "Give the reason: it goes to the audit log with the decision.";
  if (problem.status >= 500) return "The data service had a problem. Try again in a moment.";
  return problem.message || "The request was refused.";
}
