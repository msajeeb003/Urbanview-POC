/**
 * The rules of the Publish page (`/admin/publish`, the pilot scope's A4 "Publish": copies approved
 * records to a new version, computes the heatmaps and parcel links, builds the tiles, flips the
 * pointer; rollback in one action), kept pure and unit-tested: the words of the job's steps and
 * their chips, which versions a rollback may return to (the API's guards, mirrored so a button is
 * never offered that the API would refuse), and the one-line summary of a version.
 */
import type { ChipTone } from "@/components/admin/parts";
import type { PublishStatus } from "@/lib/api/types";

export type PublishVersion = PublishStatus["versions"][number];

export interface JobStep {
  name: string;
  status: "pending" | "running" | "done" | "failed" | string;
  started_at?: string | null;
  finished_at?: string | null;
}

const STEP_LABELS: Record<string, string> = {
  preflight: "Check nothing is pending",
  version: "Open the new version",
  values: "Copy the approved values",
  geometry: "Apply staged geometry",
  links: "Link cadastral and planned parcels",
  cells: "Compute the heatmaps",
  export: "Export the layers",
  tiles: "Build the map tiles",
  upload: "Upload the tiles",
  flip: "Switch the live map",
  prune: "Clear old archives",
};

export function stepLabel(name: string): string {
  return STEP_LABELS[name] ?? name.replace(/_/g, " ");
}

export function stepChip(status: string): { tone: ChipTone; label: string } {
  switch (status) {
    case "done":
      return { tone: "ok", label: "Done" };
    case "running":
      return { tone: "pend", label: "Running" };
    case "failed":
      return { tone: "rev", label: "Failed" };
    default:
      return { tone: "rev", label: "Waiting" };
  }
}

/** The job's steps from its `progress` (`{step, steps: [...]}`), or [] before it reports. */
export function jobSteps(progress: unknown): JobStep[] {
  if (!progress || typeof progress !== "object") return [];
  const steps = (progress as { steps?: unknown }).steps;
  return Array.isArray(steps) ? (steps.filter((s) => s && typeof s === "object" && "name" in s) as JobStep[]) : [];
}

/**
 * Whether "Roll back to this" may be offered for `version`: not the current one, published
 * before it, and its archive still kept (the API answers 409 otherwise).
 */
export function canRollBackTo(version: PublishVersion, current: PublishVersion | null): boolean {
  if (version.is_current || !current) return false;
  if (version.archive_pruned_at) return false;
  return Date.parse(version.published_at) < Date.parse(current.published_at) || version.id < current.id;
}

/** `12.4 MB`, `—` when unknown. */
export function sizeText(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** `1,284 values · 667 parcel links · 12 heatmap cells` from the version's counts (what it holds). */
export function countsText(counts: Record<string, unknown> | null | undefined): string {
  if (!counts) return "—";
  const num = (v: unknown) => (typeof v === "number" ? v : null);
  const sum = (v: unknown) =>
    v && typeof v === "object" ? Object.values(v as Record<string, unknown>).reduce<number>((a, b) => a + (num(b) ?? 0), 0) : num(v);
  const parts: string[] = [];
  // the publish job's counts: values carried forward + values published from the review queue
  const carried = num(counts.values_carried);
  const published = num(counts.values_published);
  if (carried != null || published != null) {
    parts.push(`${((carried ?? 0) + (published ?? 0)).toLocaleString("en-US")} values`);
  }
  const links = num(counts.parcel_links);
  if (links != null) parts.push(`${links.toLocaleString("en-US")} parcel links`);
  const cells = sum(counts.choropleth_cells);
  if (cells != null) parts.push(`${cells.toLocaleString("en-US")} heatmap cells`);
  return parts.length ? parts.join(" · ") : "—";
}

/** "Publishing waits for: DUP Novi Grad (3 pending), … and 2 geometry batches to review". */
export function blockersText(
  blockers: PublishStatus["blockers"],
  geometry: PublishStatus["geometry_blockers"] = [],
): string | null {
  const batches = geometry?.length ?? 0;
  if (!blockers.length && !batches) return null;
  const named = blockers.slice(0, 3).map((b) => `${b.document_name} (${b.pending} pending)`);
  if (blockers.length > 3) named.push(`${blockers.length - 3} more document${blockers.length - 3 === 1 ? "" : "s"}`);
  if (batches) named.push(`${batches} geometry batch${batches === 1 ? "" : "es"} to review`);
  return `Publishing waits for: ${named.join(", ")}`;
}
