"use client";

/**
 * S3 urban block panel (the client's review of 2026-10-12: a block that answered a click with
 * nothing), from `GET /v1/panel?type=block&id=`: header (URBAN BLOCK, the plan's block label,
 * "Urban block of the plan · <zone>"), the planning document(s) whose coverage holds the block (a
 * click opens the document panel), what lies in it (zone, drawn area, urban and cadastral parcel
 * counts) and the four block-level figures the parameters heatmap colours it by (site coverage,
 * FAR, floors, GFA: the current version's heatmap cells, each with the rule that made it as a
 * tooltip; "no data" where the block's parcels state nothing). Texts come from the string table
 * and the API's bilingual labels; no formula lives here.
 */
import { useEffect } from "react";

import { ApiError } from "@/lib/api/client";
import { usePanel } from "@/lib/api/hooks";
import type { BlockPanel as BlockPanelData } from "@/lib/api/types";
import { formatFigure } from "@/lib/format";
import { pickLang, useLang, useT } from "@/lib/i18n";
import { useShell } from "@/lib/store";

import { IconDoc } from "../ui/icons";
import { PanelRow } from "../ui/panel-row";
import { DataVersionLine, DocStatusChip, PanelHead, PanelLoading, PanelUnavailable, usePanelViewed } from "./panel-parts";
import { formatArea } from "./parcel-parts";

type BlockParameter = BlockPanelData["parameters"][number];

function Eyebrow() {
  const t = useT();
  return <span className="tag">{t("block.eyebrow")}</span>;
}

/** A block figure as the row shows it: `55.3`, `2.41`, `7 · P+5+Pk`, `68,792`. */
export function blockFigure(p: BlockParameter): string | null {
  if (p.value == null) return null;
  switch (p.layer) {
    case "far":
      return formatFigure(p.value, 2);
    case "coverage":
      return formatFigure(p.value, 1);
    case "height":
      return p.label ? `${formatFigure(p.value, 0)} · ${p.label}` : formatFigure(p.value, 0);
    default:
      return formatArea(Math.round(p.value));
  }
}

export function BlockPanel({ blockId }: { blockId: number }) {
  const query = usePanel({ type: "block", id: blockId });
  const setSelection = useShell((s) => s.setSelection);
  const clearSelection = useShell((s) => s.clearSelection);
  const t = useT();
  const { lang } = useLang();
  const data = query.data?.type === "block" ? query.data : undefined;
  const gone = query.error instanceof ApiError && query.error.isNotFound;
  usePanelViewed("block", data ? { block_id: blockId, zone_id: data.block.zone?.id ?? undefined } : null);
  useEffect(() => {
    if (gone) clearSelection(); // a block that no longer exists: back to the map quietly
  }, [gone, clearSelection]);

  if (!data) {
    if (gone) return null;
    if (query.isError) return <PanelUnavailable eyebrow={<Eyebrow />} onRetry={() => void query.refetch()} />;
    return <PanelLoading eyebrow={<Eyebrow />} />;
  }

  const zoneId = data.block.zone?.id ?? null;
  const openDocument = (id: number) =>
    setSelection({ kind: "feature", type: "document", id, zoneId, linkedUrbanId: null, via: "click" });
  const sub = [pickLang(lang, data.header.subtitle_en, data.header.subtitle_me), data.block.zone?.name].filter(Boolean).join(" · ");

  return (
    <div className="pscroll">
      <PanelHead eyebrow={<Eyebrow />} title={data.header.title} sub={sub} />

      <div className="sect">
        <div className="secthead">
          <span className="lbl">{t("block.sect.plan")}</span>
        </div>
        {data.documents.length > 0 ? (
          <div className="doclist">
            {data.documents.map((d) => (
              <div
                key={d.id}
                className="docitem"
                onClick={(e) => {
                  if (!(e.target as Element).closest("a, button")) openDocument(d.id);
                }}
              >
                <span className="di">
                  <IconDoc />
                </span>
                <span className="dn">
                  <button type="button" className="dnbtn" onClick={() => openDocument(d.id)}>
                    {d.name}
                  </button>
                  {d.type && <span className="dm">{d.type}</span>}
                </span>
                <DocStatusChip status={d.status} />
              </div>
            ))}
          </div>
        ) : (
          <p className="panelnote">
            {data.coverage_note_en ? pickLang(lang, data.coverage_note_en, data.coverage_note_me) : t("block.noPlan")}
          </p>
        )}
      </div>

      <div className="sect">
        <div className="secthead">
          <span className="lbl">{t("block.sect.counts")}</span>
        </div>
        {data.block.zone && <PanelRow label={t("block.zone")} value={data.block.zone.name} text />}
        {data.block.area_m2 != null && <PanelRow label={t("block.area")} value={formatArea(data.block.area_m2)} unit="m²" />}
        <PanelRow label={t("block.urbanParcels")} value={data.counts.urban_parcels} />
        <PanelRow label={t("block.cadastralParcels")} value={data.counts.cadastral_parcels} />
      </div>

      <div className="sect">
        <div className="secthead">
          <span className="lbl">{t("block.sect.params")}</span>
        </div>
        <p className="panelnote">{t("block.paramsNote")}</p>
        {data.parameters.map((p) => {
          const figure = blockFigure(p);
          const stating =
            p.parcel_count == null
              ? undefined
              : p.parcel_count === 1
                ? t("block.parcelStating")
                : t("block.parcelsStating", { n: p.parcel_count });
          return (
            <PanelRow
              key={p.key}
              label={<span title={pickLang(lang, p.rule_en, p.rule_me)}>{pickLang(lang, p.label_en, p.label_me)}</span>}
              value={
                figure == null ? (
                  <span className="notstated" title={pickLang(lang, p.rule_en, p.rule_me)}>
                    {t("block.noValue")}
                  </span>
                ) : (
                  <span title={stating}>{figure}</span>
                )
              }
              unit={figure != null && p.unit ? p.unit : undefined}
              text={p.layer === "height" && !!p.label}
            />
          );
        })}
      </div>

      <div className="sect">
        <p style={{ fontSize: 12, color: "var(--ink-2)", lineHeight: 1.55 }}>{t("block.hint")}</p>
      </div>
      <DataVersionLine version={data.data_version} date={data.data_version_date} />
    </div>
  );
}
