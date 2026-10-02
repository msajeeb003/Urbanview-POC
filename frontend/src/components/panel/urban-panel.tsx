"use client";

/**
 * S3 urban (planned) parcel panel (wireframe `renderPanelUrban`), from
 * `GET /v1/panel?type=urban&id=`: header (URBAN PARCEL +
 * zone type, "UP 12", "Centar · Podgorica I") with the backlink to the cadastral parcel, the
 * identification grid and the Parcel ID line (UrbanView's id of the cadastral parcel; internal
 * ids of the planned parcel are never shown), "Cadastral vs urban parcel" (one urban area: the
 * one the calculations use, with the drawn shape's area beside it when the two differ),
 * "Planning parameters" (source chip): the mock's seven rows, every stated value with its source
 * icon (the cited page, `source_reference_opened`), a missing one as "Not stated", the data
 * version line, then Group 2 "Market data & feasibility" (`market-section.tsx`) and the button
 * stack. Texts come from the string table and follow the language switch; the API's bilingual
 * texts pick their side.
 */
import { useEffect, type ReactNode } from "react";

import { ApiError } from "@/lib/api/client";
import { useMunicipality, usePanel } from "@/lib/api/hooks";
import type { PlanningField, UrbanPanel as UrbanPanelData } from "@/lib/api/types";
import { formatFigure } from "@/lib/format";
import { fillNodes, pickLang, useLang, useT, type Lang, type Translate } from "@/lib/i18n";
import { useSelection } from "@/lib/selection";
import { useOpenSource } from "@/lib/source";
import { useShell } from "@/lib/store";

import { IdGrid } from "../ui/id-grid";
import { PanelRow } from "../ui/panel-row";
import { SourceRef } from "../ui/source-ref";
import { MarketSection } from "./market-section";
import { DataVersionLine, PanelHead, PanelLoading, PanelUnavailable, usePanelViewed } from "./panel-parts";
import {
  AreaCompare,
  AreaFigure,
  AreaNote,
  BasisLine,
  ParcelCtas,
  RowSource,
  formatArea,
  parcelNo,
  useZoneTypeName,
} from "./parcel-parts";

function Eyebrow({ typeName }: { typeName?: string | null }) {
  const t = useT();
  return (
    <>
      <span className="tag">{t("urban.eyebrow")}</span>
      {typeName && ` ${typeName}`}
    </>
  );
}

/** Why a value is missing, for the row's tooltip. */
function missingTitle(f: PlanningField | undefined, t: Translate, lang: Lang): string {
  return (f?.reason_en ? pickLang(lang, f.reason_en, f.reason_me) : null) ?? t("row.notStatedTitle");
}

/** A value the plan does not give: the words "Not stated" (never a dash, never a guess). */
function NotStated({ title }: { title: string }) {
  const t = useT();
  return (
    <span className="notstated" title={title}>
      {t("row.notStated")}
    </span>
  );
}

/** A stored number or text as the row shows it, with its unit; null when the plan does not state it. */
function display(f: PlanningField | undefined): { value: ReactNode; unit?: string; text: boolean } | null {
  if (!f || f.value == null || f.status === "not_stated" || f.status === "cannot_compute") return null;
  if (typeof f.value === "string") return { value: f.value, text: true };
  const unit = f.unit ?? undefined;
  const value = unit === "m²" ? formatArea(f.value) : formatFigure(f.value, 2);
  return { value, unit, text: false };
}

function FieldRow({ label, field, accent = false }: { label: ReactNode; field: PlanningField | undefined; accent?: boolean }) {
  const t = useT();
  const { lang } = useLang();
  const d = display(field);
  const computed = field?.status === "computed";
  return (
    <PanelRow
      label={label}
      value={
        d ? (
          <span title={computed ? t("row.calculated", { formula: field?.formula ?? "" }).trim() : undefined}>{d.value}</span>
        ) : (
          <NotStated title={missingTitle(field, t, lang)} />
        )
      }
      unit={d?.unit}
      text={d?.text ?? false}
      accent={accent && !!d}
      after={<RowSource fields={[field]} />}
    />
  );
}

/**
 * Land use as the plan prints it. A legend code (`SS`) is followed by the name the plan's legend
 * gives it (`SS · stanovanje srednje gustine`), so a code never stands alone when its meaning is
 * known.
 */
function landUseText(f: PlanningField | undefined): PlanningField | undefined {
  if (!f || typeof f.value !== "string" || !f.value_name) return f;
  return { ...f, value: `${f.value} · ${f.value_name}` };
}

function PlanningRows({ data }: { data: UrbanPanelData }) {
  const { data: profile } = useMunicipality();
  const t = useT();
  const { lang } = useLang();
  const fields = data.planning?.fields ?? [];
  const byKey = new Map(fields.map((f) => [f.key, f]));
  const iz = profile?.terminology.site_coverage.abbreviation ?? "IZ";
  const ii = profile?.terminology.far.abbreviation ?? "II";

  // Max building height: metres and floors (the plan states either or both), each with its source
  const height = byKey.get("max_height_m");
  const floors = byKey.get("max_floors");
  const heightParts = [
    height?.status === "stated" && typeof height.value === "number" ? `${formatFigure(height.value, 1)} m` : null,
    floors?.status === "stated" && floors.value != null ? String(floors.value) : null,
  ].filter(Boolean);
  // A parcel of several buildings states the floors per building ("(a) Po+P+3, (b) Pv, (c) P+1"):
  // that wraps like any text value; the mock's short "27.5 m · P+8" keeps its look.
  const heightText = heightParts.join(" · ");
  const heightWraps = heightText.length > 24;

  // Planned parcel area: as the plan states it, else measured from the plan's geometry
  const planned = byKey.get("planned_parcel_area_m2");
  const plannedStated = planned?.status === "stated";
  const geometryArea = data.areas.urban_parcel_area_m2;

  return (
    <>
      <FieldRow label={t("row.landUse")} field={landUseText(byKey.get("land_use"))} />
      <PanelRow
        label={t("row.height")}
        value={heightParts.length ? <span>{heightText}</span> : <NotStated title={missingTitle(height, t, lang)} />}
        text={heightWraps}
        after={<RowSource fields={[height, floors]} />}
      />
      <FieldRow label={t("row.coverage", { abbr: iz })} field={byKey.get("max_site_coverage_pct")} />
      <FieldRow label={t("row.far", { abbr: ii })} field={byKey.get("max_far")} />
      {plannedStated || geometryArea == null ? (
        <FieldRow label={t("row.plannedArea")} field={planned} />
      ) : (
        <PanelRow
          label={t("row.plannedArea")}
          value={<span title={t("row.fromGeometry")}>{formatArea(geometryArea)}</span>}
          unit="m²"
          after={<RowSource fields={[]} />}
        />
      )}
      <FieldRow label={t("row.gfa")} field={byKey.get("max_gfa_m2")} accent />
      <FieldRow label={t("row.coverageArea")} field={byKey.get("max_coverage_area_m2")} />
    </>
  );
}

const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

export function UrbanPanel({ urbanParcelId }: { urbanParcelId: number }) {
  const query = usePanel({ type: "urban", id: urbanParcelId });
  const clearSelection = useShell((s) => s.clearSelection);
  const { selectLinkedParcel } = useSelection();
  const openSource = useOpenSource();
  const t = useT();
  const { lang } = useLang();
  const data = query.data?.type === "urban" ? query.data : undefined;
  const zoneId = data?.identification.zone?.id ?? null;
  const typeName = useZoneTypeName(zoneId);
  const gone = query.error instanceof ApiError && query.error.isNotFound;
  const cadId = data?.identification.cadastral_parcel?.parcel_id;
  usePanelViewed(
    "urban",
    data ? { urban_parcel_id: urbanParcelId, parcel_id: cadId ?? undefined, zone_id: zoneId ?? undefined } : null,
  );
  useEffect(() => {
    if (gone) clearSelection(); // a planned parcel that no longer exists: back to the map quietly
  }, [gone, clearSelection]);

  if (!data) {
    if (gone) return null;
    if (query.isError) return <PanelUnavailable eyebrow={<Eyebrow />} onRetry={() => void query.refetch()} />;
    return <PanelLoading eyebrow={<Eyebrow />} />;
  }

  const idn = data.identification;
  const cad = idn.cadastral_parcel;
  const eventIds = { urban_parcel_id: urbanParcelId, parcel_id: cad?.parcel_id ?? undefined, zone_id: zoneId ?? undefined };
  const linked = idn.linked_cadastral_parcels ?? [];
  const cadNo = cad ? parcelNo(cad.parcel_number, cad.sub_number) : null;
  const sub = [idn.zone?.name, cad?.ko_name].filter(Boolean).join(" · ");
  const areas = data.areas;
  const basisIsUrban = data.calculation_basis === "urban";
  const basisReason = pickLang(lang, areas.basis_reason_en, areas.basis_reason_me);

  // The section's source chip opens the first cited value (lowest page, then the panel's order)
  // by its value id, so the page opens with that value framed and named, exactly as a row's icon
  // does; a chip that opened the bare page showed no highlight at all.
  const stated = (data.planning?.fields ?? []).filter((f) => f.status === "stated" && f.source);
  const firstField = [...stated].sort((a, b) => (a.source!.page ?? Infinity) - (b.source!.page ?? Infinity))[0];
  const firstCited = firstField?.source;
  const firstHint = firstCited && {
    label: pickLang(lang, firstField.label_en, firstField.label_me),
    documentName: firstCited.document_name,
    page: firstCited.page ?? 1,
    note: firstCited.note,
    registryUrl: firstCited.registry_url,
  };

  // One urban area in the panel: the one the calculations use (the plan's own figure, else the
  // drawn parcel's). The shape drawn on the map is named beside it when the two differ (a drawing
  // can close a wrong shape; a mismatch is always surfaced, never two areas that both claim the
  // calculations).
  const drawnArea = areas.urban_parcel_area_m2;
  const statedArea = areas.planned_area_stated_m2;
  const urbanArea = basisIsUrban ? (data.basis_area_m2 ?? drawnArea) : drawnArea;
  const statedOff =
    basisIsUrban && statedArea != null && drawnArea != null && Math.abs(areas.stated_vs_geometry_delta_pct ?? 0) >= 2;
  const drawnNote = statedOff && drawnArea != null && (
    <> {fillNodes(t("cmp.drawnArea"), { area: <AreaFigure m2={drawnArea} /> })}</>
  );
  const basisLine = basisIsUrban ? (
    <>
      {drawnNote}
      <BasisLine>{statedOff ? t("basis.planStated") : t("basis.urban")}</BasisLine>
    </>
  ) : (
    <>
      <BasisLine>{t("basis.cadastral")}</BasisLine> ({basisReason}).
    </>
  );
  const also = linked
    .slice(1)
    .map((c) => `#${parcelNo(c.parcel_number, c.sub_number)}`)
    .join(", ");

  return (
    <>
      <div className="pscroll">
        <PanelHead eyebrow={<Eyebrow typeName={typeName} />} title={idn.urban_parcel_number} sub={sub || undefined} />
        {cad && cadNo && (
          <div
            className="backlink"
            role="button"
            tabIndex={0}
            onClick={() =>
              selectLinkedParcel({ type: "cadastral", id: cad.parcel_id, zoneId, linkedUrbanId: urbanParcelId })
            }
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                selectLinkedParcel({ type: "cadastral", id: cad.parcel_id, zoneId, linkedUrbanId: urbanParcelId });
              }
            }}
          >
            {t("urban.backlink", { no: cadNo })}
          </div>
        )}
        <IdGrid
          cells={[
            { key: "up", label: t("id.urbanParcel"), value: idn.urban_parcel_number },
            {
              key: "cad",
              label: t("id.cadastralParcel"),
              value: linked.length > 1 ? linked.map((c) => parcelNo(c.parcel_number, c.sub_number)).join(", ") : (cadNo ?? "—"),
            },
            { key: "ko", label: t("id.ko"), value: cad?.ko_name ?? "—" },
            { key: "block", label: t("id.block"), value: idn.urban_block?.block_ref ?? "—" },
            {
              key: "doc",
              label: t("id.document"),
              value: idn.governing_document?.name ?? "—",
              full: true,
              small: true,
            },
          ]}
        />
        {cad && <div className="parcelid">{t("id.parcelId", { id: cad.parcel_id })}</div>}

        <div className="sect">
          <div className="secthead">
            <span className="lbl">{t("sect.compare")}</span>
          </div>
          {areas.cadastral_area_m2 != null && urbanArea != null ? (
            <AreaCompare cadastralM2={areas.cadastral_area_m2} urbanM2={[urbanArea]} label={t("cmp.miniPlanned")}>
              {linked.length > 1 && <> {t(linked.length > 2 ? "cmp.alsoOnMany" : "cmp.alsoOnOne", { list: also })}</>}
              {basisLine}
            </AreaCompare>
          ) : (
            <AreaNote label={t("cmp.miniPlanned")}>
              {areas.basis_reason_code === "no_cadastral_parcel" ? t("cmp.noCadastral") : `${capital(basisReason)}.`}
              {urbanArea != null && (
                <>
                  {" "}
                  {fillNodes(t(statedOff ? "cmp.planArea" : "cmp.urbanArea"), { area: <AreaFigure m2={urbanArea} /> })}
                </>
              )}
              {basisLine}
            </AreaNote>
          )}
        </div>

        <div className="sect">
          <div className="secthead">
            <span className="lbl">{t("sect.planning")}</span>
            <SourceRef
              label={t("panel.source")}
              title={
                firstCited
                  ? firstCited.page
                    ? t("panel.sourcePage", { name: firstCited.document_name, page: firstCited.page })
                    : firstCited.document_name
                  : t("panel.sourceTitle")
              }
              onClick={
                firstCited && firstHint
                  ? () =>
                      void openSource(
                        firstCited.value_id != null
                          ? { valueId: firstCited.value_id, hint: firstHint }
                          : { documentId: firstCited.document_id, page: firstCited.page ?? 1, hint: firstHint },
                      )
                  : undefined
              }
            />
          </div>
          {data.planning ? (
            <PlanningRows data={data} />
          ) : (
            <p className="panelnote">
              {data.coverage_note_en ? pickLang(lang, data.coverage_note_en, data.coverage_note_me) : t("panel.noPlanNote")}
            </p>
          )}
        </div>
        <DataVersionLine version={data.data_version} date={data.data_version_date} />

        <MarketSection data={data} ids={eventIds} />
      </div>
      <ParcelCtas
        marketIntent
        target={{
          parcelType: "urban",
          parcelId: urbanParcelId,
          // the mock names the cadastral parcel here too ("Parcel #2001/2 · Podgorica III")
          parcel: cadNo ? `Parcel #${cadNo}` : idn.urban_parcel_number,
          ko: cad?.ko_name ?? null,
          plannedParcel: idn.urban_parcel_number,
          dataVersion: data.data_version ?? null,
          basisAreaM2: data.basis_area_m2 ?? null,
          calculationBasis: data.calculation_basis ?? null,
          ids: eventIds,
        }}
      />
    </>
  );
}
