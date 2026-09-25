"use client";

/**
 * S3 cadastral parcel panel (wireframe `renderPanelCadastral`), from
 * `GET /v1/panel?type=cadastral&id=`: header (CADASTRAL PARCEL + zone type, "Parcel #1042",
 * "Centar · Podgorica I"), the identification grid, "Corresponding urban parcel" (a card per
 * planned parcel over it, "Open urban parcel →", and the area comparison; "Not defined" when the
 * plan defines none; the coverage note when no adopted plan covers it) and the CTA stack.
 * Cadastral and urban parcels are separate objects: the card moves to the urban parcel's panel.
 */
import { useEffect } from "react";

import { ApiError } from "@/lib/api/client";
import { usePanel } from "@/lib/api/hooks";
import type { CadastralPanel as CadastralPanelData, UrbanLink } from "@/lib/api/types";
import { useSelection } from "@/lib/selection";
import { useShell } from "@/lib/store";

import { IdGrid } from "../ui/id-grid";
import { PanelHead, PanelLoading, PanelUnavailable, usePanelViewed } from "./panel-parts";
import { AreaCompare, ParcelCtas, formatArea, parcelNo, useZoneTypeName } from "./parcel-parts";

function Eyebrow({ typeName }: { typeName?: string | null }) {
  return (
    <>
      <span className="tag">CADASTRAL PARCEL</span>
      {typeName && ` ${typeName}`}
    </>
  );
}

function UrbanCard({
  link,
  primary,
  count,
  onOpen,
}: {
  link: UrbanLink;
  primary: boolean;
  /** Planned parcels on this cadastral parcel (> 1 = a split). */
  count: number;
  onOpen: () => void;
}) {
  return (
    <div
      className="upcard"
      role="button"
      tabIndex={0}
      aria-label={`Open urban parcel ${link.urban_parcel_number}`}
      style={primary ? undefined : { marginTop: 9 }}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
    >
      <div className="upn">{link.urban_parcel_number}</div>
      {primary && count === 1 ? (
        <div className="upd">
          This cadastral parcel corresponds to an urban parcel in the adopted plan. Building rights — land use, height,
          coverage, FAR — are defined on the <b>urban parcel</b>, not on the cadastral one.
        </div>
      ) : primary ? (
        <div className="upd">
          This cadastral parcel is split between {count} urban parcels in the adopted plan. Building rights — land use,
          height, coverage, FAR — are defined on the <b>urban parcels</b>, not on the cadastral one. This one covers{" "}
          <b>{formatArea(link.overlap_m2)} m²</b> of it ({link.share_of_cadastral_pct}%).
        </div>
      ) : (
        <div className="upd">
          Another urban parcel also lies on this cadastral parcel: <b>{formatArea(link.overlap_m2)} m²</b> of it (
          {link.share_of_cadastral_pct}%).
        </div>
      )}
      <div className="upgo">
        Open urban parcel <span>→</span>
      </div>
    </div>
  );
}

function Corresponding({ data }: { data: CadastralPanelData }) {
  const { selectLinkedParcel } = useSelection();
  const cad = data.identification;
  const zoneId = cad.zone?.id ?? null;

  if (!data.covered) {
    return (
      <div className="upcard none">
        <div className="upn">No adopted plan</div>
        <div className="upd">
          {data.coverage_note_en ?? "No adopted planning document covers this parcel yet."}
        </div>
      </div>
    );
  }
  const links = data.urban_parcels ?? [];
  if (links.length === 0) {
    return (
      <div className="upcard none">
        <div className="upn">Not defined</div>
        <div className="upd">
          The adopted plan defines no urban parcel over this cadastral parcel, so building rights cannot be read directly.
          An expert analysis is needed to establish what is possible here.
        </div>
      </div>
    );
  }
  const open = (link: UrbanLink) => selectLinkedParcel({ type: "urban", id: link.id, zoneId });
  return (
    <>
      {links.map((link, i) => (
        <UrbanCard key={link.id} link={link} primary={i === 0} count={links.length} onOpen={() => open(link)} />
      ))}
      {cad.cadastral_area_m2 != null && (
        <div style={{ marginTop: 11 }}>
          <AreaCompare cadastralM2={cad.cadastral_area_m2} urbanM2={links.map((l) => l.area_m2)} label="urban" />
        </div>
      )}
    </>
  );
}

export function CadastralPanel({ parcelId }: { parcelId: number }) {
  const query = usePanel({ type: "cadastral", id: parcelId });
  const clearSelection = useShell((s) => s.clearSelection);
  const data = query.data?.type === "cadastral" ? query.data : undefined;
  const zoneId = data?.identification.zone?.id ?? null;
  const typeName = useZoneTypeName(zoneId);
  const gone = query.error instanceof ApiError && query.error.isNotFound;
  usePanelViewed("cadastral", data ? { parcel_id: parcelId, zone_id: zoneId ?? undefined } : null);
  useEffect(() => {
    if (gone) clearSelection(); // a parcel that no longer exists: back to the map quietly
  }, [gone, clearSelection]);

  if (!data) {
    if (gone) return null;
    if (query.isError) return <PanelUnavailable eyebrow={<Eyebrow />} onRetry={() => void query.refetch()} />;
    return <PanelLoading eyebrow={<Eyebrow />} />;
  }

  const cad = data.identification;
  const no = parcelNo(cad.parcel_number, cad.sub_number);
  const sub = [cad.zone?.name, cad.ko_name].filter(Boolean).join(" · ");

  return (
    <>
      <div className="pscroll">
        <PanelHead eyebrow={<Eyebrow typeName={typeName} />} title={`Parcel #${no}`} sub={sub} />
        <IdGrid
          cells={[
            { key: "number", label: "Parcel number", value: no },
            { key: "ko", label: "Cadastral municipality", value: cad.ko_name },
            { key: "block", label: "Urban block", value: cad.urban_block?.block_ref ?? "—" },
            {
              key: "area",
              label: "Cadastral area",
              value: cad.cadastral_area_m2 != null ? `${formatArea(cad.cadastral_area_m2)} m²` : "—",
            },
            {
              key: "doc",
              label: "Governing document",
              value: cad.governing_document?.name ?? "No adopted plan",
              full: true,
              small: true,
            },
          ]}
        />
        <div className="sect">
          <div className="secthead">
            <span className="lbl">Corresponding urban parcel</span>
          </div>
          <Corresponding data={data} />
        </div>
      </div>
      <ParcelCtas
        question={`Tell me about cadastral parcel #${no}`}
        target={{
          parcelType: "cadastral",
          parcelId,
          parcel: `Parcel #${no}`,
          ko: cad.ko_name,
          basisAreaM2: data.basis_area_m2 ?? null,
          calculationBasis: data.calculation_basis ?? null,
          ids: { parcel_id: parcelId, zone_id: zoneId ?? undefined },
        }}
      />
    </>
  );
}
