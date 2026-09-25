"use client";

/**
 * S3 urban (planned) parcel panel (wireframe `renderPanelUrban`), from
 * `GET /v1/panel?type=urban&id=`: header (URBAN PARCEL +
 * zone type, "UP 12", "Centar · Podgorica I") with the backlink to the cadastral parcel, the
 * identification grid and the Parcel ID line, "Cadastral vs urban parcel" (area comparison and
 * the calculation basis), "Planning parameters" (Free, source chip): the mock's seven rows, then
 * the dictionary's other stated fields, every stated value with its source icon (the cited page,
 * `source_reference_opened`), then Group 2 "Market data & feasibility" (`market-section.tsx`,
 * behind the market-data boundary) and the CTA stack.
 */
import { useEffect, type ReactNode } from "react";

import { ApiError } from "@/lib/api/client";
import { useMunicipality, usePanel } from "@/lib/api/hooks";
import type { PlanningField, UrbanPanel as UrbanPanelData } from "@/lib/api/types";
import { formatFigure } from "@/lib/format";
import { useSelection } from "@/lib/selection";
import { useOpenSource } from "@/lib/source";
import { useShell } from "@/lib/store";

import { Badge } from "../ui/badge";
import { IdGrid } from "../ui/id-grid";
import { PanelRow } from "../ui/panel-row";
import { SourceRef } from "../ui/source-ref";
import { MarketSection } from "./market-section";
import { PanelHead, PanelLoading, PanelUnavailable, usePanelViewed } from "./panel-parts";
import { AreaCompare, AreaNote, ParcelCtas, RowSource, formatArea, parcelNo, useZoneTypeName } from "./parcel-parts";

/** Keys the mock's seven rows show (height and floors share "Max building height"). */
const MOCK_KEYS = new Set([
  "land_use",
  "max_height_m",
  "max_floors",
  "max_site_coverage_pct",
  "max_far",
  "planned_parcel_area_m2",
  "max_gfa_m2",
  "max_coverage_area_m2",
]);

function Eyebrow({ typeName }: { typeName?: string | null }) {
  return (
    <>
      <span className="tag">URBAN PARCEL</span>
      {typeName && ` ${typeName}`}
    </>
  );
}

/** Why a value is missing, for the row's tooltip. */
function missingTitle(f: PlanningField | undefined, fallback: string): string {
  return f?.reason_en ?? (f?.status === "not_stated" ? "not stated in plan" : fallback);
}

/** A stored number or text as the row shows it, with its unit. */
function display(f: PlanningField | undefined): { value: ReactNode; unit?: string; text: boolean; title?: string } {
  if (!f || f.value == null || f.status === "not_stated" || f.status === "cannot_compute") {
    return { value: "—", text: false, title: missingTitle(f, "not stated in plan") };
  }
  if (typeof f.value === "string") return { value: f.value, text: true };
  const unit = f.unit ?? undefined;
  const value = unit === "m²" ? formatArea(f.value) : formatFigure(f.value, 2);
  return { value, unit, text: false };
}

function FieldRow({
  label,
  field,
  accent = false,
  extraTitle,
}: {
  label: ReactNode;
  field: PlanningField | undefined;
  accent?: boolean;
  extraTitle?: string;
}) {
  const d = display(field);
  const computed = field?.status === "computed";
  const title = computed ? `Calculated: ${field?.formula ?? ""}`.trim() : (d.title ?? extraTitle);
  return (
    <PanelRow
      label={label}
      value={<span title={title}>{d.value}</span>}
      unit={d.unit}
      text={d.text}
      accent={accent}
      after={<RowSource fields={[field]} />}
    />
  );
}

function PlanningRows({ data }: { data: UrbanPanelData }) {
  const { data: profile } = useMunicipality();
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

  // Planned parcel area: as the plan states it, else measured from the plan's geometry
  const planned = byKey.get("planned_parcel_area_m2");
  const plannedStated = planned?.status === "stated";
  const geometryArea = data.areas.urban_parcel_area_m2;

  const others = fields.filter((f) => !MOCK_KEYS.has(f.key));

  return (
    <>
      <FieldRow label="Land use designation" field={byKey.get("land_use")} />
      <PanelRow
        label="Max building height"
        value={
          <span title={heightParts.length ? undefined : missingTitle(height, "not stated in plan")}>
            {heightParts.length ? heightParts.join(" · ") : "—"}
          </span>
        }
        after={<RowSource fields={[height, floors]} />}
      />
      <FieldRow label={`Max site coverage (${iz})`} field={byKey.get("max_site_coverage_pct")} />
      <FieldRow label={`Floor Area Ratio (${ii})`} field={byKey.get("max_far")} />
      {plannedStated || geometryArea == null ? (
        <FieldRow label="Planned parcel area" field={planned} />
      ) : (
        <PanelRow
          label="Planned parcel area"
          value={<span title="Measured from the plan's geometry; the plan's text does not state it">{formatArea(geometryArea)}</span>}
          unit="m²"
        />
      )}
      <FieldRow label="Max Gross Floor Area" field={byKey.get("max_gfa_m2")} accent />
      <FieldRow label="Max coverage area" field={byKey.get("max_coverage_area_m2")} />
      {others.map((f) => (
        <FieldRow key={f.key} label={f.label_en} field={f} />
      ))}
    </>
  );
}

export function UrbanPanel({ urbanParcelId }: { urbanParcelId: number }) {
  const query = usePanel({ type: "urban", id: urbanParcelId });
  const clearSelection = useShell((s) => s.clearSelection);
  const { selectLinkedParcel } = useSelection();
  const openSource = useOpenSource();
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

  // the section's source chip opens the first page the stated values cite
  const stated = (data.planning?.fields ?? []).filter((f) => f.status === "stated" && f.source);
  const firstCited = stated
    .map((f) => f.source!)
    .sort((a, b) => (a.page ?? Infinity) - (b.page ?? Infinity))[0];

  const basisLine = basisIsUrban ? (
    <>
      <br />
      <span style={{ color: "var(--brand-dark)", fontWeight: 600 }}>All calculations use the urban parcel area.</span>
    </>
  ) : (
    <>
      <br />
      <span style={{ color: "var(--brand-dark)", fontWeight: 600 }}>All calculations use the cadastral parcel area</span>{" "}
      ({areas.basis_reason_en}).
    </>
  );

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
            ← cadastral parcel #{cadNo}
          </div>
        )}
        <IdGrid
          cells={[
            { key: "up", label: "Urban parcel", value: idn.urban_parcel_number },
            {
              key: "cad",
              label: "Cadastral parcel",
              value: linked.length > 1 ? linked.map((c) => parcelNo(c.parcel_number, c.sub_number)).join(", ") : (cadNo ?? "—"),
            },
            { key: "ko", label: "Cadastral municipality", value: cad?.ko_name ?? "—" },
            { key: "block", label: "Urban block", value: idn.urban_block?.block_ref ?? "—" },
            {
              key: "doc",
              label: "Governing document",
              value: idn.governing_document?.name ?? "—",
              full: true,
              small: true,
            },
          ]}
        />
        <div className="parcelid">
          Parcel ID {cad ? cad.parcel_id : "—"} · urban parcel ID {idn.urban_parcel_id}
        </div>

        <div className="sect">
          <div className="secthead">
            <span className="lbl">Cadastral vs urban parcel</span>
          </div>
          {areas.cadastral_area_m2 != null && areas.urban_parcel_area_m2 != null ? (
            <AreaCompare cadastralM2={areas.cadastral_area_m2} urbanM2={[areas.urban_parcel_area_m2]} label="planned">
              {linked.length > 1 && (
                <>
                  {" "}
                  It also lies on cadastral parcel{linked.length > 2 ? "s" : ""}{" "}
                  {linked
                    .slice(1)
                    .map((c) => `#${parcelNo(c.parcel_number, c.sub_number)}`)
                    .join(", ")}
                  .
                </>
              )}
              {basisLine}
            </AreaCompare>
          ) : (
            <AreaNote label="planned">
              {areas.urban_parcel_area_m2 != null && (
                <>
                  Urban <b>{formatArea(areas.urban_parcel_area_m2)} m²</b>.{" "}
                </>
              )}
              {areas.basis_reason_en.charAt(0).toUpperCase() + areas.basis_reason_en.slice(1)}.{basisLine}
            </AreaNote>
          )}
        </div>

        <div className="sect">
          <div className="secthead">
            <span className="lbl">
              Planning parameters <Badge tone="free" />
            </span>
            <SourceRef
              title={firstCited ? `${firstCited.document_name}${firstCited.page ? `, page ${firstCited.page}` : ""}` : undefined}
              onClick={
                firstCited
                  ? () => void openSource({ documentId: firstCited.document_id, page: firstCited.page ?? 1 })
                  : undefined
              }
            />
          </div>
          {data.planning ? (
            <PlanningRows data={data} />
          ) : (
            <p className="panelnote">{data.coverage_note_en ?? "No adopted planning document covers this parcel."}</p>
          )}
        </div>

        <MarketSection data={data} ids={eventIds} />
      </div>
      <ParcelCtas
        question={`Tell me about urban parcel ${idn.urban_parcel_number}`}
        target={{
          parcelType: "urban",
          parcelId: urbanParcelId,
          // the mock names the cadastral parcel here too ("Parcel #2001/2 · Podgorica III")
          parcel: cadNo ? `Parcel #${cadNo}` : idn.urban_parcel_number,
          ko: cad?.ko_name ?? null,
          basisAreaM2: data.basis_area_m2 ?? null,
          calculationBasis: data.calculation_basis ?? null,
          ids: eventIds,
        }}
      />
    </>
  );
}
