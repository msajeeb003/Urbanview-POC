"use client";

/**
 * S3 zone panel (wireframe `renderPanelZone`), from `GET /v1/panel?type=zone&id=`:
 * header (ZONE + zone type, name, "Internal city division · ≈ city quarter"), the zone's planning
 * documents (status, source, adoption date when known, "n parcels with data" for a document the
 * map covers — a click opens its document panel — or "not yet digitised"; the registry name links
 * to its eRegistri entry), "General planning information" (the zone's summary, BRD §2.3), the
 * the closing note and the data version. A zone without an adopted plan says so instead of
 * listing nothing.
 */
import { useEffect } from "react";

import { ApiError } from "@/lib/api/client";
import { usePanel } from "@/lib/api/hooks";
import { ZONE_TYPES } from "@/lib/layers";
import { useOpenSource } from "@/lib/source";
import { useShell } from "@/lib/store";

import { IconDoc } from "../ui/icons";
import {
  DataVersionLine,
  DocStatusChip,
  DocumentMeta,
  PanelHead,
  PanelLoading,
  PanelUnavailable,
  usePanelViewed,
  type ZoneDoc,
} from "./panel-parts";

const SUB = "Internal city division · ≈ city quarter";

function zoneTypeName(type: string | null | undefined): string | null {
  return ZONE_TYPES.find((z) => z.key === type)?.name ?? null;
}

function Eyebrow({ type }: { type?: string | null }) {
  const name = zoneTypeName(type);
  return (
    <>
      <span className="tag">ZONE</span>
      {name && ` ${name}`}
    </>
  );
}

/**
 * One document of the zone. Its icon opens the stored PDF in the source viewer (page 1), the
 * registry name in the meta line its eRegistri entry; for a document the map covers, the name (or
 * the row) opens its document panel (and its coverage is highlighted).
 */
function DocItem({ doc, zoneId }: { doc: ZoneDoc; zoneId: number }) {
  const setSelection = useShell((s) => s.setSelection);
  const openSource = useOpenSource();
  const openDocument = () =>
    setSelection({ kind: "feature", type: "document", id: doc.id, zoneId, linkedUrbanId: null, via: "click" });
  const icon = doc.file_available ? (
    <button
      type="button"
      className="di"
      title="Open the source PDF"
      aria-label={`Open the source PDF of ${doc.name}`}
      onClick={() =>
        openSource({ documentId: doc.id, page: 1, hint: { documentName: doc.name, page: 1, registryUrl: doc.registry_url } })
      }
    >
      <IconDoc />
    </button>
  ) : (
    <span className="di">
      <IconDoc />
    </span>
  );
  const meta = (
    <span className="dm">
      <DocumentMeta doc={doc} />
    </span>
  );
  if (!doc.covered) {
    return (
      <div className="docitem static">
        {icon}
        <span className="dn">
          {doc.name}
          {meta}
        </span>
        <DocStatusChip status={doc.status} />
      </div>
    );
  }
  return (
    // the row opens the document too; its links and buttons keep their own action
    <div
      className="docitem"
      onClick={(e) => {
        if (!(e.target as Element).closest("a, button")) openDocument();
      }}
    >
      {icon}
      <span className="dn">
        <button type="button" className="dnbtn" title="Open this planning document" onClick={openDocument}>
          {doc.name}
        </button>
        {meta}
      </span>
      <DocStatusChip status={doc.status} />
    </div>
  );
}

export function ZonePanel({ zoneId, name }: { zoneId: number; name?: string }) {
  const query = usePanel({ type: "zone", id: zoneId });
  const clearSelection = useShell((s) => s.clearSelection);
  const data = query.data?.type === "zone" ? query.data : undefined;
  const gone = query.error instanceof ApiError && query.error.isNotFound;
  usePanelViewed("zone", data ? { zone_id: zoneId } : null);
  useEffect(() => {
    if (gone) clearSelection(); // a zone that no longer exists: back to the map quietly
  }, [gone, clearSelection]);

  if (!data) {
    if (gone) return null;
    if (query.isError) return <PanelUnavailable eyebrow={<Eyebrow />} title={name} onRetry={() => void query.refetch()} />;
    return <PanelLoading eyebrow={<Eyebrow />} title={name} />;
  }

  const docs = data.planning_documents;

  return (
    <div className="pscroll">
      <PanelHead eyebrow={<Eyebrow type={data.zone.zone_type} />} title={data.header.title} sub={SUB} />
      <div className="sect">
        <div className="secthead">
          <span className="lbl">
            Planning documents
          </span>
        </div>
        <p style={{ fontSize: "11.5px", color: "var(--ink-2)", margin: "-4px 0 11px", lineHeight: 1.5 }}>
          In Montenegro a zone isn&apos;t an official bounded area — it&apos;s UrbanView&apos;s own grouping of related
          planning documents, roughly a city quarter. This zone groups {docs.length > 0 ? docs.length : "none yet"}.
        </p>
        <div className="doclist">
          {data.counts.adopted === 0 && (
            <div className="docitem static docnote" role="note">
              <span className="di">⚠</span>
              <span className="dn">
                No adopted plan
                <span className="dm">
                  No adopted planning document covers this zone yet, so there are no building rights to show here.
                </span>
              </span>
            </div>
          )}
          {docs.map((d) => (
            <DocItem key={d.id} doc={d} zoneId={data.zone.id} />
          ))}
        </div>
      </div>
      <div className="sect">
        <div className="secthead">
          <span className="lbl">General planning information</span>
        </div>
        {data.zone.general_planning_summary ? (
          <p style={{ fontSize: "12.5px", color: "var(--ink-2)", margin: "-2px 0 0", lineHeight: 1.55 }}>
            {data.zone.general_planning_summary}
          </p>
        ) : (
          <p className="panelnote">A general planning summary for this zone has not been written yet.</p>
        )}
      </div>
      <div className="sect">
        <p style={{ fontSize: 12, color: "var(--ink-2)", lineHeight: 1.55 }}>
          Click a specific parcel inside this zone to see its full planning parameters, the existing-vs-planned comparison,
          and the market feasibility.
        </p>
      </div>
      <DataVersionLine version={data.data_version} date={data.data_version_date} />
    </div>
  );
}
