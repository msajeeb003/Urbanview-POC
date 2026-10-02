"use client";

/**
 * S3 cadastral parcel panel (wireframe `renderPanelCadastral`), from
 * `GET /v1/panel?type=cadastral&id=`: header (CADASTRAL PARCEL + zone type, "Parcel #1042",
 * "Centar · Podgorica I"), the identification grid, "Corresponding urban parcel" (a card per
 * planned parcel over it with how much of the parcel it covers, "Open urban parcel →", and the
 * area comparison with the area the calculations use; "Not defined" when the plan defines none;
 * the coverage note when no adopted plan covers it), the data version and the CTA stack.
 * Cadastral and urban parcels are separate objects: the card moves to the urban parcel's panel.
 */
import { useEffect } from "react";

import { ApiError } from "@/lib/api/client";
import { usePanel } from "@/lib/api/hooks";
import type { CadastralPanel as CadastralPanelData, UrbanLink } from "@/lib/api/types";
import { fillNodes, pickLang, useLang, useT } from "@/lib/i18n";
import { useSelection } from "@/lib/selection";
import { useShell } from "@/lib/store";

import { IdGrid } from "../ui/id-grid";
import { DataVersionLine, PanelHead, PanelLoading, PanelUnavailable, usePanelViewed } from "./panel-parts";
import { AreaCompare, AreaFigure, BasisLine, ParcelCtas, formatArea, parcelNo, useZoneTypeName } from "./parcel-parts";

function Eyebrow({ typeName }: { typeName?: string | null }) {
  const t = useT();
  return (
    <>
      <span className="tag">{t("cad.eyebrow")}</span>
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
  const t = useT();
  const figures = { area: <AreaFigure m2={link.overlap_m2} />, pct: link.share_of_cadastral_pct, count };
  return (
    <div
      className="upcard"
      role="button"
      tabIndex={0}
      aria-label={t("up.openAria", { up: link.urban_parcel_number })}
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
      <div className="upd">
        {primary && count === 1
          ? fillNodes(t("up.single"), { ...figures, urban: <b>{t("up.singleUrban")}</b> })
          : primary
            ? fillNodes(t("up.split"), { ...figures, urban: <b>{t("up.splitUrban")}</b> })
            : fillNodes(t("up.other"), figures)}
      </div>
      <div className="upgo">
        {t("up.open")} <span>→</span>
      </div>
    </div>
  );
}

function Corresponding({ data }: { data: CadastralPanelData }) {
  const { selectLinkedParcel } = useSelection();
  const t = useT();
  const { lang } = useLang();
  const cad = data.identification;
  const zoneId = cad.zone?.id ?? null;

  if (!data.covered) {
    return (
      <div className="upcard none">
        <div className="upn">{t("id.noPlan")}</div>
        <div className="upd">
          {data.coverage_note_en ? pickLang(lang, data.coverage_note_en, data.coverage_note_me) : t("cad.noPlanBody")}
        </div>
      </div>
    );
  }
  const links = data.urban_parcels ?? [];
  if (links.length === 0) {
    return (
      <div className="upcard none">
        <div className="upn">{t("cad.notDefined")}</div>
        <div className="upd">{t("cad.notDefinedBody")}</div>
      </div>
    );
  }
  const open = (link: UrbanLink) => selectLinkedParcel({ type: "urban", id: link.id, zoneId });
  // the planned parcel's area is the basis; a split uses the one covering the largest share (rank 1)
  const basis =
    data.calculation_basis !== "urban" ? (
      <>
        <BasisLine>{t("basis.cadastral")}</BasisLine> ({pickLang(lang, data.areas.basis_reason_en, data.areas.basis_reason_me)}).
      </>
    ) : links.length > 1 ? (
      <BasisLine>{t("basis.split", { up: (data.urban_parcel ?? links[0]).urban_parcel_number })}</BasisLine>
    ) : (
      <BasisLine>{t("basis.urban")}</BasisLine>
    );
  return (
    <>
      {links.map((link, i) => (
        <UrbanCard key={link.id} link={link} primary={i === 0} count={links.length} onOpen={() => open(link)} />
      ))}
      {cad.cadastral_area_m2 != null && (
        <div style={{ marginTop: 11 }}>
          <AreaCompare
            cadastralM2={cad.cadastral_area_m2}
            // one planned parcel: its area is the basis (the area its plan states, else the drawn one)
            urbanM2={
              links.length === 1 && data.calculation_basis === "urban" && data.basis_area_m2 != null
                ? [data.basis_area_m2]
                : links.map((l) => l.area_m2)
            }
            label={t("cmp.miniUrban")}
          >
            {basis}
          </AreaCompare>
        </div>
      )}
    </>
  );
}

export function CadastralPanel({ parcelId }: { parcelId: number }) {
  const query = usePanel({ type: "cadastral", id: parcelId });
  const clearSelection = useShell((s) => s.clearSelection);
  const t = useT();
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
        <PanelHead eyebrow={<Eyebrow typeName={typeName} />} title={t("cad.title", { no })} sub={sub} />
        <IdGrid
          cells={[
            { key: "number", label: t("id.parcelNumber"), value: no },
            { key: "ko", label: t("id.ko"), value: cad.ko_name },
            { key: "block", label: t("id.block"), value: cad.urban_block?.block_ref ?? "—" },
            {
              key: "area",
              label: t("id.cadastralArea"),
              value: cad.cadastral_area_m2 != null ? `${formatArea(cad.cadastral_area_m2)} m²` : "—",
            },
            {
              key: "doc",
              label: t("id.document"),
              value: cad.governing_document?.name ?? t("id.noPlan"),
              full: true,
              small: true,
            },
          ]}
        />
        <div className="sect">
          <div className="secthead">
            <span className="lbl">{t("sect.corresponding")}</span>
          </div>
          <Corresponding data={data} />
        </div>
        <DataVersionLine version={data.data_version} date={data.data_version_date} />
      </div>
      <ParcelCtas
        target={{
          parcelType: "cadastral",
          parcelId,
          parcel: `Parcel #${no}`,
          ko: cad.ko_name,
          plannedParcel: data.urban_parcel?.urban_parcel_number ?? null,
          dataVersion: data.data_version ?? null,
          basisAreaM2: data.basis_area_m2 ?? null,
          calculationBasis: data.calculation_basis ?? null,
          ids: { parcel_id: parcelId, zone_id: zoneId ?? undefined },
        }}
      />
    </>
  );
}
