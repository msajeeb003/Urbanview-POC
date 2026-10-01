"use client";

/**
 * "◐ Test your own assumptions" (wireframe `marketHTML` `.assum`): three range inputs —
 * construction €/m², sale price €/m², saleable % — starting from the zone's defaults in the
 * payload's `assumptions` (never hard-coded), each marked "default", or "your assumption" with
 * the default next to it, the other rates in use and their source, the market version and the
 * date it applies from, and "Reset to defaults". Edits go to the store (`assumptionEdits`, in
 * memory: a reload returns to the defaults); the market section recalculates from them. An edit
 * back on the default value stops being an edit, so Reset and "slide back" agree. Edits send no
 * event: the plan's 13 events have none for them.
 */
import { useId } from "react";

import {
  SLIDERS,
  defaultsOf,
  fromSlider,
  hasEdits,
  sliderBounds,
  toSlider,
  type EditKey,
  type OutOfRange,
} from "@/lib/assumptions";
import type { UrbanPanel } from "@/lib/api/types";
import { formatDate, formatEur } from "@/lib/format";
import { useLang, useT } from "@/lib/i18n";
import { useShell } from "@/lib/store";

import { useVersionName } from "./panel-parts";

function show(key: EditKey, value: number): string {
  return key === "saleable_share" ? `${Math.round(value * 100)}%` : formatEur(value);
}

export function AssumptionSandbox({
  data,
  errors,
}: {
  data: UrbanPanel;
  errors: Partial<Record<EditKey, OutOfRange>>;
}) {
  const id = useId();
  const t = useT();
  const { lang } = useLang();
  const edits = useShell((s) => s.assumptionEdits);
  const setAssumptionEdit = useShell((s) => s.setAssumptionEdit);
  const resetAssumptions = useShell((s) => s.resetAssumptions);
  const a = data.assumptions;
  const defaults = defaultsOf(a);
  const marketAvailable = data.market_inputs?.available !== false && !!data.engine;
  const edited = hasEdits(edits);
  const sourceDate = a?.market_source_date ? formatDate(a.market_source_date, lang) : null;
  const versionDate = a?.data_version_date ? formatDate(a.data_version_date, lang) : null;
  const versionName = useVersionName(a?.data_version);
  const market = a?.market_version;
  const marketFrom = market?.effective_from ? formatDate(market.effective_from, lang) : null;
  const rates = [
    a?.land_rate_eur_m2 != null ? t("asm.land", { value: formatEur(a.land_rate_eur_m2) }) : null,
    a?.design_documentation_eur_m2 != null ? t("asm.design", { value: formatEur(a.design_documentation_eur_m2) }) : null,
  ].filter(Boolean);
  // one line about market data: where it comes from, or that the zone has none yet
  const marketLine = a?.market_source
    ? [
        t("asm.marketData", { source: a.market_source }) + (sourceDate ? ` · ${sourceDate}` : ""),
        market ? t("asm.version", { version: market.version }) + (marketFrom ? `, ${t("asm.appliesFrom", { date: marketFrom })}` : "") : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : t("asm.noMarket");

  return (
    <div className="assum">
      <div className="ah">{t("asm.title")}</div>
      {SLIDERS.map((slider) => {
        const def = defaults[slider.key];
        const isEdited = edits[slider.key] !== undefined;
        const value = isEdited ? edits[slider.key]! : def;
        const bounds = sliderBounds(slider, def);
        const inputId = `${id}-${slider.key}`;
        const error = errors[slider.key];
        const clamped = value == null ? bounds.min : Math.min(bounds.max, Math.max(bounds.min, toSlider(slider.key, value)));
        return (
          <div key={slider.key}>
            <div className="arow">
              <label htmlFor={inputId}>
                {t(`asm.slider.${slider.key}`)}
                <span className={isEdited ? "atag yours" : "atag"}>
                  {isEdited
                    ? def != null
                      ? t("asm.yoursDefault", { value: show(slider.key, def) })
                      : t("asm.yours")
                    : t("asm.default")}
                </span>
              </label>
              <input
                id={inputId}
                type="range"
                min={bounds.min}
                max={bounds.max}
                step={slider.step}
                value={clamped}
                disabled={!marketAvailable || value == null}
                aria-invalid={error ? true : undefined}
                aria-valuetext={value != null ? show(slider.key, value) : t("asm.notAvailable")}
                onChange={(e) => {
                  const next = fromSlider(slider.key, Number(e.target.value));
                  // back on the default value: no longer an edit
                  setAssumptionEdit(slider.key, def != null && next === def ? undefined : next);
                }}
              />
              <span className="av">{value != null ? show(slider.key, value) : "—"}</span>
            </div>
            {error && (
              <div className="aerr" role="alert">
                {t("asm.outOfRange", error)}
              </div>
            )}
          </div>
        );
      })}
      <div className="assumsrc">
        {rates.length > 0 && (
          <>
            {rates.join(" · ")}
            <br />
          </>
        )}
        {marketLine}
        <br />
        {t("asm.formula", { formula: a?.formula_version ?? "", version: versionName ?? "" })}
        {versionDate ? ` · ${versionDate}` : ""}
      </div>
      <button
        type="button"
        className="assumreset"
        aria-disabled={!edited}
        onClick={() => {
          if (!edited) return;
          resetAssumptions();
        }}
      >
        {t("asm.reset")}
      </button>
    </div>
  );
}
