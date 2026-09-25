"use client";

/**
 * Pieces every information panel variant shares: the sticky header (wireframe `.phead`), the
 * loading and error bodies, the document status chip (`.dstat`), `panel_viewed`, and the small
 * text helpers for plan types and typical heights.
 */
import { useEffect, type ReactNode } from "react";

import { useTrack } from "@/lib/analytics/react";
import type { DocumentPanel, MunicipalityProfile, ZonePanel } from "@/lib/api/types";
import { formatDate, formatFigure } from "@/lib/format";
import { useShell } from "@/lib/store";

import { Cta } from "../ui/cta";

/** Keeps the title line its height while the name is not known yet. */
const NBSP = String.fromCharCode(160);

export type DocStatus = "adopted" | "in_progress" | "superseded";

const STATUS: Record<DocStatus, { cls: string; label: string; plan: string }> = {
  adopted: { cls: "adopted", label: "Adopted", plan: "adopted plan" },
  in_progress: { cls: "progress", label: "In progress", plan: "plan in progress" },
  superseded: { cls: "superseded", label: "Superseded", plan: "superseded plan" },
};

/** Status chip: a dot and the word (`.dstat.adopted | .progress | .superseded`). */
export function DocStatusChip({ status }: { status: DocStatus }) {
  const s = STATUS[status];
  return <span className={`dstat ${s.cls}`}>{s.label}</span>;
}

/** "adopted plan" / "plan in progress" / "superseded plan" (the document panel's eyebrow). */
export function planPhrase(status: DocStatus): string {
  return STATUS[status].plan;
}

/** `DUP — Detailed urban plan` from the profile's terminology (never hard-coded). */
export function docTypeLabel(type: string | null | undefined, profile: MunicipalityProfile | undefined): string {
  if (!type) return "";
  const terms = profile?.terminology;
  const name = terms?.document_types_en?.[type] ?? terms?.document_types?.[type];
  return name ? `${type} — ${name}` : type;
}

/** `24 m · 7 floors`, `7 floors`, `24 m`; null when neither is known. */
export function heightText(t: { max_height_m?: number | null; max_floors?: number | null }): string | null {
  const parts: string[] = [];
  if (t.max_height_m != null) parts.push(`${formatFigure(t.max_height_m, 1)} m`);
  if (t.max_floors != null) parts.push(`${t.max_floors} ${t.max_floors === 1 ? "floor" : "floors"}`);
  return parts.length ? parts.join(" · ") : null;
}

export type ZoneDoc = ZonePanel["planning_documents"][number];
export type DocZone = DocumentPanel["zones"][number];

/**
 * The meta line of a zone's document: `source PDF · eRegistri · adopted 12 May 2019 · 4 parcels
 * with data`; an adopted document the map does not cover yet says "not yet digitised".
 */
export function documentMeta(d: ZoneDoc): string {
  const parts: string[] = [];
  if (d.file_available) parts.push("source PDF");
  if (d.source) parts.push(d.source);
  const adopted = d.adopted_on ? formatDate(d.adopted_on) : null;
  if (adopted) parts.push(`adopted ${adopted}`);
  if (d.covered) {
    const n = d.parcel_count ?? 0;
    parts.push(`${n} ${n === 1 ? "parcel" : "parcels"} with data`);
  } else if (d.status === "adopted") {
    parts.push("not yet digitised");
  }
  return parts.join(" · ");
}

/** A zone the document spans: `24 m · FAR 3.2` from its typical values (height, else floors). */
export function zoneTypicalLine(z: DocZone): string {
  const t = z.typical;
  if (!t) return "—";
  const parts: string[] = [];
  const height = t.max_height_m != null ? `${formatFigure(t.max_height_m, 1)} m` : heightText({ max_floors: t.max_floors });
  if (height) parts.push(height);
  if (t.max_far != null) parts.push(`FAR ${formatFigure(t.max_far, 1)}`);
  return parts.length ? parts.join(" · ") : "—";
}

/** Sticky panel header: ✕ (clears the selection and its map highlight), eyebrow, title, sub-line. */
export function PanelHead({ eyebrow, title, sub }: { eyebrow: ReactNode; title: ReactNode; sub?: ReactNode }) {
  const clearSelection = useShell((s) => s.clearSelection);
  return (
    <div className="phead">
      <button type="button" className="pclose" aria-label="Close the panel" onClick={clearSelection}>
        ✕
      </button>
      <div className="peyebrow">{eyebrow}</div>
      <div className="ptitle">{title}</div>
      {sub != null && <div className="psub">{sub}</div>}
    </div>
  );
}

/** While the panel payload loads: the header with what the selection already knows. */
export function PanelLoading({ eyebrow, title }: { eyebrow: ReactNode; title?: ReactNode }) {
  return (
    <div className="pscroll" aria-busy="true">
      <PanelHead eyebrow={eyebrow} title={title ?? NBSP} />
      <div className="sect">
        <p className="panelnote">Loading…</p>
      </div>
    </div>
  );
}

/** The API did not answer: a neutral note and a retry, never an error screen. */
export function PanelUnavailable({ eyebrow, title, onRetry }: { eyebrow: ReactNode; title?: ReactNode; onRetry: () => void }) {
  return (
    <div className="pscroll">
      <PanelHead eyebrow={eyebrow} title={title ?? NBSP} />
      <div className="sect">
        <p className="panelnote">The planning data service is not answering. Try again in a moment.</p>
        <Cta variant="line" onClick={onRetry} style={{ marginTop: 12 }}>
          Try again
        </Cta>
      </div>
    </div>
  );
}

/**
 * `panel_viewed { panel_type, …ids }` once the panel has its data (`ids` null until then); again
 * whenever the ids change (another entity in the same variant) and on every new open.
 */
export function usePanelViewed(
  panelType: "zone" | "document" | "cadastral" | "urban",
  ids: Record<string, number | undefined> | null,
) {
  const track = useTrack();
  const key = ids ? JSON.stringify(ids) : "";
  useEffect(() => {
    if (!key) return;
    track("panel_viewed", { panel_type: panelType, ...(JSON.parse(key) as Record<string, number>) });
  }, [track, panelType, key]);
}
