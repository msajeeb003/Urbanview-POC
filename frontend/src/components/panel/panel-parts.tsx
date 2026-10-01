"use client";

/**
 * Pieces every information panel variant shares: the sticky header (wireframe `.phead`), the
 * loading and error bodies, the document status chip (`.dstat`), `panel_viewed`, the small
 * text helpers for plan types and typical heights, the registry link of a document and the line
 * naming the published data version a panel shows.
 */
import { Fragment, useEffect, type ReactNode } from "react";

import { useTrack } from "@/lib/analytics/react";
import { useTilesCurrent } from "@/lib/api/hooks";
import type { MunicipalityProfile, ZonePanel } from "@/lib/api/types";
import { formatDate } from "@/lib/format";
import { useLang, useT, type Lang, type Translate } from "@/lib/i18n";
import { translate } from "@/lib/i18n/strings";
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

export type ZoneDoc = ZonePanel["planning_documents"][number];

/** One part of a document's meta line; `source` marks the registry name (a link when it has one). */
export interface MetaPart {
  text: string;
  source?: true;
}

/**
 * The meta line of a zone's document: `source PDF · eRegistri · adopted 12 May 2019 · 4 parcels
 * with data`; an adopted document the map does not cover yet says "not yet digitised".
 */
export function documentMetaParts(d: ZoneDoc): MetaPart[] {
  const parts: MetaPart[] = [];
  if (d.file_available) parts.push({ text: "source PDF" });
  if (d.source) parts.push({ text: d.source, source: true });
  const adopted = d.adopted_on ? formatDate(d.adopted_on) : null;
  if (adopted) parts.push({ text: `adopted ${adopted}` });
  if (d.covered) {
    const n = d.parcel_count ?? 0;
    parts.push({ text: `${n} ${n === 1 ? "parcel" : "parcels"} with data` });
  } else if (d.status === "adopted") {
    parts.push({ text: "not yet digitised" });
  }
  return parts;
}

/** The document's registry entry (eRegistri), opened in a new tab. */
export function RegistryLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a className="srclink" href={href} target="_blank" rel="noopener noreferrer" title="Open the registry entry">
      {children}
    </a>
  );
}

/** The meta line with the registry name as a link to the document's registry entry. */
export function DocumentMeta({ doc }: { doc: ZoneDoc }) {
  return documentMetaParts(doc).map((p, i) => (
    <Fragment key={i}>
      {i > 0 && " · "}
      {p.source && doc.registry_url ? <RegistryLink href={doc.registry_url}>{p.text}</RegistryLink> : p.text}
    </Fragment>
  ));
}

const english: Translate = (key, vars) => translate("en", key, vars);

/**
 * Which published data a panel shows: `Data version 9 · published 27 Sep 2026`. `version` is what
 * `useVersionName` made of the label: the published version's number, else the label itself.
 */
export function dataVersionText(version: string, date?: string | null, t: Translate = english, lang: Lang = "en"): string {
  if (version === "unpublished") return t("panel.unpublished");
  const published = date ? formatDate(date, lang) : null;
  return published ? t("panel.dataVersionDated", { version, date: published }) : t("panel.dataVersion", { version });
}

/**
 * A data version as the visitor reads it: the number of the published version (`9`; every publish
 * is a new numbered version) when the label is the map's current one, else the label (an order
 * placed on an earlier version keeps its label).
 */
export function useVersionName(label: string | null | undefined): string | null {
  const { data: tiles } = useTilesCurrent();
  if (!label) return null;
  return tiles?.version_no != null && tiles.data_version === label ? String(tiles.version_no) : label;
}

export function DataVersionLine({ version, date }: { version: string; date?: string | null }) {
  const t = useT();
  const { lang } = useLang();
  const name = useVersionName(version) ?? version;
  return <p className="dataversion">{dataVersionText(name, date, t, lang)}</p>;
}

/** Sticky panel header: ✕ (clears the selection and its map highlight), eyebrow, title, sub-line. */
export function PanelHead({ eyebrow, title, sub }: { eyebrow: ReactNode; title: ReactNode; sub?: ReactNode }) {
  const clearSelection = useShell((s) => s.clearSelection);
  const t = useT();
  return (
    <div className="phead">
      <button type="button" className="pclose" aria-label={t("panel.close")} onClick={clearSelection}>
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
  const t = useT();
  return (
    <div className="pscroll" aria-busy="true">
      <PanelHead eyebrow={eyebrow} title={title ?? NBSP} />
      <div className="sect">
        <p className="panelnote">{t("panel.loading")}</p>
      </div>
    </div>
  );
}

/** The API did not answer: a neutral note and a retry, never an error screen. */
export function PanelUnavailable({ eyebrow, title, onRetry }: { eyebrow: ReactNode; title?: ReactNode; onRetry: () => void }) {
  const t = useT();
  return (
    <div className="pscroll">
      <PanelHead eyebrow={eyebrow} title={title ?? NBSP} />
      <div className="sect">
        <p className="panelnote">{t("panel.unavailable")}</p>
        <Cta variant="line" onClick={onRetry} style={{ marginTop: 12 }}>
          {t("panel.retry")}
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
