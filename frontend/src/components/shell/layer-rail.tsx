"use client";

/**
 * Left layer rail (wireframe `.rail`, 206 px; collapses to the 46 px opener), driven by the layer
 * registry (`lib/layers.ts` → `LAYERS`). Layers are chosen by clicking their card (swatch + name),
 * each independently:
 *
 * - core layers only toast "Core layer — always visible";
 * - a dependent layer warns (▲ + "Needs … tap to turn on") while its required layer is off;
 * - a paid layer without the market entitlement stays locked: the click records
 *   `market_data_interest` and opens "Choose your access" (market data tagged "Unlocks this");
 * - choropleth cards show their field selector while on, and only one choropleth is on at a time
 *   (turning one on turns the other off, with a toast).
 *
 * Every real toggle emits `layer_toggled { layer_id, on }` (the published layer key).
 *
 * Names, titles, notes and toasts come from the shell's string table (current language). A card
 * whose source-layers the current published version lists with no features says "no data yet".
 */
import { Fragment } from "react";

import { useTrack } from "@/lib/analytics/react";
import { useTilesCurrent } from "@/lib/api/hooks";
import { useT } from "@/lib/i18n";
import { PARAM_METRICS, PRICE_METRICS } from "@/lib/map/classes";
import { LAYERS, analyticsLayerId, hasNoData, layerById, toggleLayer, type LayerDef } from "@/lib/layers";
import { useShell } from "@/lib/store";

import { IconChevronLeft, IconMenu } from "../ui/icons";
import { DependencyNote, LayerCard } from "../ui/layer-card";

import { ACCESS_LABEL, AccessModal } from "./access-modal";

function MetricChips<T extends string>({
  label,
  options,
  value,
  shortOf,
  onPick,
}: {
  label: string;
  options: readonly { key: T; short: string }[];
  value: T;
  shortOf: (key: T) => string;
  onPick: (key: T) => void;
}) {
  return (
    <div className="lyrmetric" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.key}
          type="button"
          role="radio"
          aria-checked={o.key === value}
          className={o.key === value ? "chiphint on" : "chiphint"}
          onClick={() => onPick(o.key)}
        >
          {shortOf(o.key)}
        </button>
      ))}
    </div>
  );
}

export function LayerRail() {
  const railOpen = useShell((s) => s.railOpen);
  const layers = useShell((s) => s.layers);
  const choropleth = useShell((s) => s.choropleth);
  const marketUnlocked = useShell((s) => s.marketUnlocked);
  const setRailOpen = useShell((s) => s.setRailOpen);
  const setLayers = useShell((s) => s.setLayers);
  const setLayer = useShell((s) => s.setLayer);
  const setChoropleth = useShell((s) => s.setChoropleth);
  const showToast = useShell((s) => s.showToast);
  const openModal = useShell((s) => s.openModal);
  const track = useTrack();
  const t = useT();
  const { data: tiles } = useTilesCurrent();
  const nameOf = (l: LayerDef) => t(`layer.${l.id}`);

  if (!railOpen) {
    return (
      <div className="rail collapsed" id="rail">
        <button type="button" className="railopen" title={t("rail.show")} aria-label={t("rail.show")} onClick={() => setRailOpen(true)}>
          <IconMenu />
        </button>
      </div>
    );
  }

  const toggle = (l: LayerDef) => {
    if (l.core) {
      showToast(t("toast.core"));
      return;
    }
    if (l.paid && !marketUnlocked) {
      track("market_data_interest", { trigger: "layer", layer_id: analyticsLayerId(l) });
      openModal({
        label: ACCESS_LABEL,
        wide: true,
        content: <AccessModal focus="market" context={{ layer_id: analyticsLayerId(l) }} />,
      });
      return;
    }
    const result = toggleLayer(layers, l.id);
    setLayers(result.layers);
    track("layer_toggled", { layer_id: analyticsLayerId(l), on: result.on });
    if (result.switchedOff) {
      const other = layerById(result.switchedOff);
      track("layer_toggled", { layer_id: analyticsLayerId(other), on: false });
      showToast(t("toast.oneHeatmap", { name: nameOf(other) }));
      return;
    }
    const req = l.requires ? layerById(l.requires) : null;
    if (result.on && req && !result.layers[req.id]) {
      showToast(t("toast.needsCadastre", { name: nameOf(l), req: nameOf(req) }));
    }
  };

  return (
    <div className="rail" id="rail" aria-label={t("rail.title")}>
      <div className="railhead">
        <b>{t("rail.title")}</b>
        <button type="button" className="railtog" title={t("rail.collapse")} aria-label={t("rail.collapseLabel")} onClick={() => setRailOpen(false)}>
          <IconChevronLeft />
        </button>
      </div>
      {LAYERS.map((l, i) => {
        const on = layers[l.id];
        const req = l.requires ? layerById(l.requires) : null;
        const dep = !!(req && !layers[req.id]);
        const locked = !!(l.paid && !marketUnlocked);
        const heading = i === 0 || LAYERS[i - 1].group !== l.group ? t(`group.${l.group}`) : null;
        const showFields = !!l.choropleth && on && !locked;
        const sub = !showFields
          ? undefined
          : l.choropleth === "param"
            ? t(`metric.${choropleth.param}.label`)
            : t(`price.${choropleth.price}.label`);
        const name = nameOf(l);
        const noData = hasNoData(l, tiles);
        const title = l.core
          ? t("rail.titleCore", { name })
          : locked
            ? t("rail.titleLocked", { name })
            : noData
              ? t("rail.titleNoData", { name })
              : req
                ? t("rail.titleDep", { name, req: nameOf(req) })
                : t("rail.titleToggle", { name });
        return (
          <Fragment key={l.id}>
            {heading && <div className="rlabel">{heading}</div>}
            <LayerCard
              name={name}
              swatch={l.swatch}
              on={on}
              core={l.core}
              dependencyMissing={dep}
              paidLocked={locked}
              sub={sub}
              note={noData ? t("rail.noData") : undefined}
              lockedLabel={t("rail.subscription")}
              title={title}
              onClick={() => toggle(l)}
            />
            {on && dep && req && (
              <DependencyNote
                requiredName={nameOf(req)}
                words={{ needs: t("rail.needs"), tap: t("rail.tapToTurnOn"), title: t("rail.turnOn", { name: nameOf(req) }) }}
                onClick={() => {
                  setLayer(req.id, true);
                  track("layer_toggled", { layer_id: analyticsLayerId(req), on: true });
                  showToast(t("toast.turnedOn", { name: nameOf(req) }));
                }}
              />
            )}
            {showFields && l.choropleth === "param" && (
              <MetricChips
                label={t("rail.field", { name })}
                options={PARAM_METRICS}
                value={choropleth.param}
                shortOf={(key) => t(`metric.${key}.short`)}
                onPick={(key) => setChoropleth("param", key)}
              />
            )}
            {showFields && l.choropleth === "price" && (
              <MetricChips
                label={t("rail.level", { name })}
                options={PRICE_METRICS}
                value={choropleth.price}
                shortOf={(key) => t(`price.${key}.short`)}
                onPick={(key) => setChoropleth("price", key)}
              />
            )}
          </Fragment>
        );
      })}
    </div>
  );
}
