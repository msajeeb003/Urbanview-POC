"use client";

/**
 * Right information panel (wireframe `.panel`, 392 px, scrolling body under a sticky header). On
 * desktop it sits beside the map; at ≤ 860 px it becomes a bottom sheet (peek → half → full, see
 * `overrides.css`) with a grab handle. The map is never replaced: `panelHidden` (outside
 * coverage) only removes the panel.
 *
 * What it shows follows the selection: a zone (search) → the zone panel; a planning-document
 * coverage area → the document panel; a cadastral parcel → the cadastral panel; a planned (urban)
 * parcel → the urban panel; nothing, a pending parcel search or a point without a parcel → the
 * empty state. The variant is keyed by the entity so each open starts fresh.
 */
import { useLocate } from "@/lib/api/hooks";
import { formatCoords } from "@/lib/format";
import { useT, type StringKey } from "@/lib/i18n";
import { nextSheet, useShell, type SheetState } from "@/lib/store";

import { CadastralPanel } from "../panel/cadastral-panel";
import { DocumentPanel } from "../panel/document-panel";
import { UrbanPanel } from "../panel/urban-panel";
import { ZonePanel } from "../panel/zone-panel";
import { IconPinSmall } from "../ui/icons";

const SHEET_LABEL: Record<SheetState, StringKey> = {
  peek: "sheet.peek",
  half: "sheet.half",
  full: "sheet.full",
};

export function InfoPanel() {
  const hidden = useShell((s) => s.panelHidden);
  const sheet = useShell((s) => s.sheet);
  const setSheet = useShell((s) => s.setSheet);
  const t = useT();

  return (
    <aside
      className={["panel", hidden && "hidden", `sheet-${sheet}`].filter(Boolean).join(" ")}
      id="panel"
      aria-label={t("panel.label")}
    >
      <button
        type="button"
        className="sheethandle"
        aria-label={t(SHEET_LABEL[sheet])}
        aria-expanded={sheet !== "peek"}
        onClick={() => setSheet(nextSheet(sheet))}
      />
      <PanelContent />
    </aside>
  );
}

function PanelContent() {
  const selection = useShell((s) => s.selection);
  if (selection?.kind === "zone") return <ZonePanel key={selection.id} zoneId={selection.id} name={selection.name} />;
  if (selection?.kind === "feature") {
    if (selection.type === "document") return <DocumentPanel key={`d${selection.id}`} documentId={selection.id} />;
    if (selection.type === "cadastral") return <CadastralPanel key={`c${selection.id}`} parcelId={selection.id} />;
    return <UrbanPanel key={`u${selection.id}`} urbanParcelId={selection.id} />;
  }
  return <PanelEmpty />;
}

const HINTS: StringKey[] = ["empty.hint.parcel", "empty.hint.plan", "empty.hint.zoom", "empty.hint.pan", "empty.hint.search"];

/** "Pick a parcel to begin" (wireframe `renderPanelEmpty`), with the pin note when a click landed on no parcel. */
export function PanelEmpty() {
  const selection = useShell((s) => s.selection);
  const pin = useShell((s) => s.pin);
  const point = selection?.kind === "point" ? selection.point : null;
  const { data: res } = useLocate(point);
  const noParcelHere = !!point && !!res && res.covered && !res.cadastral_parcel;
  const t = useT();

  return (
    <div className="pempty">
      <div className="ill">
        {/* eslint-disable-next-line @next/next/no-img-element -- SVG brand mark, sized by the stylesheet */}
        <img src="/brand/UrbanView_mark.svg" alt="UrbanView" />
      </div>
      <h3>{t("empty.title")}</h3>
      {noParcelHere && pin && (
        <div className="pinnote">
          <IconPinSmall />
          <span>
            {t("empty.pinDropped")} <b>{formatCoords(pin)}</b> {t("empty.noParcel")}
          </span>
        </div>
      )}
      <p>{t("empty.body")}</p>
      <div className="hintrow">
        {HINTS.map((key) => (
          <span key={key} className="chiphint">
            {t(key)}
          </span>
        ))}
      </div>
    </div>
  );
}
