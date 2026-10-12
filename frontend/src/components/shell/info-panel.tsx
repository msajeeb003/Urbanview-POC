"use client";

/**
 * Right information panel (wireframe `.panel`, 392 px, scrolling body under a sticky header)
 * beside the map. The map is never replaced. The visitor folds the panel away with the chevron
 * on its left edge and unfolds it from the 34 px strip that stays (the rail's pattern); a new
 * selection unfolds it. An uncovered location keeps it open: the empty state then says the place
 * is not covered yet (the client's review of 2026-10-12).
 *
 * What it shows follows the selection: a zone (search) → the zone panel; a planning-document
 * coverage area → the document panel; an urban block → the block panel; a cadastral parcel → the
 * cadastral panel; a planned (urban) parcel → the urban panel; nothing, a pending parcel search or
 * a point without a parcel → the empty state. The variant is keyed by the entity so each open
 * starts fresh.
 */
import { useLocate, useMunicipality } from "@/lib/api/hooks";
import { formatCoords } from "@/lib/format";
import { useT, type StringKey } from "@/lib/i18n";
import { useShell } from "@/lib/store";

import { BlockPanel } from "../panel/block-panel";
import { CadastralPanel } from "../panel/cadastral-panel";
import { DocumentPanel } from "../panel/document-panel";
import { UrbanPanel } from "../panel/urban-panel";
import { ZonePanel } from "../panel/zone-panel";
import { IconChevronLeft, IconChevronRight, IconPinSmall } from "../ui/icons";

export function InfoPanel() {
  const hidden = useShell((s) => s.panelHidden);
  const collapsed = useShell((s) => s.panelCollapsed);
  const setPanelCollapsed = useShell((s) => s.setPanelCollapsed);
  const t = useT();

  if (collapsed) {
    return (
      <aside className="panel collapsed" id="panel" aria-label={t("panel.label")}>
        <button
          type="button"
          className="panelopen"
          title={t("panel.show")}
          aria-label={t("panel.show")}
          onClick={() => setPanelCollapsed(false)}
        >
          <IconChevronLeft />
        </button>
      </aside>
    );
  }

  return (
    <aside
      className={hidden ? "panel hidden" : "panel"}
      id="panel"
      aria-label={t("panel.label")}
    >
      <button
        type="button"
        className="paneltog"
        title={t("panel.hide")}
        aria-label={t("panel.hide")}
        onClick={() => setPanelCollapsed(true)}
      >
        <IconChevronRight />
      </button>
      <PanelContent />
    </aside>
  );
}

function PanelContent() {
  const selection = useShell((s) => s.selection);
  if (selection?.kind === "zone") return <ZonePanel key={selection.id} zoneId={selection.id} name={selection.name} />;
  if (selection?.kind === "feature") {
    if (selection.type === "document") return <DocumentPanel key={`d${selection.id}`} documentId={selection.id} />;
    if (selection.type === "block") return <BlockPanel key={`b${selection.id}`} blockId={selection.id} />;
    if (selection.type === "cadastral") return <CadastralPanel key={`c${selection.id}`} parcelId={selection.id} />;
    return <UrbanPanel key={`u${selection.id}`} urbanParcelId={selection.id} />;
  }
  return <PanelEmpty />;
}

const HINTS: StringKey[] = ["empty.hint.parcel", "empty.hint.plan", "empty.hint.zoom", "empty.hint.pan", "empty.hint.search"];

/**
 * "Pick a parcel to begin" (wireframe `renderPanelEmpty`), with the pin note when a click landed on
 * no parcel. A place no adopted plan covers keeps the panel open and the empty state becomes "Not
 * covered yet" with the pin note "— no adopted plan published here yet." / "— outside Podgorica."
 * (the client's review of 2026-10-12; before, the panel closed for 2.6 s). Neutral, never an
 * error.
 */
export function PanelEmpty() {
  const selection = useShell((s) => s.selection);
  const pin = useShell((s) => s.pin);
  const point = selection?.kind === "point" ? selection.point : null;
  const { data: res } = useLocate(point);
  const { data: profile } = useMunicipality();
  const t = useT();
  const uncovered = !!point && !!res && !res.covered;
  const outside = uncovered && res.coverage.reason === "outside_municipality" && !!profile;
  const note = !point || !res
    ? null
    : !res.covered
      ? outside
        ? t("empty.outside", { name: profile!.name })
        : t("empty.noPlan")
      : !res.cadastral_parcel && !res.urban_parcel
        ? t("empty.noParcel")
        : null;
  const body = !uncovered
    ? t("empty.body")
    : outside
      ? t("empty.outsideBody", { name: profile!.name })
      : t("empty.uncoveredBody");

  return (
    <div className={uncovered ? "pempty uncovered" : "pempty"}>
      <div className="ill">
        {/* eslint-disable-next-line @next/next/no-img-element -- SVG brand mark, sized by the stylesheet */}
        <img src="/brand/UrbanView_mark.svg" alt="UrbanView" />
      </div>
      <h3>{uncovered ? t("empty.uncoveredTitle") : t("empty.title")}</h3>
      {note && pin && (
        <div className="pinnote">
          <IconPinSmall />
          <span>
            {t("empty.pinDropped")} <b>{formatCoords(pin)}</b> {note}
          </span>
        </div>
      )}
      <p>{body}</p>
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
