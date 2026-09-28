"use client";

/**
 * Left layer rail (wireframe `.rail`, 206 px; collapses to the 46 px opener), driven by the layer
 * registry (`lib/layers.ts` → `LAYERS`). Layers are chosen by clicking their card (swatch + name),
 * each independently:
 *
 * - core layers only toast "Core layer — always visible";
 * - choropleth cards show their field selector while on; both heatmaps may be on together.
 *
 * Every real toggle emits `layer_toggled { layer_id, on }` (the published layer key).
 *
 * Names, titles, notes and toasts come from the shell's string table (current language). The card
 * never claims a layer the map is not drawing: a card whose source-layers the current published
 * version lists with no features says "no data yet"; a card that is on while the map is zoomed out
 * past the range its data is published for says "zoom in to see" (parcels are built from zoom 13,
 * the city framing is ~10.6), and turning such a layer on says so in a toast. The notes follow the
 * map live (`useLayerStates`: the same state the legend reads).
 */
import { Fragment } from "react";

import { useTrack } from "@/lib/analytics/react";
import { useTilesCurrent } from "@/lib/api/hooks";
import { useT } from "@/lib/i18n";
import { PARAM_METRICS, PRICE_METRICS } from "@/lib/map/classes";
import { LAYERS, analyticsLayerId, hasNoData, layerState, toggleLayer, type LayerDef } from "@/lib/layers";
import { useLayerStates } from "@/lib/map/use-layer-states";
import { useShell } from "@/lib/store";

import { IconChevronLeft, IconMenu } from "../ui/icons";
import { LayerCard } from "../ui/layer-card";

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
  const setRailOpen = useShell((s) => s.setRailOpen);
  const setLayers = useShell((s) => s.setLayers);
  const setChoropleth = useShell((s) => s.setChoropleth);
  const showToast = useShell((s) => s.showToast);
  const track = useTrack();
  const t = useT();
  const { data: tiles } = useTilesCurrent();
  const states = useLayerStates(tiles);
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
    // the store as it is at the click, not as it was at the last render: two clicks within one
    // frame must not toggle from the same snapshot
    const { layers, zoom } = useShell.getState();
    if (l.core) {
      showToast(t("toast.core"));
      return;
    }
    const result = toggleLayer(layers, l.id);
    setLayers(result.layers);
    track("layer_toggled", { layer_id: analyticsLayerId(l), on: result.on });
    if (!result.on) return;
    // turned on but nothing appears: say why
    const now = layerState(l.id, { layers: result.layers, zoom }, tiles);
    if (now === "no_data") showToast(t("toast.noData", { name: nameOf(l) }));
    else if (now === "zoom_in") showToast(t("toast.zoomIn", { name: nameOf(l) }));
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
        const heading = i === 0 || LAYERS[i - 1].group !== l.group ? t(`group.${l.group}`) : null;
        const showFields = !!l.choropleth && on;
        const sub = !showFields
          ? undefined
          : l.choropleth === "param"
            ? t(`metric.${choropleth.param}.label`)
            : t(`price.${choropleth.price}.label`);
        const name = nameOf(l);
        const noData = hasNoData(l, tiles);
        const zoomIn = states[l.id] === "zoom_in";
        const title = l.core
          ? t("rail.titleCore", { name })
          : noData
            ? t("rail.titleNoData", { name })
            : zoomIn
              ? t("rail.titleZoom", { name })
              : t("rail.titleToggle", { name });
        return (
          <Fragment key={l.id}>
            {heading && <div className="rlabel">{heading}</div>}
            <LayerCard
              name={name}
              swatch={l.swatch}
              on={on}
              core={l.core}
              sub={sub}
              note={noData ? t("rail.noData") : zoomIn ? t("rail.zoomIn") : undefined}
              noteKind={noData ? "no_data" : "zoom_in"}
              title={title}
              onClick={() => toggle(l)}
            />
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
