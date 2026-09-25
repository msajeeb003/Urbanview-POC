"use client";

/**
 * S3 planning-document panel (wireframe `renderPanelDoc`: clicking a coverage area), from
 * `GET /v1/panel?type=document&id=`: header (PLANNING DOCUMENT + status, name, "DUP — Detailed
 * urban plan" from the profile), document details with the source chip (the PDF's first page, or
 * the registry entry), the general planning information of its zone, coverage counts and one row
 * per zone spanned (typical height · FAR), and the CTA stack: "Ask about this document" (the
 * assistant with the question typed in, `ai_interest`) and "How we read a planning document"
 * (the methodology, step 2).
 */
import { useEffect } from "react";

import { useTrack } from "@/lib/analytics/react";
import { ApiError } from "@/lib/api/client";
import { useMunicipality, usePanel } from "@/lib/api/hooks";
import type { DocumentPanel as DocumentPanelData } from "@/lib/api/types";
import { formatDate } from "@/lib/format";
import { useOpenSource } from "@/lib/source";
import { useShell } from "@/lib/store";

import { METHODOLOGY_LABEL, MethodologyModal } from "../shell/methodology-modal";
import { Badge } from "../ui/badge";
import { Cta } from "../ui/cta";
import { IconAsk, IconSteps } from "../ui/icons";
import { PanelRow } from "../ui/panel-row";
import { SourceRef } from "../ui/source-ref";
import {
  DocStatusChip,
  PanelHead,
  PanelLoading,
  PanelUnavailable,
  docTypeLabel,
  planPhrase,
  usePanelViewed,
  zoneTypicalLine,
} from "./panel-parts";

/** The mock's text when the zone has no summary of its own. */
const GENERIC_SUMMARY =
  "This adopted plan governs building rights across its coverage area. Per-parcel parameters — land use, height, coverage, FAR — are defined on the urban parcels inside it; click any parcel to read them.";

function Eyebrow({ status }: { status?: DocumentPanelData["document"]["status"] }) {
  return (
    <>
      <span className="tag">PLANNING DOCUMENT</span>
      {status && ` ${planPhrase(status)}`}
    </>
  );
}

export function DocumentPanel({ documentId }: { documentId: number }) {
  const query = usePanel({ type: "document", id: documentId });
  const { data: profile } = useMunicipality();
  const clearSelection = useShell((s) => s.clearSelection);
  const openAiWith = useShell((s) => s.openAiWith);
  const openModal = useShell((s) => s.openModal);
  const openSource = useOpenSource();
  const track = useTrack();
  const data = query.data?.type === "document" ? query.data : undefined;
  const gone = query.error instanceof ApiError && query.error.isNotFound;
  usePanelViewed("document", data ? { document_id: documentId } : null);
  useEffect(() => {
    if (gone) clearSelection(); // a document that no longer exists: back to the map quietly
  }, [gone, clearSelection]);

  if (!data) {
    if (gone) return null;
    if (query.isError) return <PanelUnavailable eyebrow={<Eyebrow />} onRetry={() => void query.refetch()} />;
    return <PanelLoading eyebrow={<Eyebrow />} />;
  }

  const doc = data.document;
  const typeLabel = docTypeLabel(doc.type, profile);
  const adopted = doc.adopted_on ? formatDate(doc.adopted_on) : null;
  const sourceText = [doc.file_available ? "PDF" : null, doc.source].filter(Boolean).join(" · ") || "—";
  const registryUrl = doc.registry_url;
  const openDocument = doc.file_available
    ? () => void openSource({ documentId: doc.id, page: 1 })
    : registryUrl
      ? () => window.open(registryUrl, "_blank", "noopener")
      : undefined;

  return (
    <>
      <div className="pscroll">
        <PanelHead eyebrow={<Eyebrow status={doc.status} />} title={doc.name} sub={typeLabel || undefined} />

        <div className="sect">
          <div className="secthead">
            <span className="lbl">
              Document details <Badge tone="free" />
            </span>
            <SourceRef
              onClick={openDocument}
              title={doc.file_available ? "Open the source PDF" : "Traceable to source document"}
            />
          </div>
          <PanelRow label="Document name" value={doc.name} text />
          <PanelRow label="Type" value={doc.type ?? "—"} text />
          <PanelRow label="Status" value={<DocStatusChip status={doc.status} />} />
          {adopted && <PanelRow label="Adopted" value={adopted} text />}
          <PanelRow label="Source" value={sourceText} text />
          {data.amendments_in_progress.length > 0 && (
            <div className="prow" style={{ flexDirection: "column", alignItems: "stretch", gap: 5 }}>
              <span className="pk">Amendments</span>
              {data.amendments_in_progress.map((a) => (
                <span key={a.id} style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ fontFamily: "var(--mono)", fontSize: "11.5px", color: "var(--ink)" }}>{a.name}</span>
                  <DocStatusChip status={a.status} />
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="sect">
          <div className="secthead">
            <span className="lbl">General planning information</span>
          </div>
          <p style={{ fontSize: "12.5px", color: "var(--ink-2)", margin: "-2px 0 0", lineHeight: 1.55 }}>
            {data.general_planning_summary ?? GENERIC_SUMMARY}
          </p>
        </div>

        <div className="sect">
          <div className="secthead">
            <span className="lbl">Coverage</span>
          </div>
          <PanelRow label="Urban zones spanned" value={data.zones.length} />
          <PanelRow label="Cadastral parcels" value={data.coverage_counts.cadastral_parcels} />
          <PanelRow label="Urban parcels" value={data.coverage_counts.urban_parcels} />
          {data.zones.length > 0 && (
            <div style={{ marginTop: 9, display: "flex", flexDirection: "column", gap: 5 }}>
              {data.zones.map((z) => (
                <PanelRow
                  key={z.id}
                  style={{ padding: "5px 0" }}
                  label={<span style={{ fontSize: "12.5px" }}>{z.name}</span>}
                  value={zoneTypicalLine(z)}
                  text
                />
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="ctastack">
        <Cta
          variant="ghost"
          icon={<IconAsk />}
          onClick={() => {
            track("ai_interest", { trigger: "document_panel", document_id: doc.id });
            openAiWith(`What does ${doc.name} allow?`);
          }}
        >
          Ask about this document
        </Cta>
        <Cta
          variant="line"
          icon={<IconSteps />}
          onClick={() =>
            openModal({ label: METHODOLOGY_LABEL, wide: true, content: <MethodologyModal initialStep={1} /> })
          }
        >
          How we read a planning document
        </Cta>
      </div>
    </>
  );
}
